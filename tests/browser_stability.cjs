// Isolated real-browser regression checks. The Python fixture owns all mutable data.
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.ASM_PLAYWRIGHT_MODULE || 'playwright');
const repo=require('node:path').resolve(__dirname,'..');
const output=process.argv[2];
const server=spawn('python3',['-u','-c',`
import argparse,sys,datetime as dt,os,json
sys.path[:0]=[${JSON.stringify(repo+'/tests')},${JSON.stringify(repo+'/scripts')}]
import test_maintenance as fixtures,maintenance
import daily_recommendations
from test_daily_recommendations import remote as daily_remote
daily_recommendations.AnonymousGitHub.get=daily_remote
x=fixtures.MaintenanceTests();x.setUp()
if ${process.argv.includes('--batch3-only')?'True':'False'}:
 p=x.m.plan('sync',x.row('b'),x.row('a'),allow_changes=True)
 old=dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=25)
 p['created_at']=old.isoformat()
 x.m.db.execute('update maintenance_plans set created_at=?,payload=? where id=?',(old.isoformat(),json.dumps(p),p['id']));x.m.db.commit()
 folder=x.home/'plans'/p['id']
 for child in sorted(folder.rglob('*'),key=lambda p:len(p.parts),reverse=True)+[folder]:os.utime(child,(old.timestamp(),old.timestamp()))
 config=x.a.load_config();config['platforms']['codex'].update(enabled=True,roots=[str(x.roots)],discover_defaults=False)
 x.a.save_config(config);x.m.scan()
 config=x.a.load_config();config['platforms']['codex']['removed']=True;x.a.save_config(config)
x.m.inventory()
maintenance.Manager.import_usage=lambda self,*args,**kwargs:{'imported':False,'fixture':True}
try:x.a.web_command(argparse.Namespace(host='127.0.0.1',port=0,open=False))
finally:x.tearDown()
`],{stdio:['ignore','pipe','pipe']});
let browser;
async function main(){
 const url=await new Promise((resolve,reject)=>{let data='';const timer=setTimeout(()=>reject(Error('Fixture server timed out')),15000);server.stdout.on('data',chunk=>{data+=chunk;const match=data.match(/http:\/\/127\.0\.0\.1:\d+/);if(match){clearTimeout(timer);resolve(match[0]);}});server.on('exit',code=>reject(Error('Server exited '+code)));});
 browser=await chromium.launch({executablePath:process.env.ASM_BROWSER_EXECUTABLE || undefined,headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],report={};
 page.on('pageerror',error=>errors.push(String(error)));
 await page.goto(url+'/manage');
 await page.waitForFunction(()=>inventoryReady&&inventory.length===2);
 report.initialRows=await page.locator('#items tr').count();assert.equal(report.initialRows,2);
 if(process.argv.includes('--batch3-only')){
  report.trashSorting=await page.evaluate(()=>{
   backupView='quarantine';tableSorts.backups={key:'date',direction:'desc'};
   backupData=[{id:'old-backup-new-trash',name:'a',type:'quarantine',state:'available',saved_time:{value:'2020-01-01'},trashed_at:'2026-10-12T10:00:00Z'},
    {id:'new-backup-old-trash',name:'b',type:'quarantine',state:'available',saved_time:{value:'2026-01-01'},trashed_at:'2026-10-11T10:00:00Z'}];
   const descending=filteredBackups().map(b=>b.id);tableSorts.backups.direction='asc';const ascending=filteredBackups().map(b=>b.id);
   return {descending,ascending,correct:descending[0]==='old-backup-new-trash'&&ascending[0]==='new-backup-old-trash'};
  });
  report.trashSortingTimezoneAndMissing=await page.evaluate(()=>{
   backupData=[{id:'early',name:'a',type:'quarantine',state:'available',trashed_at:'2026-10-12T10:00:00+08:00'},
    {id:'late',name:'b',type:'quarantine',state:'available',trashed_at:'2026-10-12T03:00:00Z'},
    {id:'unknown',name:'c',type:'quarantine',state:'available'}];
   tableSorts.backups.direction='desc';const descending=filteredBackups().map(b=>b.id);
   tableSorts.backups.direction='asc';const ascending=filteredBackups().map(b=>b.id);
   return descending.join(',')==='late,early,unknown'&&ascending.join(',')==='early,late,unknown';
  });
  assert.ok(report.trashSortingTimezoneAndMissing);
  await page.evaluate(async()=>{backupData=[];await tab('settings','storage')});
  report.cacheCleanupAvailable=await page.locator('[data-clean-cache]').count()>0;
  assert.equal(await page.locator('[data-clean-cache]').isEnabled(),true);
  let cleanupStarts=0,deleteReferences=[];
  page.on('request',req=>{if(!req.url().endsWith('/api/maintenance')||req.method()!=='POST')return;const data=req.postDataJSON();if(data.action==='job-start'&&data.request.action==='cache-clean')cleanupStarts++;if(data.action==='job-start'&&data.request.operation==='delete')deleteReferences=Object.values(data.request.expected_references||{}).flat();});
  await page.locator('[data-clean-cache]').click();await page.locator('#deleteConfirmation [data-cancel-delete]').click();
  await page.waitForFunction(()=>cacheStorage?.count===1&&!cacheStorageRequest);
  assert.equal(cleanupStarts,0);report.cacheCancelDoesNotSubmit=true;
  await page.screenshot({path:output.replace('.json','-storage-desktop.png'),fullPage:true});
  await page.setViewportSize({width:780,height:900});
  await page.screenshot({path:output.replace('.json','-storage-narrow.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:output.replace('.json','-storage-mobile.png'),fullPage:true});
  report.storageMobileNoOverlap=await page.evaluate(()=>[...el('storageRows').children].every(row=>{const boxes=[...row.children].map(c=>c.getBoundingClientRect());return boxes.every((a,n)=>boxes.slice(n+1).every(b=>a.right<=b.left||b.right<=a.left||a.bottom<=b.top||b.bottom<=a.top))}));
  assert.ok(report.storageMobileNoOverlap);
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('[data-clean-cache]').click();await page.locator('#deleteConfirmation .confirm-delete').click();
  await page.waitForFunction(()=>cacheStorage?.count===0&&!activeTasks.size&&!cacheStorageRequest);
  assert.equal(cleanupStarts,1);assert.equal(await page.locator('[data-clean-cache]').isDisabled(),true);
  report.cacheCleanup={jobSubmissions:cleanupStarts,remaining:0,skillsUntouched:await page.evaluate(()=>inventory.filter(i=>i.present).length===2)};
  await page.evaluate(()=>tab('skills'));
  await page.locator('[data-list-operation="delete"]').first().click();await page.locator('#deleteConfirmation .confirm-delete').click();
  await page.waitForFunction(()=>!activeTasks.size&&inventory.filter(i=>i.present).length===1,{},{timeout:20000});
  report.uninstallWithRemovedAgent={references:deleteReferences.map(r=>r.platform).sort(),completed:true};
  assert.deepEqual(report.uninstallWithRemovedAgent.references,['codex','shared']);
  assert.ok(report.trashSorting.correct&&report.cacheCleanup.skillsUntouched);
  assert.deepEqual(errors,[]);report.pageErrors=errors;
  fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));return;
 }
 if(process.argv.includes('--performance-only')){
  report.render=await page.evaluate(()=>{
   const template=structuredClone(inventorySnapshot),base=template.items[0],agents=['codex','claude_code','pi','opencode','workbuddy','qwen_code'];
   template.items=Array.from({length:2500},(_,n)=>({...structuredClone(base),id:'probe-'+n,name:'probe-'+n,
    path:'/fixture/skill-'+n,real_path:'/fixture/skill-'+n,platform:agents[n%agents.length],known_agents:[agents[n%agents.length]],references:['probe-'+n],reference_paths:['/fixture/skill-'+n]}));
   const samples=[];
   for(let n=0;n<5;n++){template.cache.generation=inventoryRevision+1;const start=performance.now();loadInventory(structuredClone(template));samples.push(performance.now()-start);}
   return {items:2500,samples_ms:samples,median_ms:[...samples].sort((a,b)=>a-b)[2]};
  });
  assert.deepEqual(errors,[]);fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));return;
 }
 await page.evaluate(()=>tab('skills'));
 report.authorizationRenewed=await page.evaluate(async()=>{apiToken='expired-test-token';const v=await request('inventory-cached');return v.items.length===2&&apiToken!=='expired-test-token';});
 const foreign=await page.request.get(url+'/api/session',{headers:{Origin:'https://foreign.example'}});
 assert.equal(foreign.status(),403);assert.ok(!(await foreign.text()).includes('"token"'));
 report.foreignOriginRejected=true;
 let failures=0,deleteStarts=0;
 await page.route('**/api/maintenance',async route=>{
  const data=route.request().postDataJSON();
  if(data.action==='job-start'&&data.request.operation==='delete')deleteStarts++;
  if(data.action==='job-status'&&data.id&&failures<2){failures++;await route.abort('connectionfailed');return;}
  await route.continue();
 });
 await page.locator('[data-list-operation="delete"]').first().click();
 await page.locator('#deleteConfirmation .confirm-delete').click();
 await page.waitForFunction(()=>!activeTasks.size&&inventory.filter(i=>i.present).length===1,{},{timeout:20000});
 assert.equal(deleteStarts,1);assert.equal(failures,2);
 report.deleteAfterTwoDisconnects={jobSubmissions:deleteStarts,remainingSkills:1};
 await page.unroute('**/api/maintenance');
 await page.waitForFunction(()=>backupData.some(b=>b.type==='quarantine'));
 const bid=await page.evaluate(()=>backupData.find(b=>b.type==='quarantine').id);
 await page.evaluate(async id=>executeOperation('restore',{id}),bid);
 await page.waitForFunction(()=>inventory.filter(i=>i.present).length===2&&!activeTasks.size);
 report.restoreSucceeded=true;
 let reloadStarts=0,reloadFailures=0;
 const reloadRequests=[];
 await page.route('**/api/maintenance',async route=>{
  const data=route.request().postDataJSON();
  reloadRequests.push(data);
  if(data.action==='job-start'&&data.request.operation==='delete')reloadStarts++;
  if(data.action==='job-status'&&data.id&&reloadFailures++===0){await route.abort('connectionfailed');return;}
  await route.continue();
 });
 await page.locator('[data-list-operation="delete"]').first().click();
 await page.locator('#deleteConfirmation .confirm-delete').click();
 await page.waitForFunction(()=>[...activeTasks.values()].some(t=>t.disconnected));
 const beforeReload=await page.evaluate(()=>({tasks:[...activeTasks.values()],saved:storedJobs()}));
 await page.reload();
 try{await page.waitForFunction(()=>inventoryReady&&!activeTasks.size&&inventory.filter(i=>i.present).length===1,{},{timeout:20000});}
 catch(error){console.error(JSON.stringify({beforeReload,reloadRequests,after:await page.evaluate(async()=>({inventoryReady,tasks:[...activeTasks.values()],items:inventory.map(i=>({id:i.id,present:i.present})),errors:[...viewErrors],saved:storedJobs(),message:el('message')?.textContent})),job:await page.evaluate(id=>request('job-status',{id}),beforeReload.tasks[0].id)},null,2));throw error;}
 assert.equal(reloadStarts,1);report.pageReloadResumesWriteWithoutResubmission=true;
 await page.unroute('**/api/maintenance');
 await page.waitForFunction(()=>backupData.some(b=>b.type==='quarantine'));
 const restoredId=await page.evaluate(()=>backupData.find(b=>b.type==='quarantine').id);
 await page.evaluate(async id=>executeOperation('restore',{id}),restoredId);
 await page.waitForFunction(()=>inventory.filter(i=>i.present).length===2&&!activeTasks.size);
 report.groupCacheTracksCheckResults=await page.evaluate(()=>{
  const id=inventory[0].id,job='fixture-partial';groupSkills();
  taskView({id:job,action:'check-many',state:'running',partial_checks:[{id,status:'latest',checked_at:'2099-01-01'}]});
  const updated=groupSkills().flatMap(i=>i._members).find(i=>i.id===id).update_check?.status==='latest';
  activeTasks.delete(job);storage();renderTasks();return updated;
 });
 assert.ok(report.groupCacheTracksCheckResults);
 report.explicitNullClearsOldCheck=await page.evaluate(()=>{
  const saved=structuredClone(inventorySnapshot),id=inventory[0].id;
  inventory[0].update_check={status:'update_available',checked_at:'2099-01-01',local_fingerprint:'old'};
  const incoming=structuredClone(saved);incoming.items.find(i=>i.id===id).update_check=null;
  incoming.cache.generation=inventoryRevision+1;loadInventory(incoming);
  return inventory.find(i=>i.id===id).update_check===null;
 });
 report.emptyScopePatchRemovesRow=await page.evaluate(()=>{
  const id=inventory[0].id,v=structuredClone(inventorySnapshot);
  v.items=[];v.scope={ids:[id],refreshed_ids:[]};v.cache.generation=inventoryRevision+1;
  loadInventory(v);return !inventory.some(i=>i.id===id);
 });
 assert.ok(report.explicitNullClearsOldCheck&&report.emptyScopePatchRemovesRow);
 // A resumed write queries its existing job and applies the returned row patch, never resubmits it.
 report.resumedWriteRefreshesRows=await page.evaluate(async()=>{
  const scope=viewScope(),id='fixture-resume',originalPoll=poll,originalRefresh=refreshAfterOperation;
  let refreshed=false;
  poll=async()=>({inventory_ids:['fixture-id'],state:'succeeded',action:'delete'});
  refreshAfterOperation=async v=>{refreshed=v.inventory_ids[0]==='fixture-id';};
  activeTasks.set(id,{id,write:true,action:'execute',ui_scope:scope,disconnected:true});
  try{renderTasks();if(!document.querySelector('[data-task-resume]'))return false;await reconnect(id);return refreshed;}
  finally{poll=originalPoll;refreshAfterOperation=originalRefresh;activeTasks.delete(id);renderTasks();}
 });
 assert.ok(report.resumedWriteRefreshesRows);
 await page.screenshot({path:output.replace(/\.json$/,'.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:output.replace(/\.json$/,'-mobile.png'),fullPage:true});
 assert.deepEqual(errors,[]);report.pageErrors=errors;
 fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));
}
main().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();server.kill('SIGINT');await new Promise(resolve=>server.exitCode!==null?resolve():server.once('exit',resolve));});
