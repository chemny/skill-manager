"""Public update services; searches produce candidates, never source bindings."""
import hashlib
import json
import os
import re
import subprocess
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse


class IntegrityError(ValueError):
    pass


def public_url(value):
    value = str(value or '').strip().rstrip('/')
    p = urlparse(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment or re.search(r'\s', value):
        raise ValueError('请输入不含凭证和查询参数的 HTTPS 地址')
    return value


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != 'https':
            raise ValueError('下载重定向必须使用 HTTPS')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urlparse(req.full_url).netloc != urlparse(newurl).netloc:
            for name in ('X-api-key', 'Authorization'):
                redirected.remove_header(name)
        return redirected


def request(channel, suffix, limit=4_000_000):
    headers = {'User-Agent':'Skill-Manager', 'Accept':'application/json'}
    env = 'SKILLHUB_API_KEY' if channel['name'] == 'skillhub' else 'ASM_CHANNEL_' + re.sub(r'[^A-Z0-9]', '_', channel['name'].upper()) + '_KEY'
    if os.environ.get(env):
        headers['X-API-Key'] = os.environ[env]
    opener = urllib.request.build_opener(PublicRedirect())
    from network_io import read
    return read(urllib.request.Request(channel['base_url'] + suffix, headers=headers), limit=limit,
                opener=opener.open)


def api_json(channel, suffix):
    return json.loads(request(channel, suffix))


def channels(legacy, registry=None):
    if registry is not None:
        if hasattr(registry, '_batch_update_channels'):
            if registry._batch_update_channels is None:
                registry._batch_update_channels = registry.update_channels()
            return registry._batch_update_channels
        return registry.update_channels()
    registry = legacy.Registry()
    try:
        return registry.update_channels()
    finally:
        registry.conn.close()


def service(legacy, name, registry=None):
    channel = next((c for c in channels(legacy, registry) if c['name'] == name), None)
    if not channel or not channel['enabled']:
        raise ValueError('更新源不存在或未启用')
    return channel


def identity(src):
    if not src:
        return None
    return tuple(str(src.get(key) or '') for key in ('provider', 'channel', 'url', 'slug', 'ref', 'path'))


def validate_source(legacy, src, registry=None):
    if not src.get('channel'):
        raise ValueError('来源绑定缺少渠道信息，请重新绑定来源')
    channel = service(legacy, src['channel'], registry)
    if channel['kind'] != src['provider']:
        raise ValueError('更新源类型已变化，请重新绑定来源')
    url = public_url(src['url'])
    base = public_url(channel['base_url'])
    if src['provider'] == 'git':
        matches = url.removesuffix('.git') == base.removesuffix('.git')
    else:
        slug = src.get('slug', '')
        matches = bool(re.fullmatch(r'[A-Za-z0-9_.-]+', slug)) and url == base + '/api/v1/skills/' + slug
    if not matches:
        raise ValueError('更新源地址已变化，请重新绑定来源')
    return channel


def source(legacy, url, ref='', path='', evidence='user-confirmed', registry=None):
    try:
        clean = public_url(url)
    except ValueError:
        return None
    for channel in channels(legacy, registry):
        base = channel['base_url'].rstrip('/')
        if channel['kind'] == 'skillhub':
            prefix = base + '/api/v1/skills/'
            slug = clean[len(prefix):] if clean.startswith(prefix) else ''
            if slug and re.fullmatch(r'[A-Za-z0-9_.-]+', slug):
                return {'provider':'skillhub', 'channel':channel['name'], 'url':prefix+slug,
                        'slug':slug, 'ref':ref, 'path':'', 'evidence':evidence}
        if channel['kind'] == 'git' and clean.removesuffix('.git') == base.removesuffix('.git'):
            return {'provider':'git', 'channel':channel['name'], 'url':base,
                    'ref':ref, 'path':path, 'evidence':evidence}
    return None


def search(legacy, name, channel_name=None):
    results, errors = [], []
    for channel in channels(legacy):
        if not channel['enabled'] or channel['kind'] not in ('skillhub', 'git') or (channel_name and channel['name'] != channel_name):
            continue
        try:
            if channel['kind'] == 'skillhub':
                payload = api_json(channel, '/api/skills?' + urlencode({'keyword':name, 'pageSize':10}))
                for item in payload.get('data', {}).get('skills', []):
                    slug = item.get('slug', '')
                    candidate = source(legacy, channel['base_url']+'/api/v1/skills/'+slug, evidence='remote-search')
                    if candidate:
                        results.append({'source':candidate, 'name':item.get('name') or slug,
                                        'version':item.get('version'), 'evidence':'remote-search',
                                        'from_path':channel['label']+' · '+(item.get('name') or slug)})
            else:
                results.append({'source':source(legacy, channel['base_url'], evidence='remote-search'),
                                'evidence':'remote-search', 'from_path':channel['label']+' · '+channel['base_url']})
            legacy.record_channel_event(channel['name'], 'search', 'success', name)
        except Exception as exc:
            # Do not persist server responses or credential-bearing URLs.
            message = type(exc).__name__
            errors.append({'channel':channel['label'], 'error':message})
            legacy.record_channel_event(channel['name'], 'search', 'failure', name, message)
    return {'candidates':results, 'errors':errors}


def download(legacy, src, folder):
    channel = validate_source(legacy, src)
    folder = folder / 'provider-download'
    folder.mkdir()
    if src['provider'] == 'skillhub':
        slug = src['slug']
        detail = api_json(channel, '/api/v1/skills/' + quote(slug, safe=''))
        version = src.get('ref') or detail['latestVersion']['version']
        archive = request(channel, '/api/v1/download?' + urlencode({'slug':slug, 'version':version}), 64_000_000)
        archive_path = folder / 'skill.zip'
        archive_path.write_bytes(archive)
        with zipfile.ZipFile(archive_path) as zf:
            if len(zf.infolist()) > 10000 or sum(f.file_size for f in zf.infolist()) > 256_000_000:
                raise ValueError('安装包解压后超过大小限制')
            legacy.safe_extract_zip(zf, folder / 'content')
        candidates = legacy.skill_install_candidates(folder / 'content')
        if len(candidates) != 1:
            raise ValueError('更新源安装包必须包含一个明确的 Skill')
        skill = folder / 'content' / candidates[0]['remote_path']
        from maintenance import manifest
        files = manifest(skill)
        if any(f['kind'] == 'link' for f in files.values()):
            raise ValueError('更新源安装包包含链接')
        listed = api_json(channel, '/api/v1/skills/' + quote(slug, safe='') + '/files?' + urlencode({'version':version}))
        expected = {f['path']:f['sha256'] for f in listed['files']}
        actual = {p.relative_to(skill).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in skill.rglob('*') if p.is_file()}
        if '_meta.json' in actual and '_meta.json' not in expected:
            meta = json.loads((skill/'_meta.json').read_text(encoding='utf-8'))
            if meta.get('slug') != slug or meta.get('version') != version:
                raise ValueError('安装包元信息与请求的 Skill 版本不一致')
            actual.pop('_meta.json')
        if not expected or actual != expected:
            missing = sorted(set(expected)-set(actual))
            extra = sorted(set(actual)-set(expected))
            changed = sorted(p for p in set(expected)&set(actual) if expected[p]!=actual[p])
            detail = '; '.join(label+': '+', '.join(paths[:6]) for label,paths in
                               [('missing',missing),('extra',extra),('changed',changed)] if paths)
            raise IntegrityError('安装包文件与更新源校验清单不一致; '+detail)
        legacy.record_channel_event(channel['name'], 'download', 'success', slug)
        return skill, {**src, 'resolved_ref':version, 'commit':hashlib.sha256(archive).hexdigest()}
    if src['provider'] == 'git':
        root = legacy.clone_git_repository(channel['base_url'], folder, src.get('ref') or '')
        from maintenance import safe_relative, manifest
        safe_relative(src.get('path', ''))
        skill = root / src.get('path', '')
        if not (skill/'SKILL.md').is_file() or (skill.resolve()!=root.resolve() and root.resolve() not in skill.resolve().parents):
            raise ValueError('请确认仓库内准确的 Skill 子目录')
        if any(f['kind'] == 'link' for f in manifest(skill).values()):
            raise ValueError('仓库 Skill 包含链接')
        commit = subprocess.run(['git','-C',str(root),'rev-parse','HEAD'], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        legacy.record_channel_event(channel['name'], 'download', 'success', skill.name)
        return skill, {**src, 'resolved_ref':legacy.current_git_branch(root), 'commit':commit}
    raise ValueError('不支持的更新源类型')


def test_connection(legacy, name):
    channel = service(legacy, name)
    if name == 'github':
        data = legacy.github_json('https://api.github.com/rate_limit')
        if not isinstance(data.get('resources'),dict): raise ValueError('GitHub 返回格式不兼容')
        result = {'message':'GitHub API 连接成功'}
    elif channel['kind'] == 'skills-sh':
        data = api_json(channel, '/api/search?' + urlencode({'q':'find-skills','limit':1}))
        if not isinstance(data.get('skills'),list): raise ValueError('skills.sh 返回格式不兼容')
        result = {'message':'Vercel Skills 搜索接口连接成功', 'matches':len(data['skills'])}
    elif channel['kind'] == 'skillhub':
        data = api_json(channel, '/api/skills?' + urlencode({'keyword':'find skill', 'pageSize':1}))
        if not isinstance(data.get('data', {}).get('skills'), list):
            raise ValueError('更新源返回格式不兼容')
        result = {'message':'搜索接口连接成功', 'matches':data['data'].get('total', 0)}
    elif channel['kind'] == 'git':
        run = subprocess.run(['git','ls-remote',channel['base_url'],'HEAD'], capture_output=True, text=True, timeout=20, check=False)
        if run.returncode or not run.stdout.strip():
            raise ValueError('无法读取公开 Git 仓库，请检查地址及访问权限')
        result = {'message':'Git 仓库连接成功'}
    else:
        raise ValueError('此渠道尚不支持独立连接测试')
    legacy.record_channel_event(name, 'test', 'success')
    return result
