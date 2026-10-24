// Real panel frontend against isolated HTTP fixtures: project tree entry, add-folder
// dialog with the native picker, real storage state. Every declared panel asset is
// served, as the real server and the Codex bundle do.
// Every response is served by the local fixture below; no live workbench or record
// is read or written, so the check stays offline by default.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require('./browser_helpers.cjs');
const web=path.resolve(__dirname,'../panel');
const ASSETS={'/':'index.html','/app.js':'app.js','/workspace.js':'workspace.js','/reconciliation.js':'reconciliation.js','/governance.js':'governance.js','/flow.js':'flow.js','/style.css':'style.css','/workspace.css':'workspace.css','/governance.css':'governance.css','/reconciliation.css':'reconciliation.css','/flow.css':'flow.css'};
const A='C:\\fixture\\A',B='C:\\fixture\\B',N='C:\\fixture\\New';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const base={models:[{id:'home',name:'Qwen',provider:'local',api_base:'http://127.0.0.1:9/v1',model:'fixture-model',enabled:true,
    bypass_proxy:true,max_input_tokens:32768,max_output_tokens:4096,last_check:{ok:true,latency_ms:120},
    default_agent:'follow_global',default_agent_valid:true}],
  default_model:'home',workspace:'C:\\fixture',
  agent:{ready:true,edit_sandbox_mode:'danger-full-access',chat_sandbox_mode:'read-only'},
  workflow:{enabled:true,readable:true,error:'',paid_allowed:true,enforce_paid_budget:false,enforce_model_call_limit:false,
    task_budget_cny:5,daily_budget_cny:20,max_model_calls:30,model_scope:'all',execution_agent:'dsh',
    daily:{settled_cny:0,has_actual:false,unsettled_cny:0,unknown_calls:0}},
  execution_agents:{dsh:{name:'DeepSeek Harness',ready:true,configuration:{description:'',model_fields:[]}},
    pi:{name:'Pi Agent',ready:true,configuration:{description:'',model_fields:[]}}},
  codex_usage:{status:'ready',buckets:[]},
  delegation:{delegated:0,executor_completed:0,accepted:0,returned:0,recovery_pending:0},
  stages:{stats:{goals:0,stages:0,accepted:0,recovery_pending:0,by_status:{}},recovery:[]},
  server_time:new Date().toISOString()};
async function main(){
 const mutations=[],errors=[],requests=[],storageReads=[],workspaceWrites=[];
 let nextPick={status:'cancelled'},pickerResult=null,pickCount=0,pickCreateDelayMs=0;
 const tasks=[A,B].map((project,i)=>({id:i?'b':'a',title:i?'TASK_B':'TASK_A',kind:'chat',engine:'dsh',model_id:'home',model_name:'Qwen',project,status:'completed',phase:'回复完成',dsh_session_id:'s'+i,created_at:new Date().toISOString()}));
 const storage={
  [A]:{path:A+'\\.model-desk\\records.db',project:A,scope:'project',initialized:true,exists:true,writable:true,linked:false,reason:'',size_bytes:40960,free_bytes:8192},
  [B]:{path:B+'\\.model-desk\\records.db',project:B,scope:'project',initialized:true,exists:true,writable:true,linked:false,reason:'',size_bytes:20480,free_bytes:0}};
 const uninitialized=project=>({path:project+'\\.model-desk\\records.db',project,scope:'project',initialized:false,exists:false,writable:true,linked:false,reason:'项目尚未初始化记录库',size_bytes:0,free_bytes:0});
 const legacyRow={path:'E:\\fixture-workbench\\stages.db',scope:'legacy',initialized:true,exists:true,size_bytes:65536};
 const server=http.createServer(async(req,res)=>{
  const pathname=new URL(req.url,'http://local').pathname;
  const json=(v,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(v));};
  if(!pathname.startsWith('/api/')){const file=ASSETS[pathname];if(!file)return json({},404);res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});return res.end(fs.readFileSync(path.join(web,file)));}
  let view;try{view=JSON.parse(Buffer.from(req.headers['x-model-desk-view']||'','base64url').toString('utf8'));}catch{return json({error:'missing explicit view'},400);}
  requests.push({pathname,view});
  // Sidebar registration/selection is installation workspace config, like remember: never a record write.
  if(pathname==='/api/workspace/projects'&&req.method==='POST'){const body=await new Promise(resolve=>{let raw='';req.on('data',c=>raw+=c);req.on('end',()=>resolve(JSON.parse(raw||'{}')));});workspaceWrites.push({action:body.action,path:body.path});return json({ok:true});}
  if(req.method!=='GET'&&!['/api/projects/pick','/api/projects/remember'].includes(pathname))mutations.push(pathname);
  if(pathname==='/api/projects/pick'&&req.method==='POST'){pickCount++;if(pickCreateDelayMs)await sleep(pickCreateDelayMs);pickerResult={id:'c'.repeat(32),...nextPick};return json({id:pickerResult.id,status:'pending'});}
  if(pathname==='/api/projects/pick/'+'c'.repeat(32))return json(pickerResult);
  if(pathname==='/api/db/space'){
    storageReads.push(view.mode==='project'?view.project:view.mode);
    return view.mode==='project'
      ? json({scope:'project',project:view.project,databases:[storage[view.project]||uninitialized(view.project)]})
      : json({scope:view.mode,project:null,databases:[legacyRow]});
  }
  if(pathname==='/api/db/compact'&&req.method==='POST'){
    if(view.mode!=='project')return json({error:'请先明确选择要压缩的项目'},400);
    const row=storage[view.project];
    if(!row||!row.exists)return json({scope:'project',project:view.project,compacted:[],reclaimed_bytes:0,initialized:false,message:'该项目尚无 records.db，无需压缩'});
    return json({scope:'project',project:view.project,compacted:[{path:row.path,before:row.size_bytes,after:row.size_bytes-4096,reclaimed_bytes:4096}],reclaimed_bytes:4096});
  }
  const selected=view.mode==='all'?tasks:view.mode==='project'?tasks.filter(t=>t.project===view.project):[];
  const goals=selected.map(t=>({id:'g'+t.id,title:'GOAL_'+t.id.toUpperCase(),project:t.project,status:'active',stages:[],stage_count:0,accepted_count:0}));
  const stats={goals:goals.length,stages:0,accepted:0,recovery_pending:0,by_status:{}};
  if(pathname==='/api/session')return json({token:'fixture'});
  if(pathname==='/api/projects')return json({projects:[A,B,N].map(p=>({path:p,label:p})),recent_projects:[{path:A}]});
  if(pathname==='/api/projects/remember')return json({projects:[{path:view.project}]});
  if(pathname==='/api/state')return json({...base,tasks:selected,workspace:'E:\\wrong\\installation',view,execution_activity:{scope:'global',total:1,complete:true,tasks:[{id:'active-a',title:'LIVE_JOB_A',status:'running',project:A,model_id:'home',model_name:'Qwen'}]},delegation:{delegated:0},stages:{stats,recovery:[]},workflow:{...base.workflow,paid_allowed:false,daily_scope:'global'}});
  if(pathname==='/api/tree')return json({goals,stats,recovery:[],view});
  if(pathname==='/api/workspace')return json({projects:[A,B,N].map(p=>({path:p,name:p.split('\\').pop(),managed:true,available:true,bindings:[{id:'exec-'+p.split('\\').pop(),model_id:'home',role:'execution',enabled:true,is_default:true,ready:true,status:'ready',model:base.models[0]}],work_concurrency:1,waiting_tasks:[]})),accounts:{},catalogs:{},view});
  if(pathname==='/api/governance')return json({goals:[],decisions:[],policy:{},view});
  const id=pathname.split('/')[3],task=selected.find(t=>t.id===id);
  if(pathname.endsWith('/observability'))return json({task_id:id,cost:{known:false},view});
  if(pathname==='/api/tasks/a')return setTimeout(()=>json({task,prompt:'OLD_A_PROMPT'}),350);
  if(task)return json({task,prompt:'B_PROMPT'});
  return json({error:'fixture denied'},403);
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1280,height:800}});
  context.setDefaultTimeout(12000);
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  const url=`http://127.0.0.1:${server.address().port}/`;
  const checks=[];const ok=name=>checks.push(name);
  const ready=p=>p.waitForFunction(()=>/最近同步/.test(document.getElementById('last-sync')?.textContent||''));
  const narrow=p=>p.evaluate(()=>innerWidth<=900);
  const openDrawer=async p=>{if(await narrow(p)&&!(await p.locator('#desk-project-tree').isVisible()))await p.locator('#sidebar-toggle').click();await p.locator('#desk-project-tree').waitFor({state:'visible'});};
  const treeItem=async(p,project)=>{await openDrawer(p);await p.locator('#desk-project-tree .project-label').first().waitFor();const handle=await p.evaluateHandle(proj=>[...document.querySelectorAll('#desk-project-tree [data-desk-select-project]')].find(n=>n.dataset.deskSelectProject===proj),project);const node=handle.asElement();if(!node)throw new Error('tree item missing: '+project);return node;};
  const pick=async(p,project)=>{await (await treeItem(p,project)).click();await p.waitForFunction(proj=>projectView.mode==='project'&&projectView.project===proj,project);};
  const showTasks=async p=>{if(!(await p.locator('#panel-tasks').isVisible()))await p.locator('#tab-tasks').click();};
  const headerPath=p=>p.locator('#desk-header-path').getAttribute('title');
  const openAdd=async p=>{await openDrawer(p);await p.locator('#desk-add-project').click();await p.locator('#desk-project-dialog').waitFor({state:'visible'});};
  const pickerSettled=p=>p.waitForFunction(()=>{const b=document.querySelector('#desk-folder-pick');return !!b&&!b.disabled;},{timeout:20000});
  const setAppearance=(p,font,theme)=>p.evaluate(([font,theme])=>{for(const [id,value] of [['font-scale',font],['theme-mode',theme]]){const s=$(id);s.value=value;s.dispatchEvent(new Event('change',{bubbles:true}));}},[font,theme]);

  await page.goto(url);await ready(page);
  // 1) unbound: the main area offers the known projects; no tabs, tasks, path or project actions.
  assert.equal(await page.locator('.task-row').count(),0);
  assert.equal((await page.locator('#desk-page-title').textContent()).trim(),'项目工作台');
  assert.equal(await page.locator('#desk-header-path').isVisible(),false);
  assert.equal(await page.locator('#tab-tasks').isVisible(),false);
  assert.equal(await page.locator('.header-actions').isVisible(),false);
  assert.match(await page.locator('.desk-welcome').textContent(),/选择一个项目/);
  assert.equal(await page.locator('.welcome-project').count(),3);
  assert.match(await page.locator('#task-list').textContent(),/尚未选择项目/);
  assert.equal(await page.locator('#execution-activity').isVisible(),true);
  assert.match(await page.locator('#execution-activity').textContent(),/LIVE_JOB_A/);
  assert.equal((await page.locator('#delegation-stats').textContent()).trim(),'委派 0 · 待验收 0 · 通过 0 · 退回 0');
  ok('unbound shows the project picker only; no tabs, tasks, path or project actions');

  // 2) the project tree lists known projects; its menu closes on Escape/outside click and returns focus.
  assert.equal(await page.locator('#desk-project-tree .project-label').count(),3);
  const menuButton=page.locator('#desk-project-tree [data-desk-project-menu]').first();
  await menuButton.click();
  await page.locator('#desk-context-menu').waitFor({state:'visible'});
  assert.match(await page.locator('#desk-context-menu').textContent(),/打开项目/);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#desk-context-menu').isVisible(),false);
  assert.equal(await page.evaluate(()=>document.activeElement?.hasAttribute('data-desk-project-menu')),true);
  await menuButton.click();await page.locator('#desk-context-menu').waitFor({state:'visible'});
  await page.locator('h1').click();
  assert.equal(await page.locator('#desk-context-menu').isVisible(),false);
  ok('project tree lists known projects; its menu closes on Escape/outside click');

  // 3) explicit tree selection binds B; the full path stays visible as read-only text.
  await pick(page,B);await showTasks(page);await page.locator('[data-task-id="b"]').waitFor();
  assert.equal((await page.locator('#desk-page-title').textContent()).trim(),'B');
  assert.equal(await headerPath(page),B);
  assert.equal(await page.locator('#desk-header-path').isVisible(),true);
  assert.equal(await page.locator('#desk-header-path').evaluate(el=>el.tagName),'CODE');
  assert.equal(await page.locator('#desk-header-path').evaluate(el=>el.isContentEditable),false);
  ok('tree selection binds B; the full path is shown read-only with its title');

  // 4) native picker fills the add-folder path; selected / cancelled / failed never bind on their own.
  nextPick={status:'selected',project:A};await openAdd(page);await page.locator('#desk-folder-pick').click();await pickerSettled(page);
  assert.equal(await page.locator('#desk-project-dialog #desk-project-path').inputValue(),A);
  assert.equal(projectFromView(await page.evaluate(()=>projectView)),B,'a picked folder is not bound before 添加项目');
  await page.locator('#desk-project-form [type=submit]').click();
  await page.waitForFunction(proj=>projectView.project===proj,A);
  assert.equal(await headerPath(page),A);
  assert.equal(new URL(page.url()).searchParams.get('project'),A);
  nextPick={status:'cancelled'};await openAdd(page);await page.locator('#desk-folder-pick').click();await pickerSettled(page);
  assert.equal(await page.locator('#desk-project-dialog #desk-project-path').inputValue(),'');
  assert.equal((await page.locator('#desk-project-error').textContent()).trim(),'');
  nextPick={status:'failed',error:'picker fixture error'};await page.locator('#desk-folder-pick').click();await pickerSettled(page);
  assert.match(await page.locator('#desk-project-error').textContent(),/picker fixture error/);
  await page.locator('#desk-project-dialog .dialog-heading [data-desk-close]').click();
  assert.equal(await headerPath(page),A);
  assert.equal(pickCount,3);
  ok('native picker selected/cancelled/failed only fills the add-folder path; binding needs 添加项目');

  // 5) reload / same-panel return remember the binding; an explicit URL project wins over it.
  await page.reload();await ready(page);await showTasks(page);await page.locator('[data-task-id="a"]').waitFor();
  assert.equal(await headerPath(page),A);
  await page.goto(url);await ready(page);await showTasks(page);await page.locator('[data-task-id="a"]').waitFor();
  assert.equal(await headerPath(page),A,'same panel session restores its binding');
  await page.goto(`${url}?project=${encodeURIComponent(B)}`);await ready(page);await showTasks(page);await page.locator('[data-task-id="b"]').waitFor();
  assert.equal(await headerPath(page),B,'an explicit URL project wins over the remembered one');
  ok('refresh/same-panel memory and explicit-URL priority');

  // 6) independent panels: a new panel starts unbound and binds only on explicit choice.
  const other=await context.newPage();other.on('pageerror',e=>errors.push(e.message));
  await other.goto(url);await ready(other);
  assert.equal((await other.locator('#desk-page-title').textContent()).trim(),'项目工作台');
  assert.equal(await other.locator('.task-row').count(),0);
  await pick(other,A);await showTasks(other);await other.locator('[data-task-id="a"]').waitFor();
  assert.equal(await page.locator('[data-task-id="b"]').count(),1);
  assert.equal(await other.locator('[data-task-id="b"]').count(),0);
  ok('panels keep independent projects; a new panel binds a known project only when explicitly chosen');

  // 7) real storage state: an uninitialized project is not reported as lost records, and nothing is written.
  await pick(other,N);
  await other.waitForFunction(()=>/尚未初始化记录/.test(document.querySelector('#project-storage-state').textContent));
  assert.match(await other.locator('#project-storage-state').textContent(),/不是记录丢失/);
  assert.equal(await other.locator('#project-storage-state').isVisible(),true);
  await showTasks(other);
  await other.waitForFunction(()=>/尚未初始化记录/.test(document.querySelector('#task-list').textContent));
  assert.ok(storageReads.includes(N),'the uninitialized project state is read from /api/db/space, not guessed');
  assert.deepEqual(mutations,[],'binding an uninitialized project must not write any record');
  ok('uninitialized project shows 尚未初始化记录 (never lost records) and writes no record');

  // 8) a late task response after a project switch never lands in the new detail.
  await page.goto(`${url}?project=${encodeURIComponent(A)}`);await ready(page);await showTasks(page);await page.locator('[data-task-id="a"]').waitFor();
  await page.locator('[data-task-id="a"]').click();
  await page.locator('[data-tab="prompt"]').evaluate(e=>e.click());
  await pick(page,B);await showTasks(page);await page.locator('[data-task-id="b"]').waitFor();
  await sleep(500);
  assert.ok(!(await page.locator('#task-detail').textContent()).includes('OLD_A_PROMPT'));
  ok('a late task response after switching projects is discarded');

  // 9) a late picker result never binds or overrides a newer explicit choice.
  await page.goto(`${url}?project=${encodeURIComponent(A)}`);await ready(page);
  nextPick={status:'selected',project:N};pickCreateDelayMs=1200;
  await openAdd(page);await page.locator('#desk-folder-pick').click();
  await sleep(300);await page.locator('#desk-project-dialog .dialog-heading [data-desk-close]').click();
  await pick(page,B);await showTasks(page);await page.locator('[data-task-id="b"]').waitFor();
  await pickerSettled(page);await sleep(900);
  pickCreateDelayMs=0;
  assert.equal(await headerPath(page),B,'stale picker must not overwrite the newer choice');
  assert.equal(workspaceWrites.some(w=>w.path===N&&w.action==='add'),false,'a late pick never registers a folder');
  ok('a stale picker result never binds or overwrites a newer explicit project choice');

  // 10) new task / goal prefill the explicitly bound project directory.
  await page.locator('#new-task').click();assert.equal(await page.locator('#task-project').inputValue(),B);await page.locator('#task-dialog .close-dialog').first().click();
  // 新建目标 belongs to the governance module: a goal dialog scoped to (and naming) the bound project.
  await page.locator('#new-goal').click();await page.locator('#gov-goal-dialog').waitFor({state:'visible'});
  assert.equal(await page.locator('#gov-goal-dialog .gov-dialog-scope').getAttribute('title'),B);
  assert.match(await page.locator('#gov-goal-dialog .gov-dialog-scope').textContent(),/保存到项目：B/);
  await page.locator('#gov-goal-dialog .dialog-heading [data-gov-close]').click();
  ok('new task prefills, and the new goal dialog names, the bound project directory');

  // 11) all-projects is an explicit URL/host view: only the cross-project task list; records need one project.
  await page.goto(`${url}?view=all`);await ready(page);await page.locator('[data-task-id="b"]').waitFor();
  assert.equal(await page.locator('.task-row').count(),2);
  assert.equal((await page.locator('#desk-page-title').textContent()).trim(),'全部项目');
  assert.deepEqual(await page.locator('.desk-tabs [role=tab]:visible').allTextContents().then(t=>t.map(v=>v.replace(/\d+$/,'').trim())),['任务']);
  assert.equal(await page.locator('.header-actions').isVisible(),false);
  assert.match(await page.locator('[data-task-id="a"] .task-project').textContent(),/A/);
  await page.locator('#task-history-open').click();
  assert.equal(await page.evaluate(()=>$('task-records-dialog').open),false);
  assert.match(await page.locator('#toast').textContent(),/只按具体项目操作/);
  ok('all-projects stays explicit; record management still requires a concrete project');

  // 12) theme + narrow screens: no horizontal overflow; the project menu stays inside the viewport.
  const layouts=[];
  for(const spec of [{width:1280,height:600,font:'standard',theme:'light'},{width:480,height:640,font:'xlarge',theme:'dark'},{width:380,height:560,font:'xlarge',theme:'light'}]){
   await page.setViewportSize({width:spec.width,height:spec.height});
   await page.goto(`${url}?project=${encodeURIComponent(A)}`);await ready(page);await setAppearance(page,spec.font,spec.theme);
   await showTasks(page);await page.locator('[data-task-id="a"]').waitFor();
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,`page overflow at ${spec.width}`);
   await openDrawer(page);
   const button=page.locator('#desk-project-tree [data-desk-project-menu]').first();await button.click();
   const box=await page.locator('#desk-context-menu').boundingBox();
   assert.ok(box.x>=-1&&box.x+box.width<=spec.width+1,JSON.stringify({spec,box}));
   assert.ok(box.y>=-1&&box.y+box.height<=spec.height+1,JSON.stringify({spec,box}));
   await page.keyboard.press('Escape');
   layouts.push(spec);
  }
  // 13) settings dialog keeps its actions visible at the same narrow sizes (unchanged behavior).
  for(const spec of [{width:1280,height:600,font:'standard',theme:'light'},{width:480,height:640,font:'xlarge',theme:'dark'},{width:800,height:400,font:'xlarge',theme:'light'}]){
   await page.setViewportSize({width:spec.width,height:spec.height});await setAppearance(page,spec.font,spec.theme);
   await openDrawer(page);await page.locator('#desk-budget-open').click();
   const save=page.locator('#workflow-form button[type="submit"]'),cancel=page.locator('#workflow-form .dialog-footer button[type="button"]');
   for(const button of [save,cancel]){const rect=await button.boundingBox();assert.ok(rect.y>=0&&rect.y+rect.height<=spec.height+1,JSON.stringify({spec,rect}));assert.equal(await button.isVisible(),true);await button.click({trial:true});}
   await page.locator('#workflow-paid').check();assert.match(await page.locator('#workflow-save-status').textContent(),/未保存/);
   await page.locator('.workflow-scroll').evaluate(e=>{e.scrollTop=e.scrollHeight;});await page.locator('#workflow-max-calls').scrollIntoViewIfNeeded();
   const r=await save.boundingBox();assert.ok(r.y+r.height<=spec.height+1);await cancel.click();
  }
  assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);
  assert.ok(workspaceWrites.every(w=>['select','add'].includes(w.action)&&[A,B,N].includes(w.path)),JSON.stringify(workspaceWrites));
  const report={passed:true,checks,layouts,paid_generation:false,pickCount,mutations,workspaceWrites,errors,storageReads:[...new Set(storageReads)]};
  fs.writeFileSync(path.join(__dirname,'project-views-ui-report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }catch(error){console.error(error);throw error;}finally{await browser.close();server.closeAllConnections();await new Promise(r=>server.close(r));}
}
const projectFromView=view=>view?.mode==='project'?view.project:null;
main().catch(e=>{console.error(e);process.exitCode=1;});
