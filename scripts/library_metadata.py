"""Bounded local metadata recovery. Repository mentions remain unconfirmed clues."""
import datetime as dt
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

CATEGORIES = ('内容创作', '编程开发', '信息研究', '办公效率', '工具与自动化')
TAGS = ('写作', '图片', '音视频', '代码', '搜索', '数据', '文档', '自动化')


def directory_date(path):
    try:
        s = Path(path).resolve(strict=True).stat()
        value = getattr(s, 'st_birthtime', None)
        if value is None and os.name == 'nt':
            value = s.st_ctime
        if value and 0 < value <= dt.datetime.now(dt.timezone.utc).timestamp():
            return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(timespec='seconds')
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def installer_record(path):
    """Only exact children of a lock file's skills directory; never match names globally."""
    folder = Path(path).resolve()
    lock = folder.parent.parent / '.skill-lock.json'
    if folder.parent.name != 'skills':
        return None
    try:
        if lock.stat().st_size > 4_000_000:
            return None
        record = json.loads(lock.read_text(encoding='utf-8')).get('skills', {}).get(folder.name)
        return record if isinstance(record, dict) else None
    except (OSError, ValueError, AttributeError):
        return None


def installation(path, installed_at=None, first_seen=None):
    if installed_at:
        return {'at': installed_at, 'basis': 'installed'}
    record = installer_record(path) or {}
    value = record.get('installedAt')
    try:
        at = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if at.tzinfo and at <= dt.datetime.now(dt.timezone.utc):
            return {'at': at.isoformat(), 'basis': 'installer-record'}
    except (AttributeError, TypeError, ValueError):
        pass
    value = directory_date(path)
    return {'at': value or first_seen, 'basis': 'directory-created' if value else 'first-recorded'}


def repository(legacy, url, ref='', path='', evidence='file-link'):
    if not isinstance(url, str):
        return None
    # Git's standard SSH origin is normalized without executing network commands.
    url = re.sub(r'^git@(github\.com|gitlab\.com|gitee\.com):', r'https://\1/', url.strip())
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    from update_providers import source as registered_source
    registered = registered_source(legacy, url, ref, path, evidence)
    if registered:
        if Path(path).is_absolute() or '..' in Path(path).parts or '\\' in path:
            return None
        return registered
    parsers = {'github.com': legacy.parse_github_url, 'gitlab.com': legacy.parse_gitlab_url,
               'gitee.com': legacy.parse_gitee_url, 'gitcode.com': legacy.parse_gitcode_url}
    parser = parsers.get(parsed.hostname)
    info = parser(url) if parser else None
    if not info:
        return None
    provider = info['provider']
    canonical = f"https://github.com/{info['owner']}/{info['repo']}" if provider == 'github' else f"https://gitlab.com/{info['project']}" if provider == 'gitlab' else legacy.git_repo_url(info).removesuffix('.git')
    subpath = path or info.get('path') or ''
    if subpath == 'SKILL.md': subpath = ''
    elif subpath.endswith('/SKILL.md'): subpath = subpath[:-9]
    if Path(subpath).is_absolute() or '..' in Path(subpath).parts or '\\' in subpath:
        return None
    return {'provider': provider, 'url': canonical, 'ref': ref or info.get('ref') or '', 'path': subpath, 'evidence': evidence}


def locked_source(legacy, path):
    record = installer_record(path) or {}
    return repository(legacy, record.get('sourceUrl', ''), path=record.get('skillPath', ''), evidence='installer-lock')


def file_candidates(legacy, path):
    folder = Path(path).resolve()
    result, seen = [], set()
    for name in ('SKILL.md', 'README.md', '.skill.json', 'metadata.json'):
        p = folder / name
        try:
            if p.is_symlink() or p.stat().st_size > 300_000:
                continue
            text = p.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            continue
        for url in re.findall(r'https://(?:github\.com|gitlab\.com|gitee\.com|gitcode\.com)/[^\s<>"\x27`)\]}]+', text):
            src = repository(legacy, url.rstrip('.,;:'))
            if src and src['url'] not in seen:
                seen.add(src['url'])
                result.append({'source': src, 'evidence': 'file-link', 'from_path': str(p)})
    return result[:8]
