"""Portable, checksummed Skill snapshots. Never package Agent credentials/config."""
import datetime as dt
import json
import os
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
import maintenance as c

ACTIONS={'create-snapshot','import-snapshot','export-backup','snapshot-detail','preview-restore-many','apply-restore-many','preview-empty-trash'}
FORMAT='skill-manager-snapshot-v1'
MAX_BYTES=2*1024**3

def zip_members(z):
    entries=z.infolist()
    if len(entries)>100000 or sum(x.file_size for x in entries)>MAX_BYTES:raise ValueError('备份包过大')
    seen=set()
    for i in entries:
        p=PurePosixPath(i.filename)
        if not i.filename or p.is_absolute() or '..' in p.parts or '\\' in i.filename or ':' in i.filename or i.filename in seen:raise ValueError('Unsafe archive path')
        seen.add(i.filename)
        mode=i.external_attr>>16
        if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR)) or i.flag_bits&1:raise ValueError('Unsupported archive entry')
    return entries

def extract_zip(source,target):
    with zipfile.ZipFile(source) as z:
        zip_members(z);target.mkdir(parents=True)
        z.extractall(target)

def _read_package(path, verify=True):
    with zipfile.ZipFile(path) as z:
        entries=zip_members(z)
        if z.getinfo('snapshot.json').file_size>16*1024**2:raise ValueError('备份清单过大')
        data=json.loads(z.read('snapshot.json'))
        if data.get('format')!=FORMAT or not isinstance(data.get('items'),list) or not data['items']:raise ValueError('不是 Skill Manager 备份包')
        try:
            created=dt.datetime.fromisoformat(data['created_at'].replace('Z','+00:00'))
            if not created.tzinfo:raise ValueError()
        except (ValueError,AttributeError):raise ValueError('备份包缺少有效创建时间')
        allowed={'snapshot.json'};keys=set()
        for item in data['items']:
            key=item['key'];c.safe_relative(key)
            if '/' in key or key in ('','.'):raise ValueError('Unsafe snapshot key')
            if key in keys:raise ValueError('Duplicate snapshot key')
            keys.add(key)
            if not isinstance(item.get('name'),str) or not item['name']:raise ValueError('Missing Skill name')
            for rel,meta in item['manifest'].items():
                c.safe_relative(rel)
                if rel=='.' and meta['kind']=='dir':continue
                if meta['kind']=='dir':continue
                if meta['kind']!='file':raise ValueError('Snapshot links are not supported')
                name='skills/'+key+'/'+rel;allowed.add(name)
                if z.getinfo(name).file_size!=meta['bytes']:raise ValueError('Snapshot size mismatch')
                if verify:
                    import hashlib
                    h=hashlib.sha256()
                    with z.open(name) as f:
                        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
                    if h.hexdigest()!=meta['sha256']:raise ValueError('Snapshot checksum mismatch')
            if 'SKILL.md' not in item['manifest']:raise ValueError('Snapshot Skill is missing SKILL.md')
        if any(i.filename not in allowed and not i.is_dir() for i in entries):raise ValueError('Unlisted snapshot content')
        return data

def read_package(path,verify=True):
    try:return _read_package(path,verify)
    except (KeyError,TypeError,AttributeError,zipfile.BadZipFile,UnicodeError) as exc:raise ValueError('备份包清单无效或文件不完整') from exc

def items_for(m,scope,ids=None,agent=None):
    rows=m.inventory()['items']
    if scope=='selected':
        if not isinstance(ids,list) or not ids:raise ValueError('请选择 Skills')
        requested=set(ids);rows=[r for r in rows if r['id'] in requested]
        if {r['id'] for r in rows}!=requested:raise ValueError('所选 Skills 已变化，请刷新')
    elif scope=='agent':
        rows=[r for r in rows if agent in r.get('known_agents',[]) or r.get('platform')==agent]
    elif scope!='all':raise ValueError('Invalid backup scope')
    grouped={}
    for row in rows:
        path=Path(row['path']).resolve()
        if not (path/'SKILL.md').is_file():continue
        entry=grouped.setdefault(str(path),{'row':row,'agents':set(),'references':[]})
        entry['agents'].update(row.get('known_agents',[]));entry['references'].append({'platform':row['platform'],'path':row['path']})
    if not grouped:raise ValueError('没有可备份的 Skills')
    return grouped

def write_package(m,dest,grouped,scope):
    if hasattr(m,'batch_progress'):m.batch_progress(0,len(grouped),'正在打包备份')
    data={'format':FORMAT,'created_at':c.stamp(),'scope':scope,'items':[]}
    with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED) as z:
        for n,(raw,entry) in enumerate(grouped.items()):
            root=Path(raw);row=entry['row'];files=c.manifest(root)
            if any(v['kind']=='link' for v in files.values()):raise ValueError('Skill 包含内部链接，无法安全打包：'+row['name'])
            key='skill-'+str(n+1)
            item={'key':key,'name':row['name'],'folder':row.get('folder') or root.name,'version':row.get('version'),
                  'agents':sorted(entry['agents']),'references':entry['references'],'source':row.get('source'),
                  'labels':row.get('classification',{}),'manifest':files}
            data['items'].append(item)
            for rel,v in files.items():
                if v['kind']=='file':z.write(root/rel,'skills/'+key+'/'+rel)
            if c.manifest(root)!=files:raise ValueError('备份时源文件发生变化：'+row['name'])
            if hasattr(m,'batch_progress'):m.batch_progress(n+1,len(grouped),'正在打包备份')
        z.writestr('snapshot.json',json.dumps(data,ensure_ascii=False))
    if hasattr(m,'batch_progress'):m.batch_progress(len(grouped),len(grouped),'正在校验备份包')
    read_package(dest)
    return data

def register(m,source,data,reason):
    bid=c.uid('backup');container=m.home/'backups'/bid;container.mkdir(mode=0o700)
    try:
        (container/'content').mkdir();shutil.copyfile(source,container/'content'/'snapshot.zip');os.chmod(container/'content'/'snapshot.zip',0o600)
        read_package(container/'content'/'snapshot.zip')
        meta={'id':bid,'created_at':c.stamp(),'original_path':None,'type':'backup','bundle':True,
              'name':({'all':'全部 Skills','codex':'Codex Skills','claude_code':'Claude Code Skills','hermes':'Hermes Skills','openclaw':'OpenClaw Skills'}.get(data.get('scope'),'Skills 备份'))+' · '+str(len(data['items']))+' 项',
              'reason':reason,'scope':data.get('scope'),'snapshot_at':data['created_at'],'entry_count':len(data['items']),
              'manifest':c.manifest(container/'content'), 'size':c.size(container/'content')}
        c.write_json(container/'manifest.json',meta)
        m.db.execute('insert into maintenance_backups values (?,?,?,?,?)',(bid,meta['created_at'],str(container),json.dumps(meta),'available'));m.db.commit()
        result={'id':bid,'name':meta['name'],'count':len(data['items'])}
        oid=c.uid('op');m.operation(oid,'snapshot','succeeded',{'id':oid,'action':'snapshot','reason':reason,'backups':[result],'target':meta['name']});m.r.log_operation('snapshot',bid,result)
        return result
    except Exception:
        shutil.rmtree(container);raise

def package_path(m,bid):
    container,meta=m.backup_record(bid)
    if not meta.get('bundle') or not m.verify_backup(bid)['valid']:raise ValueError('备份包不可用或校验失败')
    return container/'content'/'snapshot.zip'

def stage_item(m,bid,key,destination):
    source=package_path(m,bid);data=read_package(source)
    item=next((i for i in data['items'] if i['key']==key),None)
    if not item:raise ValueError('备份中不存在此 Skill')
    destination.mkdir()
    with zipfile.ZipFile(source) as z:
        for rel,meta in item['manifest'].items():
            c.safe_relative(rel)
            target=destination/rel
            if meta['kind']=='dir':target.mkdir(parents=True,exist_ok=True)
            elif meta['kind']=='file':
                target.parent.mkdir(parents=True,exist_ok=True)
                with z.open('skills/'+key+'/'+rel) as src,target.open('wb') as dst:shutil.copyfileobj(src,dst)
                os.chmod(target,meta.get('mode',0o600)&0o777)
        # Set directory modes after their children have been written.
        for rel,meta in sorted(item['manifest'].items(),key=lambda kv:len(kv[0]),reverse=True):
            if meta['kind']=='dir':os.chmod(destination/rel,meta.get('mode',0o700)&0o777)
    # File permissions differ between macOS/Linux and Windows; compare all paths,
    # kinds and hashes while apply pins the actual native staging permissions.
    portable=lambda files:{k:{a:b for a,b in v.items() if a!='mode'} for k,v in files.items()}
    if portable(c.manifest(destination))!=portable(item['manifest']):raise ValueError('Restored staging checksum mismatch')
    return item

def restore_targets(m,p):
    entries=p.get('entries')
    if not isinstance(entries,list) or not 1<=len(entries)<=10000:raise ValueError('请选择要恢复的内容')
    root=p.get('target_root')
    if root:root,_=m.install_root(root)
    result=[];destinations=set()
    for e in entries:
        container,meta=m.backup_record(e['id'])
        if not m.verify_backup(e['id'])['valid']:raise ValueError('Backup is damaged or incomplete')
        name=meta.get('name');before=c.manifest(container) if meta.get('external') else meta.get('manifest',{})
        if meta.get('bundle'):
            item=next((i for i in read_package(package_path(m,e['id']))['items'] if i['key']==e.get('key')),None)
            if not item or not root:raise ValueError('请选择备份包内容及恢复目录')
            name=item.get('folder') or item['name'];before=item['manifest']
        if root:
            c.safe_relative(name)
            if Path(name).name!=name or name in ('','.'):raise ValueError('Unsafe Skill folder name')
            target=str(root/name)
        else:target=meta.get('original_path')
        if not target:raise ValueError('历史备份需要指定恢复目录')
        target=m.validate_target(target)
        real=str(target.resolve()).casefold()
        if real in destinations:raise ValueError('多个 Skills 将恢复到同一位置，请分批选择')
        destinations.add(real)
        current=c.manifest(target)
        result.append({**e,'path':str(target),'exists':c.exists(target),
                       'identical':bool(current) and c.content(current)==c.content(before)})
    return {'items':result}


def preview_restore(m,p):
    if hasattr(m,'batch_progress'):m.batch_progress(0,None,'正在核对恢复位置')
    plans=[];destinations=set();entries=p.get('entries')
    if not isinstance(entries,list) or not 1<=len(entries)<=10000:raise ValueError('请选择要恢复的内容')
    root=p.get('target_root')
    if root:root,_=m.install_root(root)
    try:
        for e in entries:
            container,meta=m.backup_record(e['id'])
            key=e.get('key');name=meta.get('name')
            if meta.get('bundle'):
                data=read_package(package_path(m,e['id']),verify=False)
                item=next((i for i in data['items'] if i['key']==key),None)
                if not item:raise ValueError('请选择备份包中的 Skill')
                name=item.get('folder') or item['name']
                if not root:raise ValueError('请选择新设备上的恢复目录')
            if root:
                c.safe_relative(name)
                if Path(name).name!=name or name in ('','.'):raise ValueError('Unsafe Skill folder name')
                target=str(root/name)
            else:target=meta.get('original_path')
            if not target:raise ValueError('历史备份需要指定恢复目录')
            real=str(Path(target).resolve()).casefold()
            if real in destinations:raise ValueError('多个 Skills 将恢复到同一位置，请分批选择')
            destinations.add(real)
            plans.append(m.plan('restore',backup_id=e['id'],restore_path=target,allow_changes=bool(p.get('allow_overwrite')),bundle_item=key))
        return {'plans':plans}
    except Exception:
        for plan in plans:m.discard_plan(plan['id'])
        raise

def apply_restore(m,ids,confirmed):
    if confirmed is not True:raise ValueError('Confirmation required')
    if not isinstance(ids,list) or not 1<=len(ids)<=10000 or len(set(ids))!=len(ids):raise ValueError('Invalid restore plans')
    for pid in ids:
        p=m.get_plan(pid)
        if p.get('action')!='restore' or p.get('state')!='preview' or p.get('expired'):raise ValueError('恢复预览已失效')
        if c.fingerprint(c.manifest(Path(p['target'])))!=p['before']:raise ValueError('目标已变化，请重新预览')
    results=[]
    if hasattr(m,'batch_progress'):m.batch_progress(0,len(ids),'正在恢复')
    for pid in ids:
        try:results.append(m.apply(pid))
        except (OSError,ValueError) as exc:return {'state':'partial','completed':len(results),'total':len(ids),'results':results,'error':c.redact(str(exc)),'remaining':ids[len(results):]}
        if hasattr(m,'batch_progress'):m.batch_progress(len(results),len(ids),'正在恢复')
    return {'state':'succeeded','completed':len(results),'total':len(ids),'results':results}

def dispatch(m,p):
    action=p['action']
    if action=='create-snapshot':
        temporary=m.home/(c.uid('snapshot')+'.zip')
        try:
            data=write_package(m,temporary,items_for(m,p.get('scope','all'),p.get('ids'),p.get('agent')),p.get('agent') if p.get('scope')=='agent' else p.get('scope','all'))
            return register(m,temporary,data,'snapshot')
        finally:temporary.unlink(missing_ok=True)
    if action=='import-snapshot':
        source=c.absolute(p['path']);return register(m,source,read_package(source),'imported-snapshot')
    if action=='snapshot-detail':return read_package(package_path(m,p['id']))
    if action=='export-backup':
        bid=p['id'];container,meta=m.backup_record(bid)
        folder=m.home/'exports';folder.mkdir(mode=0o700,exist_ok=True)
        # Bound retained downloads to 24 hours.
        for old in folder.glob('export-*.zip'):
            if dt.datetime.now().timestamp()-old.stat().st_mtime>86400:old.unlink()
        name=c.uid('export')+'.zip';dest=folder/name
        try:
            if meta.get('bundle'):shutil.copyfile(package_path(m,bid),dest)
            else:
                if not m.verify_backup(bid)['valid']:raise ValueError('备份校验失败')
                source=container if meta.get('external') else container/'content'
                if not (source/'SKILL.md').is_file():raise ValueError('此备份格式暂不支持导出')
                row={'name':meta.get('name') or source.name,'version':None,'folder':Path(meta.get('original_path') or meta.get('name') or source.name).name}
                write_package(m,dest,{str(source):{'row':row,'agents':set(),'references':[]}},'selected')
            os.chmod(dest,0o600)
            return {'download_id':name,'filename':'skills-backup.zip'}
        except Exception:dest.unlink(missing_ok=True);raise
    if action=='preview-restore-many':return preview_restore(m,p)
    if action=='apply-restore-many':return apply_restore(m,p.get('ids'),p.get('confirmed'))
    if action=='preview-empty-trash':
        from recovery import preview_purge
        rows=[r for r in m.backups(False)['backups'] if r['type']=='quarantine' and r['state']=='available']
        ids=[r['id'] for r in rows if not r['retained']]
        return {**(preview_purge(m,ids) if ids else {'plans':[],'bytes':0}), 'skipped_retained':sum(r['retained'] for r in rows)}
