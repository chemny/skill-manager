// All bindings and writes use a disposable fixture, never the user's running service.
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.ASM_PLAYWRIGHT_MODULE || 'playwright');
const repo=path.resolve(__dirname,'..'),output=process.argv[2],baseline=process.argv.includes('--baseline');
const server=spawn('python3',['-u','-c',`
import argparse,json,sys
sys.path[:0]=[${JSON.stringify(repo+'/tests')},${JSON.stringify(repo+'/scripts')}]
import test_maintenance as fixtures,maintenance,github_subset
import daily_recommendations
from test_daily_recommendations import remote as daily_remote
daily_recommendations.AnonymousGitHub.get=daily_remote
x=fixtures.MaintenanceTests();x.setUp()
for name in ('c','d','e'):x.skill(name,'fixture')
x.m.scan()
for name,code in (('a','remote_unavailable'),('b','remote_source_path'),('c','remote_rate_limit'),('d','remote_unavailable'),('e','remote_integrity')):
 row=x.m.select(name);x.m.bind_source(row,'https://github.com/example/skills','main',name if name!='b' else '')
 check={'id':row['id'],'status':'update_available' if name=='a' else 'check_failed','code':code,
        'checked_at':maintenance.stamp(),'comparison_source':x.m.source(row),'message':'fixture reason',
        'local_version':'1.0','remote_version':'2.0','local_fingerprint':maintenance.fingerprint(maintenance.manifest(x.roots/name,ignore_git=True))}
 if name=='a':check.update(attempt_failed=True,attempted_at=maintenance.stamp())
 if name=='b':check['source_issue']={'kind':'repository_root_not_skill','declared_path':'','candidate_paths':['skills/b'],'candidates_verified':False}
 if name=='c':
  import datetime
  check['retry_at']=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=60)).isoformat()
 x.m.db.execute('update maintenance_items set last_check=? where id=?',(json.dumps(check),row['id']))
x.m.db.commit();x.m.inventory()
def remote(self,row,folder):
 source=self.source(row)
 if row['name']=='b' and source.get('path')!='skills/b':raise github_subset.SourcePathError('', ['skills/b'])
 return x.roots/row['name'],source
maintenance.Manager.remote=remote
maintenance.Manager.import_usage=lambda self,*args,**kwargs:{'imported':False}
try:x.a.web_command(argparse.Namespace(host='127.0.0.1',port=0,open=False))
finally:x.tearDown()
`],{stdio:['ignore','pipe','pipe']});
let browser,stderr='';server.stderr.on('data',chunk=>stderr+=chunk);
async function main(){
 const url=await new Promise((resolve,reject)=>{
  let text='';const timer=setTimeout(()=>reject(Error('Fixture startup: '+stderr)),15000);
  server.stdout.on('data',chunk=>{text+=chunk;const match=text.match(/http:\/\/127\.0\.0\.1:\d+/);if(match){clearTimeout(timer);resolve(match[0]);}});
  server.on('exit',code=>{clearTimeout(timer);reject(Error('Fixture exited '+code+': '+stderr));});
 });
 browser=await chromium.launch({executablePath:process.env.ASM_BROWSER_EXECUTABLE || undefined,headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],requests=[];
 page.on('pageerror',error=>errors.push(String(error)));
 page.on('request',req=>{if(req.url().endsWith('/api/maintenance')&&req.method()==='POST')requests.push(req.postDataJSON());});
 await page.goto(url+'/manage');await page.waitForFunction(()=>inventoryReady&&inventory.length===5);
 await page.evaluate(()=>{tab('updates');el('updateView').value='';renderUpdates();});
 const row=name=>page.locator('#updateRows tr').filter({has:page.locator('[data-update-detail] .skill-name').filter({hasText:new RegExp('^'+name+'$')})});
 const report={kind:'isolated-update-reliability-ui',baseline};
 report.knownUpdateVisible=(await row('a').textContent()).includes('有更新');
 report.failedAttemptVisible=(await row('a').textContent()).includes('本次未核实');
 const sourceText=await row('b').textContent();
 report.sourceFailureVisible=sourceText.includes('需核对来源')&&sourceText.includes('找到新目录，待核对');
 report.rateLimitVisible=(await row('c').textContent()).includes('请求限流');
 report.bulkFailureRetryRemoved=await page.locator('#retryFailedChecks').count()===0;
 assert.equal(await page.locator('#updates .list-heading').count(),1);
 assert.equal(await page.locator('#discovery .list-heading').count(),1);
 assert.equal(await page.locator('#updateListResults').textContent(),'5');
 assert.equal(await page.locator('#updateListFailed').textContent(),'4');
 assert.equal(await page.locator('#updateFailureBreakdown').count(),0);
 const baselineState=await page.evaluate(()=>{const item={...inventory[0],_members:undefined,update_check:{status:'baseline_unknown'},source_readiness:'check_supported'};return {state:listUpdateState(item),badge:listUpdateBadge(item),action:checkAction(item),reason:updateChange(item)};});
 assert.equal(baselineState.state,'baseline_unknown');
 assert.match(baselineState.badge,/已查到来源，缺少可信比较基线/);
 assert.match(baselineState.action,/查看说明/);
 assert.match(baselineState.reason,/暂不能判断是否有更新/);
 assert.equal(await page.locator('#updateView option[value="baseline_unknown"]').count(),1);
 assert.equal(await page.locator('#statusFilter option[value="baseline_unknown"]').count(),1);
 const bid=await page.evaluate(()=>inventory.find(i=>i.name==='b').id);
 await page.evaluate(id=>openSourceEditor(id),bid);
 report.existingSourceEditable=await page.locator('#sourcePath').count()>0;
 report.candidateOffered=(await page.locator('#resultBody').textContent()).includes('skills/b');
 report.noAutomaticBinding=!requests.some(r=>r.request?.action==='source-bind');
 if(!baseline){
  assert.ok(report.knownUpdateVisible&&report.failedAttemptVisible&&report.sourceFailureVisible&&report.rateLimitVisible,JSON.stringify(report));
  assert.ok(report.bulkFailureRetryRemoved&&report.existingSourceEditable&&report.candidateOffered&&report.noAutomaticBinding);
  await page.locator('[data-remote-candidate="0"]').click();
  assert.equal(await page.locator('#sourcePath').inputValue(),'skills/b');
  assert.ok(!requests.some(r=>r.request?.action==='source-bind'));
  await page.locator('#bind').click();
  await page.waitForFunction(()=>!activeTasks.size&&!el('resultDialog').open);
  report.correctedPath=await page.evaluate(id=>inventory.find(i=>i.id===id).source.path,bid);
  assert.equal(report.correctedPath,'skills/b');
  await page.evaluate(id=>checkMany([id]),bid);
  report.correctedSourceChecksSuccessfully=await page.evaluate(id=>inventory.find(i=>i.id===id).update_check.status==='latest',bid);
  assert.ok(report.correctedSourceChecksSuccessfully);
  const start=requests.length;
  await row('d').locator('[data-update-check]').click();
  await page.waitForFunction(()=>!activeTasks.size);
  const retry=requests.slice(start).find(r=>r.action==='job-start'&&r.request.action==='check-many');
  assert.ok(retry);
  report.retryNames=await page.evaluate(ids=>ids.map(id=>inventory.find(i=>i.id===id).name).sort(),retry.request.ids);
  assert.deepEqual(report.retryNames,['d']);
  await page.evaluate(()=>tab('skills'));
  const installedTotal=await page.locator('#libraryTotal').textContent();
  await page.locator('#filter').fill('no-matching-skill');
  assert.equal(await page.locator('#libraryTotal').textContent(),installedTotal);
  assert.equal(await page.locator('#libraryMatched').textContent(),'0');
  assert.equal(await page.locator('#libraryMatchSummary').isVisible(),true);
  report.stableListTotal=true;
  await page.evaluate(()=>tab('settings','taxonomy'));
  assert.equal(await page.locator('#autoClassify').innerText(),'自动分类');
  assert.equal(await page.locator('#autoTag').innerText(),'自动打标签');
  assert.equal(await page.locator('#undoClassification').count(),0);
  await page.locator('#autoClassify').click();
  await page.waitForFunction(()=>!activeTasks.size&&!buttonPending.get(el('autoClassify')));
  await page.locator('#autoTag').click();
  await page.waitForFunction(()=>!activeTasks.size&&!buttonPending.get(el('autoTag')));
  assert.deepEqual(requests.filter(r=>r.action==='job-start'&&r.request.action==='classification-apply').map(r=>r.request.field),['category','tags']);
  await page.evaluate(()=>{activeTasks.set('classification-ui-test',{id:'classification-ui-test',action:'classification-apply',field:'category',state:'queued'});setBusy();});
  assert.match(await page.locator('#autoClassify').innerText(),/等待处理/);
  assert.equal(await page.locator('#autoClassify .loading-ring').count(),1);
  assert.equal(await page.locator('#autoTag').isDisabled(),true);
  await page.evaluate(()=>{activeTasks.set('classification-ui-test',{id:'classification-ui-test',action:'classification-apply',field:'tags',state:'running'});setBusy();});
  assert.match(await page.locator('#autoTag').innerText(),/处理中/);
  assert.equal(await page.locator('#autoClassify').innerText(),'自动分类');
  await page.evaluate(()=>{activeTasks.delete('classification-ui-test');setBusy();});
  assert.equal(await page.locator('#autoClassify').innerText(),'自动分类');
  report.classificationProgress=true;
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:output.replace(/\.json$/,'.png'),fullPage:true});
  report.mobileControlsFit=await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth);
  assert.ok(report.mobileControlsFit);
 }
 report.pageErrors=errors;assert.deepEqual(errors,[]);
 fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));
}
main().catch(error=>{console.error(error);console.error(stderr);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();server.kill('SIGTERM');});
