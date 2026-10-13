"""Persistent comparison cache; filesystem fingerprints are always revalidated."""
import datetime as dt
import json
import threading
import weakref
from pathlib import Path

LOCKS = weakref.WeakValueDictionary()
LOCKS_GUARD = threading.Lock()
TTL_SECONDS = 15 * 60


def path_lock(path):
    # Different installations must not serialize network I/O through a hash collision.
    with LOCKS_GUARD:
        lock = LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            LOCKS[path] = lock
        return lock


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
    return max(deadlines).isoformat() if deadlines else (now + dt.timedelta(seconds=60)).isoformat()


def save_references(manager, row, value, source):
    """Persist one fingerprint-validated outcome, never an alias's binding or baseline."""
    from check_results import source_key
    path=str(Path(row['path']).resolve())
    groups=getattr(manager,'_check_references',None)
    if groups is None:
        groups={}
        for candidate in manager.db.execute("select * from capabilities where kind='skill'"):
            candidate=dict(candidate)
            try:groups.setdefault(str(Path(candidate['path']).resolve()),[]).append(candidate)
            except (OSError,RuntimeError):continue
        manager._check_references=groups
    changes=[]
    for candidate in groups.get(path,[row]):
        try:
            if str(Path(candidate['path']).resolve())!=path:continue
            state=manager.db.execute('select source,last_check from maintenance_items where id=?',(candidate['id'],)).fetchone()
            if not state:continue
            binding=json.loads(state['source'] or 'null')
            if binding and source_key(binding)!=source_key(source):continue
            if source_key(manager.source(candidate))!=source_key(source):continue
            updated={**value,'id':candidate['id']}
            ignored={'cache_hit','remote_requested','retry_after_seconds'}
            stored=json.loads(state['last_check'] or '{}')
            if {k:v for k,v in stored.items() if k not in ignored}!={k:v for k,v in updated.items() if k not in ignored}:
                changes.append((json.dumps(updated),candidate['id']))
        except (OSError,RuntimeError):continue
    if changes:
        manager.db.executemany('update maintenance_items set last_check=? where id=?',changes)
        manager.db.commit()
        manager.invalidate_cache()


def compare(manager, row, force=False, cache_only=False):
    import maintenance as core
    path = str(Path(row['path']).resolve())
    lock = path_lock(path)
    with lock:
        source = manager.source(row)
        if not source:
            if cache_only: return None
            result = manager._check_remote(row)
            result.update(cache_hit=False, remote_requested=False)
            return result
        local = core.fingerprint(core.manifest(Path(path), ignore_git=True))
        state = manager.state(row)
        # Single-item checks must honor the same REST cooldown as full batches.
        if source.get('provider')=='github' and getattr(manager,'_targeted_checks',False):
            recorded=json.loads(state['last_check'] or '{}')
            deadline=recorded.get('api_retry_at') or (recorded.get('retry_at') if recorded.get('code')=='remote_rate_limit' and recorded.get('rate_limit_scope') not in ('github-archive','github-git') else None)
            try:
                if dt.datetime.fromisoformat(deadline)>dt.datetime.now(dt.timezone.utc):
                    api_state=getattr(manager,'_github_api_state',None)
                    if api_state is None:manager._github_api_state=api_state={}
                    api_state.update(limited=True,retry_at=max(deadline,api_state.get('retry_at') or ''))
            except (ValueError,TypeError):pass
        from version_policy import POLICY_VERSION, normalize_check, version
        identity = {'policy_version':POLICY_VERSION, 'path':path, 'source':{k:source.get(k) for k in ('provider','url','ref','path','channel')},
                    'local':local, 'baseline':state['baseline'], 'version':row.get('version')}
        key = core.fingerprint(identity)
        cached = manager.db.execute('select payload from maintenance_check_cache where cache_key=?', (key,)).fetchone()
        if not cached and POLICY_VERSION == 3:
            previous = core.fingerprint({**identity, 'policy_version':2})
            cached = manager.db.execute('select payload from maintenance_check_cache where cache_key=?', (previous,)).fetchone()
        if not cached and POLICY_VERSION == 4:
            # Old staging-path failures must not hide releases for another cache interval.
            for policy in (3, 2):
                previous = core.fingerprint({**identity, 'policy_version':policy})
                candidate = manager.db.execute('select payload from maintenance_check_cache where cache_key=?', (previous,)).fetchone()
                value = json.loads(candidate['payload']) if candidate else {}
                if version(value.get('local_version')) is not None and version(value.get('remote_version')) is not None:
                    cached = candidate
                    break
        now = dt.datetime.now(dt.timezone.utc)
        saved = json.loads(state['last_check'] or '{}')
        previous_value = normalize_check(json.loads(cached['payload'])) if cached else (
            normalize_check(saved) if saved.get('comparison_key') == key else None)
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
            success = value['status'] in ('latest','update_available','local_modified','local_ahead','baseline_unknown') and not value.get('attempt_failed')
            git_fallback = (value.get('code') == 'remote_rate_limit' and source.get('provider') == 'github'
                            and value.get('rate_limit_scope') != 'github-git'
                            and ((value.get('rate_limit_scope') != 'github-archive'
                                  and getattr(manager, '_github_git_fallback', False))
                                 or getattr(manager, '_targeted_checks', False)))
            if (cooldown > 0 and not git_fallback) or (not force and success and 0 <= age < TTL_SECONDS):
                value.update(id=row['id'], cache_hit=True, remote_requested=False,
                             retry_after_seconds=max(0, int(cooldown)))
                save_references(manager,row,value,source)
                return value
        if cache_only: return None
        deferred = getattr(manager, '_defer_check_save', False)
        manager._defer_check_save = True
        try:
            value = manager._check_remote(row)
        finally:
            manager._defer_check_save = deferred
        value.update(cache_hit=False, remote_requested=True, comparison_source=source,
                     local_fingerprint=value.get('local_fingerprint', local), comparison_key=key)
        api_retry_at = getattr(manager, '_github_api_state', {}).get('retry_at')
        if source.get('provider') == 'github' and api_retry_at:
            value['api_retry_at'] = api_retry_at
        # Never publish an outcome under a fingerprint that changed during I/O.
        if value.get('local_fingerprint',local)==local and core.fingerprint(core.manifest(Path(path), ignore_git=True))==local:
            if value['status'] == 'check_failed' and previous_value and previous_value['status'] in ('latest','update_available','local_modified','local_ahead','baseline_unknown'):
                # Keep confirmed comparison evidence, but retry the failed attempt next time.
                value = {**previous_value, 'id':row['id'], 'cache_hit':False,
                         'remote_requested':True, 'attempt_failed':True,
                         'comparison_source':source, 'local_fingerprint':local, 'comparison_key':key,
                         'attempted_at':value['checked_at'],
                         **{k:value.get(k) for k in ('error','error_id','message','code','retry_at','rate_limit_scope',
                                                   'source_issue','retryable')}}
                if api_retry_at: value['api_retry_at'] = api_retry_at
            manager.db.execute('update maintenance_items set last_check=? where id=?', (json.dumps(value),row['id']))
            manager.db.execute('delete from maintenance_check_cache where real_path=?', (path,))
            manager.db.execute('insert into maintenance_check_cache values (?,?,?)', (key,path,json.dumps(value)))
            manager.db.commit()
            manager.invalidate_cache()
            save_references(manager,row,value,source)
        else:
            value.update(status='stale',message='Local content changed during the comparison; check again')
            manager.db.execute('update maintenance_items set last_check=? where id=?',(json.dumps(value),row['id']))
            manager.db.execute('delete from maintenance_check_cache where real_path=?',(path,))
            manager.db.commit()
            manager.invalidate_cache()
            save_references(manager,row,value,source)
        return value
