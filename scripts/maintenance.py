"""Local maintenance core shared by CLI and HTTP. Standard library only.

Plans pin local fingerprints and downloaded content; apply never downloads again.
Backups are private, checksummed, and symlinks are preserved rather than followed.
"""
from __future__ import annotations
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

BROWSE_ACTIONS = frozenset({'discovery-cached','uninstall-references','cache-inspect','source-search-policy','taxonomy','inventory-cached','inventory-detail','backups','history','plans','get-plan','install-roots','usage','update-summary','duplicate-cached','backup-detail','backup-advice','restore-targets','open-directory'})

SCHEMA = '''
create table if not exists maintenance_space_catalogs(id text primary key, created_at text, payload text);
create table if not exists maintenance_space_preferences(key text primary key, payload text);
create table if not exists maintenance_project_analyses(path text primary key, payload text);
create table if not exists maintenance_project_comparisons(id text primary key, report_id text, created_at text, payload text);
create table if not exists maintenance_disk_scans(id text primary key, created_at text, payload text);
create table if not exists maintenance_space_scans(id text primary key, created_at text, payload text);
create table if not exists maintenance_space_trash(id text primary key, created_at text, state text, payload text);
create table if not exists maintenance_plans(id text primary key, created_at text, state text, payload text);
create table if not exists maintenance_backups(id text primary key, created_at text, path text, payload text, state text);
create table if not exists maintenance_backup_flags(id text primary key, retained integer default 0);
create table if not exists maintenance_backup_trash(id text primary key, trashed_at text);
create table if not exists maintenance_recovery_candidates(id text primary key, payload text, state text);
create table if not exists maintenance_operations(id text primary key, created_at text, updated_at text, action text, state text, payload text);
create table if not exists maintenance_item_dates(id text primary key, first_seen_at text, installed_at text);
create table if not exists maintenance_labels(real_path text primary key, category text, tags text);
create table if not exists maintenance_items(id text primary key, protected integer default 0, source text, baseline text, last_check text);
create table if not exists maintenance_check_cache(cache_key text primary key, real_path text, payload text);
create index if not exists maintenance_check_cache_path on maintenance_check_cache(real_path);
create table if not exists maintenance_check_runs(id text primary key, payload text);
create table if not exists maintenance_fs_identity(platform text, device integer, inode integer, item_id text, primary key(platform,device,inode));
create table if not exists maintenance_scans(id text primary key, created_at text, payload text);
create table if not exists maintenance_inventory_cache(id integer primary key, generation integer default 0, payload text, generated_at text, payload_generation integer);
'''

def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')

def uid(prefix):
    return prefix + '-' + uuid.uuid4().hex[:16]

def absolute(path):
    from skill_locations import path as expand_location
    return expand_location(path)

def exists(path):
    return os.path.lexists(path)


@contextlib.contextmanager
def source_batch(manager):
    """Cache discovery only within a comparison batch; explicit bindings stay per ID."""
    previous = getattr(manager, '_source_context', None)
    missing = object()
    previous_channels = getattr(manager.r, '_batch_update_channels', missing)
    manager._source_context = {'sources': {}, 'installer': {}, 'git': {}}
    manager.r._batch_update_channels = None
    try:
        yield manager._source_context
    finally:
        manager._source_context = previous
        if previous_channels is missing: delattr(manager.r, '_batch_update_channels')
        else: manager.r._batch_update_channels = previous_channels


def inventory_summary(value):
    """Keep list decisions intact, but fetch candidates and comparison details on demand."""
    omitted = {'local_source_candidates', 'source_candidates', 'classification_meta',
               'references', 'reference_paths', 'differences', 'storage_roots', 'usage_note'}
    def project(node):
        if isinstance(node, list): return [project(item) for item in node]
        if not isinstance(node, dict): return node
        result = {key:project(item) for key,item in node.items() if key not in omitted}
        return result
    result = project(value)
    for item in result.get('items', []): item['_summary'] = True
    return result


def prune_agent_cache(registry, platform):
    """Patch presentation only; registered consumers remain available to write checks."""
    db = registry.conn
    if not db.execute("select 1 from sqlite_master where name='maintenance_inventory_cache'").fetchone():
        return
    try:
        db.execute('begin immediate')
        row = db.execute('select * from maintenance_inventory_cache where id=1').fetchone()
        if not row:
            db.commit()
            return
        try:
            payload = json.loads(row['payload'] or 'null')
        except (ValueError, TypeError):
            payload = None
        valid = (isinstance(payload, dict) and isinstance(payload.get('cache'), dict)
                 and payload['cache'].get('schema') == 10 and isinstance(payload.get('items'), list))
        generation = row['generation'] + 1
        if valid:
            affected = {i['real_path'] for i in payload['items'] if i['platform'] == platform}
            items = payload['items'] = [i for i in payload['items'] if i['platform'] != platform]
            groups = {}
            for item in items:
                groups.setdefault(item['real_path'], []).append(item)
            for group in groups.values():
                agents = sorted({a for i in group for a in i.get('known_agents', []) if a != platform})
                paths = sorted({i['path'] for i in group})
                for item in group:
                    item.update(known_agents=agents, shared=len(agents) > 1,
                                references=[i['id'] for i in group], reference_paths=paths,
                                multiple_references=len(paths) > 1)
            payload['locations'] = [l for l in payload.get('locations', []) if l['platform'] != platform]
            removed_ids = {r['id'] for r in db.execute('select id from capabilities where platform=?', (platform,))}
            payload['excluded_registered_paths'] = [i for i in payload.get('excluded_registered_paths', []) if i['id'] not in removed_ids]
            from insights import summarize
            payload['insights'] = summarize(items)
            payload['shared_targets'] = [[i['id'] for i in group] for group in groups.values() if group[0]['shared']]
            payload['same_source_copies'] = [[t['id'] for t in group['targets']] for group in payload['insights']['independent_copies']]
            payload['cleanup_candidates'] = payload['insights']['space_review']
            unique = {i['real_path']:i.get('target_space', i.get('space', {})) for i in items if i['present'] and i['health'] != 'broken-link'}
            payload['space']['installed_unique'] = {key:sum(s.get(key, 0) for s in unique.values()) for key in ('bytes', 'allocated_bytes')}
            pending = set(payload['cache'].get('recheck_ids', []))
            payload['cache'].update(generation=generation,
                                    recheck_ids=[i['id'] for i in items if i['real_path'] in affected or i['id'] in pending])
        # Preserve unrelated invalidation and the original freshness timestamps.
        payload_generation = generation if row['generation'] == row['payload_generation'] else row['payload_generation']
        db.execute('update maintenance_inventory_cache set payload=?,generation=?,payload_generation=? where id=1',
                   (json.dumps(payload, ensure_ascii=False) if valid else None, generation, payload_generation))
        db.commit()
    except Exception:
        db.rollback()
        raise


def job_summary(value):
    """Project comparison/list jobs only; never strip recovery or write-operation evidence."""
    result = dict(value)
    if 'jobs' in result:
        result['jobs'] = [job_summary(job) for job in result['jobs']]
        result['active'] = job_summary(result['active']) if result.get('active') else None
    if result.get('action') in ('inventory', 'check', 'check-many'):
        if result.get('result'): result['result'] = inventory_summary(result['result'])
        if result.get('partial_checks'):
            result['partial_checks'] = inventory_summary({'checks':result['partial_checks']})['checks']
    return result

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chmod(temp, 0o600)
    os.replace(temp, path)

def digest_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def manifest(path, ignore_git=False):
    """No following links. Includes empty dirs and permissions, rejects devices."""
    path = Path(path)
    if not exists(path):
        return {}
    result = {}
    def visit(p, rel):
        if ignore_git and (rel == '.git' or rel.startswith('.git/')):
            return
        s = p.lstat()
        mode = stat.S_IMODE(s.st_mode)
        if stat.S_ISLNK(s.st_mode):
            result[rel] = {'kind': 'link', 'target': os.readlink(p), 'bytes': s.st_size}
        elif stat.S_ISREG(s.st_mode):
            result[rel] = {'kind': 'file', 'sha256': digest_file(p), 'bytes': s.st_size, 'mode': mode}
        elif stat.S_ISDIR(s.st_mode):
            result[rel] = {'kind': 'dir', 'mode': mode}
            for child in sorted(p.iterdir()):
                visit(child, child.name if rel == '.' else rel + '/' + child.name)
        else:
            raise ValueError('Unsupported special file: ' + str(p))
    visit(path, '.')
    return result

def fingerprint(m):
    return hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()

def comparison_fingerprint(files):
    return fingerprint({k:v for k,v in files.items() if k != '.git' and not k.startswith('.git/')})

def duplicate_content_fingerprints(items, saved_items=None, lightweight=False):
    """Identify identical installed copies without hashing every Skill in the list."""
    by_name = {}
    by_real = {}
    for item in items:
        if item['kind'] == 'skill' and item['present'] and item['health'] == 'ok':
            by_name.setdefault(item['name'].casefold(), {})[item['real_path']] = item
            by_real.setdefault(item['real_path'], []).append(item)
    for copies in by_name.values():
        if len(copies) < 2:
            continue
        by_skill_file = {}
        for real, item in copies.items():
            path = Path(real)
            if item.get('target_space', {}).get('bytes', 0) > 16_000_000:
                continue
            try:
                skill_file = path / 'SKILL.md'
                if not skill_file.is_file() or skill_file.stat().st_size > 1_000_000:
                    continue
                by_skill_file.setdefault(digest_file(skill_file), []).append(item)
            except OSError:
                continue
        for candidates in by_skill_file.values():
            if len(candidates) < 2:
                continue
            for item in candidates:
                saved = (saved_items or {}).get(item['id'], {})
                if lightweight and saved.get('real_path') == item['real_path'] and saved.get('content_fingerprint'):
                    item['content_fingerprint'] = saved['content_fingerprint']
                else:
                    try:
                        item['content_fingerprint'] = comparison_fingerprint(manifest(item['real_path'], ignore_git=True))
                    except (OSError, ValueError):
                        continue
                for reference in by_real[item['real_path']]:
                    reference['content_fingerprint'] = item['content_fingerprint']

def copy_path(source, destination):
    if source.is_symlink():
        destination.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)

def remove_path(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)

def content(m):
    return {k: {f: v for f, v in entry.items() if f != 'mode'} for k, entry in m.items()
            if entry['kind'] != 'dir' and not k.startswith('.git/') and k not in ('.DS_Store', '.git')}

def diff(before, after):
    b, a = content(before), content(after)
    return {'added': sorted(a.keys() - b.keys()), 'removed': sorted(b.keys() - a.keys()),
            'changed': sorted(k for k in b.keys() & a.keys() if b[k] != a[k])}

def size(path, seen=None, inode_sizes=None):
    seen = seen if seen is not None else set()
    total = allocated = 0
    if not exists(path):
        return {'bytes': 0, 'allocated_bytes': 0}
    def visit(p):
        nonlocal total, allocated
        s = p.lstat()
        key = (s.st_dev, s.st_ino)
        if key in seen:
            return
        seen.add(key)
        blocks = getattr(s, 'st_blocks', (s.st_size + 511) // 512) * 512
        allocated += blocks
        if inode_sizes is not None:
            inode_sizes[key] = {'bytes':0 if stat.S_ISDIR(s.st_mode) else s.st_size,
                                'allocated_bytes':blocks}
        if not stat.S_ISDIR(s.st_mode):
            total += s.st_size
        else:
            for child in p.iterdir():
                visit(child)
    visit(Path(path))
    return {'bytes': total, 'allocated_bytes': allocated}

class Manager:
    def __init__(self, legacy, registry=None):
        self.a = legacy
        self.r = registry or legacy.Registry()
        self.db = self.r.conn
        self.db.executescript(SCHEMA)
        from taxonomy import initialize
        initialize(self)
        import classification, source_search, skill_discovery
        classification.initialize(self)
        source_search.initialize(self)
        skill_discovery.initialize(self)
        self.db.execute('insert or ignore into maintenance_inventory_cache(id) values (1)')
        self.home = legacy.APP_DIR.resolve()
        for name in ('plans', 'backups', 'quarantine'):
            p = self.home / name
            p.mkdir(parents=True, exist_ok=True)
            os.chmod(p, 0o700)
        self.db.commit()

    def invalidate_cache(self):
        self.db.execute('update maintenance_inventory_cache set generation=generation+1 where id=1')
        self.db.commit()

    def cached_inventory(self):
        row = self.db.execute('select * from maintenance_inventory_cache where id=1').fetchone()
        try:
            payload = json.loads(row['payload']) if row['payload'] else None
        except (ValueError, TypeError):
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get('cache'), dict) or payload['cache'].get('schema') != 10 or not isinstance(payload.get('items'), list):
            return {'items':[], 'cache':{'available':False, 'from_cache':True,'dirty':True}}
        payload['cache'].update(from_cache=True, dirty=row['generation'] != row['payload_generation'])
        from version_policy import normalize_check
        from check_results import observed_at, source_key
        saved = {r['id']:dict(r) for r in self.db.execute('select id,source,last_check from maintenance_items')} if payload['cache']['dirty'] else {}
        source_changed=False
        for item in payload['items']:
            state = saved.get(item['id'])
            if state:
                recorded = json.loads(state['last_check'] or 'null')
                binding = json.loads(state['source'] or 'null')
                current = item.get('source')
                if binding and source_key(binding) != source_key(current):
                    item['update_check'] = None
                    item['source'] = binding
                    source_changed=True
                elif not recorded:
                    item['update_check'] = None
                elif (not (recorded.get('comparison_source') or recorded.get('source'))
                      or source_key(recorded.get('comparison_source') or recorded.get('source')) == source_key(current)):
                    if observed_at(recorded) >= observed_at(item.get('update_check') or {}):
                        item['update_check'] = recorded
            item['update_check'] = normalize_check(item.get('update_check'),item.get('version'))
            item.setdefault('synced_at', payload['cache'].get('generated_at'))
        if source_changed:
            from insights import summarize
            payload.update(summarize(payload['items']))
        from check_results import reconcile
        reconcile(payload['items'])
        return payload

    @contextlib.contextmanager
    def lock(self):
        """OS lock is released on crash; never remove another process's lock file."""
        with (self.home / 'maintenance.lock').open('a+b') as f:
            if os.name == 'nt':
                import msvcrt
                f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise ValueError('Another maintenance operation is running')
                try:
                    yield
                finally:
                    f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError('Another maintenance operation is running')
                try:
                    yield
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)

    def select(self, selector, platform=''):
        row = self.r.find_one(selector, platform)
        if not row:
            raise ValueError('Skill not found; scan first: ' + selector)
        item = dict(row)
        if item.get('source_type') == 'builtin':
            item['source_type'] = self.a.normalize_source(json.loads(item.get('metadata_json') or '{}'), Path(item['path']))
        return item

    def state(self, item):
        saved = self.db.execute('select * from maintenance_items where id=?', (item['id'],)).fetchone()
        if saved:
            return dict(saved)
        self.db.execute('insert or ignore into maintenance_items(id) values (?)', (item['id'],))
        self.db.commit()
        return dict(self.db.execute('select * from maintenance_items where id=?', (item['id'],)).fetchone())

    def register_changed_path(self, actual):
        """Refresh one installed location and its known root/link aliases."""
        actual = Path(actual)
        real = actual.resolve()
        candidates = {(i['platform'], i['path'], i['status']) for i in self.cached_inventory().get('items', [])
                      if i.get('real_path') == str(real)}
        candidates.update((r['platform'], r['path'], r['status']) for r in
                          self.db.execute('select platform,path,status from capabilities where path=?', (str(actual),)))
        for platform, root, status in self.roots():
            resolved_root = root.resolve()
            if resolved_root in real.parents:
                candidates.add((platform, str(root / real.relative_to(resolved_root)), status))
        result = []
        for platform, value, status in sorted(candidates):
            ref = Path(value)
            if not ref.is_dir() or not (ref / 'SKILL.md').is_file():
                continue
            previous = self.db.execute('select * from capabilities where platform=? and path=?', (platform, value)).fetchone()
            cap = self.a.capability_from_skill_md(platform, ref, ref / 'SKILL.md')
            cap.update(status=status)
            if previous: cap['id'] = previous['id']
            self.r.upsert_capability(cap)
            self.db.execute('insert or ignore into maintenance_items(id) values (?)', (cap['id'],))
            fs = ref.lstat()
            self.db.execute('insert into maintenance_fs_identity values (?,?,?,?) on conflict(platform,device,inode) do update set item_id=excluded.item_id',
                            (platform, fs.st_dev, fs.st_ino, cap['id']))
            self.db.execute('insert or ignore into maintenance_item_dates(id,first_seen_at) values (?,?)', (cap['id'], stamp()))
            result.append((cap, dict(previous) if previous else cap))
        return result

    def roots(self):
        from skill_locations import locations
        roots = []
        entries, _ = locations(self.a.load_config())
        for entry in entries:
            if entry['enabled']:
                p = Path(entry['expanded'])
                roots.extend([(entry['platform'], p, 'active'), (entry['platform'], self.a.disabled_root_for(p), 'inactive')])
        return roots

    def validate_target(self, path, item=None):
        path = absolute(path)
        from component_ownership import classify
        ownership = classify(path)
        if ownership['read_only']:
            raise ValueError(ownership['reason'])
        matching = [root for _, root, _ in self.roots() if path != root and root in path.parents]
        if not matching:
            raise ValueError('Target must be below an enabled configured root: ' + str(path))
        root = max(matching, key=lambda p: len(p.parts))
        if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
            raise ValueError('This Agent root is a shared symlink. Select the actual shared source directory to change content for all affected Agents; per-Agent removal is unavailable here.')
        # An intermediate link is never an implicit write target.
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError('Intermediate symlink; use its explicitly scanned reference: ' + str(parent))
        return path

    def affected(self, path):
        target = path.resolve()
        if path.is_symlink():
            fs = path.lstat()
            def same_reference(p):
                try:
                    s = p.lstat();return (s.st_dev,s.st_ino)==(fs.st_dev,fs.st_ino)
                except OSError:return False
            refs = [dict(r) for r in self.db.execute('select id,platform,path from capabilities') if same_reference(Path(r['path']))]
        else:
            refs = [dict(r) for r in self.db.execute('select id,platform,path from capabilities') if Path(r['path']).resolve() == target]
        return [{'id': r['id'], 'platform': r['platform'], 'path': r['path']} for r in refs]

    def affected_protected(self, path):
        # New Agent associations must not bypass protection on an older ID for
        # the same physical content. Link-only operations still affect the link.
        return any(self.db.execute('select protected from maintenance_items where id=?', (r['id'],)).fetchone()[0]
                   for r in self.affected(path)
                   if self.db.execute('select protected from maintenance_items where id=?', (r['id'],)).fetchone())

    def scan(self, project=None, import_logs=False):
        config = self.a.load_config()
        self.a.save_config(config)
        if project:
            project = absolute(project)
            for platform, relative in [('claude_code', '.claude/skills'), ('codex', '.agents/skills'), ('codex', '.codex/skills'), ('hermes', '.hermes/skills'), ('hermes', '.agents/skills'), ('openclaw', 'skills'), ('openclaw', '.agents/skills')]:
                settings = config['platforms'].setdefault(platform, {'enabled': True, 'roots': []})
                path = str(project / relative)
                if path not in settings['roots']:
                    settings['roots'].append(path)
            self.a.save_config(config)
        caps, reports, seen = [], [], set()
        old = {(r['platform'], r['path']): dict(r) for r in self.db.execute('select * from capabilities')}
        for platform, root, status in self.roots():
            report = {'platform': platform, 'root': str(root), 'status': status, 'errors': [], 'count': 0}
            found = []
            def walk(folder, ancestors, depth=0):
                if depth > 9:
                    return
                try:
                    real = folder.resolve()
                    if real in ancestors:
                        return
                    children = sorted(folder.iterdir())
                    if (folder / 'SKILL.md').is_file():
                        cap = self.a.capability_from_skill_md(platform, folder, folder / 'SKILL.md')
                        cap['status'] = status
                        found.append(cap)
                        return
                    for child in children:
                        if (child.name.startswith('.') and child.name not in ('.system','.codex-plugin','.claude-plugin')) or child.name in ('node_modules','__pycache__'):
                            continue
                        if child.is_symlink() and not child.exists():
                            cap = {'id': self.a.stable_id(platform, 'skill', child, child.name), 'name': child.name,
                                   'platform': platform, 'kind': 'skill', 'path': str(child), 'status': status,
                                   'health': 'broken-link', 'metadata': {'link_target': os.readlink(child)}}
                            found.append(cap)
                        elif child.is_dir():
                            walk(child, ancestors | {real}, depth + 1)
                        elif child.suffix == '.md' and root.name.removesuffix('.disabled') in ('commands', 'agents'):
                            cap = self.a.capability_from_markdown(platform, child)
                            cap['status'] = status; found.append(cap)
                except (OSError, ValueError, RuntimeError) as exc:
                    report['errors'].append({'path': str(folder), 'error': type(exc).__name__})
            if not exists(root):
                report['result'] = 'not-found'
            else:
                walk(root, set())
                report['result'] = 'incomplete' if report['errors'] else 'complete'
            for cap in found:
                key = (platform, cap['path'])
                if key in seen:
                    continue
                seen.add(key)
                if key in old:
                    cap['id'] = old[key]['id']
                fs = Path(cap['path']).lstat()
                identity = self.db.execute('select item_id from maintenance_fs_identity where platform=? and device=? and inode=?', (platform,fs.st_dev,fs.st_ino)).fetchone()
                if key not in old and identity:
                    previous = self.db.execute('select path from capabilities where id=?',(identity['item_id'],)).fetchone()
                    if previous and not exists(Path(previous['path'])):
                        cap['id'] = identity['item_id']
                        self.db.execute('update capabilities set path=? where id=?',(cap['path'],cap['id']))
                self.db.execute('insert into maintenance_fs_identity values (?,?,?,?) on conflict(platform,device,inode) do update set item_id=excluded.item_id',(platform,fs.st_dev,fs.st_ino,cap['id']))
                self.r.upsert_capability(cap); caps.append(cap)
                self.db.execute('insert or ignore into maintenance_item_dates(id,first_seen_at) values (?,?)', (cap['id'],stamp()))
                report['count'] += 1
            # Keep history and IDs. An incomplete root never invalidates old entries.
            for key, row in old.items():
                if key[0] != platform or key in seen:
                    continue
                p = Path(key[1])
                if root in p.parents and not report['errors']:
                    self.db.execute('update capabilities set health=? where id=?', ('not-scanned' if exists(p) else 'missing',row['id']))
            reports.append(report)
        self.db.commit()
        automation_errors = []
        usage = {'imported': False}
        if import_logs:
            try:
                usage = self.import_usage(days=90)
            except Exception as exc:
                # A log failure never discards successful directory discovery.
                automation_errors.append({'action':'import-usage','message':redact(str(exc))})
        payload = {'count': len(caps), 'roots': reports, 'usage_import': usage, 'automation_errors':automation_errors, 'scanned_at': stamp()}
        self.db.execute('insert into maintenance_scans values (?,?,?)', (uid('scan'), stamp(), json.dumps(payload)))
        self.db.commit()
        self.invalidate_cache()
        return payload

    def inventory(self, ids=None, lightweight=False):
        from component_ownership import Ownership
        ownership = Ownership()
        from insights import summarize
        from skill_presentation import concise
        from library_metadata import installation, file_candidates, CATEGORIES, TAGS
        from skill_locations import AGENTS, locations
        generation = self.db.execute('select generation from maintenance_inventory_cache where id=1').fetchone()[0]
        result, by_real, by_source, excluded = [], {}, {}, []
        target_sizes, backup_index = {}, {}
        snapshot = self.cached_inventory()
        lightweight = bool(lightweight and snapshot.get('cache', {}).get('available'))
        saved_items = {i['id']:i for i in snapshot.get('items', [])}
        basic_metadata, basic_text, fingerprints, ownership_cache, candidate_cache = {}, {}, {}, {}, {}
        installer_cache = {}
        # Scoped reads must not trigger global usage-count writes.
        if ids is None and not lightweight:
            raw_rows = self.r.all_capabilities()
        elif ids is None:
            raw_rows = self.db.execute('select * from capabilities').fetchall()
        else:
            if not isinstance(ids,list) or len(ids)>10000 or any(not isinstance(i,str) for i in ids):
                raise ValueError('Invalid inventory scope')
            cached = snapshot
            rows = self.db.execute('select * from capabilities where id in (select value from json_each(?))', (json.dumps(ids),)).fetchall()
            targets = {str(Path(r['path']).resolve()) for r in rows}
            related = set(ids) | {i['id'] for i in cached.get('items', []) if i.get('real_path') in targets}
            raw_rows = self.db.execute('select * from capabilities where id in (select value from json_each(?))', (json.dumps(sorted(related)),)).fetchall() if cached.get('cache', {}).get('available') else self.db.execute('select * from capabilities').fetchall()
        removed = {key for key, settings in self.a.load_config().get('platforms', {}).items() if settings.get('removed')}
        raw_rows = [row for row in raw_rows if row['platform'] not in removed]
        if ids is not None:
            if not isinstance(ids,list) or len(ids)>10000 or any(not isinstance(i,str) for i in ids):
                raise ValueError('Invalid inventory scope')
            requested=set(ids)
            # Include aliases of the same files, never unrelated Skill contents.
            cached = saved_items
            targets={str(Path(r['path']).resolve()) for r in raw_rows if r['id'] in requested}
            raw_rows=[r for r in raw_rows if r['id'] in requested or
                      (cached[r['id']]['real_path'] if r['id'] in cached else str(Path(r['path']).resolve())) in targets]
        self.db.executemany('insert or ignore into maintenance_items(id) values (?)', [(r['id'],) for r in raw_rows])
        self.db.commit()
        self.db.executemany('insert or ignore into maintenance_item_dates(id,first_seen_at) values (?,?)', [(r['id'],stamp()) for r in raw_rows])
        # Recover dates only from completed manager installations tied to the exact item ID.
        for history in (self.db.execute("select o.updated_at,p.payload from maintenance_operations o join maintenance_plans p on p.id=json_extract(o.payload,'$.plan_id') where o.action='install' and o.state='succeeded' order by o.updated_at desc") if ids is None else []):
            item_id = json.loads(history['payload']).get('item',{}).get('id')
            if item_id:
                self.db.execute('update maintenance_item_dates set installed_at=? where id=? and installed_at is null', (history['updated_at'],item_id))
        # Directory reads and hashing must never hold the SQLite writer lock.
        self.db.commit()
        selected_ids = json.dumps([r['id'] for r in raw_rows])
        dates = {r['id']:dict(r) for r in self.db.execute('select * from maintenance_item_dates where id in (select value from json_each(?))', (selected_ids,))}
        frequencies = self.library_frequencies(raw_rows)
        latest_reads = self.library_last_reads(raw_rows)
        states = {r['id']:dict(r) for r in self.db.execute('select * from maintenance_items where id in (select value from json_each(?))', (selected_ids,))}
        classification_states = {r['real_path']:json.loads(r['payload']) for r in self.db.execute('select real_path,payload from classification_state')}
        try: searched_candidates = json.loads((self.home/'source-candidates.json').read_text())
        except (OSError,ValueError): searched_candidates = {}
        labels = {r['real_path']:{'category':r['category'], 'tags':json.loads(r['tags'])} for r in self.db.execute('select * from maintenance_labels')}
        for saved in self.db.execute('select real_path,payload from source_candidates'):
            searched_candidates[saved['real_path']] = json.loads(saved['payload'])['candidates']
        roots, sources = self.roots(), {}
        descriptions_path = self.home / 'descriptions.json'
        try:
            descriptions = json.loads(descriptions_path.read_text())
        except (OSError, ValueError):
            descriptions = {}
        locations_list, location_warnings = locations(self.a.load_config())
        for raw in self.db.execute("select id,created_at,payload from maintenance_backups where state='available' order by created_at desc"):
            meta = json.loads(raw['payload'])
            backup_index.setdefault(meta['original_path'], []).append({'id':raw['id'], 'created_at':raw['created_at'], 'type':meta['type']})
        unique, target_inodes = set(), {}
        total = {'bytes': 0, 'allocated_bytes': 0}
        for raw in raw_rows:
            row = dict(raw); p = Path(row['path'])
            matching_roots = [root for platform, root, _ in roots if platform == row['platform'] and (root in p.parents or root==p)]
            if row['health'] == 'not-scanned' or not matching_roots:
                excluded.append({'id':row['id'],'path':row['path'],'reason':'Outside current discovery rules; retained for historical lookup'})
                continue
            state = states[row['id']]
            entry = {k: row.get(k) for k in ('id', 'name', 'description', 'platform', 'kind', 'path', 'status', 'health', 'version', 'source_type', 'last_used_at', 'usage_30d')}
            real_path = str(p.resolve())
            saved = saved_items.get(row['id'], {})
            reusable = lightweight and saved.get('real_path') == real_path
            if str(p) not in ownership_cache:
                ownership_cache[str(p)] = saved.get('ownership') if reusable and saved.get('ownership') else ownership.classify(p)
            entry['ownership'] = ownership_cache[str(p)]
            # Old cached source flags are not evidence of component ownership.
            if entry['source_type'] == 'builtin' and entry['ownership']['kind'] != 'system':
                entry['source_type'] = self.a.normalize_source(json.loads(row.get('metadata_json') or '{}'), p)
            entry['reference_type'] = 'symlink' if p.is_symlink() else 'directory' if p.is_dir() else 'file'
            if p.is_symlink():
                entry['health'] = 'ok' if p.exists() else 'broken-link'
            entry['real_path'] = real_path
            recorded = dates[row['id']]
            entry['installation'] = installation(p, recorded['installed_at'], recorded['first_seen_at'], installer_cache)
            entry['classification'] = labels.get(entry['real_path'], {'category':'', 'tags':[]})
            entry['classification_meta'] = classification_states.get(real_path, {})
            if reusable:
                entry['local_source_candidates'] = saved.get('local_source_candidates', [])
            else:
                if real_path not in candidate_cache:
                    candidate_cache[real_path] = file_candidates(self.a, p, registry=self.r) if row['kind']=='skill' and p.is_dir() and not entry['ownership']['read_only'] else []
                entry['local_source_candidates'] = candidate_cache[real_path] + searched_candidates.get(real_path, [])
            entry['usage_frequency'] = frequencies.get(entry['real_path'], {'today':None, '7':None, '14':None, '30':None, '90':None})
            entry['usage_last_recorded_at'] = latest_reads.get(entry['real_path'])
            meta = json.loads(row.get('metadata_json') or '{}')
            if lightweight and row['kind'] == 'skill' and p.is_dir():
                if real_path not in basic_metadata:
                    try:
                        basic_text[real_path] = (p/'SKILL.md').read_text(encoding='utf-8')
                        basic_metadata[real_path] = self.a.parse_frontmatter_text(basic_text[real_path])
                    except (OSError, UnicodeError):
                        basic_metadata[real_path] = None
                current_meta = basic_metadata[real_path]
                if current_meta is not None:
                    inferred = self.a.infer_version(p.name, current_meta)
                    entry['version'] = (self.a.version_from_path(p) or inferred) if inferred == 'unknown' else inferred
                    entry['description'] = current_meta.get('description') or entry['description']
            if (not entry.get('description') or re.fullmatch(r'[|>][+-]?', str(entry.get('description') or '').strip())) and p.is_dir():
                try:
                    if real_path not in basic_text:
                        basic_text[real_path] = (p / 'SKILL.md').read_text(encoding='utf-8')
                    skill_text = basic_text[real_path]
                    parsed = self.a.parse_frontmatter_text(skill_text)
                    entry['description'] = parsed.get('description', entry['description'])
                    if not entry['description']:
                        body = skill_text.split('---', 2)[-1] if skill_text.startswith('---') else skill_text
                        intro = next((line.strip() for line in body.splitlines()
                                      if line.strip() and not line.lstrip().startswith(('#', '-', '*', '`', '>'))), '')
                        entry['description'] = intro
                        entry['description_origin'] = 'skill-intro'
                except (OSError, UnicodeError):
                    pass
            # Saved legacy briefs stay on disk but never override upstream metadata.
            brief = concise(entry.get('description'), row.get('name', ''), meta)
            entry['display_description'] = brief['text']
            entry['description_basis'] = brief['basis']
            entry['storage_roots'] = sorted({str(root.resolve()) for root in matching_roots})
            aliased_roots = [root for _,root,_ in roots if root.is_symlink() and root in p.parents]
            if aliased_roots and not p.is_symlink(): entry['reference_type'] = 'shared-root'
            entry['link_target'] = os.readlink(p) if p.is_symlink() else str(p.resolve()) if aliased_roots else None
            entry['present'] = exists(p)
            entry['protected'] = bool(state['protected'])
            entry['usage_status'] = 'observed' if row.get('last_used_at') else 'unknown'
            entry['usage_note'] = 'Recorded reads are evidence, not proof of complete runtime coverage.'
            source_key = (entry['real_path'], state['source'], row.get('metadata_json'), row.get('github_url'), row.get('github_ref'), row.get('github_path'))
            if source_key not in sources:
                sources[source_key] = (json.loads(state['source']) if state['source'] else saved.get('source') if reusable else None) if lightweight else self.source(row, state, installer_cache=installer_cache)
            entry['source'] = sources[source_key]
            entry['update_check'] = json.loads(state['last_check']) if state['last_check'] else None
            if not lightweight and entry['update_check'] and entry['update_check'].get('local_fingerprint') and entry['present']:
                try:
                    if real_path not in fingerprints:
                        fingerprints[real_path] = fingerprint(manifest(p.resolve(), ignore_git=True))
                    if fingerprints[real_path] != entry['update_check']['local_fingerprint']:
                        entry['update_check'] = {**entry['update_check'], 'status':'stale', 'message':'Local content changed since the check'}
                except (OSError, ValueError):
                    entry['update_check'] = {**entry['update_check'], 'status':'stale'}
            from version_policy import normalize_check
            if lightweight and entry['update_check'] and reusable and entry['version'] != saved.get('version'):
                entry['update_check'] = {**entry['update_check'], 'local_version':entry['version']}
                if entry['update_check']['status'] == 'latest':
                    entry['update_check']['status'] = 'baseline_unknown'
            entry['update_check'] = normalize_check(entry['update_check'],entry.get('version'))
            entry['synced_at'] = stamp()
            entry['backup_summary'] = {'count':len(backup_index.get(row['path'], [])),
                                       'latest':next(iter(backup_index.get(row['path'], [])), None),
                                       'verification':'Verified at creation; verify again before restoring'}
            try:
                if lightweight:
                    entry['space'] = saved.get('space', {}) if reusable and entry['present'] else {'bytes':0,'allocated_bytes':0}
                    entry['target_space'] = saved.get('target_space', entry['space']) if reusable and entry['present'] else entry['space']
                    if entry['present'] and real_path not in target_sizes:
                        target_sizes[real_path] = entry['target_space']
                        for k in total:
                            total[k] += entry['target_space'].get(k, 0)
                else:
                    own_inodes = {}
                    if p.is_symlink():
                        entry['space'] = size(p, inode_sizes=own_inodes)
                    if real_path not in target_sizes:
                        records = target_inodes.setdefault(real_path, {})
                        target_sizes[real_path] = size(p.resolve(), inode_sizes=records)
                    entry['target_space'] = target_sizes[real_path]
                    if not p.is_symlink(): entry['space'] = entry['target_space']
                    for records in (own_inodes, target_inodes[real_path]):
                        for inode, occupied in records.items():
                            if inode not in unique:
                                unique.add(inode)
                                for k in total: total[k] += occupied[k]
            except OSError as exc:
                entry['space'] = {'error': type(exc).__name__}
            result.append(entry)
            if p.exists():
                by_real.setdefault(entry['real_path'], []).append(entry)
            src = entry['source']
            if src:
                key = json.dumps({k: src.get(k) for k in ('url', 'path')}, sort_keys=True)
                by_source.setdefault(key, []).append(entry)
        for group in by_real.values():
            confirmed = [e['installation'] for e in group if e['installation']['basis'] in ('installed','installer-record')]
            installation = min(confirmed or [e['installation'] for e in group], key=lambda d:d['at'] or '')
            for entry in group:
                entry['installation'] = installation
                entry['references'] = [e['id'] for e in group]
                agent_ids = {location['agent'] for location in locations_list if location.get('agent')}
                entry['known_agents'] = sorted({e['platform'] for e in group if e['platform'] in agent_ids})
                entry['shared'] = len(entry['known_agents']) > 1
                entry['multiple_references'] = len({e['path'] for e in group}) > 1
                entry['reference_paths'] = sorted({e['path'] for e in group})
        for entry in result:
            entry.setdefault('known_agents', [])
            entry.setdefault('shared', False)
        duplicate_content_fingerprints(result, saved_items, lightweight)
        from check_results import reconcile
        reconcile(result)
        last_scan = self.db.execute('select payload from maintenance_scans order by rowid desc limit 1').fetchone()
        from recovery import stored_space
        payload = {'items': result, 'last_scan':json.loads(last_scan['payload']) if last_scan else None,
                'excluded_registered_paths':excluded, 'shared_targets': [[e['id'] for e in g] for g in by_real.values() if g[0]['shared']],
                'locations':locations_list, 'location_warnings':location_warnings,
                'same_source_copies': [[e['id'] for e in g] for g in by_source.values() if len(g) > 1],
                'cleanup_candidates': sorted([{'id':i['id'],'name':i['name'],'path':i['path'],'space':i['space'],'usage_status':i['usage_status'],'reason':'Review occupied space; no inactivity claim'} for i in result if i['present'] and not i['protected'] and i['source_type']!='builtin'],key=lambda i:i['space'].get('allocated_bytes',0),reverse=True),
                'space': {'installed_unique': total, **stored_space(self), 'staged_plans':snapshot.get('space',{}).get('staged_plans',{'bytes':0,'allocated_bytes':0}) if ids is not None or lightweight else size(self.home/'plans')},
                'note': 'Allocated sizes are filesystem estimates; shared targets/hardlinks counted once. References identify scanned paths, not loaded sessions or complete Agent consumers. Unknown usage is not inactivity.'}
        payload['insights'] = summarize(result)
        from taxonomy import names
        payload['taxonomy'] = names(self)
        try: payload['usage_import'] = json.loads((self.home/'usage-import.json').read_text())
        except (OSError,ValueError): payload['usage_import'] = None
        payload['same_source_copies'] = [[t['id'] for t in g['targets']] for g in payload['insights']['independent_copies']]
        payload['cleanup_candidates'] = payload['insights']['space_review']
        payload['cache'] = {'schema':10, 'available':True, 'from_cache':False, 'generated_at':stamp(),
                            'lightweight':lightweight,
                            'generation':generation,
                            'dirty':self.db.execute('select generation from maintenance_inventory_cache where id=1').fetchone()[0] != generation,
                            'note':'Historical snapshot; external edits are not watched. Refresh and review a new plan before writes.'}
        if ids is not None:
            payload['scope']={'ids':ids,'refreshed_ids':[r['id'] for r in raw_rows]}
            # Patch saved rows without pretending unrelated stale rows were checked.
            cached = snapshot
            if cached.get('cache', {}).get('available') and not payload['cache']['dirty']:
                replaced = set(ids) | set(payload['scope']['refreshed_ids'])
                cached['items'] = [i for i in cached['items'] if i['id'] not in replaced] + result
                cached['cache']['recheck_ids'] = [i for i in cached['cache'].get('recheck_ids', []) if i not in replaced]
                payload['cache']['recheck_ids'] = cached['cache']['recheck_ids']
                cached['insights'] = summarize(cached['items'])
                payload['insights'] = cached['insights']
                cached['cleanup_candidates'] = cached['insights']['space_review']
                unique = {i['real_path']:i.get('target_space', i.get('space', {})) for i in cached['items'] if i['present'] and i['health']!='broken-link'}
                cached['space']['installed_unique'] = {key:sum(s.get(key,0) for s in unique.values()) for key in ('bytes','allocated_bytes')}
                from recovery import stored_space
                cached['space'].update(stored_space(self))
                cached['cache']['generated_at'] = stamp()
                self.db.execute('update maintenance_inventory_cache set payload=?,payload_generation=? where id=1 and generation=?',
                                (json.dumps(cached, ensure_ascii=False),generation,generation))
                self.db.commit()
            return payload
        # Never publish a snapshot assembled across a concurrent state change.
        self.db.execute('update maintenance_inventory_cache set payload=?,generated_at=?,payload_generation=? where id=1 and generation=?',
                        (json.dumps(payload,ensure_ascii=False), payload['cache']['generated_at'], generation,generation))
        self.db.commit()
        return payload

    def inspect_space(self, row):
        from insights import space_category
        path = Path(row['path']).resolve()
        if row['kind'] != 'skill' or not path.is_dir() or not (path / 'SKILL.md').is_file():
            raise ValueError('Select a present Skill directory')
        entries, seen = [], set()
        for child in sorted(path.iterdir()):
            try:
                entries.append({'name':child.name, 'category':'reference' if child.is_symlink() else space_category(child.name),
                                'space':size(child, seen), 'is_link':child.is_symlink()})
            except OSError as exc:
                entries.append({'name':child.name, 'category':'unknown', 'space':{'error':type(exc).__name__}})
        entries.sort(key=lambda i:i['space'].get('allocated_bytes',0), reverse=True)
        return {'id':row['id'], 'path':str(path), 'entries':entries, 'checked_at':stamp(),
                'note':'Immediate children only; symlink targets are not followed. Dependencies may be required, artifacts may contain deliverables, and version records support updates. No files removed.'}

    def install_roots(self):
        from skill_locations import locations
        descriptors, warnings = locations(self.a.load_config())
        result, seen = [], set()
        for platform, root, status in self.roots():
            if status != 'active' or root in seen or root.name in ('commands','agents') or root.is_symlink() or self.a.is_builtin_path(root / '__install__'):
                continue
            if any(p.is_symlink() for p in root.parents):
                continue
            seen.add(root)
            agents = sorted({e['agent'] for e in descriptors if e['enabled'] and e['agent'] and e['real_path'] == str(root.resolve())})
            result.append({'platform':platform, 'path':str(root), 'exists':root.is_dir(),
                           'agents':agents, 'location_type':'agent' if agents else 'custom'})
        return {'roots':result, 'warnings':warnings}

    def install_root(self, raw):
        root = absolute(raw)
        selected = next((r for r in self.install_roots()['roots'] if r['path'] == str(root)), None)
        if not selected:
            raise ValueError('Select an enabled editable Skill root; shared aliases and plugins cannot receive installs')
        return root, selected['platform']

    def identify_install(self, url, target_root):
        from urllib.parse import urlparse
        if urlparse(url).scheme != 'https' or urllib_credentials(url):
            raise ValueError('Use an HTTPS source URL without credentials')
        root, _ = self.install_root(target_root)
        pid = uid('selection'); directory = self.home / 'plans' / pid
        directory.mkdir(mode=0o700)
        try:
            (directory / 'download').mkdir()
            archive, source = self.a.install_source_from_url(url, directory / 'download')
            archive = archive.resolve()
            if directory.resolve() not in archive.parents:
                raise ValueError('Download escaped staging directory')
            if urllib_credentials(source.get('source_url',url)):
                raise ValueError('Source URL must not contain credentials')
            candidates = self.a.skill_install_candidates(archive, source.get('source_path',''), source.get('source_skill',''))
            for c in candidates:
                safe_relative(c['remote_path'])
                folder = archive / c['remote_path']
                if archive != folder.resolve() and archive not in folder.resolve().parents:
                    raise ValueError('Skill escaped downloaded archive')
                files = manifest(folder)
                if any(f['kind'] == 'link' for f in files.values()):
                    raise ValueError('Installation archive contains links; manual review required')
                c['fingerprint'] = fingerprint(files)
                c['space'] = size(folder)
            if not candidates: raise ValueError('No SKILL.md was found in this link')
            payload = {'id':pid, 'action':'install-selection', 'created_at':stamp(), 'target':str(root),
                       'archive':str(archive), 'source':source, 'candidates':candidates}
            self.db.execute('insert into maintenance_plans values (?,?,?,?)',(pid,stamp(),'selection',json.dumps(payload)))
            self.db.commit(); self.invalidate_cache()
            return payload
        except Exception:
            shutil.rmtree(directory)
            raise

    def plan_install(self, selection_id, remote_path):
        record = self.db.execute("select payload from maintenance_plans where id=? and state='selection'",(selection_id,)).fetchone()
        if not record: raise ValueError('Install selection not found; identify source again')
        selection = json.loads(record['payload'])
        if dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(selection['created_at']) > dt.timedelta(hours=24):
            raise ValueError('Install selection expired; identify source again')
        root, platform = self.install_root(selection['target'])
        candidate = next((c for c in selection['candidates'] if c['remote_path']==remote_path),None)
        if not candidate: raise ValueError('Select a recognized Skill')
        safe_relative(candidate['target_name'])
        target = root / candidate['target_name']
        self.validate_target(target)
        if exists(target): raise ValueError('Target already exists; use an update or synchronization preview')
        src = Path(selection['archive']) / candidate['remote_path']
        files = manifest(src)
        if fingerprint(files) != candidate['fingerprint']: raise ValueError('Downloaded content changed; identify source again')
        pid=uid('plan'); directory=self.home/'plans'/pid;directory.mkdir(mode=0o700)
        try:
            copy_path(src,directory/'content')
            if manifest(directory/'content') != files: raise ValueError('Installation staging verification failed')
            row=self.a.capability_from_skill_md(platform,target,directory/'content'/'SKILL.md')
            row.update(status='active')
            incoming=selection['source']
            source={'provider':incoming['source_type'], 'url':incoming['source_url'], 'ref':incoming.get('source_ref',''),
                    'path':remote_path if remote_path != '.' else '', 'evidence':'user-confirmed'}
            if source['provider'] in ('git', 'skillhub'):
                source.update(channel=incoming.get('channel'), slug=incoming.get('slug', ''))
            plan={'id':pid, 'action':'install', 'created_at':stamp(), 'target':str(target), 'install_root':str(root),
                  'install_agents':next(r['agents'] for r in self.install_roots()['roots'] if r['path']==str(root)),
                  'item':{k:v for k,v in row.items() if k not in ('metadata','description')}, 'source':source,
                  'before':fingerprint({}), 'after':fingerprint(files), 'baseline':json.dumps(files),
                  'differences':diff({},files), 'affected_references':self.affected(target), 'original_size':size(target),
                  'install_size':candidate['space'], 'reference_type':'directory',
                  'metadata_checks':{'name':bool(self.a.parse_frontmatter(directory/'content'/'SKILL.md').get('name')),'description':bool(self.a.parse_frontmatter(directory/'content'/'SKILL.md').get('description'))}}
            self.db.execute('insert into maintenance_plans values (?,?,?,?)',(pid,stamp(),'preview',json.dumps(plan)))
            self.db.commit();write_json(directory/'preview.json',plan)
            self.discard_plan(selection_id)
            return plan
        except Exception:
            shutil.rmtree(directory)
            raise

    def source(self, row, state=None, installer_cache=None):
        context = getattr(self, '_source_context', None)
        if context is not None and row['id'] in context['sources']:
            return context['sources'][row['id']]
        result = self._source(row, state, installer_cache)
        if context is not None: context['sources'][row['id']] = result
        return result

    def _source(self, row, state=None, installer_cache=None):
        context = getattr(self, '_source_context', None)
        if context is not None and installer_cache is None: installer_cache = context['installer']
        state = self.state(row) if state is None else state
        if state['source']:
            source = json.loads(state['source'])
            if source.get('provider') in ('skillhub', 'git') and not source.get('channel'):
                from update_providers import source as registered_source
                recovered = registered_source(self.a, source['url'], source.get('ref', ''), source.get('path', ''), source.get('evidence', 'installed-metadata'), registry=self.r)
                if recovered: return recovered
            return source
        from library_metadata import locked_source
        recovered = locked_source(self.a, row['path'], registry=self.r, installer_cache=installer_cache)
        if recovered: return recovered
        info = self.a.remote_info_for_cap(row)
        if not info:
            folder = Path(row['path'])
            parent = next((p for p in (folder,*folder.parents) if (p/'.git').exists()),None)
            if parent:
                try:
                    key = str(parent.resolve())
                    cache = context['git'] if context is not None else {}
                    if key not in cache:
                        run = lambda *args: subprocess.run(['git','-C',key,*args],capture_output=True,text=True,timeout=3,check=True).stdout.strip()
                        try:
                            url = run('remote','get-url','origin')
                            root = Path(run('rev-parse','--show-toplevel'))
                            ref = run('rev-parse','--abbrev-ref','HEAD')
                            cache[key] = (url, root, ref if ref!='HEAD' else run('rev-parse','HEAD'))
                        except (OSError, subprocess.SubprocessError):
                            cache[key] = None
                    if cache[key] is None: return None
                    url, root, ref = cache[key]
                    from library_metadata import repository
                    git_source = repository(self.a, url, evidence='git-origin', registry=self.r)
                    if git_source:
                        git_source.update(ref=ref, path=str(folder.resolve().relative_to(root.resolve())).replace('\\','/') if folder.resolve()!=root.resolve() else '')
                        return git_source
                except (OSError,ValueError,subprocess.SubprocessError):
                    pass
            return None
        path = row.get('github_path') or info.get('path') or ''
        if path.endswith('SKILL.md'):
            path = str(Path(path).parent)
        provider = info.get('provider','github')
        canonical = f"https://github.com/{info['owner']}/{info['repo']}" if provider=='github' else f"https://gitlab.com/{info['project']}" if provider=='gitlab' else self.a.git_repo_url(info)
        if urllib_credentials(canonical): return None
        return {'provider':provider,'url':canonical,'ref':row.get('github_ref') or info.get('ref') or '', 'path':path if path!='.' else '', 'evidence':'installed-metadata'}


    def bind_source(self, row, url, ref='', path=''):
        from library_metadata import repository
        source = repository(self.a, url, ref, path, 'user-confirmed')
        if not source: raise ValueError('Use a public supported HTTPS Git repository URL without credentials')
        safe_relative(source['path'])
        self.state(row)
        self.db.execute('update maintenance_items set source=?,baseline=null,last_check=null where id=?', (json.dumps(source), row['id']))
        self.db.commit()
        self.invalidate_cache()
        self.r.log_operation('source-bind', row['id'], source)
        return source

    def remote_json(self, url):
        from update_checks import path_lock
        with path_lock('github-json:'+url):
            return self._remote_json(url)

    def _remote_json(self, url):
        cache = getattr(self, '_remote_json_cache', None)
        if cache is None: self._remote_json_cache = cache = {}
        if url not in cache:
            try: cache[url] = self.a.github_json(url)
            except Exception as exc: cache[url] = exc
        if isinstance(cache[url],Exception): raise cache[url]
        return cache[url]

    def set_labels(self, targets, category=None, tags=None, mode='replace'):
        from taxonomy import names
        values = names(self)
        CATEGORIES, TAGS = values['categories'], values['tags']
        if not isinstance(targets,list) or not targets or len(targets)>3000:
            raise ValueError('Select one or more Skills')
        if category is not None and category not in ('',*CATEGORIES): raise ValueError('Unknown category')
        if tags is not None and (not isinstance(tags,list) or any(t not in TAGS for t in tags)):
            raise ValueError('Unknown tag')
        if mode not in ('replace','add','remove'): raise ValueError('Invalid tag mode')
        paths = {str(Path(self.select(t)['path']).resolve()) for t in targets}
        updates = []
        for path in paths:
            old = self.db.execute('select * from maintenance_labels where real_path=?',(path,)).fetchone()
            previous = json.loads(old['tags']) if old else []
            new = previous if tags is None else list(dict.fromkeys(tags)) if mode=='replace' else list(dict.fromkeys(previous+tags)) if mode=='add' else [t for t in previous if t not in tags]
            if len(new)>3: raise ValueError('Each Skill can have at most three tags')
            updates.append((path, category if category is not None else old['category'] if old else '',json.dumps(new,ensure_ascii=False)))
        with self.db:
            self.db.executemany('insert into maintenance_labels values (?,?,?) on conflict(real_path) do update set category=excluded.category,tags=excluded.tags',updates)
            from classification import mark_manual
            for path,_,_ in updates: mark_manual(self,path,category,tags)
        self.invalidate_cache()
        return {'saved':len(updates)}

    def import_usage(self, days=90, replay=False, roots=None, progress=None):
        result = self.a.import_usage_from_logs(self.r, days, replay=replay, roots=roots, progress=progress)
        result['imported_at'] = stamp()
        write_json(self.home/'usage-import.json',result)
        self.invalidate_cache()
        return result

    def discover_sources(self, selector=None, remote=False):
        from library_metadata import file_candidates, repository
        if remote and not selector: raise ValueError('Select a Skill for remote search')
        rows = [self.select(selector)] if selector else [dict(r) for r in self.r.all_capabilities()]
        results = []
        for row in rows:
            if row['kind']!='skill' or row['source_type']=='builtin' or not Path(row['path']).is_dir(): continue
            source = self.source(row)
            candidates = file_candidates(self.a,row['path']) if not source else []
            trace = []
            if remote and source and source.get('provider') == 'github':
                try:
                    found = self.probe_source(row['id'], source['url'], source.get('ref',''))
                    candidates.extend(found['candidates'])
                    trace.append({'channel':'original-repository','status':'partial' if found['truncated'] else 'success',
                                  'candidates':len(found['candidates'])})
                except Exception as exc:
                    from diagnostics import classify
                    code, message = classify(exc)
                    trace.append({'channel':'original-repository','status':'failed','reason':message,'code':code})
            if remote and not any(c.get('evidence') == 'content-match' for c in candidates):
                from source_search import search, cached_matches
                candidates.extend(cached_matches(self,row))
                found = search(self, row['name'])
                candidates.extend(found['candidates'])
                trace.extend(found['trace'])
            if remote:
                from source_search import save_candidates
                save_candidates(self,Path(row['path']).resolve(),candidates,trace)
            results.append({'id':row['id'],'name':row['name'],'source':source,'candidates':candidates,'trace':trace})
        self.invalidate_cache()
        return {'items':results,'identified':sum(bool(r['source']) for r in results),'candidates':sum(bool(r['candidates']) for r in results),'note':'Candidates are not confirmed update sources.'}

    def probe_source(self, selector, url, ref=''):
        from library_metadata import repository
        from urllib.parse import quote
        row=self.select(selector)
        source=repository(self.a,url,ref)
        if not source or source['provider']!='github': raise ValueError('Repository content matching currently supports GitHub')
        info=self.a.parse_github_url(source['url'])
        api=f"https://api.github.com/repos/{info['owner']}/{info['repo']}"
        ref=source['ref'] or self.remote_json(api)['default_branch']
        commit=self.remote_json(api+'/commits/'+quote(ref,safe=''))['sha']
        tree=self.remote_json(api+'/git/trees/'+commit+'?recursive=1')
        from source_search import remember_tree
        remember_tree(self,{**source,'ref':ref},commit,tree)
        folder=Path(row['path']).resolve()
        body=(folder/'SKILL.md').read_bytes()
        blob=hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest()
        candidates=[]
        for entry in tree.get('tree',[]):
            path=entry.get('path','')
            if entry.get('type')!='blob' or Path(path).name!='SKILL.md': continue
            exact=entry.get('sha')==blob
            if exact or Path(path).parent.name in (folder.name,row['name']):
                candidates.append({'source':{**source,'path':str(Path(path).parent) if Path(path).parent!=Path('.') else '', 'ref':ref},'evidence':'content-match' if exact else 'name-only','from_path':path})
        return {'candidates':candidates[:20],'truncated':bool(tree.get('truncated')),'commit':commit,'note':'Review the exact skill directory before confirming a source.'}

    def remote(self, row, folder):
        folder = Path(folder).resolve()
        source = self.source(row)
        if not source:
            raise ValueError('Source unknown; bind the confirmed repository and skill subdirectory first')
        if source.get('provider') in ('skillhub', 'git'):
            from update_providers import download
            return download(self.a, source, folder)
        if source.get('provider') != 'github':
            raise ValueError('Safe maintenance currently supports GitHub; other channels remain available for discovery')
        if not self.r.channel_enabled('github'):
            raise ValueError('GitHub 更新渠道已停用')
        info = self.a.parse_github_url(source['url'])
        if not info:
            raise ValueError('Invalid GitHub source')
        info = {**info, '_skip_unrelated_links':True}
        safe_relative(source['path'])
        api = f"https://api.github.com/repos/{info['owner']}/{info['repo']}"
        from github_transport import can_fallback, resolve
        from urllib.parse import quote
        api_state = getattr(self, '_github_api_state', {})
        transport = 'github-api'
        if api_state.get('limited'):
            transport = 'git-https'
            commit, ref = resolve(self, source)
        else:
            try:
                ref = source['ref'] or self.remote_json(api)['default_branch']
                commit = self.remote_json(api + '/commits/' + quote(ref, safe=''))['sha']
            except Exception as exc:
                if not can_fallback(exc): raise
                transport = 'git-https'
                from diagnostics import classify
                if classify(exc)[0] == 'remote_rate_limit':
                    from update_checks import rate_limit_deadline
                    api_state['limited'] = True
                    api_state['retry_at'] = rate_limit_deadline(exc)
                commit, ref = resolve(self, source)
        info['_direct_archive'] = transport == 'git-https'
        if getattr(self,'_targeted_checks',False) and getattr(self,'_comparison_only',False):
            from github_subset import snapshot
            skill, stats = snapshot(self,source,commit,folder,Path(row['path']).resolve(),use_git=transport=='git-https')
            return skill, {**source,'resolved_ref':ref,'commit':commit,'transport':transport,**stats}
        archive_home = getattr(self,'_repository_archive_home',None)
        if archive_home:
            archive_home = Path(archive_home).resolve()
            key = hashlib.sha256((source['url']+commit).encode()).hexdigest()
            cache = getattr(self,'_archive_cache',None)
            if cache is None: self._archive_cache = cache = {}
            if key not in cache:
                destination=archive_home/key; destination.mkdir()
                try: cache[key]=self.a.download_github_archive(info,commit,destination)
                except Exception as exc: cache[key]=exc
            if isinstance(cache[key],Exception): raise cache[key]
            root=cache[key]
        else: root = self.a.download_github_archive(info, commit, folder)
        skill = root / source['path']
        link_index = root.parent / '.asm-archive-links.json'
        if link_index.is_file():
            selected_path = skill.relative_to(root.parent)
            for archived in json.loads(link_index.read_text(encoding='utf-8')):
                linked = Path(archived)
                if linked == selected_path or linked in selected_path.parents or selected_path in linked.parents:
                    raise ValueError('Remote Skill contains symlinks; manual review required')
        if not (skill / 'SKILL.md').is_file():
            raise ValueError('Confirmed source path does not contain SKILL.md; bind its exact subdirectory')
        # Reject archive links; a downloaded skill must not escape its staged tree.
        m = manifest(skill)
        if any(e['kind'] == 'link' for e in m.values()):
            raise ValueError('Remote archive contains symlinks; manual review required')
        return skill, {**source, 'resolved_ref': ref, 'commit': commit, 'transport':transport}

    def check(self, row):
        if getattr(self, '_use_check_cache', False):
            from update_checks import compare
            try:
                return compare(self, row, getattr(self, '_force_check', False))
            except (OSError, ValueError) as exc:
                from diagnostics import record
                event = record(self.home, exc, 'check')
                checked = {'id':row['id'],'checked_at':stamp(),'status':'check_failed','cache_hit':False,'remote_requested':False,
                           'error':event['error'],'code':event['code'],'message':event['message']}
                self.state(row)
                self.db.execute('update maintenance_items set last_check=? where id=?',(json.dumps(checked),row['id']))
                self.db.commit()
                self.invalidate_cache()
                return checked
        return self._check_remote(row)

    def _check_remote(self, row):
        checked = {'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'status': 'source_unknown', 'id': row['id']}
        if self.source(row):
            try:
                import tempfile
                with tempfile.TemporaryDirectory(prefix='asm-check-') as tmp:
                    # The staging root is ours; canonicalize OS aliases such as macOS /var.
                    previous = getattr(self,'_comparison_only',False)
                    self._comparison_only = True
                    try: skill, source = self.remote(row, Path(tmp).resolve())
                    finally: self._comparison_only = previous
                    current = manifest(Path(row['path']).resolve(), ignore_git=True)
                    remote = manifest(skill)
                    delta = diff(current, remote)
                    state = self.state(row)
                    base = json.loads(state['baseline']) if state['baseline'] else None
                    from recovery import skill_info
                    remote_version = skill_info(self,skill).get('version','unknown')
                    local_version = skill_info(self,Path(row['path']).resolve()).get('version','unknown')
                    from version_policy import assess
                    checked.update(assess(current,remote,base,local_version,remote_version,self.a.version_gt),
                                   source=source, differences=delta, local_version=local_version,
                                   remote_version=remote_version, local_fingerprint=fingerprint(current))
                    relocated=source.get('relocated_path')
                    if relocated:
                        from check_results import source_key
                        original=self.source(row)
                        corrected={**original,'path':relocated,'evidence':'verified-relocation',
                                   'relocated_from':original['path']}
                        real=str(Path(row['path']).resolve())
                        references=getattr(self,'_check_references',None)
                        candidates=(references.get(real,[]) if references is not None
                                    else self.r.all_capabilities())
                        changes=[]
                        for candidate in candidates:
                            if str(Path(candidate['path']).resolve())!=real: continue
                            binding=self.source(candidate)
                            if not binding or binding.get('evidence')=='user-confirmed' or source_key(binding)!=source_key(original): continue
                            self.state(candidate)
                            changes.append((json.dumps(corrected),candidate['id']))
                            if getattr(self,'_source_context',None) is not None:
                                self._source_context['sources'][candidate['id']]=corrected
                        if changes:
                            self.db.executemany('update maintenance_items set source=? where id=?',changes)
                            self.db.commit()
                            self.invalidate_cache()
                        checked['source']={**source,'path':relocated}
                        checked['source_correction']=relocated
            except Exception as exc:
                from diagnostics import record
                event = record(self.home, exc, 'check')
                checked.update(status='check_failed', error=event['error'], error_id=event['error_id'], message=event['message'], code=event['code'])
                from network_io import transient
                checked['retryable'] = transient(exc)
                if event.get('source_issue'):checked['source_issue']=event['source_issue']
                if (self.source(row) or {}).get('provider') == 'skillhub' and event['code'] == 'remote_not_found':
                    checked['message'] = '更新源找不到此 Skill 或版本，请在来源设置核对地址；不会自动替换成同名项目。'
                if event['code'] == 'remote_rate_limit':
                    from update_checks import rate_limit_deadline
                    from urllib.parse import urlparse
                    checked['retry_at'] = rate_limit_deadline(exc)
                    remote_url = urlparse(getattr(exc, 'url', '') or '')
                    checked['rate_limit_scope'] = getattr(exc, 'rate_limit_scope', None) or (('github-archive' if remote_url.hostname == 'codeload.github.com' or '/zipball/' in remote_url.path else 'github-api') if (self.source(row) or {}).get('provider') == 'github' else 'provider')
        if not getattr(self, '_defer_check_save', False):
            self.state(row)
            self.db.execute('update maintenance_items set last_check=? where id=?', (json.dumps(checked), row['id']))
            self.db.commit()
            self.invalidate_cache()
        return checked

    def backups(self, verify=True, ids=None):
        from recovery import flags, candidates, historical_rows, enrich_backup, stored_space
        retained = flags(self)
        result = []
        if ids is not None and (not isinstance(ids, list) or len(ids)>10000 or any(not isinstance(i,str) for i in ids)):
            raise ValueError('Invalid backup scope')
        query = 'select * from maintenance_backups'
        if ids is not None: query += ' where id in (' + ','.join('?' for _ in ids) + ')'
        for r in self.db.execute(query + ' order by created_at desc', ids or []):
            data = json.loads(r['payload'])
            result.append({'id': r['id'], 'created_at': r['created_at'], 'path': r['path'], 'state': r['state'],
                           'original_path': data['original_path'], 'operation_id': data.get('operation_id'),
                           'type': data['type'], 'size':(data.get('size') or size(Path(r['path']))) if r['state']=='available' else {'bytes':0,'allocated_bytes':0}, 'item_id': data.get('item_id'),
                           'name': data.get('name') or Path(data['original_path']).name, 'reason': data.get('reason', 'manual'),
                           'retained':retained.get(r['id'],False), 'historical_path':data.get('historical_path'), 'original_date_unknown':data.get('original_date_unknown',False),
                           'affected_references':data.get('affected_references',[]),
                           'restore_status':'ready' if r['state']=='available' else 'unsupported', 'external':False,
                           'target_exists': bool(data.get('original_path')) and exists(Path(data['original_path'])),
                           'bundle':data.get('bundle',False), 'entry_count':data.get('entry_count'), 'scope':data.get('scope'), 'snapshot_at':data.get('snapshot_at'),
                           'verified': (self.verify_backup(r['id'])['valid'] if verify else None) if r['state'] == 'available' else False})
        known = {r['path'] for r in result}
        legacy = [str(p) for p in (self.home / 'backups').iterdir() if str(p) not in known] if ids is None else []
        return {'backups': [enrich_backup(self,b) for b in result+historical_rows(self, ids)], 'scope':ids,
                'space':stored_space(self),
                'candidates':candidates(self) if ids is None else [], 'legacy_unverified': legacy, 'note': 'Saved sizes are estimates; recovery validates actual files.'}

    def backup_record(self, bid):
        if isinstance(bid,str) and bid.startswith('historical-'):
            from recovery import historical_record
            return historical_record(self,bid)
        r = self.db.execute('select * from maintenance_backups where id=?', (bid,)).fetchone()
        if not r or r['state'] != 'available':
            raise ValueError('Available backup not found: ' + bid)
        meta = json.loads(r['payload'])
        return Path(r['path']), meta

    def verify_backup(self, bid):
        try:
            container, meta = self.backup_record(bid)
            if meta.get('external'):
                current=manifest(container)
                valid=meta['format']=='directory' and (container/'SKILL.md').is_file() and not any(v['kind']=='link' for v in current.values()) and fingerprint(current)==meta.get('fingerprint')
                return {'id':bid,'valid':valid}
            stored = json.loads((container / 'manifest.json').read_text())
            valid = stored == meta and manifest(container / 'content') == meta['manifest']
            return {'id': bid, 'valid': valid}
        except (ValueError, OSError, json.JSONDecodeError):
            return {'id': bid, 'valid': False}

    def make_backup(self, row, reason='manual', operation_id=None, move=False):
        source = Path(row['path']); before = manifest(source)
        if not before:
            raise ValueError('Cannot back up missing path')
        bid = uid('backup'); kind = 'quarantine' if move else 'backup'
        container = self.home / ('quarantine' if move else 'backups') / bid
        container.mkdir(mode=0o700)
        meta = {'id': bid, 'created_at': stamp(), 'original_path': str(source), 'item_id': row.get('id'),
                'platform': row.get('platform'), 'name': row.get('name'), 'type': kind, 'reason': reason,
                'operation_id': operation_id, 'affected_references':self.affected(source), 'manifest': before, 'size': size(source), 'baseline': self.state(row)['baseline'] if row.get('id') else None,
                'source':self.source(row) if row.get('id') else None}
        # Register before moving so crash recovery can locate the original content.
        write_json(container / 'manifest.json', meta)
        self.db.execute('insert into maintenance_backups values (?,?,?,?,?)', (bid, stamp(), str(container), json.dumps(meta), 'available'))
        self.db.commit()
        try:
            if move:
                os.replace(source, container / 'content')
            else:
                copy_path(source, container / 'content')
        except Exception:
            # Keep partial content for diagnosis, but never offer it as a ready backup.
            self.db.execute("update maintenance_backups set state='invalid' where id=?", (bid,))
            self.db.commit()
            raise
        if not self.verify_backup(bid)['valid'] or (not move and manifest(source) != before):
            if move and not exists(source) and exists(container / 'content'):
                os.replace(container / 'content', source)
            self.db.execute("update maintenance_backups set state='invalid' where id=?", (bid,)); self.db.commit()
            raise ValueError('Backup verification failed; target may have changed')
        self.invalidate_cache()
        return {'id': bid, 'path': str(container), 'verified': True, 'type': kind}

    def backup(self, row):
        with self.lock():
            oid = uid('op'); result = {'id':oid, 'action':'backup', 'target':row['path'], 'backups':[]}
            self.operation(oid, 'backup', 'running', result)
            try:
                backup = self.make_backup(row, operation_id=oid)
                result.update(state='succeeded', backups=[backup])
                self.operation(oid,'backup','succeeded',result)
                self.r.log_operation('backup',row['id'],result)
                return {**backup,'operation_id':oid}
            except Exception as exc:
                self.operation(oid,'backup','failed',{**result,'error':redact(str(exc))})
                raise

    def protect(self, row, protected):
        self.state(row)
        self.db.execute('update maintenance_items set protected=? where id=?', (int(protected), row['id']))
        self.db.commit()
        self.invalidate_cache()
        return {'id': row['id'], 'protected': protected}

    def ensure_backup_unused(self, bid, exclude=None, check_retained=True):
        from recovery import flags
        if check_retained and flags(self).get(bid): raise ValueError('Backup marked to retain; cancel that mark before deleting')
        for raw in self.db.execute("select id,payload from maintenance_plans where state in ('preview','running')"):
            if raw['id'] != exclude and json.loads(raw['payload']).get('backup_id') == bid:
                raise ValueError('Backup is referenced by a pending plan: '+raw['id'])
        _,meta=self.backup_record(bid)
        if meta.get('operation_id') and self.db.execute("select id from maintenance_operations where id=? and state='running'",(meta['operation_id'],)).fetchone():
            raise ValueError('Backup belongs to an interrupted/running operation; recover it first')

    def plan(self, action, row=None, source_row=None, backup_id=None, allow_changes=False, restore_path=None, bundle_item=None):
        pid = uid('plan'); directory = self.home / 'plans' / pid
        directory.mkdir(mode=0o700)
        plan = {'id': pid, 'action': action, 'created_at': stamp(), 'allow_local_changes': allow_changes}
        try:
            if action in ('purge','trash-backup','restore-backup'):
                container, meta = self.backup_record(backup_id)
                from recovery import ensure_storage_safe
                ensure_storage_safe(self, container)
                self.ensure_backup_unused(backup_id, check_retained=action!='restore-backup')
                if action != 'purge':
                    from backup_recycle import validate
                    validate(self, backup_id, action, meta)
                plan.update(backup_name=meta.get('name'), backup_id=backup_id, container=str(container), before=fingerprint(manifest(container)),
                            size=size(container), effect='Permanently remove this backup/quarantine entry; it cannot be restored afterward.')
                from backup_recycle import entry
                plan['recycle_marker'] = entry(self, backup_id)
                if action != 'purge': plan['effect'] = 'Move to recycle bin; recoverable.' if action=='trash-backup' else 'Restore to backup list; installed Skills are unchanged.'
            else:
                if action == 'restore':
                    container, meta = self.backup_record(backup_id)
                    if not self.verify_backup(backup_id)['valid']:
                        raise ValueError('Backup is damaged or incomplete')
                    if meta.get('bundle') and not bundle_item:raise ValueError('请选择备份包内的 Skills 和恢复位置')
                    if not (restore_path or meta['original_path']):raise ValueError('Choose a recovery location below a configured Skills root')
                    path = self.validate_target(restore_path or meta['original_path'])
                    row = dict(self.db.execute('select * from capabilities where path=?', (str(path),)).fetchone() or {}) or {'path': str(path), 'id': self.a.stable_id(meta.get('platform') or 'shared','skill',path,meta.get('name') or path.name), 'name': meta.get('name') or path.name, 'platform': meta.get('platform') or 'shared'}
                    staged = directory / 'content'
                    if meta.get('bundle'):
                        from snapshots import stage_item
                        entry=stage_item(self,backup_id,bundle_item,staged)
                        plan['snapshot_entry']=entry
                    else:copy_path(container if meta.get('external') else container / 'content', staged)
                    plan.update(backup_id=backup_id, baseline=meta.get('baseline'))
                    if exists(path) and not allow_changes:
                        raise ValueError('Restore conflict: target exists; inspect it, then preview with --allow-local-changes')
                else:
                    path = self.validate_target(row['path'], row)
                self.validate_target(path, row)
                state = self.state(row)
                if (state['protected'] or self.affected_protected(path)) and action in ('share','delete', 'deactivate', 'sync', 'update', 'restore'):
                    raise ValueError('Skill is protected on an affected reference; explicitly unprotect that reference before this operation')
                before = manifest(path)
                if action != 'restore' and not before:
                    raise ValueError('Path is missing')
                plan.update(item={k:v for k,v in row.items() if k not in ('metadata_json','description')}, target=str(path), before=fingerprint(before), affected_references=self.affected(path),
                            original_size=size(path), reference_type='symlink' if path.is_symlink() else 'directory')
                if action in ('activate', 'deactivate'):
                    roots = [root for _, root, status in self.roots() if status == ('active' if action == 'deactivate' else 'inactive') and root in path.parents]
                    if not roots: raise ValueError('Reference is not in requested state')
                    root = max(roots, key=lambda p: len(p.parts))
                    dest_root = self.a.disabled_root_for(root) if action == 'deactivate' else root.with_name(root.name[:-9])
                    destination = dest_root / path.relative_to(root)
                    self.validate_target(destination)
                    if exists(destination): raise ValueError('Destination already exists')
                    plan.update(destination=str(destination), destination_before=fingerprint({}))
                if action in ('share','sync', 'update'):
                    if path.is_symlink():
                        raise ValueError('Select the actual directory to modify shared content; references can only be moved/removed')
                    if not path.is_dir() or not (path / 'SKILL.md').is_file():
                        raise ValueError('Only skill directories can be synchronized/updated')
                    payload = directory / 'incoming'
                    if action in ('sync','share'):
                        src = Path(source_row['path']).resolve()
                        if src == path.resolve() or src in path.resolve().parents or path.resolve() in src.parents:
                            raise ValueError('Source and target must be separate, non-nested skill directories')
                        if not (src / 'SKILL.md').is_file(): raise ValueError('Source is not a skill directory')
                        source_before = manifest(src)
                        if action == 'share':
                            if os.name == 'nt':raise ValueError('Automatic shared links are not supported on Windows yet')
                            self.validate_target(Path(source_row['path']),source_row)
                            if Path(source_row['path']).is_symlink() or self.state(source_row)['protected'] or self.affected_protected(src):raise ValueError('Retained source must be an unprotected independent directory')
                            if source_row['name'].casefold()!=row['name'].casefold() or fingerprint(source_before)!=fingerprint(before):raise ValueError('Copies are not exactly equal; compare again')
                            if self.source(row)!=self.source(source_row):raise ValueError('Declared sources differ; review manually')
                            if any(e['kind']=='link' for e in before.values()):raise ValueError('Nested symlinks require manual review')
                        if action != 'share':
                            copy_path(src, payload)
                            if manifest(src) != source_before or manifest(payload) != source_before:
                                raise ValueError('Source changed during staging')
                        plan.update(source_target=str(src), source_before=fingerprint(source_before), source_id=source_row['id'])
                    else:
                        from update_providers import identity
                        plan['bound_source'] = self.source(row)
                        payload_src, remote = self.remote(row, directory)
                        if identity(self.source(row)) != identity(plan['bound_source']):
                            raise ValueError('Bound source changed during staging; preview again')
                        copy_path(payload_src, payload)
                        plan['source'] = remote
                    if action == 'share':
                        plan['baseline']=json.dumps(source_before)
                    else:
                        incoming = manifest(payload)
                        # Protect local extras, delete only files known from the last installed baseline.
                        base = json.loads(state['baseline']) if state['baseline'] else None
                        if action=='update':
                            from recovery import skill_info
                            from version_policy import assess
                            decision=assess(before,incoming,base,skill_info(self,path).get('version'),skill_info(self,payload).get('version'),self.a.version_gt)
                            plan['update_assessment']=decision
                            if decision['update_decision'] in ('review','keep_local') and not allow_changes:
                                raise ValueError('Local conflicts or no trusted baseline: '+decision['decision_reason']+'; review, then preview with --allow-local-changes')
                        changed = diff(before, incoming)['changed']
                        local_delta = diff(base, before) if base else None
                        blocked = changed if not base else sorted((set(changed) & set(local_delta['changed'] + local_delta['added'])) | (set(content(incoming)) & set(local_delta['removed'])))
                        if blocked and not allow_changes:
                            raise ValueError('Local conflicts or no trusted baseline: ' + ', '.join(blocked[:10]) + '; review, then preview with --allow-local-changes')
                        staged = directory / 'content'; copy_path(path, staged)
                        preserved = sorted(set(content(before)) - set(content(incoming)))
                        removed = []
                        for rel in preserved:
                            if base and rel in content(base) and content(before)[rel] == content(base)[rel]:
                                remove_path(staged / rel); removed.append(rel)
                        for rel, entry in incoming.items():
                            if rel == '.' or rel == '.git' or rel.startswith('.git/'):
                                continue
                            dest = staged / rel
                            # Never follow an existing symlink in the overlay.
                            for parent in dest.parents:
                                if parent == staged: break
                                if parent.is_symlink(): raise ValueError('Overlay would traverse a local symlink: ' + str(parent.relative_to(staged)))
                            if entry['kind'] == 'dir':
                                if dest.is_symlink(): raise ValueError('Overlay would traverse a local symlink: ' + rel)
                                if exists(dest) and not dest.is_dir(): remove_path(dest)
                                dest.mkdir(parents=True, exist_ok=True)
                            else:
                                if exists(dest): remove_path(dest)
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                copy_path(payload / rel, dest)
                        plan.update(baseline=json.dumps(incoming), preserved_local_files=[r for r in preserved if r not in removed], conflicts=blocked)
                if action == 'share':
                    remove_path(directory/'content')
                    (directory/'content').symlink_to(src,target_is_directory=True)
                    plan.update(retained_target=str(src), retained_references=self.affected(src), estimated_net_freed_bytes=0,
                                effect='Replace one identical copy with a symlink; verified backup remains on disk. Updates to the retained directory affect all linked Agents.')
                if action in ('share','restore', 'sync', 'update'):
                    after = manifest(directory / 'content')
                    plan.update(after=fingerprint(after), differences=diff(before, after))
                if action == 'delete':
                    plan['effect'] = 'Remove only this reference' if path.is_symlink() else 'Move content to recoverable quarantine'
                    plan['estimated_net_freed_bytes'] = 0
            self.db.execute('insert into maintenance_plans values (?,?,?,?)', (pid, stamp(), 'preview', json.dumps(plan)))
            self.db.commit()
            write_json(directory / 'preview.json', plan)
            self.invalidate_cache()
            return plan
        except Exception:
            shutil.rmtree(directory)
            raise

    def operation(self, oid, action, state, payload, commit=True):
        self.db.execute('insert into maintenance_operations values (?,?,?,?,?,?) on conflict(id) do update set updated_at=excluded.updated_at,state=excluded.state,payload=excluded.payload',
                        (oid, stamp(), stamp(), action, state, json.dumps(payload)))
        self.db.execute('update maintenance_inventory_cache set generation=generation+1 where id=1')
        if commit:
            self.db.commit()

    def update_now(self, row):
        from recovery import skill_info
        from version_policy import normalize_check
        current = manifest(Path(row['path']))
        before = fingerprint(current)
        check = json.loads(self.state(row)['last_check'] or '{}')
        check = normalize_check(check, skill_info(self, Path(row['path'])).get('version'))
        if check.get('status') != 'update_available' or check.get('local_fingerprint') != comparison_fingerprint(current):
            raise ValueError('请先重新检查更新，本地内容或更新记录已变化')
        source = self.source(row)
        plan = self.plan('update', row, allow_changes=True)
        try:
            # Recheck the downloaded release, not just a cached update label.
            if (plan['before'] != before or plan['update_assessment']['status'] != 'update_available'
                    or self.source(row) != source):
                raise ValueError('版本或本地内容已变化，请重新检查更新')
            return self.apply(plan['id'])
        except Exception:
            record = self.db.execute('select state from maintenance_plans where id=?', (plan['id'],)).fetchone()
            if record and record['state'] == 'preview':
                self.discard_plan(plan['id'])
            raise

    def validate_update_source(self, plan):
        from update_providers import identity, validate_source
        bound = plan.get('bound_source')
        if not bound or identity(self.source(self.select(plan['item']['id']))) != identity(bound):
            raise ValueError('Bound source changed; preview again')
        if bound.get('provider') in ('git', 'skillhub'):
            validate_source(self.a, bound, registry=self.r)
        elif bound.get('provider') == 'github' and not self.r.channel_enabled('github'):
            raise ValueError('GitHub 更新渠道已停用')

    def apply(self, pid):
        from backup_recycle import apply as apply_recycle
        if self.get_plan(pid).get('action') in ('trash-backup','restore-backup'):
            return apply_recycle(self, pid)
        with self.lock():
            pending = self.db.execute("select id from maintenance_operations where state='running'").fetchall()
            if pending:
                raise ValueError('Interrupted operation needs recovery first: ' + ', '.join(r['id'] for r in pending))
            rec = self.db.execute('select * from maintenance_plans where id=?', (pid,)).fetchone()
            if not rec: raise ValueError('Plan not found')
            if rec['state'] != 'preview': raise ValueError('Plan is already applied/failed; create a new preview')
            plan = json.loads(rec['payload'])
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(plan['created_at'])
            if age > dt.timedelta(hours=24): raise ValueError('Plan expired; preview again')
            action = plan['action']; directory = self.home / 'plans' / pid
            if action == 'purge':
                target, _ = self.backup_record(plan['backup_id'])
                from recovery import ensure_storage_safe
                ensure_storage_safe(self, target)
                self.ensure_backup_unused(plan['backup_id'],pid)
                from backup_recycle import entry
                if entry(self, plan['backup_id']) != plan.get('recycle_marker'):
                    raise ValueError('Backup recycle state changed; preview again')
                if fingerprint(manifest(target)) != plan['before']: raise ValueError('Backup changed; preview again')
            else:
                if action == 'install':
                    self.install_root(plan['install_root'])
                    if plan['source'].get('provider') in ('git', 'skillhub'):
                        from update_providers import validate_source
                        validate_source(self.a, plan['source'], registry=self.r)
                    current_agents = next(r['agents'] for r in self.install_roots()['roots'] if r['path']==plan['install_root'])
                    if 'install_agents' in plan and current_agents != plan['install_agents']:
                        raise ValueError('Known Agent references changed; preview again')
                target = self.validate_target(plan['target'], plan['item'])
                if action == 'install' and exists(target):
                    raise ValueError('Target changed: appeared; installation never overwrites an existing directory')
                if fingerprint(manifest(target)) != plan['before']: raise ValueError('Target changed; preview again')
                if self.affected(target) != plan['affected_references']: raise ValueError('Known Agent references changed; preview again')
                if (self.state(plan['item'])['protected'] or self.affected_protected(target)) and action in ('share','delete','deactivate','sync','update','restore'):
                    raise ValueError('Target became protected')
                if action in ('share','update','sync','restore','install'):
                    staged_manifest = manifest(directory / 'content')
                    if fingerprint(staged_manifest) != plan['after']:
                        raise ValueError('Staged content changed; preview again')
                if plan.get('source_target') and fingerprint(manifest(Path(plan['source_target']))) != plan['source_before']:
                    raise ValueError('Source changed; preview again')
                if action=='share':
                    source_row=self.select(plan['source_id'])
                    self.validate_target(Path(source_row['path']),source_row)
                    if self.affected(Path(plan['source_target']))!=plan['retained_references'] or self.affected_protected(Path(plan['source_target'])) or self.source(plan['item'])!=self.source(source_row):raise ValueError('Retained source references, protection or sources changed; preview again')
                if action == 'update':
                    self.validate_update_source(plan)
                if plan.get('destination'):
                    self.validate_target(plan['destination'])
                    if exists(Path(plan['destination'])): raise ValueError('Destination appeared; preview again')
            oid = uid('op'); result = {'id': oid, 'plan_id': pid, 'action': action, 'target': str(target), 'backups': []}
            self.operation(oid, action, 'running', result)
            self.db.execute("update maintenance_plans set state='running' where id=?", (pid,)); self.db.commit()
            hold = target.with_name('.asm-hold-' + oid)
            temp = target.with_name('.asm-stage-' + oid)
            moved = False
            installed = False
            relocated = False
            try:
                existing_parent = next(p for p in (target.parent,*target.parent.parents) if p.exists())
                free_before = shutil.disk_usage(existing_parent).free
                if action == 'purge':
                    remove_path(target)
                    self.db.execute("update maintenance_backups set state='purged' where id=?", (plan['backup_id'],))
                    if plan['backup_id'].startswith('historical-'):
                        self.db.execute("update maintenance_recovery_candidates set state='purged' where id=?",(plan['backup_id'],))
                elif action == 'delete':
                    self.validate_target(target, plan['item'])
                    b = self.make_backup(plan['item'], action, oid, move=True)
                    result['backups'].append(b)
                    result['net_freed_bytes_estimate'] = 0
                    self.db.execute("update capabilities set health='missing' where id=?", (plan['item']['id'],))
                    result['inventory_ids'] = [r['id'] for r in plan['affected_references']]
                elif action in ('activate','deactivate'):
                    b = self.make_backup(plan['item'], action, oid)
                    result['backups'].append(b); self.operation(oid, action, 'running', result)
                    dest = Path(plan['destination'])
                    self.validate_target(dest)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    self.validate_target(dest)
                    if exists(dest): raise ValueError('Destination appeared during backup; preview again')
                    if fingerprint(manifest(target)) != plan['before']: raise ValueError('Target changed during backup; preview again')
                    os.replace(target, dest)
                    relocated = True
                    self.db.execute('update capabilities set path=?,status=? where id=?', (str(dest), 'active' if action == 'activate' else 'inactive', plan['item']['id']))
                    result['destination'] = str(dest)
                else:
                    if action == 'install' and exists(target):
                        raise ValueError('Target changed: appeared during staging; installation never overwrites an existing directory')
                    if exists(target):
                        b = self.make_backup(plan['item'], action, oid)
                        result['backups'].append(b)
                    self.validate_target(target, plan['item'])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    copy_path(directory / 'content', temp)
                    if fingerprint(manifest(temp)) != plan['after']: raise ValueError('Staged copy verification failed')
                    result.update(hold=str(hold), temporary=str(temp), expected_after=plan['after'], originally_present=exists(target))
                    self.operation(oid, action, 'running', result)
                    if action == 'install' and exists(target):
                        raise ValueError('Target changed: appeared during staging; installation never overwrites an existing directory')
                    self.validate_target(target, plan['item'])
                    if action == 'update': self.validate_update_source(plan)
                    if action == 'install' and plan['source'].get('provider') in ('git', 'skillhub'):
                        validate_source(self.a, plan['source'], registry=self.r)
                    if fingerprint(manifest(target)) != plan['before']: raise ValueError('Target changed during backup/staging; preview again')
                    if exists(target): os.replace(target, hold); moved = True
                    os.replace(temp, target)
                    installed = True
                    if fingerprint(manifest(target)) != plan['after']: raise ValueError('Installed content verification failed')
                    if action=='share' and fingerprint(manifest(target.resolve()))!=plan['source_before']:raise ValueError('Shared source changed during apply')
                    if plan.get('item',{}).get('id'):
                        self.db.execute('update maintenance_items set baseline=? where id=?', (plan.get('baseline'), plan['item']['id']))
                        self.db.execute("update capabilities set health='ok' where id=?", (plan['item']['id'],))
                if action == 'install':
                    cap = self.a.capability_from_skill_md(plan['item']['platform'],target,target/'SKILL.md')
                    cap.update(id=plan['item']['id'],status='active')
                    self.r.upsert_capability(cap)
                    self.db.execute('update maintenance_items set source=?,baseline=? where id=?',
                                    (json.dumps(plan['source']) if plan['source'].get('provider')!='local' else None,plan['baseline'],cap['id']))
                    result['inventory_ids'] = [c['id'] for c, _ in self.register_changed_path(target)]
                    metadata=self.a.parse_frontmatter(target/'SKILL.md')
                    result['deployment']={'status':'deployed','agents':plan.get('install_agents',[]),'name_valid':bool(metadata.get('name')),'description_valid':bool(metadata.get('description')),'message':'已写入安装目录；Agent 在下次加载时发现。未验证运行时加载状态。'}
                if action in ('share','sync','update','restore','activate','deactivate'):
                    actual = Path(plan.get('destination') or str(target))
                    changed = self.register_changed_path(actual)
                    result['inventory_ids'] = [c['id'] for c, _ in changed]
                    for cap, raw in changed:
                        self.db.execute('update maintenance_items set last_check=null where id=?', (cap['id'],))
                        source = (self.source(cap) or {}) if action == 'update' else {}
                        if action == 'update' and all(source.get(k,'') == (plan.get('source') or {}).get(k,'') for k in ('provider','url','ref','path')):
                            checked = {'id':cap['id'], 'status':'latest', 'checked_at':stamp(),
                                       'source':plan['source'], 'remote_version':cap.get('version'),
                                       'remote_update_available':False, 'local_fingerprint':comparison_fingerprint(staged_manifest),
                                       'basis':'verified-update'}
                            self.db.execute('update maintenance_items set last_check=? where id=?', (json.dumps(checked), cap['id']))
                if action=='restore' and plan.get('snapshot_entry'):
                    from taxonomy import names
                    values = names(self)
                    CATEGORIES,TAGS = values['categories'],values['tags']
                    labels=plan['snapshot_entry'].get('labels') or {}
                    category=labels.get('category','');tags=labels.get('tags',[])
                    if category in ('',*CATEGORIES) and isinstance(tags,list) and all(t in TAGS for t in tags):
                        self.db.execute('insert or replace into maintenance_labels values (?,?,?)',(str(target.resolve()),category,json.dumps(tags)))
                result['filesystem_free_delta_bytes'] = shutil.disk_usage(target.parent).free - free_before
                result['space_note'] = 'Filesystem free delta includes concurrent system activity; quarantine frees approximately zero.'
                result['state'] = 'succeeded'
                self.operation(oid, action, 'succeeded', result, commit=False)
                if action == 'install':
                    self.db.execute('insert into maintenance_item_dates(id,first_seen_at,installed_at) values (?,?,?) on conflict(id) do update set installed_at=excluded.installed_at', (cap['id'],stamp(),stamp()))
                self.db.execute("update maintenance_plans set state='applied' where id=?", (pid,)); self.db.commit()
            except Exception as exc:
                self.db.rollback()
                if exists(hold):
                    if exists(target): remove_path(target)
                    os.replace(hold, target)
                elif installed and action in ('share','restore','sync','update','install') and not result.get('originally_present',True) and exists(target):
                    remove_path(target)
                elif relocated and exists(dest) and not exists(target):
                    os.replace(dest, target)
                elif action == 'delete' and result['backups'] and not exists(target):
                    container, _ = self.backup_record(result['backups'][-1]['id'])
                    copy_path(container / 'content', target)
                if exists(temp): remove_path(temp)
                result.update(state='failed', error=redact(str(exc)))
                self.operation(oid, action, 'failed', result)
                self.db.execute("update maintenance_plans set state='failed' where id=?", (pid,)); self.db.commit()
                raise ValueError(f"Operation {oid} failed; see history: {redact(str(exc))}") from exc
            # Filesystem rollback remains possible until the success record commits.
            if action in ('install','update'):
                try:
                    from classification import classify
                    with self.db:
                        result['classification']=classify(self,dict(self.select(plan['item']['id'])),apply=True)['after']
                except Exception as classification_error:
                    result['classification_pending']=True
                    result['classification_error']=type(classification_error).__name__
            try:
                if exists(hold): remove_path(hold)
                if directory.exists(): shutil.rmtree(directory)
            except OSError:
                result['cleanup_pending'] = True
            try:
                self.r.log_operation(action, plan.get('item',{}).get('id', plan.get('backup_id','')), result)
            except Exception:
                self.db.rollback()
                result['history_pending'] = True
            return result

    def library_last_reads(self, raw_rows, now=None):
        """Latest valid non-future read, including evidence older than 90 days."""
        now = now or dt.datetime.now(dt.timezone.utc)
        aliases = {r['id']:str(Path(r['path']).resolve()) for r in raw_rows}
        result = {}
        for event in self.db.execute("select capability_id,occurred_at from usage_events where capability_id in (select value from json_each(?)) and julianday(occurred_at)<=julianday(?) order by julianday(occurred_at) desc", (json.dumps(list(aliases)), now.isoformat())):
            real = aliases.get(event['capability_id'])
            if real is None or real in result:
                continue
            try:
                at = dt.datetime.fromisoformat(event['occurred_at'].replace('Z','+00:00'))
            except (ValueError,AttributeError):
                continue
            if at.tzinfo is not None and at <= now:
                result[real] = at.astimezone(dt.timezone.utc).isoformat()
        return result

    def library_frequencies(self, raw_rows, now=None):
        """Count imported read evidence once per real path, never infer inactivity."""
        local_midnight = dt.datetime.combine(dt.date.today(),dt.time.min).astimezone() if now is None else now.replace(hour=0,minute=0,second=0,microsecond=0)
        now = now or dt.datetime.now().astimezone()
        if now.tzinfo is None:
            raise ValueError('Frequency reference time requires a timezone')
        cutoffs = {'today':local_midnight,
                   **{str(days):now.astimezone(dt.timezone.utc)-dt.timedelta(days=days) for days in (7,14,30,90)}}
        aliases = {r['id']:str(Path(r['path']).resolve()) for r in raw_rows}
        seen, counts = set(), {}
        for event in self.db.execute("select capability_id,occurred_at,source,confidence,session_id,evidence from usage_events where capability_id in (select value from json_each(?)) and julianday(occurred_at)>=julianday(?)", (json.dumps(list(aliases)), cutoffs['90'].isoformat())):
            real = aliases.get(event['capability_id'])
            if real is None:
                continue
            try:
                at = dt.datetime.fromisoformat(event['occurred_at'].replace('Z','+00:00'))
            except (ValueError, AttributeError):
                continue
            if at.tzinfo is None or at > now:
                continue
            key = (real,at.isoformat(),event['source'],event['confidence'],event['session_id'],event['evidence'])
            if key in seen:
                continue
            seen.add(key)
            bucket = counts.setdefault(real, {period:None for period in cutoffs})
            for period, cutoff in cutoffs.items():
                if at >= cutoff:
                    bucket[period] = (bucket[period] or 0)+1
        return counts

    def usage(self, days=30, selector=None, platform=''):
        if days < 1: raise ValueError('Days must be positive')
        cutoff = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=days)).isoformat()
        selected = self.select(selector,platform) if selector else None
        all_rows = [dict(r) for r in self.db.execute('select * from capabilities').fetchall()]
        alias_map = {}
        for r in all_rows: alias_map.setdefault(str(Path(r['path']).resolve()),[]).append(r['id'])
        rows = [selected] if selected else all_rows
        result = []
        for row in rows:
            aliases = alias_map.get(str(Path(row['path']).resolve()),[row['id']])
            marks = ','.join('?' for _ in aliases)
            events = [dict(r) for r in self.db.execute('select occurred_at,source,confidence,session_id,evidence from usage_events where capability_id in ('+marks+') and julianday(occurred_at)>=julianday(?) order by julianday(occurred_at) desc',(*aliases,cutoff))]
            unique = {json.dumps(e,sort_keys=True):e for e in events}; events=list(unique.values())
            daily = {}
            for event in events:
                try:
                    at=dt.datetime.fromisoformat(event['occurred_at'].replace('Z','+00:00'))
                    if not at.tzinfo or at>dt.datetime.now(dt.timezone.utc): continue
                    day=at.astimezone().date().isoformat()
                except (ValueError,AttributeError): continue
                bucket=daily.setdefault(day,{'date':day,'records':0,'invocations':0})
                bucket['records']+=1
                bucket['invocations']+=int(event['confidence']=='invocation')
            first=self.db.execute('select occurred_at from usage_events where capability_id in ('+marks+') order by julianday(occurred_at) asc limit 1',aliases).fetchone()
            last=self.db.execute('select occurred_at from usage_events where capability_id in ('+marks+') order by julianday(occurred_at) desc limit 1',aliases).fetchone()
            result.append({'id':row['id'],'name':row['name'],'platform':row['platform'],'path':row['path'],'days':days,'recorded_reads':len(events),'status':'observed' if events else 'unknown','coverage':'partial evidence; does not establish inactivity','evidence_range':{'first':first[0] if first else None,'last':last[0] if last else None},'events':events[:100] if selected else [], 'daily':sorted(daily.values(),key=lambda x:x['date'],reverse=True), 'invocations':sum(x['invocations'] for x in daily.values())})
        return {'items':result,'days':days,'note':'Session logs capture some SKILL.md reads. Recorded reads are not execution counts. No records means unknown; observation endpoints do not prove continuous coverage.'}

    def history(self, limit=50):
        rows = self.db.execute('select * from maintenance_operations order by created_at desc limit ?', (limit,)).fetchall()
        internal = ('source-channel-search','usage-import','scan','status','report','health','smart-upgrade-scan')
        marks = ','.join('?' for _ in internal)
        return {'interrupted_operations':[{**dict(r), 'payload':json.loads(r['payload'])} for r in self.db.execute("select * from maintenance_operations where state='running' order by created_at desc")],
                'scans':[json.loads(r['payload']) for r in self.db.execute('select payload from maintenance_scans order by created_at desc limit ?', (limit,))],
                'operations': [{**dict(r), 'payload': json.loads(r['payload'])} for r in rows],
                'legacy_operations': [dict(r) for r in self.db.execute('select * from operation_logs order by id desc limit ?', (limit,))],
                'user_legacy_operations': [dict(r) for r in self.db.execute('select * from operation_logs where action not in ('+marks+') order by id desc limit ?', (*internal,limit))]}

    def recover(self, oid):
        with self.lock():
            row = self.db.execute("select * from maintenance_operations where id=? and state='running'", (oid,)).fetchone()
            if not row: raise ValueError('Interrupted operation not found')
            payload = json.loads(row['payload']); action = row['action']
            target = Path(payload['target'])
            if action in ('share','update','sync','restore','install'):
                hold = Path(payload.get('hold', str(target.with_name('.asm-hold-' + oid))))
                temp = Path(payload.get('temporary', str(target.with_name('.asm-stage-' + oid))))
                # If target verifies, final placement completed; otherwise restore the held original.
                if payload.get('expected_after') and fingerprint(manifest(target)) == payload['expected_after']:
                    if action=='share':
                        record=self.db.execute('select payload from maintenance_plans where id=?',(payload['plan_id'],)).fetchone()
                        source_hash=json.loads(record['payload']).get('source_before') if record else None
                        if not source_hash or fingerprint(manifest(target.resolve()))!=source_hash:raise ValueError('Shared source changed; preserve held original and backup for manual recovery')
                    if exists(hold): remove_path(hold)
                    final = 'succeeded'
                elif exists(hold) and not exists(target):
                    os.replace(hold, target); final = 'rolled_back'
                elif exists(hold):
                    raise ValueError('Target has changed; preserve both target and held original for manual review')
                else:
                    final = 'interrupted'
                if exists(temp): remove_path(temp)
            elif action in ('delete','backup'):
                candidates = [r for r in self.db.execute('select id,payload from maintenance_backups') if json.loads(r['payload']).get('operation_id') == oid]
                final = 'succeeded' if (action=='backup' or not exists(target)) and any(self.verify_backup(r['id'])['valid'] for r in candidates) else 'interrupted'
                payload['backups'] = [{'id': r['id']} for r in candidates]
            elif action in ('activate','deactivate'):
                rec = self.db.execute('select payload from maintenance_plans where id=?', (payload['plan_id'],)).fetchone()
                plan = json.loads(rec['payload']); destination = Path(plan['destination'])
                if not exists(target) and exists(destination):
                    self.db.execute('update capabilities set path=?,status=? where id=?', (str(destination), 'active' if action=='activate' else 'inactive',plan['item']['id']))
                    final = 'succeeded'
                else: final = 'interrupted'
            else:
                final = 'succeeded' if not exists(target) else 'interrupted'
                if final == 'succeeded':
                    rec = self.db.execute('select payload from maintenance_plans where id=?', (payload['plan_id'],)).fetchone()
                    bid=json.loads(rec['payload'])['backup_id']
                    self.db.execute("update maintenance_backups set state='purged' where id=?", (bid,))
                    if bid.startswith('historical-'):
                        self.db.execute("update maintenance_recovery_candidates set state='purged' where id=?",(bid,))
            if final == 'succeeded' and payload.get('plan_id'):
                record = self.db.execute('select payload from maintenance_plans where id=?',(payload['plan_id'],)).fetchone()
                plan = json.loads(record['payload']) if record else {}
                item = plan.get('item',{})
                if item.get('id'):
                    if action == 'install':
                        cap=self.a.capability_from_skill_md(item['platform'],target,target/'SKILL.md');cap.update(id=item['id'],status='active')
                        self.r.upsert_capability(cap)
                        self.state(cap)
                        self.db.execute('insert into maintenance_item_dates(id,first_seen_at,installed_at) values (?,?,?) on conflict(id) do update set installed_at=excluded.installed_at', (item['id'],stamp(),stamp()))
                        self.db.execute('update maintenance_items set source=? where id=?',(json.dumps(plan['source']),item['id']))
                    if action in ('share','update','sync','restore','install'):
                        self.db.execute('update maintenance_items set baseline=? where id=?',(plan.get('baseline'),item['id']))
                    self.db.execute('update capabilities set health=? where id=?',('missing' if action=='delete' else 'ok',item['id']))
            payload.update(state=final, recovered_at=stamp())
            self.operation(oid, action, final, payload)
            if payload.get('plan_id'):
                self.db.execute('update maintenance_plans set state=? where id=?', ('applied' if final=='succeeded' else 'failed',payload['plan_id']))
            self.db.commit()
            self.invalidate_cache()
            return payload

    def plans(self):
        plans=[]
        for row in self.db.execute("select * from maintenance_plans where state in ('preview','failed','running','selection') order by created_at desc"):
            p=json.loads(row['payload'])
            expired=dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(p['created_at']) > dt.timedelta(hours=24)
            plans.append({'id':row['id'],'created_at':row['created_at'],'state':row['state'],'action':p['action'],
                          'target':p.get('target'),'name':p.get('item',{}).get('name'), 'expired':expired,
                          'staged_space':size(self.home/'plans'/row['id'])})
        return {'plans':plans}

    def get_plan(self, pid):
        row = self.db.execute('select state,payload from maintenance_plans where id=?', (pid,)).fetchone()
        if not row: raise ValueError('Plan not found')
        plan=json.loads(row['payload'])
        expired=dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(plan['created_at']) > dt.timedelta(hours=24)
        return {**plan, 'state':row['state'], 'expired':expired}

    def discard_plan(self, pid):
        rec = self.db.execute('select state from maintenance_plans where id=?', (pid,)).fetchone()
        if not rec or rec['state']=='running': raise ValueError('Plan cannot be discarded')
        directory = self.home / 'plans' / pid
        if directory.exists(): shutil.rmtree(directory)
        self.db.execute("update maintenance_plans set state='discarded' where id=?", (pid,)); self.db.commit()
        self.invalidate_cache()
        return {'id':pid,'state':'discarded'}


def safe_relative(path):
    p = Path(path)
    if p.is_absolute() or '..' in p.parts or '\\' in path or ':' in path:
        raise ValueError('Unsafe skill subdirectory')

def urllib_credentials(url):
    from urllib.parse import urlparse
    u = urlparse(url)
    return u.username or u.password or u.query or u.fragment

def redact(text):
    from diagnostics import redact as safe_redact
    return safe_redact(text)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def register(sub, legacy):
    def run(args):
        m = None
        try:
            m = Manager(legacy)
            c = args.command
            if c in ('discovery-cached','discovery-refresh','discovery-verify','classification-preview','classification-apply','classification-undo','discover-sources-batch','source-search-policy','source-search-policy-update'):
                result=dispatch(m,{'action':c,**vars(args)})
            elif c in ('discover-backups','retain-backup','ignore-backup-candidate','import-backup','preview-purge-many','apply-purge-many'):
                result=dispatch(m,{'action':c,**vars(args),'retained':not getattr(args,'off',False),'confirmed':getattr(args,'yes',False)})
            elif c in ('cache-inspect','cache-clean'):
                result=dispatch(m,{'action':c,'fingerprint':getattr(args,'fingerprint',None),'confirmed':getattr(args,'yes',False)})
            elif c == 'inventory': result = m.inventory()
            elif c == 'import-usage': result = m.import_usage(args.days,args.replay,[{'agent':args.log_agent,'path':args.log_path}] if args.log_path else None)
            elif c == 'set-labels': result = m.set_labels(args.targets,args.category,args.tags,args.mode)
            elif c == 'discover-sources': result = m.discover_sources(args.name,args.remote)
            elif c == 'probe-source': result = m.probe_source(args.name,args.url,args.ref)
            elif c == 'scan': result = m.scan(args.project, args.import_logs)
            elif c == 'check-updates':
                rows = [m.select(args.name,args.platform or '')] if args.name else [dict(r) for r in m.r.all_capabilities("c.kind='skill'" + (" and c.platform=?" if args.platform else ''), (args.platform,) if args.platform else ())]
                from check_batches import run as check_batch
                result = check_batch(m, [r['id'] for r in rows])
            elif c == 'source-bind': result = m.bind_source(m.select(args.name,args.platform or ''), args.url, args.ref, args.path)
            elif c == 'backup': result = m.backup(m.select(args.name,args.platform or ''))
            elif c == 'backups': result = m.backups()
            elif c == 'plans': result = m.plans()
            elif c == 'verify-backup': result = m.verify_backup(args.id)
            elif c == 'history': result = m.history(args.limit)
            elif c == 'usage': result = m.usage(args.days,args.name,args.platform or '')
            elif c == 'protect': result = m.protect(m.select(args.name,args.platform or ''), not args.off)
            elif c == 'apply':
                if not args.yes: raise ValueError('Apply requires --yes after reviewing the plan')
                result = m.apply(args.id)
            elif c == 'recover-operation':
                if not args.yes: raise ValueError('Recovery requires --yes after checking history')
                result = m.recover(args.id)
            elif c == 'discard-plan': result = m.discard_plan(args.id)
            elif c == 'service': result = service_command(legacy,args)
            elif c == 'roots':
                if args.action == 'list': result = {'roots':[{'platform':p,'path':str(r),'status':s} for p,r,s in m.roots()]}
                else:
                    if not args.platform or not args.path: raise ValueError('--platform and --path required')
                    result = legacy.update_source(args.action,args.platform,args.path)
            elif c == 'sync': result = m.plan('sync',m.select(args.target,args.platform or ''), m.select(args.source,args.source_platform or ''),allow_changes=args.allow_local_changes)
            elif c in ('restore','purge'): result = m.plan(c,backup_id=args.id,allow_changes=getattr(args,'allow_local_changes',False),restore_path=getattr(args,'path',None))
            else: result = m.plan(c, m.select(args.name,args.platform or ''),allow_changes=getattr(args,'allow_local_changes',False))
            emit(result)
        except (ValueError,OSError) as exc:
            emit({'error':redact(str(exc))}); raise SystemExit(1)
        finally:
            if m is not None: m.db.close()
    # Replace legacy handlers only for operations now supported by this common core.
    for c in ('scan','check-updates','backup','delete','update','activate','deactivate','usage'):
        p = sub.choices[c]
        p.set_defaults(func=run)
        if c == 'scan':
            p.add_argument('--project'); p.add_argument('--import-logs',action='store_true')
        elif c == 'check-updates': p.add_argument('name',nargs='?')
        elif c == 'update': p.add_argument('--allow-local-changes',action='store_true')
        elif c == 'usage':
            p.add_argument('name',nargs='?');p.add_argument('--platform')
    for c in ('discovery-cached','discovery-refresh','discovery-verify'):
        p=sub.add_parser(c,help='Read official Skill catalogs without installing or binding sources');p.set_defaults(func=run)
        if c=='discovery-verify':p.add_argument('id')
        else:
            p.add_argument('--view',choices=['trending','new','search'],default='trending');p.add_argument('--query',default='')
    p=sub.add_parser('cache-inspect',help='Inspect registered staging older than 24 hours');p.set_defaults(func=run)
    p=sub.add_parser('cache-clean',help='Remove only inspected expired manager staging');p.set_defaults(func=run)
    p.add_argument('--fingerprint',required=True);p.add_argument('--yes',action='store_true')
    p=sub.add_parser('import-usage',help='Import Agent usage log evidence');p.set_defaults(func=run)
    p.add_argument('--days',type=int,default=90);p.add_argument('--replay',action='store_true');p.add_argument('--log-path');p.add_argument('--log-agent',choices=['codex','claude_code','hermes','openclaw'],default='codex')
    p=sub.add_parser('set-labels',help='Edit one category and up to three tags');p.set_defaults(func=run)
    p.add_argument('targets',nargs='+');p.add_argument('--category');p.add_argument('--tags',nargs='*');p.add_argument('--mode',choices=['replace','add','remove'],default='replace')
    p=sub.add_parser('discover-sources',help='Recover local sources; remote results remain candidates');p.set_defaults(func=run)
    p.add_argument('name',nargs='?');p.add_argument('--remote',action='store_true')
    p=sub.add_parser('probe-source',help='Match GitHub Skill subdirectories before binding');p.set_defaults(func=run)
    p.add_argument('name');p.add_argument('--url',required=True);p.add_argument('--ref',default='')
    for c in ('classification-preview','classification-apply','classification-undo','discover-sources-batch','source-search-policy','source-search-policy-update'):
        p=sub.add_parser(c,help='Metadata management: '+c);p.set_defaults(func=run)
        if c=='classification-undo':p.add_argument('id')
        if c=='discover-sources-batch':p.add_argument('--limit',type=int,default=10)
        if c=='source-search-policy-update':p.add_argument('--mode',choices=['auto','domestic-first'],required=True)
    for c in ('discover-backups','retain-backup','ignore-backup-candidate','import-backup','preview-purge-many','apply-purge-many'):
        p=sub.add_parser(c,help='Local recovery: '+c);p.set_defaults(func=run)
        if c=='discover-backups':p.add_argument('--path')
        if c in ('retain-backup','ignore-backup-candidate','import-backup'):p.add_argument('id')
        if c=='retain-backup':p.add_argument('--off',action='store_true')
        if c=='import-backup':
            p.add_argument('--original-path',required=True);p.add_argument('--type',choices=['backup','quarantine'],required=True);p.add_argument('--yes',action='store_true')
        if c in ('preview-purge-many','apply-purge-many'):p.add_argument('ids',nargs='+')
        if c=='apply-purge-many':p.add_argument('--yes',action='store_true')
    for c in ('inventory','backups','history','source-bind','verify-backup','restore','purge','sync','apply','protect','recover-operation','discard-plan','service','roots','plans'):
        p = sub.add_parser(c, help='Local maintenance: '+c); p.set_defaults(func=run)
        if c in ('source-bind','protect'):
            p.add_argument('name'); p.add_argument('--platform')
        if c == 'source-bind':
            p.add_argument('--url',required=True); p.add_argument('--ref',default=''); p.add_argument('--path',default='')
        if c == 'protect': p.add_argument('--off',action='store_true')
        if c in ('verify-backup','restore','purge','apply','recover-operation','discard-plan'): p.add_argument('id')
        if c in ('apply','recover-operation'): p.add_argument('--yes',action='store_true')
        if c == 'restore':
            p.add_argument('--allow-local-changes',action='store_true');p.add_argument('--path',help='Alternative recovery path below a configured Skills root')
        if c == 'sync':
            p.add_argument('source'); p.add_argument('target'); p.add_argument('--platform'); p.add_argument('--source-platform'); p.add_argument('--allow-local-changes',action='store_true')
        if c == 'history': p.add_argument('--limit',type=int,default=50)
        if c == 'roots':
            p.add_argument('action',choices=['list','add','remove']);p.add_argument('--platform');p.add_argument('--path')
        if c == 'service':
            p.add_argument('action',choices=['start','stop','status']); p.add_argument('--port',type=int); p.add_argument('--open',action='store_true')


def api_dispatch(legacy, payload):
    if payload.get('action') in BROWSE_ACTIONS and legacy.DB_PATH.exists():
        # Browsing must not initialize schemas, commit, or queue behind a worker.
        m = Manager.__new__(Manager)
        m.a, m.home = legacy, legacy.APP_DIR
        m.r = legacy.Registry.__new__(legacy.Registry)
        m.db = sqlite3.connect(legacy.DB_PATH.as_uri()+'?mode=ro',uri=True,timeout=2)
        m.db.row_factory = sqlite3.Row
        m.r.conn = m.db
        if not m.db.execute("select 1 from sqlite_master where name='maintenance_inventory_cache'").fetchone():
            m.db.close()
            m = Manager(legacy)
    else:
        m = Manager(legacy)
    try:
        return dispatch(m, payload)
    finally:
        m.db.close()


def dispatch(m, payload):
    action = payload.get('action')
    if action in ('project-analyze','project-analysis-stop'):
        import project_analysis
        return project_analysis.stop() if action=='project-analysis-stop' else project_analysis.analyze(m, payload)
    if action in ('space-catalog','space-add-project','disk-scan','disk-report','disk-stop'):
        import project_space
        return project_space.dispatch(m, payload)
    if action in ('space-advice','space-trash-batch','space-restore-many','trash-purge-many'):
        import project_cleanup
        return project_cleanup.dispatch(m, payload)
    if action in ('space-scan','space-results','space-trash','space-trash-list','space-restore','space-purge'):
        import space_cleanup
        return space_cleanup.dispatch(m, payload)
    if action == 'translate-descriptions':
        import description_translation
        return description_translation.descriptions(m, payload)
    if action in ('discovery-cached','discovery-refresh','discovery-verify'):
        import skill_discovery
        return skill_discovery.dispatch(m,payload)
    if action in ('cache-inspect','cache-clean'):
        import cache_cleanup
        return cache_cleanup.inspect(m) if action=='cache-inspect' else cache_cleanup.clean(m,payload.get('fingerprint'),payload.get('confirmed'))
    import web_execution
    if action=='uninstall-references': return web_execution.uninstall_references(m,payload.get('ids'))
    if action in web_execution.ACTIONS: return web_execution.dispatch(m, payload)
    if action in ('classification-preview','classification-apply','classification-undo'):
        import classification
        return classification.undo(m,payload['id']) if action=='classification-undo' else classification.batch(m,apply=action=='classification-apply',field=payload.get('field','both'))
    if action in ('source-search-policy','source-search-policy-update','discover-sources-batch'):
        import source_search
        if action=='discover-sources-batch': return source_search.batch(m,payload.get('limit',10))
        return source_search.set_policy(m,payload.get('mode')) if action=='source-search-policy-update' else source_search.policy(m)
    if action in ('taxonomy', 'taxonomy-update'):
        from taxonomy import overview, update
        result=overview(m) if action=='taxonomy' else update(m,payload)
        if action=='taxonomy':
            import classification
            result['classification']=classification.summary(m)
        return result
    import snapshots, library_actions, library_batches
    if action in library_batches.ACTIONS:return library_batches.dispatch(m,payload)
    if action in snapshots.ACTIONS:return snapshots.dispatch(m,payload)
    if action in library_actions.ACTIONS:return library_actions.dispatch(m,payload)
    if action in ('discover-backups','retain-backup','ignore-backup-candidate','import-backup','preview-purge-many','apply-purge-many'):
        import recovery
        if action=='discover-backups':return recovery.discover(m,payload.get('path'))
        if action=='retain-backup':return recovery.retain(m,payload['id'],payload.get('retained'))
        if action=='ignore-backup-candidate':return recovery.ignore(m,payload['id'])
        if action=='import-backup':return recovery.import_candidate(m,payload['id'],payload.get('original_path'),payload.get('type'),payload.get('confirmed'))
        if action=='preview-purge-many':
            from backup_recycle import web_operation
            ids = payload.get('ids')
            operation = payload.get('operation','purge')
            if isinstance(ids,list) and ids:
                operations = {web_operation(m, operation, bid) for bid in ids}
                if len(operations)!=1:raise ValueError('请分别处理备份和回收站内容')
                operation = operations.pop()
            return recovery.preview_purge(m,ids,operation)
        if action=='apply-purge-many':
            from backup_recycle import web_operation
            ids = payload.get('ids')
            if isinstance(ids,list):
                for pid in ids:
                    plan = m.get_plan(pid)
                    if plan['action']=='purge' and web_operation(m,'purge',plan['backup_id'])!='purge':
                        raise ValueError('请取消旧预览，重新从备份列表删除；删除后进入回收站')
            return recovery.apply_purge(m,ids,payload.get('confirmed'))
    if action == 'restore-targets':
        from snapshots import restore_targets
        return restore_targets(m,payload)
    if action == 'open-directory':
        path = absolute(payload.get('path', ''))
        if not path.is_dir() or not any(path == absolute(root) for _,root,_ in m.roots()):
            raise ValueError('请选择已登记且存在的目录')
        if sys.platform == 'win32': os.startfile(str(path.resolve()))
        else: subprocess.run(['open' if sys.platform == 'darwin' else 'xdg-open', str(path.resolve())],check=True,timeout=10,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return {'opened':True}
    if action == 'open-folder':
        row = m.select(payload.get('target'))
        path = absolute(row['path'])
        if row['kind'] != 'skill' or not path.is_dir() or not any(root == path or root in path.parents for _,root,_ in m.roots()):
            raise ValueError('Skill folder is unavailable; scan again')
        path = path.resolve()
        if sys.platform == 'win32':
            os.startfile(str(path))
        else:
            subprocess.run(['open' if sys.platform == 'darwin' else 'xdg-open', str(path)], check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {'opened':True}
    if action == 'set-labels': return m.set_labels(payload.get('targets'),payload.get('category'),payload.get('tags'),payload.get('mode','replace'))
    if action == 'import-usage': return m.import_usage(int(payload.get('days',90)),bool(payload.get('replay')),payload.get('roots'))
    if action == 'probe-source': return m.probe_source(payload.get('target'),payload.get('url'),payload.get('ref',''))
    if action == 'discover-sources': return m.discover_sources(payload.get('target'),bool(payload.get('remote')))
    if action == 'scan': return m.scan(payload.get('project'),bool(payload.get('import_logs',True)))
    if action == 'duplicate-cached':
        try:
            result=json.loads((m.home/'duplicate-review.json').read_text())
            generation=m.db.execute('select generation from maintenance_inventory_cache where id=1').fetchone()[0]
            return result if result.get('inventory_generation')==generation else {'groups':[],'checked_at':None}
        except (OSError,ValueError): return {'groups':[], 'checked_at':None}
    if action == 'duplicate-review':
        from duplicates import review
        return review(m)
    if action == 'update-summary':
        try:
            run = json.loads((m.home / 'update-run.json').read_text())
        except (OSError, ValueError):
            run = None
        if run and run.get('state') == 'running':
            # Live progress comes from Jobs; a saved running record is conservatively unfinished.
            run = {**run, 'state':'interrupted'}
        return {'run':run}
    if action in ('inventory', 'inventory-cached'):
        value = m.cached_inventory() if action == 'inventory-cached' else m.inventory(payload.get('ids'), lightweight=bool(payload.get('lightweight')))
        return inventory_summary(value) if payload.get('summary') else value
    if action == 'inventory-detail':
        target = payload.get('id')
        if not isinstance(target, str): raise ValueError('Select a Skill ID')
        item = next((i for i in m.cached_inventory().get('items', []) if i['id'] == target), None)
        if item is None: raise ValueError('Skill not found; refresh the list')
        return {'item':item}
    if action == 'install-roots': return m.install_roots()
    if action == 'install-identify': return m.identify_install(payload['url'],payload['target_root'])
    if action == 'history':
        from diagnostics import recent
        return {**m.history(), 'errors': recent(m.home)}
    if action == 'set-description':
        row = m.select(payload.get('target', ''))
        value = payload.get('description')
        if not isinstance(value, str) or len(value.strip()) > 80:
            raise ValueError('Description must be at most 80 characters')
        path = m.home / 'descriptions.json'
        try:
            values = json.loads(path.read_text())
        except (OSError, ValueError):
            values = {}
        key = str(Path(row['path']).resolve())
        if value.strip(): values[key] = value.strip()
        else: values.pop(key, None)
        write_json(path, values)
        m.invalidate_cache()
        return {'saved':True}
    if action == 'usage': return m.usage(int(payload.get('days',30)),payload.get('target'))
    if action == 'backup-detail':
        from recovery import backup_detail
        return backup_detail(m,payload['id'])
    if action == 'backup-advice':
        from recovery import backup_advice
        return backup_advice(m,payload.get('ids'))
    if action == 'backups': return m.backups(verify=payload.get('verify') is True, ids=payload.get('ids'))
    if action == 'plans': return m.plans()
    if action == 'get-plan': return m.get_plan(payload['id'])
    if action == 'recover':
        if payload.get('confirmed') is not True: raise ValueError('Confirmation required')
        return m.recover(payload['id'])
    if action == 'verify-backup': return m.verify_backup(payload['id'])
    if action == 'discard-plan': return m.discard_plan(payload['id'])
    if action == 'apply':
        if payload.get('confirmed') is not True: raise ValueError('Confirmation required')
        from backup_recycle import web_operation
        plan = m.get_plan(payload['id'])
        if plan['action']=='purge' and web_operation(m, 'purge', plan['backup_id'])!='purge':
            raise ValueError('请取消旧预览，重新从备份列表删除；删除后进入回收站')
        return m.apply(payload['id'])
    if action == 'update-now':
        if payload.get('confirmed') is not True: raise ValueError('Confirmation required')
        return m.update_now(m.select(payload['target']))
    if action == 'preview':
        op = payload.get('operation')
        if op == 'install': return m.plan_install(payload['id'],payload['remote_path'])
        if op not in ('share','sync','update','delete','activate','deactivate','restore','purge','trash-backup','restore-backup'):
            raise ValueError('Unsupported operation')
        if op in ('restore','purge','trash-backup','restore-backup'):
            from backup_recycle import web_operation
            op = web_operation(m, op, payload['id'])
            return m.plan(op,backup_id=payload['id'],allow_changes=bool(payload.get('allow_local_changes')),restore_path=payload.get('restore_path'))
        row = m.select(payload['target'])
        return m.plan(op,row,source_row=m.select(payload['source']) if op in ('sync','share') else None,allow_changes=bool(payload.get('allow_local_changes')))
    row = m.select(payload.get('target',''))
    if action == 'inspect-space': return m.inspect_space(row)
    if action == 'check':
        m._targeted_checks = True
        return m.check(row)
    if action == 'backup': return m.backup(row)
    if action in ('protect','unprotect'): return m.protect(row,action=='protect')
    if action == 'source-bind': return m.bind_source(row,payload['url'],payload.get('ref',''),payload.get('path',''))
    raise ValueError('Unsupported management action')


def service_request(record, shutdown=False):
    import urllib.request
    url = record['url'] + ('api/service-stop' if shutdown else 'api/service')
    headers = {'X-ASM-Token':record['token']} if shutdown else {}
    req = urllib.request.Request(url,headers=headers,data=b'{}' if shutdown else None)
    with urllib.request.urlopen(req,timeout=2) as response:
        result = json.load(response)
    if result.get('instance') != record['instance']:
        raise ValueError('Service instance mismatch; refusing to control it')
    return result


def service_command(legacy, args):
    statefile = legacy.APP_DIR / 'service.json'
    try:
        record = json.loads(statefile.read_text())
        healthy = service_request(record)
    except (OSError,ValueError,KeyError):
        record = healthy = None
    if args.action == 'status':
        return {'running':bool(healthy),'url':record['url'] if healthy else None,'pid':healthy.get('pid') if healthy else None}
    if args.action == 'stop':
        if not healthy: return {'running':False,'message':'No verified manager service is running'}
        service_request(record,True)
        for _ in range(30):
            try: service_request(record)
            except (OSError,ValueError): return {'running':False,'stopped':True}
            time.sleep(.1)
        raise ValueError('Service shutdown is pending; check status again')
    if healthy:
        if args.open:
            import webbrowser
            webbrowser.open(record['url']+'manage')
        return {'running':True,'url':record['url']+'manage','already_running':True}
    script = Path(legacy.__file__).resolve()
    port = args.port if args.port is not None else int(legacy.load_config().get('web',{}).get('port',8765))
    log = legacy.LOG_DIR / 'service.log'
    log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('ab') as output:
        child = subprocess.Popen([sys.executable,'-B',str(script),'web','--host','127.0.0.1','--port',str(port)],stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True)
    for _ in range(50):
        if child.poll() is not None: raise ValueError('Service failed to start; inspect '+str(log))
        try:
            record = json.loads(statefile.read_text())
            healthy = service_request(record)
            if healthy.get('pid') != child.pid: raise ValueError('Another process owns this service record')
            if args.open:
                import webbrowser
                webbrowser.open(record['url']+'manage')
            return {'running':True,'url':record['url']+'manage','pid':child.pid,'log':str(log)}
        except (OSError,ValueError,KeyError): time.sleep(.1)
    child.terminate()
    raise ValueError('Service did not become reachable within five seconds')
