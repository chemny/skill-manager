// Real writes use only the temporary Python fixture. Advice failures use a controlled clock.
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.ASM_PLAYWRIGHT_MODULE || 'playwright');
const repo=path.resolve(__dirname,'..'),output=process.argv[2];
const server=spawn('python3',['-u','-c',`
import argparse,sys
sys.path[:0]=[${JSON.stringify(repo+'/tests')},${JSON.stringify(repo+'/scripts')}]
import test_maintenance as fixtures,maintenance
import daily_recommendations
from test_daily_recommendations import remote as daily_remote
daily_recommendations.AnonymousGitHub.get=daily_remote
x=fixtures.MaintenanceTests();x.setUp()
for agent in ('codex','claude_code'):x.a.update_source('add',agent,str(x.roots))
x.m.scan()
candidate=x.base/'.devin/skills/example';candidate.mkdir(parents=True)
(candidate/'SKILL.md').write_text('---\\nname: example\\n---\\n')
import skill_locations
original_candidate_scan=skill_locations.discover_agent_candidates
skill_locations.discover_agent_candidates=lambda config:original_candidate_scan(config,home=x.base)
x.m.backup(x.m.select(str(x.roots/'a'),'shared'))
(x.roots/'a/SKILL.md').write_text('---\\nname: a\\nversion: 1.1\\n---\\nchanged after backup')
x.m.inventory()
maintenance.Manager.import_usage=lambda self,*args,**kwargs:{'imported':False,'fixture':True}
try:x.a.web_command(argparse.Namespace(host='127.0.0.1',port=0,open=False))
finally:x.tearDown()
`],{stdio:['ignore','pipe','pipe']});
let browser,stderr='';server.stderr.on('data',chunk=>stderr+=chunk);
async function main(){
 const url=await new Promise((resolve,reject)=>{
  let data='';const timer=setTimeout(()=>reject(Error('Fixture startup timed out: '+stderr)),15000);
  server.stdout.on('data',chunk=>{data+=chunk;const match=data.match(/http:\/\/127\.0\.0\.1:\d+/);if(match){clearTimeout(timer);resolve(match[0]);}});
  server.on('exit',code=>{clearTimeout(timer);reject(Error('Fixture exited '+code+': '+stderr));});
 });
 browser=await chromium.launch({executablePath:process.env.ASM_BROWSER_EXECUTABLE || undefined,headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],requests=[],report={kind:'isolated-management-boundaries'};
 page.on('pageerror',error=>errors.push(String(error)));
 page.on('request',req=>{if(req.url().endsWith('/api/maintenance')&&req.method()==='POST')requests.push(req.postDataJSON());});
 await page.goto(url+'/manage');await page.waitForFunction(()=>inventoryReady&&inventory.length===6);
 await page.evaluate(()=>tab('backups'));await page.waitForFunction(()=>backupsLoaded&&backupAdviceWorkers===0);
 assert.deepEqual(await page.locator('#backupTopActions > button:visible, #backupTopActions > details:visible > summary').allTextContents(),['扫描','导入','导出 ▾']);
 await page.locator('#backupMore summary').click();
 await page.locator('#backupMore [data-scan-custom]').waitFor({state:'visible'});
 assert.equal(await page.locator('#backupMore #createSnapshot').isVisible(),true);
 assert.equal(await page.locator('#backupMore [data-backup-agent]').count(),await page.evaluate(()=>agentKeys().length));
 await page.locator('#backupMore summary').click();
 assert.equal(await page.locator('#createSnapshot').isVisible(),false);
 report.backupExportMenu=true;
 const good=await page.evaluate(()=>backupData.find(b=>b.type==='backup').id);
 await page.evaluate(id=>openBackupDetail(id),good);
 const detailText=await page.locator('#detail').innerText();
 for(const removed of ['最近读取记录','技术信息','能否恢复','恢复与删除','同名匹配仅供比较','同步基线'])assert.ok(!detailText.includes(removed),removed);
 assert.ok(detailText.includes('删除后进入回收站，可以恢复'));
 assert.equal(await page.locator('#detail [data-restore]').count(),1);
 assert.equal(await page.locator('#detail details[open]').count(),0);
 await page.screenshot({path:output.replace(/\.json$/,'-backup.png')});
 await page.evaluate(async id=>{
  const original=browse;
  const data=await original('backup-detail',{id});
  try{
   browse=async(action,payload)=>action==='backup-detail'?{...data,type:'quarantine',retained:true,valid:false,restore_status:'unsupported',status_detail:'文件校验未通过'}:original(action,payload);
   await openBackupDetail(id);
  }finally{browse=original;}
 },good);
 assert.ok((await page.locator('#detail').innerText()).includes('永久删除后无法恢复'));
 assert.ok((await page.locator('#detail').innerText()).includes('文件校验未通过'));
 assert.equal(await page.locator('#detail [data-restore],#detail [data-purge]').count(),0);
 await page.evaluate(()=>closeDetail());
 assert.equal(await page.evaluate(()=>backupAdviceText({summary:'与安装项内容不同'})),'备份与当前内容不同，请确认是否需要保留');
 assert.equal(await page.evaluate(()=>sourceTraceLabel('cooldown')),'暂时无法查询');
 report.conciseBackupCopy=true;
 await page.evaluate(()=>{window.testMessages=[];const original=message;message=(...args)=>{window.testMessages.push(String(args[0]));return original(...args)};});
 await page.evaluate(id=>{
  const b=backupData.find(b=>b.id===id);
  backupData.push({...b,id:'unsupported-fixture',name:'unreadable archive',restore_status:'unsupported',status_detail:'归档格式不支持恢复'});
  backupSelected=new Set([id,'unsupported-fixture']);renderBackups();
 },good);
 assert.equal(await page.locator('#restoreSelected').isEnabled(),true);
 await page.locator('#restoreSelected').click();await page.locator('#recoveryDialog').waitFor({state:'visible'});
 assert.equal(await page.locator('#recoveryTitle').textContent(),'恢复并替换');
 await page.locator('#recoveryContinue').click();
 await page.waitForFunction(()=>!activeTasks.size&&window.testMessages.some(m=>m.includes('跳过 1 项'))).catch(async error=>{console.error(await page.evaluate(()=>({messages:window.testMessages,error:el('toastText').textContent,tasks:activeTasks.size})));throw error;});
 const submissions=requests.filter(r=>r.action==='job-start'&&r.request.action==='execute-restore-many');
 assert.equal(submissions.length,1);assert.deepEqual(submissions[0].request.entries,[{id:good}]);
 report.mixedRestore={submitted:submissions.length,skippedRemainsSelected:await page.evaluate(()=>backupSelected.has('unsupported-fixture')),message:await page.evaluate(()=>window.testMessages.find(m=>m.includes('跳过 1 项'))),restored:(await page.evaluate(id=>browse('backup-detail',{id}),good)).comparison_state==='identical'};
 assert.ok(report.mixedRestore.skippedRemainsSelected&&report.mixedRestore.restored);
 assert.match(report.mixedRestore.message,/归档格式不支持恢复/);
 assert.ok(!await page.locator('#deleteConfirmation').evaluate(n=>n.open));
 const jobsBefore=requests.filter(r=>r.action==='job-start').length;
 await page.evaluate(id=>restoreBackups([id,'unsupported-fixture']),good);
 report.identicalRestoreNoWrite=requests.filter(r=>r.action==='job-start').length===jobsBefore;
 assert.ok(report.identicalRestoreNoWrite);
 await page.evaluate(()=>{backupSelected=new Set(['unsupported-fixture','missing-fixture']);renderBackups();});
 report.allUnsupportedDisabled=await page.locator('#restoreSelected').isDisabled();assert.ok(report.allUnsupportedDisabled);
 await page.evaluate(()=>{backupData=backupData.filter(b=>b.id!=='unsupported-fixture');backupSelected.clear();renderBackups();});

 const inventoryStarts=()=>requests.filter(r=>r.action==='job-start'&&r.request.action==='inventory');
 const beforeRemove=inventoryStarts().length;
 await page.evaluate(()=>tab('settings','scan'));
 await page.locator('#rootRows .agent-directory-section').first().waitFor();
 assert.match(await page.locator('#rootRows [data-configured-agent="shared"]').innerText(),/公共目录/);
 assert.equal(await page.locator('#rootRows .directory-choice-row,#rootRows [data-remove-root]').count(),0);
 assert.equal(await page.locator('#rootRows [data-agent-directories]').count(),await page.locator('#rootRows .agent-directory-section').count());
 assert.match(await page.locator('#rootRows [data-configured-agent="codex"]').innerText(),/Codex.*目录.*移除目录.*移除 Agent/s);
 assert.ok(await page.locator('#rootRows [data-configured-agent="codex"]').evaluate(n=>n.getBoundingClientRect().height<70));
 await page.locator('#rootRows [data-agent-directories="codex"]').click();
 await page.locator('#directoryDialog').waitFor({state:'visible'});
 assert.ok(await page.locator('#directoryDialog [data-remove-root]').count()>0);
 assert.equal(await page.locator('#directoryDialog .directory-choice-actions .muted').count(),await page.evaluate(()=>scanLocationGroups.filter(g=>g.group[0].platform==='codex'&&!g.removable.length).length));
 await page.screenshot({path:output.replace(/\.json$/,'-directories-dialog.png')});
 const rootsBefore=await page.locator('#directoryDialog .directory-choice-row').count();
 await page.locator('#directoryDialog [data-remove-root]').first().click();
 await page.locator('#deleteConfirmation').waitFor({state:'visible'});
 await page.locator('#deleteConfirmation [data-cancel-delete]').click();
 assert.equal(await page.locator('#directoryDialog .directory-choice-row').count(),rootsBefore);
 await page.locator('#closeDirectoryDialog').click();
 assert.equal(await page.locator('#rootRows .available-agents').evaluate(n=>Boolean(n.compareDocumentPosition(document.querySelector('#rootRows .agent-directory-section')) & Node.DOCUMENT_POSITION_PRECEDING)),true);
 await page.screenshot({path:output.replace(/\.json$/,'-directories-desktop.png')});
 await page.setViewportSize({width:390,height:844});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 assert.ok(await page.locator('#rootRows [data-configured-agent="codex"]').evaluate(n=>n.getBoundingClientRect().height<70));
 await page.screenshot({path:output.replace(/\.json$/,'-directories-mobile.png')});
 await page.setViewportSize({width:1440,height:1000});
 report.directoryManagement=true;
 await page.evaluate(()=>selectSetting('channels'));
 await page.locator('#channelRows .channel-row').first().waitFor();
 const channelCount=await page.locator('#channelRows .channel-row').count();
 assert.equal(await page.locator('#channelRows [data-channel-test]').count(),channelCount);
 assert.equal(await page.locator('#channelRows [data-channel-search]').count(),await page.evaluate(()=>updateChannels.filter(c=>c.search_supported).length));
 assert.equal(await page.locator('#channelRows .channel-availability').count(),channelCount);
 assert.match(await page.locator('#channelRows .channel-row:has([data-channel-test="gitcode"])').innerText(),/仅仓库更新/);
 assert.equal(await page.locator('#channelRows [data-channel-search="gitcode"]').count(),0);
 assert.equal(await page.locator('#channelRows [data-channel-search="gitlab"]').count(),1);
 assert.equal(await page.locator('#channelRows .channel-row:has([data-channel-test="find-skills"])').count(),0);
 await page.locator('#channelRows [data-channel-test="local"]').click();
 await page.waitForFunction(()=>document.querySelector('#channel-test-local')?.textContent.includes('无需网络'));
 assert.match(await page.locator('#channelRows .channel-row:has([data-channel-test="local"]) .channel-availability').innerText(),/本机记录/);
 report.channelCapabilities=true;
 await page.evaluate(()=>selectSetting('scan'));
 await page.locator('[data-agent-remove="codex"]').click();await page.locator('#deleteConfirmation .confirm-delete').click();
 await page.waitForFunction(()=>!inventory.some(i=>i.platform==='codex')&&inventory.length===4&&inventorySnapshot.cache.recheck_ids?.length===4);
 await page.evaluate(()=>tab('skills'));
 await page.waitForFunction(()=>!inventoryRefreshes.size&&inventorySnapshot.cache.recheck_ids?.length===0);
 const cached=await page.evaluate(()=>request('inventory-cached'));
 const removedScope=inventoryStarts().slice(beforeRemove).map(r=>r.request);
 assert.ok(cached.cache.available);assert.equal(cached.items.length,4);
 assert.equal(removedScope.length,1);
 assert.ok(removedScope.every(r=>r.lightweight&&Array.isArray(r.ids)&&r.ids.length===4));
 const consumers=await page.evaluate(()=>browse('uninstall-references',{ids:[inventory[0].id]}));
 assert.ok(Object.values(consumers.references).flat().some(r=>r.platform==='codex'));
 report.agentRemoval={cacheAvailable:cached.cache.available,remainingReferences:cached.items.length,fullRebuilds:removedScope.filter(r=>!Array.isArray(r.ids)).length,scopedRefreshes:removedScope.length,removedConsumerStillChecked:true};
 await page.evaluate(()=>openAgentEditor());
 await page.locator('[data-remove-preset="claude_code"]').click();await page.locator('#deleteConfirmation .confirm-delete').click();
 await page.waitForFunction(()=>!inventory.some(i=>i.platform==='claude_code')&&inventory.length===2&&inventorySnapshot.cache.recheck_ids?.length===0&&!inventoryRefreshes.size);
 await page.locator('#recoveryCancel').click();await page.waitForFunction(()=>!inventoryRefreshes.size);
 report.pickerRemovalKeepsRemainingRows=await page.locator('#items tr').count()===2;
 assert.ok(report.pickerRemovalKeepsRemainingRows);
 await page.reload();await page.waitForFunction(()=>inventoryReady&&inventory.length===2);
 report.reloadKeepsSnapshot=(await page.evaluate(()=>request('inventory-cached'))).cache.available;
 assert.ok(report.reloadKeepsSnapshot);

 await page.evaluate(()=>tab('backups'));await page.waitForFunction(()=>backupAdviceWorkers===0);
 await page.clock.install();
 await page.clock.pauseAt(await page.evaluate(()=>Date.now()+1000));
 await page.evaluate(()=>{
  window.originalAdviceBrowse=browse;window.adviceCalls=0;
  backupData=backupData.slice(0,1);backupAdviceCache.clear();backupComparisons.clear();backupAdviceFailures.clear();
  browse=async(action,data)=>{if(action!=='backup-advice')return originalAdviceBrowse(action,data);adviceCalls++;if(adviceCalls===1)throw Error('Injected transient connection failure');return {items:data.ids.map(id=>({id,cleanup_advice:{code:'review',reason:'已完成比较，证据不足'}}))};};
  renderBackups();
 });
 await page.waitForFunction(()=>backupAdviceWorkers===0&&adviceCalls===1);
 assert.equal(await page.evaluate(()=>backupAdviceCache.size),0);
 assert.equal((await page.locator('[data-backup-advice]').first().textContent()).trim(),'—');
 assert.match(await page.locator('[data-backup-advice]').first().getAttribute('title'),/Injected transient connection failure/);
 await page.clock.runFor(29999);assert.equal(await page.evaluate(()=>adviceCalls),1);
 await page.clock.runFor(1);await page.waitForFunction(()=>backupAdviceWorkers===0&&adviceCalls===2);
 await page.evaluate(()=>scheduleBackupAdvice());assert.equal(await page.evaluate(()=>adviceCalls),2);
 report.adviceRetry={firstFailureCachedAsSuccess:false,recoveredAfterSeconds:30,requests:2,actualReviewCached:await page.evaluate(()=>backupAdviceCache.values().next().value.code==='review')};
 assert.ok(report.adviceRetry.actualReviewCached);
 await page.evaluate(()=>{
  adviceCalls=0;backupAdviceCache.clear();backupAdviceFailures.clear();
  browse=async()=>{adviceCalls++;throw Error('Injected persistent failure');};scheduleBackupAdvice();
 });
 await page.waitForFunction(()=>backupAdviceWorkers===0&&adviceCalls===1);
 await page.clock.runFor(30000);await page.waitForFunction(()=>backupAdviceWorkers===0&&adviceCalls===2);
 await page.clock.runFor(59999);assert.equal(await page.evaluate(()=>adviceCalls),2);
 await page.clock.runFor(1);await page.waitForFunction(()=>backupAdviceWorkers===0&&adviceCalls===3);
 report.persistentFailureBackoff={requests:3,retryDelaysSeconds:[30,60],noSuccessCache:await page.evaluate(()=>backupAdviceCache.size===0)};
 assert.ok(report.persistentFailureBackoff.noSuccessCache);
 await page.evaluate(()=>{
  backupAdviceFailures.clear();backupAdviceCache.clear();adviceCalls=0;
  browse=async()=>{adviceCalls++;return {items:[]};};scheduleBackupAdvice();
 });
 await page.waitForFunction(()=>backupAdviceWorkers===0&&adviceCalls===1);
 report.incompleteResponseRetries=await page.evaluate(()=>backupAdviceCache.size===0&&backupAdviceFailures.size===1);
 assert.ok(report.incompleteResponseRetries);
 await page.evaluate(()=>{
  backupAdviceFailures.clear();adviceCalls=0;
  browse=async(action,data)=>{adviceCalls++;return {items:data.ids.map(id=>({id,cleanup_advice:null,error:'Injected per-item read failure'}))};};scheduleBackupAdvice();
 });
 await page.waitForFunction(()=>backupAdviceWorkers===0&&adviceCalls===1);
 report.perItemFailureRetries=await page.evaluate(()=>backupAdviceCache.size===0&&[...backupAdviceFailures.values()].every(f=>f.reason.includes('Injected per-item read failure')));
 assert.ok(report.perItemFailureRetries);
 await page.evaluate(()=>{activeTab='skills';scheduleBackupAdvice();});
 await page.clock.runFor(300000);assert.equal(await page.evaluate(()=>adviceCalls),1);
 report.inactivePageDoesNotRetry=true;
 await page.evaluate(()=>tab('settings','scan'));
 await page.locator('#rootRows [data-agent-scan]').waitFor();
 assert.equal(await page.locator('#rootRows .available-agents').evaluate(n=>getComputedStyle(n).gridTemplateColumns.split(' ').length),3);
 assert.equal(await page.locator('#rootRows .available-agent-row span').count(),await page.locator('#rootRows .available-agent-row').count());
 assert.equal(await page.locator('#rootRows .available-agent-row').first().innerText(),await page.locator('#rootRows .available-agent-row strong').first().innerText());
 const beforeScan=await page.evaluate(()=>endpoint('/api/sources'));
 await page.locator('#rootRows [data-agent-scan]').click();
 await page.locator('#rootRows [data-agent-candidate]').waitFor();
 assert.match(await page.locator('#rootRows .agent-scan-results').innerText(),/Devin/);
 assert.equal((await page.evaluate(()=>endpoint('/api/sources'))).agents.some(a=>a.id==='devin'&&a.added),false);
 await page.locator('#rootRows [data-agent-candidate]').click();
 await page.waitForFunction(()=>agentCatalog.some(a=>a.id==='devin'&&a.added));
 assert.equal((await page.evaluate(()=>endpoint('/api/sources'))).agents.some(a=>a.id==='devin'&&a.added),true);
 report.agentCandidateScan={readOnlyBeforeAdd:!beforeScan.agents.some(a=>a.id==='devin'&&a.added),addedOnChoice:true};
 assert.deepEqual(errors,[]);report.pageErrors=errors;
 fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
 if(browser)await browser.close();server.kill('SIGINT');
 await new Promise(resolve=>server.exitCode!==null?resolve():server.once('exit',resolve));
});
