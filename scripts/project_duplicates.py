"""Immutable comparisons across the projects explicitly selected for analysis."""
from collections import defaultdict
import json
import os
from pathlib import Path
import time

import maintenance as core
import space_cleanup as space

HASH_BUDGET = 8 * 1024**3


def exclusions(report, root):
    return {p for p in report.get('scope_paths', [p['path'] for p in report['projects']])
            if Path(root) in Path(p).parents}


def compatible(scan, report, root):
    return (scan and scan['root'] == root and scan.get('policy_version') == space.POLICY_VERSION
            and set(scan.get('exclude_roots', [])) == exclusions(report, root))


def generated_root():
    return str(Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser().absolute() / 'generated_images')


def build(m, report, paths, deadline, stopped=lambda: False):
    scans, missing = {}, []
    for path in paths:
        row = m.db.execute('select payload from maintenance_project_analyses where path=?', (path,)).fetchone()
        analysis = json.loads(row[0]) if row else {}
        scan = space.results(m, analysis.get('scan_id')) if analysis.get('scan_id') else None
        if compatible(scan, report, path):
            scans[path] = scan
        else:
            missing.append(path)
    sizes, coverage = defaultdict(list), {}
    reserved = space.protected(m, Path(paths[0]))
    for root, scan in scans.items():
        coverage[root] = {'hash_candidates': 0, 'hashed_files': 0, 'hash_skipped': 0}
        for item in scan['items']:
            if (item.get('duplicate_eligible') and item['size'] and not item.get('recycled')
                    and not space.blocked(Path(item['path']), reserved)):
                sizes[item['size']].append({**item, 'root': root, 'scan_id': scan['id']})
    candidates = [r for rows in sizes.values() if len(rows) > 1 for r in rows]
    hashed, hashes, last = 0, defaultdict(list), 0
    for index, row in enumerate(candidates):
        cov = coverage[row['root']]
        cov['hash_candidates'] += 1
        try:
            if stopped() or time.monotonic() >= deadline:
                raise TimeoutError('跨项目核对尚未完成')
            with space.directory(Path(row['root'])) as fd:
                s = os.fstat(fd)
                if [s.st_dev, s.st_ino] != scans[row['root']]['root_identity']:
                    raise ValueError('目录已变化')
            with space.directory(Path(row['path']).parent) as fd:
                if space.fingerprint(os.stat(Path(row['path']).name, dir_fd=fd, follow_symlinks=False)) != row['fingerprint']:
                    raise ValueError('文件已变化')
            if not row.get('sha256'):
                if row['size'] > space.HASH_LIMIT or hashed + row['size'] > HASH_BUDGET:
                    raise ValueError('达到内容读取上限')
                row['sha256'] = space.read_file(Path(row['path']), row['fingerprint'], digest=True, deadline=deadline)
                hashed += row['size']
            cov['hashed_files'] += 1
            hashes[row['sha256']].append(row)
        except (OSError, ValueError, TimeoutError):
            cov['hash_skipped'] += 1
        if time.monotonic() - last > .25:
            getattr(m, 'project_current', lambda path: None)(row['root'])
            space.progress(m, len(scans), len(paths), '正在核对跨项目重复文件')
            last = time.monotonic()
    offered, cross_groups = {}, 0
    for rows in hashes.values():
        if len(rows) < 2:
            continue
        by_project = defaultdict(list)
        for row in rows:
            by_project[row['root']].append(row)
        keepers = []
        def offer(row, keeper):
            offered[row['path']] = {'id': row['id'], 'scan_id': row['scan_id'], 'root': row['root'],
                'sha256': row['sha256'], 'keeper': keeper['path'], 'keeper_root': keeper['root'],
                'keeper_identity': scans[keeper['root']]['root_identity'],
                'keeper_fingerprint': keeper['fingerprint']}
        for root, members in by_project.items():
            keeper, extras = space.duplicate_choices(members)
            keepers.append(keeper)
            for row in extras:
                offer(row, keeper)
        if len(by_project) > 1:
            cross_groups += 1
            project_keepers = [r for r in keepers if r['root'] != generated_root()]
            if project_keepers:
                keeper = min(project_keepers, key=space.keeper_key)
                # Independent projects keep their own copy. Only client-generated
                # images may use a verified project copy as their retained master.
                for row in by_project.get(generated_root(), []):
                    if not space.retained_role(row):
                        offer(row, keeper)
    assert not {r['keeper'] for r in offered.values()} & set(offered)
    value = {'id': core.uid('comparison'), 'report_id': report['id'], 'created_at': core.stamp(),
        'policy_version': space.POLICY_VERSION, 'paths': paths,
        'scans': {root: scan['id'] for root, scan in scans.items()}, 'missing': missing,
        'candidates': list(offered.values()), 'coverage': coverage,
        'cross_groups': cross_groups, 'additional_hash_bytes': hashed,
        'state': 'partial' if missing or any(r['hash_skipped'] for r in coverage.values()) else 'completed'}
    m.db.execute('insert into maintenance_project_comparisons values(?,?,?,?)',
                 (value['id'], report['id'], value['created_at'], json.dumps(value)))
    m.db.execute('delete from maintenance_project_comparisons where id not in '
                 '(select id from maintenance_project_comparisons order by created_at desc,rowid desc limit 10)')
    m.db.commit()
    return value


def latest(m, report, paths):
    rows = m.db.execute('select payload from maintenance_project_comparisons where report_id=? '
                        'order by created_at desc,rowid desc', (report['id'],)).fetchall()
    for row in rows:
        value = json.loads(row[0])
        if value.get('policy_version') != space.POLICY_VERSION or set(value['paths']) != set(paths):
            continue
        for root, sid in value['scans'].items():
            current = m.db.execute('select payload from maintenance_project_analyses where path=?', (root,)).fetchone()
            if not current or json.loads(current[0]).get('scan_id') != sid:
                break
        else:
            return value
    return None
