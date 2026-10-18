const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
const {chromium}=require(process.env.ASM_PLAYWRIGHT_MODULE||'playwright');
const repo=path.resolve(__dirname,'..');
const server=spawn('python3',['-u','-c',`
import argparse,sys
sys.path[:0]=[${JSON.stringify(repo+'/tests')},${JSON.stringify(repo+'/scripts')}]
import test_maintenance as fixtures
x=fixtures.MaintenanceTests();x.setUp()
x.skill('营销设计','Unspecified helper.');x.m.scan()
x.m.inventory()
x.m.invalidate_cache()
try:x.a.web_command(argparse.Namespace(host='127.0.0.1',port=0,open=False))
finally:x.tearDown()
`],{stdio:['ignore','pipe','pipe']});
let browser,stderr='';server.stderr.on('data',chunk=>stderr+=chunk);
async function main(){
 const url=await new Promise((resolve,reject)=>{let data='';const timer=setTimeout(()=>reject(Error('Fixture startup timeout: '+stderr)),15000);server.stdout.on('data',chunk=>{data+=chunk;const match=data.match(/http:\/\/127\.0\.0\.1:\d+/);if(match){clearTimeout(timer);resolve(match[0]);}});server.on('exit',code=>{clearTimeout(timer);reject(Error('Fixture exited '+code+': '+stderr));});});
 browser=await chromium.launch({executablePath:process.env.ASM_BROWSER_EXECUTABLE||undefined,headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];
 page.on('pageerror',error=>errors.push(String(error)));
 await page.goto(url+'/manage');await page.waitForFunction(()=>inventoryReady);
 assert.equal(await page.evaluate(()=>inventorySnapshot.cache.dirty),false);
 await page.evaluate(()=>{el('categoryFilter').value='其他';render();});
 assert.equal(await page.locator('#categoryFilter option').filter({hasText:'未分类'}).count(),0);
 assert.equal(await page.locator('#categoryFilter').inputValue(),'其他');
 assert.ok(Number(await page.locator('#libraryMatched').textContent())>=1);
 await page.evaluate(()=>openLabels([inventory.find(i=>i.name==='营销设计').id]));
 assert.equal(await page.locator('#labelCategory').inputValue(),'其他');
 assert.equal(await page.locator('#labelCategory option').filter({hasText:'未分类'}).count(),0);
 await page.locator('#saveLabels').click();
 await page.waitForFunction(()=>!el('labelsDialog').open);
 await page.reload();await page.waitForFunction(()=>inventoryReady);
 assert.equal(await page.evaluate(()=>inventory.find(i=>i.name==='营销设计').classification?.category||''),'');
 await page.evaluate(()=>tab('settings','taxonomy'));
 await page.waitForFunction(()=>el('classificationStatus').textContent.includes('待分类'));
 assert.equal(await page.locator('.classification-review').count(),0);
 await page.screenshot({path:'/tmp/asm-classification-review-desktop.png'});
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:'/tmp/asm-classification-review-mobile.png'});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.setViewportSize({width:1440,height:900});
 await page.locator('#autoClassify').click();
 await page.waitForFunction(()=>physicalSkills().find(i=>i.name==='营销设计')?.classification?.category&&inventorySnapshot?.cache?.dirty===false,{timeout:15000}).catch(async error=>{console.error(await page.evaluate(()=>({toast:el('toastText')?.textContent,tasks:[...activeTasks.values()],cache:inventorySnapshot?.cache})));throw error});
 assert.equal(await page.evaluate(()=>inventorySnapshot.cache.dirty),false);
 await page.reload();await page.waitForFunction(()=>inventoryReady);
 const result=await page.evaluate(()=>{const row=inventory.find(i=>i.name==='营销设计');return {category:row?.classification?.category,dirty:inventorySnapshot.cache.dirty,unclassified:physicalSkills().filter(i=>!i.classification?.category).length};});
 assert.ok(result.category);assert.equal(result.dirty,false);
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify(result));
}
main().catch(error=>{console.error(error,stderr);process.exitCode=1}).finally(async()=>{if(browser)await browser.close();server.kill();});
