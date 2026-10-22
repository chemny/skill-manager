"""Agent project scopes and metadata-only space statistics. Never moves files."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from urllib.parse import unquote, urlparse

import maintenance as core
import space_cleanup as space

_lock = threading.Lock()
_stop = threading.Event()
SCAN_SECONDS = 300
PROJECT_LIMIT = 2000
COLLECTIONS = {'codex', 'inbox', 'projects', 'workspaces', 'workspace', 'outputs'}
PROJECT_MARKERS = {'.git', 'AGENTS.md', 'pyproject.toml', 'package.json', 'Cargo.toml', 'go.mod'}
AGENTS = [('codex', 'Codex', '.codex'), ('claude_code', 'Claude Code', '.claude'),
          ('cursor', 'Cursor', '.cursor'), ('windsurf', 'Windsurf', '.codeium/windsurf'),
          ('openclaw', 'OpenClaw', '.openclaw'), ('hermes', 'Hermes', '.hermes')]


def project_id(path):
    return 'project-' + hashlib.sha256(str(path).encode()).hexdigest()[:24]


def read_json(path):
    try:
        if path.stat().st_size > 2 * 1024 * 1024 or path.is_symlink():
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def discover(home):
    """Read project-location metadata only; no session messages or project contents."""
    records, issues = [], []
    def add(agent, raw):
        if isinstance(raw, str) and Path(raw).expanduser().is_absolute():
            records.append((agent, raw))
    codex = Path(os.environ.get('CODEX_HOME', home / '.codex')).expanduser().absolute()
    for database in list(codex.glob('state_*.sqlite'))[:20]:
        try:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                for (cwd,) in db.execute('select distinct cwd from threads limit 2000'):
                    add('codex', cwd)
        except sqlite3.Error:
            issues.append({'agent': 'codex', 'reason': '部分项目记录无法读取'})
    try:
        import tomllib
        config = codex / 'config.toml'
        if config.stat().st_size <= 2 * 1024 * 1024:
            for path in tomllib.loads(config.read_text()).get('projects', {}):
                add('codex', path)
    except (OSError, ValueError, ImportError, TypeError):
        pass
    if (codex / 'generated_images').is_dir():
        add('codex', str(codex / 'generated_images'))
    claude_projects = read_json(home / '.claude.json').get('projects', {})
    for path in claude_projects if isinstance(claude_projects, dict) else []:
        add('claude_code', path)
    for agent, app in [('cursor', 'Cursor'), ('windsurf', 'Windsurf')]:
        storage = home / 'Library/Application Support' / app / 'User/workspaceStorage'
        for metadata in list(storage.glob('*/workspace.json'))[:2000]:
            value = read_json(metadata)
            folder = value.get('folder')
            if isinstance(folder, str) and urlparse(folder).scheme == 'file':
                add(agent, unquote(urlparse(folder).path))
            workspace = value.get('workspace')
            if isinstance(workspace, str) and urlparse(workspace).scheme == 'file':
                workspace_path = Path(unquote(urlparse(workspace).path))
                folders = read_json(workspace_path).get('folders', [])
                for entry in folders if isinstance(folders, list) else []:
                    if isinstance(entry, dict) and isinstance(entry.get('path'), str):
                        add(agent, str(workspace_path.parent / entry['path']))
    config = read_json(home / '.openclaw/openclaw.json').get('agents', {})
    if isinstance(config, dict):
        defaults = config.get('defaults', {})
        if isinstance(defaults, dict):
            add('openclaw', defaults.get('workspace'))
        for entry in config.get('list', []) if isinstance(config.get('list', []), list) else []:
            if isinstance(entry, dict):
                add('openclaw', entry.get('workspace'))
    return records, issues


def preference(m, key, default=None):
    row = m.db.execute('select payload from maintenance_space_preferences where key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def save_preference(m, key, value):
    m.db.execute('insert or replace into maintenance_space_preferences values(?,?)', (key, json.dumps(value)))
    m.db.commit()


def partition(m, projects, reserved, issues):
    """Split known workspace collections using directory names, never file contents."""
    queue = [(p, 0) for p in list(projects.values())]
    visited, deadline = set(), time.monotonic() + 8
    while queue and len(projects) < PROJECT_LIMIT:
        if time.monotonic() >= deadline:
            issues.append({'reason': '部分项目目录尚未识别，可再次扫描'})
            break
        project, depth = queue.pop(0)
        root = Path(project['path'])
        if root in visited or depth > 5:
            continue
        visited.add(root)
        try:
            with space.directory(root) as fd, os.scandir(fd) as entries:
                children, names = [], set()
                for entry in entries:
                    names.add(entry.name)
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith('.'):
                        children.append(root / entry.name)
            collection = (root.name.casefold() in COLLECTIONS or re.fullmatch(r'\d{4}-\d{2}-\d{2}', root.name)
                          or not names.intersection(PROJECT_MARKERS) and any(
                              p.name.casefold() in {'inbox', 'projects', 'workspaces'} for p in children))
            if not collection:
                continue
            for path in sorted(children):
                if len(projects) >= PROJECT_LIMIT:
                    break
                if space.blocked(path, reserved) or (path / 'SKILL.md').exists():
                    continue
                with space.directory(path) as fd:
                    identity = os.fstat(fd)
                if identity.st_dev != project['identity'][0]:
                    continue
                key = project_id(path)
                row = projects.setdefault(key, {'id': key, 'path': str(path), 'name': path.name,
                    'agents': [], 'source': project['source'], 'discovery': 'workspace_layout',
                    'identity': [identity.st_dev, identity.st_ino]})
                row['agents'] = sorted(set(row['agents']) | set(project['agents']))
                queue.append((row, depth + 1))
        except (OSError, ValueError):
            issues.append({'path': str(root), 'reason': '部分项目位置无法识别'})


def catalog(m, home=None):
    home = home or Path.home()
    records, issues = discover(home)
    for item in preference(m, 'manual', []):
        if isinstance(item, dict) and item.get('agent') in {key for key, _, _ in AGENTS} and isinstance(item.get('path'), str):
            records.append((item['agent'], item['path']))
    reserved = space.protected(m, home)
    projects = {}
    for agent, raw in records[:PROJECT_LIMIT * 4]:
        try:
            path = Path(raw).expanduser().resolve(strict=True)
            if path in (home, Path(path.anchor)) or path == m.home or m.home in path.parents:
                continue
            if space.blocked(path, reserved):
                continue
            with space.directory(path) as fd:
                s = os.fstat(fd)
            key = project_id(path)
            if key not in projects:
                projects[key] = {'id': key, 'path': str(path), 'name': path.name,
                                 'agents': [], 'source': agent, 'identity': [s.st_dev, s.st_ino]}
            if agent not in projects[key]['agents']:
                projects[key]['agents'].append(agent)
        except (OSError, ValueError):
            if len(issues) < 100:
                issues.append({'agent': agent, 'reason': '部分已记录的项目已移除或无法访问'})
    partition(m, projects, reserved, issues)
    projects = sorted(projects.values(), key=lambda p: p['path'].casefold())[:PROJECT_LIMIT]
    agents = [{'id': key, 'name': name, 'supported': key != 'hermes',
               'project_count': sum(key in p['agents'] for p in projects)}
              for key, name, directory in AGENTS
              if (home / directory).is_dir() or any(key in p['agents'] for p in projects)]
    saved = preference(m, 'scope')
    chosen = saved['agents'] if saved else ['codex'] if any(a['id'] == 'codex' for a in agents) else [a['id'] for a in agents if a['supported']][:1]
    excluded = set(saved.get('excluded', [])) if saved else set()
    included = set(saved.get('included', [])) if saved else set()
    value = {'id': core.uid('catalog'), 'created_at': core.stamp(), 'agents': agents,
             'projects': projects, 'issues': issues,
             'selection': {'agents': chosen, 'ids': [p['id'] for p in projects
                           if set(p['agents']) & set(chosen) and (p['id'] in included or
                           not ({p['id']} | {project_id(parent) for parent in Path(p['path']).parents}) & excluded)]},
             'read_only': True}
    m.db.execute('insert into maintenance_space_catalogs values(?,?,?)',
                 (value['id'], value['created_at'], json.dumps(value)))
    m.db.execute('delete from maintenance_space_catalogs where id not in '
                 '(select id from maintenance_space_catalogs order by created_at desc, rowid desc limit 10)')
    m.db.commit()
    return value


def add_project(m, payload):
    agent, raw = payload.get('agent'), payload.get('path')
    if agent not in {key for key, _, _ in AGENTS} or not isinstance(raw, str) or not Path(raw).expanduser().is_absolute():
        raise ValueError('请选择项目目录和 Agent')
    path = Path(raw).expanduser().resolve(strict=True)
    if path in (Path.home(), Path(path.anchor), m.home) or m.home in path.parents or space.blocked(path, space.protected(m, path)):
        raise ValueError('请选择具体项目目录')
    with space.directory(path):
        pass
    saved = preference(m, 'manual', [])
    if len(saved) >= PROJECT_LIMIT:
        raise ValueError('项目数量已达上限')
    if not any(item.get('path') == str(path) and item.get('agent') == agent for item in saved):
        saved.append({'path': str(path), 'agent': agent})
    save_preference(m, 'manual', saved)
    chosen = preference(m, 'scope', {'agents': [], 'excluded': []})
    chosen['agents'] = sorted(set(chosen['agents']) | {agent})
    chosen['excluded'] = [key for key in chosen.get('excluded', []) if key != project_id(path)]
    save_preference(m, 'scope', chosen)
    return catalog(m)


def latest(m):
    row = m.db.execute("select payload from maintenance_disk_scans where json_extract(payload,'$.mode')='project_space' "
                       'order by created_at desc, rowid desc limit 1').fetchone()
    report = json.loads(row[0]) if row else None
    if report:
        analyses = {r['path']: json.loads(r['payload']) for r in m.db.execute('select * from maintenance_project_analyses')}
        for project in report['projects']:
            result = analyses.get(project['path'])
            exclusions = {p for p in report.get('scope_paths', []) if Path(project['path']) in Path(p).parents}
            if (result and result.get('policy_version') == space.POLICY_VERSION
                    and set(result.get('excluded_projects', [])) == exclusions):
                project['analysis'] = result
            else:
                project.pop('analysis', None)
    return {'report': report, 'batch': preference(m, 'batch', {})}


def scan(m, payload):
    if not _lock.acquire(blocking=False):
        raise ValueError('项目扫描正在进行，请等待完成或停止扫描')
    try:
        _stop.clear()
        if getattr(m, 'cancel_requested', lambda: False)():
            _stop.set()
        return _scan(m, payload)
    finally:
        _lock.release()


def _scan(m, payload):
    previous = latest(m)['report']
    if payload.get('rescan'):
        if not previous:
            raise ValueError('请先选择要扫描的 Agent 和项目')
        projects = [{k: v for k, v in p.items() if k not in ('space', 'analysis')} for p in previous['projects']]
        universe = previous['scope_paths']
    else:
        row = m.db.execute('select payload from maintenance_space_catalogs where id=?', (payload.get('catalog_id'),)).fetchone()
        if not row:
            raise ValueError('扫描范围已失效，请重新选择')
        catalog_data = json.loads(row[0])
        ids, agents = payload.get('ids'), payload.get('agents')
        known_agents = {a['id'] for a in catalog_data['agents'] if a['supported']}
        if (not isinstance(ids, list) or not ids or len(ids) > PROJECT_LIMIT or any(not isinstance(i, str) for i in ids)
                or not isinstance(agents, list) or not agents or any(a not in known_agents for a in agents)):
            raise ValueError('请选择要扫描的 Agent 和项目')
        chosen = set(ids)
        projects = [p for p in catalog_data['projects'] if p['id'] in chosen and set(p['agents']) & set(agents)]
        if {p['id'] for p in projects} != chosen:
            raise ValueError('扫描范围包含未选择 Agent 的项目，请重新选择')
        universe = [p['path'] for p in catalog_data['projects']]
        save_preference(m, 'scope', {'agents': agents, 'included': sorted(chosen), 'excluded': [p['id'] for p in catalog_data['projects']
                           if set(p['agents']) & set(agents) and p['id'] not in chosen]})
    old = {p['path']: p for p in previous['projects']} if previous else {}
    report = {'id': core.uid('disk'), 'mode': 'project_space', 'created_at': core.stamp(),
              'state': 'running', 'projects': projects, 'scope_paths': universe,
              'read_only': True, 'issues': [], 'summary': {}}
    for p in projects:
        p['excluded_paths'] = [child for child in universe if Path(p['path']) in Path(child).parents]
        p['name'] = ('Codex 生成图片' if Path(p['path']).name == 'generated_images' and 'codex' in p['agents'] else Path(p['path']).name) + ('（其他文件）' if p['excluded_paths'] else '')
        p['space'] = {**old.get(p['path'], {}).get('space', {}), 'state': 'pending'}
    started = time.monotonic()
    deadline = started + SCAN_SECONDS
    seen = set()
    def publish():
        report['summary'] = {'projects': len(projects), 'completed': sum(p['space']['state'] == 'completed' for p in projects),
                             'allocated_bytes': sum(p['space'].get('allocated_bytes', 0) for p in projects),
                             'files': sum(p['space'].get('files', 0) for p in projects)}
        m.db.execute('insert or replace into maintenance_disk_scans values(?,?,?)',
                     (report['id'], report['created_at'], json.dumps(report)))
        m.db.commit()
        getattr(m, 'disk_result', lambda value: None)(report)
    publish()
    for index, project in enumerate(sorted(projects, key=lambda p: (-len(Path(p['path']).parts), p['path']))):
        if _stop.is_set() or getattr(m, 'cancel_requested', lambda: False)() or time.monotonic() >= deadline:
            break
        getattr(m, 'project_current', lambda path: None)(project['path'])
        def progress(files):
            space.progress(m, index, len(projects), '正在统计空间 · ' + f'{files:,}' + ' 个文件')
        project['space'] = measure(m, project, seen, deadline, progress)
        publish()
        space.progress(m, index + 1, len(projects), '已扫描 ' + str(index + 1) + '/' + str(len(projects)) + ' 个项目')
    report['state'] = 'completed' if all(p['space']['state'] == 'completed' for p in projects) else 'partial'
    report['stop_reason'] = '扫描已停止' if _stop.is_set() or getattr(m, 'cancel_requested', lambda: False)() else '部分位置尚未完整统计' if report['state'] == 'partial' else ''
    report['elapsed_seconds'] = round(time.monotonic() - started, 2)
    projects.sort(key=lambda p: (-p['space'].get('allocated_bytes', -1), p['path']))
    publish()
    return latest(m)['report']


def measure(m, project, seen, deadline, notify):
    root = Path(project['path'])
    excluded = {Path(path) for path in project['excluded_paths']}
    value = {'files': 0, 'bytes': 0, 'allocated_bytes': 0, 'state': 'completed', 'issues': 0}
    reserved = space.protected(m, root)
    try:
        with space.directory(root) as fd:
            identity = os.fstat(fd)
            if [identity.st_dev, identity.st_ino] != project['identity'] or space.blocked(root, reserved):
                raise ValueError('项目目录已变化，请重新选择扫描范围')
        stack = [root]
        last = time.monotonic()
        while stack:
            if _stop.is_set() or getattr(m, 'cancel_requested', lambda: False)() or time.monotonic() >= deadline:
                value['state'] = 'partial'
                break
            folder = stack.pop()
            try:
                with space.directory(folder) as fd, os.scandir(fd) as entries:
                    for entry in entries:
                        if _stop.is_set() or getattr(m, 'cancel_requested', lambda: False)() or time.monotonic() >= deadline:
                            value['state'] = 'partial'
                            stack.clear()
                            break
                        path = folder / entry.name
                        s = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(s.st_mode):
                            if (path not in excluded and path != m.home and m.home not in path.parents
                                    and s.st_dev == identity.st_dev):
                                stack.append(path)
                            continue
                        if stat.S_ISREG(s.st_mode):
                            inode = (s.st_dev, s.st_ino)
                            if inode not in seen:
                                seen.add(inode)
                                value['files'] += 1
                                value['bytes'] += s.st_size
                                value['allocated_bytes'] += getattr(s, 'st_blocks', 0) * 512
                        now = time.monotonic()
                        if now - last >= .25:
                            notify(value['files'])
                            last = now
            except (OSError, ValueError):
                value['issues'] += 1
                value['state'] = 'partial'
    except (OSError, ValueError) as exc:
        value.update(state='failed', reason='目录无法访问或已变化', error=str(exc)[:150])
    value['checked_at'] = core.stamp()
    return value


def dispatch(m, payload):
    action = payload['action']
    if action == 'space-catalog':
        return catalog(m)
    if action == 'space-add-project':
        return add_project(m, payload)
    if action == 'disk-report':
        return latest(m)
    if action == 'disk-stop':
        _stop.set()
        return {'requested': True}
    return scan(m, payload)
