"""Official public catalogs, persistent bounded caches, and explicit GitHub verification.

Catalog entries are untrusted candidates, never local update-source bindings.
"""
import base64
import concurrent.futures
import email.utils
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from network_io import read
from update_checks import path_lock

CATALOG_TTL = 6 * 3600
SEARCH_TTL = 24 * 3600
VERIFY_TTL = 6 * 3600
PROVIDERS = ('clawhub', 'skillsmp')
SEARCH_PAGE_SIZE = 30
SEARCH_CANDIDATE_PAGES = 4


def title_rank(name, query):
    title = unicodedata.normalize('NFKC', str(name or '')).strip().casefold()
    query = unicodedata.normalize('NFKC', query).strip().casefold()
    if title == query:
        return 0
    if title.startswith(query):
        return 1
    if title.endswith(query):
        return 3
    return 2 if query in title else None


def selection(payload):
    provider = payload.get('provider', '')
    category = payload.get('category', '')
    page = payload.get('page', 1)
    if provider not in ('', 'skillhub', *PROVIDERS) or not isinstance(category, str) or not re.fullmatch(r'[a-z0-9-]{0,80}', category):
        raise ValueError('Invalid discovery filter')
    if type(page) is not int or not 1 <= page <= 10000:
        raise ValueError('Invalid discovery page')
    if category and provider != 'skillhub':
        raise ValueError('此来源暂不支持分类或分页')
    if page != 1 and provider != 'skillhub' and not (provider == 'clawhub' and payload.get('view') == 'all'):
        raise ValueError('此来源暂不支持分类或分页')
    if provider == 'skillsmp' and payload.get('view') != 'search':
        raise ValueError('SkillsMP 仅支持搜索')
    if provider == 'skillhub' and payload.get('view') == 'new':
        raise ValueError('此来源未提供首次发布时间排序')
    return provider, category, page


def request_key(provider, view, query, payload):
    _, category, page = selection(payload)
    if view == 'search':
        return json.dumps(['title-v1', provider, query, category], ensure_ascii=False)
    return cache_key(provider, view, query) if not category and page == 1 else json.dumps([provider, view, query, category, page])


class Catalog(list):
    def __init__(self, items, total, categories, limited=False):
        super().__init__(items)
        self.total, self.categories = total, categories
        self.limited = limited


def fetch_skillhub(view, query, category, page):
    rows, total = [], 0
    pages = range(1, SEARCH_CANDIDATE_PAGES + 1) if view == 'search' else (page,)
    for current_page in pages:
        params = {'page': current_page, 'pageSize': SEARCH_PAGE_SIZE, 'sortBy': 'downloads', 'order': 'desc'}
        if query:
            params['keyword'] = query
        if category:
            params['category'] = category
        data = remote_json('https://api.skillhub.cn/api/skills?' + urllib.parse.urlencode(params), 'skillhub')
        if data.get('code') != 0 or not isinstance(data.get('data', {}).get('skills'), list):
            raise ValueError('Invalid SkillHub response')
        batch = data['data']['skills']
        rows.extend(batch)
        total = data['data'].get('total', len(rows))
        if view != 'search' or not batch or len(rows) >= total:
            break
    categories = remote_json('https://api.skillhub.cn/api/v1/categories', 'skillhub').get('items', [])
    categories = [{'key': c['key'], 'name': short(c.get('name'), 60)} for c in categories
                  if isinstance(c, dict) and re.fullmatch(r'[a-z0-9-]{1,80}', str(c.get('key', '')))]
    items = []
    for row in rows:
        url = safe_url(row.get('homepage'), 'api.skillhub.cn')
        if not url or not row.get('slug'):
            continue
        items.append({'id': 'skillhub:' + hashlib.sha256(url.encode()).hexdigest()[:24],
                      'provider': 'skillhub', 'identity': url, 'url': url, 'github_url': '',
                      'name': short(row.get('name') or row['slug'], 160), 'slug': short(row['slug'], 160),
                      'owner': short(row.get('ownerName'), 120), 'category': short(row.get('category'), 80),
                      'description': short(row.get('description_zh') or row.get('description'), 1500),
                      'version': short(row.get('version'), 100), 'published_at': row.get('created_at'),
                      'stats': {k: row[k] for k in ('downloads', 'stars', 'installs') if isinstance(row.get(k), (int, float)) and row[k] >= 0}})
    if rows and not items:
        raise ValueError('Catalog items could not be identified')
    return Catalog(items, total, categories, view == 'search' and len(rows) < total)
SCHEMA = """
create table if not exists discovery_cache (
 cache_key text primary key, provider text not null, fetched_at real not null default 0,
 attempted_at real not null, retry_at real not null default 0, error text not null default '',
 payload text not null default '[]');
create table if not exists discovery_candidates (
 id text primary key, payload text not null, seen_at real not null,
 verification text, verified_at real not null default 0, error text not null default '');
create table if not exists discovery_cooldowns (provider text primary key, until_at real not null);
"""


def initialize(manager):
    manager.db.executescript(SCHEMA)
    import daily_recommendations
    daily_recommendations.initialize(manager)


def short(value, limit=1000):
    return ''.join(c for c in str(value or '') if c >= ' ' or c in '\n\t')[:limit]


def safe_url(value, host):
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 32 for c in value):
        return ''
    p = urllib.parse.urlsplit(value)
    if p.scheme != 'https' or p.netloc.lower() != host or p.username or p.password or p.query or p.fragment:
        return ''
    return value


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc or not newurl.startswith('https://'):
            raise ValueError('Cross-host catalog redirect refused')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def remote_json(url, provider):
    headers = {'User-Agent': 'AgentSkillManager/Discovery', 'Accept': 'application/json'}
    token = os.environ.get({'skillsmp': 'SKILLSMP_API_KEY', 'github': 'GITHUB_TOKEN'}.get(provider, ''))
    if token:
        headers['Authorization'] = 'Bearer ' + token
    opener = urllib.request.build_opener(SameHostRedirect()).open
    result = json.loads(read(urllib.request.Request(url, headers=headers), opener=opener,
                             timeout=20, attempts=2, limit=2_000_000))
    if not isinstance(result, dict):
        raise ValueError('Invalid catalog response')
    return result


def failure(error):
    now = time.time()
    if isinstance(error, urllib.error.HTTPError):
        headers = error.headers or {}
        error.close()
        rate_limit = error.code == 429 or (error.code == 403 and headers.get('X-RateLimit-Remaining') == '0')
        deadlines = []
        raw = headers.get('Retry-After', '')
        try:
            deadlines.append(now + max(1, float(raw)))
        except (TypeError, ValueError):
            try:
                deadlines.append(email.utils.parsedate_to_datetime(raw).timestamp())
            except (TypeError, ValueError, OverflowError):
                pass
        for key, base in (('X-RateLimit-Reset', 0), ('RateLimit-Reset', now)):
            try:
                deadlines.append(base + float(headers[key]))
            except (KeyError, TypeError, ValueError):
                pass
        retry = max((t for t in deadlines if math.isfinite(t) and t > now), default=now + 60)
        if rate_limit:
            return '接口限流，冷却后可重试', max(now + 1, retry), True
        return {401: '接口授权失败', 403: '接口拒绝访问', 404: '原始路径不存在',
                503: '来源服务暂时不可用'}.get(error.code, '来源返回 HTTP ' + str(error.code)), max(now + 60, retry), False
    if isinstance(error, (TimeoutError, urllib.error.URLError, ConnectionError)):
        return '来源连接失败，可稍后重试', now + 60, False
    messages = {'Catalog and Skill metadata do not match': '平台信息与原始文件不一致',
                'GitHub file hash mismatch': '原始文件校验失败',
                'Unsupported Skill file': '原始路径不是支持的 Skill 文件',
                '缺少明确的 GitHub 原始路径': '缺少明确的 GitHub 原始路径',
                'Remote response exceeds size limit': '来源响应超过大小限制'}
    return messages.get(str(error), '来源数据无法核实'), now + 60, False


def options(payload):
    view = payload.get('view', 'trending')
    query = payload.get('query', '')
    if view not in ('all', 'trending', 'new', 'search') or not isinstance(query, str) or len(query) > 160:
        raise ValueError('Invalid discovery query')
    query = query.strip()
    if any(ord(c) < 32 for c in query) or (view == 'search' and not query):
        raise ValueError('请输入搜索关键词')
    return view, query if view == 'search' else ''


def cache_key(provider, view, query):
    return json.dumps([provider, view, query], ensure_ascii=False, separators=(',', ':'))


def normalize(provider, data):
    if provider == 'skillsmp':
        if data.get('success') is not True or not isinstance(data.get('data'), dict):
            raise ValueError('SkillsMP returned no successful result')
        rows = data['data'].get('skills')
    else:
        rows = data.get('items', data.get('results'))
    if not isinstance(rows, list):
        raise ValueError('Catalog items missing')
    items, seen = [], set()
    for row in rows[:50]:
        if not isinstance(row, dict):
            continue
        if provider == 'clawhub':
            owner = row.get('ownerHandle') or (row.get('owner') or {}).get('handle')
            slug = row.get('slug')
            if not all(isinstance(s, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', s) for s in (owner, slug)):
                continue
            identity = owner + '/' + slug
            url = 'https://clawhub.ai/' + urllib.parse.quote(identity, safe='/')
            stats = row.get('stats') or {}
            github = ''
            name = row.get('displayName') or slug
            description = row.get('summary')
            version = (row.get('latestVersion') or {}).get('version') or row.get('version')
            published = row.get('createdAt')
        else:
            identity = short(row.get('id'), 300)
            url = safe_url(row.get('skillUrl'), 'skillsmp.com')
            if not identity or not url:
                continue
            owner, slug = short(row.get('author'), 120), short(row.get('name'), 120)
            stats = {'stars': row.get('stars')}
            github = safe_url(row.get('githubUrl'), 'github.com')
            name, description, version, published = slug, row.get('description'), '', None
        item_id = provider + ':' + hashlib.sha256(identity.encode()).hexdigest()[:24]
        if item_id in seen:
            continue
        seen.add(item_id)
        items.append({'id': item_id, 'provider': provider, 'identity': identity, 'owner': owner,
                      'slug': slug, 'name': short(name, 160), 'description': short(description, 1500),
                      'url': url, 'github_url': github, 'version': short(version, 100),
                      'published_at': published if isinstance(published, (int, float)) else None,
                      'stats': {k: v for k, v in stats.items() if k in ('downloads', 'stars', 'installs')
                                and isinstance(v, (int, float)) and v >= 0}})
    if rows and not items:
        raise ValueError('Catalog items could not be identified')
    return items


def fetch(provider, view, query, cursor=None):
    if provider == 'clawhub':
        params = {'limit': 30, 'nonSuspiciousOnly': 'true'}
        if view == 'search':
            params['q'] = query
        elif view == 'all':
            if cursor:
                params['cursor'] = cursor
        else:
            params['sort'] = 'trending' if view == 'trending' else 'createdAt'
        url = 'https://clawhub.ai/api/v1/' + ('search' if view == 'search' else 'skills')
    else:
        params = {'q': query, 'limit': 30, 'sortBy': 'stars'}
        url = 'https://skillsmp.com/api/v1/skills/search'
    data = remote_json(url + '?' + urllib.parse.urlencode(params), provider)
    items = normalize(provider, data)
    if view == 'all' and provider == 'clawhub':
        return Catalog(items, 0, [], bool(data.get('nextCursor'))), data.get('nextCursor')
    return items


def table_exists(db, name):
    return bool(db.execute('select 1 from sqlite_master where name=?', (name,)).fetchone())


def installed_index(manager):
    sources, names, catalog_sources = set(), set(), set()
    for row in manager.db.execute("select c.name,m.source from capabilities c left join maintenance_items m on m.id=c.id where c.kind='skill' and c.health not in ('missing','not-scanned','broken-link')"):
        names.add(row['name'].casefold())
        try:
            source = json.loads(row['source'] or '{}')
            if source.get('provider') in ('skillhub', 'clawhub') and re.fullmatch(r'[A-Za-z0-9_.-]+', str(source.get('slug') or '')):
                catalog_sources.add((source['provider'],source['slug'].casefold()))
            if source.get('provider') != 'github':
                continue
            url = safe_url(source.get('url', ''), 'github.com')
            parts = urllib.parse.urlsplit(url).path.strip('/').split('/')
            if len(parts)<2 or not all(re.fullmatch(r'[A-Za-z0-9_.-]+', p) for p in parts[:2]):
                continue
            path = source.get('path')
            if path is None:
                path = github_location(url)['path']
            path = path.removesuffix('/SKILL.md').strip('/')
            if path == 'SKILL.md' or path == '.':path = ''
            sources.add(('/'.join(parts[:2]).removesuffix('.git').casefold(), path))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    return sources, names, catalog_sources


def group_verified_copies(items):
    """Collapse only candidates verified at the same pinned GitHub Skill path."""
    groups, result = {}, []
    for item in items:
        verified = item.get('verification') if item.get('verification_fresh') and not item.get('verification_error') else None
        key = None
        if verified and verified.get('install_url'):
            try:
                location = github_location(verified['install_url'])
                key = (location['repo'].casefold(), location['ref'], location['path'], item['name'].casefold())
            except (TypeError, ValueError, KeyError):
                pass
        if key is not None and key in groups:
            group = groups[key]
            group['alternatives'].append({'id': item['id'], 'provider': item['provider'],
                                          'url': item['url'], 'name': item['name']})
            if item.get('installed') == 'source_match' or (item.get('installed') == 'name_only' and group.get('installed') == 'unknown'):
                group['installed'] = item['installed']
            continue
        if key is not None:
            groups[key] = item
            item['alternatives'] = []
        result.append(item)
    return result


def cached(manager, payload):
    view, query = options(payload)
    provider, category, page = selection(payload)
    providers = (provider,) if provider else PROVIDERS if view == 'search' else ('skillhub',) if view == 'all' else ('clawhub',)
    now, items, states = time.time(), [], []
    total, categories, limited, has_more = 0, [], False, False
    available = table_exists(manager.db, 'discovery_cache')
    sources, names, catalog_sources = installed_index(manager)
    for provider in providers:
        key = request_key(provider, view, query, payload)
        row = manager.db.execute('select * from discovery_cache where cache_key=?', (key,)).fetchone() if available else None
        cooldown = manager.db.execute('select until_at from discovery_cooldowns where provider=?', (provider,)).fetchone() if available else None
        retry_at = max(row['retry_at'] if row else 0, cooldown[0] if cooldown else 0)
        fresh = bool(row and row['fetched_at'] and now - row['fetched_at'] < (SEARCH_TTL if view == 'search' else CATALOG_TTL))
        state = {'provider': provider, 'fresh': fresh, 'fetched_at': row['fetched_at'] if row else 0,
                 'error': row['error'] if row else '', 'retry_at': retry_at,
                 'needs_refresh': not fresh and retry_at <= now}
        states.append(state)
        stored = json.loads(row['payload'] if row else '[]')
        if isinstance(stored, dict):
            total += stored.get('total', 0)
            categories = stored.get('categories', [])
            limited |= bool(stored.get('limited', False))
            has_more |= bool(stored.get('next_cursor'))
            stored = stored['items']
        else:
            total += len(stored)
            if view == 'search' and len(stored) >= SEARCH_PAGE_SIZE:
                limited = True
        for item in stored:
            saved = manager.db.execute('select payload,verification,verified_at,error from discovery_candidates where id=?', (item['id'],)).fetchone()
            if saved:
                # Another search may have observed a changed original URL for this identity.
                item.update(json.loads(saved['payload']))
            item['installed'] = 'unknown'
            if (item['provider'],str(item.get('slug') or '').casefold()) in catalog_sources:
                item['installed'] = 'source_match'
            try:
                info = github_location(item['github_url'])
                if (info['repo'].casefold(), info['path']) in sources:
                    item['installed'] = 'source_match'
            except ValueError:
                pass
            if item['installed'] == 'unknown' and item['slug'].casefold() in names:
                item['installed'] = 'name_only'
            item['verification'] = json.loads(saved['verification']) if saved and saved['verification'] else None
            if item['verification']:
                item['verification'].pop('preview', None)
            item['verification_error'] = saved['error'] if saved else ''
            item['verification_fresh'] = bool(saved and now - saved['verified_at'] < VERIFY_TTL)
            items.append(item)
    if view == 'search':
        items = [item for item in items if title_rank(item['name'], query) is not None]
        items.sort(key=lambda item: (title_rank(item['name'], query), unicodedata.normalize('NFKC', item['name']).casefold(), item['id']))
        items = group_verified_copies(items)
        total = len(items)
        if provider == 'skillhub':
            items = items[(page - 1) * SEARCH_PAGE_SIZE:page * SEARCH_PAGE_SIZE]
    else:
        items = group_verified_copies(items)
    return {'view': view, 'query': query, 'items': items, 'providers': states, 'total': total, 'categories': categories, 'page': page, 'has_more': has_more, 'search_limited': limited if view == 'search' else False}


def refresh(manager, payload):
    view, query = options(payload)
    selected, category, page = selection(payload)
    with path_lock('discovery:' + request_key(selected, view, query, payload)):
        state = cached(manager, payload)
        pending = [s['provider'] for s in state['providers'] if s['needs_refresh']]
        # Network runs outside SQLite transactions; one unavailable source cannot erase another.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = {}
            for p in pending:
                if p == 'skillhub':
                    futures[p] = pool.submit(fetch_skillhub, view, query, category, page)
                elif p == 'clawhub' and view == 'all':
                    previous = manager.db.execute('select payload from discovery_cache where cache_key=?', (request_key(p, view, query, dict(payload, page=page - 1)),)).fetchone() if page > 1 else None
                    previous_data = json.loads(previous[0]) if previous else None
                    if page > 1 and (not isinstance(previous_data, dict) or not previous_data.get('next_cursor')):
                        raise ValueError('请从第一页依次浏览')
                    futures[p] = pool.submit(fetch, p, view, query, previous_data.get('next_cursor') if previous_data else None)
                else:
                    futures[p] = pool.submit(fetch, p, view, query)
            for provider, future in futures.items():
                key, now = request_key(provider, view, query, payload), time.time()
                try:
                    fetched = future.result()
                    items, next_cursor = fetched if provider == 'clawhub' and view == 'all' else (fetched, None)
                except Exception as error:
                    message, retry, rate_limit = failure(error)
                    manager.db.execute("insert into discovery_cache(cache_key,provider,attempted_at,retry_at,error) values(?,?,?,?,?) on conflict(cache_key) do update set attempted_at=excluded.attempted_at,retry_at=excluded.retry_at,error=excluded.error", (key, provider, now, retry, message))
                    if rate_limit:
                        manager.db.execute('insert into discovery_cooldowns values(?,?) on conflict(provider) do update set until_at=max(until_at,excluded.until_at)', (provider, retry))
                else:
                    stored = {'items': items, 'total': items.total, 'categories': items.categories, 'limited': items.limited, 'next_cursor': next_cursor} if isinstance(items, Catalog) else items
                    manager.db.execute("insert into discovery_cache values(?,?,?,?,?,?,?) on conflict(cache_key) do update set fetched_at=excluded.fetched_at,attempted_at=excluded.attempted_at,retry_at=0,error='',payload=excluded.payload", (key, provider, now, now, 0, '', json.dumps(stored)))
                    for item in items:
                        old = manager.db.execute('select payload from discovery_candidates where id=?', (item['id'],)).fetchone()
                        if old and json.loads(old[0]).get('github_url') != item['github_url']:
                            manager.db.execute("update discovery_candidates set verification=null,verified_at=0,error='' where id=?", (item['id'],))
                        manager.db.execute('insert into discovery_candidates(id,payload,seen_at) values(?,?,?) on conflict(id) do update set payload=excluded.payload,seen_at=excluded.seen_at', (item['id'], json.dumps(item), now))
                manager.db.commit()
        manager.db.execute('delete from discovery_cache where cache_key not in (select cache_key from discovery_cache order by attempted_at desc limit 128)')
        manager.db.execute('delete from discovery_candidates where seen_at<?', (time.time() - 30 * 86400,))
        manager.db.execute('delete from discovery_candidates where id not in (select id from discovery_candidates order by seen_at desc limit 8000)')
        manager.db.commit()
        return cached(manager, payload)


def github_location(url):
    if not safe_url(url, 'github.com'):
        raise ValueError('缺少明确的 GitHub 原始路径')
    parts = urllib.parse.unquote(urllib.parse.urlsplit(url).path).strip('/').split('/')
    if len(parts) < 4 or parts[2] not in ('tree', 'blob') or not all(re.fullmatch(r'[A-Za-z0-9_.-]+', p) for p in parts[:2]):
        raise ValueError('缺少明确的 GitHub 原始路径')
    ref, path = parts[3], '/'.join(parts[4:])
    if path == 'SKILL.md':
        path = ''
    else:
        path = path.removesuffix('/SKILL.md')
    if not ref or any(p in ('.', '..', '') or '\\' in p or any(ord(c) < 32 for c in p) for p in parts[3:]):
        raise ValueError('Invalid GitHub path')
    return {'repo': '/'.join(parts[:2]), 'ref': ref, 'path': path}


def verify_github(manager, item):
    info = github_location(item['github_url'])
    api = 'https://api.github.com/repos/' + info['repo']
    commit = remote_json(api + '/commits/' + urllib.parse.quote(info['ref'], safe=''), 'github').get('sha', '')
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid GitHub commit')
    path = (info['path'] + '/' if info['path'] else '') + 'SKILL.md'
    data = remote_json(api + '/contents/' + urllib.parse.quote(path, safe='/') + '?ref=' + commit, 'github')
    if data.get('type') != 'file' or data.get('encoding') != 'base64' or not isinstance(data.get('size'), int) or not 0 < data['size'] <= 128_000:
        raise ValueError('Unsupported Skill file')
    content = base64.b64decode(''.join(data.get('content', '').split()), validate=True)
    digest = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
    if len(content) != data['size'] or digest != data.get('sha'):
        raise ValueError('GitHub file hash mismatch')
    text = content.decode('utf-8')
    meta = manager.a.parse_frontmatter_text(text)
    if not all(isinstance(meta.get(k), str) and meta[k].strip() for k in ('name', 'description')) or meta['name'].casefold() != item['slug'].casefold():
        raise ValueError('Catalog and Skill metadata do not match')
    install_url = 'https://github.com/' + info['repo'] + '/tree/' + commit
    if info['path']:
        install_url += '/' + urllib.parse.quote(info['path'], safe='/')
    return {'commit': commit, 'path': path, 'blob': digest, 'name': short(meta['name'], 160),
            'description': short(meta['description'], 1500), 'version': short(meta.get('version'), 100),
            'install_url': install_url, 'file_url': 'https://github.com/' + info['repo'] + '/blob/' + commit + '/' + urllib.parse.quote(path, safe='/'),
            'checked_at': time.time(), 'preview': text[:16000]}


def verify(manager, payload):
    item_id = payload.get('id')
    if not isinstance(item_id, str) or len(item_id) > 100:
        raise ValueError('Invalid discovery candidate')
    with path_lock('discovery-verify:' + item_id):
        row = manager.db.execute('select * from discovery_candidates where id=?', (item_id,)).fetchone()
        if not row:
            raise ValueError('推荐结果已过期，请重新搜索')
        item = json.loads(row['payload'])
        if payload.get('github_url') is not None and payload['github_url'] != item['github_url']:
            return {'id': item_id, 'error': '来源信息已变化，请重新读取结果'}
        if row['verification'] and time.time() - row['verified_at'] < VERIFY_TTL and not row['error']:
            return {'id': item_id, 'verification': json.loads(row['verification']), 'from_cache': True}
        cooldown = manager.db.execute("select until_at from discovery_cooldowns where provider='github'").fetchone()
        if cooldown and cooldown[0] > time.time():
            return {'id': item_id, 'error': 'GitHub 接口限流，冷却后可重试', 'retry_at': cooldown[0]}
        try:
            verified = verify_github(manager, item)
        except Exception as error:
            message, retry, rate_limit = failure(error)
            manager.db.execute('update discovery_candidates set error=? where id=?', (message, item_id))
            if rate_limit:
                manager.db.execute("insert into discovery_cooldowns values('github',?) on conflict(provider) do update set until_at=max(until_at,excluded.until_at)", (retry,))
            manager.db.commit()
            return {'id': item_id, 'error': message, 'retry_at': retry}
        manager.db.execute("update discovery_candidates set verification=?,verified_at=?,error='' where id=?", (json.dumps(verified), time.time(), item_id))
        manager.db.commit()
        return {'id': item_id, 'verification': verified, 'from_cache': False}


def dispatch(manager, payload):
    if payload.get('daily') is True and payload['action'] in ('discovery-cached', 'discovery-refresh'):
        import daily_recommendations
        return (daily_recommendations.cached if payload['action'] == 'discovery-cached' else daily_recommendations.refresh)(manager, payload)
    return {'discovery-cached': cached, 'discovery-refresh': refresh, 'discovery-verify': verify}[payload['action']](manager, payload)
