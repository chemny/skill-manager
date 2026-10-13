"""Reconcile historical Agent references without touching files or persisted evidence."""
import copy
import datetime as dt


def source_key(source):
    if not source: return None
    return tuple(source.get(k) or '' for k in ('provider','url','ref','path','channel'))


def observed_at(check):
    try:
        value = dt.datetime.fromisoformat(check.get('attempted_at') or check.get('checked_at') or '')
        return value.timestamp() if value.tzinfo else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def reconcile(items):
    groups = {}
    for item in items:
        if not item.get('present') or item.get('source_readiness') == 'source_conflict': continue
        key = source_key(item.get('source'))
        if key: groups.setdefault((item.get('real_path') or item.get('path'), key), []).append(item)
    for (_, source), refs in groups.items():
        checks = []
        for item in refs:
            check = item.get('update_check')
            if not check: continue
            recorded = check.get('comparison_source') or check.get('source')
            if recorded and source_key(recorded) != source: continue
            checks.append(check)
        fingerprints = {c['local_fingerprint'] for c in checks if c.get('local_fingerprint')}
        # Historical snapshots cannot establish which fingerprint is current.
        if len(fingerprints) > 1 or any(c.get('status') == 'stale' for c in checks): continue
        if len({i.get('version') for i in refs}) > 1 or not checks: continue
        latest = max(checks, key=observed_at)
        if not observed_at(latest): continue
        tied = [c for c in checks if observed_at(c)==observed_at(latest)]
        if len({(c.get('status'),bool(c.get('attempt_failed'))) for c in tied})>1: continue
        if latest.get('status') != 'check_failed' and not latest.get('local_fingerprint'): continue
        for item in refs:
            item['update_check'] = {**copy.deepcopy(latest), 'id':item['id']}
    return items
