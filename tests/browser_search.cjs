const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
const {chromium}=require(process.env.ASM_PLAYWRIGHT_MODULE||'playwright');

const repo=path.resolve(__dirname,'..');
const server=spawn('python3',['-u','-c',`
import argparse,sys
sys.path[:0]=[${JSON.stringify(repo+'/tests')},${JSON.stringify(repo+'/scripts')}]
import test_maintenance as fixtures,skill_discovery as discovery,daily_recommendations
from test_daily_recommendations import remote as daily_remote
daily_recommendations.AnonymousGitHub.get=daily_remote
discovery.remote_json=lambda url,provider:{'code':0,'data':{'total':0,'skills':[]}} if provider=='skillhub' and '/categories' not in url else {'items':[]}
x=fixtures.MaintenanceTests();x.setUp()
for name in ('code','code-review','my-code-tool','helper-code','unrelated'):
 x.skill(name,'code appears in the description' if name=='unrelated' else 'example')
x.m.scan()
try:x.a.web_command(argparse.Namespace(host='127.0.0.1',port=0,open=False))
finally:x.tearDown()
`],{stdio:['ignore','pipe','pipe']});
let browser,stderr='';server.stderr.on('data',chunk=>stderr+=chunk);
async function main(){
 const url=await new Promise((resolve,reject)=>{
  let output='';const timer=setTimeout(()=>reject(Error('Fixture startup: '+stderr)),15000);
  server.stdout.on('data',chunk=>{output+=chunk;const match=output.match(/http:\/\/127\.0\.0\.1:\d+/);if(match){clearTimeout(timer);resolve(match[0]);}});
  server.on('exit',code=>{clearTimeout(timer);reject(Error('Fixture exited '+code+': '+stderr));});
 });
 browser=await chromium.launch({executablePath:process.env.ASM_BROWSER_EXECUTABLE||undefined,headless:true});
 const page=await browser.newPage(),errors=[];page.on('pageerror',error=>errors.push(String(error)));
 await page.goto(url+'/manage');
 await page.waitForFunction(()=>inventoryReady&&inventory.length>=7);
 await page.locator('.sidebar [data-tab="skills"]').click();
 await page.locator('#filter').fill(' CODE ');
 const names=await page.locator('#items .skill-name').allTextContents();
 assert.deepEqual(names,['code','code-review','my-code-tool','helper-code']);
 assert.equal(await page.locator('#filter').getAttribute('placeholder'),'搜索 Skill 名称');
 await page.locator('#filter').fill('description');
 assert.equal(await page.locator('#items .skill-name').count(),0);
 await page.locator('#filter').fill('code');
 await page.locator('[data-sort-view="skills"][data-sort-key="name"]').click();
 assert.deepEqual(await page.locator('#items .skill-name').allTextContents(),['my-code-tool','helper-code','code-review','code']);
 await page.locator('#filter').fill('');
 assert.ok((await page.locator('#items .skill-name').allTextContents()).includes('unrelated'));
 await page.locator('.sidebar [data-tab="updates"]').click();
 await page.locator('#updateView').selectOption('');
 await page.locator('#updateSearch').fill('code');
 assert.deepEqual(await page.locator('#updateRows .skill-name').allTextContents(),['code','code-review','my-code-tool','helper-code']);
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({libraryOrder:names,updateTitleSearch:true,manualSort:true,pageErrors:errors}));
}
main().catch(error=>{console.error(error);console.error(stderr);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();server.kill('SIGTERM');});
