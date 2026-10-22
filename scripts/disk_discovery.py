"""Lightweight project-directory discovery; never enumerates project files."""
import collections
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time

import maintenance as core
import space_cleanup as space

_lock = threading.Lock()
_stop = threading.Event()
LIMITS = {'seconds': 120, 'directories': 50000, 'depth': 25}
SYSTEM = {'/System', '/dev', '/proc', '/sys', '/Volumes', '/Network', '/net',
          '/bin', '/sbin', '/cores', '/private', '/usr', '/Library',
          '/opt/homebrew', '/opt/local'}
SKIP = {'.ssh', '.aws', '.gnupg', '.kube', '.config', 'Keychains', 'Library',
        '.git', '.svn', '.hg', '.Trash', '.Trashes', 'node_modules', '.venv',
        'venv', 'site-packages', '__pycache__', '.pytest_cache', '.mypy_cache',
        '.ruff_cache', '.npm', '.cache', '.cargo', '.rustup', '.gradle', '.m2',
        '.nuget', '.vscode', '.agents', '.claude', '.hermes', '.codex'}
EXACT_MARKERS = {'.git', 'pyproject.toml', 'package.json', 'Cargo.toml', 'go.mod', 'AGENTS.md'}


def local_roots():
    if os.name == 'posix':
        return [Path('/')]
    import ctypes
    return [Path(f'{c}:\\') for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
            if ctypes.windll.kernel32.GetDriveTypeW(f'{c}:\\') == 3]


def codex_projects(home):
    projects, issues = set(), []
    for database in (home / '.codex').glob('state_*.sqlite'):
        try:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                for row in db.execute('select distinct cwd from threads limit 10000'):
                    if isinstance(row[0], str) and Path(row[0]).is_absolute():
                        projects.add(os.path.normpath(row[0]))
        except sqlite3.Error:
            issues.append({'path': str(database), 'reason': '无法读取 Codex 项目目录元数据'})
    return projects, issues


def excluded(path, home, manager_home):
    if str(path) in SYSTEM:
        return '系统、临时数据或非默认扫描卷'
    if path == manager_home or manager_home in path.parents:
        return '管理器自身数据'
    if path.name in SKIP:
        return '内部数据、缓存或依赖目录'
    if path.suffix.lower() in {'.app', '.photoslibrary', '.photolibrary', '.framework', '.bundle'}:
        return '应用或媒体资料库包'
    return None


def stop():
    _stop.set()
    return {'requested': True}


def latest(m):
    try:
        row = m.db.execute('select payload from maintenance_disk_scans order by created_at desc, rowid desc limit 1').fetchone()
    except sqlite3.OperationalError:
        row = None
    report = json.loads(row[0]) if row else None
    if report:
        saved = {r['path']: json.loads(r['payload']) for r in m.db.execute('select * from maintenance_project_analyses')}
        for project in report['projects']:
            if project['path'] in saved:
                project['analysis'] = saved[project['path']]
    return {'report': report}


def scan(m, roots=None, home=None, limits=None):
    if not _lock.acquire(blocking=False):
        raise ValueError('项目目录发现正在进行，请等待完成或停止扫描。')
    try:
        _stop.clear()
        return _scan(m, roots or local_roots(), home or Path.home(), {**LIMITS, **(limits or {})})
    finally:
        _lock.release()


def _scan(m, roots, home, limits):
    started = time.monotonic()
    known, issues = codex_projects(home)
    counts = collections.Counter()
    visited, project_ids = set(), set()
    projects = []
    dirs = 0
    stop_reason = None
    last_progress = 0
    roots = [p.absolute() for p in roots]
    allowed_devices = {p.stat().st_dev for p in roots if p.exists()}
    if sys.platform == 'darwin' and roots == [Path('/')]:
        allowed_devices.add(Path('/System/Volumes/Data').stat().st_dev)

    def note(path, reason):
        counts[reason] += 1
        if len(issues) < 500:
            issues.append({'path': str(path), 'reason': reason})

    def add_project(path, identity, source, markers=()):
        if identity in project_ids:
            note(path, '重复项目入口已合并')
            return
        project_ids.add(identity)
        projects.append({'path': str(path), 'name': path.name or str(path),
                         'source': source, 'markers': sorted(markers),
                         'evidence': 'Codex 线程工作目录记录' if source == 'codex' else '项目标识：' + '、'.join(sorted(markers)),
                         'analysis_state': 'not_analyzed',
                         'reason': '仅发现目录；占用空间和清理建议尚未分析'})

    for raw in sorted(known):
        if _stop.is_set():
            break
        path = Path(raw)
        if path == home or path == Path(path.anchor):
            continue
        reason = excluded(path, home, m.home)
        if reason:
            note(path, reason)
            continue
        try:
            with space.directory(path) as fd:
                s = os.fstat(fd)
                if s.st_dev not in allowed_devices:
                    note(path, '已记录项目位于其他挂载卷')
                    continue
                identity = (s.st_dev, s.st_ino)
                visited.add(identity)
                add_project(path, identity, 'codex')
                dirs += 1
        except (OSError, ValueError):
            note(path, '已记录项目不可访问、已移除或路径包含链接')

    stack = [(p, 0) for p in roots]
    while stack:
        if _stop.is_set() or time.monotonic() - started >= limits['seconds'] or dirs >= limits['directories']:
            stop_reason = '用户停止' if _stop.is_set() else '达到目录发现的时间或数量上限'
            break
        folder, depth = stack.pop()
        reason = excluded(folder, home, m.home)
        if reason:
            note(folder, reason)
            continue
        try:
            with space.directory(folder) as fd:
                s = os.fstat(fd)
                if s.st_dev not in allowed_devices:
                    note(folder, '其他挂载卷未扫描')
                    continue
                identity = (s.st_dev, s.st_ino)
                if identity in visited:
                    continue
                visited.add(identity)
                dirs += 1
                markers, children = set(), []
                # Names and entry types only: no regular-file stat, open or hash.
                with os.scandir(fd) as entries:
                    for entry in entries:
                        if entry.name in EXACT_MARKERS or entry.name.endswith(('.xcodeproj', '.sln')):
                            markers.add(entry.name)
                        if entry.is_dir(follow_symlinks=False):
                            children.append(folder / entry.name)
                        elif entry.is_symlink():
                            note(folder / entry.name, '符号链接未跟随')
                        if _stop.is_set():
                            break
                if markers and folder != home and folder not in roots:
                    add_project(folder, identity, 'marker', markers)
                    continue
                for child in children:
                    if depth >= limits['depth']:
                        note(child, '达到目录发现深度上限')
                    else:
                        stack.append((child, depth + 1))
        except (OSError, ValueError) as exc:
            note(folder, '目录无法访问：' + str(exc)[:120])
        now = time.monotonic()
        if now - last_progress > .25:
            space.progress(m, dirs, None, f'已检查 {dirs:,} 个目录 · 发现 {len(projects)} 个项目位置')
            last_progress = now

    result = {'id': core.uid('disk'), 'mode': 'directory_discovery', 'created_at': core.stamp(),
              'elapsed_seconds': round(time.monotonic() - started, 2),
              'state': 'partial' if stop_reason else 'completed', 'stop_reason': stop_reason,
              'roots': [str(p) for p in roots], 'read_only': True,
              'projects': sorted(projects, key=lambda p: (p['source'] != 'codex', p['path'].casefold())),
              'issues': issues, 'issue_counts': dict(counts),
              'pending_locations': [str(p) for p, _ in stack[:200]], 'limits': limits,
              'summary': {'directories': dirs, 'projects': len(projects),
                          'known_projects': sum(p['source'] == 'codex' for p in projects),
                          'marker_projects': sum(p['source'] == 'marker' for p in projects),
                          'files_analyzed': 0, 'content_bytes_read': 0},
              'note': '仅检查目录条目、项目标识名称和 Codex 工作目录元数据；不读取项目文件内容，不计算大小、哈希或清理建议。'}
    m.db.execute('insert into maintenance_disk_scans values(?,?,?)', (result['id'], result['created_at'], json.dumps(result)))
    m.db.commit()
    return result


def dispatch(m, payload):
    if payload['action'] == 'disk-stop':
        return stop()
    if payload['action'] == 'disk-report':
        return latest(m)
    return scan(m)
