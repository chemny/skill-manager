"""Reversible backup recycling; keep files and recovery metadata intact until purge."""
import json
import shutil
import datetime as dt
import maintenance as core


def entry(m, bid):
    if not m.db.execute("select 1 from sqlite_master where name='maintenance_backup_trash'").fetchone():
        return None
    row = m.db.execute('select trashed_at from maintenance_backup_trash where id=?', (bid,)).fetchone()
    return row['trashed_at'] if row else None


def validate(m, bid, action, meta):
    recycled = entry(m, bid)
    if action == 'trash-backup' and (recycled or meta.get('type') == 'quarantine'):
        raise ValueError('此内容已在回收站，请刷新列表')
    if action == 'restore-backup' and not recycled:
        raise ValueError('此内容不是回收站中的备份，请刷新列表')


def web_operation(m, operation, bid):
    if operation == 'purge':
        _, meta = m.backup_record(bid)
        if meta.get('type') != 'quarantine' and not entry(m, bid):
            return 'trash-backup'
    return operation


def apply(m, pid):
    with m.lock():
        plan = m.get_plan(pid)
        if plan.get('state') != 'preview' or plan.get('expired'):
            raise ValueError('Plan expired or already applied; preview again')
        bid, action = plan['backup_id'], plan['action']
        path, meta = m.backup_record(bid)
        validate(m, bid, action, meta)
        m.ensure_backup_unused(bid, pid, check_retained=action!='restore-backup')
        if entry(m, bid) != plan.get('recycle_marker'):
            raise ValueError('Backup recycle state changed; preview again')
        if core.fingerprint(core.manifest(path)) != plan['before']:
            raise ValueError('Backup changed; preview again')
        oid = core.uid('op')
        result = {'id':oid, 'plan_id':pid, 'action':action, 'state':'succeeded',
                  'backup_id':bid, 'target':str(path), 'backups':[],
                  'destination_view':'trash' if action=='trash-backup' else 'backups',
                  'net_freed_bytes_estimate':0}
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds')
        # The recycle marker, plan and history commit together; no file is destroyed.
        with m.db:
            if action == 'trash-backup':
                m.db.execute('insert into maintenance_backup_trash values (?,?)', (bid, now))
            else:
                m.db.execute('delete from maintenance_backup_trash where id=?', (bid,))
            m.db.execute("update maintenance_plans set state='applied' where id=?", (pid,))
            m.db.execute('insert into maintenance_operations values (?,?,?,?,?,?)',
                         (oid, now, now, action, 'succeeded', json.dumps(result)))
        shutil.rmtree(m.home / 'plans' / pid, ignore_errors=True)
        m.invalidate_cache()
        return result
