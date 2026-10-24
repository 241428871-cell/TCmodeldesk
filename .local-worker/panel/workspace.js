/* Project associations are configuration; activity comes from task and Goal receipts. */
(() => {
  const roles = {decision:'决策模型',execution:'执行模型'};
  const roleIcons = {decision:'i-compass',execution:'i-bolt'};
  const descriptions = {decision:'理解需求、制定方案与独立验收',execution:'按任务范围执行、测试并交付结果'};
  const names = {codex_cli:'Codex CLI',claude_cli:'Claude CLI',deepseek:'DeepSeek'};
  const tabNames = {overview:'概览',goal:'目标',tasks:'任务',models:'模型',settings:'设置'};
  let workspace = {projects:[],accounts:{},defaults:{}};
  let selectedBinding = null, pendingAssignment = null, editorAssignment = null, projectAction = 'add';
  let workspaceRequest = null, lastLoaded = 0, menuContext = null, menuReturnFocus = null;
  let libraryRole = 'execution', catalog = null, inspectorDirty = false, lastContentKey = '', settingsDirty = false, lastSettingsKey = '';
  let activeTab = 'overview';
  try{const saved=sessionStorage.getItem('model-desk.tab');if(tabNames[saved])activeTab=saved;}catch{}
  const catalogs = new Map();
  const sourceDrafts = new Map(), sourceSaving = new Set();
  const el = id => document.getElementById(id);
  const text = value => escapeText(String(value ?? ''));
  const icon = (id,cls='i') => `<svg class="${cls}" aria-hidden="true"><use href="#${id}"/></svg>`;
  const isCli = model => ['codex_cli','claude_cli'].includes(model?.provider);
  const boundPath = () => projectView.mode==='project'?projectView.project:null;
  const currentProject = () => workspace.projects.find(p => sameProjectPath(p.path,projectView.project));
  const currentBinding = () => currentProject()?.bindings.find(b => b.id === selectedBinding);
  const modelOf = binding => binding?.model || state.models.find(m => m.id === binding?.model_id);
  // Configured/enabled is never shown as running: only persisted receipts count.
  const statusName = binding => {
    const parts=[];
    if(binding.role==='decision'){
      if(binding.running_count)parts.push(binding.running_count===1?'已启动':`已启动 ${binding.running_count}`);
      if(binding.starting_count)parts.push(binding.starting_count===1?'启动中':`启动中 ${binding.starting_count}`);
      if(binding.unknown_count)parts.push(binding.unknown_count===1?'待核对':`待核对 ${binding.unknown_count}`);
      if(parts.length)return parts.join(' · ');
    }else if(binding.running_count)return binding.running_count===1?'运行中':`运行中 ${binding.running_count}`;
    if(binding.status==='unknown')return '待核对';
    if(binding.status==='starting')return '启动中';
    return !binding.enabled ? '已停用' : binding.ready ? '已就绪' : '未就绪';
  };
  const statusKind = binding => binding.status==='unknown'||binding.unknown_count ? 'unknown' : binding.running_count ? 'running' : binding.status==='starting'||binding.starting_count ? 'starting' : !binding.enabled ? 'disabled' : binding.ready ? 'ready' : 'unavailable';
  const modelLabel = binding => modelOf(binding)?.name || binding.model_id || '连接已移除';
  const statusMarkup = binding => `<span class="stag s-${statusKind(binding)}"><span class="sdot s-${statusKind(binding)}"></span>${statusName(binding)}</span>`;
  const endpoint = (model) => state.execution_endpoints?.rows?.find(e => e.id === (isCli(model) ? model.provider : effectiveModelAgent(model)));
  const nativeCapabilities = model => (catalogs.get(model.provider)?.models || []).find(m => m.id === model.model);
  const hm = value => {const date=new Date(value);return Number.isNaN(date.getTime())?'':date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});};
  const tokens = value => Number.isFinite(Number(value))?(Number(value)>=1000?`${Math.round(Number(value)/1000)}k`:String(value)):'—';
  const shortPath = path => {const value=String(path||'');return value.length>64?value.slice(0,20)+'…'+value.slice(-40):value;};
  const accountSources = {
    claude_cli:'Claude CLI 当前登录账户 · 原生 get_usage',
    codex_cli:'Codex 当前登录账户 · 原生 account/rateLimits/read',
    deepseek:'DeepSeek 官方 API 账户 · /user/balance',
  };
  function accountNotice(account,hasValues){
    const stateName={error:'读取失败',unavailable:'当前不可用',unknown:'尚未读取'}[account.state];
    const detail=account.error||account.message||'';
    if(stateName)return `<p class="account-notice ${account.state==='error'?'error':''}"><strong>${stateName}</strong>${detail?' · '+text(detail):''}</p>`;
    if(account.stale&&detail)return `<p class="account-notice"><strong>显示上次读取值</strong> · ${text(detail)}</p>`;
    return hasValues?'':`<p class="account-unknown">${text(detail||'当前没有可显示的账户数据。')}</p>`;
  }
  function accountProvenance(provider,account){
    const source=accountSources[provider]||names[provider]||provider;
    const adapter=account.source&& !source.includes(account.source)?` · 接口 ${account.source}`:'';
    const sampled=account.updated_at?` · ${clock(account.updated_at)} 采样`:' · 尚未采样';
    const stale=account.stale?' · 非实时数据':'';
    const expired=(account.windows||[]).some(window=>window.expired)?' · 部分窗口已过重置时间':'';
    return `<p class="account-meta account-provenance">来源：${text(source)}${text(adapter)}${text(sampled+stale+expired)}</p>`;
  }
  function closeSidebar(){const returnFocus=el('project-sidebar').contains(document.activeElement)&&innerWidth<=900;document.querySelector('.desk-shell').classList.remove('sidebar-open');el('sidebar-scrim').hidden=true;el('sidebar-toggle').setAttribute('aria-expanded','false');document.querySelector('.desk-shell>.app').inert=false;if(returnFocus)el('sidebar-toggle').focus();}
  function openSidebar(){document.querySelector('.desk-shell').classList.add('sidebar-open');el('sidebar-scrim').hidden=false;el('sidebar-toggle').setAttribute('aria-expanded','true');document.querySelector('.desk-shell>.app').inert=true;el('sidebar-close').focus();}

  // ---------- tabs: one visible panel; hidden panels keep their last render ----------
  function setTab(name,{focus=false}={}){
    if(!tabNames[name])return;
    if(allView())name='tasks';
    activeTab=name;try{sessionStorage.setItem('model-desk.tab',name);}catch{}
    for(const tab of document.querySelectorAll('[data-desk-tab]')){
      const on=tab.dataset.deskTab===name;
      tab.setAttribute('aria-selected',String(on));tab.tabIndex=on?0:-1;
      el('panel-'+tab.dataset.deskTab).hidden=!on;
      if(on&&focus)tab.focus();
    }
    el('desk-current-name').textContent=boundPath()?tabNames[name]:'项目总览';
  }
  // The explicit all-projects view (URL/host only) lists every project's tasks;
  // project-scoped tabs and actions stay hidden there.
  function allView(){return projectView.mode==='all';}
  function renderHeader(project){
    const path=boundPath(),all=allView();
    el('desk-page-title').textContent=project?.name||(path?projectBaseName(path):all?'全部项目':'项目工作台');
    el('desk-current-name').textContent=path?tabNames[activeTab]:all?'全部项目 · 任务汇总':'项目总览';
    const chip=el('desk-header-path');chip.hidden=!path;chip.textContent=shortPath(path);chip.title=path||'';
    document.body.dataset.project=path?'bound':all?'all':'none';
    if(all&&activeTab!=='tasks')setTab('tasks');
  }
  function renderWelcome(){
    const projects=workspace.projects||[];
    if(allView()){setMarkup(el('desk-welcome'),'');return;}
    if(!boundPath()&&projects.length){
      setMarkup(el('desk-welcome'),`<section class="desk-welcome"><h2>选择一个项目</h2><p>模型、目标和任务都按项目分开管理。打开项目只切换视图，不会调用模型。</p><ul class="welcome-projects">${projects.map(project=>{const rows=project.bindings||[];return `<li><button type="button" class="welcome-project" data-desk-select-project="${text(project.path)}" ${project.available===false?'aria-describedby="welcome-unavailable"':''}>${icon('i-folder','folder-icon')}<span class="welcome-project-text"><strong>${text(project.name)}</strong><small>${text(shortPath(project.path))}</small></span><span class="welcome-project-meta">${project.available===false?'<span class="stag s-unavailable"><span class="sdot s-unavailable" aria-hidden="true"></span>目录不可用</span>':`${projectFlags(project)}<small>${rows.length} 个模型</small>`}</span></button></li>`;}).join('')}</ul>${projects.some(project=>project.available===false)?'<p id="welcome-unavailable" class="field-hint">“目录不可用”表示该路径当前无法读取；记录仍保留，可在项目菜单中移除。</p>':''}<div class="desk-library-actions"><button class="button secondary" data-desk-add-project>${icon('i-plus')}添加项目文件夹</button></div></section>`);
      return;
    }
    setMarkup(el('desk-welcome'),boundPath()?'':`<section class="desk-welcome"><h2>从一个项目文件夹开始</h2><p>工作台按项目组织决策模型、执行模型、目标和任务。添加文件夹只记录位置，不会移动文件或调用模型。</p><ol class="welcome-steps"><li><span><strong>添加项目文件夹</strong>选择本机已有目录，左侧会出现项目树。</span></li><li><span><strong>为项目选择模型</strong>决策模型负责拆分与验收，执行模型负责具体任务。</span></li><li><span><strong>固定目标与准则</strong>主脑按准则动态生成阶段，需要你判断时会进入“待审核”。</span></li></ol><div class="desk-library-actions"><button class="button primary" data-desk-add-project>${icon('i-plus')}添加项目文件夹</button><button class="button secondary" data-desk-open-library>查看连接与模型库</button></div></section>`);
  }

  async function loadWorkspace(force = false) {
    if(workspaceRequest){await workspaceRequest;if(!force)return;}
    if(!force && Date.now()-lastLoaded<4000){renderAll();return;}
    workspaceRequest=(async()=>{
      try{
        const next=await api('/api/workspace');
        if(!Array.isArray(next.projects))throw new Error('项目配置返回格式不正确');
        workspace=next;lastLoaded=Date.now();for(const [provider,value] of Object.entries(next.catalogs||{})){if(value.models?.length)catalogs.set(provider,value);}renderAll();
      }catch(error){if(!error.stale)setMarkup(el('desk-project-tree'),`<p class="sidebar-empty">${text(error.message)}<br><button class="text-button" data-desk-retry>重新读取</button></p>`);}
    })().finally(()=>{workspaceRequest=null;});
    return workspaceRequest;
  }
  function projectFlags(project){
    const rows=project.bindings||[];
    const live=rows.reduce((sum,b)=>sum+(b.running_count||0)+(b.starting_count||0),0);
    const unknown=rows.reduce((sum,b)=>sum+(b.unknown_count||(b.status==='unknown'?1:0)),0);
    return `${live?`<span class="flag s-running" title="${live} 个已启动或运行中的会话/任务"><span class="sdot s-running"></span>${live}</span>`:''}${unknown?`<span class="flag s-unknown" title="${unknown} 个状态待核对，不是失败或已停止"><span class="sdot s-unknown"></span>${unknown}</span>`:''}${project.available===false?'<span class="flag s-unavailable" title="目录不存在或当前不可读；记录仍保留">不可用</span>':''}`;
  }
  function renderTree(){
    const tree=el('desk-project-tree'),filter=el('desk-project-search').value.trim().toLocaleLowerCase();
    const previousDetails=new Map([...tree.querySelectorAll('details')].map(n=>[n.dataset.folder,n.open]));
    const isOpen=(key,fallback)=>filter|| (previousDetails.has(key)?previousDetails.get(key):fallback);
    const focus=tree.contains(document.activeElement)?document.activeElement?.dataset?.deskSelectBinding:null;
    const markup=workspace.projects.map(project=>{
      const bindings=project.bindings||[],matchesProject=(project.name+' '+project.path).toLocaleLowerCase().includes(filter);
      const matching=bindings.filter(b=>matchesProject || (modelLabel(b)+' '+(modelOf(b)?.model||'')).toLocaleLowerCase().includes(filter));
      if(filter && !matchesProject && !matching.length)return '';
      const current=projectView.mode==='project'&&sameProjectPath(project.path,projectView.project);
      const folderKey='project:'+project.path;
      return `<details class="project-folder ${current?'current':''}" data-folder="${text(folderKey)}" ${isOpen(folderKey,current)?'open':''}><summary title="${text(project.path)}" tabindex="-1"><span class="tree-caret" aria-hidden="true"></span>${icon('i-folder','folder-icon')}<button class="project-label" data-desk-select-project="${text(project.path)}" ${current?'aria-current="page"':''}>${text(project.name)}</button><span class="project-flags">${projectFlags(project)}</span><button class="tree-menu-button" data-desk-project-menu="${text(project.path)}" aria-label="${text(project.name)}项目操作" aria-haspopup="menu">${icon('i-more')}</button></summary>${Object.entries(roles).map(([role,label])=>{
        const rows=filter?matching.filter(b=>b.role===role):bindings.filter(b=>b.role===role),key=project.path+':'+role;
        return `<details class="role-folder" data-folder="${text(key)}" ${isOpen(key,true)?'open':''}><summary tabindex="-1" data-role-summary="${role}"><span class="tree-caret" aria-hidden="true"></span>${icon(roleIcons[role],'i role-icon')}<span class="role-label">${label}</span><span class="role-count">${rows.length}</span><button class="tree-menu-button" data-desk-add-role="${role}" data-project="${text(project.path)}" aria-label="向${text(project.name)}添加${label}" title="添加${label}">${icon('i-plus')}</button></summary>${rows.map(binding=>{const kind=statusKind(binding);return `<div class="tree-model-row ${current&&selectedBinding===binding.id?'selected':''}" data-binding-row="${binding.id}" data-project="${text(project.path)}"><button class="tree-model-button" data-desk-select-binding="${binding.id}" data-project="${text(project.path)}" title="${text(modelOf(binding)?.model)} · ${statusName(binding)}"><span class="sdot s-${kind}" aria-hidden="true"></span><span class="tree-model-name">${text(modelLabel(binding))}</span>${binding.is_default?'<span class="tree-default" title="本角色默认模型"><span class="tree-default-text">默认</span><span class="tree-default-mark" aria-hidden="true">★</span></span>':''}<span class="tree-state s-${kind}">${statusName(binding)}</span></button><button class="tree-menu-button" data-desk-binding-menu="${binding.id}" data-project="${text(project.path)}" aria-label="${text(modelLabel(binding))}模型操作" aria-haspopup="menu">${icon('i-more')}</button></div>`;}).join('')||'<p class="tree-role-empty">尚未添加模型</p>'}</details>`;
      }).join('')}</details>`;
    }).join('');
    setMarkup(tree,markup||`<p class="sidebar-empty">${filter?'没有匹配的项目或模型。':'添加项目文件夹，再为它选择决策和执行模型。'}</p>${filter?'':'<button class="sidebar-nav-button" data-desk-add-project>'+icon('i-plus')+'<span>添加第一个项目</span></button>'}`);
    if(focus)tree.querySelector(`[data-desk-select-binding="${CSS.escape(focus)}"]`)?.focus({preventScroll:true});
  }

  // ---------- accounts: compact summary in the sidebar, full detail in a dialog ----------
  function accountSummaryRows(){
    const rows=[];
    const windowsOf=list=>(list||[]).filter(w=>Number.isFinite(w?.remaining_percent));
    const quota=(name,windows,updated,stale,stateName,source,detail)=>{
      if(windows.length){const low=windows.reduce((a,b)=>a.remaining_percent<=b.remaining_percent?a:b);const percent=Math.max(0,Math.min(100,low.remaining_percent));
        return {name,value:`${Math.round(percent)}%`,meter:percent,low:percent<20,meta:`剩余 · ${low.label||'最紧窗口'}`,time:updated?(stale?'上次 ':'')+hm(updated):'',state:stale?'过期':'',kind:stale?'waiting':'',source};}
      return {name,value:stateName==='读取失败'?'读取失败':'未知',valueClass:stateName==='读取失败'?'failed':'unknown',meta:detail||stateName||'尚未读取',time:updated?hm(updated):'',state:stateName||'未读取',kind:stateName==='读取失败'?'failed':'unknown',source};
    };
    const nativeCodex=workspace.accounts?.codex_cli,usage=state.codex_usage;
    if(nativeCodex?.updated_at)rows.push(quota('Codex 额度',windowsOf(nativeCodex.windows),nativeCodex.updated_at,nativeCodex.stale,{error:'读取失败',unavailable:'不可用',unknown:'未读取'}[nativeCodex.state],accountSources.codex_cli,nativeCodex.message));
    else{const windows=(usage?.buckets||[]).flatMap(bucket=>(bucket.windows||[]).map(w=>({...w,label:usageWindowLabel(w.window_minutes,w.slot)})));rows.push(quota('Codex 额度',windowsOf(windows),usage?.updated_at,usage?.stale,usage?.status==='error'?'读取失败':usage?.updated_at?'':'未读取','Codex 当前登录账号共享额度',usage?.error));}
    const claude=workspace.accounts?.claude_cli||{};
    rows.push(quota('Claude 额度',windowsOf(claude.windows),claude.updated_at,claude.stale,{error:'读取失败',unavailable:'不可用',unknown:'未读取'}[claude.state],accountSources.claude_cli,claude.message));
    const host=state.host_account;
    if(host&&typeof hostAccountPresentation==='function'){
      const shown=hostAccountPresentation(host),wallet=shown.rows.find(row=>row.label.startsWith('账户余额'))||shown.rows[0];
      rows.push(wallet?{name:'DSH 钱包',value:wallet.value,meta:'DSH 官方登录账户',time:host.fetched_at&&hm(host.fetched_at)?(shown.stale?'上次 ':'')+hm(host.fetched_at):'',state:shown.stale?'过期':'',kind:shown.stale?'waiting':'',source:shown.sourceText}:{name:'DSH 钱包',value:'未知',valueClass:'unknown',meta:shown.message||shown.statusText,state:shown.statusText,kind:host.status==='error'?'failed':'unknown',source:shown.sourceText});
    }
    const deepseek=workspace.accounts?.deepseek;
    if(deepseek){
      const balance=(deepseek.balance_infos||deepseek.balance||[])[0];
      rows.push(balance&&deepseek.state==='ready'||balance&&deepseek.stale?{name:'DeepSeek API',value:`${balance.currency==='CNY'?'¥':balance.currency+' '}${balance.total_balance??'未知'}`,meta:'官方 API 余额',time:deepseek.updated_at?(deepseek.stale?'上次 ':'')+hm(deepseek.updated_at):'',state:deepseek.stale?'过期':deepseek.available===false?'不可用':'',kind:deepseek.stale||deepseek.available===false?'waiting':'',source:accountSources.deepseek}:{name:'DeepSeek API',value:deepseek.state==='error'?'读取失败':'未知',valueClass:deepseek.state==='error'?'failed':'unknown',meta:deepseek.state==='error'?'官方 API 余额 · 未显示旧值':{unavailable:'未配置或不可用',unknown:'尚未读取'}[deepseek.state]||'尚未读取',kind:deepseek.state==='error'?'failed':'unknown',source:accountSources.deepseek});
    }
    // The DSH sidebar page leads with the account that host actually bills.
    if(isDshHost){const index=rows.findIndex(row=>row.name==='DSH 钱包');if(index>0)rows.unshift(...rows.splice(index,1));}
    return rows;
  }
  function renderAccountSummary(){
    setMarkup(el('desk-account-summary'),accountSummaryRows().map(row=>`<button type="button" class="acct-row" data-desk-open-accounts ${row.low?'data-low="true"':''} title="${text([row.source,row.source?.includes(row.meta)?'':row.meta,row.time&&'更新 '+row.time].filter(Boolean).join(' · '))}"><span class="acct-name">${text(row.name)}</span><span class="acct-value ${row.valueClass||''}">${text(row.value)}</span><span class="acct-meta">${row.state?`<span class="acct-state s-${row.kind||'unknown'}">${text(row.state)}</span>`:''}<span>${text(row.meta)}${row.time?' · '+text(row.time):''}</span></span>${Number.isFinite(row.meter)?`<span class="acct-bar" aria-hidden="true"><i data-meter="${row.meter}"></i></span>`:''}</button>`).join(''));
    for(const bar of el('desk-account-summary').querySelectorAll('[data-meter]'))bar.style.setProperty('--w',bar.dataset.meter+'%');
  }
  function renderAccounts(){
    for(const provider of ['claude_cli','deepseek']){
      const account=workspace.accounts?.[provider]||{},node=el(provider==='claude_cli'?'claude-account-card':'deepseek-account-card');
      const windows=(account.windows||[]).filter(w=>Number.isFinite(w.remaining_percent));
      const balance=account.balance_infos||account.balance||[];
      const balances=Array.isArray(balance)?balance:[];
      const hasValues=windows.length>0||balances.length>0;
      const values=windows.length?windows.slice(0,2).map(w=>`<div class="account-window-line"><span>${text(w.label||'使用窗口')}</span><strong>剩余 ${Math.round(w.remaining_percent)}%</strong></div><progress class="account-meter" value="${w.remaining_percent}" max="100" aria-label="${text(w.label||'额度')}剩余百分比"></progress><p class="account-meta">${w.resets_at?text(new Date(w.resets_at).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}))+' 重置':''}</p>`).join(''):balances.map(b=>`<p class="account-value">${text(b.currency==='CNY'?'¥ ':b.currency+' ')}${text(b.total_balance??b.balance??'未知')}</p><p class="account-meta">赠送 ${text(b.granted_balance??'—')} · 充值 ${text(b.topped_up_balance??'—')}</p>`).join('');
      setMarkup(node,`<div class="account-heading"><strong>${provider==='claude_cli'?'Claude 额度':'DeepSeek API 余额'}</strong><button class="text-button" data-desk-account-refresh="${provider}">刷新</button></div>${values}${accountNotice(account,hasValues)}${accountProvenance(provider,account)}`);
    }
    // Preserve the desktop bridge's initial snapshot, then refresh through the native CLI.
    const native=workspace.accounts?.codex_cli;
    let node=el('desk-native-codex-windows');
    if(native?.updated_at){
      if(!node){node=document.createElement('div');node.id='desk-native-codex-windows';el('usage-bar').append(node);}
      const windows=(native.windows||[]).filter(w=>Number.isFinite(w.remaining_percent));
      setMarkup(node,windows.slice(0,2).map(w=>`<div class="account-window-line"><span>${text(w.label||'额度')}</span><strong>剩余 ${Math.round(w.remaining_percent)}%</strong></div><progress class="account-meter" value="${w.remaining_percent}" max="100" aria-label="Codex ${text(w.label||'额度')}剩余百分比"></progress>`).join('')+accountNotice(native,windows.length>0)+accountProvenance('codex_cli',native));
      el('usage-windows').hidden=true;
    }
    // The DSH login wallet stays a separate card; in the DSH host it is the top bar itself.
    const host=el('host-account-card');
    if(host){host.hidden=isDshHost||!state.host_account;if(!host.hidden&&typeof hostAccountPresentation==='function'){const shown=hostAccountPresentation(state.host_account);setMarkup(host,`<div class="account-heading"><strong>${text(shown.title)}</strong><span class="subtle">${text(shown.statusText)}</span></div>${shown.rows.map(row=>`<div class="account-window-line"><span>${text(row.label)}</span><strong>${text(row.value)}</strong></div>`).join('')||`<p class="account-unknown">${text(shown.message||'DSH 账户余额未知')}</p>`}${shown.error?`<p class="account-notice error"><strong>读取失败</strong> · ${text(shown.error)}</p>`:''}<p class="account-meta account-provenance">${text(shown.sourceText)} · ${text(shown.refreshNote)}</p>`);}}
  }

  // ---------- overview and models tab ----------
  function renderOverviewModels(project){
    const node=el('overview-models');if(!node)return;
    if(!project){setMarkup(node,boundPath()?`<div class="ov-head"><div><p class="ov-kicker">项目模型</p><h2>此项目尚未加入侧栏</h2></div></div><p class="ov-empty">加入侧栏后才能按项目选择决策与执行模型。</p><div class="ov-actions"><button class="button secondary" data-desk-adopt-project>${icon('i-plus')}加入侧栏</button></div>`:'');return;}
    const total=project.bindings.length;
    setMarkup(node,`<div class="ov-head"><div><p class="ov-kicker">项目模型</p><h2>${total} 个模型配置</h2><p>“已就绪”只表示可用，不代表已启动。</p></div><button class="text-button" data-desk-tab-jump="models">管理</button></div>${Object.entries(roles).map(([role,label])=>{const rows=project.bindings.filter(b=>b.role===role);return `<div class="ov-role"><div class="ov-role-head"><span>${icon(roleIcons[role])}${label}</span><button class="text-button" data-desk-add-role="${role}" data-project="${text(project.path)}">添加</button></div>${rows.map(b=>`<button class="ov-model" data-desk-select-binding="${b.id}" data-project="${text(project.path)}"><span class="sdot s-${statusKind(b)}" aria-hidden="true"></span><span class="ov-model-main"><strong>${text(modelLabel(b))}${b.is_default?'<span class="desk-default-flag">默认</span>':''}</strong><small>${text(modelOf(b)?.model||'')}${isCli(modelOf(b))?' · effort '+text(b.effort||'medium'):''}</small></span>${statusMarkup(b)}</button>`).join('')||`<p class="ov-empty">尚未添加${label}。</p>`}</div>`;}).join('')}`);
  }
  function roleRow(project,b){
    return `<div class="desk-role-row ${selectedBinding===b.id?'selected':''}" data-binding-row="${b.id}" data-project="${text(project.path)}"><button data-desk-select-binding="${b.id}" data-project="${text(project.path)}" ${selectedBinding===b.id?'aria-current="true"':''}><span class="sdot s-${statusKind(b)}" aria-hidden="true"></span><span class="role-row-text"><strong>${text(modelLabel(b))}${b.is_default?'<span class="desk-default-flag">默认</span>':''}</strong><small>${text(modelOf(b)?.model||'')} · ${isCli(modelOf(b))?text(b.effort||'medium')+' effort':'连接配置参数'}</small></span>${statusMarkup(b)}</button><button class="tree-menu-button" data-desk-binding-menu="${b.id}" data-project="${text(project.path)}" aria-label="${text(modelLabel(b))}模型操作" aria-haspopup="menu">${icon('i-more')}</button></div>`;
  }
  function renderModelsList(project){
    return Object.entries(roles).map(([role,label])=>{const rows=project.bindings.filter(b=>b.role===role);return `<section class="desk-role-panel" aria-label="${label}"><div class="role-panel-heading"><h3>${icon(roleIcons[role])}${label}<span class="count">${rows.length}</span></h3><button class="button secondary" data-desk-add-role="${role}" data-project="${text(project.path)}">${icon('i-plus')}添加</button></div><p class="desk-role-description">${descriptions[role]}</p>${rows.map(b=>roleRow(project,b)).join('')||`<div class="role-empty">为此项目选择${label}。${role==='decision'?'Claude 或 Codex CLI 可担任主脑，负责拆分与验收。':'可以选择 CLI、API 或本地模型执行任务。'}</div>`}</section>`;}).join('');
  }
  function inspectorEmpty(){
    return `<div class="inspector-empty"><h3>选择一个模型查看设置</h3><p>左侧列表与项目树中的模型都可以打开；右键或 ⋯ 菜单提供启用、消息流和移除等操作。</p><dl><dt><span class="sdot s-ready"></span>已就绪</dt><dd>配置已启用且执行端可用，尚未代表任何运行。</dd><dt><span class="sdot s-running"></span>运行中 / 已启动</dt><dd>来自真实任务或主脑轮次回执。</dd><dt><span class="sdot s-unknown"></span>待核对</dt><dd>回执未确认，不等于失败或已停止。</dd><dt><span class="sdot s-disabled"></span>已停用</dt><dd>只阻止新派发，不会停止在途任务。</dd></dl></div>`;
  }
  function ensureModelsLayout(){
    if(el('desk-models-list'))return;
    setMarkup(el('desk-project-content'),'<div class="models-layout"><div id="desk-models-list" class="models-list"></div><div id="desk-models-detail" class="models-detail"></div></div>');
    lastContentKey='';
  }
  function renderProjectContent(force=false){
    const project=currentProject(),binding=currentBinding();
    const node=el('desk-project-content');
    if(!project){lastContentKey='';setMarkup(node,boundPath()?'<div class="inspector-empty"><h3>此项目尚未加入侧栏</h3><p>项目任务仍可查看；加入侧栏后才能为它选择决策和执行模型。</p><div class="ov-actions"><button class="button primary" data-desk-adopt-project>加入侧栏</button></div></div>':'');return;}
    ensureModelsLayout();
    setMarkup(el('desk-models-list'),renderModelsList(project));
    const key=JSON.stringify({project:project.path,binding,models:state.models.map(m=>[m.id,m.name,m.enabled]),catalog:[...catalogs.entries()].map(([k,v])=>[k,v.updated_at,v.models?.length])});
    if(!force&&(inspectorDirty||key===lastContentKey))return;
    lastContentKey=key;
    setMarkup(el('desk-models-detail'),binding?renderInspector(project,binding):inspectorEmpty());
  }
  function renderLeaseWaits(project){
    const rows=project?.waiting_tasks||[];
    return rows.length?`<h3>等待资源 · ${rows.length}</h3><ul>${rows.map(row=>`<li>${text(row.title||row.task_id)}：${text(row.reason||'等待执行资源')}</li>`).join('')}</ul>`:'<p class="field-hint">当前没有等待资源的任务。</p>';
  }
  function renderSettings(){
    const project=currentProject(),node=el('desk-project-settings');if(!node)return;
    if(!project){lastSettingsKey='';setMarkup(node,'<div class="settings-card-heading"><h2>本项目执行设置</h2></div><p class="field-hint">项目尚未加入侧栏，暂无项目级设置。</p>');return;}
    const waiting=el('desk-lease-waiting');
    const key=project.path+':'+(project.work_concurrency||1);
    if(waiting&&(settingsDirty||key===lastSettingsKey)){setMarkup(waiting,renderLeaseWaits(project));return;}
    lastSettingsKey=key;settingsDirty=false;
    setMarkup(node,`<div class="settings-card-heading"><h2>本项目执行设置 <span class="scope-tag">仅此项目</span></h2></div><form id="desk-concurrency-form" class="inspector-settings" data-project="${text(project.path)}"><label>工作并发<input name="work_concurrency" type="number" min="1" max="4" step="1" value="${project.work_concurrency||1}" required aria-describedby="desk-concurrency-hint"></label><p id="desk-concurrency-hint" class="field-hint">默认 1，最多 4。同账户最多 1 个任务；写入文件冲突时仍会等待。不同项目、不同账户可同时执行。保存后用于尚未申请资源的任务，不中止在途执行。</p><p id="desk-concurrency-error" class="form-error" role="alert"></p><div class="inspector-save-row"><span id="desk-concurrency-state">设置只影响此项目。</span><button type="submit" class="button primary">保存并发设置</button></div></form><section id="desk-lease-waiting" class="lease-waiting" aria-label="等待资源的任务">${renderLeaseWaits(project)}</section>`);
  }
  function renderEnforcement(binding){
    const report=binding.enforcement;
    if(!report||report.basis!=='adapter_declaration')return '<p class="desk-inline-note unknown">权限约束：未支持 · 尚无适配器声明。</p>';
    return `<details class="inspector-settings"><summary>权限约束 · ${text(report.label||'未支持')}</summary><p class="field-hint">适配器声明，未核验本次运行。启用或就绪不代表隔离已经生效。</p>${report.controls?.length?`<ul>${report.controls.map(value=>`<li>${text(value)}</li>`).join('')}</ul>`:''}<h4>覆盖范围与限制</h4><ul>${(report.limitations||[]).map(value=>`<li>${text(value)}</li>`).join('')}</ul></details>`;
  }
  function catalogLine(model){
    const value=catalogs.get(model.provider);
    if(!value)return '尚未读取原生目录；effort 选项为常见值，以 CLI 实际支持为准。';
    const cap=nativeCapabilities(model);
    return `${value.live?'来自本机 CLI 原生列表':'缓存模型列表'}${value.updated_at?' · '+clock(value.updated_at):''}${cap?'':' · 当前模型不在目录中，按未确认处理'}`;
  }
  function renderInspector(project,binding){
    const model=modelOf(binding),native=isCli(model),cap=native?nativeCapabilities(model):null;
    const efforts=cap?.effort_levels?.length?[...cap.effort_levels]:(model?.provider==='claude_cli'?['low','medium','high','xhigh','max']:['low','medium','high','xhigh']);
    const chosen=binding.effort||'medium';if(!efforts.includes(chosen))efforts.unshift(chosen);
    const window=binding.context_window||200000,compact=binding.autocompact_tokens||Math.floor(window*.8);
    const kind=statusKind(binding);
    const activityNote=binding.role==='decision'?(binding.running_count||binding.starting_count||binding.unknown_count?`主脑轮次：${statusName(binding)}。停用只影响之后的选择，不会停止已在进行的轮次。`:'这个配置用于记录项目的决策与验收模型。只有持久轮次取得启动回执后才显示“已启动”。'):binding.running_count?'当前有 '+binding.running_count+' 个任务运行。停用仅阻止新任务，运行中的任务继续。':'CLI 和 API 模型按任务运行。“已就绪”表示配置已启用且执行端可用；有真实任务时才显示“运行中”。';
    const fastKnown=cap&&typeof cap.fast_supported==='boolean';
    const fastLocked=model?.provider==='codex_cli'&&!binding.fast&&(cap?.fast_supported===false||!fastKnown);
    const fastNote=cap?.fast_supported===false?'此模型没有返回 fast 支持，保持关闭。':!fastKnown?'尚未确认此模型的 fast 能力：先读取原生模型与能力，确认支持后才能开启。':'使用原生快速服务层；可用性与消耗按当前账户和模型计算。';
    const nativeWindow=cap?.context_window?`原生窗口 ${tokens(cap.context_window)} tokens（目录读取值）`:'原生窗口未从目录取得';
    return `<div class="desk-inspector">${binding.blocked_reason&&!binding.ready?`<p class="desk-inline-note warn">${text(binding.blocked_reason)}${binding.runtime_ready&&!binding.endpoint_enabled?` <button class="text-button" data-desk-enable-endpoint="${text(isCli(model)?model.provider:effectiveModelAgent(model))}">启用此执行端</button>`:''}</p>`:''}<div class="inspector-heading"><div><p class="ov-kicker">${roles[binding.role]} · ${text(names[model?.provider]||engineNames[effectiveModelAgent(model)]||model?.provider||'连接未知')}</p><h2>${text(modelLabel(binding))}</h2><p class="desk-model-code">${text(model?.model||binding.model_id)}</p></div>${statusMarkup(binding)}</div><div class="inspector-toolbar"><button class="button ${binding.enabled?'secondary':'primary'}" data-desk-toggle-binding="${binding.id}">${binding.enabled?'停用此模型':'启用此模型'}</button>${binding.role==='execution'?`<button class="button primary" data-desk-new-task="${binding.id}" ${!binding.enabled||!binding.ready?'disabled title="模型未启用或执行端未就绪"':''}>${icon('i-send')}分派任务</button>`:''}<button class="button secondary" data-desk-binding-flow="${binding.id}" data-project="${text(project.path)}">${icon('i-flow')}查看消息流</button><button class="button secondary" data-desk-edit-connection="${text(binding.model_id)}">编辑连接</button><button class="icon-button" data-desk-binding-menu="${binding.id}" data-project="${text(project.path)}" aria-haspopup="menu" aria-label="更多操作">${icon('i-more')}</button></div><p class="desk-inline-note ${kind==='unknown'?'unknown':''}">${activityNote}</p>${renderEnforcement(binding)}<form id="desk-binding-form" class="inspector-settings" data-binding-id="${binding.id}"><h3>此项目的运行设置</h3><p class="settings-section-note">保存后用于新任务和新会话；已在运行的会话保留启动时的快照，实际生效值见消息流检查器。</p>${native?`<div class="inspector-grid"><label>Effort · 思考强度<select name="effort">${efforts.map(v=>`<option value="${v}" ${v===chosen?'selected':''}>${text({low:'低 · low',medium:'中 · medium',high:'高 · high',xhigh:'超高 · xhigh',max:'最高 · max',default:'沿用原生默认',none:'关闭 · none',minimal:'最少 · minimal',ultra:'极高 · ultra'}[v]||v)}</option>`).join('')}</select><p class="field-hint">${cap?'选项来自本机 CLI 的此模型能力。':'默认使用 medium；读取原生模型能力后按模型选择支持的等级。'}</p></label><div><span class="field-label">模型能力</span><button type="button" class="button secondary" data-desk-inspect-catalog="${model.provider}">${icon('i-refresh')}读取原生模型与能力</button><p class="field-hint">${text(catalogLine(model))}。只读模型元数据，不发送生成请求。</p></div></div>${model.provider==='codex_cli'?`<label class="inspector-check ${fastLocked?'is-disabled':''}"><input name="fast" type="checkbox" ${binding.fast?'checked':''} ${fastLocked?'disabled':''}>开启 Codex fast</label><p class="field-hint">${fastNote} 默认关闭。</p>`:''}<div class="inspector-grid"><label>工作台上下文预算 / tokens<input name="context_window" type="number" min="100000" max="2000000" step="1000" value="${window}" required><p class="field-hint">仅为工作台预算，实际模型窗口以服务商为准；${text(nativeWindow)}。</p></label><label>${model.provider==='claude_cli'?'自动压缩窗口':'自动压缩触发阈值'} / tokens<input name="autocompact_tokens" type="number" min="${model.provider==='claude_cli'?100000:1000}" max="${model.provider==='claude_cli'?1000000:2000000}" step="1000" value="${compact}" required><p class="field-hint">${model.provider==='claude_cli'?'Claude 窗口须为 100k–1M；实际触发会保留原生摘要缓冲。':'达到此 token 数时，交给 Codex 原生压缩。'}默认取预算的 80%。</p></label></div><button type="button" class="text-button" data-desk-reset-compact>设为预算的 80%</button><p class="effective-line"><span>已保存：</span><b>effort ${text(binding.effort||'medium')}</b>${model.provider==='codex_cli'?`<b>fast ${binding.fast?'开':'关'}</b>`:''}<b>预算 ${tokens(binding.context_window)}</b><b>压缩 ${tokens(binding.autocompact_tokens)}</b></p>`:`<p class="field-hint">API / 本地模型的 token 与思考等级使用现有连接配置；在“编辑连接”中设置。CLI 专属 effort、fast 和自动压缩不应用到 API 请求。</p>`}<label class="inspector-check"><input name="is_default" type="checkbox" ${binding.is_default?'checked':''}>设为本项目的默认${roles[binding.role]}</label><p id="desk-binding-error" class="form-error" role="alert"></p><div class="inspector-save-row"><span id="desk-binding-save-state">${binding.role==='decision'?'设置只作用于新轮次；进行中的轮次保留启动时的快照。':'设置只作用于新任务；已有任务保留派发时的快照。'}</span><button type="button" class="button secondary" data-desk-revert>还原</button><button type="submit" class="button primary">保存设置</button></div></form></div>`;
  }
  function renderTabBadges(project){
    const rows=project?.bindings||[];
    const unknown=rows.reduce((sum,b)=>sum+(b.unknown_count||(b.status==='unknown'?1:0)),0);
    const badge=el('tab-models-badge');badge.hidden=!unknown;badge.textContent=unknown;badge.className='tab-badge s-unknown';badge.title=unknown?`${unknown} 个模型状态待核对`:'';
  }
  function renderAll(){
    const project=currentProject();
    renderHeader(project);renderWelcome();renderTree();renderAccounts();renderAccountSummary();renderProjectContent();renderSettings();renderOverviewModels(project);renderTabBadges(project);
    const rows=(project?.bindings||[]).filter(b=>b.role==='execution');
    el('stat-models').textContent=project?rows.filter(b=>b.enabled).length:'—';el('model-caption').textContent=project?`/ ${rows.length} 个项目执行模型`:'项目未加入侧栏';
    if(el('desk-library-dialog').open)renderLibrary();if(el('desk-engines-dialog').open)renderEngines();
  }
  async function selectProject(path,bindingId=null){
    if(inspectorDirty&&!confirm('有未保存的模型设置，放弃这些修改并切换？'))return;
    inspectorDirty=false;selectedBinding=bindingId;lastContentKey='';
    if(bindingId)setTab('models');
    await api('/api/workspace/projects',{action:'select',path});
    await switchToProject(path);await loadWorkspace(true);renderProjectContent(true);
    [...el('desk-project-tree').querySelectorAll('.project-folder')].find(n=>n.dataset.folder==='project:'+path)?.setAttribute('open','');
    closeSidebar();
  }
  function openProjectDialog(action='add',project=null){
    projectAction=action;el('desk-project-form').reset();el('desk-project-error').textContent='';
    el('desk-project-dialog-title').textContent=action==='rename'?'重命名项目':'添加项目文件夹';
    el('desk-project-form').querySelector('[type=submit]').textContent=action==='rename'?'保存名称':'添加项目';
    el('desk-project-path').value=project?.path||'';el('desk-project-path').readOnly=action==='rename';
    el('desk-project-name').value=project?.name||'';el('desk-folder-pick').hidden=action==='rename';
    el('desk-project-dialog').showModal();
  }
  function openLibrary(role=null,project=null){
    pendingAssignment=role?{role,project:project||projectView.project}:null;
    libraryRole=role||'execution';catalog=null;el('desk-catalog-list').innerHTML='';el('desk-catalog-source').textContent='';el('desk-library-error').textContent='';renderLibrary();el('desk-library-dialog').showModal();
  }
  function renderLibrary(){
    const project=currentProject();
    el('desk-library-title').textContent=pendingAssignment?'为项目添加'+roles[pendingAssignment.role]:'连接与模型库';
    el('desk-library-hint').textContent=pendingAssignment?'选择已有连接，或从本机 CLI 读取并导入模型。连接凭据共享，项目角色和运行参数独立。':'连接库保存账户与接口。项目侧栏只显示加入该项目的模型；同一连接可以用于多个项目和角色。';
    setMarkup(el('desk-library-list'),state.models.map(model=>`<div class="desk-library-row"><div><strong>${text(model.name)}</strong><p>${text(names[model.provider]||model.provider)} · ${text(model.model)}${model.enabled?'':' · 已停用'}</p></div><button class="button secondary" data-desk-edit-connection="${text(model.id)}">配置</button>${pendingAssignment?`<button class="button primary" data-desk-assign-model="${text(model.id)}">加入${roles[pendingAssignment.role]}</button>`:project?`<button class="button secondary" data-desk-model-project="${text(model.id)}">加入项目…</button>`:''}</div>`).join('')||'<p class="role-empty">还没有保存的连接。读取 Claude / Codex 原生模型，或添加 API 与本地服务。</p>');
  }
  async function readCatalog(provider,button,inspector=false){
    const label=button.innerHTML;button.disabled=true;button.textContent='读取中…';
    const errorNode=el(inspector?'desk-binding-error':'desk-library-error');if(errorNode)errorNode.textContent='';
    try{
      const result=await api('/api/provider-catalog',{provider});catalogs.set(provider,result);
      if(result.error||(!result.models?.length&&result.message))throw new Error(result.error||result.message);
      if(inspector){
        const wasDirty=inspectorDirty;
        const draft=[...el('desk-binding-form').elements].filter(field=>field.name).map(field=>({name:field.name,value:field.value,checked:field.checked,type:field.type}));
        inspectorDirty=false;lastContentKey='';renderProjectContent(true);
        for(const saved of draft){const field=el('desk-binding-form').elements[saved.name];if(!field)continue;if(saved.type==='checkbox')field.checked=saved.checked;else{if(field.tagName==='SELECT'&&![...field.options].some(option=>option.value===saved.value))field.add(new Option(saved.value+'（请核对支持情况）',saved.value));field.value=saved.value;}}
        inspectorDirty=wasDirty;if(wasDirty)markInspectorDirty();return;
      }
      catalog={...result,provider};
      el('desk-catalog-source').textContent=`${result.live?'来自本机 CLI 原生列表':'缓存模型列表'} · ${names[provider]} · ${result.models?.length||0} 个模型${result.updated_at?' · '+clock(result.updated_at):''}`;
      el('desk-catalog-list').innerHTML=(result.models||[]).map((model,index)=>{const exists=state.models.find(m=>m.provider===provider&&m.model===model.id);return `<div class="desk-library-row"><div><strong>${text(model.name||model.id)}${model.is_default?' · 原生默认':''}</strong><p>${text(model.id)}${model.effort_levels?.length?' · effort '+text(model.effort_levels.join(' / ')):''}${model.context_window?' · 窗口 '+tokens(model.context_window):''}</p></div><button class="button ${exists?'secondary':'primary'}" data-desk-import-model="${index}">${exists?(pendingAssignment?'加入项目':'已保存'):'导入'+(pendingAssignment?'并加入':'')}</button></div>`;}).join('');
    }catch(error){if(errorNode)errorNode.textContent=error.message;}finally{if(button.isConnected){button.disabled=false;button.innerHTML=label;}}
  }
  async function assignModel(modelId,target=pendingAssignment){
    if(!target?.project)throw new Error('请先选择项目和模型角色');
    const existing=workspace.projects.find(p=>sameProjectPath(p.path,target.project))?.bindings.find(b=>b.model_id===modelId&&b.role===target.role);
    if(existing){el('desk-library-dialog').close();await selectProject(target.project,existing.id);return;}
    const model=state.models.find(m=>m.id===modelId);
    await api('/api/workspace/bindings',{action:'save',project:target.project,model_id:modelId,role:target.role,enabled:true,effort:'medium',fast:false,context_window:isCli(model)?200000:(model?.max_input_tokens||32768),...(isCli(model)?{autocompact_tokens:160000}:{})});
    el('desk-library-dialog').close();await loadWorkspace(true);await selectProject(target.project);toast('模型已加入'+roles[target.role]);
  }

  // ---------- execution endpoints and CLI versions ----------
  // The panel only explains the update gate; the server re-checks every project.
  function updateGate(){
    const activity=state.execution_activity;
    if(!activity||activity.complete===false||workspace.readable===false)return {blocked:true,reason:'执行状态暂时不可读，暂不允许更新 CLI。'};
    if((activity.total||0)>0)return {blocked:true,reason:`仍有 ${activity.total} 个执行任务在运行或排队，完成后才能更新 CLI。`};
    const brains=workspace.projects.flatMap(p=>(p.bindings||[]).filter(b=>b.role==='decision'&&(b.running_count||b.starting_count||b.unknown_count)).map(()=>p.name));
    if(brains.length)return {blocked:true,reason:`主脑轮次仍在进行或待核对（${[...new Set(brains)].join('、')}），暂不允许更新 CLI。`};
    return {blocked:false,reason:'当前未发现执行任务或主脑轮次。更新前工作台仍会再次核对所有项目；请确认没有其他终端在使用同一 CLI。'};
  }
  function renderEngines(){
    const gate=updateGate(),rows=state.execution_endpoints?.rows||[];
    const gateNode=el('desk-engines-gate');gateNode.textContent=rows.some(row=>row.runtime?.backend_sources||row.runtime?.software_update?.can_update)?gate.reason.replaceAll('更新 CLI','更新 CLI 或切换来源'):'';gateNode.classList.toggle('blocked',gate.blocked);
    setMarkup(el('desk-engines-list'),rows.map(row=>{
      const ready=!!row.runtime?.ready;
      const availability=row.enabled?(ready?'已启用 · 可用':'已启用 · 不可用'):'已停用';
      const kind=row.enabled?(ready?'ready':'unavailable'):'disabled';
      const launchPolicy=row.runtime?.ui_policy==='background-only'?'后台执行 · 不唤起桌面窗口':'';
      const update=row.runtime?.software_update;
      const backends=row.runtime?.backend_sources;
      const selectedBackend=backends?.options.find(option=>option.id===backends.selected);
      const backendPicker=backends?`<form class="engine-source-form" data-desk-source-form="${text(row.id)}"><label>执行端来源<select name="cli_source" aria-label="${text(row.name)}执行端来源" ${gate.blocked||sourceSaving.has(row.id)?'disabled':''}>${backends.options.map(option=>`<option value="${option.id}" ${option.id===(sourceDrafts.get(row.id)||backends.selected)?'selected':''} ${!option.available?'disabled':''}>${text(option.label)}${option.available?'':' · 未安装'}</option>`).join('')}</select></label><button type="submit" class="button secondary" ${gate.blocked||sourceSaving.has(row.id)?'disabled':''}>${sourceSaving.has(row.id)?'保存中…':'保存来源'}</button></form><p class="engine-source-path" title="${text(selectedBackend?.executable||'')}">当前使用：${text(selectedBackend?.executable||'所选来源不可用，请检查安装')}</p><p class="field-hint">保存后用于新任务与新轮次；不会更改账户、模型和费用授权。</p>`:'';
      const updateState=!update?'':update.version_status==='unavailable'?`官方版本暂不可用${update.latest?` · 上次 ${text(update.latest)}（已过期）`:''}`:update.stale?`上次官方包 ${text(update.latest||'未知')} · 已过期，请重新检查`:!update.checked?'尚未检查官方版本':update.update_available===true?`<span class="update-flag">可更新到 ${text(update.latest||'未知')}</span>`:update.update_available===false?`已是最新版 ${text(update.latest)}`:`官方包 ${text(update.latest||'未知')} · 宿主管理`;
      const source=update?({'host-app':'宿主应用包（签名状态见下方）',standalone:'独立 CLI（不依赖桌面应用）',native:'本机 CLI（安装归属需核对）'}[update.source]||'安装来源未确认'):'';
      const facts=[update?.installed?`<li>已安装 <b>${text(update.installed)}</b></li>`:row.runtime?.version?`<li>版本 <b>${text(row.runtime.version)}</b></li>`:'<li>版本未确认</li>',update?`<li>${updateState}${update.checked_at?` · 核对于 ${text(update.checked_at)}`:''}</li>`:'',source?`<li>${text(source)}</li>`:'',launchPolicy?`<li>${launchPolicy}</li>`:'',row.runtime?.access_mode?`<li>${row.runtime.access_mode==='full'?'完全访问':'项目范围'}</li>`:''].join('');
      const note=ready?'':text(row.message||row.runtime?.message||'运行时尚未就绪');
      // Same current-model, account and readiness facts as the endpoint settings (app.js).
      const shared=typeof endpointFacts==='function'?endpointFacts(row):null;
      const current=shared?`<div class="engine-current"><div class="engine-model"><span class="label">${shared.native?'指定原生模型':'当前模型 · 自动路由'}</span><strong>${text(shared.name)}</strong><span class="model-code">${text(shared.modelCode)}</span></div>${shared.wallet}<div class="engine-status">${row.selection_error||(row.enabled&&row.ready&&!(row.models||[]).some(m=>m.enabled))?shared.status:''}<span>${text(shared.note)}</span></div></div>`:'';
      const updateButton=update?.can_update?`<button class="button secondary" data-desk-cli-update="${row.id}" ${gate.blocked?`disabled title="${text(gate.reason)}"`:''}>更新 CLI</button>`:'';
      return `<div class="engine-row" data-engine-id="${text(row.id)}"><span class="engine-mark" aria-hidden="true">${({pi:'Pi',dsh:'DSH',workbuddy:'WB',claude_cli:'Cl',codex_cli:'Cx'})[row.id]||'API'}</span><div class="engine-main"><div class="engine-title"><strong>${text(row.name)}</strong><span class="stag s-${kind}"><span class="sdot s-${kind}"></span>${availability}</span></div><ul class="engine-facts">${facts}</ul>${backendPicker}${current}${note?`<p class="engine-note">${note}</p>`:''}${update?.note?`<p class="engine-note">${text(update.note)}</p>`:''}</div><div class="engine-actions"><button type="button" class="switch-button" role="switch" aria-checked="${row.enabled}" data-desk-engine-toggle="${row.id}" aria-label="允许新任务使用${text(row.name)}"><span class="switch-track" aria-hidden="true"></span>${row.enabled?'已启用':'已停用'}</button><div class="engine-buttons"><button class="button secondary" data-desk-engine-config="${row.id}">参数设置</button>${update?`<button class="button secondary" data-desk-cli-check="${row.id}">检查更新</button>${updateButton}`:''}</div></div></div>`;
    }).join('')||'<p class="role-empty">尚未读取执行端。</p>');
  }

  // ---------- context menus: right-click and ⋯ share one action list ----------
  // Clamp in viewport pixels, then convert back: the font-scale setting zooms the
  // document, so rects/clientX are zoomed while style offsets are not.
  function placeMenu(node,anchor,event){
    node.style.left='0px';node.style.top='0px';
    const box=anchor.getBoundingClientRect(),own=node.getBoundingClientRect(),ratio=own.width&&node.offsetWidth?own.width/node.offsetWidth:1;
    const left=Math.max(8,Math.min(event?.clientX??box.right-own.width,window.innerWidth-own.width-8));
    const top=Math.max(8,Math.min(event?.clientY??box.bottom+4,window.innerHeight-own.height-8));
    node.style.left=left/ratio+'px';node.style.top=top/ratio+'px';
  }
  function closeMenu(){const node=el('desk-context-menu');if(node.hidden)return;node.hidden=true;menuContext=null;menuReturnFocus?.focus?.({preventScroll:true});menuReturnFocus=null;}
  function openMenu(context,anchor,event=null){
    closeMenu();menuContext=context;menuReturnFocus=anchor;
    const project=workspace.projects.find(p=>p.path===context.project),binding=project?.bindings.find(b=>b.id===context.bindingId);
    const item=(id,label,{disabled='',destructive=false}={})=>`<button role="menuitem" data-desk-menu-action="${id}" ${destructive?'data-destructive':''} ${disabled?`disabled title="${text(disabled)}"`:''}>${label}</button>`;
    let markup;
    if(binding){
      const other=binding.role==='decision'?'execution':'decision';
      markup=`<p class="menu-label">${text(modelLabel(binding))} · ${roles[binding.role]}</p>${item('settings','运行设置')}${item('flow','查看消息流与控制…')}${binding.role==='execution'?item('task','用此模型分派任务…',{disabled:!binding.enabled||!binding.ready?'模型未启用或执行端未就绪':''}):''}<hr>${item(binding.enabled?'disable':'enable',binding.enabled?'停用此模型':'启用此模型')}${item('default','设为此角色默认',{disabled:binding.is_default?'已是此角色默认':''})}${item('copy-role','复制到'+roles[other])}${item('edit','编辑连接')}<hr>${item('remove-binding','移除项目关联',{destructive:true})}`;
    }else markup=`<p class="menu-label">${text(project?.name||'项目')}</p>${item('open','打开项目')}${item('add-decision','添加决策模型')}${item('add-execution','添加执行模型')}<hr>${item('rename','重命名项目')}${item('remove-project','从侧栏移除',{destructive:true})}`;
    const node=el('desk-context-menu');node.innerHTML=markup;
    node.hidden=false;placeMenu(node,anchor,event);node.querySelector('button:not(:disabled)')?.focus();
  }
  async function menuAction(action){
    const context=menuContext;if(!context)return;const project=workspace.projects.find(p=>p.path===context.project),binding=project?.bindings.find(b=>b.id===context.bindingId);closeMenu();
    if(action==='open')return selectProject(context.project);
    if(action==='rename')return openProjectDialog('rename',project);
    if(action.startsWith('add-'))return openLibrary(action.slice(4),context.project);
    if(action==='flow')return openBindingFlow(context.project,binding);
    if(action==='settings')return selectProject(context.project,binding.id);
    if(action==='task'){await selectProject(context.project,binding.id);startTask(binding);return;}
    if(action==='edit'){openModel(binding.model_id);return;}
    if(action==='remove-project'){
      if(!confirm(`从侧栏移除「${project.name}」？文件夹、文件和任务记录会保留。`))return;
      await api('/api/workspace/projects',{action:'remove',path:context.project});
      if(sameProjectPath(context.project,projectView.project))changeProjectView({mode:'unbound',project:null});
      selectedBinding=null;inspectorDirty=false;
    }else if(binding){
      if(action==='remove-binding'&&!confirm('移除此项目中的模型关联？全局连接和已有任务会保留。'))return;
      const payload={project:context.project,binding_id:binding.id,model_id:binding.model_id,role:binding.role};
      if(action==='copy-role'){const role=binding.role==='decision'?'execution':'decision';const existing=project.bindings.find(b=>b.model_id===binding.model_id&&b.role===role);if(existing)return selectProject(context.project,existing.id);const {id,...values}=binding;await api('/api/workspace/bindings',{...values,action:'save',project:context.project,role,is_default:false});}
      else await api('/api/workspace/bindings',{...payload,action:action==='default'?'save':action==='remove-binding'?'remove':action,...(action==='default'?{is_default:true}:{})});
      inspectorDirty=false;
      if(action==='remove-binding')selectedBinding=null;
    }
    await loadWorkspace(true);
  }
  async function openBindingFlow(project,binding){
    if(!binding)return;
    if(!sameProjectPath(projectView.project,project))await selectProject(project,binding.id);
    window.modelDeskFlow.open({modelId:binding.model_id,role:binding.role});
  }
  function startTask(binding){openTask();if(el('task-dialog').open){el('task-route').value='manual';el('task-model').value=binding.model_id;routeFields();}}
  async function refreshAccount(provider,button){
    button.disabled=true;button.textContent='读取中…';
    try{await api('/api/accounts/refresh',{provider});await loadWorkspace(true);}
    catch(error){toast(error.message,true);}finally{button.disabled=false;button.textContent='刷新';}
  }
  function openAccounts(){renderAccounts();if(!el('desk-accounts-dialog').open)el('desk-accounts-dialog').showModal();}
  function markInspectorDirty(){inspectorDirty=true;const node=el('desk-binding-save-state');if(node){node.textContent='有未保存的修改';node.classList.add('dirty');}}
  function toggleAppearance(force){const pop=el('desk-appearance'),open=force??pop.hidden;pop.hidden=!open;el('desk-appearance-open').setAttribute('aria-expanded',String(open));if(open)el('theme-mode').focus();}
  document.addEventListener('click',async event=>{
    const button=event.target.closest('button');
    if(!el('desk-appearance').hidden&&!event.target.closest('#desk-appearance,#desk-appearance-open'))toggleAppearance(false);
    if(!button)return;
    try{
      if(button.hasAttribute('data-desk-close')){button.closest('dialog').close();return;}
      if(button.hasAttribute('data-desk-retry'))return loadWorkspace(true);
      if(button.hasAttribute('data-desk-add-project'))return openProjectDialog();
      if(button.hasAttribute('data-desk-adopt-project')){openProjectDialog();el('desk-project-path').value=boundPath()||'';return;}
      if(button.hasAttribute('data-desk-select-project')){event.preventDefault();return await selectProject(button.dataset.deskSelectProject);}
      if(button.hasAttribute('data-desk-select-binding'))return await selectProject(button.dataset.project,button.dataset.deskSelectBinding);
      if(button.hasAttribute('data-desk-add-role')){event.preventDefault();return openLibrary(button.dataset.deskAddRole,button.dataset.project);}
      if(button.hasAttribute('data-desk-project-menu')){event.preventDefault();openMenu({project:button.dataset.deskProjectMenu},button);return;}
      if(button.hasAttribute('data-desk-binding-menu')){event.preventDefault();openMenu({project:button.dataset.project,bindingId:button.dataset.deskBindingMenu},button);return;}
      if(button.hasAttribute('data-desk-menu-action'))return await menuAction(button.dataset.deskMenuAction);
      if(button.hasAttribute('data-desk-open-library'))return openLibrary();
      if(button.hasAttribute('data-desk-open-accounts'))return openAccounts();
      if(button.hasAttribute('data-desk-tab'))return setTab(button.dataset.deskTab);
      if(button.hasAttribute('data-desk-tab-jump'))return setTab(button.dataset.deskTabJump);
      if(button.hasAttribute('data-desk-back-project')){inspectorDirty=false;selectedBinding=null;renderAll();return;}
      if(button.hasAttribute('data-desk-binding-flow')){const binding=currentBinding();return await openBindingFlow(button.dataset.project,binding);}
      if(button.hasAttribute('data-desk-edit-connection')){editorAssignment=null;el('desk-library-dialog').close();openModel(button.dataset.deskEditConnection);return;}
      if(button.hasAttribute('data-desk-catalog'))return await readCatalog(button.dataset.deskCatalog,button);
      if(button.hasAttribute('data-desk-inspect-catalog'))return await readCatalog(button.dataset.deskInspectCatalog,button,true);
      if(button.hasAttribute('data-desk-assign-model'))return await assignModel(button.dataset.deskAssignModel);
      if(button.hasAttribute('data-desk-model-project')){const model=button.dataset.deskModelProject;openMenu({project:projectView.project},button);el('desk-context-menu').innerHTML=`<button role="menuitem" data-desk-quick-assign="decision" data-model="${text(model)}">加入决策模型</button><button role="menuitem" data-desk-quick-assign="execution" data-model="${text(model)}">加入执行模型</button>`;el('desk-context-menu').querySelector('button').focus();return;}
      if(button.hasAttribute('data-desk-quick-assign')){closeMenu();return await assignModel(button.dataset.model,{project:projectView.project,role:button.dataset.deskQuickAssign});}
      if(button.hasAttribute('data-desk-import-model')){
        const entry=catalog?.models[Number(button.dataset.deskImportModel)];if(!entry)return;
        button.disabled=true;let model=state.models.find(m=>m.provider===catalog.provider&&m.model===entry.id);
        if(!model){model=await api('/api/models',{name:entry.name||entry.id,model:entry.id,provider:catalog.provider,enabled:true,default_agent:catalog.provider,max_input_tokens:entry.context_window||200000,max_output_tokens:32000});await refresh();}
        if(pendingAssignment)await assignModel(model.id);else{renderLibrary();button.textContent='已保存';}
        return;
      }
      if(button.hasAttribute('data-desk-account-refresh'))return await refreshAccount(button.dataset.deskAccountRefresh,button);
      if(button.hasAttribute('data-desk-engine-toggle')){const row=state.execution_endpoints?.rows.find(e=>e.id===button.dataset.deskEngineToggle);if(row){button.disabled=true;await saveEndpoint(row.id,{enabled:!row.enabled});renderEngines();}return;}
      if(button.hasAttribute('data-desk-cli-check')){button.disabled=true;try{const row=await api('/api/cli-updates/'+button.dataset.deskCliCheck+'/check',{});toast(row.latest?`已安装 ${row.installed||'未知'}；官方软件包 ${row.latest}${row.update_available?'，有可用更新':row.update_available===false?'，已是最新版':'；由宿主应用管理更新'}`:'未取得版本');await refresh();renderEngines();}finally{button.disabled=false;}return;}
      if(button.hasAttribute('data-desk-cli-update')){const gate=updateGate();if(gate.blocked){toast(gate.reason,true);return;}if(!confirm('更新前请确认此 CLI 没有在其他终端运行。继续更新？'))return;button.disabled=true;try{const row=await api('/api/cli-updates/'+button.dataset.deskCliUpdate+'/update',{});toast(`更新完成 · ${row.installed||'请重新读取版本'}`);await refresh();renderEngines();}finally{button.disabled=false;}return;}
      if(button.hasAttribute('data-desk-enable-endpoint')){await api('/api/execution-endpoints/'+button.dataset.deskEnableEndpoint,{enabled:true});await refresh();await loadWorkspace(true);return;}
      if(button.hasAttribute('data-desk-toggle-binding')){const binding=currentBinding();await api('/api/workspace/bindings',{action:binding.enabled?'disable':'enable',project:projectView.project,binding_id:binding.id,role:binding.role});await loadWorkspace(true);return;}
      if(button.hasAttribute('data-desk-new-task')){startTask(currentBinding());return;}
      if(button.hasAttribute('data-desk-reset-compact')){const form=button.closest('form');form.elements.autocompact_tokens.value=Math.floor(Number(form.elements.context_window.value)*.8/1000)*1000;markInspectorDirty();return;}
      if(button.hasAttribute('data-desk-revert')){inspectorDirty=false;renderProjectContent(true);return;}
      if(button.hasAttribute('data-desk-engine-config')){el('desk-engines-dialog').close();openEndpointDialog(button.dataset.deskEngineConfig);return;}
    }catch(error){const box=el('desk-library-dialog').open?el('desk-library-error'):el('desk-project-dialog').open?el('desk-project-error'):null;if(box)box.textContent=error.message;else if(!error.stale)toast(error.message,true);button.disabled=false;}
  });
  document.addEventListener('contextmenu',event=>{
    const model=event.target.closest('[data-binding-row]'),folder=event.target.closest('.project-folder>summary');
    if(model){event.preventDefault();openMenu({project:model.dataset.project,bindingId:model.dataset.bindingRow},model.querySelector('button'),event);}
    else if(folder){event.preventDefault();const button=folder.querySelector('[data-desk-project-menu]');openMenu({project:button.dataset.deskProjectMenu},button,event);}
  });
  document.addEventListener('pointerdown',event=>{if(!el('desk-context-menu').contains(event.target)&&!event.target.closest('[data-desk-binding-menu],[data-desk-project-menu],[data-desk-model-project]'))closeMenu();});
  // Tree keyboard: arrows move, Right/Left expand or collapse, Shift+F10 / ContextMenu opens the same menu.
  // Children of a collapsed folder keep layout boxes but cannot take focus.
  function inClosedFolder(node){for(let folder=node.closest('details');folder;folder=folder.parentElement?.closest('details')){if(!folder.open&&!folder.querySelector(':scope>summary')?.contains(node))return true;}return false;}
  function treeItems(){return [...el('desk-project-tree').querySelectorAll('.project-label,.role-folder>summary,.tree-model-button')].filter(node=>node.getClientRects().length&&!inClosedFolder(node));}
  function treeKey(event){
    const items=treeItems(),current=document.activeElement,index=items.indexOf(current);if(index<0)return false;
    const folder=current.matches('.project-label')?current.closest('.project-folder'):current.matches('summary')?current.parentElement:null;
    if(event.key==='ContextMenu'||(event.shiftKey&&event.key==='F10')){const row=current.closest('[data-binding-row]');if(row)openMenu({project:row.dataset.project,bindingId:row.dataset.bindingRow},current);else if(current.matches('.project-label'))openMenu({project:current.dataset.deskSelectProject},current);return true;}
    if(event.key==='ArrowRight'){if(folder&&!folder.open){folder.open=true;return true;}items[index+1]?.focus();return true;}
    if(event.key==='ArrowLeft'){if(folder&&folder.open){folder.open=false;return true;}const parent=current.closest('.role-folder')?.querySelector(':scope>summary');if(current.matches('.tree-model-button')&&parent){parent.focus();return true;}current.closest('.project-folder')?.querySelector('.project-label')?.focus();return true;}
    if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){const next=event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length;items[next].focus();return true;}
    return false;
  }
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape'){if(!el('desk-context-menu').hidden){closeMenu();return;}if(!el('desk-appearance').hidden){toggleAppearance(false);el('desk-appearance-open').focus();return;}closeSidebar();}
    if(event.target.closest('#desk-project-tree')&&treeKey(event)){event.preventDefault();return;}
    const tab=event.target.closest('[data-desk-tab]');
    if(tab&&['ArrowRight','ArrowLeft','Home','End'].includes(event.key)){event.preventDefault();const list=[...document.querySelectorAll('[data-desk-tab]')],index=list.indexOf(tab);const next=event.key==='Home'?0:event.key==='End'?list.length-1:(index+(event.key==='ArrowRight'?1:-1)+list.length)%list.length;setTab(list[next].dataset.deskTab,{focus:true});return;}
    const menu=el('desk-context-menu');if(menu.hidden)return;
    if(event.key==='Tab'){closeMenu();return;}
    if(!['ArrowDown','ArrowUp','Home','End'].includes(event.key))return;
    event.preventDefault();const buttons=[...menu.querySelectorAll('button:not(:disabled)')],index=buttons.indexOf(document.activeElement);const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length;buttons[next]?.focus();
  });
  document.addEventListener('input',event=>{if(event.target.closest('#desk-binding-form'))markInspectorDirty();});
  document.addEventListener('change',event=>{
    if(event.target.closest('#desk-binding-form'))markInspectorDirty();
    const sourceForm=event.target.closest('[data-desk-source-form]');
    if(sourceForm)sourceDrafts.set(sourceForm.dataset.deskSourceForm,event.target.value);
  });
  document.addEventListener('submit',async event=>{
    if(event.target.hasAttribute('data-desk-source-form')){
      event.preventDefault();const form=event.target,gate=updateGate(),id=form.dataset.deskSourceForm;
      if(gate.blocked){toast(gate.reason,true);return;}
      if(sourceSaving.has(id))return;
      const selected=form.elements.cli_source.value;sourceSaving.add(id);renderEngines();
      try{await api('/api/workflow',{endpoint_settings:{id,values:{cli_source:selected}}});sourceDrafts.delete(id);catalogs.delete(id);if(catalog?.provider===id)catalog=null;toast('执行端来源已保存');await refresh();await loadWorkspace(true);}
      catch(error){toast(error.message,true);}finally{sourceSaving.delete(id);renderEngines();}
      return;
    }
    if(event.target.id!=='desk-concurrency-form')return;
    event.preventDefault();const form=event.target,button=event.submitter;
    const project=form.dataset.project;button.disabled=true;
    form.querySelector('#desk-concurrency-error').textContent='';
    try{
      await api('/api/workspace/projects',{action:'concurrency',path:project,work_concurrency:Number(form.elements.work_concurrency.value)});
      settingsDirty=false;lastSettingsKey='';
      await loadWorkspace(true);toast('项目并发设置已保存');
    }catch(error){form.querySelector('#desk-concurrency-error').textContent=error.message;}
    finally{button.disabled=false;}
  });
  document.addEventListener('input',event=>{
    if(event.target.closest('#desk-concurrency-form')){settingsDirty=true;const node=el('desk-concurrency-state');node.textContent='有未保存的修改';node.classList.add('dirty');}
  });
  document.addEventListener('submit',async event=>{
    if(event.target.id!=='desk-binding-form')return;event.preventDefault();const form=event.target,button=event.submitter;button.disabled=true;
    try{
      const binding=currentBinding(),values={action:'save',project:projectView.project,binding_id:binding.id,role:binding.role,model_id:binding.model_id,is_default:form.elements.is_default.checked};
      if(isCli(modelOf(binding)))Object.assign(values,{effort:form.elements.effort.value,fast:form.elements.fast?.checked||false,context_window:Number(form.elements.context_window.value),autocompact_tokens:Number(form.elements.autocompact_tokens.value)});
      await api('/api/workspace/bindings',values);inspectorDirty=false;lastContentKey='';await loadWorkspace(true);el('desk-binding-save-state').textContent='已保存 · 新任务将使用此配置';
    }catch(error){el('desk-binding-error').textContent=error.message;}finally{button.disabled=false;}
  });
  el('desk-project-form').onsubmit=async event=>{
    event.preventDefault();const button=event.submitter;button.disabled=true;el('desk-project-error').textContent='';
    try{const path=el('desk-project-path').value.trim(),name=el('desk-project-name').value.trim();await api('/api/workspace/projects',{action:projectAction,path,...(name?{name}:{})});el('desk-project-dialog').close();await loadWorkspace(true);await selectProject(path);}
    catch(error){el('desk-project-error').textContent=error.message;}finally{button.disabled=false;}
  };
  el('desk-folder-pick').onclick=async()=>{
    const button=el('desk-folder-pick');button.disabled=true;button.textContent='选择中…';
    try{let result=await api('/api/projects/pick',{});const deadline=Date.now()+330000;while(result.status==='pending'){if(Date.now()>deadline)throw new Error('文件夹选择超时，请重试');await new Promise(resolve=>setTimeout(resolve,750));result=await api('/api/projects/pick/'+result.id);}if(result.status==='selected')el('desk-project-path').value=result.project;else if(result.status!=='cancelled')throw new Error(result.error||'无法选择文件夹');}
    catch(error){el('desk-project-error').textContent=error.message;}finally{button.disabled=false;button.textContent='选择文件夹';}
  };
  el('desk-add-project').onclick=()=>openProjectDialog();el('desk-library-open').onclick=()=>openLibrary();
  el('desk-add-api').onclick=()=>{editorAssignment=pendingAssignment?{...pendingAssignment}:null;el('desk-library-dialog').close();openModel();};
  el('desk-library-dialog').addEventListener('close',()=>{pendingAssignment=null;});
  el('model-dialog').addEventListener('close',()=>{editorAssignment=null;});
  el('desk-engines-open').onclick=()=>{renderEngines();el('desk-engines-dialog').showModal();};
  el('desk-accounts-open').onclick=openAccounts;
  el('desk-budget-open').onclick=()=>el('workflow-settings').click();
  el('desk-appearance-open').onclick=()=>toggleAppearance();
  el('sidebar-toggle').onclick=openSidebar;el('sidebar-close').onclick=closeSidebar;el('sidebar-scrim').onclick=closeSidebar;
  el('desk-project-search').oninput=renderTree;
  window.matchMedia('(max-width:900px)').addEventListener('change',closeSidebar);
  for(const tile of document.querySelectorAll('[data-stat-jump]'))tile.onclick=()=>{
    const jump=tile.dataset.statJump;
    if(jump==='review'){el('gov-inbox-open').click();return;}
    if(jump==='models'){setTab('models');return;}
    const filter=jump==='completed'?'completed':'active';
    document.querySelector(`[data-filter="${filter}"]`)?.click();setTab('tasks');
  };
  if(!isDshHost)el('usage-refresh').onclick=()=>refreshAccount('codex_cli',el('usage-refresh'));
  window.modelDeskTabs={set:setTab,current:()=>activeTab};
  window.modelDeskWorkspace={
    inspectModel:async (modelId,role)=>{const binding=currentProject()?.bindings.find(b=>b.model_id===modelId&&b.role===role);if(!binding){toast('此会话的模型已不在当前项目分组中；原运行参数仍可在检查器查看。',true);return;}await selectProject(projectView.project,binding.id);},
    refresh:()=>loadWorkspace(true),
    selectedBinding:()=>currentBinding(),
    executionModels:()=>{const project=currentProject();return project&&(project.managed||project.bindings.length)?project.bindings.filter(b=>b.role==='execution'&&b.enabled).map(b=>b.model_id):null;},
    executionDefault:()=>currentProject()?.bindings.find(b=>b.role==='execution'&&b.is_default&&b.enabled)?.model_id,
    takeEditorAssignment:()=>{const value=editorAssignment;editorAssignment=null;return value;},
    afterModelSave:async (model,assignment)=>{if(assignment&&model?.id)await assignModel(model.id,assignment);else await loadWorkspace(true);},
    discover:async provider=>{const result=await api('/api/provider-catalog',{provider});catalogs.set(provider,result);if(result.error)throw new Error(result.error);return result;}
  };
  document.addEventListener('desk-state',()=>{loadWorkspace();renderAccountSummary();if(el('desk-engines-dialog').open)renderEngines();});
  document.addEventListener('desk-project-changing',()=>{settingsDirty=false;lastSettingsKey='';renderHeader(null);});
  setTab(activeTab);renderHeader(null);renderWelcome();
  // A native account request remains explicit; first paint only reads local snapshots.
  loadWorkspace(true);
})();
