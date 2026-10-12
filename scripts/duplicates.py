"""Duplicate candidates are compared by local contents, never by names alone."""
from collections import defaultdict
from pathlib import Path
import os
import maintenance as core


def review(manager, progress=None):
    cached = manager.cached_inventory()
    items = cached['items'] if cached.get('cache',{}).get('available') and not cached['cache'].get('dirty') else manager.inventory()['items']
    paths = {}
    for item in sorted(items,key=lambda i:(i['reference_type']!='directory',not bool(i.get('source')))):
        if item['kind']=='skill' and item['present'] and item['health']!='broken-link':
            paths.setdefault(item['real_path'],item)
    names=defaultdict(list)
    for item in paths.values(): names[item['name'].casefold()].append(item)
    candidates=[g for g in names.values() if len(g)>1]
    total=sum(len(g) for g in candidates);done=0;groups=[]
    for group in sorted(candidates,key=lambda g:g[0]['name'].casefold()):
        targets=[];manifests={}
        for item in group:
            done+=1
            if progress:progress(done-1,total,'比对：'+item['name'])
            path=Path(item['real_path']);entry={k:item.get(k) for k in ('id','name','version','real_path','known_agents','source','protected','source_readiness')}
            entry['path']=item['path'];entry['bytes']=item.get('target_space',{}).get('allocated_bytes',0)
            entry.update(eligible=False,error=None)
            try:
                raw_path=Path(item['path'])
                if raw_path.resolve()!=path or not path.is_dir():raise ValueError('目录已变化，请重新扫描')
                m=core.manifest(path);manifests[item['id']]=m
                entry['content_hash']=core.fingerprint(core.content(m));entry['exact_hash']=core.fingerprint(m)
                entry['has_links']=any(v['kind']=='link' for v in m.values())
                try:manager.validate_target(raw_path,manager.select(item['id']));allowed=True
                except ValueError:allowed=False
                entry['eligible']=os.name!='nt' and allowed and not raw_path.is_symlink() and not entry['has_links'] and not item['protected'] and not manager.affected_protected(raw_path) and item['source_readiness']!='observe_only'
                if not entry['eligible']:entry['note']='平台维护、受保护、链接入口或当前系统不支持自动整理'
            except (OSError,ValueError) as exc:entry['error']=str(exc);entry['note']='无法完成比对，不能整理'
            targets.append(entry)
        valid=[t for t in targets if not t['error']]
        status='unverified' if len(valid)!=len(targets) else 'identical' if len({t['exact_hash'] for t in valid})==1 else 'content_equal' if len({t['content_hash'] for t in valid})==1 else 'different'
        for target in targets:
            target['retainers']=[t['id'] for t in targets if t['id']!=target['id'] and target['eligible'] and t['eligible'] and t.get('exact_hash')==target.get('exact_hash') and t.get('source')==target.get('source') and target.get('exact_hash')]
        delta=[]
        if len(valid)>1:
            base=manifests[valid[0]['id']]
            for t in valid[1:]:delta.append({'id':t['id'],**core.diff(base,manifests[t['id']])})
        groups.append({'name':group[0]['name'],'status':status,'targets':targets,'differences':delta})
    if progress:progress(total,total,'比对完成')
    result={'checked_at':core.stamp(),'groups':groups,'unique_skills':len(paths),'compared':total,'inventory_generation':manager.db.execute('select generation from maintenance_inventory_cache where id=1').fetchone()[0],'note':'同一实际目录已去重。仅文件、权限与来源一致的独立目录可预览改为引用；未知使用记录不用于删除判断。'}
    core.write_json(manager.home/'duplicate-review.json',result)
    return result
