// Real deletion is restricted to the fixture's temporary backups.
const {spawn}=require('node:child_process');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.ASM_PLAYWRIGHT_MODULE||'playwright');
const repo=path.resolve(__dirname,'..'),output=process.argv[2];
const server=spawn('python3',['-u','-c',`
import argparse,sys,time
sys.path[:0]=[${JSON.stringify(repo+'/tests')},${JSON.stringify(repo+'/scripts')}]
import test_maintenance as fixtures,maintenance,recovery,skill_discovery,daily_recommendations
from test_daily_recommendations import remote
daily_recommendations.AnonymousGitHub.get=remote
skill_discovery.fetch_skillhub=lambda *args:skill_discovery.Catalog([],0,[])
skill_discovery.fetch=lambda *args:[]
x=fixtures.MaintenanceTests();x.setUp()
ids=[x.m.backup(x.row('a'))['id'] for _ in range(31)]
maintenance.dispatch(x.m,{'action':'execute-backups','operation':'trash-backup','ids':ids,'confirmed':True})
recovery.retain(x.m,ids[-1],True)
original=maintenance.Manager.apply
def slow(self,pid,*args,**kwargs):
 if self.get_plan(pid)['action']=='purge':time.sleep(.4)
 return original(self,pid,*args,**kwargs)
maintenance.Manager.apply=slow
original_discover=recovery.discover
def slow_discover(m,extra=None):
 m.batch_progress(0,None,'已检查 0 个位置 · 发现 0 项备份')
 time.sleep(4)
 return original_discover(m,extra)
recovery.discover=slow_discover
maintenance.Manager.import_usage=lambda *args,**kwargs:{'imported':False}
try:x.a.web_command(argparse.Namespace(host='127.0.0.1',port=0,open=False))
finally:x.tearDown()
`],{stdio:['ignore','pipe','pipe']});
let browser,stderr='';server.stderr.on('data',v=>stderr+=v);
async function main(){
 const url=await new Promise((resolve,reject)=>{let data='';const timer=setTimeout(()=>reject(Error(stderr||'Startup timed out')),20000);server.stdout.on('data',v=>{data+=v;const m=data.match(/http:\/\/127\.0\.0\.1:\d+/);if(m){clearTimeout(timer);resolve(m[0]);}});server.on('exit',code=>reject(Error('Fixture exited '+code+stderr)));});
 browser=await chromium.launch({executablePath:process.env.ASM_BROWSER_EXECUTABLE||undefined,headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:900},hasTouch:true}),errors=[],requests=[],report={};
 page.on('pageerror',e=>errors.push(String(e)));
 page.on('request',r=>{if(r.url().endsWith('/api/maintenance')&&r.method()==='POST')requests.push(r.postDataJSON());});
 await page.route('**/api/maintenance',async route=>{const p=route.request().postDataJSON();if(p.action==='job-start'&&p.request?.action==='empty-trash')await new Promise(r=>setTimeout(r,800));await route.continue();});
 await page.goto(url+'/manage');await page.waitForFunction(()=>inventoryReady);
 await page.evaluate(()=>tab('backups'));await page.waitForFunction(()=>backupsLoaded);
 await page.locator('#backupMore summary').click();
 assert.ok(await page.locator('#backupMore').evaluate(e=>e.open));
 await page.locator('#viewTitle').click();
 assert.equal(await page.locator('#backupMore').evaluate(e=>e.open),false);
 await page.locator('#backupMore summary').click();await page.keyboard.press('Escape');
 assert.equal(await page.locator('#backupMore').evaluate(e=>e.open),false);
 await page.evaluate(()=>{el('backupMore').open=true;});
 await page.evaluate(()=>tab('install'));
 assert.equal(await page.locator('#backupMore').evaluate(e=>e.open),false);
 await page.locator('#installSourceType').selectOption('local');
 await page.locator('#installBrowse summary').click();
 await page.locator('#viewTitle').click();
 assert.equal(await page.locator('#installBrowse').evaluate(e=>e.open),false);
 await page.evaluate(()=>{el('installBrowse').open=true;});
 await page.keyboard.press('Escape');
 assert.equal(await page.locator('#installBrowse').evaluate(e=>e.open),false);
 await page.evaluate(()=>{el('backupMore').open=true;});
 await page.waitForTimeout(50);
 await page.evaluate(()=>{el('installBrowse').open=true;});
 await page.waitForTimeout(50);
 assert.equal(await page.locator('#backupMore').evaluate(e=>e.open),false);
 await page.evaluate(()=>tab('backups'));
 await page.setViewportSize({width:390,height:844});
 await page.locator('#backupMore summary').tap();await page.locator('#viewTitle').tap();
 assert.equal(await page.locator('#backupMore').evaluate(e=>e.open),false);
 await page.setViewportSize({width:1440,height:900});
 await page.evaluate(()=>{const d=document.createElement('details');d.id='content-fixture';d.innerHTML='<summary>文件差异</summary><p>内容</p>';document.body.append(d);d.open=true;});
 await page.locator('#viewTitle').click();await page.keyboard.press('Escape');
 assert.equal(await page.locator('#content-fixture').evaluate(e=>e.open),true);
 await page.evaluate(()=>el('content-fixture').remove());
 await page.locator('#discoverBackups').click();
 await page.locator('#taskList progress').waitFor({state:'visible'});
 assert.equal(await page.locator('#taskList progress').getAttribute('value'),null);
 assert.ok(await page.locator('#discoverBackups').isDisabled());
 await page.reload();await page.waitForFunction(()=>inventoryReady);
 await page.waitForFunction(()=>[...operationNotices.values()].some(t=>t.action==='discover-backups'),{},{timeout:20000});
 await page.waitForFunction(()=>operationSyncCount===0);
 assert.equal(await page.locator('#resultDialog').evaluate(e=>e.open),false);
 await page.locator('[data-operation-detail]').first().click();
 assert.ok(await page.locator('#resultDialog').evaluate(e=>e.open));
 await page.evaluate(()=>el('resultDialog').close());
 await page.evaluate(()=>{operationNotices.clear();renderTasks();});
 report.scanAndMenus={outsideClick:true,escape:true,navigation:true,mobile:true,contentDetailsPreserved:true,mutualExclusion:true,scanProgress:true,reloadResumes:true,noAutomaticDrawer:true};
 await page.evaluate(()=>tab('trash'));await page.waitForFunction(()=>backupsLoaded);
 await page.locator('#emptyTrash').click();await page.locator('#deleteConfirmation .confirm-delete').click();
 await page.waitForFunction(()=>el('emptyTrash').textContent.includes('中'));
 assert.ok(await page.locator('#emptyTrash').isDisabled());
 await page.locator('#task').waitFor({state:'visible'});
 await page.waitForFunction(()=>[...activeTasks.values()].some(t=>t.action==='empty-trash'&&t.completed>=1));
 assert.ok((await page.locator('#taskList').innerText()).includes('/ 30 项'));
 await page.screenshot({path:output.replace('.json','-desktop.png')});
 const centered=await page.locator('#taskList').boundingBox(),host=await page.locator('.view-host').boundingBox();
 assert.ok(Math.abs(centered.x+centered.width/2-host.x-host.width/2)<3);
 assert.ok(Math.abs(centered.y+centered.height/2-host.y-host.height/2)<3);
 await page.locator('.sidebar [data-tab="skills"]').click();assert.equal(await page.locator('#task').isVisible(),false);
 await page.reload();await page.waitForFunction(()=>inventoryReady&&[...activeTasks.values()].some(t=>t.action==='empty-trash'));
 assert.equal(await page.locator('#task').isVisible(),false);
 await page.evaluate(()=>tab('trash'));assert.ok(await page.locator('#emptyTrash').isDisabled());
 await page.evaluate(()=>{for(const t of activeTasks.values())if(t.action==='empty-trash')t.uiStarted=Date.now()-11000;renderTasks();});
 assert.ok((await page.locator('#taskList').innerText()).includes('可继续浏览'));
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:output.replace('.json','-mobile.png')});
 assert.ok(await page.locator('#task').evaluate(e=>e.scrollWidth<=e.clientWidth+1));
 await page.waitForFunction(()=>!([...activeTasks.values()].some(t=>t.action==='empty-trash'))&&!operationSyncCount,{},{timeout:30000});
 assert.ok((await page.locator('#taskList').innerText()).includes('已完成 30 项'));
 assert.ok((await page.locator('#taskList').innerText()).includes('跳过 1 项'));
 assert.equal(requests.filter(r=>r.action==='job-start'&&r.request?.action==='empty-trash').length,1);
 const remaining=await page.evaluate(()=>backupData.filter(b=>b.type==='quarantine'&&b.state==='available'));
 assert.equal(remaining.length,1);assert.equal(remaining[0].retained,true);
 report.realThirtyItemDelete={singleSubmission:true,reloadResumes:true,centeredProgress:true,navigationUnblocked:true,retainedSurvives:true};
 await page.evaluate(()=>{activeTasks.set('offline',{id:'offline',action:'empty-trash',state:'running',created_at:new Date().toISOString(),disconnected:true});renderTasks();});
 assert.ok((await page.locator('#taskList').innerText()).includes('任务结果待确认'));
 assert.equal(await page.locator('[data-task-resume="offline"]').count(),1);
 await page.evaluate(()=>{activeTasks.delete('offline');taskView({id:'partial-test',action:'empty-trash',state:'succeeded',result:{state:'partial',completed:2,total:5,remaining:['a','b','c'],error:'fixture'}});activeTasks.delete('partial-test');renderTasks();});
 assert.ok((await page.locator('#taskList').innerText()).includes('部分完成 2 项'));
 assert.ok((await page.locator('#taskList').innerText()).includes('失败 1 项 · 未处理 2 项'));
 report.disconnectionAndPartialVisible=true;report.pageErrors=errors;assert.deepEqual(errors,[]);
 fs.writeFileSync(output,JSON.stringify(report,null,2));console.log(JSON.stringify(report));
}
main().catch(e=>{console.error(e,stderr);process.exitCode=1}).finally(async()=>{if(browser)await browser.close();server.kill('SIGTERM');});
