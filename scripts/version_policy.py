"""New-version visibility is independent of permission to replace local changes."""
import re
POLICY_VERSION = 4


def version(value):
    match = re.fullmatch(r'v?(\d+(?:\.\d+){0,3})(?:\+[A-Za-z0-9.-]+)?', str(value or ''))
    if not match:
        return None
    parts = tuple(int(n) for n in match[1].split('.'))
    return parts + (0,) * (4-len(parts))


def normalize_check(check, saved_local_version=None):
    """Reinterpret saved comparisons without downloading or trusting stale results."""
    if not check or check.get('status') not in ('update_available','local_modified','baseline_unknown','local_ahead'):
        return check
    value = dict(check)
    if 'local_version' not in value and value.get('local_fingerprint') and saved_local_version:
        value['local_version'] = saved_local_version
    local, remote = version(value.get('local_version')), version(value.get('remote_version'))
    if local is not None and remote is not None and remote > local:
        review = value.get('baseline_known') is not True or any((value.get('local_changes') or {}).values())
        value.update(status='update_available', remote_update_available=True,
                     update_decision='review' if review else 'update',
                     decision_reason=('远端版本更高；本地修改情况不明，更新前需确认' if value.get('baseline_known') is not True
                                      else '远端版本更高；本地有修改，更新前需确认' if review else '远端版本更高'))
    elif local is not None and remote is not None and local > remote:
        value.update(status='local_ahead', update_decision='keep_local', remote_update_available=False,
                     decision_reason='本地版本较高，保留本地内容')
    else:
        value.update(status='local_modified' if value.get('baseline_known') and any((value.get('local_changes') or {}).values()) else 'baseline_unknown',
                     update_decision='keep_local' if value.get('update_decision')=='keep_local' else 'review',
                     decision_reason='版本相同但内容不同，请核对差异' if local is not None and local == remote else '版本无法可靠比较，请核对内容')
    value['policy_version'] = POLICY_VERSION
    return value


def assess(current, remote, baseline, local_version, remote_version, version_gt):
    from maintenance import diff
    different = any(diff(current, remote).values())
    local = diff(baseline, current) if baseline is not None else None
    upstream = diff(baseline, remote) if baseline is not None else None
    status = 'baseline_unknown' if different else 'latest'
    decision = 'keep_local' if local and any(local.values()) and not any(upstream.values()) else 'review'
    if not different:
        decision = 'none'
    return normalize_check({'status':status, 'update_decision':decision, 'decision_reason':'本地与远端内容一致' if not different else '',
            'local_version':local_version, 'remote_version':remote_version,
            'policy_version':POLICY_VERSION, 'baseline_known':baseline is not None,
            'local_changes':local, 'remote_update_available':any(upstream.values()) if upstream is not None else (False if not different else None)})
