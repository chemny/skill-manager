"""Explicit-directory artifact review and recoverable moves; never purges files."""
import contextlib
import hashlib
import importlib.util
import json
import os
import stat
import time
from pathlib import Path

import maintenance as core

ACTIONS = {'space-scan', 'space-results', 'space-trash', 'space-trash-list', 'space-restore', 'space-purge'}
MAX_FILES = 20000
POLICY_VERSION = 3
HASH_LIMIT = 128 * 1024 * 1024
HASH_BUDGET = 4 * 1024 * 1024 * 1024
MEDIA = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.mp4', '.mov', '.webm',
         '.wav', '.mp3', '.glb', '.gltf', '.obj', '.stl', '.fbx', '.blend', '.pdf',
         '.pptx', '.docx', '.xlsx', '.zip'}
MODELS = {'.safetensors', '.gguf', '.ckpt', '.pt', '.pth', '.onnx', '.bin',
          '.glb', '.gltf', '.obj', '.stl', '.fbx', '.blend'}
DOCUMENTS = {'.pdf', '.pptx', '.docx', '.xlsx', '.zip'}
ORIGINALS = {'input', 'inputs', 'source', 'sources', 'original', 'originals', 'raw', 'reference', 'references'}
TEXT = {'.md', '.txt', '.json', '.yaml', '.yml', '.toml', '.html', '.css', '.js',
        '.ts', '.tsx', '.jsx', '.py', '.mtl', '.gltf', '.obj', '.ipynb'}
SKIP_DIRS = {'.git', '.codex', '.agents', '.claude', '.ssh', '.aws', '.config',
             '.gnupg', '.kube', '.Trash', '.Trashes', 'node_modules', '.venv', 'venv',
             'site-packages', 'dist-packages', 'Library'}
CACHES = {'__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}
SYSTEM_ROOTS = {Path(p) for p in ('/System', '/Applications', '/Library', '/usr', '/bin', '/sbin',
                                 '/dev', '/proc', '/sys', '/private/etc', '/private/var/db')}
APP_SUFFIXES = {'.app', '.framework', '.bundle', '.photoslibrary', '.photolibrary'}


def fingerprint(s):
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def is_offline(s):
    # macOS UF_OFFLINE / SF_DATALESS: reading can download the entire file.
    return bool(getattr(s, 'st_flags', 0) & 0x40001000)


@contextlib.contextmanager
def directory(path):
    # Pin every ancestor without following links, including during the final rename.
    if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW'):
        raise ValueError('当前系统暂不支持安全的文件移动，请保留文件并使用系统文件管理器。')
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('请选择绝对目录路径')
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


def protected(m, root):
    paths = [m.home.absolute()]
    config = m.a.load_config()
    for settings in config.get('platforms', {}).values():
        for raw in settings.get('roots', []):
            paths.append(core.absolute(raw).resolve())
    paths.extend(Path(r['path']).resolve() for r in m.db.execute(
        "select path from capabilities where kind='skill'"))
    return set(paths)


def blocked(path, reserved):
    generated = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser().absolute() / 'generated_images'
    internal = SKIP_DIRS - {'.codex'} if (path == generated or generated in path.parents) and not generated.is_symlink() else SKIP_DIRS
    return any(parent in reserved or parent in SYSTEM_ROOTS or parent.suffix.lower() in APP_SUFFIXES for parent in (path, *path.parents)) or any(
        part in internal for part in path.parts[1:])


def validate_root(m, raw):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('请先选择扫描目录')
    root = Path(os.path.abspath(os.path.expanduser(raw)))
    if root == Path(root.anchor) or root == Path.home():
        raise ValueError('请选择具体项目或产物目录，不支持直接扫描整个磁盘或用户主目录。')
    if blocked(root, protected(m, root)):
        raise ValueError('此位置属于受保护的应用或 Skill 目录，请选择项目产物目录。')
    for parent in (root, *root.parents):
        if (parent / 'SKILL.md').is_file():
            raise ValueError('Skill 安装内容暂不参与产物清理，请选择独立的输出目录。')
    with directory(root) as fd:
        s = os.fstat(fd)
        identity = [s.st_dev, s.st_ino]
    return root, identity


def read_file(path, expected=None, limit=None, digest=False, deadline=None):
    with directory(path.parent) as parent:
        if is_offline(os.stat(path.name, dir_fd=parent, follow_symlinks=False)):
            raise ValueError('云端文件未下载，仅统计大小，不读取内容')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or expected and fingerprint(before) != expected:
                raise ValueError('文件已变化，请重新扫描')
            if is_offline(before):
                raise ValueError('云端文件未下载，仅统计大小，不读取内容')
            if digest:
                value = hashlib.sha256()
                consumed = 0
                while True:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError('内容核对达到时间上限')
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    consumed += len(chunk)
                    if consumed > before.st_size:
                        raise ValueError('文件在读取时发生变化，请重新扫描')
                    value.update(chunk)
                value = value.hexdigest()
            else:
                value = stream.read(limit)
            if fingerprint(os.fstat(stream.fileno())) != fingerprint(before):
                raise ValueError('文件在读取时发生变化，请重新扫描')
            return value


def progress(m, done, total, text):
    getattr(m, 'batch_progress', lambda *args: None)(done, total, text)


def cache_source(path):
    if path.parent.name != '__pycache__' or path.suffix.lower() != '.pyc':
        return None
    try:
        source = Path(importlib.util.source_from_cache(str(path)))
    except ValueError:
        source = path.parent.parent / (path.name.split('.')[0] + '.py')
    try:
        with directory(source.parent) as fd:
            s = os.stat(source.name, dir_fd=fd, follow_symlinks=False)
            return str(source) if stat.S_ISREG(s.st_mode) and not is_offline(s) else None
    except OSError:
        return None


def keeper_key(row):
    return (not bool(retained_role(row)), not bool(row.get('references')),
            -row['modified'], row['path'])


def retained_role(row):
    return row.get('protected_role') or ('model_dependency' if any(
        Path(path).suffix.lower() in {'.gltf', '.obj', '.mtl'} for path in row.get('references', [])) else None)


def duplicate_choices(rows):
    keeper = min(rows, key=keeper_key)
    return keeper, [r for r in rows if r is not keeper and not retained_role(r)]


def scan(m, raw, exclude_roots=(), max_files=None, seconds=None):
    deadline = time.monotonic() + seconds if seconds is not None else None
    file_limit = MAX_FILES if max_files is None else max_files
    root, identity = validate_root(m, raw)
    reserved = protected(m, root)
    excluded = {Path(p) for p in exclude_roots if root in Path(p).parents}
    files, issues = [], []
    truncated = False
    analysis_limited = False
    references_incomplete = False
    enumeration_incomplete = False
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            with directory(folder) as fd:
                entries = sorted(os.scandir(fd), key=lambda e: e.name)
                for entry in entries:
                    path = folder / entry.name
                    if len(files) >= file_limit or deadline is not None and time.monotonic() >= deadline:
                        truncated = True
                        stack.clear()
                        break
                    if entry.is_symlink():
                        issues.append({'path': str(path), 'reason': '跳过符号链接'})
                        continue
                    s = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(s.st_mode):
                        if path in excluded:
                            issues.append({'path': str(path), 'reason': '独立子项目，单独统计'})
                        elif (blocked(path, reserved) or entry.name.startswith('.') and entry.name not in CACHES
                                or (path / 'SKILL.md').exists()):
                            issues.append({'path': str(path), 'reason': '跳过受保护目录'})
                        elif len(path.relative_to(root).parts) > 30:
                            enumeration_incomplete = True
                            issues.append({'path': str(path), 'reason': '超过扫描深度'})
                        else:
                            stack.append(path)
                        continue
                    if not stat.S_ISREG(s.st_mode):
                        continue
                    if entry.name.startswith('.') or path.suffix.lower() in {'.pem', '.key', '.db', '.sqlite', '.sqlite3'}:
                        issues.append({'path': str(path), 'reason': '跳过配置、凭证或数据库文件'})
                        continue
                    ext = path.suffix.lower()
                    offline = is_offline(s)
                    cache = any(p in CACHES for p in path.relative_to(root).parts[:-1])
                    original = bool({part.lower() for part in path.parts[:-1]} & ORIGINALS)
                    role = 'model' if ext in MODELS else 'document' if ext in DOCUMENTS else 'original' if original else None
                    source = cache_source(path) if cache else None
                    duplicate_eligible = ext in MEDIA and ext not in MODELS | DOCUMENTS and s.st_nlink == 1 and not offline
                    eligible = (duplicate_eligible or bool(source)) and s.st_nlink == 1 and not offline and not role
                    row = {'id': core.uid('file'), 'path': str(path), 'name': path.name,
                           'relative': str(path.relative_to(root)), 'size': s.st_size,
                           'allocated': getattr(s, 'st_blocks', 0) * 512,
                           'modified': s.st_mtime, 'fingerprint': fingerprint(s), 'cache': cache, 'offline': offline, 'references': [],
                           'selectable': eligible, 'duplicate_eligible': duplicate_eligible,
                           'protected_role': role, 'cache_source': source, 'advice': 'review' if eligible else 'keep',
                           'reason': '用途待查看；文件较旧不代表可以删除' if eligible else
                                     '源文件、模型或依赖默认保留'}
                    if s.st_nlink > 1:
                        row['reason'] = '硬链接文件，保留以避免影响其他位置'
                    if offline:
                        row['reason'] = '云端文件未下载，仅统计大小，保留文件'
                    files.append(row)
                    if len(files) % 100 == 0:
                        progress(m, len(files), None, f'已扫描 {len(files)} 个文件')
        except (OSError, ValueError) as exc:
            enumeration_incomplete = True
            issues.append({'path': str(folder), 'reason': '无法完整读取：' + str(exc)})
    progress(m, len(files), None, '空间统计完成，正在检查本地引用')
    import project_references
    import sys
    references, reference_coverage = project_references.check(m, root, files, deadline, sys.modules[__name__])
    references_incomplete = bool(reference_coverage['reference_pending'])
    # Protected/offline files never need duplicate or reference reads.
    for row in files:
        if row['path'] not in references:
            continue
        row['references'] = sorted(references[row['path']])
        row['protected_role'] = retained_role(row)
        row['references'] = row['references'][:20]
        if row['references']:
            row.update(selectable=False, advice='keep', reason='在项目文件中发现引用，建议保留')
    sizes = {}
    for row in files:
        if row['duplicate_eligible'] and row['size']:
            sizes.setdefault(row['size'], []).append(row)
    hashed = 0
    duplicates = {}
    candidates = [r for rows in sizes.values() if len(rows) > 1 for r in rows]
    for index, row in enumerate(candidates):
        if deadline is not None and time.monotonic() >= deadline:
            analysis_limited = True
            break
        if row['size'] > HASH_LIMIT or hashed + row['size'] > HASH_BUDGET:
            analysis_limited = True
            continue
        progress(m, index, len(candidates), '正在核对重复文件')
        try:
            row['sha256'] = read_file(Path(row['path']), row['fingerprint'], digest=True, deadline=deadline)
            hashed += row['size']
            duplicates.setdefault(row['sha256'], []).append(row)
        except TimeoutError:
            analysis_limited = True
            break
        except (OSError, ValueError):
            analysis_limited = True
            row.update(selectable=False, advice='keep', reason='文件读取失败或已变化')
    for row in files:
        if row['selectable'] and row['cache_source'] and time.time() - row['modified'] > 7 * 86400:
            row.update(advice='clean', category='cache', reason='存在对应源码的旧 Python 缓存')
    for rows in duplicates.values():
        if len(rows) < 2:
            continue
        keeper, extras = duplicate_choices(rows)
        keeper.update(selectable=False, advice='keep', reason='完全重复组的保留副本')
        for row in rows:
            row['duplicate_keeper'] = keeper['path']
            if row in extras:
                row.update(selectable=True, advice='review' if row['references'] else 'clean', category='duplicates',
                           reason='保留相同副本；删除原路径可能影响历史预览')
    if truncated or references_incomplete:
        for row in files:
            if row['advice'] == 'clean':
                row.update(advice='review', reason='扫描或引用未完整核实，请先查看，不自动列入建议清理项')
    elif excluded:
        for row in files:
            if row['advice'] == 'clean' and not row['cache']:
                row.update(advice='review', reason='独立子项目中的引用未核实，请先查看重复文件')
    unique_files = {(r['fingerprint'][0], r['fingerprint'][1]): r for r in files}
    skipped_hashes = sum('sha256' not in r for r in candidates)
    result = {'id': core.uid('space'), 'root': str(root), 'root_identity': identity,
              'policy_version': POLICY_VERSION,
              'exclude_roots': [str(p) for p in sorted(excluded)],
              'created_at': core.stamp(), 'items': sorted(files, key=lambda r: -r['size']),
              'issues': issues[:200], 'issue_count': len(issues), 'truncated': truncated,
              'analysis_limited': analysis_limited, 'references_incomplete': references_incomplete,
              'enumeration_incomplete': enumeration_incomplete,
              'coverage': {'hash_candidates': len(candidates), 'hashed_files': len(candidates) - skipped_hashes,
                           'hash_skipped': skipped_hashes, 'hash_bytes': hashed,
                           **reference_coverage,
                           'stages': {'space': not (truncated or enumeration_incomplete),
                                      'duplicates': not skipped_hashes,
                                      'references': not references_incomplete}},
              'file_limit': file_limit,
              'reference_note': '仅检查本次目录内的有限文本引用；未发现引用不代表未使用。',
              'summary': {'files': len(files), 'bytes': sum(r['size'] for r in unique_files.values()),
                          'allocated_bytes': sum(r['allocated'] for r in unique_files.values()),
                          'offline_files': sum(r['offline'] for r in unique_files.values()),
                          'offline_bytes': sum(r['size'] for r in unique_files.values() if r['offline']),
                          'suggested_bytes': sum(r['size'] for r in files if r['advice'] == 'clean')}}
    m.db.execute('insert into maintenance_space_scans values(?,?,?)',
                 (result['id'], result['created_at'], json.dumps(result)))
    m.db.execute('delete from maintenance_space_scans where id not in '
                 '(select id from maintenance_space_scans order by created_at desc, rowid desc limit 10) '
                 "and id not in (select json_extract(payload,'$.scan_id') from maintenance_project_analyses where json_extract(payload,'$.scan_id') is not null)")
    m.db.commit()
    return result


def results(m, sid=None):
    row = m.db.execute('select payload from maintenance_space_scans ' +
                       ('where id=?' if sid else 'order by created_at desc, rowid desc limit 1'),
                       (sid,) if sid else ()).fetchone()
    if not row:
        return None
    report = json.loads(row[0])
    recycled = {json.loads(r[0])['original'] for r in m.db.execute(
        "select payload from maintenance_space_trash where state in ('available','moving','restoring','purging')")}
    for item in report['items']:
        if item['path'] in recycled and not Path(item['path']).exists():
            item.update(selectable=False, advice='keep', reason='已移入项目文件回收站', recycled=True)
    return report


def trash_list(m):
    rows = []
    for record in m.db.execute('select * from maintenance_space_trash order by created_at desc'):
        item = json.loads(record['payload'])
        item['state'] = record['state']
        if record['state'] == 'purging':
            item['state'] = 'available' if Path(item['stored']).is_file() else 'purged'
        elif record['state'] in ('moving', 'restoring'):
            # A crash can happen between filesystem mutation and the DB update.
            item['state'] = 'available' if Path(item['stored']).is_file() else 'interrupted'
        rows.append(item)
    return {'items': rows}


def move_selected(m, payload, verified=None):
    if payload.get('confirmed') is not True:
        raise ValueError('请先确认移入回收站')
    report = results(m, payload.get('scan_id'))
    if not report or not payload.get('scan_id') or report.get('policy_version') != POLICY_VERSION:
        raise ValueError('扫描记录不存在，请重新扫描')
    ids = payload.get('ids')
    if not isinstance(ids, list) or not ids or len(ids) > MAX_FILES:
        raise ValueError('请选择需要清理的文件')
    by_id = {r['id']: r for r in report['items']}
    verified = verified or {}
    for key, proof in verified.items():
        row = by_id.get(key)
        if row and proof['scan_id'] == report['id'] and row['duplicate_eligible'] and not retained_role(row):
            row.update(selectable=True, sha256=proof['sha256'], duplicate_keeper=proof['keeper'])
    if any(i not in by_id or not by_id[i]['selectable'] for i in ids):
        raise ValueError('包含受保护或不在扫描结果中的文件')
    root, identity = validate_root(m, report['root'])
    if identity != report['root_identity']:
        raise ValueError('扫描目录已变化，请重新扫描')
    # Re-read references and duplicate keepers before moving any selected content.
    fresh = scan(m, str(root), exclude_roots=report.get('exclude_roots', ()), max_files=report.get('file_limit', MAX_FILES))
    current = {r['path']: r for r in fresh['items']}
    dest = m.home / 'space-trash'
    dest.mkdir(mode=0o700, exist_ok=True)
    completed, skipped = [], []
    for index, fid in enumerate(dict.fromkeys(ids)):
        old = by_id[fid]
        path = Path(old['path'])
        try:
            now = current.get(str(path))
            proof = verified.get(fid)
            eligible = now and (now['duplicate_eligible'] and not retained_role(now) if proof else now['selectable'])
            if not eligible or now['fingerprint'] != old['fingerprint']:
                raise ValueError('文件或引用已变化，请重新扫描')
            if proof:
                keeper_root, keeper_identity = validate_root(m, proof['keeper_root'])
                keeper_path = Path(proof['keeper'])
                if (keeper_identity != proof['keeper_identity'] or not keeper_path.is_relative_to(keeper_root)
                        or keeper_path == path or blocked(keeper_path, protected(m, keeper_root))):
                    raise ValueError('保留副本位置已变化，请重新分析')
                if (read_file(path, old['fingerprint'], digest=True) != proof['sha256']
                        or read_file(keeper_path, proof['keeper_fingerprint'], digest=True) != proof['sha256']):
                    raise ValueError('保留副本已变化，请重新分析')
            elif old.get('duplicate_keeper'):
                keeper = current.get(old['duplicate_keeper'])
                if not keeper or keeper.get('sha256') != old.get('sha256'):
                    raise ValueError('保留副本已变化，请重新扫描')
            tid = core.uid('artifact')
            stored = dest / tid
            item = {**old, 'id': tid, 'original': str(path), 'stored': str(stored),
                    'root': str(root), 'created_at': core.stamp()}
            with directory(path.parent) as parent, directory(dest) as target:
                s = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or fingerprint(s) != old['fingerprint']:
                    raise ValueError('文件已变化，请重新扫描')
                if s.st_dev != os.fstat(target).st_dev:
                    raise ValueError('跨磁盘移动暂不支持，原文件已保留')
                m.db.execute('insert into maintenance_space_trash values(?,?,?,?)',
                             (tid, item['created_at'], 'moving', json.dumps(item)))
                m.db.commit()
                os.rename(path.name, tid, src_dir_fd=parent, dst_dir_fd=target)
                item['stored_fingerprint'] = fingerprint(os.stat(tid, dir_fd=target, follow_symlinks=False))
                m.db.execute("update maintenance_space_trash set state='available', payload=? where id=?", (json.dumps(item), tid))
                m.db.commit()
            completed.append(tid)
        except (OSError, ValueError) as exc:
            skipped.append({'path': str(path), 'reason': str(exc)})
        progress(m, index + 1, len(ids), '正在移入项目文件回收站')
    return {'completed': len(completed), 'skipped': skipped,
            'state': 'partial' if skipped else 'complete', 'ids': completed}


def restore(m, payload):
    if payload.get('confirmed') is not True:
        raise ValueError('请先确认恢复')
    item = next((r for r in trash_list(m)['items'] if r['id'] == payload.get('id') and r['state'] == 'available'), None)
    if not item:
        raise ValueError('回收文件不存在或状态待核实')
    path, stored = Path(item['original']), Path(item['stored'])
    validate_root(m, item['root'])
    with directory(path.parent) as target, directory(stored.parent) as source:
        s = os.stat(stored.name, dir_fd=source, follow_symlinks=False)
        if (not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_size != item['size']
                or s.st_ino != item['fingerprint'][1] or s.st_mtime_ns != item['fingerprint'][3]
                or item.get('stored_fingerprint') and fingerprint(s) != item['stored_fingerprint']):
            raise ValueError('回收文件已变化，请核实内容')
        m.db.execute("update maintenance_space_trash set state='restoring' where id=?", (item['id'],))
        m.db.commit()
        # link fails if the original path exists; no overwrite window.
        os.link(stored.name, path.name, src_dir_fd=source, dst_dir_fd=target, follow_symlinks=False)
        os.unlink(stored.name, dir_fd=source)
        m.db.execute("update maintenance_space_trash set state='restored' where id=?", (item['id'],))
        m.db.commit()
    return {'completed': 1}


def purge(m, payload):
    if payload.get('confirmed') is not True:
        raise ValueError('请先确认永久删除')
    ids = payload.get('ids')
    if not isinstance(ids, list) or not ids or len(ids) > MAX_FILES or any(not isinstance(i, str) for i in ids):
        raise ValueError('请选择回收站中的文件')
    rows = {r['id']: r for r in trash_list(m)['items'] if r['state'] == 'available'}
    if any(i not in rows for i in ids):
        raise ValueError('回收内容已变化，请刷新回收站')
    completed, skipped = 0, []
    for index, tid in enumerate(dict.fromkeys(ids)):
        item = rows[tid]
        stored = m.home / 'space-trash' / tid
        try:
            if str(stored) != item['stored'] or not item.get('stored_fingerprint'):
                raise ValueError('回收记录无法核实，文件已保留')
            with directory(stored.parent) as parent:
                s = os.stat(tid, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or fingerprint(s) != item['stored_fingerprint']:
                    raise ValueError('回收文件已变化，文件已保留')
                m.db.execute("update maintenance_space_trash set state='purging' where id=?", (tid,))
                m.db.commit()
                os.unlink(tid, dir_fd=parent)
                m.db.execute("update maintenance_space_trash set state='purged' where id=?", (tid,))
                m.db.commit()
            completed += 1
        except (OSError, ValueError) as exc:
            skipped.append({'path': item['original'], 'reason': str(exc)})
        progress(m, index + 1, len(ids), '正在永久删除回收文件')
    return {'completed': completed, 'skipped': skipped, 'state': 'partial' if skipped else 'complete'}


def dispatch(m, payload):
    action = payload['action']
    if action == 'space-scan':
        return scan(m, payload.get('path'))
    if action == 'space-results':
        return {'scan': results(m, payload.get('id'))}
    if action == 'space-trash-list':
        return trash_list(m)
    if action == 'space-trash':
        return move_selected(m, payload)
    if action == 'space-restore':
        return restore(m, payload)
    if action == 'space-purge':
        return purge(m, payload)
