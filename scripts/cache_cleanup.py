"""Explicit cleanup of expired, registered manager staging only."""
import datetime as dt
import json
from pathlib import Path
import shutil
import stat
import time

import maintenance as core

MAX_AGE = 24 * 3600
SAFE_STATES = {'preview', 'selection', 'applied', 'discarded'}


def tree_state(path, cutoff):
    entries, allocated, logical = [], 0, 0
    pending = [path]
    while pending:
        current = pending.pop()
        value = current.lstat()
        if not (stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode)):
            raise ValueError('Staging contains a link or special file')
        if value.st_mtime > cutoff:
            raise ValueError('Staging was used recently')
        entries.append([str(current.relative_to(path)), value.st_dev, value.st_ino,
                        value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns])
        allocated += getattr(value, 'st_blocks', 0) * 512
        if stat.S_ISDIR(value.st_mode):
            pending.extend(current.iterdir())
        else:
            logical += value.st_size
        if len(entries) > 10000:
            raise ValueError('Staging inspection limit reached')
    return {'signature':core.fingerprint(sorted(entries)),
            'size':{'bytes':logical, 'allocated_bytes':allocated}}


def inspect(m, now=None):
    from recovery import ensure_storage_safe
    cutoff = (time.time() if now is None else now) - MAX_AGE
    home = Path(m.home).absolute()
    base = home / 'plans'
    result = {'items':[], 'count':0, 'skipped':0,
              'size':{'bytes':0, 'allocated_bytes':0}, 'minimum_age_hours':24}
    if base.is_symlink() or any(p.is_symlink() for p in base.parents):
        raise ValueError('Manager staging has a symlink ancestor')
    try:
        ensure_storage_safe(m, base)
    except ValueError:
        return {**result, 'blocked':True, 'fingerprint':core.fingerprint([])}
    active = set()
    for row in m.db.execute("select state,payload from maintenance_operations where state in ('running','failed','partial')"):
        try:
            payload = json.loads(row['payload'])
            if not isinstance(payload, dict) or not isinstance(payload.get('plan_id'),str):
                raise ValueError('Invalid operation')
            active.add(payload.get('plan_id'))
        except (ValueError, TypeError):
            # Unknown interrupted operations may still need any staging directory.
            if row['state']=='running':
                return {**result, 'blocked':True, 'fingerprint':core.fingerprint([])}
    for row in m.db.execute('select id,state,created_at from maintenance_plans order by id'):
        if row['state'] not in SAFE_STATES or row['id'] in active:
            result['skipped'] += 1
            continue
        pid = row['id']
        if not isinstance(pid,str) or Path(pid).name != pid or pid in ('.','..'):
            result['skipped'] += 1
            continue
        path = base / pid
        try:
            created = dt.datetime.fromisoformat(row['created_at'])
            if created.tzinfo is None or created.timestamp() > cutoff: continue
            if not path.is_dir() or path.is_symlink(): continue
            snapshot = tree_state(path, cutoff)
        except (OSError, ValueError, TypeError):
            result['skipped'] += 1
            continue
        result['items'].append({'id':pid, 'state':row['state'],
                                'created_at':row['created_at'], **snapshot})
        for key in result['size']: result['size'][key] += snapshot['size'][key]
    result['count'] = len(result['items'])
    result['fingerprint'] = core.fingerprint(result['items'])
    return result


def clean(m, fingerprint, confirmed=False):
    if confirmed is not True: raise ValueError('Confirmation required')
    if not isinstance(fingerprint,str) or not fingerprint: raise ValueError('Inspect staging first')
    with m.lock():
        current = inspect(m)
        if current.get('blocked') or current['fingerprint'] != fingerprint:
            raise ValueError('暂存清单已变化，请刷新存储空间后重试')
        completed, failures = [], []
        removed = {'bytes':0, 'allocated_bytes':0}
        for item in current['items']:
            path = m.home / 'plans' / item['id']
            try:
                # Confirm the same inode tree immediately before disposal.
                if path.is_symlink() or any(p.is_symlink() for p in path.parents) or tree_state(path, time.time()-MAX_AGE)['signature'] != item['signature']:
                    raise ValueError('Staging changed')
                shutil.rmtree(path)
                m.db.execute("update maintenance_plans set state='discarded' where id=?", (item['id'],))
                m.db.commit()
                completed.append(item['id'])
                for key in removed: removed[key] += item['size'][key]
            except (OSError, ValueError) as exc:
                failures.append({'id':item['id'], 'error':core.redact(exc)})
        result = {'state':'partial' if failures else 'succeeded', 'completed':len(completed),
                  'ids':completed, 'removed_size':removed, 'failures':failures,
                  'staged_space':core.size(m.home / 'plans')}
        row = m.db.execute('select payload from maintenance_inventory_cache where id=1').fetchone()
        if row and row['payload']:
            try:
                cached = json.loads(row['payload'])
            except ValueError:
                cached = None
            if isinstance(cached,dict) and isinstance(cached.get('space'),dict):
                cached['space']['staged_plans'] = result['staged_space']
                m.db.execute('update maintenance_inventory_cache set payload=? where id=1',
                             (json.dumps(cached),))
        m.operation(core.uid('op'), 'cache-clean', result['state'], result)
        return result
