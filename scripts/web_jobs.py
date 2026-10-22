"""Process-local HTTP task queue; all filesystem work stays in maintenance.Manager.

Independent queries run concurrently. A page reload can resume polling; restarts lose the
queue, while plans/backups/operation history remain in the maintenance database.
"""
import copy
import threading
import contextlib
import maintenance
import task_scopes


class BusyError(ValueError):
    busy = True


class Jobs:
    TAXONOMY_ACTIONS = {'taxonomy', 'taxonomy-update','classification-preview','classification-apply','classification-undo','source-search-policy','source-search-policy-update','discover-sources-batch'}
    ACTIONS = __import__('library_batches').ACTIONS | __import__('snapshots').ACTIONS | __import__('library_actions').ACTIONS | {'discover-backups','retain-backup','ignore-backup-candidate','import-backup','preview-purge-many','apply-purge-many','probe-source','set-labels','import-usage','discover-sources','duplicate-review','inventory', 'scan', 'check', 'check-many', 'preview', 'apply',
               'project-analyze','project-analysis-stop','space-catalog','space-add-project','disk-scan','disk-report','disk-stop','space-scan','space-results','space-trash','space-trash-list','space-restore','space-purge','space-advice','space-trash-batch','space-restore-many','trash-purge-many',
               'discovery-refresh','discovery-verify','translate-descriptions','cache-clean','execute','execute-library-many','execute-backups','execute-restore-many','empty-trash',
               'update-now', 'backup', 'backups', 'backup-detail', 'verify-backup', 'protect',
               'unprotect', 'source-bind', 'history', 'plans', 'get-plan',
               'discard-plan', 'usage', 'recover', 'inspect-space', 'install-roots', 'install-identify', 'set-description','open-folder'}

    def __init__(self, legacy):
        self.legacy = legacy
        self.lock = threading.Lock()
        self.scope_changed = threading.Condition(self.lock)
        self.records = {}
        self.active = set()
        self.scopes = {}
        self.network_slots = threading.BoundedSemaphore(3)
        self.inventory_slots = threading.BoundedSemaphore(1)
        self.commit_slot = threading.Lock()
        self.latest_check = None

    @contextlib.contextmanager
    def network(self):
        if not self.network_slots.acquire(timeout=5):
            raise BusyError('查询请求较多，请稍后重试；其他操作不受影响。')
        try: yield
        finally: self.network_slots.release()

    @contextlib.contextmanager
    def configure(self):
        with self.lock:
            if any(s['write'] for s in self.scopes.values()):
                raise BusyError('目录或文件正在变更，请稍后修改设置。')
            yield

    def busy(self):
        with self.lock:
            return bool(self.active)

    def snapshot(self, jid=None):
        with self.lock:
            if jid:
                if jid not in self.records:
                    raise ValueError('Task not found; service may have restarted. Check history before retrying writes.')
                return copy.deepcopy(self.records[jid])
            running=[copy.deepcopy(r) for k,r in self.records.items() if k in self.active]
            return {'active':running[0] if running else None,'jobs':running}

    def update(self, jid, **values):
        with self.lock:
            self.records[jid].update(values, updated_at=maintenance.stamp())

    def start(self, request):
        if not isinstance(request, dict) or request.get('action') not in self.ACTIONS | self.TAXONOMY_ACTIONS:
            raise ValueError('Unsupported task action')
        request = copy.deepcopy(request)
        if request.get('action') == 'check-many' and request.get('scope') == 'all':
            registry = self.legacy.Registry()
            try:
                removed = {k for k,v in self.legacy.load_config()['platforms'].items() if v.get('removed')}
                request['ids'] = [r['id'] for r in registry.conn.execute("select id,kind,platform,health,path from capabilities where kind='skill'") if r['platform'] not in removed and r['health']!='not-scanned' and maintenance.absolute(r['path']).exists()]
            finally:
                registry.conn.close()
        if request.get('action') in ('space-trash','space-restore','space-purge','space-trash-batch','space-restore-many','trash-purge-many','cache-clean','execute','execute-library-many','execute-backups','execute-restore-many','empty-trash','update-now','apply', 'recover','import-backup','apply-purge-many','directory-add','apply-restore-many','apply-library-many') and request.get('confirmed') is not True:
            raise ValueError('Confirmation required')
        if request.get('action') == 'check-many' and (not isinstance(request.get('ids'), list) or
                not request['ids'] or len(request['ids']) > 10000 or
                any(not isinstance(x, str) for x in request['ids'])):
            raise ValueError('Select at least one skill to check')
        if request['action'] == 'inventory' and request.get('ids') is not None and (not isinstance(request['ids'], list) or
                len(request['ids']) > 10000 or any(not isinstance(x, str) for x in request['ids'])):
            raise ValueError('Invalid inventory scope')
        scope = task_scopes.describe(self.legacy,request)
        ui_scope=request.get('ui_scope')
        if ui_scope is not None and (not isinstance(ui_scope,dict) or ui_scope.get('view') not in ('space','home','discovery','skills','updates','backups','trash','settings','sources','suggestions','install','plans') or not isinstance(ui_scope.get('key'),str) or len(ui_scope['key'])>12000):
            raise ValueError('Invalid view scope')
        with self.lock:
            if request['action'] in ('disk-stop', 'project-analysis-stop'):
                target = 'disk-scan' if request['action']=='disk-stop' else 'project-analyze'
                for existing in self.active:
                    if self.records[existing]['action']==target:
                        self.records[existing]['stop_requested']=True
            if request['action'] in ('project-analyze', 'disk-scan'):
                for existing in self.active:
                    if self.records[existing]['action'] in ('project-analyze', 'disk-scan'):
                        if request['action']=='disk-scan' and self.records[existing]['action']=='disk-scan' and self.records[existing].get('_project_scope_key')==repr((request.get('catalog_id'), request.get('ids'), request.get('agents'), request.get('rescan'))):
                            return copy.deepcopy(self.records[existing])
                        raise BusyError('另一个项目扫描或分析正在进行，请等待完成或停止。')
            if request['action'] == 'classification-apply':
                for existing in self.active:
                    if self.records[existing]['action'] == 'classification-apply':
                        if self.records[existing].get('field','both') == request.get('field','both'):
                            return copy.deepcopy(self.records[existing])
                        raise BusyError('另一个自动分类任务正在处理，请等待完成后重试。')
            inventory_key = (None if request.get('ids') is None else tuple(sorted(set(request['ids']))), bool(request.get('lightweight'))) if request['action']=='inventory' else None
            if inventory_key:
                for existing in self.active:
                    if self.records[existing].get('_inventory_key') == inventory_key:
                        return copy.deepcopy(self.records[existing])
            check_key = (tuple(sorted(set(request.get('ids',[])))), bool(request.get('force'))) if request['action']=='check-many' else None
            if check_key:
                for existing in self.active:
                    if self.records[existing].get('_check_key') == check_key:
                        if request.get('scope') == 'all':
                            self.records[existing]['ui_scope'] = copy.deepcopy(ui_scope)
                        return copy.deepcopy(self.records[existing])
            if len(self.active)>=32:raise BusyError('后台任务较多，请稍后重试。')
            waiting = any(task_scopes.conflicts(scope,s) for s in self.scopes.values())
            if waiting and request['action'] != 'classification-apply':
                raise BusyError('相关 Skill 或目录正在操作，请等待该任务完成；其他项目仍可使用。')
            jid = maintenance.uid('job')
            self.records[jid] = {'id':jid, 'action':request['action'], 'state':'queued',
                                 'field':request.get('field','both') if request['action']=='classification-apply' else None,
                                 'resources':scope['resources'],'write':scope['write'],
                                 'global':scope['global'],'catalog':scope['catalog'],'plan_id':request.get('id'),
                                 'operation':scope['operation'],
                                 'ui_scope':copy.deepcopy(ui_scope),
                                 'project_paths':copy.deepcopy(request.get('paths', [])) if request['action']=='project-analyze' else [],
                                 '_project_scope_key':repr((request.get('catalog_id'), request.get('ids'), request.get('agents'), request.get('rescan'))) if request['action']=='disk-scan' else None,
                                 '_check_key':check_key,
                                 '_inventory_key':inventory_key,
                                 'created_at':maintenance.stamp(), 'completed':0, 'total':None,
                                 'message':'等待其他任务完成' if waiting else '正在处理', 'result':None}
            self.active.add(jid)
            self.scopes[jid]=scope
            if request['action']=='check-many':self.latest_check=jid
            # Bounded memory; persisted maintenance records are not removed.
            for old in list(self.records)[:-30]:
                if old not in self.active:del self.records[old]
        threading.Thread(target=self.run, args=(jid, copy.deepcopy(request)), daemon=True).start()
        return self.snapshot(jid)

    def run(self, jid, request):
        m = None
        final_state = 'failed'
        slot = None
        try:
            with self.scope_changed:
                while any(task_scopes.conflicts(self.scopes[jid], s)
                          for other, s in self.scopes.items() if other != jid):
                    self.scope_changed.wait()
                scope=self.scopes[jid]
            slot = (self.network_slots if scope['lane']=='network' else self.inventory_slots
                    if scope['lane']=='inventory' else self.commit_slot
                    if request['action'] in task_scopes.COMMITS else None)
            if slot:
                self.update(jid,message='等待执行')
                slot.acquire()
            self.update(jid,state='running',message='正在处理')
            m = maintenance.Manager(self.legacy)
            m._use_check_cache = request['action'] in ('check','check-many')
            m._force_check = bool(request.get('force'))
            def save_check(run):
                with self.lock:
                    if self.latest_check==jid:maintenance.write_json(m.home/'update-run.json',run)
            if request['action']=='inventory' and self.commit_slot.locked():
                result=m.cached_inventory()
                result['cache']['dirty']=True
                self.update(jid,result=result,message='文件操作进行中，保留当前清单')
                final_state='succeeded'
                return
            if request['action']=='duplicate-review':
                from duplicates import review
                result=review(m,lambda completed,total,message:self.update(jid,completed=completed,total=total,message=message))
            elif request['action'] == 'import-usage':
                result=m.import_usage(int(request.get('days',90)),bool(request.get('replay')),request.get('roots'),
                    progress=lambda completed,total,message:self.update(jid,completed=completed,total=total,message=message))
            elif request['action'] == 'check-many':
                from check_batches import run as check_batch
                last_publish = [0.0]
                def progress(run, results):
                    import time
                    now = time.monotonic()
                    if len(results) > 1 and run['completed'] != run['total'] and now - last_publish[0] < .1:
                        return
                    last_publish[0] = now
                    save_check(run)
                    self.update(jid, total=run['total'], completed=run['completed'],
                                partial_checks=results, check_run=run, message='检查更新')
                result = check_batch(m, request['ids'], force=bool(request.get('force')),
                                     progress=progress, run_id=jid, scope=request.get('scope','partial'))
            else:
                m.batch_progress=lambda completed,total,message:self.update(jid,completed=completed,total=total,message=message)
                if request['action'] in ('project-analyze', 'disk-scan'):
                    m.project_current = lambda path: self.update(jid, current_project=path)
                    def cancel_requested():
                        with self.lock:
                            return bool(self.records[jid].get('stop_requested'))
                    m.cancel_requested = cancel_requested
                if request['action'] == 'project-analyze':
                    m.project_result = lambda rows: self.update(jid, partial_analyses=copy.deepcopy(rows))
                if request['action'] == 'disk-scan':
                    m.disk_result = lambda report: self.update(jid, partial_report=copy.deepcopy(report))
                result = maintenance.dispatch(m, request)
            self.update(jid, result=result, message='处理完成')
            final_state = 'succeeded'
        except Exception as exc:
            from diagnostics import record
            event = record(self.legacy.APP_DIR, exc, request.get('action', 'unknown'), jid)
            self.update(jid, error=event['error'], message=event['message'], error_id=event['error_id'], code=event['code'])
        finally:
            try:
                if m: m.db.close()
            finally:
                with self.lock:
                    self.records[jid]['state'] = final_state
                    self.active.discard(jid)
                    self.scopes.pop(jid,None)
                    self.scope_changed.notify_all()
                if slot:slot.release()
