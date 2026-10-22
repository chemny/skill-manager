// Full workflow against disposable projects; never scans or changes real user projects.
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict'),path=require('node:path');
const {chromium}=require(process.env.ASM_PLAYWRIGHT_MODULE||'playwright');
const repo=path.resolve(__dirname,'..');
const server=spawn('python3',['-B','-u','-c',`
import argparse,sys,os,time
sys.path[:0]=[${JSON.stringify(repo+'/tests')},${JSON.stringify(repo+'/scripts')}]
import test_maintenance as fixtures,skill_discovery,daily_recommendations,project_space,project_analysis
from test_daily_recommendations import remote
daily_recommendations.AnonymousGitHub.get=remote
skill_discovery.fetch_skillhub=lambda *args:skill_discovery.Catalog([],0,[])
skill_discovery.fetch=lambda *args:[]
x=fixtures.MaintenanceTests();x.setUp()
root=x.base/'projects';root.mkdir()
first=root/'video-project';first.mkdir();(first/'AGENTS.md').write_text('project')
(first/'draft-1.png').write_bytes(b'preview');(first/'draft-2.png').write_bytes(b'preview')
(first/'report.html').write_text('<img src="draft-1.png"><img src="draft-2.png">')
cache=first/'__pycache__';cache.mkdir();p=cache/'render.pyc';p.write_bytes(b'cache');old=time.time()-9*86400;os.utime(p,(old,old))
(first/'render.py').write_text('print(1)')
second=root/'model-project';second.mkdir();(second/'weights.gguf').write_bytes(b'weights')
third=root/'claude-project';third.mkdir();(third/'notes.txt').write_text('keep this original')
project_space.discover=lambda home:([('codex',str(first)),('codex',str(second)),('claude_code',str(third))],[])
real_worker=project_analysis.run_worker
def slow(*args):
 time.sleep(.4)
 return real_worker(*args)
project_analysis.run_worker=slow
try:x.a.web_command(argparse.Namespace(host='127.0.0.1',port=0,open=False))
finally:x.tearDown()
`],{stdio:['ignore','pipe','pipe']});
let browser,stderr='';server.stderr.on('data',value=>stderr+=value);
async function main(){
 const url=await new Promise((resolve,reject)=>{let text='';const timer=setTimeout(()=>reject(Error(stderr)),15000);server.stdout.on('data',value=>{text+=value;const found=text.match(/http:\/\/127\.0\.0\.1:\d+/);if(found){clearTimeout(timer);resolve(found[0])}})});
 browser=await chromium.launch({executablePath:process.env.ASM_BROWSER_EXECUTABLE||undefined,headless:true});
 const page=await browser.newPage({viewport:{width:1920,height:1080}}),errors=[],requests=[];
 page.on('pageerror',error=>errors.push(String(error)));page.on('request',request=>{if(request.method()==='POST'&&request.url().endsWith('/api/maintenance'))requests.push(request.postDataJSON())});
 const requested=action=>requests.filter(payload=>payload.request?.action===action),rows=()=>page.locator('#diskRows tr[data-project]');
 async function waitScan(){await page.waitForFunction(()=>diskReport?.id&&diskReport.state!=='running'&&!diskStarting)}
 async function analyze(){await page.locator('#diskAnalyzeSelected').click();await page.waitForFunction(()=>!projectStarting&&diskBatch.completed?.length>0);await page.waitForFunction(()=>document.getElementById('diskAnalyzeSelected').textContent==='查看清理建议')}
 async function choosePlans(){await page.locator('#diskAnalyzeSelected').click();await page.waitForFunction(()=>spaceMode==='advice');await page.locator('#spaceSelectPage').check()}
 async function move(){await page.locator('#spaceMove').click();await page.locator('#deleteConfirmation .confirm-delete').click();await page.waitForFunction(()=>spaceAdviceData.groups.length===0)}
 await page.goto(url+'/manage');await page.waitForFunction(()=>inventoryReady);
 await page.locator('.sidebar [data-tab="space"]').click();
 assert.equal(await page.locator('#diskAgent').isVisible(),false);assert.equal(await page.locator('#diskRescan').isVisible(),false);assert.equal(await page.locator('#space [role="tablist"]').count(),0);
 await page.locator('#diskStart').click();await page.locator('[data-scope-agent="codex"]').waitFor();
 assert.equal(await page.locator('[data-scope-agent="codex"]').isChecked(),true);
 await page.locator('[data-project-manage="codex"]').click();
 const model=page.locator('.project-scope-item').filter({hasText:'model-project'}).locator('input');
 await model.uncheck();await page.locator('#projectScanCancel').click();assert.equal(requested('disk-scan').length,0);
 await page.locator('#diskStart').click();await page.locator('[data-project-manage="codex"]').click();assert.equal(await model.isChecked(),true,'Cancel must discard scope edits');
 await model.uncheck();await page.locator('[data-scope-agent="claude_code"]').check();await page.screenshot({path:'/tmp/skill-manager-space-scope-desktop.png'});
 await page.locator('#projectScanConfirm').click();await waitScan();
 assert.equal(await rows().count(),2);assert.equal((await page.locator('#diskRows').innerText()).includes('model-project'),false);
 assert.deepEqual((await page.locator('#diskAgent option').allTextContents()).sort(),['全部 Agent','Claude Code','Codex'].sort());
 assert.ok((await page.locator('#diskSize').innerText()).startsWith('占用'));assert.equal(await page.locator('#diskRows button').count(),0);assert.equal(await page.locator('#deleteConfirmation').evaluate(node=>node.open),false);
 await page.locator('#diskRescan').click();await waitScan();assert.equal(await rows().count(),2);assert.equal(await page.locator('#projectScanDialog').evaluate(node=>node.open),false);
 await page.locator('#diskSelectPage').check();await page.mouse.move(0,0);
 assert.ok(await page.locator('#diskRows .project-name').evaluateAll(nodes=>nodes.every(node=>getComputedStyle(node).fontWeight==='400')));assert.ok(await rows().evaluateAll(nodes=>nodes.every(node=>getComputedStyle(node).backgroundColor==='rgba(0, 0, 0, 0)')));
 await page.locator('#diskAnalyzeSelected').click();await page.locator('.project-state progress').first().waitFor({state:'visible'});
 assert.ok(await rows().evaluateAll(nodes=>nodes.every(node=>node.getBoundingClientRect().height<=48)));await page.screenshot({path:'/tmp/skill-manager-space-analysis-progress.png'});
 await page.reload();await page.waitForFunction(()=>activeTab==='space'&&diskReport?.projects.length===2);await page.waitForFunction(()=>diskReport.projects.every(project=>project.analysis?.scan_id));await page.waitForFunction(()=>document.getElementById('diskAnalyzeSelected').textContent==='查看清理建议');assert.equal(await page.locator('#diskPanel').isVisible(),true);
 await page.locator('#diskStart').click();await page.locator('[data-project-manage="codex"]').click();assert.equal(await model.isChecked(),false,'Saved exclusions must survive reload');await page.keyboard.press('Escape');assert.equal(await page.evaluate(()=>diskSelected.size),2,'Closing scope dialog must not clear list selection');
 for(const width of [1920,1440,1100,824,390,360]){await page.setViewportSize({width,height:width>=1440?1080:844});await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.ok(await rows().evaluateAll(nodes=>nodes.every(node=>node.getBoundingClientRect().height<=48)),'One line at '+width);assert.ok(await page.locator('#diskAnalyzeSelected').evaluate(node=>{const r=node.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight}),'Batch action inside viewport at '+width);if(width===390)await page.screenshot({path:'/tmp/skill-manager-space-list-mobile.png'})}
 await page.setViewportSize({width:1920,height:1080});await page.screenshot({path:'/tmp/skill-manager-space-list-desktop.png'});
 await page.locator('#diskAnalyzeSelected').click();await page.waitForFunction(()=>spaceMode==='advice');assert.equal(await page.locator('#spacePlanRows tr[data-project-plan]').count(),2);assert.equal(await page.locator('#spacePlanRows input:checked').count(),0);assert.ok((await page.locator('#spacePlanRows').innerText()).includes('可能无法显示'));assert.equal(await page.locator('#spacePlanRows button').count(),0);
 await page.locator('#spaceSelectPage').check();await page.screenshot({path:'/tmp/skill-manager-space-advice-desktop.png'});
 await page.locator('#spaceMove').click();await page.locator('#deleteConfirmation [data-cancel-delete]').click();assert.equal(requested('space-trash-batch').length,0);await move();
 await page.locator('.sidebar [data-tab="trash"]').click();await page.locator('#projectTrashRows tr[data-recycle]').first().waitFor();assert.equal(await page.locator('.sidebar [data-tab="trash"]').count(),1);await page.locator('#projectTrashKind').selectOption('projects');assert.equal(await page.locator('#projectTrashRows tr[data-recycle]').count(),2);
 await page.locator('#projectTrashSelectPage').check();await page.locator('#projectTrashRestore').click();await page.waitForFunction(()=>artifactTrash.filter(item=>item.state==='available').length===0);
 await page.locator('.sidebar [data-tab="space"]').click();await page.locator('#spaceAdviceBack').click();await page.locator('#diskRows tr[data-project]').filter({hasText:'claude-project'}).locator('input').uncheck();
 await analyze();await choosePlans();await move();
 await page.locator('.sidebar [data-tab="trash"]').click();await page.locator('#projectTrashRows tr[data-recycle]').first().waitFor();await page.locator('#projectTrashSelectPage').check();await page.screenshot({path:'/tmp/skill-manager-space-trash-desktop.png'});
 await page.locator('#projectTrashPurge').click();await page.locator('#deleteConfirmation [data-cancel-delete]').click();assert.equal(requested('trash-purge-many').length,0);
 await page.locator('#projectTrashPurge').click();await page.locator('#deleteConfirmation .confirm-delete').click();await page.waitForFunction(()=>artifactTrash.filter(item=>item.state==='available').length===0);
 assert.deepEqual(errors,[]);console.log(JSON.stringify({scopedScan:true,rememberExclusions:true,rescanOnlyAdded:true,batchAnalysis:true,resumeAfterReload:true,singleLineRegularWeight:true,checkboxOnlySelection:true,groupedImpacts:true,cancelDoesNotMove:true,restore:true,confirmedPurge:true,viewports:[1920,1440,1100,824,390,360],pageErrors:errors}));
}
main().catch(async error=>{console.error(error,stderr);const page=browser?.contexts()[0]?.pages()[0];if(page){console.error(await page.evaluate(()=>({view:activeTab,mode:spaceMode,report:diskReport,batch:diskBatch,selection:[...diskSelected],tasks:[...activeTasks.values()],error:lastErrorData})));await page.screenshot({path:'/tmp/skill-manager-space-test-failure.png'})}process.exitCode=1}).finally(async()=>{if(browser)await browser.close();server.kill('SIGTERM')});
