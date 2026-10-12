"""Persistent comparison cache; filesystem fingerprints are always revalidated."""
import datetime as dt
import hashlib
import json
import threading
from pathlib import Path

LOCKS = [threading.Lock() for _ in range(64)]
TTL_SECONDS = 15 * 60


def rate_limit_deadline(error):
    """Honor explicit server cooldowns without caching ordinary failures."""
    from email.utils import parsedate_to_datetime
    headers = getattr(error, 'headers', None) or {}
    now = dt.datetime.now(dt.timezone.utc)
    deadlines = []
    retry = headers.get('Retry-After')
    if retry:
        try:
            deadlines.append(now + dt.timedelta(seconds=max(0, float(retry))))
        except ValueError:
            try:
                deadlines.append(parsedate_to_datetime(retry).astimezone(dt.timezone.utc))
            except (ValueError, TypeError, OverflowError):
                pass
    try:
        deadlines.append(dt.datetime.fromtimestamp(float(headers['X-RateLimit-Reset']), dt.timezone.utc))
    except (KeyError, ValueError, TypeError, OverflowError):
        pass
    return max(deadlines).isoformat() if deadlines else None


def compare(manager, row, force=False):
    import maintenance as core
    path = str(Path(row['path']).resolve())
    lock = LOCKS[int(hashlib.sha256(path.encode()).hexdigest(), 16) % len(LOCKS)]
    with lock:
        source = manager.source(row)
        if not source:
            result = manager._check_remote(row)
            result.update(cache_hit=False, remote_requested=False)
            return result
        local = core.fingerprint(core.manifest(Path(path)))
        state = manager.state(row)
        from version_policy import POLICY_VERSION, normalize_check
        identity = {'policy_version':POLICY_VERSION, 'path':path, 'source':{k:source.get(k) for k in ('provider','url','ref','path','channel')},
                    'local':local, 'baseline':state['baseline'], 'version':row.get('version')}
        key = core.fingerprint(identity)
        cached = manager.db.execute('select payload from maintenance_check_cache where cache_key=?', (key,)).fetchone()
        if not cached and POLICY_VERSION == 3:
            previous = core.fingerprint({**identity, 'policy_version':2})
            cached = manager.db.execute('select payload from maintenance_check_cache where cache_key=?', (previous,)).fetchone()
        now = dt.datetime.now(dt.timezone.utc)
        previous_value = normalize_check(json.loads(cached['payload'])) if cached else None
        if previous_value:
            value = dict(previous_value)
            try:
                age = (now - dt.datetime.fromisoformat(value['checked_at'])).total_seconds()
            except (KeyError, ValueError, TypeError):
                age = -1
            try:
                cooldown = max(0, (dt.datetime.fromisoformat(value['retry_at']) - now).total_seconds())
            except (KeyError, ValueError, TypeError):
                cooldown = 0
            success = value['status'] != 'check_failed' and not value.get('attempt_failed')
            if cooldown > 0 or (not force and success and 0 <= age < TTL_SECONDS):
                value.update(id=row['id'], cache_hit=True, remote_requested=False,
                             retry_after_seconds=max(0, int(cooldown)))
                manager.db.execute('update maintenance_items set last_check=? where id=?', (json.dumps(value),row['id']))
                manager.db.commit()
                manager.invalidate_cache()
                return value
        value = manager._check_remote(row)
        value.update(cache_hit=False, remote_requested=True)
        # Never publish an outcome under a fingerprint that changed during I/O.
        if value.get('local_fingerprint',local)==local and core.fingerprint(core.manifest(Path(path)))==local:
            if value['status'] == 'check_failed' and previous_value and previous_value['status'] != 'check_failed':
                # Keep confirmed comparison evidence, but retry the failed attempt next time.
                value = {**previous_value, 'id':row['id'], 'cache_hit':False,
                         'remote_requested':True, 'attempt_failed':True,
                         'attempted_at':value['checked_at'],
                         **{k:value.get(k) for k in ('error','error_id','message','code','retry_at')}}
                manager.db.execute('update maintenance_items set last_check=? where id=?', (json.dumps(value),row['id']))
            manager.db.execute('delete from maintenance_check_cache where real_path=?', (path,))
            manager.db.execute('insert into maintenance_check_cache values (?,?,?)', (key,path,json.dumps(value)))
            manager.db.commit()
        return value
