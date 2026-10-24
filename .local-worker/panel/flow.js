/* Domain receipts and bounded output. Opening this view never dispatches or attaches stdin. */
(() => {
  const esc = value => escapeText(String(value ?? ''));
  const el = id => document.getElementById(id);
  const recordLabels = {claimed:"持有执行权",started:"已登记原进程",unknown:"回执待核对",completed:"本轮已结算",finished:"本轮已结算",failed:"本轮失败",blocked:"等待处理",reconciled:"原轮次已核对",human_reconciled:"人工已核对并结束",revoked:"授权已撤销",abandoned:"已放弃原轮次",queued:"等待派发",running:"执行中",cancelled:"已停止"};
  const statusLabels = {registered:'等待启动',idle:'空闲',running:'运行中',exited:'原进程已退出',unknown:'状态待核对'};
  const nodeStatus=node=>node.role==='external'?(node.status==='exited'?'已完成':node.status==='unknown'?'回执待核对':statusLabels[node.status]):statusLabels[node.status];
  const nodeKind=node=>node.status==='unknown'?'unknown':node.status==='running'?'running':node.status==='registered'?'waiting':node.status==='exited'?(node.role==='external'?'done':'stopped'):'idle';
  const tag=node=>`<span class="stag s-${nodeKind(node)}"><span class="sdot s-${nodeKind(node)}"></span>${esc(nodeStatus(node)||'状态未知')}</span>`;
  const closeIcon='<svg class="i" aria-hidden="true"><use href="#i-close"/></svg>';
  let trace = null, selected = '', mode = window.matchMedia('(max-width:900px)').matches?'text':'graph', channel = 'stdout', view = 'raw', generation = 0;
  let loading = false, outputLoading = false, openedOutput = false, menuKey = '', menuFocus = null;
  let lastMarkup = '', lastRead = 0, outputGeneration = 0, lastOutputRead = 0;
  let pendingRecord = '', pendingModel = '', pendingRole = '', afterClose = null;
  const dialog = document.createElement('dialog');
  dialog.id = 'flow-dialog'; dialog.className = 'flow-dialog'; dialog.setAttribute('aria-labelledby','flow-title');
  dialog.innerHTML = `<div class="flow-shell"><header class="flow-header"><div class="flow-title-block"><h2 id="flow-title">消息流</h2><p id="flow-project">只展示项目真实回执 · 双击节点或按 Enter 查看原生输出</p></div><button class="icon-button" id="flow-close" aria-label="关闭消息流">${closeIcon}</button></header><div class="flow-toolbar"><div class="segmented" role="group" aria-label="消息流显示方式"><button data-flow-mode="graph" aria-pressed="true"><svg class="i" aria-hidden="true"><use href="#i-grid"/></svg>流图</button><button data-flow-mode="text" aria-pressed="false"><svg class="i" aria-hidden="true"><use href="#i-list"/></svg>文字</button></div><button class="button secondary" id="flow-refresh"><svg class="i" aria-hidden="true"><use href="#i-refresh"/></svg>刷新</button><label class="flow-live" title="只停止画面跟随，不暂停任何任务"><input type="checkbox" id="flow-live" checked>查看时同步</label><span id="flow-summary" class="flow-sync" role="status">尚未读取</span><div class="flow-legend" aria-label="图例"><span><i class="legend-edge"></i>真实消息</span><span><span class="sdot s-running"></span>运行中</span><span><span class="sdot s-unknown"></span>待核对</span></div><p id="flow-error" class="flow-error" role="alert" hidden></p></div><div class="flow-body"><section id="flow-scene" class="flow-scene" aria-label="真实消息流图"></section><aside class="flow-inspector" aria-label="CLI 只读检查器"><div id="flow-metadata"><h3>CLI 检查器 · 只读</h3><p class="flow-output-note">选择节点查看登记信息，双击读取对应原生输出。</p></div><div class="flow-output-toolbar"><div class="segmented" role="group" aria-label="输出通道"><button data-flow-channel="stdout" aria-pressed="true">标准输出</button><button data-flow-channel="stderr" aria-pressed="false">错误输出</button></div><div class="segmented" role="group" aria-label="输出视图"><button data-flow-view="raw" aria-pressed="true">原文</button><button data-flow-view="events" aria-pressed="false" title="把 Claude stream-json 或 Codex JSON 事件整理为工具、命令、子代理和结果">结构化</button></div><button class="button primary" id="flow-read-output">读取输出</button><button class="button secondary" id="flow-node-actions" aria-haspopup="menu">操作…</button></div><div class="flow-terminal"><div class="flow-terminal-bar" aria-hidden="true"><i></i><i></i><i></i><span>只读 · 无输入</span></div><pre id="flow-output" tabindex="0" aria-label="原生 CLI 输出，只读">尚未选择会话。</pre></div><p id="flow-output-note" class="flow-output-note">原生输出视图；没有终端输入或 PTY 连接。</p></aside></div></div><div id="flow-menu" class="flow-menu" role="menu" aria-label="会话操作" hidden></div>`;
  document.body.append(dialog);
  const reference = key => trace?.references.find(row=>row.session_key===key);
  const nodeOf = key => trace?.flow.nodes.find(row=>row.session_key===key);
  function error(message=''){el('flow-error').textContent=message;el('flow-error').hidden=!message;}
  function metadata(){
    const ref=reference(selected),node=nodeOf(selected);
    el('flow-read-output').disabled=!node||node.view_kind==='unavailable';el('flow-node-actions').disabled=!ref;
    if(!node){el('flow-metadata').innerHTML='<h3>CLI 检查器 · 只读</h3><p class="flow-output-note">选择节点查看登记信息，双击读取对应原生输出。</p>';return;}
    if(!ref){el('flow-metadata').innerHTML=`<div class="flow-inspector-head"><h3>${esc(node.model_label)}</h3>${tag(node)}</div><dl><dt>角色</dt><dd>CLI 工具或子代理事件 · ${esc(nodeStatus(node))}</dd><dt>来源</dt><dd>原生 JSON 事件流的有界只读投影</dd><dt>权限</dt><dd>没有输入、控制或独立输出读取能力</dd></dl>`;return;}
    const runtime=ref.runtime;
    el('flow-metadata').innerHTML=`<div class="flow-inspector-head"><h3>${esc(node.model_label)}</h3>${tag(node)}</div><dl><dt>角色</dt><dd>${ref.role==='decision'?'决策主脑':'执行端'} · ${esc(statusLabels[node.status])}</dd>${ref.operation_status?`<dt>执行记录</dt><dd>${esc(recordLabels[ref.operation_status]||ref.operation_status)}</dd>`:""}<dt>后端</dt><dd>${esc(ref.engine||'未记录')} · ${node.view_kind==='native_events'?'原生输出':'输出适配未接入'}</dd><dt>原生会话</dt><dd>${esc(ref.native_session_id||'尚未取得')}</dd><dt>权限快照</dt><dd>${esc(ref.permission_mode)}</dd>${runtime?`<dt>运行快照</dt><dd>effort ${esc(runtime.effort)} · fast ${runtime.fast?'开启':'关闭'}<br>窗口 ${esc(runtime.context_window??'原生默认')} · 自动压缩 ${esc(runtime.autocompact_tokens??'原生默认')}</dd>`:''}<dt>记录</dt><dd>${esc(ref.record_id)}</dd></dl>`;
  }
  function choose(key){
    if(!nodeOf(key))return;
    if(selected!==key){selected=key;openedOutput=false;++outputGeneration;el('flow-output').textContent='双击节点或点击“读取输出”，查看实际捕获的 CLI 输出。';el('flow-output-note').textContent='只读查看；模型设置的修改只用于后续工作。';}
    dialog.querySelectorAll('[data-flow-node]').forEach(node=>node.classList.toggle('selected',node.dataset.flowNode===key));metadata();
  }
  function nodeMarkup(node){const activity=node.role==='external';return `<article class="flow-node flow-role-${esc(node.role)}${activity?' flow-activity-node':''}" data-flow-node="${esc(node.session_key)}"><span class="flow-port in" aria-hidden="true"></span><span class="flow-port out" aria-hidden="true"></span><button class="flow-node-main" data-flow-select="${esc(node.session_key)}" title="${esc(node.model_label)}${activity?' · 只读事件节点':' · 双击查看 CLI 输出'}"><span class="flow-node-title"><span class="sdot s-${nodeKind(node)}" aria-hidden="true"></span><strong>${esc(node.model_label)}</strong></span><span class="flow-node-row"><span>状态</span><b class="s-${nodeKind(node)}">${esc(nodeStatus(node))}${node.stale?' · 未实时核对':''}</b></span><span class="flow-node-row"><span>输出</span><b>${activity?'CLI 事件摘要 · 无输入':node.view_kind==='native_events'?'原生输出可读':'输出适配未接入'}</b></span></button>${activity?'':`<button class="tree-menu-button" data-flow-menu="${esc(node.session_key)}" aria-label="${esc(node.model_label)}会话操作" aria-haspopup="menu"><svg class="i" aria-hidden="true"><use href="#i-more"/></svg></button>`}</article>`;}
  function render(){
    dialog.querySelectorAll('[data-flow-mode]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.flowMode===mode)));
    const flow=trace.flow,nodes=flow.nodes,scene=el('flow-scene');
    const omitted=flow.omitted_nodes+flow.omitted_edges+trace.observation_gaps+flow.unresolved_messages;
    el('flow-summary').textContent=`${nodes.length} 个节点 · ${flow.edges.length} 条消息流${omitted?' · '+omitted+' 处省略或缺口':''}${trace.window_limited?' · 已限制历史窗口':''}`;
    const markup=mode==='graph'?`<div id="flow-canvas" class="flow-canvas"><svg class="flow-edges" id="flow-edges" aria-hidden="true"></svg><div class="flow-lanes">${[['decision','决策主脑'],['execution','执行模型'],['external','工具与子代理']].map(([role,label])=>`<div class="flow-lane flow-lane-${role}"><h3>${label}<span class="count">${nodes.filter(node=>node.role===role).length}</span></h3>${nodes.filter(node=>node.role===role).map(nodeMarkup).join('')||'<p class="flow-empty">暂无已观察节点</p>'}</div>`).join('')}</div></div>`:`<h3>节点</h3><ul class="flow-text-list">${nodes.map(node=>`<li><button class="flow-text-node" data-flow-select="${esc(node.session_key)}" data-flow-node="${esc(node.session_key)}"><span class="flow-text-name">${esc(node.model_label)}</span>${tag(node)}</button></li>`).join('')}</ul><h3>消息方向</h3><ul class="flow-text-list">${flow.edges.map(edge=>`<li>${esc(nodeOf(edge.source_session)?.model_label)} → ${esc(nodeOf(edge.target_session)?.model_label)} · ${edge.message_count} 条消息（发送 ${edge.sent_count} / 返回 ${edge.received_count}）</li>`).join('')||'<li>没有已观察到的消息；任务依赖不会被画成消息线。</li>'}</ul>`;
    const content=nodes.length?markup:'<div class="flow-empty"><strong>还没有真实会话</strong><p>主脑运行和实际派发后，这里会显示消息方向。打开流图不会启动模型。</p></div>';
    if(content!==lastMarkup){const focus=document.activeElement?.dataset?.flowSelect;scene.innerHTML=content;lastMarkup=content;if(focus)scene.querySelector(`[data-flow-select="${CSS.escape(focus)}"]`)?.focus({preventScroll:true});requestAnimationFrame(drawEdges);}
    dialog.querySelectorAll('[data-flow-node]').forEach(node=>node.classList.toggle('selected',node.dataset.flowNode===selected));metadata();
  }
  function drawEdges(){
    const canvas=el('flow-canvas'),svg=el('flow-edges');if(!canvas||!svg||!trace)return;
    const root=canvas.getBoundingClientRect();
    svg.setAttribute('viewBox',`0 0 ${root.width} ${root.height}`);
    svg.innerHTML='<defs><marker id="flow-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path class="flow-arrow" d="M 0 0 L 10 5 L 0 10 z"/></marker></defs>';
    for(const edge of trace.flow.edges){
      const source=canvas.querySelector(`[data-flow-node="${CSS.escape(edge.source_session)}"]`),target=canvas.querySelector(`[data-flow-node="${CSS.escape(edge.target_session)}"]`);if(!source||!target)continue;
      const a=source.getBoundingClientRect(),b=target.getBoundingClientRect(),forward=a.left<b.left;
      const x1=(forward?a.right:a.left)-root.left,x2=(forward?b.left:b.right)-root.left,y1=a.top+a.height/2-root.top,y2=b.top+b.height/2-root.top;
      const bend=(forward?1:-1)*Math.max(16,Math.abs(x2-x1)/2);
      const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',`M ${x1} ${y1} C ${x1+bend} ${y1-12}, ${x2-bend} ${y2-12}, ${x2} ${y2}`);path.setAttribute('class','flow-edge');path.setAttribute('marker-end','url(#flow-arrow)');svg.append(path);
      const label=document.createElementNS('http://www.w3.org/2000/svg','text');label.setAttribute('x',String((x1+x2)/2));label.setAttribute('y',String((y1+y2)/2+(forward?-8:16)));label.setAttribute('text-anchor','middle');label.setAttribute('class','flow-edge-label');label.textContent=String(edge.message_count);svg.append(label);
    }
  }
  async function load(force=false){
    if(loading||!dialog.open||document.hidden||(!force&&Date.now()-lastRead<5000))return;
    const own=++generation,project=projectView.project;loading=true;el('flow-refresh').disabled=true;
    try{const next=await api('/api/flow');if(own!==generation||!dialog.open||project!==projectView.project)return;if(next.flow?.project_id!==project)throw new Error('消息流项目不一致，已忽略返回结果');if(selected&&!next.flow.nodes.some(row=>row.session_key===selected)){selected='';openedOutput=false;++outputGeneration;el('flow-output').textContent='此会话已移出当前历史窗口，请重新选择。';}if(selected&&reference(selected)?.native_capture_id!==next.references.find(row=>row.session_key===selected)?.native_capture_id){openedOutput=false;++outputGeneration;el("flow-output").textContent="此任务已进入新的原生调用；请重新双击查看。";}trace=next;lastRead=Date.now();error();render();}
    catch(problem){if(own===generation&&!problem.stale)error(`消息流读取失败：${problem.message}`);}
    finally{if(own===generation){loading=false;el('flow-refresh').disabled=false;}}
    if(own===generation&&trace&&pendingRecord){const requested=pendingRecord;pendingRecord='';const ref=trace.references.find(row=>row.record_id===requested);if(ref){choose(ref.session_key);readOutput();}else error('原轮次不在当前消息流窗口中；请核对目标阶段与原始记录。');}
    else if(own===generation&&trace&&pendingModel){const model=pendingModel,role=pendingRole;pendingModel='';pendingRole='';const ref=trace.references.find(row=>row.model_id===model&&(!role||row.role===role));if(ref)choose(ref.session_key);else error('此模型在当前消息流窗口中没有可定位的真实会话；启用或就绪不代表已经启动。');}
  }
  async function readOutput(force=true){
    if(!dialog.open||(!force&&Date.now()-lastOutputRead<5000))return;
    const node=nodeOf(selected);if(!node||node.view_kind==='unavailable'){el('flow-output').textContent='此执行适配器尚未提供可读取的原生输出。可在任务动态中查看现有结果与验收记录。';return;}
    openedOutput=true;if(force&&innerWidth<=900)dialog.querySelector('.flow-inspector').scrollIntoView({block:'start',behavior:window.matchMedia('(prefers-reduced-motion:reduce)').matches?'auto':'smooth'});if(outputLoading)return;const own=++outputGeneration,key=selected,readChannel=channel,capture=reference(selected)?.native_capture_id||"";outputLoading=true;lastOutputRead=Date.now();el('flow-read-output').disabled=true;
    try{const readView=view;const tail=await api(`/api/flow/sessions/${encodeURIComponent(key)}?channel=${readChannel}&capture_id=${encodeURIComponent(capture)}&view=${readView}`);if(own!==outputGeneration||!dialog.open||selected!==key||channel!==readChannel||view!==readView)return;
      if(readView==='events'){const output=el('flow-output');output.textContent=eventText(tail);el('flow-output-note').textContent=`结构化事件 · 只读 · ${tail.events.length} 条${tail.truncated?' · 仅含输出尾部':''}${tail.skipped_lines?' · '+tail.skipped_lines+' 行非事件':''}`;return;}
      const output=el('flow-output'),bottom=output.scrollHeight-output.scrollTop-output.clientHeight<32;output.textContent=tail.available?(tail.text||'当前输出为空。'):'尚未捕获到本通道输出。';if(bottom)output.scrollTop=output.scrollHeight;el('flow-output-note').textContent=`原生输出 · 只读 · ${tail.total_bytes} 字节${tail.omitted_bytes?' · 前 '+tail.omitted_bytes+' 字节已省略':''}`;
    }catch(problem){if(own===outputGeneration&&!problem.stale)el('flow-output-note').textContent=`输出读取失败：${problem.message}`;}
    finally{outputLoading=false;metadata();if(own!==outputGeneration&&dialog.open&&openedOutput)readOutput();}
  }
  const eventLabels={session:'会话',message:'回复',tool:'工具',tool_result:'结果',command:'命令',file_change:'文件',result:'结束',error:'错误'};
  function eventText(view){
    if(view.format==='unknown')return '此输出不是可识别的 JSON 事件流；请切换到“原文”查看。';
    if(!view.events.length)return '尾部没有可整理的事件。';
    return view.events.map(row=>`${row.parent?'    ↳ 子代理 ':''}[${eventLabels[row.kind]||row.kind}] ${row.title}${row.detail?' — '+row.detail:''}${row.status?' ('+row.status+')':''}`).join('\n');
  }
  function closeMenu(restore=false){const key=menuKey;el('flow-menu').hidden=true;menuKey='';if(restore){const target=menuFocus?.isConnected?menuFocus:dialog.querySelector(`[data-flow-select="${CSS.escape(key)}"]`);target?.focus({preventScroll:true});}}
  function menu(key,anchor,event){
    choose(key);const ref=reference(key);if(!ref)return;menuKey=key;menuFocus=anchor.matches('button')?anchor:anchor.querySelector('button');
    const goalActions=ref.goal_id?'<button role="menuitem" data-flow-action="progress">/btw 查看该目标进展</button><button role="menuitem" data-flow-action="requirement">向该目标插入最新需求…</button>':'';
    const box=el('flow-menu');box.innerHTML=`<button role="menuitem" data-flow-action="inspect">只读查看 CLI 输出</button>${goalActions}<button role="menuitem" data-flow-action="settings">编辑项目模型设置…</button>${ref.role==='decision'?'<button role="menuitem" data-flow-action="pause">暂停该目标的新派发</button><button role="menuitem" data-flow-action="stop">停止该目标</button>':'<button role="menuitem" data-flow-action="cancel">停止该任务</button>'}`;
    box.hidden=false;const bounds=dialog.getBoundingClientRect(),at=anchor.getBoundingClientRect(),scale=bounds.width/dialog.offsetWidth;if(innerWidth>900){box.style.left=Math.max(8,Math.min(((event?.clientX??at.right)-bounds.left)/scale,dialog.clientWidth-box.offsetWidth-12))+'px';box.style.top=Math.max(8,Math.min(((event?.clientY??at.bottom)-bounds.top)/scale,dialog.clientHeight-box.offsetHeight-12))+'px';}else{box.style.left='';box.style.top='';}box.querySelector('button').focus();
  }
  async function action(name){
    const ref=reference(menuKey);if(!ref)return;const key=menuKey;closeMenu();error();
    try{
      if(name==='inspect'){choose(key);await readOutput();return;}
      if(name==='progress'){await window.modelDeskGovernance.openProgress(ref.goal_id);return;}
      if(name==='requirement'){window.modelDeskGovernance.openRequirement(ref.goal_id);return;}
      if(name==='settings'){dialog.close();await window.modelDeskWorkspace.inspectModel(ref.model_id,ref.role);return;}
      if(name==='cancel')await api(`/api/tasks/${encodeURIComponent(ref.record_id)}/cancel`,{});
      else await api(`/api/governance/goals/${encodeURIComponent(ref.goal_id)}/control`,{action:name});
      await load(true);await refresh();
    }catch(problem){if(dialog.open)error(problem.message);else toast(problem.message,true);}
  }
  function open(options={}){if(projectView.mode!=='project'){toast('请先在左侧选择具体项目，再查看消息流。',true);return;}if(dialog.open)return;afterClose=options.onClose||null;pendingRecord=options.recordId||'';pendingModel=options.modelId||'';pendingRole=options.role||'';trace=null;selected='';lastMarkup='';lastRead=0;openedOutput=false;el('flow-scene').innerHTML='<p class="flow-empty">正在读取真实运行记录…</p>';el('flow-project').textContent=projectView.project;dialog.showModal();metadata();load(true);}
  el('flow-open').onclick=()=>open();
  window.modelDeskFlow={open};
  el('flow-close').onclick=()=>dialog.close();el('flow-refresh').onclick=()=>load(true);el('flow-read-output').onclick=readOutput;
  el('flow-node-actions').onclick=event=>{if(selected)menu(selected,event.currentTarget);};
  dialog.addEventListener('close',()=>{++generation;++outputGeneration;loading=false;closeMenu();trace=null;openedOutput=false;pendingRecord='';pendingModel='';pendingRole='';el('flow-output').textContent='尚未选择会话。';el('flow-output-note').textContent='原生输出视图；没有终端输入或 PTY 连接。';const callback=afterClose;afterClose=null;if(callback)callback();else el('flow-open').focus();});
  dialog.addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button)return;
    if(button.dataset.flowMode&&trace){mode=button.dataset.flowMode;dialog.querySelectorAll('[data-flow-mode]').forEach(v=>v.setAttribute('aria-pressed',String(v===button)));render();}
    if(button.dataset.flowSelect)choose(button.dataset.flowSelect);
    if(button.dataset.flowMenu)menu(button.dataset.flowMenu,button,event);
    if(button.dataset.flowAction)action(button.dataset.flowAction);
    if(button.dataset.flowView){view=button.dataset.flowView;++outputGeneration;dialog.querySelectorAll('[data-flow-view]').forEach(v=>v.setAttribute('aria-pressed',String(v===button)));if(selected)readOutput();}
    if(button.dataset.flowChannel){channel=button.dataset.flowChannel;++outputGeneration;dialog.querySelectorAll('[data-flow-channel]').forEach(v=>v.setAttribute('aria-pressed',String(v===button)));readOutput();}
  });
  dialog.addEventListener('dblclick',event=>{const button=event.target.closest('[data-flow-select]');if(button){choose(button.dataset.flowSelect);readOutput();}});
  dialog.addEventListener('contextmenu',event=>{const node=event.target.closest('[data-flow-node],[data-flow-select]');if(node){event.preventDefault();menu(node.dataset.flowNode||node.dataset.flowSelect,node,event);}});
  dialog.addEventListener('pointerdown',event=>{if(!el('flow-menu').hidden&&!el('flow-menu').contains(event.target)&&!event.target.closest('[data-flow-menu],#flow-node-actions'))closeMenu();});
  dialog.addEventListener('keydown',event=>{
    if(!el('flow-menu').hidden){const items=[...el('flow-menu').querySelectorAll('button')],index=items.indexOf(document.activeElement);if(event.key==='Escape'){event.preventDefault();event.stopPropagation();closeMenu(true);}if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();items[event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length]?.focus();}if(event.key==='Tab')closeMenu();return;}
    const node=event.target.closest('[data-flow-select]');if(node&&event.key==='Enter'){event.preventDefault();choose(node.dataset.flowSelect);readOutput();}if(node&&(event.key==='ContextMenu'||(event.shiftKey&&event.key==='F10'))){event.preventDefault();menu(node.dataset.flowSelect,node);}
  });
  const observer=new ResizeObserver(()=>{if(dialog.open&&mode==='graph')requestAnimationFrame(drawEdges);});observer.observe(el('flow-scene'));
  document.addEventListener('desk-state',()=>{if(el('flow-live').checked){load();if(dialog.open&&!document.hidden&&openedOutput)readOutput(false);}});
  document.addEventListener('desk-project-changing',()=>{++generation;++outputGeneration;if(dialog.open)dialog.close();});
})();
