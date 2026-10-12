"""Read-only cleanup advice. Uncertainty never implies that a backup is disposable."""
import datetime as dt
import json
import re
from pathlib import Path
import maintenance as core


def advice(code, reason, **extra):
    return {'code':code, 'label':{'keep':'不建议删除','review':'自行判断','cleanable':'建议删除'}[code],
            'reason':reason, **extra}


def version(value):
    match=re.fullmatch(r'v?(\d+(?:\.\d+){0,3})', str(value or ''))
    if not match:return None
    parts=tuple(int(n) for n in match[1].split('.'))
    return parts+(0,)*(4-len(parts))


def pristine(meta):
    baseline=meta.get('baseline')
    if isinstance(baseline,str):
        try:baseline=json.loads(baseline)
        except (ValueError,TypeError):return False
    return baseline is not None and not any(core.diff(baseline,meta.get('manifest',{})).values())


def lookup_names(row):
    names={row['name'],row.get('skill_info',{}).get('name')}-{None,''}
    # Folder versions are candidate lookup clues, not verified version labels.
    return names | {re.sub(r'-v?\d+(?:[.-]\d+){1,3}$','',n) for n in names}


def duplicate_keeper(m, row, before, source):
    """Only a verified, independent, complete backup can replace another backup."""
    from recovery import skill_info
    rows = getattr(m, '_advice_rows', None)
    if rows is None:rows = m.backups(verify=False)['backups']
    rank = lambda b:(bool(b.get('retained')), b.get('saved_time',{}).get('value') or b.get('created_at') or '', b['id'])
    names = lookup_names(row)
    for other in sorted(rows,key=rank,reverse=True):
        if (other['id']==row['id'] or other.get('state')!='available' or other.get('type')!='backup'
                or other.get('bundle') or other.get('format')=='archive' or rank(other)<=rank(row)):
            continue
        if not names.intersection(lookup_names(other)):continue
        path=Path(other.get('historical_path') or other['path'])
        if not other.get('external'):path=path/'content'
        if path.resolve()==source.resolve() or source.resolve() in path.resolve().parents or path.resolve() in source.resolve().parents:continue
        try:
            candidate=core.manifest(path)
            if candidate!=before or any(v['kind']=='link' for v in candidate.values()):continue
            if not m.verify_backup(other['id'])['valid'] or core.manifest(path)!=candidate:continue
            return {'keeper_id':other['id'],'keeper_name':other['name'],'keeper_version':skill_info(m,path).get('version')}
        except (ValueError,OSError):continue
    return None


def assess(m, row, meta, before, comparisons, valid):
    from recovery import skill_info, flags
    if row.get('retained'):return advice('keep','已标记防止误删',summary='已设防误删')
    source=Path(row['path']) if meta.get('external') else Path(row['path'])/'content'
    active_roots=[root.resolve() for _,root,status in m.roots() if status=='active']
    for raw in m.db.execute("select path from capabilities where kind='skill' and status='active' and health!='not-scanned'"):
        registered=Path(raw['path']).absolute();path=registered.resolve()
        if path.exists() and any(root==registered or root in registered.parents for root in active_roots) and (path==source.resolve() or source.resolve() in path.parents):
            return advice('keep','这份备份目录仍被扫描为安装项，不能按闲置备份清理',summary='目录仍被引用')
    if not valid:return advice('review','文件校验未通过，请先核实完整性',summary='完整性未通过校验')
    if meta.get('type')=='quarantine':return advice('review','回收内容需人工确认不再需要，不能仅凭版本号判断')
    keeper=duplicate_keeper(m,row,before,source)
    if keeper:
        return advice('cleanable','另有校验通过且文件、目录及权限完全相同的独立备份；保留该副本后可删除此重复备份',summary='另有完整相同备份',**keeper)
    if meta.get('external') or not meta.get('original_path') or not meta.get('source'):
        if not comparisons:return advice('review','未找到对应安装项，也未找到可替代的完整相同备份',summary='未找到对应安装项')
        if len(comparisons)>1:return advice('review','找到多个同名安装项，尚未确认这份备份对应哪一项',summary='对应多个同名安装项')
        current=comparisons[0]
        if current.get('state')=='unavailable':return advice('review','对应安装目录无法完整读取，未能完成内容比较',summary='安装项未能完整读取')
        if current.get('state')=='identical':return advice('keep','备份与对应安装项内容一致；没有其他可替代备份，保留用于回退',summary='保留一份当前内容副本')
        saved,current_version=version(row.get('skill_info',{}).get('version')),version(current.get('version'))
        if saved is not None and current_version is not None and saved>current_version:
            return advice('keep','备份版本高于同名安装项，可能包含尚未恢复的内容',summary='备份版本高于安装项')
        return advice('review','已比较对应安装项，内容存在差异；缺少可信历史记录，不能确认差异是否可以丢弃',summary='与安装项内容不同')
    if meta.get('baseline') is None:return advice('review','缺少同步基线，无法排除本地独有修改',summary='无法排除独有修改')
    if not pristine(meta):return advice('keep','备份含有偏离同步基线的本地修改，保留以免丢失',summary='含本地修改')
    if len(comparisons)!=1 or comparisons[0].get('basis')!='original' or comparisons[0].get('state')=='unavailable':
        return advice('review','未核实唯一的当前安装项，不能确认回退关系',summary='对应安装项不明确')
    current=Path(comparisons[0]['path'])
    raw=m.db.execute("select * from capabilities where path=? and status='active'",(meta['original_path'],)).fetchone()
    if not raw:return advice('review','当前安装记录不明确',summary='对应安装项不明确')
    item=dict(raw);state=m.db.execute('select * from maintenance_items where id=?',(item['id'],)).fetchone()
    check=json.loads(state['last_check']) if state and state['last_check'] else {}
    try:
        recent=0 <= (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(check['checked_at'].replace('Z','+00:00'))).total_seconds() <= 21600
    except (KeyError,ValueError,TypeError):recent=False
    now=core.manifest(current)
    baseline=json.loads(state['baseline']) if state and state['baseline'] else None
    if (not recent or check.get('status')!='latest' or check.get('local_fingerprint')!=core.fingerprint(now)
            or baseline is None or any(core.diff(baseline,now).values()) or m.source(item,dict(state))!=meta['source']):
        return advice('review','当前版本、来源或本地修改尚未核实；请先检查更新，保留备份',summary='当前版本尚未核实')
    saved_version=version(row.get('skill_info',{}).get('version'))
    current_version=version(comparisons[0].get('version'))
    if saved_version is None or current_version is None:return advice('review','版本号不明确，不能按新旧建议删除',summary='版本关系不明')
    siblings=[]
    retained=flags(m)
    for record in m.db.execute("select * from maintenance_backups where state='available' and id!=?",(row['id'],)):
        other=json.loads(record['payload'])
        if (other.get('type')!='backup' or other.get('bundle') or other.get('source')!=meta['source']
                or other.get('original_path')!=meta['original_path'] or not pristine(other)):continue
        p=Path(record['path'])/'content'
        version_label=skill_info(m,p).get('version');v=version(version_label)
        if v is None or not m.verify_backup(record['id'])['valid']:continue
        siblings.append({'id':record['id'],'version':v,'version_label':version_label,'created_at':record['created_at'],
                         'manifest':other['manifest'],'retained':retained.get(record['id'],False)})
    # A deterministic keeper prevents two duplicate copies recommending each other's deletion.
    newer_duplicates=[s for s in siblings if s['manifest']==before
                      and (s['created_at'],s['id'])>(row.get('created_at') or '',row['id'])]
    if newer_duplicates:
        keeper=max(newer_duplicates,key=lambda s:(s['retained'],s['created_at'],s['id']))
        return advice('cleanable','另有校验通过的完整相同副本；保留该副本后可删除此重复备份',summary='另有完整相同副本',keeper_id=keeper['id'],keeper_version=keeper['version_label'])
    rollback=[s for s in siblings if saved_version<s['version']<current_version]
    if saved_version<current_version and rollback:
        keeper=max(rollback,key=lambda s:(s['version'],s['created_at'],s['id']))
        return advice('cleanable','当前安装已核实，另有更近的可信回退版本；此较早备份可清理',summary='已有较新回退副本',keeper_id=keeper['id'],keeper_version=keeper['version_label'])
    return advice('keep','保留当前版本的副本或最近一份可信回退备份',summary='保留当前或回退副本')
