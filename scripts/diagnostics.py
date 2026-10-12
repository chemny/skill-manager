"""Local, redacted error records. Never store request bodies or credentials."""
import datetime
import json
import os
from pathlib import Path
import re
import threading
import uuid
import urllib.error

_lock = threading.Lock()


def redact(value):
    text = str(value)
    text = re.sub(r'(https?://)[^/\s@]+@', r'\1[redacted]@', text)
    text = re.sub(r'(https?://[^\s?#]+)[?#][^\s]*', r'\1?[redacted]', text)
    text = re.sub(r'(?i)(bearer\s+)\S+', r'\1[redacted]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|token|password|secret|authorization)["\x27]?\s*[:=]\s*["\x27]?)[^\s,;"\x27}]+', r'\1[redacted]', text)
    text = re.sub(r'\b(?:sk-[\w-]{12,}|gh[pousr]_[\w]{12,}|github_pat_[\w_]{12,})\b', '[redacted]', text)
    return text[:1200]


def classify(exc):
    s = str(exc).lower()
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code==429 or (exc.code==403 and exc.headers and (exc.headers.get('X-RateLimit-Remaining')=='0' or exc.headers.get('Retry-After'))):
            return 'remote_rate_limit', '远端请求限流，请稍后重试；GitHub 可配置访问令牌提高额度。'
        if exc.code in (401,403): return 'remote_permission', '远端拒绝访问，请核对仓库权限或访问令牌。'
        if exc.code==404: return 'remote_not_found', '仓库、分支或提交不存在，或当前账号没有访问权限。'
    if 'certificate_verify_failed' in s: return 'remote_certificate', '远端证书校验失败，请检查本机证书或代理配置。'
    if 'task not found' in s:
        return 'task_expired', '任务记录已过期或服务已重启。写操作请先核对操作历史和待执行预览。'
    if 'running' in s and ('task' in s or 'maintenance' in s):
        return 'busy', '另一个任务正在执行，请等待完成。'
    if 'restore conflict' in s:
        return 'restore_conflict', '恢复位置已有内容，请核对后选择允许覆盖；覆盖前会自动备份。'
    if 'protected' in s:
        return 'protected', '该 Skill 已保护，请先核对是否需要取消保护。'
    if 'source' in s and ('unknown' in s or 'not bound' in s or 'not confirmed' in s):
        return 'source_required', '请先确认并绑定来源，再检查更新。'
    if 'backup' in s and any(x in s for x in ('invalid', 'corrupt', 'verification', 'damaged')):
        return 'backup_invalid', '备份校验未通过，不能恢复；请检查备份文件。'
    if isinstance(exc, PermissionError):
        return 'permission_denied', '没有目录访问权限，请检查该目录的权限后重试。'
    if any(x in s for x in ('timed out', 'timeout', 'urlopen', 'network', 'could not resolve', 'failed to connect')):
        return 'remote_unavailable', '远端暂时无法访问，请检查网络或仓库地址后重试。'
    if isinstance(exc, (ValueError, KeyError)):
        return 'invalid_request', '操作条件不满足，请核对目标、来源或预览。'
    return 'operation_failed', '操作未完成，请查看错误详情和操作历史。'


def record(home, exc, action='unknown', job_id=None):
    code, message = classify(exc)
    event = {'error_id': 'err-' + uuid.uuid4().hex[:12],
             'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
             'code': code, 'message': message, 'error': redact(exc),
             'action': re.sub(r'[^a-zA-Z0-9_-]', '', str(action))[:50],
             'exception': type(exc).__name__}
    if job_id:
        event['job_id'] = re.sub(r'[^a-zA-Z0-9_-]', '', str(job_id))[:80]
    try:
        directory = Path(home) / 'logs'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / 'errors.jsonl'
        with _lock:
            # Bounded local records; one previous segment is retained.
            if path.exists() and path.stat().st_size > 2_000_000:
                path.replace(directory / 'errors.previous.jsonl')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + '\n')
    except OSError:
        event['log_unavailable'] = True
    return event


def recent(home, limit=30):
    path = Path(home) / 'logs' / 'errors.jsonl'
    if not path.exists():
        return []
    records = []
    try:
        for line in path.read_text(encoding='utf-8').splitlines()[-limit:]:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return records[::-1]
