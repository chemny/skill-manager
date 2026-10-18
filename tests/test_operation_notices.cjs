// Exercise the shipped renderer with a fake clock; no personal files or service writes.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../assets/maintenance.html'),'utf8');
for(const script of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(script[1]);
const nodes=new Map(),timers=new Map();let clock=10000,seq=0;
function el(id){if(!nodes.has(id))nodes.set(id,{hidden:true,innerHTML:'',textContent:'',parentElement:{},dataset:{},setAttribute(){}});return nodes.get(id)}
const ctx=vm.createContext({Date:{now:()=>clock,parse:Date.parse},Map,Number,Math,el,activeTasks:new Map(),startingTasks:new Map(),buttonPending:new Map(),activeTab:'skills',backupView:'backup',document:{querySelectorAll:()=>[]},friendly:String,esc:String,setTimeout(fn,ms){const id=++seq;timers.set(id,{fn,at:clock+ms});return id},clearTimeout:id=>timers.delete(id)});
vm.runInContext(html.slice(html.indexOf('const operationNotices=new Map()'),html.indexOf('function renderTasks()')),ctx);
function run(s){return vm.runInContext(s,ctx)}
function tick(ms){clock+=ms;for(const [id,t] of [...timers])if(t.at<=clock){timers.delete(id);t.fn()}}
for(const operation of ['delete','purge','trash-backup']){
 run(`operationNotices.clear();rememberOperation({id:'ok-${operation}',action:'execute',operation:'${operation}',state:'succeeded',result:{completed:1}});renderOperationTasks()`);
 assert.equal(el('task').hidden,true);
 assert.equal(el('operationSuccess').hidden,false);
 assert.match(el('operationSuccess').textContent,/已完成 1 项/);
 tick(5000);assert.equal(el('operationSuccess').hidden,true);
}
for(const task of [
 {state:'running'}, {state:'failed',message:'fixture failure'},
 {state:'succeeded',result:{state:'partial',completed:1,error:'fixture failure'}},
 {state:'succeeded',result:{error:'fixture failure'}},
 {state:'running',disconnected:true},
]){
 ctx.task={id:'attention',action:'execute',operation:'delete',uiStarted:1,...task};
 run('operationNotices.clear();activeTasks.clear();activeTasks.set(task.id,task);rememberOperation(task);renderOperationTasks()');
 assert.equal(el('task').hidden,false,JSON.stringify(task));
 if(task.state==='failed'||task.result?.state==='partial')assert.match(el('taskList').innerHTML,/查看结果/);
}
run("activeTab='backups';activeTasks.clear();operationNotices.clear();rememberOperation({id:'scan-partial',action:'discover-backups',state:'succeeded',result:{complete:false,summary:{found:0}}});renderOperationTasks()");
assert.equal(el('task').hidden,true);
assert.equal(el('backupScanNotice').hidden,false);
assert.match(el('backupScanNoticeText').textContent,/部分位置未覆盖，已找到 0 项/);
run("operationNotices.clear();rememberOperation({id:'scan-failed',action:'discover-backups',state:'failed',message:'fixture failure'});renderOperationTasks()");
assert.equal(el('task').hidden,true);
assert.equal(el('backupScanNotice').hidden,false);
assert.match(el('backupScanNoticeText').textContent,/操作失败/);
run("activeTab='skills';renderOperationTasks()");
assert.equal(el('backupScanNotice').hidden,true);
run("activeTasks.clear();operationNotices.clear();operationSyncCount=1;rememberOperation({id:'sync',action:'execute',operation:'delete',state:'succeeded'});renderOperationTasks()");
assert.equal(el('task').hidden,true);
run("operationNotices.get('sync').state='failed';operationNotices.get('sync').message='列表同步失败';operationSyncCount=0;renderOperationTasks()");
assert.equal(el('task').hidden,false);
assert.match(el('taskList').innerHTML,/列表同步失败/);
console.log('PASS: success toast expires; running, failed, partial and disconnected operations remain visible; incomplete scans use a page-local notice; page JavaScript parses.');
