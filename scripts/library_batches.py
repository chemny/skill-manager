"""Explicit batch previews reuse the single-item maintenance safety checks."""
from pathlib import Path
import maintenance as c

ACTIONS={'preview-library-many','apply-library-many','discard-previews'}

def ids(value):
    if not isinstance(value,list) or not 1<=len(value)<=10000 or any(not isinstance(i,str) or not i for i in value):raise ValueError('请选择 1 至 10000 项')
    return list(dict.fromkeys(value))

def preview(m,operation,targets):
    if operation not in ('delete','update'):raise ValueError('Unsupported batch operation')
    rows=[m.select(i) for i in ids(targets)]
    rows.sort(key=lambda r:Path(r['path']).is_symlink())
    plans=[];skipped=[];seen=set()
    try:
        for index,row in enumerate(rows):
            if hasattr(m,'batch_progress'):m.batch_progress(0,None,'正在核对所选项目')
            path=Path(row['path']);key=str(path.absolute()) if operation=='delete' else str(path.resolve())
            if key in seen:
                skipped.append({'id':row['id'],'name':row['name'],'reason':'同一操作目标已包含在本次预览中'});continue
            seen.add(key)
            try:
                if row['kind']!='skill':raise ValueError('此项不是 Skill')
                if m.state(row)['protected'] or m.affected_protected(path):raise ValueError('受保护，不能执行此操作')
                if not path.exists():raise ValueError('文件不存在或链接失效，请重新扫描')
                m.validate_target(path,row)
                if operation=='update':
                    import json
                    state=m.state(row)
                    check=json.loads(state['last_check']) if state['last_check'] else {}
                    if check.get('status') not in ('update_available','local_modified','baseline_unknown','local_ahead'):raise ValueError('尚未检测到可更新内容，请先检查更新')
                    if check.get('local_fingerprint') and check['local_fingerprint']!=c.comparison_fingerprint(c.manifest(path.resolve())):raise ValueError('本地内容已变化，请重新检查更新')
                if any(path!=Path(p['target']) and (path in Path(p['target']).parents or Path(p['target']) in path.parents) for p in plans):raise ValueError('与本次其他目标存在父子目录关系，请分开操作')
                plans.append(m.plan(operation,row))
            except (ValueError,OSError) as exc:
                skipped.append({'id':row['id'],'name':row['name'],'reason':c.redact(str(exc))})
        return {'operation':operation,'selected':len(rows),'plans':plans,'skipped':skipped}
    except Exception:
        for p in plans:m.discard_plan(p['id'])
        raise

def apply(m,plans,operation,confirmed):
    if confirmed is not True:raise ValueError('Confirmation required')
    if operation not in ('delete','update'):raise ValueError('Unsupported batch operation')
    plan_ids=ids(plans)
    if len(plan_ids)!=len(plans):raise ValueError('Duplicate batch plans')
    # Check every target before changing any. Each apply repeats checks under lock.
    targets=[]
    for pid in plan_ids:
        p=m.get_plan(pid)
        if p.get('action')!=operation or p.get('state')!='preview' or p.get('expired'):raise ValueError('批量预览已失效，请重新生成')
        target=m.validate_target(p['target'],p['item'])
        if c.fingerprint(c.manifest(target))!=p['before']:raise ValueError('Target changed; preview again')
        if m.affected(target)!=p['affected_references']:raise ValueError('Known Agent references changed; preview again')
        if m.state(p['item'])['protected'] or m.affected_protected(target):raise ValueError('Target became protected')
        if any(target==x or target in x.parents or x in target.parents for x in targets):raise ValueError('批量目标重复或嵌套，请重新选择')
        targets.append(target)
        if operation=='update' and c.fingerprint(c.manifest(m.home/'plans'/pid/'content'))!=p['after']:raise ValueError('Staged content changed; preview again')
    results=[]
    for index,pid in enumerate(plan_ids):
        if hasattr(m,'batch_progress'):m.batch_progress(index,len(plan_ids),'执行：'+m.get_plan(pid)['item']['name'])
        try:results.append(m.apply(pid))
        except (OSError,ValueError) as exc:return {'state':'partial','action':operation,'completed':len(results),'total':len(plan_ids),'results':results,'remaining':plan_ids[len(results):],'error':c.redact(str(exc))}
        if hasattr(m,'batch_progress'):m.batch_progress(len(results),len(plan_ids),'正在处理')
    return {'state':'succeeded','action':operation,'completed':len(results),'total':len(plan_ids),'results':results}

def dispatch(m,p):
    if p['action']=='discard-previews':
        discarded=[];skipped=[]
        for pid in ids(p.get('ids')):
            try:
                plan=m.get_plan(pid)
                if plan.get('state')!='preview':skipped.append(pid);continue
                m.discard_plan(pid);discarded.append(pid)
            except (ValueError,OSError):skipped.append(pid)
        return {'discarded':discarded,'skipped':skipped}
    if p['action']=='preview-library-many':return preview(m,p.get('operation'),p.get('ids'))
    return apply(m,p.get('ids'),p.get('operation'),p.get('confirmed'))
