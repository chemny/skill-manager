"""Daily, anonymous GitHub discovery. Remote documents are data, never instructions."""
import base64
import datetime as dt
import hashlib
from html.parser import HTMLParser
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from network_io import read
from update_checks import path_lock

SCHEMA = """
create table if not exists daily_recommendation_state (
 id integer primary key check(id=1), day text not null, payload text not null,
 retry_at real not null default 0);
create table if not exists daily_recommendation_history (
 identity text primary key, repo_id integer not null, path text not null,
 recommended_at real not null, stars integer not null, major integer not null);
create table if not exists daily_recommendation_limits (
 resource text primary key, until_at real not null);
create table if not exists daily_recommendation_pool (
 identity text primary key, payload text not null, verified_at real not null);
"""
TOPICS = ('large-language-models', 'ai-agents', 'artificial-intelligence')
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
SKILL_WORD = re.compile(r'\bskills?\b|技能', re.IGNORECASE)


def initialize(manager):
    manager.db.executescript(SCHEMA)


def today():
    return dt.datetime.now().astimezone().date().isoformat()


class RepositoryLinks(HTMLParser):
    """Only repository headings, not arbitrary links in topic descriptions."""
    def __init__(self):
        super().__init__()
        self.heading = None
        self.repos = []

    def handle_starttag(self, tag, attrs):
        if tag in ('h2', 'h3'):
            self.heading = tag
        if tag == 'a' and self.heading:
            path = dict(attrs).get('href', '').strip('/')
            if REPO.fullmatch(path) and path.split('/')[0] not in ('topics', 'sponsors', 'collections') and path not in self.repos:
                self.repos.append(path)

    def handle_endtag(self, tag):
        if tag == self.heading:
            self.heading = None


class AnonymousGitHub:
    def __init__(self, manager):
        self.manager = manager
        self.calls = 0
        self.deadline = time.monotonic() + 120
        self.cooldowns = dict(manager.db.execute('select resource,until_at from daily_recommendation_limits'))

    def get(self, url, *, html=False):
        from skill_discovery import SameHostRedirect, failure
        host = urllib.parse.urlsplit(url).netloc
        if host not in ('github.com', 'api.github.com') or not url.startswith('https://'):
            raise ValueError('Unsupported recommendation host')
        resource = 'web' if html else 'search' if '/search/' in url else 'core'
        if self.cooldowns.get(resource, 0) > time.time():
            raise RuntimeError('GitHub 请求额度暂不可用')
        if self.calls >= 40 or time.monotonic() >= self.deadline:
            raise RuntimeError('本轮推荐核实已达上限')
        self.calls += 1
        # Deliberately separate from installation/update clients: never read credentials.
        request = urllib.request.Request(url, headers={
            'User-Agent': 'SkillsManager/DailyRecommendations',
            'Accept': 'text/html' if html else 'application/vnd.github+json'})
        opener = urllib.request.build_opener(SameHostRedirect()).open

        def observe(req, **kwargs):
            response = opener(req, **kwargs)
            if response.headers.get('X-RateLimit-Remaining') == '0':
                try:
                    self.cooldowns[resource] = max(time.time() + 60, float(response.headers.get('X-RateLimit-Reset', 0)))
                except ValueError:
                    self.cooldowns[resource] = time.time() + 3600
            return response

        try:
            raw = read(request, opener=observe, timeout=max(.01, min(12, self.deadline-time.monotonic())),
                       attempts=1, limit=4_000_000)
        except urllib.error.HTTPError as error:
            _, retry, limited = failure(error)
            if limited or error.code == 403:
                self.cooldowns[resource] = retry
            raise
        return raw.decode('utf-8') if html else json.loads(raw)


def collect(client):
    candidates, errors = {}, []

    def add(repo, signal, rank):
        if not isinstance(repo, str) or not REPO.fullmatch(repo):
            return
        item = candidates.setdefault(repo.casefold(), {'repo': repo, 'signals': set(), 'rank': rank})
        item['signals'].add(signal)
        item['rank'] = min(item['rank'], rank)

    pages = [('日榜', 'https://github.com/trending?since=daily')]
    pages += [(topic, 'https://github.com/topics/' + topic) for topic in TOPICS]
    for label, url in pages:
        try:
            parser = RepositoryLinks()
            parser.feed(client.get(url, html=True))
            if not parser.repos:
                raise ValueError('页面中未能识别项目列表')
            for rank, repo in enumerate(parser.repos[:25]):
                add(repo, 'trending' if label == '日榜' else 'topic', rank)
        except Exception as error:
            errors.append({'source': label, 'reason': str(error)[:160]})
    since = (dt.date.fromisoformat(today()) - dt.timedelta(days=7)).isoformat()
    for topic in ('llm', 'ai-agents', 'agent-skills'):
        try:
            query = f'topic:{topic} created:>={since} fork:false archived:false'
            result = client.get('https://api.github.com/search/repositories?' + urllib.parse.urlencode(
                {'q': query, 'sort': 'stars', 'order': 'desc', 'per_page': 20}))
            if not isinstance(result.get('items'), list) or result.get('incomplete_results'):
                raise ValueError('搜索结果不完整')
            for rank, repo in enumerate(result['items']):
                add(repo.get('full_name'), 'new', rank)
        except Exception as error:
            errors.append({'source': 'search:' + topic, 'reason': str(error)[:160]})
    return sorted(candidates.values(), key=lambda c: (
        -int('trending' in c['signals']),
        -int(bool(SKILL_WORD.search(c['repo'].split('/')[-1]))),
        -len(c['signals']), -int('trending' in c['signals']), c['rank'], c['repo'].casefold())), errors


def major_version(release):
    if release.get('draft') or release.get('prerelease') or not str(release.get('body', '')).strip():
        return 0
    match = re.fullmatch(r'v?(\d+)\.\d+\.\d+', str(release.get('tag_name', '')))
    return int(match[1]) if match else 0


def recent_date(value):
    try:
        stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        return 0 <= time.time()-stamp <= 7*86400
    except (ValueError, TypeError, AttributeError):
        return False


def timely_evidence(client, api, repo, entry, signals):
    if 'trending' in signals:
        return {'kind':'trending', 'date':today(), 'reason':'所属仓库今日登上 GitHub 日榜'}
    if recent_date(repo.get('created_at')):
        return {'kind':'new', 'date':repo['created_at'], 'reason':repo['created_at'][:10]+' 新仓库中的 Skill'}
    # Topic membership and repository push time alone are not freshness evidence.
    commits = client.get(api+'/commits?'+urllib.parse.urlencode({'path':entry['path'], 'per_page':1}))
    if not isinstance(commits, list) or not commits:
        return None
    commit = commits[0]
    date = commit.get('commit', {}).get('committer', {}).get('date')
    if not recent_date(date) or not re.fullmatch('[0-9a-f]{40}', commit.get('sha','')):
        return None
    detail = client.get(api+'/commits/'+commit['sha'])
    for file in detail.get('files', []):
        if file.get('filename') != entry['path'] or file.get('status') not in ('added','modified'):
            continue
        lines = file.get('patch','').splitlines()
        added = {' '.join(line[1:].split()) for line in lines if line.startswith('+') and not line.startswith('+++')}
        removed = {' '.join(line[1:].split()) for line in lines if line.startswith('-') and not line.startswith('---')}
        changes = added ^ removed
        meaningful = [line for line in changes if len(line)>30 and not line.startswith(('#','name:','description:'))]
        if meaningful:
            return {'kind':'updated','date':date,'commit':commit['sha'], 'reason':date[:10]+' Skill 指令内容更新'}
    return None


def repeat_reason(old, stars, major, signals, now):
    if not old:
        return ''
    if major > old['major'] and major > 0:
        return '更新：发布新主版本'
    if stars >= max(old['stars'] * 2, old['stars'] + 100):
        return '更新：关注度显著增长'
    if 'trending' in signals and now-old['recommended_at'] >= 14*86400:
        return '更新：再次进入今日热门'
    return None


def verify_repository(manager, client, candidate, history, limit=2):
    from skill_discovery import short
    api = 'https://api.github.com/repos/' + candidate['repo']
    repo = client.get(api)
    if repo.get('archived') or repo.get('disabled') or repo.get('fork') or repo.get('private'):
        return []
    if not isinstance(repo.get('id'), int) or not REPO.fullmatch(str(repo.get('full_name', ''))):
        raise ValueError('仓库身份无法核实')
    name = repo['full_name'].split('/')[-1]
    topics = repo.get('topics') or []
    skill_collection = bool(SKILL_WORD.search(name) or set(topics) & {'skills', 'agent-skills', 'claude-skills', 'codex-skills'})
    if not skill_collection and not SKILL_WORD.search(str(repo.get('description', ''))):
        return []
    tree = client.get(api + '/git/trees/' + urllib.parse.quote(repo['default_branch'], safe='') + '?recursive=1')
    if tree.get('truncated') or not isinstance(tree.get('tree'), list):
        raise ValueError('仓库目录不完整')
    entries = tree['tree']
    # Exclude examples/templates and require usable instructions, not just an AI tag.
    paths = [e for e in entries if e.get('type') == 'blob' and e.get('mode') in ('100644', '100755')
             and e.get('path', '').split('/')[-1] == 'SKILL.md'
             and not set(e['path'].lower().split('/')[:-1]) & {'test', 'tests', 'fixtures', 'examples', 'templates', 'node_modules'}
             and not any(p.startswith('.') for p in e['path'].split('/')[:-1])
             and not any(p in ('', '.', '..') for p in e['path'].split('/'))
             and 0 < e.get('size', 0) <= 128_000]
    if not paths:
        return []
    release_major = 0
    try:
        release_major = major_version(client.get(api + '/releases/latest'))
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    stars = repo.get('stargazers_count', 0)
    if type(stars) is not int or stars < 0:
        raise ValueError('仓库统计无法核实')
    paths.sort(key=lambda e: (f"{repo['id']}:{e['path']}" in history, e['path']))
    items = []
    for entry in paths:
        identity = f"{repo['id']}:{entry['path']}"
        reason = repeat_reason(history.get(identity), stars, release_major, candidate['signals'], time.time())
        evidence = timely_evidence(client, api, repo, entry, candidate['signals'])
        if not evidence:
            continue
        if reason is None:
            changed_at = (dt.datetime.fromisoformat(evidence['date'].replace('Z','+00:00')).timestamp()
                          if evidence['kind']=='updated' else 0)
            if changed_at <= history[identity]['recommended_at']:
                continue
            reason = '更新：Skill 指令内容变化'
        sha = entry.get('sha', '')
        if not re.fullmatch('[0-9a-f]{40}', sha):
            raise ValueError('Skill 文件校验信息不完整')
        blob = client.get(api + '/git/blobs/' + sha)
        if blob.get('encoding') != 'base64':
            raise ValueError('Skill 文件格式无法读取')
        content = base64.b64decode(''.join(blob.get('content', '').split()), validate=True)
        digest = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
        if len(content) != entry['size'] or digest != sha:
            raise ValueError('Skill 文件校验失败')
        text = content.decode('utf-8')
        meta = manager.a.parse_frontmatter_text(text)
        body = re.split(r'^---\s*$', text, maxsplit=2, flags=re.MULTILINE)
        if len(body) < 3 or len(body[2].strip()) < 80 or not all(isinstance(meta.get(k), str) and meta[k].strip() for k in ('name', 'description')):
            continue
        if any(i['name'].casefold() == short(meta['name'], 160).casefold() for i in items):
            continue
        url = 'https://github.com/' + repo['full_name'] + '/blob/' + urllib.parse.quote(repo['default_branch'], safe='') + '/' + urllib.parse.quote(entry['path'], safe='/')
        items.append({'id': 'daily:' + hashlib.sha256(identity.encode()).hexdigest()[:24],
                      'identity': identity, 'repo_id': repo['id'], 'path': entry['path'], 'major': release_major,
                      'name': short(meta['name'], 160), 'description': short(meta['description'], 500),
                      'url': url, 'stats': {'stars': stars}, 'updated': bool(reason),
                      'reason': evidence['reason'], 'evidence': evidence, 'verified_day': today(),
                      'signals': sorted(candidate['signals'])})
        if len(items) >= limit:
            break
    return items


def fill_recommendations(manager, items, previous=()):
    result, seen = [], set()
    for item in items:
        evidence = item.get('evidence', {})
        valid = (evidence.get('date') == today() if evidence.get('kind') == 'trending'
                 else recent_date(evidence.get('date')))
        if valid and item['identity'] not in seen:
            result.append(item)
            seen.add(item['identity'])
    return result[:8]


def cached(manager, payload=None):
    exists = manager.db.execute("select 1 from sqlite_master where name='daily_recommendation_state'").fetchone()
    row = manager.db.execute('select * from daily_recommendation_state where id=1').fetchone() if exists else None
    data = json.loads(row['payload']) if row else {'items': [], 'errors': [], 'complete': False}
    data['items'] = fill_recommendations(manager, data['items'])
    fresh = bool(row and row['day'] == today() and data.get('policy_version') == 2)
    data.update({'view': 'daily', 'day': row['day'] if row else '', 'stale': not fresh,
                 'providers': [{'provider': 'github-daily', 'error': bool(data['errors']),
                                'needs_refresh': (not fresh or not data.get('complete')) and (not row or row['retry_at'] <= time.time())}]})
    return data


def refresh(manager, payload=None):
    with path_lock('daily-recommendations'):
        state = cached(manager)
        if not state['providers'][0]['needs_refresh']:
            return state
        client = AnonymousGitHub(manager)
        history = {r['identity']: dict(r) for r in manager.db.execute('select * from daily_recommendation_history')}
        candidates, errors = collect(client)
        items, seen = [], set()
        for candidate in candidates:
            if client.cooldowns.get('core', 0) > time.time():
                errors.append({'source':'github','reason':'请求额度暂不可用，已停止本轮核实'})
                break
            if client.calls >= 40 or time.monotonic() >= client.deadline:
                errors.append({'source':'budget','reason':'本轮请求预算已用完'})
                break
            try:
                for item in verify_repository(manager, client, candidate, history):
                    if item['identity'] not in seen:
                        items.append(item)
                        seen.add(item['identity'])
            except Exception as error:
                errors.append({'source': candidate['repo'], 'reason': str(error)[:160]})
            if len(items) >= 8:
                break
        items = items[:8]
        with manager.db:
            for item in items:
                manager.db.execute('insert or replace into daily_recommendation_pool values(?,?,?)',
                                   (item['identity'], json.dumps(item), time.time()))
        # Successful empty is distinct from failure; never erase yesterday on an outage.
        keep_previous = not items and bool(errors)
        day = state['day'] if keep_previous else today()
        data = {'items': state['items'] if keep_previous else fill_recommendations(manager, items, state['items']), 'errors': errors,
                'policy_version':2, 'complete': not errors, 'request_count': client.calls, 'attempted_at': time.time()}
        with manager.db:
            manager.db.execute('insert or replace into daily_recommendation_state values(1,?,?,?)',
                               (day, json.dumps(data), time.time()+3600 if errors else 0))
            for item in (data['items'] if not keep_previous else []):
                if item.get('selection'):
                    manager.db.execute('update daily_recommendation_history set recommended_at=? where identity=?',
                                       (time.time(), item['identity']))
                    continue
                manager.db.execute('insert or replace into daily_recommendation_history values(?,?,?,?,?,?)',
                                   (item['identity'], item['repo_id'], item['path'], time.time(), item['stats'].get('stars', 0), item['major']))
            for resource, until in client.cooldowns.items():
                manager.db.execute('insert or replace into daily_recommendation_limits values(?,?)', (resource, until))
        return cached(manager)
