"""Project-scoped cleanup choices, backed by immutable scans and rechecked moves."""
import hashlib
import json
from pathlib import Path

import maintenance as core
import space_cleanup as space
import project_duplicates as duplicates


def scope(m, payload):
    row = m.db.execute('select payload from maintenance_disk_scans where id=?', (payload.get('report_id'),)).fetchone()
    if not row:
        raise ValueError('项目清单已变化，请重新打开清理建议')
    report = json.loads(row[0])
    paths = payload.get('paths')
    available = {p['path']: p for p in report['projects']}
    if (not isinstance(paths, list) or not paths or len(paths) > 2000
            or any(not isinstance(path, str) or path not in available for path in paths)):
        raise ValueError('请选择已经分析的项目')
    return report, list(dict.fromkeys(paths))


def advice(m, payload):
    report, paths = scope(m, payload)
    groups, missing = [], []
    projects = {p['path']: p for p in report['projects']}
    comparison = duplicates.latest(m, report, paths)
    proofs = {r['id']: r for r in comparison['candidates']} if comparison else {}
    partial = []
    reserved = space.protected(m, Path(paths[0]))
    for path in paths:
        row = m.db.execute('select payload from maintenance_project_analyses where path=?', (path,)).fetchone()
        analysis = json.loads(row[0]) if row else {}
        scan = space.results(m, analysis.get('scan_id')) if analysis.get('scan_id') else None
        if not duplicates.compatible(scan, report, path):
            missing.append(path)
            continue
        if analysis.get('state') != 'completed':
            partial.append(path)
        buckets = {}
        for item in scan['items']:
            proof = proofs.get(item['id'])
            if space.retained_role(item) or item.get('recycled') or space.blocked(Path(item['path']), reserved):
                continue
            if item['selectable'] and item.get('category') == 'cache':
                kind = 'cache'
            elif (proof or not comparison and item['selectable'] and item.get('category') == 'duplicates'
                  and item.get('duplicate_keeper') and item.get('sha256')):
                ext = Path(item['path']).suffix.lower()
                kind = 'images' if ext in {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg'} else 'media'
            else:
                continue
            buckets.setdefault(kind, []).append(item)
        for kind, items in buckets.items():
            key = hashlib.sha256(((comparison['id'] if comparison else '') + scan['id'] + '\n' + kind).encode()).hexdigest()[:24]
            title, impact = {
                'cache': ('程序缓存', '下次使用时会重新生成，首次运行可能稍慢。'),
                'images': ('重复图片', '保留相同副本；部分旧报告或对话中的图片可能无法显示。'),
                'media': ('重复素材', '保留相同副本；部分历史预览或旧版本可能无法打开。'),
            }[kind]
            groups.append({'id': key, 'scan_id': scan['id'], 'project': path,
                           'project_name': projects[path]['name'], 'kind': kind,
                           'title': title, 'impact': impact, 'files': len(items),
                           'comparison_id': comparison['id'] if comparison else None,
                           'allocated': sum(i['allocated'] for i in items),
                           'bytes': sum(i['size'] for i in items), 'ids': [i['id'] for i in items]})
    groups.sort(key=lambda group: (-group['allocated'], group['project'], group['kind']))
    return {'groups': groups, 'missing': missing, 'partial': partial, 'projects': len(paths),
            'cross_state': comparison['state'] if comparison else 'pending' if len(paths) > 1 else 'not_required',
            'read_only': True}


def move(m, payload):
    if payload.get('confirmed') is not True:
        raise ValueError('请先确认移入回收站')
    chosen = payload.get('groups')
    if not isinstance(chosen, list) or not chosen or any(not isinstance(key, str) for key in chosen):
        raise ValueError('请选择清理项')
    offers = {row['id']: row for row in advice(m, payload)['groups']}
    if any(key not in offers for key in chosen):
        raise ValueError('清理建议已变化，请刷新后重新选择')
    by_scan = {}
    for key in set(chosen):
        group = offers[key]
        by_scan.setdefault(group['scan_id'], []).extend(group['ids'])
    total = sum(len(ids) for ids in by_scan.values())
    if total > space.MAX_FILES:
        raise ValueError('单次最多处理 20,000 个文件，请分批选择清理项')
    completed, skipped = 0, []
    report, paths = scope(m, payload)
    comparison = duplicates.latest(m, report, paths)
    proofs = {r['id']: r for r in comparison['candidates']} if comparison else {}
    for scan_id, ids in by_scan.items():
        # The existing move rechecks directory identity, file fingerprints and keepers.
        result = space.move_selected(m, {'scan_id': scan_id, 'ids': ids, 'confirmed': True},
                                     verified={key: proofs[key] for key in ids if key in proofs})
        completed += result['completed']
        skipped.extend(result['skipped'])
    return {'completed': completed, 'total': total, 'skipped': skipped,
            'state': 'partial' if skipped else 'complete'}


def restore_many(m, payload):
    if payload.get('confirmed') is not True:
        raise ValueError('请先确认恢复')
    ids = payload.get('ids')
    if (not isinstance(ids, list) or not ids or len(ids) > space.MAX_FILES
            or any(not isinstance(key, str) for key in ids)):
        raise ValueError('请选择回收内容')
    available = {r['id'] for r in space.trash_list(m)['items'] if r['state'] == 'available'}
    if any(key not in available for key in ids):
        raise ValueError('回收内容已变化，请刷新列表')
    completed, skipped = 0, []
    for index, tid in enumerate(dict.fromkeys(ids)):
        try:
            completed += space.restore(m, {'id': tid, 'confirmed': True})['completed']
        except (OSError, ValueError) as exc:
            skipped.append({'id': tid, 'reason': str(exc)})
        space.progress(m, index + 1, len(ids), '正在恢复文件')
    return {'completed': completed, 'skipped': skipped, 'state': 'partial' if skipped else 'complete'}


def purge_many(m, payload):
    if payload.get('confirmed') is not True:
        raise ValueError('请先确认永久删除')
    artifact_ids, backup_ids = payload.get('artifact_ids', []), payload.get('backup_ids', [])
    if (not isinstance(artifact_ids, list) or not isinstance(backup_ids, list)
            or not artifact_ids and not backup_ids or len(artifact_ids) + len(backup_ids) > space.MAX_FILES
            or any(not isinstance(key, str) for key in artifact_ids + backup_ids)):
        raise ValueError('请选择回收内容')
    completed, skipped = 0, []
    if artifact_ids:
        result = space.purge(m, {'ids': artifact_ids, 'confirmed': True})
        completed += result['completed']
        skipped.extend(result['skipped'])
    if backup_ids:
        result = core.dispatch(m, {'action': 'execute-backups', 'ids': backup_ids,
                                  'operation': 'purge', 'confirmed': True})
        completed += result['completed']
        skipped.extend(result.get('skipped', []))
        if result.get('error'):
            skipped.append({'reason': result['error']})
    return {'completed': completed, 'skipped': skipped, 'state': 'partial' if skipped else 'complete'}


def dispatch(m, payload):
    return {'space-advice': advice, 'space-trash-batch': move, 'space-restore-many': restore_many,
            'trash-purge-many': purge_many}[payload['action']](m, payload)
