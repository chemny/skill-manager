"""One bounded comparison engine for CLI and web; each worker owns its SQLite connection."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading

import maintenance as core

NETWORK_SLOTS = threading.BoundedSemaphore(3)


@contextmanager
def repository_archives(manager):
    missing = object()
    names = ('_repository_archive_home', '_archive_cache', '_remote_json_cache')
    previous = {name:getattr(manager,name,missing) for name in names}
    with tempfile.TemporaryDirectory(prefix='asm-update-repos-') as directory:
        manager._repository_archive_home = Path(directory).resolve()
        manager._archive_cache = {}
        manager._remote_json_cache = {}
        try:
            yield
        finally:
            for name, value in previous.items():
                if value is missing: delattr(manager, name)
                else: setattr(manager, name, value)


def quota(source):
    return ('github',) if source.get('provider') == 'github' else (
        source.get('provider'), source.get('channel'), source.get('url', '').split('/api/')[0])


def run(manager, ids, *, force=False, progress=None, run_id=None, scope='partial'):
    manager._targeted_checks = True
    with core.source_batch(manager):
        return _run(manager, ids, force=force, progress=progress, run_id=run_id, scope=scope)


def _run(manager, ids, *, force=False, progress=None, run_id=None, scope='partial'):
    started_at = core.stamp()
    if progress:
        progress({'id':run_id, 'scope':scope, 'started_at':started_at, 'state':'running',
                  'total':None, 'completed':0, 'failed':0, 'cached':0, 'remote':0, 'skipped':0}, [])
    rows, skipped, seen, identities = [], [], set(), {}
    selected = [manager.select(i) for i in dict.fromkeys(ids)]
    if scope == 'failed':
        def failed(row):
            stored = manager.db.execute('select last_check from maintenance_items where id=?', (row['id'],)).fetchone()
            value = json.loads(stored['last_check'] or '{}') if stored else {}
            return value.get('status') == 'check_failed' or bool(value.get('attempt_failed'))
        selected = [row for row in selected if failed(row)]
    sources = {r['id']:manager.source(r) for r in selected}
    manager._check_references = {}
    for row in selected:
        manager._check_references.setdefault(str(core.absolute(row['path']).resolve()),[]).append(row)
    selected.sort(key=lambda r:(not bool(sources[r['id']]),
                  str(core.absolute(r['path'])) != str(core.absolute(r['path']).resolve())))
    for row in selected:
        real = str(core.absolute(row['path']).resolve())
        source = sources[row['id']]
        if source:
            from update_providers import identity
            identities.setdefault(real, set()).add(identity(source))
    for row in selected:
        real = str(core.absolute(row['path']).resolve())
        if real in seen: continue
        seen.add(real)
        source = sources[row['id']]
        reason = ('共享引用的更新来源不一致，需先核对' if len(identities.get(real, ())) > 1
                  else '本地 Skill 不存在' if row['kind'] != 'skill' or not core.absolute(row['path']).exists()
                  else '缺少可靠的更新来源' if not source
                  else '更新来源暂不支持内容对比' if source.get('provider') not in ('github','git','skillhub') else None)
        if reason: skipped.append({'id':row['id'], 'reason':reason})
        else: rows.append(row)
    found = {}
    if not force:
        from update_checks import compare
        for row in rows:
            try: checked = compare(manager, row, cache_only=True)
            except (OSError, ValueError): checked = None
            if checked is not None:
                found[row['id']] = {**checked, 'name':row['name']}
    groups = {}
    for row in rows:
        if row['id'] in found: continue
        source = sources[row['id']]
        # Repository work units share immutable metadata and verified downloads.
        key = (source['provider'], source.get('channel'), source['url'])
        groups.setdefault(key, []).append(row)
    run_state = {'id':run_id or core.uid('check'), 'scope':scope, 'started_at':started_at,
                 'prepared_at':core.stamp(),
                 'state':'running', 'total':len(rows)+len(skipped), 'completed':len(skipped),
                 'failed':0, 'cached':0, 'remote':0, 'skipped':len(skipped)}
    limited = {quota(sources[i]) for i, c in found.items()
               if c.get('code') == 'remote_rate_limit' and c.get('retry_after_seconds', 0) > 0}
    github_state = {'limited':any(sources[i].get('provider')=='github' and c.get('code')=='remote_rate_limit' and c.get('rate_limit_scope') not in ('github-archive','github-git') for i,c in found.items())}
    github_ids = [r['id'] for r in rows if sources[r['id']].get('provider') == 'github']
    for stored in manager.db.execute('select last_check from maintenance_items where id in (select value from json_each(?))', (json.dumps(github_ids),)):
        value = json.loads(stored['last_check'] or '{}')
        deadline = value.get('api_retry_at') or (value.get('retry_at') if value.get('code') == 'remote_rate_limit' and value.get('rate_limit_scope') not in ('github-archive','github-git') else None)
        try:
            if dt.datetime.fromisoformat(deadline) > dt.datetime.now(dt.timezone.utc):
                github_state.update(limited=True, retry_at=max(deadline, github_state.get('retry_at', '')))
        except (ValueError, TypeError): pass
    # A REST cooldown still permits verified Git HTTPS transport to the same repository.
    for key in list(found):
        if sources[key].get('provider') == 'github' and found[key].get('code') == 'remote_rate_limit' and found[key].get('rate_limit_scope') not in ('github-archive','github-git'):
            found.pop(key)
            row = next(r for r in rows if r['id'] == key)
            source = sources[key]
            groups.setdefault((source['provider'], source.get('channel'), source['url']), []).append(row)
    # Selected-directory checks never contact the archive endpoint, even in its cooldown.
    limited.discard(('github',))
    mutex = threading.Lock()
    network_context={name:{} for name in ('_remote_json_cache','_github_refs','_github_ref_errors',
                                         '_selected_git_repos','_loaded_git_blobs')}
    def publish():
        results = [found[r['id']] for r in rows if r['id'] in found]
        run_state.update(completed=len(results)+len(skipped), skipped=len(skipped),
                         failed=sum(c['status']=='check_failed' or bool(c.get('attempt_failed')) for c in results),
                         cached=sum(bool(c.get('cache_hit')) for c in results),
                         remote=sum(bool(c.get('remote_requested', True)) for c in results))
        if progress: progress(dict(run_state), results)
        return results
    publish()
    with tempfile.TemporaryDirectory(prefix='asm-check-repos-') as directory:
        def worker(group):
            with NETWORK_SLOTS:
                m = core.Manager(manager.a)
                try:
                    m._source_context = manager._source_context
                    m._check_references = manager._check_references
                    m.r._batch_update_channels = manager.r._batch_update_channels
                    m._github_api_state = github_state
                    m._github_git_fallback = True
                    m._use_check_cache = True
                    m._targeted_checks = True
                    m._force_check = force
                    m._repository_archive_home = Path(directory).resolve()
                    for name,values in network_context.items():setattr(m,name,values)
                    from github_subset import prefetch
                    bundle=prefetch(m,group)
                    with mutex:
                        totals=run_state.setdefault('prefetch',{'bundles':0,'objects':0,'fallbacks':0,'bytes':0})
                        for key,value in bundle.items():totals[key]+=value
                    for row in group:
                        bucket = quota(sources[row['id']])
                        with mutex:
                            if bucket in limited:
                                skipped.append({'id':row['id'], 'reason':'该来源请求限流，稍后重试'})
                                publish()
                                continue
                        checked = m.check(row)
                        checked['name'] = row['name']
                        with mutex:
                            found[row['id']] = checked
                            if checked.get('code') == 'remote_rate_limit': limited.add(bucket)
                            publish()
                finally:
                    m.db.close()
        # Bound each work unit so one monorepo cannot occupy only one worker for the entire run.
        def execute(work):
            chunks=[group[start:start+8] for group in work for start in range(0,len(group),8)]
            with ThreadPoolExecutor(max_workers=min(3, max(1, len(chunks))), thread_name_prefix='asm-check') as pool:
                for future in as_completed([pool.submit(worker, group) for group in chunks]):
                    future.result()
        execute(groups.values())
        retry=[row for row in rows if row['id'] in found and found[row['id']].get('code')=='remote_unavailable'
               and found[row['id']].get('retryable', True)
               and quota(sources[row['id']]) not in limited
               and (found[row['id']]['status']=='check_failed' or found[row['id']].get('attempt_failed'))]
        if retry:
            run_state.update(first_pass_failed=run_state['failed'],retried=len(retry))
            from network_io import transient
            urls={sources[row['id']]['url'] for row in retry}
            # Only transient transport errors may be retried, once, after the first pass.
            # Permanent failures and all verified metadata remain untouched.
            for name in ('_github_ref_errors','_selected_git_repos'):
                cache=network_context[name]
                for key,error in list(cache.items()):
                    if key[0] in urls and isinstance(error,Exception) and transient(error):cache.pop(key)
            retry_groups={}
            for row in retry:
                source=sources[row['id']]
                retry_groups.setdefault((source['provider'],source.get('channel'),source['url']),[]).append(row)
            execute(retry_groups.values())
            run_state['retry_succeeded']=sum(found[r['id']]['status']!='check_failed' and not found[r['id']].get('attempt_failed') for r in retry)
            run_state['retry_resolved_transport']=sum(found[r['id']].get('code')!='remote_unavailable' for r in retry)
    results = publish()
    run_state.update(state='partial' if limited or run_state['failed'] else 'succeeded', finished_at=core.stamp())
    if progress: progress(dict(run_state), results)
    result = {'run':run_state, 'checks':results, 'skipped':skipped, 'failed':run_state['failed'],
              'note':'Checks compare confirmed refs, never bind guessed sources or update files.'}
    manager.db.execute('insert or replace into maintenance_check_runs values (?,?)',
                       (run_state['id'], json.dumps(result)))
    manager.db.commit()
    return result
