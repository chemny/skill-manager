"""Local recovery catalog. Discovery never moves files or assumes original paths."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import maintenance as core


def flags(m):
    try:
        return {r['id']:bool(r['retained']) for r in m.db.execute('select * from maintenance_backup_flags')}
    except Exception as exc:
        import sqlite3
        if isinstance(exc,sqlite3.OperationalError) and 'no such table' in str(exc): return {}
        raise


def retain(m, bid, retained):
    if not isinstance(retained,bool): raise ValueError('retained must be boolean')
    with m.lock():
        m.backup_record(bid)
        m.db.execute('insert into maintenance_backup_flags values (?,?) on conflict(id) do update set retained=excluded.retained',(bid,int(retained)))
        m.db.commit()
        m.r.log_operation('backup-retain',bid,{'retained':retained})
    return {'id':bid,'retained':retained}


def candidates(m):
    try: rows=m.db.execute('select payload,state from maintenance_recovery_candidates order by rowid desc').fetchall()
    except Exception as exc:
        import sqlite3
        if isinstance(exc,sqlite3.OperationalError) and 'no such table' in str(exc): return []
        raise
    return [{**json.loads(r['payload']),'state':r['state']} for r in rows]


def locations(m, extra=None):
    home=Path.home()
    values=[home/'.agents/skill-backups',home/'.agents/skills/.trash',home/'.agents/skills/_backups',home/'.agents/skills/.backups',home/'.agents/skills/.install-backups',m.home/'backups']
    for _,root,_ in m.roots():
        values.extend(root/n for n in ('.trash','_backups','.backups','.install-backups','.cmm-skill-backups','.frontmatter-fix-backups','.route-governor-backups'))
    if extra:
        p=core.absolute(extra)
        if not p.is_dir() or p.is_symlink(): raise ValueError('Choose an existing directory, not a symbolic link')
        values.append(p)
    return list(dict.fromkeys(p.resolve() for p in values if p.is_dir()))


def discover(m, extra=None):
    known={Path(r['path']).resolve() for r in m.db.execute('select path from maintenance_backups')}
    found,errors=[],[]
    def visit(p, depth, area):
        if p.is_symlink() or p.resolve() in known: return
        if depth>5: return
        if len(found)>=1000: return
        try:
            skill=p.is_dir() and (p/'SKILL.md').is_file() and not (p/'SKILL.md').is_symlink()
            archive=p.is_file() and any(p.name.lower().endswith(s) for s in ('.zip','.tar','.tar.gz','.tgz'))
            if skill or archive:
                bid='historical-'+hashlib.sha256(str(p).encode()).hexdigest()[:20]
                entry={'id':bid,'path':str(p),'name':p.name,'suggested_type':'quarantine' if area.name=='.trash' else 'backup','format':'directory' if skill else 'archive','discovered_at':core.stamp(),'size':core.size(p),'basis':'在删除暂存目录发现；原位置需要确认' if area.name=='.trash' else '在备份目录发现；原位置需要确认'}
                if skill:
                    checked=core.manifest(p)
                    entry['fingerprint']=core.fingerprint(checked)
                    entry['restorable']=not any(v['kind']=='link' for v in checked.values())
                found.append(entry)
                return
            if p.is_dir():
                for child in sorted(p.iterdir()):
                    if child.name in ('.git','node_modules','__pycache__','.DS_Store'):continue
                    visit(child,depth+1,area)
        except (OSError,ValueError): errors.append({'path':str(p),'message':'无法读取，未收录'})
    with m.lock():
        roots=locations(m,extra)
        for root in roots: visit(root,0,root)
        for entry in found:
            m.db.execute("insert into maintenance_recovery_candidates values (?,?,'pending') on conflict(id) do update set payload=excluded.payload",(entry['id'],json.dumps(entry,ensure_ascii=False)))
        m.db.commit()
    return {'candidates':candidates(m),'locations':[str(p) for p in roots],'errors':errors,'limit':1000,'checked_at':core.stamp()}


def ignore(m, cid):
    with m.lock():
        if not m.db.execute('select 1 from maintenance_recovery_candidates where id=?',(cid,)).fetchone():raise ValueError('Historical candidate not found')
        m.db.execute("update maintenance_recovery_candidates set state='ignored' where id=? and state='pending'",(cid,));m.db.commit()
    return {'ignored':cid}


def import_candidate(m, cid, original, kind, confirmed=False):
    if confirmed is not True:raise ValueError('Confirmation required')
    if kind not in ('backup','quarantine'):raise ValueError('Choose backup or quarantine')
    with m.lock():
        raw=m.db.execute("select payload from maintenance_recovery_candidates where id=? and state='pending'",(cid,)).fetchone()
        if not raw:raise ValueError('Pending candidate not found')
        item=json.loads(raw['payload']);source=Path(item['path'])
        if item['format']!='directory':raise ValueError('Archive import is not supported; preserve it for manual review')
        if source.is_symlink() or not source.is_dir() or any(p.is_symlink() for p in source.parents):raise ValueError('Source path changed or is a symlink')
        before=core.manifest(source)
        if not (source/'SKILL.md').is_file() or any(e['kind']=='link' for e in before.values()):raise ValueError('Historical directories with links require manual review')
        if core.fingerprint(before)!=item.get('fingerprint'):raise ValueError('Historical files changed; discover again')
        if not isinstance(original,str) or not original.strip() or not Path(original).expanduser().is_absolute():raise ValueError('Confirm an absolute original Skill path')
        target=m.validate_target(original)
        if any((p.startswith('.') or p in ('_backups','backups','node_modules')) for p in target.relative_to(max((r for _,r,_ in m.roots() if r in target.parents),key=lambda p:len(p.parts))).parts):raise ValueError('Choose a Skill path outside hidden backup directories')
        if target.resolve()==source.resolve() or source.resolve() in target.resolve().parents or target.resolve() in source.resolve().parents:raise ValueError('Recovery target must be separate from historical storage')
        row=dict(m.db.execute('select * from capabilities where path=?',(str(target),)).fetchone() or {})
        if m.a.is_builtin_path(target):raise ValueError('Plugin/builtin directory is observe-only')
        bid=core.uid('backup');container=m.home/('quarantine' if kind=='quarantine' else 'backups')/bid
        container.mkdir(mode=0o700)
        meta={'id':bid,'created_at':core.stamp(),'original_path':str(target),'item_id':row.get('id') or m.a.stable_id('shared','skill',target,target.name),'platform':row.get('platform') or 'shared','name':row.get('name') or target.name,'type':kind,'reason':'historical','historical_path':str(source),'original_date_unknown':True,'manifest':before,'size':core.size(source),'baseline':None}
        try:
            core.copy_path(source,container/'content')
            if core.manifest(container/'content')!=before or core.manifest(source)!=before:raise ValueError('Historical copy verification failed')
            core.write_json(container/'manifest.json',meta)
            m.db.execute("insert into maintenance_backups values (?,?,?,?,'available')",(bid,meta['created_at'],str(container),json.dumps(meta,ensure_ascii=False)))
            m.db.execute("update maintenance_recovery_candidates set state='imported' where id=?",(cid,));m.db.commit()
        except Exception:
            m.db.rollback();core.remove_path(container);raise
        m.invalidate_cache();m.r.log_operation('backup-import',bid,{'source':str(source),'original_path':str(target),'type':kind})
        return {'id':bid,'verified':True,'type':kind,'source_preserved':True}


def preview_purge(m, ids, operation='purge'):
    if operation not in ('purge','trash-backup','restore-backup'):raise ValueError('Unsupported backup operation')
    if not isinstance(ids,list) or not 1<=len(ids)<=10000 or any(not isinstance(i,str) for i in ids):raise ValueError('Select 1 to 10000 backups')
    plans=[]
    try:
        for bid in dict.fromkeys(ids):plans.append(m.plan(operation,backup_id=bid))
    except Exception:
        for p in plans:m.discard_plan(p['id'])
        raise
    return {'plans':plans,'bytes':sum(p['size']['allocated_bytes'] for p in plans),'irreversible':operation=='purge'}


def apply_purge(m, ids, confirmed=False):
    if confirmed is not True:raise ValueError('Confirmation required')
    if not isinstance(ids,list) or not 1<=len(ids)<=10000 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):raise ValueError('Invalid cleanup plans')
    # Review all plans before the first delete, then each apply rechecks under lock.
    for pid in ids:
        p=m.get_plan(pid)
        if p.get('action') not in ('purge','trash-backup','restore-backup') or p.get('state')!='preview' or p.get('expired'):raise ValueError('Only backup management plans can be batched')
        if p['action']!='restore-backup' and flags(m).get(p['backup_id']):raise ValueError('Backup marked to retain; cancel that mark before deleting')
    results=[]
    for pid in ids:
        try:results.append(m.apply(pid))
        except (ValueError,OSError) as exc:return {'state':'partial','completed':len(results),'total':len(ids),'error':core.redact(str(exc)),'results':results,'remaining':ids[len(results):]}
    return {'state':'succeeded','completed':len(results),'total':len(ids),'results':results}


def historical_record(m, cid):
    """External entries stay in place; use only persisted discovery paths."""
    entry=next((c for c in candidates(m) if c['id']==cid and c['state']=='pending'),None)
    if not entry:raise ValueError('Historical backup is no longer available')
    p=Path(entry['path'])
    if p.is_symlink() or any(parent.is_symlink() for parent in p.parents):raise ValueError('Historical path changed; refresh backup discovery')
    if not core.exists(p):raise ValueError('Historical backup is missing; refresh backup discovery')
    return p,{'external':True,'name':entry['name'],'original_path':None,'item_id':None,'platform':'shared','baseline':None,'format':entry['format'],'fingerprint':entry.get('fingerprint'),'type':entry['suggested_type']}


def historical_rows(m, ids=None):
    retained=flags(m)
    result=[]
    for c in candidates(m):
        if ids is not None and c['id'] not in ids: continue
        if c['state']!='pending':continue
        p=Path(c['path'])
        available=p.exists() and not p.is_symlink()
        supported=c['format']=='directory' and available and (p/'SKILL.md').is_file() and c.get('restorable',True)
        result.append({'id':c['id'],'name':c['name'],'type':c['suggested_type'],'state':'available' if available else 'missing','external':True,'original_path':None,'path':str(p),'historical_path':str(p),'reason':'historical','created_at':None,'discovered_at':c['discovered_at'],'size':c['size'] if available else {'allocated_bytes':0,'bytes':0},'retained':retained.get(c['id'],False),'restore_status':'needs_target' if supported else 'unsupported','status_detail':'恢复时选择安装位置；文件将在预览时重新校验' if supported else '压缩包或文件不可用，暂不支持恢复','format':c['format'],'affected_references':[],'verified':None,'target_exists':False})
    return result


def stored_space(m):
    """Last recorded content sizes; never traverse backup trees for counters."""
    totals = {key:{'bytes':0, 'allocated_bytes':0, 'basis':'recorded-estimate'} for key in ('backups', 'quarantine')}
    for row in m.db.execute("select b.payload,t.trashed_at from maintenance_backups b left join maintenance_backup_trash t on b.id=t.id where b.state='available'"):
        meta = json.loads(row['payload'])
        amount = meta.get('size')
        if not isinstance(amount, dict):
            logical = sum(f.get('bytes', 0) for f in meta.get('manifest', {}).values())
            amount = {'bytes':logical, 'allocated_bytes':logical}
        key = 'quarantine' if row['trashed_at'] or meta.get('type')=='quarantine' else 'backups'
        for field in ('bytes', 'allocated_bytes'): totals[key][field] += amount.get(field, 0)
    for candidate in candidates(m):
        if candidate['state'] != 'pending': continue
        key = 'quarantine' if candidate.get('suggested_type')=='quarantine' else 'backups'
        for field in ('bytes', 'allocated_bytes'): totals[key][field] += candidate.get('size', {}).get(field, 0)
    return totals


def backup_date(path, recorded=None):
    """Filename dates are local wall-time clues, never assign an invented timezone."""
    import re
    if recorded:
        return {'value':recorded,'kind':'recorded','basis':'管理器记录的保存时间'}
    for part in reversed(Path(path).parts[-3:]):
        for hit in re.finditer(r'(?<!\d)(20\d{2})-?(\d{2})-?(\d{2})(?:[-_](\d{2})(\d{2})(\d{2}))?(?!\d)',part):
            try:
                y,mo,d,hh,mm,ss=hit.groups()
                date=dt.datetime(int(y),int(mo),int(d),int(hh or 0),int(mm or 0),int(ss or 0))
            except ValueError:
                continue
            return {'value':date.isoformat(timespec='seconds') if hh else date.date().isoformat(),'kind':'inferred','basis':'来自名称：'+part+'；时区未知，可能是批次时间'}
    return {'value':None,'kind':'unknown','basis':'没有可靠保存时间；文件修改时间不能代替备份时间'}


def skill_info(m, path):
    path=Path(path)
    # Do not follow a historical Skill directory or SKILL.md symlink to read content.
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or (path/'SKILL.md').is_symlink(): return {}
    try:
        f=path/'SKILL.md'
        if not f.is_file() or f.stat().st_size>512*1024: return {}
        info=m.a.parse_frontmatter(f)
        nested=info.get('metadata')
        if not info.get('version') and isinstance(nested,dict) and nested.get('version'):info['version']=nested['version']
        if info.get('github_hash'):info['commit']=str(info['github_hash'])[:12]
        return {k:str(info[k]) for k in ('name','description','version','commit') if info.get(k) not in (None,'','unknown')}
    except (OSError,ValueError): return {}


def enrich_backup(m, row):
    from backup_recycle import entry
    recycled = entry(m, row['id'])
    if recycled and row.get('state') != 'purged':
        row.update(type='quarantine', trashed_backup=True, trashed_at=recycled,
                   restore_status='ready', status_detail='恢复后回到备份列表，不改动已安装 Skills')
    p=Path(row.get('historical_path') or row['path'])
    row['saved_time']=backup_date(p,None if row.get('external') or row.get('original_date_unknown') else row.get('created_at'))
    content=p if row.get('external') else Path(row['path'])/'content'
    row['skill_info']={} if row.get('bundle') else skill_info(m,content)
    if row.get('snapshot_at'):
        row['saved_time']=backup_date(p,row['snapshot_at'])
        row['saved_time']['basis']='备份包清单记录的创建时间'
    return row


def backup_advice(m, ids):
    """Bounded local analysis for visible rows; no cloud queries or file changes."""
    from backup_policy import advice
    if not isinstance(ids,list) or not 1<=len(ids)<=8 or any(not isinstance(b,str) or not b for b in ids):
        raise ValueError('Select 1 to 8 backup IDs')
    rows={b['id']:b for b in m.backups(verify=False)['backups']}
    m._advice_rows=list(rows.values())
    result=[]
    for bid in dict.fromkeys(ids):
        row=rows.get(bid)
        try:
            if row is None:raise ValueError('Backup not found')
            if row.get('retained'):decision=advice('keep','已标记防止误删')
            elif row['state']!='available':decision=advice('review','备份文件不可用，暂不建议删除')
            elif row.get('bundle'):decision=advice('review','整体备份需逐项核实，暂不建议删除')
            elif row.get('type')=='quarantine':decision=advice('review','回收内容仍可恢复，请确认不再需要后再删除')
            else:
                decision=backup_detail(m,bid,row)['cleanup_advice']
        except (ValueError,OSError,KeyError,TypeError):
            decision=advice('review','分析未完成或文件已变化，暂不建议删除')
        result.append({'id':bid,'cleanup_advice':decision})
    del m._advice_rows
    return {'items':result,'checked_at':core.stamp()}


def backup_detail(m, bid, row=None):
    """Read-only local comparison; same-name candidates are not restore destinations."""
    if row is None:row=next((b for b in m.backups(verify=False)['backups'] if b['id']==bid),None)
    if not row: raise ValueError('Backup not found')
    from backup_policy import assess, advice, lookup_names
    if row['state']!='available': return {**row,'comparison_state':'unavailable','comparisons':[], 'cleanup_advice':advice('review','备份文件不可用，请先核实')}
    container,meta=m.backup_record(bid)
    if meta.get('bundle'):
        from snapshots import read_package,package_path
        data=read_package(package_path(m,bid))
        return {**row,'snapshot':data,'valid':True,'comparison_state':'bundle','comparisons':[],'checked_at':core.stamp(),'cleanup_advice':advice('review','整体备份需逐项核实，不能仅凭日期清理')}
    source=container if meta.get('external') else container/'content'
    if row.get('format')=='archive':
        return {**row,'comparison_state':'unsupported','comparisons':[],'files':[],'note':'压缩包未解压，不能自动比较或恢复。','cleanup_advice':advice('review','压缩包内容未经核实，暂不建议删除')}
    before=core.manifest(source)
    safe=not any(v['kind']=='link' for v in before.values())
    checks=m.verify_backup(bid)
    names=lookup_names(row)
    matches={}
    roots=m.roots()
    historical_paths={Path(c['path']).resolve() for c in candidates(m) if c['state']=='pending'}
    original=row.get('original_path')
    raw_rows=[dict(raw) for raw in m.db.execute("select * from capabilities where kind='skill'")]
    latest_reads=m.library_last_reads(raw_rows)
    for raw in raw_rows:
        r=dict(raw);p=Path(r['path'])
        exact=original and str(p)==original
        if not exact and (original or r['name'] not in names and p.name not in names):continue
        eligible=[root for platform,root,status in roots if platform==r['platform'] and status=='active' and root in p.parents]
        if r.get('health')=='not-scanned' or r.get('status')!='active' or not eligible:continue
        root=max(eligible,key=lambda x:len(x.parts))
        if any('backup' in part.lower() or part=='.trash' for part in p.relative_to(root).parts[:-1]):continue
        real=p.resolve()
        if any(real==p or p in real.parents for p in historical_paths):continue
        if not p.exists():continue
        real=p.resolve()
        if real==source.resolve() or source.resolve() in real.parents:continue
        if real not in matches:matches[real]={'path':str(real),'agents':[],'basis':'original' if exact else 'name','last_recorded_at':latest_reads.get(str(real))}
        if r['platform']!='shared' and r['platform'] not in matches[real]['agents']:matches[real]['agents'].append(r['platform'])
    comparisons=[]
    for p,c in matches.items():
        try:
            now=core.manifest(p)
            changes=core.diff(now,before) # describes replacing current content with backup
            c.update(state='identical' if core.content(now)==core.content(before) else 'different',changes=changes,version=skill_info(m,p).get('version'),files=sum(v['kind']=='file' for v in now.values()))
        except (OSError,ValueError):c.update(state='unavailable',changes=None)
        comparisons.append(c)
    state='not_installed' if not comparisons else comparisons[0]['state'] if len(comparisons)==1 else 'multiple'
    if not checks['valid'] or not safe:state='unavailable'
    files=[{'path':k,'bytes':v.get('bytes',0),'kind':v['kind']} for k,v in before.items() if k!='.']
    version_state='not_installed' if state=='not_installed' else 'same' if state=='identical' else 'unknown'
    if len(comparisons)==1 and comparisons[0].get('basis')=='original' and state=='different':
        import re
        def version_tuple(value):
            match=re.fullmatch(r'v?(\d+(?:\.\d+){0,3})',str(value or ''))
            return tuple(int(n) for n in match[1].split('.')) if match else None
        saved=version_tuple(row.get('skill_info',{}).get('version'));current=version_tuple(comparisons[0].get('version'))
        if saved and current:
            saved=saved+(0,)*(4-len(saved));current=current+(0,)*(4-len(current))
            version_state='older' if saved<current else 'newer' if saved>current else 'different'
    cleanup=assess(m,row,meta,before,comparisons,checks['valid'] and safe)
    return {**row,'cleanup_advice':cleanup,'version_state':version_state,'comparison_state':state,'comparisons':comparisons,'valid':checks['valid'],'files':files[:500],'file_count':len(files),'files_truncated':len(files)>500,'checked_at':core.stamp(),'note':'同名匹配仅供比较，不证明来源相同，也不会自动设置恢复位置。' if not original else '对比原安装位置；恢复前会重新校验。'}
