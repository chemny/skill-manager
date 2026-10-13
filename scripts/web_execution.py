"""Click-to-execute web actions; staging is internal, never a user approval queue."""
from pathlib import Path
import maintenance as core

ACTIONS = {'execute','execute-library-many','execute-backups','execute-restore-many','empty-trash'}


def uninstall_references(m, ids):
    if not isinstance(ids,list) or not ids or len(ids)>1000 or any(not isinstance(x,str) for x in ids):
        raise ValueError('Invalid uninstall scope')
    protected = {r['id'] for r in m.db.execute('select id from maintenance_items where protected=1')}
    from component_ownership import Ownership
    ownership = Ownership()
    by_real, by_inode, policies = {}, {}, {}
    # Build once for a bulk selection; write-time affected() still revalidates.
    for ref in m.db.execute('select id,platform,path from capabilities'):
        ref = dict(ref)
        path = Path(ref['path'])
        by_real.setdefault(str(path.resolve()),[]).append(ref)
        try:
            value = path.lstat()
            by_inode.setdefault((value.st_dev,value.st_ino),[]).append(ref)
        except OSError:
            pass
    result = {}
    for target in dict.fromkeys(ids):
        row = m.select(target)
        path = Path(row['path'])
        if path.is_symlink():
            value = path.lstat()
            refs = by_inode.get((value.st_dev,value.st_ino),[])
        else:
            refs = by_real.get(str(path.resolve()),[])
        result[row['id']] = []
        for ref in refs:
            if ref['path'] not in policies:
                policies[ref['path']] = ownership.classify(ref['path'])['read_only']
            result[row['id']].append({**ref,'protected':ref['id'] in protected,
                                      'read_only':policies[ref['path']]})
    return {'references':result}


def cleanup(m, plans):
    for plan in plans:
        saved = m.db.execute('select state from maintenance_plans where id=?', (plan['id'],)).fetchone()
        if saved and saved['state'] in ('preview','selection'):
            m.discard_plan(plan['id'])


def check_uninstall_scope(m, p, targets):
    expected = p.get('expected_references')
    if expected is None: return
    if not isinstance(expected, dict): raise ValueError('Invalid uninstall scope')
    for target in targets:
        row = m.select(target)
        references = expected.get(row['id'])
        actual = m.affected(Path(row['path']))
        if not isinstance(references, list) or any(not isinstance(r,dict) or set(r)!=set(('id','platform','path')) or any(not isinstance(v,str) for v in r.values()) for r in references):
            raise ValueError('Invalid uninstall scope')
        if sorted(references, key=lambda r:r['id']) != sorted(actual, key=lambda r:r['id']):
            raise ValueError('Agent associations changed; refresh the list and select uninstall locations again')


def dispatch(m, p):
    if p.get('confirmed') is not True: raise ValueError('Explicit action required')
    action = p['action']
    if action == 'execute':
        operation = p.get('operation')
        if operation not in ('delete','update','restore','purge','trash-backup','restore-backup',
                             'install','sync','share','activate','deactivate'):
            raise ValueError('Unsupported operation')
        if operation == 'update': return m.update_now(m.select(p['target']))
        if operation == 'delete': check_uninstall_scope(m, p, [p['target']])
        plan = None
        try:
            plan = core.dispatch(m, {**p, 'action':'preview', 'allow_local_changes':bool(p.get('allow_local_changes'))})
            if operation == 'delete': check_uninstall_scope(m, p, [p['target']])
            return core.dispatch(m, {'action':'apply','id':plan['id'],'confirmed':True})
        finally:
            if plan: cleanup(m, [plan])
            if operation == 'install' and p.get('id'):
                cleanup(m, [{'id':p['id']}])
    if action == 'execute-library-many':
        import library_batches
        targets = library_batches.ids(p.get('ids'))
        operation = p.get('operation')
        if operation not in ('delete','update'): raise ValueError('Unsupported batch operation')
        if operation == 'delete': check_uninstall_scope(m, p, targets)
        if operation == 'update':
            results, skipped, seen = [], [], set()
            rows = [m.select(target) for target in targets]
            from check_batches import repository_archives
            with repository_archives(m):
                for row in rows:
                    path = str(Path(row['path']).resolve())
                    if path in seen: continue
                    seen.add(path)
                    try: results.append(m.update_now(row))
                    except (ValueError,OSError) as exc:
                        skipped.append({'id':row['id'],'name':row['name'],'path':row['path'],'platform':row['platform'],'reason':core.redact(str(exc))})
            return {'state':'partial' if skipped else 'succeeded','action':operation,'selected':len(targets),
                    'completed':len(results),'total':len(seen),'results':results,'skipped':skipped}
        v = library_batches.preview(m, operation, targets)
        try:
            check_uninstall_scope(m, p, targets)
            result = library_batches.apply(m, [plan['id'] for plan in v['plans']], operation, True) if v['plans'] else {
                'state':'partial','action':operation,'completed':0,'total':0,'results':[]}
            return {**result,'selected':v['selected'],'skipped':v['skipped']}
        finally: cleanup(m, v['plans'])
    if action == 'execute-restore-many':
        import snapshots
        v = snapshots.preview_restore(m, p)
        try: return snapshots.apply_restore(m, [plan['id'] for plan in v['plans']], True)
        finally: cleanup(m, v['plans'])
    if action in ('execute-backups','empty-trash'):
        import recovery, snapshots
        v = snapshots.dispatch(m, {'action':'preview-empty-trash'}) if action=='empty-trash' else core.dispatch(
            m, {**p,'action':'preview-purge-many'})
        try:
            result = recovery.apply_purge(m, [plan['id'] for plan in v['plans']], True) if v['plans'] else {
                'state':'succeeded','completed':0,'total':0,'results':[]}
            return {**result,'skipped_count':v.get('skipped_retained',0)}
        finally: cleanup(m, v['plans'])
