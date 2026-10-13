"""Local, redacted error records. Never store request bodies or credentials."""
import datetime
import json
import os
from pathlib import Path
import re
import threading
import uuid
import urllib.error
import http.client

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
    if 'remote git blob integrity mismatch' in s:
        return 'remote_integrity', '远端文件与固定提交的校验值不一致，已阻止使用；未修改本地 Skill。'
    if 'confirmed source path does not contain skill.md' in s:
        issue=getattr(exc,'source_issue',{})
        if issue.get('kind')=='repository_has_no_skills':
            return 'remote_source_path', '登记仓库的当前版本没有 SKILL.md，可能误用了依赖软件的仓库地址；需要查找此 Skill 的发布来源。'
        if issue.get('kind')=='repository_root_not_skill':
            return 'remote_source_path', '来源记录指向仓库根目录，但根目录不是 Skill；需要核对确切子目录，未自动改绑。'
        return 'remote_source_path', '远端已不包含登记路径下的 SKILL.md，可能已迁移、合并或移除；需要核对来源，未自动改绑。'
    if 'remote skill contains symlinks or submodules' in s:
        return 'remote_unsafe_entries', '来源 Skill 包含链接或子模块，已停止自动处理；需要核对这些文件。'
    if 'github selected-directory request timed out' in s:
        return 'remote_unavailable', 'GitHub 目录或文件请求超时，本次已停止；可稍后重试，未修改本地 Skill。'
    if '安装包文件与更新源校验清单不一致' in s:
        return 'remote_integrity', '更新源的安装包与校验清单不一致，已阻止使用；请核对来源或等待源站修复。'
    if 'remote response exceeds size limit' in s or 'archive exceeds expanded size or file count limit' in s:
        return 'remote_archive_limit', '来源仓库安装包过大，已停止下载或解压；未修改本地 Skill。'
    if isinstance(exc, ValueError) and ('原配置已保留' in s or '未覆盖原配置' in s):
        return 'config_invalid', redact(exc)
    if isinstance(exc, ValueError) and '当前选择的是单个 skill' in s:
        return 'directory_is_skill', redact(exc)
    if isinstance(exc, ValueError) and '暂存清单已变化' in s:
        return 'cache_changed', '暂存清单已变化，请刷新存储空间后重试。'
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code==429 or (exc.code==403 and (('rate limit' in s or 'rate-limit' in s) or exc.headers and (exc.headers.get('X-RateLimit-Remaining')=='0' or exc.headers.get('Retry-After')))):
            return 'remote_rate_limit', '远端请求限流，请稍后重试；GitHub 可配置访问令牌提高额度。'
        if exc.code in (401,403): return 'remote_permission', '远端拒绝访问，请核对仓库权限或访问令牌。'
        if exc.code==404: return 'remote_not_found', '仓库、分支或提交不存在，或当前账号没有访问权限。'
        if exc.code in (500,502,503,504): return 'remote_unavailable', '远端服务暂时异常，已重试；可稍后重新检查。'
    if isinstance(exc, (http.client.IncompleteRead, http.client.RemoteDisconnected, ConnectionError, TimeoutError)):
        return 'remote_unavailable', '远端连接中断或响应不完整，已重试；可稍后重新检查。'
    if 'rate limit exceeded' in s or 'quota exceeded' in s:
        return 'remote_rate_limit', '远端请求限流，请稍后重试；GitHub 可配置访问令牌提高额度。'
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
    if getattr(exc,'source_issue',None):
        issue=exc.source_issue
        event['source_issue']={'kind':issue.get('kind'),'declared_path':redact(issue.get('declared_path','')),
                               'candidate_paths':[redact(p) for p in issue.get('candidate_paths',[])[:20]],
                               'candidates_verified':False}
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
