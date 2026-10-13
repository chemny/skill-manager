"""Conservative resource classification, resolved without initializing a writer DB."""
import json
from contextlib import closing
import os
import sqlite3
from pathlib import Path

NETWORK = {'discovery-refresh','discovery-verify','translate-descriptions','check','check-many','discover-sources','discover-sources-batch','probe-source',
           'install-identify','preview','preview-library-many'}
READS = NETWORK | {'uninstall-references','cache-inspect','inventory','duplicate-review','inspect-space','directory-preview',
                   'classification-preview','verify-backup','backup-detail','backups','usage',
                   'history','plans','get-plan','install-roots','open-folder','taxonomy','source-search-policy'}
COMMITS = {'cache-clean','execute','execute-library-many','execute-backups','execute-restore-many','empty-trash',
           'update-now','apply','apply-library-many','apply-restore-many','apply-purge-many','backup',
           'recover','import-backup','create-snapshot','import-snapshot','export-backup'}


def overlaps(a, b):
    a, b = Path(a), Path(b)
    return a == b or a in b.parents or b in a.parents


def describe(legacy, request):
    action=request['action']; paths=set()
    write=action not in READS
    result={'write':write,'resources':[], 'global':False, 'catalog':action=='scan', 'operation':request.get('operation'),
            'lane':'network' if action in NETWORK else 'inventory' if action=='inventory' else 'local'}
    if action == 'update-now': result['operation'] = 'update'
    def add(raw):
        if not raw: return
        path=Path(raw).expanduser().absolute()
        paths.update((os.path.normcase(str(path)),os.path.normcase(str(path.resolve()))))
    if not legacy.DB_PATH.exists():
        result['global']=write
        return result
    with closing(sqlite3.connect(legacy.DB_PATH.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        db.row_factory=sqlite3.Row
        def target(selector):
            if not isinstance(selector,str) or not selector: return
            rows=db.execute('select path from capabilities where id=? or path=? or name=?',
                            (selector,selector,selector)).fetchall()
            for row in rows:add(row['path'])
        def plan(pid):
            row=db.execute('select payload from maintenance_plans where id=?',(pid,)).fetchone()
            if not row:return
            p=json.loads(row[0])
            result['operation']=p.get('action')
            for field in ('target','destination','source_target','container'):add(p.get(field))
            add(legacy.APP_DIR/'plans'/pid)
        def backup(bid):
            row=db.execute('select path from maintenance_backups where id=?',(bid,)).fetchone()
            if row:add(row['path'])
            else:
                row=db.execute('select payload from maintenance_recovery_candidates where id=?',(bid,)).fetchone()
                if row:add(json.loads(row['payload']).get('path'))
        target(request.get('target'));target(request.get('source'))
        if action == 'execute' and request.get('operation')=='install':
            plan(request.get('id'))
            result['operation']='install'
        if action == 'execute' and request.get('operation') in ('restore','purge','trash-backup','restore-backup'):
            backup(request.get('id'))
        for selector in request.get('targets',[]):target(selector)
        ids=request.get('ids',[])
        if action in ('check-many','inventory') and ids:
            exact = {r['id']:r['path'] for r in db.execute('select id,path from capabilities where id in (select value from json_each(?))', (json.dumps(ids),))}
            for path in set(exact.values()): add(path)
            ids = [selector for selector in ids if selector not in exact]
        for selector in ids:
            if action=='execute-backups':backup(selector)
            elif action.startswith('apply-') or action=='discard-previews':plan(selector)
            else:target(selector)
        if action in ('apply','discard-plan'):plan(request.get('id'))
        for field in ('restore_path','target_root','path'):add(request.get(field))
    result['resources']=sorted(paths)
    # Shared JSON/config files and recovery operations stay mutually exclusive.
    result['global']=write and (not paths or action in {'set-description','scan','recover'})
    return result


def conflicts(a, b):
    if a['write'] and b['write'] and (a['global'] or b['global']): return True
    if a['global'] and not a['catalog'] and b['resources']: return True
    if b['global'] and not b['catalog'] and a['resources']: return True
    return (a['write'] or b['write']) and any(overlaps(x,y) for x in a['resources'] for y in b['resources'])
