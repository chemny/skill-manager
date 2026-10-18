// All mutable data belongs to the temporary Python fixture, not the user's service.
const {spawn}=require('node:child_process');
const fs=require('node:fs'),assert=require('node:assert/strict'),path=require('node:path');
const {chromium}=require(process.env.ASM_PLAYWRIGHT_MODULE || 'playwright');
const repo=path.resolve(__dirname,'..'),output=process.argv[2];
const server=spawn('python3',['-u','-c',`
import argparse,sys,time,json
sys.path[:0]=[${JSON.stringify(repo+'/tests')},${JSON.stringify(repo+'/scripts')}]
import test_maintenance as fixtures,skill_discovery as d
import daily_recommendations
from test_daily_recommendations import remote as daily_remote
daily_recommendations.AnonymousGitHub.get=daily_remote
import description_translation
description_translation.model_available=lambda:False
from test_skill_discovery import claw,skillsmp,file_response
x=fixtures.MaintenanceTests();x.setUp()
PREVIEW=${process.argv.includes('--preview')?'True':'False'}
if PREVIEW:
 examples=[('meeting-notes','会议纪要整理','整理讨论要点、决定和待办事项'),('pdf-extract','PDF 内容提取','提取文档中的文字与表格'),('research-brief','资料调研摘要','归纳参考资料并整理来源'),('slide-outline','演示文稿提纲','把主题整理成清晰的演示结构'),('code-review','代码审查助手','检查代码变更并标记风险'),('data-cleanup','表格数据清理','规范字段、去重并检查异常数据')]
 for slug,title,description in examples:
  folder=x.skill(slug,description,title)
  (folder/'SKILL.md').write_text('---\\nname: '+title+'\\ndescription: '+description+'\\nversion: 1.0\\n---\\n\\n# '+title+'\\n\\n'+description+'。')
 config=x.a.load_config()
 for platform in ('codex','claude_code'):
  config['platforms'][platform].update(enabled=True,roots=[str(x.roots)],discover_defaults=False)
 x.a.save_config(config)
 x.m.scan()
x.m.inventory()
if not PREVIEW:x.m.bind_source(x.row('a'),'https://github.com/example/skills','main','skills/demo')
def remote(url,provider):
 time.sleep(.15)
 if PREVIEW:
  examples=[('meeting-notes','会议纪要整理','把会议记录归纳为要点与待办',23500,1840),('document-writer','文档写作助手','起草和修订结构清晰的文档',19700,1520),('pdf-toolkit','PDF 文档工具','提取文字、表格并整理内容',16800,1290),('research-assistant','资料调研助手','汇总资料并保留可追溯来源',14200,980),('spreadsheet-helper','表格处理助手','清理数据并生成汇总分析',12000,810),('presentation-maker','演示文稿制作','将内容整理为演示提纲与页面',9800,670),('code-reviewer','代码审查助手','检查变更、风险与测试遗漏',8500,590),('workflow-planner','工作流程规划','拆解目标并安排执行步骤',7200,480)]
  if provider=='skillhub':
   if '/categories' in url:return {'items':[{'key':'office-efficiency','name':'办公效率'},{'key':'content-creation','name':'内容创作'},{'key':'research','name':'研究'}]}
   return {'code':0,'data':{'total':len(examples),'skills':[{'slug':slug,'name':title,'homepage':'https://api.skillhub.cn/example/'+slug,'category':'office-efficiency','downloads':downloads,'stars':stars,'description_zh':description} for slug,title,description,downloads,stars in examples]}}
  if provider=='clawhub':
   rows=[]
   arrivals=[('inbox-triage','收件箱整理','归纳邮件主题并提取待办事项',420),('citation-checker','引用核对','检查资料引用与链接是否完整',360),('image-caption','图片说明生成','为图片撰写清晰的替代文字',290),('calendar-summary','日程摘要','整理当日安排和准备事项',240),('table-insights','表格洞察','提炼表格中的趋势与异常',180),('reading-list','阅读清单整理','对文章分类并提炼摘要',150)]
   for slug,title,description,stars in arrivals:
    row=claw('example',slug);row.update(displayName=title,summary=description,createdAt=int(time.time()*1000),stats={'stars':stars,'downloads':stars*5});rows.append(row)
   return {'items':rows}
 if provider=='skillhub':
  if '/categories' in url:return {'items':[{'key':'office-efficiency','name':'办公效率'},{'key':'content-creation','name':'内容创作'},{'key':'research','name':'研究'}]}
  if 'category=research' in url:raise TimeoutError('fixture')
  if 'category=content-creation' in url:return {'code':0,'data':{'total':1,'skills':[{'slug':'writer','name':'Writer','homepage':'https://api.skillhub.cn/example/writer','category':'content-creation','description_zh':'写作工具'}]}}
  return {'code':0,'data':{'total':61,'skills':[{'slug':'demo','name':'Demo','homepage':'https://api.skillhub.cn/example/demo','category':'office-efficiency','downloads':100,'stars':319,'description_zh':'整理 PDF 文档'}]}}
 if provider=='github':
  if '/commits/' in url:return {'sha':'a'*40}
  return file_response('---\\nname: demo\\ndescription: Demo skill\\n---\\n<img src=x onerror=window.injected=true>')
 if provider=='skillsmp':
  if 'broken' in url:raise TimeoutError('fixture')
  return {'success':True,'data':{'skills':[skillsmp()]}}
 return {'items':[claw(slug='broken' if 'broken' in url else 'demo')]}
d.remote_json=remote
try:x.a.web_command(argparse.Namespace(host='127.0.0.1',port=0,open=False))
finally:x.tearDown()
`],{stdio:['ignore','pipe','pipe']});
let browser,stderr='';server.stderr.on('data',c=>stderr+=c);
async function main(){
 const url=await new Promise((resolve,reject)=>{let text='';const timer=setTimeout(()=>reject(Error('Server startup timeout')),15000);server.stdout.on('data',c=>{text+=c;const m=text.match(/http:\/\/127\.0\.0\.1:\d+/);if(m){clearTimeout(timer);resolve(m[0]);}});server.on('exit',code=>reject(Error('Server exited '+code)));});
 browser=await chromium.launch({executablePath:process.env.ASM_BROWSER_EXECUTABLE || undefined,headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],requests=[],report={};
 page.on('pageerror',e=>errors.push(String(e)));
 page.on('request',r=>{if(r.url().endsWith('/api/maintenance'))requests.push(r.postDataJSON());});
 await page.goto(url+'/manage');await page.waitForFunction(expected=>inventoryReady&&(expected===0||inventory.length===expected),process.argv.includes('--preview')?0:2);
 await page.waitForFunction(()=>activeTab==='home'&&homeBoardLoading.size===0&&!homeSceneLoading);
 const localCopies=await page.evaluate(()=>{const original=inventory;try{inventory=original.map(i=>({...i,name:'same-skill',content_fingerprint:'matching-content'}));invalidateSkillGroups();return groupSkills().map(i=>i._members.length);}finally{inventory=original;invalidateSkillGroups();}});
 assert.deepEqual(localCopies,[2]);report.identicalLocalCopiesGrouped=true;
 if(process.argv.includes('--preview')){
  await page.waitForFunction(()=>document.querySelectorAll('#homeNew li').length>=5&&document.querySelectorAll('#homeTrending li').length>=5&&document.querySelectorAll('#homeSceneItems .home-scene-item').length>=8);
  await page.setViewportSize({width:1920,height:1080});
  await page.screenshot({path:output.replace(/\.json$/,'-home.png')});
  console.log(output.replace(/\.json$/,'-home.png'));return;
 }
 assert.equal(await page.locator('.home-shortcuts').count(),0);
 assert.deepEqual(await page.locator('.home-boards h2').allTextContents(),['今日上新','热门 Skills']);
 assert.equal(await page.locator('.home-scenes h2').textContent(),'Skills 商店');
 assert.equal(await page.locator('#homeSceneItems .home-scene-item').count(),1);
 assert.ok((await page.locator('#homeSceneItems').innerText()).includes('319 Stars'));
 assert.ok(!(await page.locator('#homeTrending').innerText()).includes('腾讯 SkillHub'));
 assert.ok(!(await page.locator('#homeNew').innerText()).includes('ClawHub'));
 assert.ok((await page.locator('#homeNew').innerText()).includes('demo'));
 assert.ok((await page.locator('#homeNew a').first().getAttribute('href')).includes('clawhub'));
 await page.waitForFunction(()=>homeTranslationPending.size===0);
 await page.waitForFunction(()=>!document.querySelector('#homeNew')?.textContent?.includes('正在准备中文简介'));
 assert.ok((await page.locator('#homeNew').innerText()).includes('暂无中文简介'));
 assert.ok(!(await page.locator('#homeNew').innerText()).includes('Demo description'));
 const dailyBefore=requests.filter(r=>r.action==='job-start'&&r.request?.daily).length;
 await page.evaluate(()=>loadHomeBoard('new'));
 assert.equal(requests.filter(r=>r.action==='job-start'&&r.request?.daily).length,dailyBefore);
 report.dailyVerifiedAndCached=true;
 const fallback=await page.evaluate(async()=>{
  const original=browse,calls=[];
  try{
   browse=async(action,payload)=>{calls.push(payload);if(payload.daily)throw Error('daily unavailable');return {view:'new',items:[{name:'New arrival',url:'https://example.com/new',description:'Fresh skill',stats:{},published_at:Date.now()}],providers:[]};};
   await loadHomeBoard('new');
   return {title:el('homeNew').closest('section').querySelector('h2').textContent,text:el('homeNew').textContent,calls};
  }finally{browse=original;}
 });
 assert.equal(fallback.title,'今日上新');assert.ok(fallback.text.includes('New arrival'));
 assert.equal(fallback.calls[1].daily,undefined);assert.equal(fallback.calls[1].view,'new');
 const unavailable=await page.evaluate(async()=>{
  const original=browse;let calls=0;
  try{browse=async()=>{calls++;throw Error('offline');};await loadHomeBoard('new');return {calls,text:el('homeNew').textContent,title:el('homeNew').closest('section').querySelector('h2').textContent};}
  finally{browse=original;}
 });
 assert.equal(unavailable.calls,2);assert.equal(unavailable.title,'今日上新');assert.ok(unavailable.text.includes('重试'));
 const threshold=await page.evaluate(async()=>{
  const original=browse,make=(n)=>({view:'daily',items:Array.from({length:n},(_,i)=>({name:'Pick '+i,url:'https://example.com/pick/'+i,description:'Today',stats:{stars:i}})),providers:[]});
  try{
   browse=async(action,payload)=>payload.daily?make(4):{view:'new',items:[{name:'New arrival',url:'https://example.com/new',description:'Fresh skill',stats:{}}],providers:[]};
   await loadHomeBoard('new');
   const short={title:el('homeNew').closest('section').querySelector('h2').textContent,text:el('homeNew').textContent};
   browse=async()=>make(5);
   await loadHomeBoard('new');
   const full={title:el('homeNew').closest('section').querySelector('h2').textContent,count:el('homeNew').querySelectorAll('.home-ranking-hit').length};
   return {short,full};
  }finally{browse=original;}
 });
 assert.equal(threshold.short.title,'今日上新');assert.ok(threshold.short.text.includes('New arrival'));assert.ok(!threshold.short.text.includes('Pick'));
 assert.equal(threshold.full.title,'今日推荐');assert.equal(threshold.full.count,5);
 for(const board of ['#homeNew','#homeTrending']){
  await page.locator(board+' .home-ranking-hit').first().hover();
  assert.equal(await page.locator(board+' li').first().evaluate(e=>getComputedStyle(e).backgroundColor),'rgb(245, 247, 246)');
  assert.ok(await page.locator(board+' li').first().evaluate(e=>Math.abs(e.getBoundingClientRect().left-e.closest('.home-section').getBoundingClientRect().left)<=2));
 }
 const translated=await page.evaluate(async()=>{
  const original=work,source='Recommend and price-check AI models for any task.';
  try{
   work=async action=>action==='translate-descriptions'?{translations:{[source]:'为任何任务推荐并比价 AI 模型。'}}:original(action);
   renderHomeBoard('new',{view:'new',items:[{name:'Model finder',url:'https://example.com/model',description:source,stats:{}}],providers:[]});
   await new Promise(resolve=>setTimeout(resolve,0));
   return el('homeNew').textContent;
  }finally{work=original;}
 });
 assert.ok(translated.includes('为任何任务推荐并比价 AI 模型。'));
 await page.evaluate(()=>loadHomeBoard('new'));
 report.dailyThresholdAndFallback=true;
 assert.equal(await page.evaluate(()=>discoveryStars({stats:{stars:0}})),'0 Stars');
 assert.equal(await page.evaluate(()=>discoveryStars({stats:{}})),'—');
 await page.locator('[data-home-scene="content-creation"]').click();await page.waitForFunction(()=>!homeSceneLoading);
 assert.ok((await page.locator('#homeSceneItems').innerText()).includes('Writer'));
 await page.locator('#homeSceneMore').click();await page.waitForFunction(()=>!discoveryLoading);
 assert.equal(await page.locator('#discoveryCategory').inputValue(),'content-creation');
 assert.ok(!(await page.locator('#discoveryRows').innerText()).includes('腾讯 SkillHub'));
 assert.equal(await page.locator('#discoveryRows tr').first().locator('td').nth(2).innerText(),'—');
 assert.equal(await page.locator('#discoveryRows [data-discovery-detail]').last().innerText(),'查看详情');
 await page.locator('.sidebar [data-tab="home"]').click();await page.waitForFunction(()=>!homeSceneLoading&&homeBoardLoading.size===0);
 await page.locator('[data-home-scene="research"]').click();await page.waitForFunction(()=>!homeSceneLoading);
 assert.ok((await page.locator('#homeSceneStatus').innerText()).includes('加载'));
 assert.equal(await page.locator('[data-home-scene-retry]').count(),1);
 await page.locator('[data-home-scene="office-efficiency"]').click();await page.waitForFunction(()=>!homeSceneLoading);
 report.sceneCategoriesAndFailure=true;
 assert.equal(await page.locator('#home').isVisible(),true);
 assert.ok((await page.locator('#homeSummary').innerText()).includes('已安装 Skills'));
 assert.equal(await page.locator('#homeSummary button').count(),4);
 assert.ok((await page.locator('#homeSummary').innerText()).includes('Skills 占用'));
 assert.equal(await page.locator('#homeSummary strong').first().evaluate(e=>getComputedStyle(e).borderRadius),'0px');
 await page.evaluate(async()=>{
  const savedWork=work,savedRefresh=refreshBackupList;
  try{
   work=async()=>({complete:false,summary:{found:3,in_trash:1,ignored:1,partial:1},locations:['/test/backups'],errors:[],rejected:[],unscanned:[{path:'/test/backups/deep',reason:'深度上限'}]});
   refreshBackupList=async()=>{};
   await discoverBackups();
   if(el('resultDialog').open)throw Error('Scan opened an automatic dialog');
   showBackupScanResult(await work());
  }finally{work=savedWork;refreshBackupList=savedRefresh;}
 });
 assert.ok((await page.locator('#resultDialog').innerText()).includes('备份扫描未完成'));
 assert.ok((await page.locator('#resultDialog').innerText()).includes('已在回收站'));
 assert.ok((await page.locator('#resultDialog').innerText()).includes('深度上限'));
 await page.evaluate(()=>el('resultDialog').close());
 await page.locator('#homeSummary [data-home-go="storage"]').click();
 assert.equal(await page.locator('#setting-storage').isVisible(),true);
 await page.locator('.sidebar [data-tab="home"]').click();
 assert.equal(await page.locator('#homeTrending li a').count(),1);
 assert.equal(await page.locator('#homeNew li a').count(),1);
 assert.ok(!requests.some(r=>['scan','check','check-many','execute'].includes(r.request?.action)));
 await page.screenshot({path:output.replace(/\.json$/, '-home.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 await page.screenshot({path:output.replace(/\.json$/, '-home-mobile.png'),fullPage:true});
 await page.setViewportSize({width:1440,height:1000});
 await page.locator('#homeQuery').fill('pdf');await page.locator('#homeQuery').press('Enter');await page.waitForFunction(()=>!discoveryLoading&&activeTab==='discovery');
 assert.equal(await page.locator('#discoveryQuery').inputValue(),'pdf');
 await page.locator('.sidebar [data-tab="home"]').click();
 await page.locator('#home [data-home-go="skills"]').first().click();
 assert.equal(await page.locator('#skills').isVisible(),true);
 report.homeDefaultAndRoutes=true;
 await page.locator('.sidebar [data-tab="home"]').click();
 await page.locator('#home [data-home-discover="trending"]').first().click();
 await page.waitForFunction(()=>!discoveryLoading);
 await page.locator('.sidebar [data-tab="discovery"]').click();
 await page.locator('.sidebar [data-tab="skills"]').click();
 report.navigationDuringFetch=await page.locator('#skills').isVisible();assert.ok(report.navigationDuringFetch);
 await page.waitForFunction(()=>!activeTasks.size&&!discoveryLoading);
 await page.locator('.sidebar [data-tab="discovery"]').click();
 await page.waitForFunction(()=>!discoveryLoading);
 assert.equal(await page.locator('#discoveryRows tr').count(),1);
 assert.equal(await page.locator('#discoveryCount').textContent(),'61');
 assert.match(await page.locator('#discovery .library-summary').innerText(),/61个 Skills/);
 assert.ok(!(await page.locator('#discovery .library-summary').innerText()).includes('来源共'));
 assert.equal(await page.locator('#discoveryProvider').isVisible(),false);
 assert.equal(await page.locator('#discoveryRefresh,#discoveryHideInstalled').count(),0);
 assert.equal(await page.locator('#discoveryBasis').isVisible(),false);
 assert.equal(await page.locator('#discoveryStatus').isVisible(),false);
 assert.ok(!(await page.locator('#discovery').innerText()).includes('已缓存'));
 const count=requests.filter(r=>r.request?.action==='discovery-refresh').length;
 report.titleSearch=await page.evaluate(()=>{
  const candidates=['helper-code','my-code-tool','code-review','code','unrelated'].map((name,id)=>({name,id:String(id)}));
  const ordered=searchRows(candidates,' CODE ','skills').map(item=>item.name);
  el('filter').value='one';
  const descriptionOnly=filtered().length;
  el('filter').value='a';
  const visible=filtered().map(item=>item.name);
  el('filter').value='';
  return {ordered,descriptionOnly,visible};
 });
 assert.deepEqual(report.titleSearch.ordered,['code','code-review','my-code-tool','helper-code']);
 assert.equal(report.titleSearch.descriptionOnly,0);
 assert.deepEqual(report.titleSearch.visible,['a']);
 await page.locator('.sidebar [data-tab="skills"]').click();await page.locator('.sidebar [data-tab="discovery"]').click();
 await page.waitForFunction(()=>!discoveryLoading);
 report.noJobOnFreshReentry=requests.filter(r=>r.request?.action==='discovery-refresh').length===count;assert.ok(report.noJobOnFreshReentry);
 await page.locator('#discoveryCategory').selectOption('office-efficiency');await page.waitForFunction(()=>!discoveryLoading);
 await page.locator('#discoveryNext').click();await page.waitForFunction(()=>!discoveryLoading&&discoveryPage===2);
 report.categoryAndPaging=requests.some(r=>r.request?.category==='office-efficiency'&&r.request?.page===2);assert.ok(report.categoryAndPaging);
 assert.equal(await page.locator('#discovery th').count(),5);
 assert.equal(await page.locator('#discoveryRows tr').first().locator('td').count(),5);
 assert.equal(await page.locator('.discovery-description').first().evaluate(e=>getComputedStyle(e).webkitLineClamp),'1');
 report.compactDiscoveryColumns=true;
 await page.screenshot({path:output.replace(/\.json$/, '-ranking.png'),fullPage:true});
 await page.locator('[data-discovery-view="new"]').click();await page.waitForFunction(()=>!discoveryLoading);
 assert.ok(!(await page.locator('#discovery').innerText()).includes('按首次发布时间排序'));
 await page.locator('[data-discovery-view="all"]').click();await page.waitForFunction(()=>!discoveryLoading);
 assert.equal(await page.locator('[data-discovery-view="all"]').getAttribute('aria-pressed'),'true');
 assert.equal(await page.locator('#discoveryCount').textContent(),'61');
 assert.match(await page.locator('#discoveryPageInfo').innerText(),/1 \/ 3/);
 await page.locator('#discoveryQuery').fill('demo');await page.locator('#discoveryQuery').press('Enter');
 await page.waitForFunction(()=>!discoveryLoading);
 assert.equal(await page.locator('[data-discovery-view="all"]').getAttribute('aria-pressed'),'true');
 report.searchRows=await page.locator('#discoveryRows tr').count();assert.ok(report.searchRows>=1);
 const previousCategory=await page.evaluate(()=>{const old=discoveryData.items[0].category;discoveryData.items[0].category='';renderDiscovery();return old;});
 assert.match(await page.locator('#discoveryRows tr').first().innerText(),/其他/);
 await page.evaluate(category=>{discoveryData.items[0].category=category;renderDiscovery();},previousCategory);
 await page.locator('#discoveryRows [data-discovery-detail]').last().click();
 assert.match(await page.locator('#resultDialog').innerText(),/整理 PDF 文档/);
 assert.equal(await page.locator('#resultDialog [data-discovery-detail-install]').count(),1);
 await page.locator('#closeResult').click();
 await page.evaluate(()=>{discoveryData.items[0].installed='source_match';renderDiscovery();});
 assert.equal(await page.locator('#discoveryRows button:disabled').last().innerText(),'已安装');
 await page.evaluate(()=>{discoveryData.items[0].installed='unknown';renderDiscovery();});
 report.detailAndInstalledState=true;
 await page.screenshot({path:output.replace(/\.json$/, '-desktop.png'),fullPage:true});
 const installationRequests=requests.length;
 await page.locator('[data-discovery-install]').first().click();
 await page.waitForFunction(()=>activeTab==='install'&&el('installUrl').value==='https://api.skillhub.cn/api/v1/skills/demo');
 report.installUrl=await page.locator('#installUrl').inputValue();
 assert.equal(await page.locator('#installTarget').inputValue(),'');
 assert.equal(await page.locator('#identifyInstall').isDisabled(),true);
 report.noAutomaticInstall=!requests.slice(installationRequests).some(r=>['execute','install-identify'].includes(r.request?.action));assert.ok(report.noAutomaticInstall);
 await page.locator('.sidebar [data-tab="discovery"]').click();await page.waitForFunction(()=>!discoveryLoading);
 await page.locator('[data-discovery-view="new"]').click();await page.waitForFunction(()=>!discoveryLoading&&discoveryData.items.length===1);
 assert.equal(await page.locator('#discoveryCategory').isVisible(),false);
 await page.locator('[data-discovery-install]').click();
 await page.waitForFunction(()=>activeTab==='install'&&el('installUrl').value==='https://clawhub.ai/api/v1/download?slug=demo');
 report.newArrivalInstallUrl=await page.locator('#installUrl').inputValue();
 await page.locator('.sidebar [data-tab="discovery"]').click();await page.waitForFunction(()=>!discoveryLoading);
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:output.replace(/\.json$/, '-mobile.png'),fullPage:true});
 report.mobilePageFits=await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth);assert.ok(report.mobilePageFits);
 report.mobileActionsVisible=await page.locator('#discoveryRows tr').first().evaluate(row=>{const wrap=document.querySelector('#discovery .tablewrap'),button=row.querySelector('[data-discovery-install]'),bounds=button.getBoundingClientRect();return wrap.scrollWidth<=wrap.clientWidth&&bounds.left>=0&&bounds.right<=innerWidth;});assert.ok(report.mobileActionsVisible);
 report.desktopAndMobileScreenshots=true;
 report.errors=errors;assert.deepEqual(errors,[]);
 fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));
}
main().catch(e=>{console.error(e);console.error(stderr);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();server.kill('SIGTERM');});
