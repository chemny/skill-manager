"""Local category/tag names and references. Never edits upstream Skill files."""
import json
import sqlite3


def initialize(m):
    from library_metadata import CATEGORIES, TAGS
    m.db.execute('create table if not exists maintenance_taxonomy(kind text, name text, primary key(kind,name))')
    m.db.execute('create table if not exists maintenance_settings(key text primary key, value text)')
    if not m.db.execute("select 1 from maintenance_settings where key='taxonomy_initialized'").fetchone():
        m.db.executemany('insert or ignore into maintenance_taxonomy values (?,?)',
                         [('categories', n) for n in CATEGORIES] + [('tags', n) for n in TAGS])
        m.db.execute("insert into maintenance_settings values ('taxonomy_initialized','1')")


def names(m):
    from library_metadata import CATEGORIES, TAGS
    try:
        rows = m.db.execute('select kind,name from maintenance_taxonomy order by rowid').fetchall()
        return {kind:[r['name'] for r in rows if r['kind']==kind] for kind in ('categories','tags')}
    except sqlite3.OperationalError:
        return {'categories':list(CATEGORIES), 'tags':list(TAGS)}


def overview(m):
    values = names(m)
    counts = {kind:dict.fromkeys(values[kind], 0) for kind in values}
    for row in m.db.execute('select category,tags from maintenance_labels'):
        if row['category'] in counts['categories']:
            counts['categories'][row['category']] += 1
        for tag in json.loads(row['tags']):
            if tag in counts['tags']: counts['tags'][tag] += 1
    return {kind:[{'name':n, 'count':counts[kind][n]} for n in values[kind]] for kind in values}


def update(m, payload):
    kind, action = payload.get('kind'), payload.get('operation')
    name, new = str(payload.get('name') or '').strip(), str(payload.get('new_name') or '').strip()
    if kind not in ('categories','tags') or action not in ('add','rename','delete'):
        raise ValueError('无效的分类或标签操作')
    if not name or len(name)>40 or any(ord(c)<32 for c in name) or (action=='rename' and (not new or len(new)>40 or any(ord(c)<32 for c in new))):
        raise ValueError('名称须为 1 至 40 个字符')
    current = names(m)[kind]
    if kind=='categories' and action in ('add','rename') and (new if action=='rename' else name) in ('keep','none'):
        raise ValueError('此名称为系统保留值，请使用其他名称')
    if action=='add' and name in current: raise ValueError('名称已存在')
    if action!='add' and name not in current: raise ValueError('项目已变化，请刷新')
    if action=='rename' and new in current: raise ValueError('名称已存在')
    if action=='add' and len(current)>=100: raise ValueError('分类或标签最多 100 个')
    with m.db:
        if action in ('rename','delete'):
            from classification import mark_manual
            for row in m.db.execute('select real_path,category,tags from maintenance_labels').fetchall():
                if (kind=='categories' and row['category']==name) or (kind=='tags' and name in json.loads(row['tags'])):
                    mark_manual(m,row['real_path'],name if kind=='categories' else None,[] if kind=='tags' else None)
        if action=='add': m.db.execute('insert into maintenance_taxonomy values (?,?)',(kind,name))
        else:
            if action=='rename': m.db.execute('update maintenance_taxonomy set name=? where kind=? and name=?',(new,kind,name))
            else: m.db.execute('delete from maintenance_taxonomy where kind=? and name=?',(kind,name))
            if kind=='categories':
                m.db.execute('update maintenance_labels set category=? where category=?',(new if action=='rename' else '',name))
            else:
                for row in m.db.execute('select real_path,tags from maintenance_labels').fetchall():
                    tags=json.loads(row['tags'])
                    if name not in tags: continue
                    tags=[new if t==name else t for t in tags] if action=='rename' else [t for t in tags if t!=name]
                    m.db.execute('update maintenance_labels set tags=? where real_path=?',(json.dumps(tags,ensure_ascii=False),row['real_path']))
        m.db.execute('update maintenance_inventory_cache set generation=generation+1 where id=1')
        m.r.log_operation('taxonomy-'+action, name, {'kind':kind,'new_name':new}, commit=False)
    return overview(m)
