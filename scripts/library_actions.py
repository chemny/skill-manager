"""Explicit directory adoption and local installation, without Agent config edits."""
import json
import os
import shutil
from pathlib import Path
import maintenance as c

ACTIONS={'directory-preview','directory-add','install-local-identify'}

def preview(m, raw):
    root=c.absolute(raw)
    if not root.is_dir():raise ValueError('目录不存在或不可读取')
    found=[];errors=[]
    def visit(p,depth):
        if depth>9:return
        try:
            if (p/'SKILL.md').is_file():
                info=m.a.capability_from_skill_md('shared',p,p/'SKILL.md')
                found.append({'path':str(p),'name':info['name'],'description':info.get('description',''),'digest':c.digest_file(p/'SKILL.md')})
                return
            for child in sorted(p.iterdir()):
                if child.is_symlink():continue
                if child.is_dir() and not child.name.startswith('.') and child.name not in ('node_modules','__pycache__'):visit(child,depth+1)
                if len(found)>10000:raise ValueError('目录包含过多 Skills，请选择更具体的位置')
        except OSError as e:errors.append({'path':str(p),'error':c.redact(str(e))})
    visit(root,0)
    return {'path':str(root),'items':found,'count':len(found),'errors':errors,'fingerprint':c.fingerprint(found)}

def dispatch(m,p):
    action=p['action']
    if action=='directory-preview':return preview(m,p.get('path',''))
    if action=='directory-add':
        if p.get('confirmed') is not True:raise ValueError('Confirmation required')
        v=preview(m,p['path'])
        if not v['count']:raise ValueError('这个目录没有发现 SKILL.md')
        if v['fingerprint']!=p.get('fingerprint'):raise ValueError('目录内容已变化，请重新预览')
        platform=p.get('platform') or 'shared'
        from skill_locations import agent_catalog
        if platform != 'shared' and not any(a['id']==platform and a['added'] for a in agent_catalog(m.a.load_config())):
            raise ValueError('请先添加对应 Agent')
        config=m.a.load_config();group=config['platforms'].setdefault(platform,{'enabled':True,'roots':[]})
        group.update(enabled=True, removed=False)
        if v['path'] not in group['roots']:group['roots'].append(v['path'])
        m.a.save_config(config);scan=m.scan()
        return {'registered':True,'count':v['count'],'path':v['path'],'platform':platform,'scan':scan}
    root,_=m.install_root(p['target_root']);source=c.absolute(p['path'])
    pid=c.uid('selection');directory=m.home/'plans'/pid;directory.mkdir(mode=0o700)
    try:
        archive=directory/'source'
        if source.is_dir():
            files=c.manifest(source)
            if any(v['kind']=='link' for v in files.values()):raise ValueError('本地安装内容包含链接，请先核实')
            c.copy_path(source,archive)
        elif source.is_file() and source.suffix.lower()=='.zip':
            from snapshots import extract_zip
            extract_zip(source,archive)
        else:raise ValueError('请选择本地 Skill 文件夹或 ZIP 文件')
        candidates=m.a.skill_install_candidates(archive)
        for item in candidates:
            files=c.manifest(archive/item['remote_path'])
            if any(v['kind']=='link' for v in files.values()):raise ValueError('安装包包含链接')
            item.update(fingerprint=c.fingerprint(files),space=c.size(archive/item['remote_path']))
        if not candidates:raise ValueError('没有找到 SKILL.md')
        value={'id':pid,'action':'install-selection','created_at':c.stamp(),'target':str(root),'archive':str(archive),
               'source':{'source_type':'local','source_url':str(source)},'candidates':candidates}
        m.db.execute('insert into maintenance_plans values (?,?,?,?)',(pid,c.stamp(),'selection',json.dumps(value)));m.db.commit();m.invalidate_cache()
        return value
    except Exception:
        shutil.rmtree(directory);raise
