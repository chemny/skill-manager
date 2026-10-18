"""Local classification, manual locks and reversible batch labels."""
import hashlib
import json
import re
from pathlib import Path

VERSION='local-rules-3'
CATEGORIES={
 '内容创作':'写作 文案 文章 口播 脚本 创作 课程 小红书 公众号 writer writing copywriting article blog newsletter storytelling course content-writing',
 '设计与多媒体':'图片 视觉 海报 插画 漫画 音频 视频 剪辑 封面 设计 image video audio ffmpeg illustrator infographic poster banner canvas figma design slides deck remotion animation 3d',
 '编程开发':'编程 代码 测试 开发 架构 code coding programming debugging debug testing test-driven frontend backend api database sql react python typescript deployment git github supabase vercel software',
 '数据分析':'数据 分析 报表 指标 回测 量化 股票 股市 金融 data-analytics analytics dataset statistics stock backtest quantitative kpi spreadsheet pandas visualization finance',
 '信息研究':'搜索 检索 研究 调研 论文 新闻 资料 search research academic paper news intelligence investigate discovery web-search',
 '运营与商业':'营销 商业 增长 电商 市场 竞品 定价 用户 转化 运营 推广 marketing commerce ecommerce amazon etsy seo growth customer acquisition pricing business gtm sales product-strategy',
 '办公效率':'办公 文档 知识 笔记 记忆 学习 邮件 会议 日历 document docx pdf pptx excel office notion obsidian knowledge memory learning email calendar meeting',
 '工具与自动化':'自动化 工作流 系统 浏览器 安装 发布 同步 备份 安全 密码 automation workflow browser install installer publisher publishing sync backup security password cli terminal system scheduling cron',
}
TAGS={
 '写作':'写作 文案 文章 writer writing article copywriting blog author editorial prose newsletter', '图片':'图片 海报 插画 image poster illustration illustrator infographic banner comic cover',
 '音视频':'音频 视频 剪辑 audio video ffmpeg remotion transcript', '代码':'编程 代码 开发 coding code programming debug testing frontend backend api sql git',
 '搜索':'搜索 检索 调研 search research discovery', '数据':'数据 分析 指标 analytics dataset data-analysis statistics kpi',
 '文档':'文档 办公 document docx pdf pptx spreadsheet slides deck markdown excel', '自动化':'自动化 工作流 automation workflow cron browser scheduler scheduling',
 '发布':'发布 推送 publishing publisher publish upload', '知识管理':'知识 笔记 记忆 knowledge memory obsidian notion archive chronicle',
 '营销':'营销 增长 电商 marketing ecommerce commerce seo gtm amazon etsy customer sales', '金融':'金融 股票 股市 回测 finance stock backtest',
 '设计':'设计 视觉 design figma layout diagram comic art visual', '安全':'安全 密码 security password 1password credentials',
}

PURPOSE_PATTERNS=[
 ('办公效率',r'(?:to-vba|image-to-ppt|markdown-to-html|x-to-markdown)'),
 ('编程开发',r'(?:code|coding|frontend|backend|debug|debugging|testing|test-driven|architecture|supabase|claude-api|gitops|swiftui)'),
 ('运营与商业',r'(?:amazon|etsy|seo|gtm|commerce|ecommerce|acquisition|customer|business|pricing|marketing|product-strategy)'),
 ('数据分析',r'(?:data-analytics|data-analysis|data-report|backtest|stock|finance|kpi|pandas|quantitative)'),
 ('设计与多媒体',r'(?:image|video|audio|ffmpeg|figma|diagram|infographic|poster|banner|comic|canvas|deck|slides|illustrator|illustration|art|remotion|animation|brand-guidelines)'),
 ('内容创作',r'(?:article|writer|writing|content-writing|content-planning|topic-selector|author-style|copywriting|script-writer|course|storytelling)'),
 ('信息研究',r'(?:news|research|search|aminer|arxiv|company-intel)'),
 ('办公效率',r'(?:memory|archive|obsidian|notion|docx|pdf|pptx|learning|knowledge|markdown|summary|email|calendar)'),
 ('工具与自动化',r'(?:publisher|post-to|scheduler|cron|automation|browser|install|installer|skill-manager|backup|sync|1password|10x-chat)'),
]


def initialize(m):
    m.db.executescript('''
create table if not exists classification_state(real_path text primary key, payload text);
create table if not exists classification_batches(id text primary key, created_at text, state text, payload text);
''')
    if not m.db.execute("select 1 from maintenance_settings where key='classification_taxonomy_v1'").fetchone():
        # Extend the existing vocabulary, never replace user edits or remove old names.
        m.db.executemany('insert or ignore into maintenance_taxonomy values (?,?)',
                         [('categories',n) for n in CATEGORIES if n not in ('内容创作','编程开发','信息研究','办公效率','工具与自动化')]+
                         [('tags',n) for n in TAGS if n not in ('写作','图片','音视频','代码','搜索','数据','文档','自动化')])
        m.db.execute("insert into maintenance_settings values ('classification_taxonomy_v1','1')")
    if not m.db.execute("select 1 from maintenance_settings where key='classification_other_v1'").fetchone():
        m.db.execute("insert or ignore into maintenance_taxonomy values ('categories','其他')")
        m.db.execute("insert into maintenance_settings values ('classification_other_v1','1')")


def hits(text, vocabulary):
    matched=[]
    for term in vocabulary.split():
        if (term in text if re.search(r'[^\x00-\x7f]',term) else re.search(r'(?<![a-z0-9])'+re.escape(term)+r'(?![a-z0-9])',text)):
            matched.append(term)
    return matched


def infer(m, row):
    from taxonomy import names
    path=Path(row['path'])/'SKILL.md'
    if path.stat().st_size>1_000_000: raise ValueError('Skill 文档过大，需手动分类')
    body=path.read_text(encoding='utf-8')
    meta=m.a.parse_frontmatter_text(body)
    title=str(meta.get('name') or row['name']).lower().replace('_','-')
    description=str(meta.get('description') or row.get('description') or '').lower()
    # The opening purpose has more weight than unrelated tools/examples later in a Skill.
    intro=body.split('---',2)[-1] if body.startswith('---') else body
    intro='\n'.join(line for line in intro.splitlines()[:24] if not line.startswith('```')).lower()[:3000]
    values=names(m)
    scores=[]
    for category,terms in CATEGORIES.items():
        if category not in values['categories']: continue
        title_hits,desc_hits,intro_hits=hits(title,terms),hits(description,terms),hits(intro,terms)
        score=5*len(title_hits)+3*len(desc_hits)+len(intro_hits)
        scores.append((score,category,list(dict.fromkeys(title_hits+desc_hits+intro_hits))[:8]))
    scores.sort(key=lambda s:s[0],reverse=True)
    best=scores[0] if scores else (0,'',[])
    second=scores[1][0] if len(scores)>1 else 0
    confident=best[0]>=5 and (best[0]-second>=3 or best[0]>=second*1.4)
    for category,pattern in PURPOSE_PATTERNS:
        match=re.search(r'(?<![a-z0-9])'+pattern+r'(?![a-z0-9])',title)
        if match and category in values['categories']:
            best=(max(best[0],5),category,['用途名称: '+match.group(0)]+best[2][:5])
            confident=True
            break
    tags=[]
    for tag,terms in TAGS.items():
        if tag not in values['tags']: continue
        score=5*len(hits(title,terms))+3*len(hits(description or intro[:500],terms))
        if score>=3: tags.append((score,tag))
    tags.sort(key=lambda t:t[0],reverse=True)
    category=best[1] if best[0] else ('其他' if '其他' in values['categories'] else '')
    return {'category':category, 'tags':[tag for _,tag in tags[:3]],
            'confidence':'high' if confident else 'low' if best[0] else 'unmatched',
            'evidence':best[2], 'version':VERSION, 'hash':hashlib.sha256(body.encode()).hexdigest()}


def existing(m, real):
    label=m.db.execute('select category,tags from maintenance_labels where real_path=?',(real,)).fetchone()
    state=m.db.execute('select payload from classification_state where real_path=?',(real,)).fetchone()
    return ({'category':label['category'],'tags':json.loads(label['tags'])} if label else None,
            json.loads(state['payload']) if state else None)


def mark_manual(m, real, category, tags):
    _,state=existing(m,real)
    state=state or {}
    if category is not None: state['manual_category']=True
    if tags is not None: state['manual_tags']=True
    m.db.execute('insert or replace into classification_state values (?,?)',(real,json.dumps(state)))


def classify(m, row, apply=False):
    real=str(Path(row['path']).resolve())
    before,state=existing(m,real)
    inferred=infer(m,row)
    # Older labels without provenance are treated as user-owned, never overwritten.
    manual_category=bool(before and (state.get('manual_category') if state is not None else before['category']))
    manual_tags=bool(before and (state.get('manual_tags') if state is not None else before['tags']))
    after={'category':before['category'] if manual_category else inferred['category'],
           'tags':before['tags'] if manual_tags else inferred['tags']}
    new_state={**inferred,'manual_category':manual_category,'manual_tags':manual_tags}
    result={'real_path':real,'name':row['name'],'before':before,'before_state':state,'after':after,
            'after_state':new_state,'changed':before!=after,'manual_preserved':manual_category or manual_tags}
    if apply:
        m.db.execute('insert or replace into maintenance_labels values (?,?,?)',(real,after['category'],json.dumps(after['tags'],ensure_ascii=False)))
        m.db.execute('insert or replace into classification_state values (?,?)',(real,json.dumps(new_state,ensure_ascii=False)))
    return result


def batch(m, apply=False, field='both'):
    from maintenance import stamp,uid
    if field not in ('both','category','tags'): raise ValueError('不支持的分类操作')
    rows=[];seen=set();errors=[]
    roots=[root for _,root,_ in m.roots()]
    for raw in m.r.all_capabilities():
        row=dict(raw);path=Path(row['path']);real=str(path.resolve())
        if real in seen or row['kind']!='skill' or row['health']=='not-scanned' or not (path/'SKILL.md').is_file() or not any(root==path or root in path.parents for root in roots): continue
        seen.add(real)
        try:
            result=classify(m,row,False)
            if field!='both':
                before=result['before'] or {'category':'','tags':[]}
                after=result['after']
                result['after']={
                    'category':(before['category'] or after['category']) if field=='category' else before['category'],
                    'tags':(before['tags'] or after['tags']) if field=='tags' else before['tags'],
                }
                result['changed']=before!=result['after']
            rows.append(result)
        except (OSError,UnicodeError,ValueError) as exc: errors.append({'name':row['name'],'error':type(exc).__name__})
    bid=uid('classification')
    if apply:
        record_rows=rows if field=='both' else [row for row in rows if row['changed']]
        with m.db:
            if field!='both':
                for row in rows:
                    if row['changed'] or row['after_state']==row['before_state']:
                        continue
                    real=row['real_path'];current,current_state=existing(m,real)
                    if current==row['before'] and current_state==row['before_state']:
                        m.db.execute('insert or replace into classification_state values (?,?)',(real,json.dumps(row['after_state'],ensure_ascii=False)))
            for row in record_rows:
                real=row['real_path'];after=row['after']
                current,current_state=existing(m,real)
                if current!=row['before'] or current_state!=row['before_state']:
                    row['skipped_concurrent']=True
                    row['changed']=False
                    continue
                m.db.execute('insert or replace into maintenance_labels values (?,?,?)',(real,after['category'],json.dumps(after['tags'],ensure_ascii=False)))
                m.db.execute('insert or replace into classification_state values (?,?)',(real,json.dumps(row['after_state'],ensure_ascii=False)))
            m.db.execute('insert into classification_batches values (?,?,?,?)',(bid,stamp(),'applied',json.dumps(record_rows,ensure_ascii=False)))
            m.r.log_operation('classification-batch',bid,{'total':len(rows),'changed':sum(r['changed'] for r in rows)},commit=False)
        m.invalidate_cache()
    return {'id':bid if apply else None,'applied':apply,'total':len(rows),'changed':sum(r['changed'] for r in rows),
            'classified':sum(bool(r['after']['category']) for r in rows),'tagged':sum(bool(r['after']['tags']) for r in rows),
            'review':sum(not r['after']['category'] for r in rows),'manual_preserved':sum(r['manual_preserved'] for r in rows),
            'categories':{name:sum(r['after']['category']==name for r in rows) for name in CATEGORIES},'errors':errors}


def undo(m, bid):
    rec=m.db.execute('select * from classification_batches where id=?',(bid,)).fetchone()
    if not rec or rec['state']!='applied': raise ValueError('该批次不存在或已撤回')
    restored=skipped=0
    with m.db:
        for row in json.loads(rec['payload']):
            if row.get('skipped_concurrent'): skipped+=1;continue
            real=row['real_path'];labels,state=existing(m,real)
            if labels!=row['after'] or state!=row['after_state']: skipped+=1;continue
            if row['before'] is None: m.db.execute('delete from maintenance_labels where real_path=?',(real,))
            else: m.db.execute('insert or replace into maintenance_labels values (?,?,?)',(real,row['before']['category'],json.dumps(row['before']['tags'])))
            if row['before_state'] is None: m.db.execute('delete from classification_state where real_path=?',(real,))
            else: m.db.execute('insert or replace into classification_state values (?,?)',(real,json.dumps(row['before_state'])))
            restored+=1
        m.db.execute("update classification_batches set state='undone' where id=?",(bid,))
        m.r.log_operation('classification-undo',bid,{'restored':restored,'skipped':skipped},commit=False)
    m.invalidate_cache()
    return {'restored':restored,'skipped':skipped}


def summary(m):
    rec=m.db.execute('select id,created_at,state from classification_batches order by rowid desc limit 1').fetchone()
    return {'last_batch':dict(rec) if rec else None,'version':VERSION}
