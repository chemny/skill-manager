// Real project scans and analyses; file changes require an explicit user action.
Object.assign(actions,{'space-catalog':'识别项目位置','space-add-project':'添加项目','disk-scan':'扫描项目','disk-report':'读取项目清单','disk-stop':'停止扫描',
 'project-analyze':'分析项目','project-analysis-stop':'停止分析','space-advice':'读取清理建议','space-trash-batch':'移入回收站',
 'space-trash-list':'读取回收内容','space-restore-many':'恢复文件','trash-purge-many':'永久删除'});
const spaceNav=document.createElement('button');spaceNav.dataset.tab='space';spaceNav.setAttribute('role','tab');spaceNav.title='空间清理';spaceNav.innerHTML=icons.storage+'<span class="nav-text">空间清理</span>';spaceNav.onclick=guard(()=>tab('space'));document.querySelector('.sidebar nav').append(spaceNav);
const spaceTop=document.createElement('button');spaceTop.id='diskStart';spaceTop.className='primary';spaceTop.hidden=true;spaceTop.innerHTML=icons.scan+'扫描项目';document.querySelector('.top-actions').append(spaceTop);
el('backups').append(el('projectRecycle'));
for(const node of document.querySelectorAll('#space [data-icon="search"],#projectRecycle [data-icon="search"]'))node.innerHTML=icons.search;
let diskReport=null,diskCatalog=null,diskPage=0,spaceMode='disk',diskStarting=false,projectStarting=false;
let diskBatch={},projectStartingPaths=[],spaceAdviceData={groups:[],missing:[]},spaceAdvicePaths=[];
let spaceNavigation=0,adviceRequest=0;
const diskSelected=new Set(),spacePlanSelected=new Set(),projectTrashSelected=new Set();
let scopeAgents=new Set(),scopeProjects=new Set(),scopeOpen=null,scopeQueries={},scopeLoading=false;
let artifactTrash=[],projectTrashPage=0,recycleLoading=null;
const projectSelectionKey='asm-project-space-selection';
try{for(const path of JSON.parse(localStorage.getItem(projectSelectionKey)||'[]'))if(typeof path==='string')diskSelected.add(path)}catch{}
const projectBytes=value=>bytes(value??0),projectTotal=rows=>rows.reduce((sum,row)=>sum+(row.space?.allocated_bytes||row.allocated||0),0);
function projectEligible(id,allowed){const button=el(id);button.dataset.eligible=String(allowed);button.disabled=!allowed||!!buttonPending.get(button)}
function projectJob(){return [...activeTasks.values(),...startingTasks.values()].find(t=>['disk-scan','project-analyze'].includes(t.action)&&['queued','running'].includes(t.state))}
function saveProjectSelection(){try{localStorage.setItem(projectSelectionKey,JSON.stringify([...diskSelected]))}catch{}}
function selectedProjects(){return (diskReport?.projects||[]).filter(project=>diskSelected.has(project.path))}
function sameBatch(){const paths=diskBatch.paths||[];return diskBatch.report_id===diskReport?.id&&paths.length===diskSelected.size&&paths.every(path=>diskSelected.has(path))}
function canViewAdvice(){return sameBatch()&&(diskBatch.completed?.length||0)>0}
function projectStatus(project){
 const job=projectJob(),paths=projectStartingPaths.length?projectStartingPaths:job?.project_paths||[];
 if((job||projectStarting||diskStarting)&&job?.current_project===project.path)return job?.action==='disk-scan'?'scanning':'running';
 if((job?.action==='project-analyze'||projectStarting)&&paths.includes(project.path)&&!job?.partial_analyses?.some(row=>row.path===project.path))return 'waiting';
 if((job?.action==='disk-scan'||diskStarting)&&['pending','running'].includes(project.space?.state))return 'scan-waiting';
 if(project.analysis)return ['completed','partial'].includes(project.analysis.state)?project.analysis.state:'failed';
 return project.space?.state==='failed'?'scan-failed':project.space?.state==='partial'?'scan-partial':'pending';
}
function projectProgress(job,action){return '<span class="project-progress" title="'+esc(job?.disconnected?'正在重新连接':job?.message||'正在准备')+'"><span class="project-running"><span class="loading-ring" aria-hidden="true"></span>'+action+'中</span><progress aria-label="'+action+'进度"></progress></span>'}
function projectStatusMarkup(project){
 const state=projectStatus(project);
 if(state==='running'||state==='scanning')return projectProgress(projectJob(),state==='scanning'?'扫描':'分析');
 return esc({waiting:'等待分析','scan-waiting':'等待扫描',completed:'已分析',partial:'部分结果',failed:'未完成',pending:'待分析','scan-failed':'无法访问','scan-partial':'部分统计'}[state]||'待分析');
}
function filteredProjects(){
 const query=searchText(el('diskSearch').value),agent=el('diskAgent').value,state=el('diskStatusFilter').value;
 return (diskReport?.projects||[]).filter(project=>(!query||searchText(project.name).includes(query))&&(!agent||project.agents?.includes(agent))&&(!state||projectStatus(project)===state)).sort((a,b)=>(b.space?.allocated_bytes??-1)-(a.space?.allocated_bytes??-1)||textOrder.compare(a.name,b.name));
}
function renderProjectControls(){
 const job=projectJob(),locked=!!job||diskStarting||projectStarting,projects=diskReport?.projects||[],chosen=selectedProjects(),rows=filteredProjects().slice(diskPage*30,(diskPage+1)*30);
 projectEligible('diskStart',!locked);projectEligible('diskRescan',!locked&&!!projects.length);projectEligible('diskAnalyzeSelected',!locked&&!!chosen.length);
 el('diskRescan').hidden=!projects.length;
 el('diskAnalyzeSelected').textContent=projectStarting||job?.action==='project-analyze'?'分析中…':canViewAdvice()?'查看清理建议':'分析选中项目';
 el('diskAnalyzeSelected').setAttribute('aria-busy',String(projectStarting||job?.action==='project-analyze'));
 el('diskStop').hidden=!locked;el('diskStop').textContent=job?.action==='disk-scan'||diskStarting?'停止扫描':'停止分析';
 el('diskReconnect').hidden=!job?.disconnected;
 el('diskClearSelection').hidden=!chosen.length;el('diskClearSelection').disabled=locked;
 el('diskSelection').textContent=chosen.length?'已选 '+chosen.length+' 个项目 · '+projectBytes(projectTotal(chosen)):'尚未选择项目';
 el('diskSelection').title=chosen.length?'包含跨页和筛选后隐藏的已选项目':'';
 el('diskBatchCount').textContent=job?job.disconnected?'连接中断，结果待确认':job.total>0?'已完成 '+(job.completed||0)+' / '+job.total:'正在准备':sameBatch()&&diskBatch.paths?.length?'已完成 '+(diskBatch.completed?.length||0)+' / '+diskBatch.paths.length:'';
 el('diskSelectPage').disabled=locked||!rows.length;el('diskSelectPage').checked=!!rows.length&&rows.every(project=>diskSelected.has(project.path));el('diskSelectPage').indeterminate=rows.some(project=>diskSelected.has(project.path))&&!el('diskSelectPage').checked;
 el('diskStart').hidden=activeTab!=='space'||spaceMode!=='disk';
}
function renderDisk(){
 const projects=diskReport?.projects||[],ids=[...new Set(projects.flatMap(project=>project.agents||[project.source]))],previous=el('diskAgent').value;
 el('diskAgent').innerHTML=(ids.length>1?'<option value="">全部 Agent</option>':'')+ids.map(id=>'<option value="'+esc(id)+'">'+esc(agentName(id))+'</option>').join('');
 if([...el('diskAgent').options].some(option=>option.value===previous))el('diskAgent').value=previous;
 el('diskAgent').hidden=!ids.length;
 const rows=filteredProjects();diskPage=Math.max(0,Math.min(diskPage,Math.ceil(rows.length/30)-1));
 el('diskCount').textContent=projects.length?rows.length+' 个项目':'项目清单';el('diskSize').textContent=projects.length?(projects.some(project=>project.space?.state!=='completed')?'已统计 ':'占用 ')+projectBytes(projectTotal(rows)):'';
 for(const id of ['diskDirectories','diskPager','diskFooter'])el(id).hidden=!projects.length;
 el('diskEmpty').hidden=!!projects.length;el('diskSearch').disabled=!projects.length;el('diskStatusFilter').disabled=!projects.length;
 const job=projectJob(),locked=!!job||diskStarting||projectStarting,scroll=el('diskDirectories').scrollTop;
 el('diskRows').innerHTML=rows.slice(diskPage*30,(diskPage+1)*30).map((project,index)=>{
  const stats=project.space,analysis=project.analysis,state=projectStatus(project),tip=analysis?.reason||stats?.reason|| (stats?.state==='partial'?'部分位置尚未完整统计':'');
  return '<tr class="disk-directory" data-project="'+esc(project.path)+'"><td class="project-pick"><input id="disk-project-'+index+'" data-project-select="'+esc(project.path)+'" type="checkbox" aria-label="选择项目 '+esc(project.name)+'" '+(diskSelected.has(project.path)?'checked ':'')+(locked?'disabled':'')+'></td><td><label class="project-name" for="disk-project-'+index+'" title="'+esc(project.name+'\n'+project.path)+'">'+esc(project.name)+'</label></td><td class="num">'+(Number.isFinite(stats?.allocated_bytes)?(stats.state==='partial'?'≥ ':'')+projectBytes(stats.allocated_bytes):'—')+'</td><td class="num">'+(Number.isFinite(stats?.files)?stats.files.toLocaleString('zh-CN'):'—')+'</td><td class="project-state" data-project-state="'+esc(project.path)+'" title="'+esc(tip)+'" role="status">'+projectStatusMarkup(project)+'</td></tr>';
 }).join('')||'<tr><td colspan="5">没有匹配的项目</td></tr>';
 el('diskDirectories').scrollTop=scroll;el('diskPageInfo').textContent='第 '+(diskPage+1)+' / '+Math.max(1,Math.ceil(rows.length/30))+' 页';el('diskPrev').disabled=!diskPage;el('diskNext').disabled=(diskPage+1)*30>=rows.length;
 renderProjectControls();
}
function showSpacePage(mode){if(spaceMode!==mode)spaceNavigation++;spaceMode=mode;el('diskPanel').hidden=mode!=='disk';el('spaceAdvicePanel').hidden=mode!=='advice';if(activeTab==='space')el('viewTitle').textContent=mode==='advice'?'空间清理 / 清理建议':'空间清理';renderProjectControls()}
async function loadSpace(){
 const navigation=spaceNavigation,mode=spaceMode;
 const value=await work('disk-report');diskReport=value.report;diskBatch=value.batch||{};
 if(!diskSelected.size&&diskBatch.state==='running')for(const path of diskBatch.paths||[])diskSelected.add(path);
 if(diskReport){const known=new Set(diskReport.projects.map(project=>project.path));for(const path of diskSelected)if(!known.has(path))diskSelected.delete(path)}
 saveProjectSelection();showSpacePage(spaceMode);renderDisk();
 el('diskStatus').hidden=!diskReport||diskReport.state==='completed'||!!projectJob();el('diskStatus').textContent='上次扫描尚未完成，可重新扫描。';
 if(mode==='advice'&&spaceMode==='advice'&&navigation===spaceNavigation&&activeTab==='space')await loadProjectAdvice(spaceAdvicePaths);
}
async function openProjectScan(){
 if(projectJob()||diskStarting||projectStarting||scopeLoading)return;
 scopeLoading=true;el('projectAgentChoices').innerHTML='<p class="project-running"><span class="loading-ring" aria-hidden="true"></span>正在识别 Agent 和项目…</p>';el('projectScopeFeedback').hidden=true;el('projectScopeSelection').textContent='';projectEligible('projectScanConfirm',false);el('projectScanDialog').showModal();
 try{diskCatalog=await work('space-catalog');scopeAgents=new Set(diskCatalog.selection.agents);scopeProjects=new Set(diskCatalog.selection.ids);scopeOpen=null;scopeQueries={};renderProjectAgents()}
 catch(error){el('projectScopeFeedback').textContent=friendly(error.message);el('projectScopeFeedback').hidden=false}
 finally{scopeLoading=false}
}
function agentScopeProjects(agent,filtered=false){const query=filtered?searchText(scopeQueries[agent]||''):'';return (diskCatalog?.projects||[]).filter(project=>project.agents.includes(agent)&&(!query||searchText(project.name+' '+project.path).includes(query)))}
function renderProjectScope(agent){
 const host=document.querySelector('[data-project-scope-list="'+agent+'"]');if(!host)return;
 host.innerHTML=agentScopeProjects(agent,true).map(project=>'<label class="project-scope-item"><input type="checkbox" data-scope-project="'+esc(project.id)+'" '+(scopeProjects.has(project.id)?'checked':'')+'><span title="'+esc(project.path)+'">'+esc(project.name)+'</span></label>').join('')||'<p class="project-scope-note">没有匹配的项目</p>';
 updateProjectScopeSelection();
}
function updateProjectScopeSelection(){
 const chosen=(diskCatalog?.projects||[]).filter(project=>scopeProjects.has(project.id)&&project.agents.some(agent=>scopeAgents.has(agent)));
 for(const agent of diskCatalog?.agents||[]){
  const input=document.querySelector('[data-scope-agent="'+agent.id+'"]'),rows=agentScopeProjects(agent.id),amount=rows.filter(project=>scopeProjects.has(project.id)&&scopeAgents.has(agent.id)).length;
  if(input){input.checked=scopeAgents.has(agent.id);input.indeterminate=amount>0&&amount<rows.length}
  const label=document.querySelector('[data-scope-agent-count="'+agent.id+'"]');if(label)label.textContent=agent.supported?(rows.length?'已选 '+amount+' / '+rows.length+' 个项目':'暂未找到项目'):'暂不支持项目扫描';
 }
 const all=el('projectAgentChoices').querySelector('[data-scope-all]');if(all){const rows=agentScopeProjects(all.dataset.scopeAll,true);all.checked=!!rows.length&&rows.every(project=>scopeProjects.has(project.id));all.indeterminate=rows.some(project=>scopeProjects.has(project.id))&&!all.checked;all.disabled=!rows.length}
 el('projectScopeSelection').textContent='已选 '+scopeAgents.size+' 个 Agent · '+chosen.length+' 个项目';projectEligible('projectScanConfirm',chosen.length>0&&!scopeLoading);
}
function renderProjectAgents(){
 el('projectAgentChoices').innerHTML=(diskCatalog?.agents||[]).map(agent=>{
  const open=scopeOpen===agent.id;
  return '<div class="project-agent-row"><label><input data-scope-agent="'+esc(agent.id)+'" type="checkbox" '+(!agent.supported?'disabled':'')+'><span>'+esc(agent.name)+'</span></label><small data-scope-agent-count="'+esc(agent.id)+'"></small>'+(agent.supported&&agent.project_count?'<button data-project-manage="'+esc(agent.id)+'" aria-expanded="'+open+'">选择项目</button>':'')+'</div>'+(open?'<div class="project-scope-projects"><div class="project-scope-toolbar"><label class="search"><input type="search" data-project-scope-search="'+esc(agent.id)+'" value="'+esc(scopeQueries[agent.id]||'')+'" placeholder="搜索项目" aria-label="搜索待扫描项目"></label><label><input type="checkbox" data-scope-all="'+esc(agent.id)+'">全选搜索结果</label></div><div class="project-scope-list" data-project-scope-list="'+esc(agent.id)+'"></div></div>':'');
 }).join('')||'<p class="project-scope-note">尚未识别到 Agent。可以添加项目目录。</p>';
 scopeLoading=false;if(scopeOpen)renderProjectScope(scopeOpen);updateProjectScopeSelection();
}
el('diskStart').onclick=guard(openProjectScan);el('diskEmptyStart').onclick=guard(openProjectScan);
el('projectScanCancel').onclick=el('projectScanClose').onclick=()=>el('projectScanDialog').close();
el('projectScanDialog').addEventListener('click',event=>{if(event.target!==el('projectScanDialog'))return;const box=event.target.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)event.target.close()});
el('projectAgentChoices').addEventListener('click',event=>{const button=event.target.closest('[data-project-manage]');if(button){scopeOpen=scopeOpen===button.dataset.projectManage?null:button.dataset.projectManage;renderProjectAgents()}});
el('projectAgentChoices').addEventListener('input',event=>{const agent=event.target.dataset.projectScopeSearch;if(agent){scopeQueries[agent]=event.target.value;renderProjectScope(agent)}});
el('projectAgentChoices').addEventListener('change',event=>{
 const agent=event.target.dataset.scopeAgent,all=event.target.dataset.scopeAll,id=event.target.dataset.scopeProject;
 if(agent){event.target.checked?scopeAgents.add(agent):scopeAgents.delete(agent);for(const project of agentScopeProjects(agent))event.target.checked?scopeProjects.add(project.id):scopeProjects.delete(project.id)}
 if(all){for(const project of agentScopeProjects(all,true))event.target.checked?scopeProjects.add(project.id):scopeProjects.delete(project.id);scopeAgents.add(all)}
 if(id){event.target.checked?scopeProjects.add(id):scopeProjects.delete(id);if(scopeOpen)scopeAgents.add(scopeOpen)}
 if(scopeOpen&&(agent||all))renderProjectScope(scopeOpen);updateProjectScopeSelection();
});
el('projectAdd').onclick=guard(async()=>{
 const picked=await endpoint('/api/pick-directory',{});if(!picked.path)return;
 diskCatalog=await work('space-add-project',{path:picked.path,agent:[...scopeAgents][0]||'codex'});scopeAgents=new Set(diskCatalog.selection.agents);scopeProjects=new Set(diskCatalog.selection.ids);scopeOpen=null;renderProjectAgents();
});
async function startProjectScan(rescan=false){
 if(diskStarting||projectJob())return;
 const chosen=(diskCatalog?.projects||[]).filter(project=>scopeProjects.has(project.id)&&project.agents.some(agent=>scopeAgents.has(agent)));
 if(!rescan&&!chosen.length)return;
 const payload=rescan?{rescan:true}:{catalog_id:diskCatalog.id,agents:[...scopeAgents],ids:chosen.map(project=>project.id)};
 if(!rescan){diskReport={projects:chosen.map(project=>({...project,space:{state:'pending'}})),state:'running'};el('projectScanDialog').close()}
 diskStarting=true;el('diskSearch').value='';el('diskStatusFilter').value='';diskPage=0;showSpacePage('disk');renderDisk();
 try{diskReport=await work('disk-scan',payload);const valid=new Set(diskReport.projects.map(project=>project.path));for(const path of diskSelected)if(!valid.has(path))diskSelected.delete(path);saveProjectSelection();el('diskStatus').hidden=diskReport.state!=='partial';el('diskStatus').textContent=diskReport.stop_reason||'部分位置尚未完整统计，可重新扫描。'}
 finally{diskStarting=false;renderDisk()}
}
el('projectScanConfirm').onclick=guard(()=>startProjectScan(false));el('diskRescan').onclick=guard(()=>startProjectScan(true));
el('diskStop').onclick=guard(async()=>{await work(projectJob()?.action==='project-analyze'||projectStarting?'project-analysis-stop':'disk-stop')});
el('diskReconnect').onclick=guard(async()=>{const task=projectJob();if(task)await reconnect(task.id);await loadSpace()});
function changeProjectSelection(paths,checked){if(projectJob()||diskStarting||projectStarting)return;for(const path of paths)checked?diskSelected.add(path):diskSelected.delete(path);saveProjectSelection();renderDisk()}
el('diskSelectPage').onchange=event=>changeProjectSelection(filteredProjects().slice(diskPage*30,(diskPage+1)*30).map(project=>project.path),event.target.checked);
el('diskClearSelection').onclick=()=>{diskSelected.clear();saveProjectSelection();renderDisk()};
el('diskRows').addEventListener('change',event=>{if(event.target.dataset.projectSelect)changeProjectSelection([event.target.dataset.projectSelect],event.target.checked)});
el('diskRows').addEventListener('click',event=>{if(event.target.closest('input,label'))return;const row=event.target.closest('[data-project]');if(row)changeProjectSelection([row.dataset.project],!diskSelected.has(row.dataset.project))});
for(const id of ['diskSearch','diskAgent','diskStatusFilter'])el(id).addEventListener(id==='diskSearch'?'input':'change',()=>{diskPage=0;renderDisk()});
el('diskPrev').onclick=()=>{diskPage--;el('diskDirectories').scrollTop=0;renderDisk()};el('diskNext').onclick=()=>{diskPage++;el('diskDirectories').scrollTop=0;renderDisk()};
function applyProjectAnalyses(rows){const byPath=new Map(rows.map(row=>[row.path,row]));for(const project of diskReport?.projects||[])if(byPath.has(project.path))project.analysis=byPath.get(project.path);renderDisk()}
async function analyzeProjects(paths=[...diskSelected]){
 if(projectJob()||projectStarting||!diskReport?.id||!paths.length)return;
 projectStarting=true;projectStartingPaths=paths;diskBatch={report_id:diskReport.id,paths:[...diskSelected],completed:[],state:'running'};renderDisk();
 try{const result=await work('project-analyze',{report_id:diskReport.id,paths,context_paths:[...diskSelected]});applyProjectAnalyses(result.analyses);diskBatch={report_id:diskReport.id,paths:[...diskSelected],completed:result.analyses.map(row=>row.path),state:result.state,remaining:result.remaining};saveProjectSelection()}
 finally{projectStarting=false;projectStartingPaths=[];renderDisk()}
}
el('diskAnalyzeSelected').onclick=guard(async()=>{if(canViewAdvice())await loadProjectAdvice([...diskSelected]);else await analyzeProjects()});
async function loadProjectAdvice(paths){
 if(!diskReport?.id||!paths.length)return;
 const revision=++adviceRequest,navigation=spaceNavigation,reportId=diskReport.id;
 const data=await work('space-advice',{report_id:reportId,paths});
 if(revision!==adviceRequest||navigation!==spaceNavigation||activeTab!=='space'||diskReport?.id!==reportId)return;
 spaceAdvicePaths=paths;spaceAdviceData=data;spacePlanSelected.clear();showSpacePage('advice');renderProjectAdvice();
}
function filteredAdvice(){const query=searchText(el('spaceAdviceSearch').value),kind=el('spaceAdviceKind').value;return spaceAdviceData.groups.filter(group=>(!kind||group.kind===kind)&&(!query||searchText(group.title+' '+group.project_name).includes(query)))}
function renderProjectAdvice(){
 const rows=filteredAdvice(),chosen=spaceAdviceData.groups.filter(group=>spacePlanSelected.has(group.id));
 el('spaceAdviceCount').textContent=rows.length+' 项清理建议';el('spaceAdviceSize').textContent=rows.length?projectBytes(projectTotal(rows)):'';
 el('spacePlanRows').innerHTML=rows.map((group,index)=>'<tr data-project-plan="'+esc(group.id)+'"><td class="project-pick"><input id="project-plan-'+index+'" type="checkbox" data-plan-select="'+esc(group.id)+'" aria-label="选择 '+esc(group.project_name+' '+group.title)+'" '+(spacePlanSelected.has(group.id)?'checked':'')+'></td><td><label class="project-name" for="project-plan-'+index+'" title="'+esc(group.project)+'">'+esc(group.project_name+' · '+group.title)+'</label></td><td class="num">'+group.files.toLocaleString('zh-CN')+'</td><td class="num">'+projectBytes(group.allocated)+'</td><td class="project-impact">'+esc(group.impact)+'</td></tr>').join('')||'<tr><td colspan="5">暂无清理建议</td></tr>';
 el('spaceSelectPage').disabled=!rows.length;el('spaceSelectPage').checked=!!rows.length&&rows.every(group=>spacePlanSelected.has(group.id));el('spaceSelectPage').indeterminate=rows.some(group=>spacePlanSelected.has(group.id))&&!el('spaceSelectPage').checked;
 const incomplete=spaceAdviceData.missing.length||(spaceAdviceData.partial||[]).length||spaceAdviceData.cross_state==='pending'||spaceAdviceData.cross_state==='partial';
 el('spaceAdviceNotice').hidden=!incomplete;el('spaceAdviceNotice').textContent=spaceAdviceData.missing.length?spaceAdviceData.missing.length+' 个项目尚未完成分析。':'部分结果尚未核对，相关文件已保留。';el('spaceAnalyzeRemaining').hidden=!spaceAdviceData.missing.length;
 el('spaceSelection').textContent=chosen.length?'已选 '+chosen.length+' 项 · '+chosen.reduce((sum,group)=>sum+group.files,0).toLocaleString('zh-CN')+' 个文件 · '+projectBytes(projectTotal(chosen)):'尚未选择清理项';projectEligible('spaceMove',!!chosen.length&&!projectJob());
}
el('spaceAdviceBack').onclick=()=>{showSpacePage('disk');renderDisk()};
el('spaceAdviceSearch').oninput=renderProjectAdvice;el('spaceAdviceKind').onchange=renderProjectAdvice;
el('spaceSelectPage').onchange=event=>{for(const group of filteredAdvice())event.target.checked?spacePlanSelected.add(group.id):spacePlanSelected.delete(group.id);renderProjectAdvice()};
el('spacePlanRows').addEventListener('change',event=>{const id=event.target.dataset.planSelect;if(id){event.target.checked?spacePlanSelected.add(id):spacePlanSelected.delete(id);renderProjectAdvice()}});
el('spacePlanRows').addEventListener('click',event=>{if(event.target.closest('input,label'))return;const row=event.target.closest('[data-project-plan]');if(row){const id=row.dataset.projectPlan;spacePlanSelected.has(id)?spacePlanSelected.delete(id):spacePlanSelected.add(id);renderProjectAdvice()}});
el('spaceAnalyzeRemaining').onclick=guard(async()=>{const paths=spaceAdviceData.missing;showSpacePage('disk');await analyzeProjects(paths)});
el('spaceMove').onclick=guard(async()=>{
 const chosen=spaceAdviceData.groups.filter(group=>spacePlanSelected.has(group.id));if(!chosen.length)return;
 const result=await work('space-trash-batch',{report_id:diskReport.id,paths:spaceAdvicePaths,groups:chosen.map(group=>group.id),file_count:chosen.reduce((sum,group)=>sum+group.files,0),confirmed:true});
 await loadProjectAdvice(spaceAdvicePaths);message('已移入回收站 '+result.completed+' 个文件'+(result.skipped.length?'，'+result.skipped.length+' 个文件已保留。':'。'));
});
const beforeProjectTaskView=taskView;
taskView=function(value){
 beforeProjectTaskView(value);
 if(value.action==='disk-scan'){
  if(value.result?.projects||value.partial_report?.projects)diskReport=value.result||value.partial_report;
  renderDisk();
 }else if(value.action==='project-analyze'){
  if(value.result?.report_id&&diskReport?.id&&value.result.report_id!==diskReport.id)return;
  applyProjectAnalyses(value.result?.analyses||value.partial_analyses||[]);
  if(value.result){diskBatch={report_id:value.result.report_id||diskBatch.report_id,paths:diskBatch.paths?.length?diskBatch.paths:value.result.analyses.map(row=>row.path).concat(value.result.remaining||[]),completed:value.result.analyses.map(row=>row.path),state:value.result.state,remaining:value.result.remaining||[]};renderDisk()}
 }
 if(['disk-scan','project-analyze'].includes(value.action)&&value.state==='failed'){el('diskStatus').hidden=false;el('diskStatus').textContent=friendly(value.message||value.error)}
 if(['space-trash-batch','space-restore-many','trash-purge-many'].includes(value.action)&&activeTab==='trash')renderProjectRecycle();
};
function recycleGroups(){
 const groups=new Map();
 for(const item of artifactTrash.filter(row=>row.state==='available'||row.state==='interrupted')){
  const key=item.root+'::'+(item.category||'files'),title=item.category==='cache'?'程序缓存':item.category==='duplicates'?'重复素材':'项目文件';
  if(!groups.has(key))groups.set(key,{id:'files:'+key,name:item.root.split('/').at(-1)+' · '+title,type:'projects',ids:[],files:0,allocated:0,created_at:item.created_at,available:true});
  const group=groups.get(key);group.ids.push(item.id);group.files++;group.allocated+=item.allocated??item.size;group.available=group.available&&item.state==='available';
 }
 const skills=backupData.filter(backup=>backup.type==='quarantine'&&backup.state!=='purged').map(backup=>({id:'backup:'+backup.id,name:backup.name,type:'skills',ids:[backup.id],files:null,allocated:backup.size?.allocated_bytes||0,created_at:backup.created_at,available:backup.state==='available',retained:backup.retained,backup}));
 return [...skills,...groups.values()].sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at)));
}
function filteredRecycle(){const query=searchText(el('projectTrashSearch').value),kind=el('projectTrashKind').value;return recycleGroups().filter(row=>(!kind||row.type===kind)&&(!query||searchText(row.name).includes(query)))}
function renderProjectRecycle(){
 if(activeTab!=='trash')return;
 const rows=filteredRecycle(),all=recycleGroups(),chosen=all.filter(row=>projectTrashSelected.has(row.id)),locked=[...activeTasks.values(),...startingTasks.values()].some(task=>['space-trash-batch','space-restore-many','trash-purge-many'].includes(task.action)&&['queued','running'].includes(task.state));projectTrashPage=Math.max(0,Math.min(projectTrashPage,Math.ceil(rows.length/30)-1));
 el('backups').classList.add('unified-trash');el('projectRecycle').hidden=false;
 for(const id of ['createSnapshot','discoverBackups','importSnapshot'])el(id).hidden=true;
 el('emptyTrash').hidden=false;projectEligible('emptyTrash',!locked&&all.some(row=>row.available&&!row.retained));
 el('projectTrashCount').textContent=rows.length+' 项';el('projectTrashSize').textContent=projectBytes(projectTotal(rows));
 const visible=rows.slice(projectTrashPage*30,(projectTrashPage+1)*30);
 el('projectTrashRows').innerHTML=visible.map((row,index)=>'<tr data-recycle="'+esc(row.id)+'"><td class="project-pick"><input type="checkbox" id="recycle-'+index+'" data-recycle-select="'+esc(row.id)+'" aria-label="选择 '+esc(row.name)+'" '+(projectTrashSelected.has(row.id)?'checked ':'')+(!row.available?'disabled':'')+'></td><td><label class="project-name" for="recycle-'+index+'" title="'+esc(row.name)+'">'+esc(row.name)+(row.retained?' · 防止误删':'')+'</label></td><td>'+esc(row.type==='projects'?'项目文件':'Skill / 备份')+'</td><td class="num">'+(row.files??'—')+'</td><td class="num">'+projectBytes(row.allocated)+'</td><td>'+esc(time(row.created_at))+'</td></tr>').join('')||'<tr><td colspan="6">回收站为空</td></tr>';
 el('projectTrashSelectPage').checked=!!visible.length&&visible.filter(row=>row.available).every(row=>projectTrashSelected.has(row.id));el('projectTrashSelectPage').disabled=!visible.some(row=>row.available);
 el('projectTrashSelection').textContent=chosen.length?'已选 '+chosen.length+' 项 · '+projectBytes(projectTotal(chosen)):'尚未选择';projectEligible('projectTrashRestore',!locked&&chosen.some(row=>row.available));projectEligible('projectTrashPurge',!locked&&chosen.some(row=>row.available&&!row.retained));
 el('projectTrashPageInfo').textContent='第 '+(projectTrashPage+1)+' / '+Math.max(1,Math.ceil(rows.length/30))+' 页';el('projectTrashPrev').disabled=!projectTrashPage;el('projectTrashNext').disabled=(projectTrashPage+1)*30>=rows.length;
}
async function loadProjectRecycle(){
 if(recycleLoading)return recycleLoading;
 recycleLoading=(async()=>{artifactTrash=(await work('space-trash-list')).items;const valid=new Set(recycleGroups().map(row=>row.id));for(const id of projectTrashSelected)if(!valid.has(id))projectTrashSelected.delete(id);renderProjectRecycle()})();
 try{return await recycleLoading}finally{recycleLoading=null}
}
const beforeProjectBackups=renderBackups;
renderBackups=function(...args){beforeProjectBackups(...args);el('backups').classList.toggle('unified-trash',activeTab==='trash');el('projectRecycle').hidden=activeTab!=='trash';if(activeTab==='trash')renderProjectRecycle()};
const beforeProjectTab=tab;
tab=async function(...args){await beforeProjectTab(...args);el('backups').classList.toggle('unified-trash',activeTab==='trash');el('diskStart').hidden=activeTab!=='space'||spaceMode!=='disk';if(activeTab==='trash')await loadProjectRecycle()};
for(const id of ['projectTrashSearch','projectTrashKind'])el(id).addEventListener(id==='projectTrashSearch'?'input':'change',()=>{projectTrashPage=0;projectTrashSelected.clear();renderProjectRecycle()});
el('projectTrashSelectPage').onchange=event=>{for(const row of filteredRecycle().slice(projectTrashPage*30,(projectTrashPage+1)*30))if(row.available)event.target.checked?projectTrashSelected.add(row.id):projectTrashSelected.delete(row.id);renderProjectRecycle()};
el('projectTrashRows').addEventListener('change',event=>{const id=event.target.dataset.recycleSelect;if(id){event.target.checked?projectTrashSelected.add(id):projectTrashSelected.delete(id);renderProjectRecycle()}});
el('projectTrashRows').addEventListener('click',event=>{if(event.target.closest('input,label'))return;const row=event.target.closest('[data-recycle]');if(row){const item=recycleGroups().find(item=>item.id===row.dataset.recycle);if(item?.available){projectTrashSelected.has(item.id)?projectTrashSelected.delete(item.id):projectTrashSelected.add(item.id);renderProjectRecycle()}}});
el('projectTrashPrev').onclick=()=>{projectTrashPage--;renderProjectRecycle()};el('projectTrashNext').onclick=()=>{projectTrashPage++;renderProjectRecycle()};
el('projectTrashRestore').onclick=guard(async()=>{
 const rows=recycleGroups().filter(row=>projectTrashSelected.has(row.id)&&row.available),files=rows.filter(row=>row.type==='projects').flatMap(row=>row.ids),skills=rows.filter(row=>row.type==='skills').flatMap(row=>row.ids);
 if(files.length){const result=await work('space-restore-many',{ids:files,confirmed:true});if(result.skipped.length)message(result.skipped.length+' 个文件未恢复，原位置可能已有文件。',true)}
 if(skills.length)await restoreBackups(skills);
 projectTrashSelected.clear();await loadProjectRecycle();await loadBackups();
});
async function purgeProjectRecycle(all=false){
 const rows=recycleGroups().filter(row=>(all||projectTrashSelected.has(row.id))&&row.available&&!row.retained);if(!rows.length)return;
 const result=await work('trash-purge-many',{artifact_ids:rows.filter(row=>row.type==='projects').flatMap(row=>row.ids),backup_ids:rows.filter(row=>row.type==='skills').flatMap(row=>row.ids),confirmed:true});
 projectTrashSelected.clear();await loadProjectRecycle();await loadBackups();message(result.skipped.length?'部分内容已保留，请刷新列表查看。':'已永久删除所选回收内容。',!!result.skipped.length);
}
el('projectTrashPurge').onclick=guard(()=>purgeProjectRecycle(false));el('emptyTrash').onclick=guard(()=>purgeProjectRecycle(true));
document.addEventListener('keydown',event=>{if(event.key!=='Escape'||event.defaultPrevented||event.target.closest?.('dialog')||document.querySelector('dialog[open]'))return;if(activeTab==='space'){if(spaceMode==='disk'){diskSelected.clear();saveProjectSelection();renderDisk()}else{spacePlanSelected.clear();renderProjectAdvice()}}else if(activeTab==='trash'){projectTrashSelected.clear();renderProjectRecycle()}});
