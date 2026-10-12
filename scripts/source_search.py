"""Ordered, cached source discovery. Results are evidence, not update bindings."""
import json
import re
import time
import urllib.error
from urllib.parse import urlencode


SCHEMA = '''
create table if not exists source_search_cache(channel text, query text, expires real, payload text, primary key(channel,query));
create table if not exists source_channel_health(channel text primary key, retry_at real, reason text);
create table if not exists source_candidates(real_path text primary key, searched_at real, payload text);
create table if not exists source_repository_index(url text, ref text, commit_id text, expires real, payload text, primary key(url,ref));
'''


def initialize(m):
    m.db.executescript(SCHEMA)


def policy(m):
    row = m.db.execute("select value from maintenance_settings where key='source_search_mode'").fetchone()
    return {'mode':row['value'] if row else 'auto', 'order':['github','skills-sh','git','skillhub'],
            'timeout_seconds':20, 'budget_seconds':70, 'cache_hours':24, 'failure_cooldown_minutes':10}


def set_policy(m, mode):
    if mode not in ('auto','domestic-first'): raise ValueError('无效的来源查找策略')
    with m.db:
        m.db.execute("insert or replace into maintenance_settings values ('source_search_mode',?)", (mode,))
    return policy(m)


def remember_tree(m, source, commit, tree):
    with m.db:
        m.db.execute('insert or replace into source_repository_index values (?,?,?,?,?)',
                     (source['url'],source.get('ref',''),commit,time.time()+86400,json.dumps(tree)))


def cached_matches(m, row):
    import hashlib
    from pathlib import Path
    from library_metadata import repository
    body = (Path(row['path'])/'SKILL.md').read_bytes()
    digest = hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest()
    result=[]
    for indexed in m.db.execute('select * from source_repository_index where expires>?',(time.time(),)):
        src=repository(m.a,indexed['url'],indexed['ref'],evidence='repository-index')
        if not src: continue
        for item in json.loads(indexed['payload']).get('tree',[]):
            if item.get('type')=='blob' and Path(item.get('path','')).name=='SKILL.md' and item.get('sha')==digest:
                subdir=str(Path(item['path']).parent)
                result.append({'source':{**src,'path':'' if subdir=='.' else subdir}, 'evidence':'content-match',
                               'from_path':'已缓存仓库内容匹配 · '+item['path']})
    return result


def query_channel(m, channel, query):
    from library_metadata import repository
    import update_providers as providers
    kind=channel['kind']
    if channel['name']=='github':
        data=m.a.github_json('https://api.github.com/search/repositories?'+urlencode({'q':query+' skill in:name,description','per_page':5}))
        if not isinstance(data.get('items'),list): raise ValueError('GitHub 返回格式不兼容')
        return [{'source':src,'evidence':'remote-search','from_path':'GitHub 仓库搜索（未核对内容）'}
                for item in data['items'] if (src:=repository(m.a,item.get('html_url',''),evidence='remote-search'))]
    if kind=='skills-sh':
        data=providers.api_json(channel,'/api/search?'+urlencode({'q':query,'limit':10}))
        if not isinstance(data.get('skills'),list): raise ValueError('skills.sh 返回格式不兼容')
        result=[]
        for item in data['skills']:
            repo=item.get('source','')
            if not isinstance(repo,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo): continue
            src=repository(m.a,'https://github.com/'+repo,evidence='skills-sh-search')
            if src: result.append({'source':src,'name':item.get('name',''),'evidence':'remote-search',
                                   'skill_slug':item.get('skillId',''),'from_path':'Vercel skills.sh（目录待核对）'})
        return result
    if kind=='git':
        return [{'source':providers.source(m.a,channel['base_url'],evidence='configured-repository'),
                 'evidence':'configured-repository','from_path':channel['label']+'（目录待核对）'}]
    if kind=='skillhub':
        data=providers.api_json(channel,'/api/skills?'+urlencode({'keyword':query,'pageSize':10}))
        skills=data.get('data',{}).get('skills')
        if not isinstance(skills,list): raise ValueError('SkillHub 返回格式不兼容')
        return [{'source':src,'name':item.get('name') or item['slug'],'version':item.get('version'),
                 'evidence':'remote-search','from_path':channel['label']+'（未核对内容）'} for item in skills
                if (src:=providers.source(m.a,channel['base_url']+'/api/v1/skills/'+str(item.get('slug','')),evidence='remote-search'))]
    raise NotImplementedError('尚未验证公开检索接口')


def search(m, query, channel_name=None):
    query=str(query or '').strip()[:200]
    if not query: raise ValueError('请输入关键词')
    config=policy(m)
    channels=m.r.update_channels()
    ranks={'github':0,'skills-sh':1,'git':2,'skillhub':3}
    channels.sort(key=lambda c:(0 if config['mode']=='domestic-first' and c['name']=='skillhub' else ranks.get(c['kind'],ranks.get(c['name'],9))+1,-c['priority'],c['name']))
    result,trace=[],[]
    deadline=time.monotonic()+config['budget_seconds']
    network_blocked=False
    while channels:
        channel=channels.pop(0)
        if channel_name and channel['name']!=channel_name: continue
        name=channel['name']
        cache_query=json.dumps([query,channel['kind'],channel['base_url']])
        if not channel['enabled']:
            trace.append({'channel':name,'status':'disabled'});continue
        if channel['kind'] not in ('skills-sh','git','skillhub') and name!='github':
            trace.append({'channel':name,'status':'unsupported','reason':'未接入可靠的公开检索接口'});continue
        cached=m.db.execute('select * from source_search_cache where channel=? and query=? and expires>?',(name,cache_query,time.time())).fetchone()
        if cached:
            found=json.loads(cached['payload']);result.extend(found)
            trace.append({'channel':name,'status':'cached','candidates':len(found)});continue
        health=m.db.execute('select * from source_channel_health where channel=?',(name,)).fetchone()
        if health and health['retry_at']>time.time():
            trace.append({'channel':name,'status':'cooldown','reason':health['reason'],'retry_at':health['retry_at']})
            if health['reason']=='网络不可达': channels.sort(key=lambda c:0 if c['name']=='skillhub' else 1)
            continue
        if time.monotonic()>=deadline:
            trace.append({'channel':name,'status':'deferred','reason':'已到本轮时间预算'});continue
        try:
            found=query_channel(m,channel,query)
            result.extend(found)
            with m.db:
                m.db.execute('insert or replace into source_search_cache values (?,?,?,?)',(name,cache_query,time.time()+(86400 if found else 3600),json.dumps(found)))
                m.db.execute('delete from source_channel_health where channel=?',(name,))
                m.r.log_operation('source-channel-search',name,{'state':'success','count':len(found)},commit=False)
            trace.append({'channel':name,'status':'success' if found else 'empty','candidates':len(found)})
        except Exception as exc:
            code=getattr(exc,'code',None)
            reason='限流或拒绝访问' if code in (403,429) else '网络不可达' if not code and isinstance(exc,(urllib.error.URLError,TimeoutError,OSError)) else '接口不兼容或服务异常'
            wait=600
            try: wait=max(60,min(86400,int(exc.headers.get('Retry-After',600))))
            except (AttributeError,TypeError,ValueError): pass
            try:
                if code in (403,429): wait=max(wait,min(86400,int(exc.headers.get('X-RateLimit-Reset',0))-time.time()))
            except (AttributeError,TypeError,ValueError): pass
            with m.db:
                m.db.execute('insert or replace into source_channel_health values (?,?,?)',(name,time.time()+wait,reason))
                m.r.log_operation('source-channel-search',name,{'state':'failed','reason':reason},commit=False)
            trace.append({'channel':name,'status':'failed','reason':reason,'retry_at':time.time()+wait})
            if reason=='网络不可达' and not network_blocked:
                # Promote domestic services for this attempt, without changing saved preference.
                channels.sort(key=lambda c:0 if c['name']=='skillhub' else 1)
                network_blocked=True
    unique={}
    for candidate in result:
        src=candidate.get('source')
        if src: unique.setdefault((src['url'],src.get('ref',''),src.get('path','')),candidate)
    return {'candidates':list(unique.values()),'trace':trace,'policy':config,'confirmed':False,
            'errors':[{'channel':t['channel'],'error':t['reason']} for t in trace if t['status']=='failed']}


def save_candidates(m, path, candidates, trace):
    retry=[t.get('retry_at',time.time()+60) for t in trace if t['status'] in ('failed','cooldown','deferred')]
    next_attempt=min(retry) if retry else time.time()+86400
    with m.db:
        m.db.execute('insert or replace into source_candidates values (?,?,?)',
                     (str(path),time.time(),json.dumps({'candidates':candidates,'trace':trace,'next_attempt':next_attempt})))


def batch(m, limit=10, skip_ids=None, skip_paths=None):
    from pathlib import Path
    limit=max(1,min(int(limit),10))
    done=[];seen=set();remaining=0;skip_ids=set(skip_ids or []);skip_paths=set(skip_paths or [])
    known=getattr(m,'_source_known_paths',set())
    m._source_known_paths=known
    roots=[root for _,root,_ in m.roots()]
    for raw in m.r.all_capabilities():
        row=dict(raw);path=Path(row['path']);real=str(path.resolve())
        if real in seen or row['kind']!='skill' or row['source_type']=='builtin' or row['health']=='not-scanned' or not path.is_dir() or not any(root==path or root in path.parents for root in roots): continue
        seen.add(real)
        if row['id'] in skip_ids or real in skip_paths: continue
        if real in known: continue
        if m.source(row): known.add(real);continue
        prior=m.db.execute('select searched_at,payload from source_candidates where real_path=?',(real,)).fetchone()
        if prior and json.loads(prior['payload']).get('next_attempt',prior['searched_at']+86400)>time.time(): continue
        if len(done)>=limit: remaining+=1;continue
        try:
            found=m.discover_sources(row['id'],remote=True)
            item=found['items'][0]
        except Exception as exc:
            item={'candidates':[],'trace':[{'channel':'local','status':'failed','reason':type(exc).__name__,'retry_at':time.time()+600}]}
            save_candidates(m,real,[],item['trace'])
        done.append({'id':row['id'],'real_path':real,'name':row['name'],'candidates':len(item['candidates']),'trace':item.get('trace',[])})
    return {'items':done,'processed':len(done),'remaining':remaining,'confirmed':0,'note':'仅保存候选，不自动绑定；继续时跳过 24 小时内已检索项。'}
