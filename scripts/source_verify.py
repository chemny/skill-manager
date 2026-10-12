"""Content verification for source recovery; never edits installed Skill files."""
import hashlib
import json
import stat
from pathlib import Path, PurePosixPath


def package_hashes(folder):
    folder = Path(folder).resolve(strict=True)
    result = {}
    for p in folder.rglob('*'):
        rel = p.relative_to(folder)
        if any(part in ('.git', '__pycache__', '.pytest_cache') for part in rel.parts):
            continue
        if p.is_symlink():
            raise ValueError('Package contains links')
        if p.is_file() and p.name != '.DS_Store':
            if p.stat().st_size > 64_000_000:
                raise ValueError('Package file too large')
            result[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    receipt = '.paseo-managed-files.json'
    if receipt in result:
        try:
            data = json.loads((folder / receipt).read_text(encoding='utf-8'))
            files = data.get('files')
            if (data.get('version') == 1 and isinstance(files, dict) and files
                    and all(name != receipt and result.get(name) == value for name, value in files.items())):
                result.pop(receipt)
        except (ValueError, AttributeError, UnicodeError):
            pass
    return result


def archive_packages(archive):
    """Index bounded ZIPs without extracting or executing any repository content."""
    infos = archive.infolist()
    if len(infos) > 100000 or sum(i.file_size for i in infos) > 512_000_000:
        raise ValueError('Repository archive too large')
    files = {}
    for i in infos:
        path = PurePosixPath(i.filename)
        if path.is_absolute() or '..' in path.parts or '\\' in i.filename:
            raise ValueError('Unsafe archive path')
        if i.is_dir():
            continue
        if i.filename in files or i.file_size > 64_000_000:
            raise ValueError('Duplicate archive path or oversized file')
        files[i.filename] = i
    roots = [p[:-8] for p in files if p.endswith('/SKILL.md')]
    result = []
    for root in roots:
        package = {}
        for name, info in files.items():
            if name.startswith(root):
                rel = name[len(root):]
                if any(part in ('.git', '__pycache__', '.pytest_cache') for part in PurePosixPath(rel).parts):
                    continue
                if PurePosixPath(rel).name != '.DS_Store':
                    package[rel] = ('unsupported-link' if stat.S_ISLNK(info.external_attr >> 16)
                                    else hashlib.sha256(archive.read(info)).hexdigest())
        result.append({'path':root.split('/', 1)[1].rstrip('/'), 'files':package})
    return result


def compare(local, remote):
    changed = sorted(k for k in local.keys() & remote.keys() if local[k] != remote[k])
    missing = sorted(remote.keys() - local.keys())
    extra = sorted(local.keys() - remote.keys())
    return {'exact':bool(remote.get('SKILL.md')) and not (changed or missing or extra),
            'document_match':bool(remote.get('SKILL.md')) and local.get('SKILL.md') == remote['SKILL.md'],
            'changed':changed, 'missing':missing, 'extra':extra,
            'matched_files':len(local.keys() & remote.keys()) - len(changed)}


def bind_verified(manager, row, source, expected_files, proof):
    """Recheck bytes immediately before writing all shared aliases atomically."""
    from library_metadata import repository
    src = repository(manager.a, source['url'], source.get('ref', ''), source.get('path', ''), 'verified-package')
    if not src or not compare(package_hashes(row['path']), expected_files)['exact']:
        raise ValueError('Source package not verified or local files changed')
    src['verification'] = proof
    real = Path(row['path']).resolve(strict=True)
    refs = [dict(r) for r in manager.r.all_capabilities()
            if r['kind'] == 'skill' and Path(r['path']).resolve() == real]
    for ref in refs:
        old = manager.source(ref)
        if old:
            raise ValueError('A shared reference already has a source; review manually')
    previous = []
    # state() commits, so create every row before the atomic update transaction.
    for ref in refs:
        state = manager.state(ref)
        previous.append({'id':ref['id'], 'source':state['source'],
                         'baseline':state['baseline'], 'last_check':state['last_check']})
    with manager.db:
        for ref in refs:
            manager.db.execute('update maintenance_items set source=?,baseline=null,last_check=null where id=?',
                               (json.dumps(src), ref['id']))
    manager.r.log_operation('source-verified-bind', row['id'], {'source':src, 'previous':previous})
    manager.invalidate_cache()
    return {'source':src, 'references':len(refs), 'previous':previous}
