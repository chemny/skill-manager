"""On-demand project analyses; isolated workers never move project files."""
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import sys
import threading
import time

import maintenance as core
import space_cleanup as space

_lock = threading.Lock()
_stop = threading.Event()
PROJECT_SECONDS = 60
PROJECT_FILES = 100000
BATCH_SECONDS = 180


def stop():
    _stop.set()
    return {'requested': True}


def worker(script, home, root, exclusions, pipe):
    manager = None
    try:
        os.environ['ASM_HOME'] = home
        spec = importlib.util.spec_from_file_location('project_analysis_legacy', script)
        legacy = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = legacy
        spec.loader.exec_module(legacy)
        manager = core.Manager(legacy)
        manager.batch_progress = lambda done, total, message: pipe.send({'progress': message})
        report = space.scan(manager, root, exclude_roots=exclusions,
                            max_files=PROJECT_FILES, seconds=PROJECT_SECONDS * .75)
        pipe.send({'result': {'scan_id': report['id'], 'summary': report['summary'],
                              'policy_version': space.POLICY_VERSION, 'coverage': report['coverage'],
                              'issue_count': report['issue_count'], 'truncated': report['truncated'],
                              'analysis_limited': report['analysis_limited'],
                              'enumeration_incomplete': report['enumeration_incomplete'],
                              'references_incomplete': report['references_incomplete'],
                              'excluded_projects': report['exclude_roots']}})
    except Exception as exc:
        pipe.send({'error': str(exc)[:500]})
    finally:
        if manager:
            manager.db.close()
        pipe.close()


def run_worker(m, root, exclusions, deadline, notify):
    context = multiprocessing.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(str(m.a.__file__), str(m.home), root, exclusions, sender), daemon=True)
    try:
        process.start()
        sender.close()
        while True:
            if _stop.is_set() or getattr(m, 'cancel_requested', lambda: False)():
                return {'state': 'stopped', 'reason': '已停止，文件未改动'}
            if time.monotonic() >= deadline:
                return {'state': 'timeout', 'reason': '分析达到时间上限；文件未改动，可分析更小的目录'}
            if receiver.poll(.1):
                message = receiver.recv()
                if 'progress' in message:
                    notify(message['progress'])
                elif 'error' in message:
                    return {'state': 'failed', 'reason': message['error']}
                elif 'result' in message:
                    result = message['result']
                    partial = (result['truncated'] or result.get('analysis_limited')
                               or result.get('references_incomplete') or result.get('enumeration_incomplete'))
                    reason = ('已统计部分空间，可查看已完成结果' if result['truncated'] else
                              '空间统计完成，部分内容核对未完成' if result.get('analysis_limited') else
                              '空间统计完成，云端或未读取文件已保留' if result.get('references_incomplete') else '')
                    return {**result, 'state': 'partial' if partial else 'completed', 'reason': reason}
            elif not process.is_alive():
                return {'state': 'failed', 'reason': '分析进程意外结束，文件未改动'}
    except (EOFError, OSError) as exc:
        return {'state': 'failed', 'reason': '分析未完成：' + str(exc)[:160]}
    finally:
        receiver.close()
        sender.close()
        if process.pid:
            process.join(.2)
            if process.is_alive():
                process.terminate()
                process.join(.5)
            if process.is_alive():
                process.kill()
                process.join(.5)
            if not process.is_alive():
                process.close()


def analyze(m, payload):
    if not _lock.acquire(blocking=False):
        raise ValueError('项目分析正在进行，请等待完成或停止分析。')
    try:
        _stop.clear()
        if getattr(m, 'cancel_requested', lambda: False)():
            _stop.set()
        return _analyze(m, payload)
    finally:
        _lock.release()


def _analyze(m, payload):
    row = m.db.execute('select payload from maintenance_disk_scans where id=?', (payload.get('report_id'),)).fetchone()
    if not row:
        raise ValueError('项目列表已失效，请重新发现目录')
    report = json.loads(row[0])
    candidates = {p['path']: p for p in report['projects']}
    if payload.get('scope') == 'known':
        paths = [p['path'] for p in report['projects'] if p['source'] == 'codex']
    else:
        paths = payload.get('paths')
    if not isinstance(paths, list) or not paths or len(paths) > 2000 or any(not isinstance(p, str) or p not in candidates for p in paths):
        raise ValueError('请选择已发现的项目目录')
    paths = list(dict.fromkeys(paths))
    context_paths = payload.get('context_paths', paths)
    if (not isinstance(context_paths, list) or not context_paths or len(context_paths) > 2000
            or any(not isinstance(p, str) or p not in candidates for p in context_paths)
            or not set(paths).issubset(context_paths)):
        raise ValueError('项目分析范围已变化，请重新选择')
    context_paths = list(dict.fromkeys(context_paths))
    import project_space
    import project_duplicates
    previous = project_space.preference(m, 'batch', {})
    completed = previous.get('completed', []) if (previous.get('report_id') == report['id']
                and set(previous.get('paths', [])) == set(context_paths)) else []
    batch = {'report_id': report['id'], 'paths': context_paths, 'completed': completed, 'state': 'running'}
    project_space.save_preference(m, 'batch', batch)
    started = time.monotonic()
    results = []
    for index, root in enumerate(paths):
        if _stop.is_set() or time.monotonic() - started >= BATCH_SECONDS:
            break
        exclusions = [p for p in report.get('scope_paths', list(candidates)) if Path(root) in Path(p).parents]
        name = candidates[root]['name']
        getattr(m, 'project_current', lambda path: None)(root)
        def notify(message):
            space.progress(m, index, len(paths), f'{index + 1}/{len(paths)} · {name} · {message}')
        notify('准备分析')
        result = run_worker(m, root, exclusions, min(started + BATCH_SECONDS, time.monotonic() + PROJECT_SECONDS), notify)
        result.update(path=root, analyzed_at=core.stamp(), policy_version=space.POLICY_VERSION, read_only=True)
        m.db.execute('insert or replace into maintenance_project_analyses values(?,?)', (root, json.dumps(result)))
        m.db.commit()
        results.append(result)
        batch['completed'] = list(dict.fromkeys(completed + [r['path'] for r in results]))
        project_space.save_preference(m, 'batch', batch)
        getattr(m, 'project_result', lambda rows: None)(results)
        space.progress(m, index + 1, len(paths), f'已处理 {index + 1}/{len(paths)} 个项目')
        if _stop.is_set():
            break
    remaining = paths[len(results):]
    comparison = project_duplicates.build(m, report, context_paths, min(started + BATCH_SECONDS,
        time.monotonic() + PROJECT_SECONDS), lambda: _stop.is_set() or getattr(m, 'cancel_requested', lambda: False)())
    updated = []
    for root in context_paths:
        row = m.db.execute('select payload from maintenance_project_analyses where path=?', (root,)).fetchone()
        value = json.loads(row[0]) if row else {}
        if value.get('policy_version') != space.POLICY_VERSION:
            continue
        if root in comparison['scans']:
            coverage = comparison['coverage'][root]
            value['cross_coverage'] = coverage
            value.setdefault('coverage', {})['stages'] = {
                'space': not (value.get('truncated') or value.get('enumeration_incomplete')),
                'duplicates': not (coverage['hash_skipped'] or comparison['missing']),
                'references': not value.get('references_incomplete', True)}
            value['comparison_id'] = comparison['id']
            value['analysis_limited'] = bool(coverage['hash_skipped'])
            partial = (value.get('truncated') or value.get('enumeration_incomplete')
                       or value.get('references_incomplete') or coverage['hash_skipped'] or comparison['missing'])
            value['state'] = 'partial' if partial else 'completed'
            value['reason'] = '部分内容尚未核对，已保留未核对文件' if partial else ''
            m.db.execute('insert or replace into maintenance_project_analyses values(?,?)', (root, json.dumps(value)))
        updated.append(value)
    m.db.commit()
    getattr(m, 'project_result', lambda rows: None)(updated)
    batch.update(state='partial' if remaining else 'completed', remaining=remaining)
    project_space.save_preference(m, 'batch', batch)
    return {'report_id': report['id'], 'analyses': updated, 'completed': len(results), 'total': len(paths),
            'comparison': {k: v for k, v in comparison.items() if k not in ('candidates', 'coverage')},
            'remaining': remaining, 'state': 'partial' if remaining or any(r['state'] != 'completed' for r in updated) else 'complete',
            'elapsed_seconds': round(time.monotonic() - started, 2), 'read_only': True}
