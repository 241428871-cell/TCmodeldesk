/* Humans own the contract; the selected native brain owns decomposition. */
(() => {
  const el=id=>document.getElementById(id), esc=value=>escapeText(String(value??''));
  const lines=value=>value.split('\n').map(v=>v.trim()).filter(Boolean);
  const when=value=>value?new Date(value).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'尚未安排';
  const closeIcon='<svg class="i" aria-hidden="true"><use href="#i-close"/></svg>';
  const stageNames={pending:'等待依赖',ready:'可派发',dispatched:'已派发',running:'执行中',executor_completed:'待主脑验收',accepted:'已验收',final_accepted:'最终验收',returned:'待整改',blocked:'受阻',failed:'失败',cancelled:'已取消 / 已替代',recovery_pending:'待核对恢复'};
  const stageKinds={pending:'idle',ready:'ready',dispatched:'running',running:'running',executor_completed:'waiting',accepted:'done',final_accepted:'done',returned:'waiting',blocked:'review',failed:'failed',cancelled:'stopped',recovery_pending:'unknown'};
  const clockNames={paused:'已暂停',stopped:'已停止',held:'保护等待',running:'主脑处理中',due:'等待本轮启动',waiting:'等待下次续跑',closed:'已结项'};
  const clockKinds={paused:'paused',stopped:'stopped',held:'waiting',running:'running',due:'waiting',waiting:'ready',closed:'done'};
  let data=null, selected='', loading=null, refreshPending=false, lastRead=0, lastMarkup='', project='', activeDialog=null;

  function dialog(id,title,content,footer=''){
    let node=el(id);
    if(!node){node=document.createElement('dialog');node.id=id;node.className='gov-dialog';document.body.append(node);}
    node.innerHTML=`<div class="gov-modal"><div class="dialog-heading"><h2>${esc(title)}</h2><button type="button" class="icon-button" data-gov-close aria-label="关闭">${closeIcon}</button></div>${content}${footer}</div>`;
    node.querySelectorAll('[data-gov-close]').forEach(button=>button.onclick=()=>node.close());
    node.addEventListener('close',()=>{activeDialog=null;},{once:true});
    node.showModal();activeDialog=node;return node;
  }
  async function action(button,run){
    button.disabled=true;
    try{await run();await refresh();}
    catch(error){if(!error.stale)toast(error.message,true);}
    finally{if(button.isConnected)button.disabled=false;}
  }
  function current(){return data?.goals.find(row=>row.goal.id===selected);}
  function goalRow(goalId=''){return goalId?data?.goals.find(row=>row.goal.id===goalId):current();}
  function allDecisions(){return Array.isArray(data?.decisions)?data.decisions:[];}
  function pending(){return allDecisions().filter(row=>row.status==='pending');}
  function recoveries(){return (data?.goals||[]).filter(row=>row.clock.active_intent&&(['unknown','revoked'].includes(row.clock.active_intent.status)||row.clock.hold?.kind==='unknown'));}
  async function refresh(force=true){
    // The main panel emits desk-state frequently while work is live. Governance
    // snapshots traverse every managed goal, so background ticks are bounded to
    // one read per four seconds. Explicit human actions always force a read.
    if(loading){refreshPending=true;return loading;}
    // Between reads, re-render from the cached snapshot so names from the
    // shared model list appear without another request (DOM writes are deduped).
    if(!force&&Date.now()-lastRead<4000){if(data&&project===(projectView.mode==='project'?projectView.project:''))render();return;}
    const bound=projectView.mode==='project'?projectView.project:'';
    const request=(async()=>{
      try{
        const fresh=await api('/api/governance');
        const currentBound=projectView.mode==='project'?projectView.project:'';
        if(bound!==currentBound){refreshPending=true;return;}
        if(bound!==project){selected='';lastMarkup='';project=bound;}
        data=fresh;lastRead=Date.now();
        if(!data.goals.some(row=>row.goal.id===selected))selected=(data.goals.find(row=>row.goal.status==='active')||data.goals[0])?.goal.id||'';
        render();
      }catch(error){if(!error.stale){el('governance-workbench').innerHTML=`<p class="form-error" role="alert">${esc(error.message)}</p>`;lastMarkup='';setMarkup(el('overview-goal'),`<p class="form-error" role="alert">目标状态读取失败：${esc(error.message)}</p>`);}}
    })().finally(()=>{
      if(loading===request)loading=null;
      if(refreshPending){refreshPending=false;void refresh(true);}
    });
    loading=request;return request;
  }
  function clockState(row){
    const {goal,clock}=row;
    if(goal.status!=='active')return {kind:goal.status==='completed'?'done':'stopped',label:goal.status==='completed'?'目标已完成':'目标已取消'};
    if(!clock.exists)return {kind:'idle',label:'未启用续跑'};
    if(clock.hold?.kind==='unknown'||['unknown','revoked'].includes(clock.active_intent?.status))return {kind:'unknown',label:clock.hold?.kind==='unknown'?'原轮次待核对':'原轮次授权已撤销 · 待核对'};
    return {kind:clockKinds[clock.status]||'idle',label:clockNames[clock.status]||clock.status};
  }
  const stateTag=(kind,label)=>`<span class="stag s-${kind}"><span class="sdot s-${kind}"></span>${esc(label)}</span>`;
  function stageLabel(stage,row){
    const blocked=stage.human_blockers||[];
    if(blocked.some(v=>v.requires_replan))return {kind:'waiting',label:'待主脑修订'};
    if(blocked.length||pending().some(v=>v.affected_stage_ids?.includes(stage.id)))return {kind:'review',label:'待人工审核'};
    if(stage.status==='ready'&&!row?.clock.enabled)return {kind:'ready',label:'就绪 · 未启动'};
    return {kind:stageKinds[stage.status]||'idle',label:stageNames[stage.status]||stage.status};
  }
  function put(node,value){if(node&&node.textContent!==String(value))node.textContent=value;}
  function render(){
    el('gov-inbox-open').disabled=false;el('gov-policy-open').disabled=false;
    const attention=pending().length+recoveries().length;
    put(el('gov-inbox-count'),attention);el('gov-inbox-open').classList.toggle('has-items',attention>0);
    put(el('gov-policy-open').querySelector('.label-text'),`工程习惯${data.policy?.effective_rules?.length?' · 已选择':''}`);
    const review=el('stat-review');if(review){put(review,project?attention:'—');review.closest('.stat-tile')?.setAttribute('data-tone',attention?'review':'');put(el('stat-review-caption'),recoveries().length?`含 ${recoveries().length} 个原轮次核对`:'项');}
    const badge=el('tab-goal-badge');if(badge){badge.hidden=!attention;put(badge,attention);badge.title=attention?`${attention} 项待你处理`:'';}
    const row=current(), count=data.goals.filter(v=>v.goal.status==='active').length;
    renderSummary(row,attention);
    let markup=`<div class="gov-heading"><div class="gov-heading-text"><p class="gov-eyebrow">目标驱动 · AI 动态拆分</p><h2>${row?esc(row.goal.title):'目标与续跑'}</h2>${row?.goal.objective?`<p class="gov-objective">${esc(row.goal.objective)}</p>`:''}</div><div class="gov-heading-actions">${data.goals.length>1?`<label class="sr-only" for="gov-goal-select">当前目标</label><select id="gov-goal-select">${data.goals.map(v=>`<option value="${esc(v.goal.id)}"${v.goal.id===selected?' selected':''}>${esc(v.goal.title)}${v.goal.status==='active'?'':' · '+(v.goal.status==='completed'?'已完成':'已取消')}</option>`).join('')}</select>`:''}<button class="button secondary" data-gov-new><svg class="i" aria-hidden="true"><use href="#i-plus"/></svg>固定新目标</button></div></div>`;
    if(!project){markup+=`<div class="gov-empty"><strong>先选择一个项目</strong><p>每个项目保留自己的目标准则、AI 阶段、续跑时钟和人工决策。</p></div>`;}
    else if(!row){markup+=`<div class="gov-empty"><strong>你定目标与准则，主脑拆分和推进</strong><p>提交目标后，决策模型会生成阶段、依赖和验收条件，并依据执行结果调整。遇到需要人决定的事项，会提交到右上角的“待审核”。</p><p class="field-hint">默认每 30 分钟续跑；一个目标只保留一条正式续跑入口。保存目标不会自动调用模型。</p><button class="button primary" data-gov-new>固定目标与准则</button></div>`;}
    else markup+=goalMarkup(row,count);
    if(data.legacy_goal_count)markup+=`<p class="field-hint">另有 ${data.legacy_goal_count} 个旧版目标，原记录继续保留。<button class="text-button" data-gov-legacy>查看旧版目标</button></p>`;
    if(markup===lastMarkup)return;
    const node=el('governance-workbench');
    const open=new Set([...node.querySelectorAll('details[open]')].map(v=>v.dataset.govDisclosure));
    const focus=node.contains(document.activeElement)?document.activeElement?.dataset?.govControl:null;
    node.innerHTML=markup;lastMarkup=markup;
    node.querySelectorAll('details[data-gov-disclosure]').forEach(v=>{if(open.has(v.dataset.govDisclosure))v.open=true;});
    if(focus)node.querySelector(`[data-gov-control="${CSS.escape(focus)}"]`)?.focus({preventScroll:true});
    el('gov-goal-select')?.addEventListener('change',event=>{selected=event.target.value;render();});
    for(const bar of node.querySelectorAll('[data-progress]'))bar.style.setProperty('--w',bar.dataset.progress+'%');
  }
  // Overview card: a projection of the same governance read, never a second state source.
  function renderSummary(row,attention){
    const node=el('overview-goal');if(!node)return;
    if(!project){setMarkup(node,'');return;}
    if(!row){setMarkup(node,`<div class="ov-head"><div><p class="ov-kicker">当前目标</p><h2>还没有目标</h2></div></div><p class="ov-empty">固定目标与准则后，决策模型会生成阶段、依赖和验收条件。保存目标不会调用模型。</p><div class="ov-actions"><button class="button primary" data-gov-new>固定目标与准则</button></div>`);return;}
    const {goal,plan,clock}=row,stages=plan.stages.filter(s=>!s.data?.dynamic_plan?.superseded_by);
    const done=stages.filter(s=>['accepted','final_accepted'].includes(s.status)).length,percent=stages.length?Math.round(done/stages.length*100):0;
    const live=stages.filter(s=>['running','dispatched'].includes(s.status)).length,pendingReq=(row.requirements||[]).filter(v=>v.status==='pending').length;
    const status=clockState(row),closed=clock.closed||goal.status!=='active';
    const markup=`<div class="ov-head"><div><p class="ov-kicker">当前目标${data.goals.filter(v=>v.goal.status==='active').length>1?' · 共 '+data.goals.filter(v=>v.goal.status==='active').length+' 个进行中':''}</p><h2>${esc(goal.title)}</h2></div>${stateTag(status.kind,status.label)}</div><div class="gov-progress-bar" role="img" aria-label="${done} / ${stages.length} 个阶段已验收"><i data-progress="${percent}"></i></div><dl class="ov-facts"><div><dt>阶段</dt><dd>${stages.length?`${done} / ${stages.length} 已验收`:'等待主脑生成'}</dd></div><div><dt>执行中</dt><dd>${live}</dd></div><div><dt>计划版本</dt><dd>v${plan.plan_revision}</dd></div><div><dt>下次续跑</dt><dd>${!clock.enabled||closed?'—':esc(when(clock.next_due_at))}</dd></div></dl>${pendingReq?`<p class="ov-note s-waiting">${pendingReq} 条最新需求待主脑处理</p>`:''}${attention?`<button class="gov-review-banner" data-gov-inbox>待你处理 ${attention} 项<span>查看依据并回答 →</span></button>`:''}<div class="ov-actions"><button class="button secondary" data-gov-progress>/btw 进展</button>${closed?'':'<button class="button secondary" data-gov-requirement>插入需求…</button>'}<button class="button secondary" data-desk-tab-jump="goal">打开目标</button></div>`;
    if(setMarkup(node,markup))for(const bar of node.querySelectorAll('[data-progress]'))bar.style.setProperty('--w',bar.dataset.progress+'%');
  }
  function requirementReceipt(v){
    if(v.kind!=='direction_change'||v.status!=='pending') return v.applies_after==='current_round'?' · 主脑在下一步改动前读取':'';
    if(v.interrupt_state==='incomplete') return ` · 中断回执待核对 ${v.interrupt_pending_task_ids?.length||0} 个旧任务`;
    if(v.interrupt_state==='requested') return ` · 已请求停止 ${v.interrupted_task_ids?.length||0} 个旧任务，等待其退出`;
    return ' · 已请求旧主脑在受控边界停止';
  }
  function goalMarkup(row,count){
    const {goal,plan,clock}=row, stages=plan.stages.filter(s=>!s.data?.dynamic_plan?.superseded_by);
    const done=stages.filter(s=>['accepted','final_accepted'].includes(s.status)).length;
    const status=clockState(row);
    const paused=!clock.enabled, stopped=clock.user_intent==='STOP', closed=clock.closed||goal.status!=='active';
    const settlement=goal.status==='completed'&&clock.exists&&!clock.closed;
    const active=clock.active_intent;
    const last=clock.last_result;
    const goalPending=pending().filter(v=>v.goal_id===goal.id);
    const recoveryPending=recoveries().some(v=>v.goal.id===goal.id);
    const requirements=row.requirements||[],pendingReq=requirements.filter(v=>v.status==='pending');
    const latestDirection=pendingReq.slice().reverse().find(v=>v.kind==='direction_change');
    const primary=settlement?'<button class="button primary" data-gov-control="settle_terminal">核对并结束续跑</button>':closed?'':paused?`<button class="button primary" data-gov-control="start"${plan.criteria.length?'':' disabled title="请先补充可核验的准则"'}>${stopped?'重新授权启动':plan.plan_revision?'恢复续跑':'交给 AI 拆分'}</button>`:`<button class="button primary" data-gov-control="run_now"${active?' disabled title="主脑本轮还在处理，结束后才能再推进"':''}>${active?'本轮进行中':'立即推进'}</button><button class="button secondary" data-gov-control="pause">暂停目标</button>`;
    const secondary=closed?'':`<button class="button secondary" data-gov-clock-settings>时钟设置</button>${clock.exists&&paused&&!stopped&&!active&&!clock.hold?.kind?'<button class="button secondary" data-gov-handoff>移交主脑…</button>':''}${stopped?'':'<button class="text-button gov-stop" data-gov-control="stop">停止目标</button>'}`;
    return `<section class="gov-status-card" aria-label="续跑状态"><div class="gov-status-main">${stateTag(status.kind,status.label)}<dl class="gov-clock-facts"><div><dt>周期</dt><dd>${clock.interval_seconds?Math.round(clock.interval_seconds/60)+' 分钟':'未设置'}</dd></div><div><dt>下次</dt><dd>${paused||closed?'—':esc(when(clock.next_due_at))}</dd></div><div><dt>主脑</dt><dd ${clock.owner_epoch?`title="交接代次 ${esc(clock.owner_epoch)}"`:''}>${esc(brainName(clock.brain_model_id))}</dd></div>${active?'<div><dt>本轮</dt><dd>已持有执行权</dd></div>':''}<div><dt>进行中目标</dt><dd>${count}</dd></div></dl></div><div class="gov-controls">${primary}${secondary}</div></section>
      ${settlement?'<p class="field-hint">目标已完成，续跑收尾待核对。核对只处理原轮次和剩余义务，不会重新执行项目任务。</p>':''}
      ${clock.hold?.kind?`<div class="gov-notice ${clock.hold.kind==='unknown'?'unknown':''}" role="status"><p>${esc(clock.hold.reason)}${clock.hold.kind==='unknown'?' · 原进程与回执待核对，不会盲目重启。':''}</p><button class="button secondary" data-gov-recovery>${clock.hold.kind==='unknown'?'核对原进程':'处理保护等待'}</button></div>`:active&&paused?'<div class="gov-notice unknown" role="status"><p>暂停时本轮仍持有执行权，需要核对原轮次。</p><button class="button secondary" data-gov-recovery>核对原轮次</button></div>':''}
      ${goalPending.length||recoveryPending?`<button class="gov-review-banner" data-gov-inbox>待你处理 ${goalPending.length+(recoveryPending?1:0)} 项 <span>查看人工事项与原轮次证据 →</span></button>`:''}
      <section class="gov-interact" aria-label="运行中交互"><div><h3>运行中交互</h3><p>/btw 只读查询当前进展，不暂停、不重置、不触发模型。插入的需求按版本记录：增量需求保留原目标；方向调整会请求受影响的旧执行停止，等原进程退出、资源释放后再继续。</p>${latestDirection?`<p class="gov-inline s-waiting">方向调整 R${latestDirection.version} 待处理${esc(requirementReceipt(latestDirection))}</p>`:pendingReq.length?`<p class="gov-inline s-waiting">${pendingReq.length} 条最新需求待主脑在安全边界处理</p>`:''}</div><div class="gov-interact-actions"><button class="button secondary" data-gov-progress>/btw 进展</button>${closed?'':'<button class="button secondary" data-gov-requirement>插入最新需求…</button>'}</div></section>
      ${last?.summary?`<p class="field-hint gov-last">上轮回执：${esc(last.summary)}</p>`:''}
      ${clock.last_resolution?'<p class="field-hint">原轮次已由人工核对并结束；当前工作保留，成果验收状态未变。</p>':''}
      <div class="gov-plan-heading"><h3>主脑计划 <span class="count">v${plan.plan_revision}</span></h3><span class="subtle">${stages.length?`${done} / ${stages.length} 阶段通过验收`:'等待主脑生成'}</span></div>
      ${stages.length?`<div class="gov-progress-bar" aria-hidden="true"><i data-progress="${Math.round(done/stages.length*100)}"></i></div><ol class="gov-stages">${stages.map((stage,index)=>stageMarkup(stage,index,stages,row)).join('')}</ol>`:`<p class="gov-plan-empty">${plan.criteria.length?'准则已保存。点击“交给 AI 拆分”，使用本项目决策模型生成具体阶段。':'阶段内容由 AI 生成；请先补充准则。'}</p>`}
      <div class="gov-contract-row"><details data-gov-disclosure="criteria-${esc(goal.id)}"><summary>人类固定准则 <span class="subtle">${plan.criteria.length} 条 · ${plan.review_policy==='final'?'最终结果须人工验收':'异常事项提交审核'}</span></summary>${plan.criteria.length?`<ol>${plan.criteria.map(v=>`<li>${esc(v)}</li>`).join('')}</ol>`:'<p class="field-hint">这是原有目标，先补充可核验的准则，再交给主脑动态拆分。</p>'}<p class="field-hint">可修改文件：${plan.file_scope==='project'?'项目范围：AI 自主选择每阶段文件；运行记录和账户配置除外。':plan.allowed_files?.length?esc(plan.allowed_files.join('、')):'指定范围为空：不允许编辑。'}</p></details><button class="text-button" data-gov-criteria${closed?' disabled':''}>编辑准则</button></div>
      <details class="gov-history" data-gov-disclosure="history-${esc(goal.id)}"><summary>需求、计划修订与续跑义务 <span class="subtle">${pendingReq.length} 条待处理 · ${plan.history.length} 次修订</span></summary>${requirements.length?`<h4>运行中需求</h4><ol>${requirements.slice().reverse().map(v=>`<li><strong>R${v.version}</strong> ${esc(v.text)} <small>${v.kind==='direction_change'?'方向调整':'增量需求'} · ${v.status==='applied'?'已纳入主脑轮次':'待处理'}${requirementReceipt(v)} · ${when(v.created_at)}</small></li>`).join('')}</ol>`:'<p class="field-hint">尚未插入运行中需求。</p>'}${plan.history.length?`<h4>计划修订</h4><ol>${plan.history.slice().reverse().map(v=>`<li><strong>v${v.revision}</strong> ${esc(v.reason)} <small>${when(v.created_at)}</small></li>`).join('')}</ol>`:'<p class="field-hint">尚无 AI 修订记录。新计划会记录理由，保留已执行阶段的证据。</p>'}${clock.obligations?.length?`<h4>收尾义务</h4><ul>${clock.obligations.map(v=>`<li>${esc(v.title)} · ${v.status==='pending'?'未完成':v.status==='fulfilled'?'已完成':'已取消'}${v.evidence?' — '+esc(v.evidence):''}</li>`).join('')}</ul>`:''}<p class="field-hint">自动退避只延长周期。资源恢复不会覆盖人工暂停或停止；收尾义务清空且结果验收后才可结项。</p></details>`;
  }
  function brainName(id){return id?(state.models||[]).find(m=>m.id===id)?.name||id:'未设置';}
  function stageMarkup(stage,index,stages,row){
    const deps=(stage.depends_on||[]).map(id=>stages.find(v=>v.id===id)?.title||id);
    const depNumbers=(stage.depends_on||[]).map(id=>stages.findIndex(v=>v.id===id)+1).filter(n=>n>0);
    const label=stageLabel(stage,row);
    return `<li class="gov-stage s-${label.kind}"><details data-gov-disclosure="stage-${esc(stage.id)}"><summary><span class="gov-stage-number">${index+1}</span><span class="gov-stage-name">${esc(stage.title)}</span><span class="gov-stage-meta">${stage.kind==='edit'?'修改':'只读'}${depNumbers.length?` · 接在步骤 ${depNumbers.join('、')} 之后`:deps.length?` · 依赖 ${deps.length} 项`:''}</span>${stateTag(label.kind,label.label)}</summary><div class="gov-stage-detail"><p>${esc(stage.objective)}</p>${deps.length?`<p class="field-hint">依赖：${esc(deps.join('、'))}</p>`:''}<strong>验收条件</strong><ul>${(stage.acceptance||[]).map(v=>`<li>${esc(typeof v==='string'?v:v.text||v.id)}</li>`).join('')}</ul>${stage.files?.length?`<p class="field-hint">文件范围：${esc(stage.files.join('、'))}</p>`:''}<p class="field-hint">${stage.kind==='edit'?'修改任务':'只读分析'}${stage.active_task_id?' · 已关联执行记录':''}</p></div></details></li>`;
  }
  function openRecovery(button,row=current()){
    if(!row)return;
    const hold=row.clock.hold||{};
    if(hold.kind==='unknown'||row.clock.active_intent)return window.modelDeskReconciliation.open(row.goal.id,row.goal.title,button,refresh);
    const node=dialog('gov-recovery-dialog','处理保护等待',`<form id="gov-recovery-form"><div class="gov-form-scroll"><p>${esc(hold.reason)}</p><label>已解决的依据<textarea name="evidence" rows="3" required placeholder="说明授权、连接或资源问题如何解决。"></textarea></label><p class="field-hint">解除这项保护不会覆盖人工暂停或停止。未知进程必须先核对身份与退出状态。</p><p class="form-error" id="gov-recovery-error" role="alert"></p></div><div class="dialog-footer"><button class="button secondary" type="button" data-gov-close>取消</button><button class="button primary" type="submit">记录依据并解除保护</button></div></form>`);
    node.querySelector('[data-gov-close]').onclick=()=>node.close();
    el('gov-recovery-form').onsubmit=async event=>{event.preventDefault();const submit=event.currentTarget.querySelector('[type="submit"]');submit.disabled=true;try{await api(`/api/governance/goals/${row.goal.id}/control`,{action:'clear_hold',guard_id:hold.guard_id,authorized:true,evidence:new FormData(event.currentTarget).get('evidence')});node.close();await refresh();}catch(error){el('gov-recovery-error').textContent=error.message;}finally{submit.disabled=false;}};
  }
  function openGoal(edit=false){
    if(!project){toast('请先从左侧选择项目文件夹',true);return;}
    const row=edit?current():null;
    const node=dialog('gov-goal-dialog',row?'编辑固定准则':'固定目标与准则',`<form id="gov-goal-form"><div class="gov-form-scroll"><p class="gov-dialog-scope" title="${esc(project)}">保存到项目：<strong>${esc(projectBaseName(project))}</strong></p>${row?'':`<label>目标名称<input name="title" required maxlength="200" placeholder="例如：完成 macOS 桌面适配"></label><label>期望结果<textarea name="objective" rows="3" required placeholder="描述最后需要交付的结果；阶段拆分交给 AI。"></textarea></label>`}<label>固定验收准则 <span class="optional">每行一条</span><textarea name="criteria" rows="4" required placeholder="例如：macOS 安装与启动成功&#10;主要操作能通过浏览器完成&#10;交付改动说明与验证证据">${esc(row?.plan.criteria.join('\n')||'')}</textarea></label><label>文件范围<select id="gov-file-scope" name="file_scope"><option value="project"${!row||row.plan.file_scope==='project'?' selected':''}>项目范围 · AI 自主选择阶段文件</option><option value="selected"${row&&row.plan.file_scope!=='project'?' selected':''}>指定文件 · 保留固定清单</option></select></label><div id="gov-file-list"${!row||row.plan.file_scope==='project'?' hidden':''}><label>指定文件 <span class="optional">每行一个相对路径；留空不允许编辑</span><textarea name="allowed_files" rows="2" spellcheck="false" placeholder="src/main.py&#10;README.md">${esc(row?.plan.allowed_files?.join('\n')||'')}</textarea></label></div><label>人工审核范围<select name="review_policy"><option value="exceptions"${row?.plan.review_policy==='final'?'':' selected'}>异常事项：越权、缺失信息和需要选择的方案</option><option value="final"${row?.plan.review_policy==='final'?' selected':''}>异常事项 + 最终交付验收</option></select></label>${row?'':`<label>续跑周期 / 分钟<input name="interval" type="number" min="1" max="10080" value="30" required></label><label>收尾义务 <span class="optional">每行一条</span><textarea name="obligations" rows="2" placeholder="归档交付物&#10;汇总验证结果"></textarea></label>`}<p class="field-hint">你只固定准则和授权边界。阶段、依赖和执行策略由决策模型生成，并根据实际结果修订。保存不会自动调用模型。</p><p class="form-error" role="alert" id="gov-goal-error"></p></div><div class="dialog-footer"><button type="button" class="button secondary" data-gov-close>取消</button><button class="button primary" type="submit">保存准则</button></div></form>`);
    node.querySelectorAll('[data-gov-close]').forEach(b=>b.onclick=()=>node.close());
    el('gov-file-scope').onchange=()=>{el('gov-file-list').hidden=el('gov-file-scope').value==='project';};
    el('gov-goal-form').onsubmit=async event=>{
      event.preventDefault();const form=event.currentTarget,button=form.querySelector('[type="submit"]'),f=new FormData(form);
      button.disabled=true;el('gov-goal-error').textContent='';
      const body={...(row?{base_revision:row.plan.contract_revision}:{}),criteria:lines(f.get('criteria')),file_scope:f.get('file_scope'),allowed_files:f.get('file_scope')==='selected'?lines(f.get('allowed_files')):[],review_policy:f.get('review_policy')};
      if(!row)Object.assign(body,{title:f.get('title'),objective:f.get('objective'),interval_seconds:Number(f.get('interval'))*60,obligations:lines(f.get('obligations')).map((title,index)=>({id:`human-${index+1}`,title}))});
      try{const result=await api(row?`/api/governance/goals/${row.goal.id}/contract`:'/api/governance/goals',body);if(result.goal)selected=result.goal.id;node.close();toast(result.warnings?.length?'准则已保存；'+result.warnings.join('；'):'准则已保存；主脑将依据这些边界拆分任务。',!!result.warnings?.length);await refresh();if(!row)window.modelDeskTabs?.set('goal');}
      catch(error){el('gov-goal-error').textContent=error.message;}
      finally{button.disabled=false;}
    };
  }
  async function openHandoff(){
    const row=current(), scope=project;if(!row)return;
    const workspace=await api('/api/workspace');
    if(project!==scope||current()?.goal.id!==row.goal.id)return;
    const choices=(workspace.projects.find(item=>item.path===scope)?.bindings||[]).filter(item=>item.role==='decision'&&item.enabled&&item.model?.enabled!==false&&['codex_cli','claude_cli'].includes(item.model?.provider));
    const node=dialog('gov-handoff-dialog','移交主脑',`<form id="gov-handoff-form"><div class="gov-form-scroll"><p>当前主脑：${esc(brainName(row.clock.brain_model_id))} · 交接代次 ${row.clock.owner_epoch||0}</p><label>接手的决策模型<select name="model_id" required>${choices.map(item=>`<option value="${esc(item.model_id)}">${esc(item.model.name||item.model_id)} · ${esc(item.model.provider)}</option>`).join('')}</select></label>${choices.length?'':'<p class="gov-notice">请先在项目侧栏添加并启用 Claude 或 Codex 决策模型。</p>'}<p class="field-hint">移交保留阶段、验收记录、义务和续跑周期。原生会话归档，新主脑从项目记录重建上下文。目标保持暂停，之后可手动恢复续跑。</p><p class="field-hint">仍有执行任务或未核清的轮次时，移交将被拒绝。</p><p class="form-error" id="gov-handoff-error" role="alert"></p></div><div class="dialog-footer"><button type="button" class="button secondary" data-gov-close>取消</button><button class="button primary" type="submit"${choices.length?'':' disabled'}>移交并保持暂停</button></div></form>`);
    let requestId=crypto.randomUUID();
    node.querySelector('select').onchange=()=>{requestId=crypto.randomUUID();};
    const reasons={handoff_stale_epoch:'目标授权已变化，请关闭并重新打开移交窗口。',handoff_requires_unheld_pause:'请先暂停目标并处理保护等待。',handoff_unsettled_round:'旧主脑轮次尚未核清，请先核对原进程与回执。',handoff_unsettled_worker:'还有未结束或待核对的执行任务，请先处理。',handoff_goal_not_active:'目标已经结束，不能移交。'};
    el('gov-handoff-form').onsubmit=async event=>{
      event.preventDefault();const button=event.currentTarget.querySelector('[type="submit"]');button.disabled=true;
      try{
        if(project!==scope)throw new Error('项目已切换，请重新选择目标。');
        await api(`/api/governance/goals/${row.goal.id}/control`,{action:'handoff',project:scope,request_id:requestId,model_id:new FormData(event.currentTarget).get('model_id'),expected_owner_epoch:row.clock.owner_epoch||0,expected_authority_epoch:row.clock.authority_epoch});
        node.close();toast('主脑已移交，目标保持暂停。');await refresh();
      }catch(error){el('gov-handoff-error').textContent=reasons[error.message]||error.message;}
      finally{button.disabled=false;}
    };
  }
  function openClock(){
    const row=current();if(!row)return;
    const node=dialog('gov-clock-dialog','续跑时钟',`<form id="gov-clock-form"><div class="gov-form-scroll"><p class="field-hint">同一目标始终复用这一条续跑入口。你可以显式调整周期；自动退避只能延长。</p><label>续跑周期 / 分钟<input name="interval" type="number" min="1" max="10080" value="${Math.round((row.clock.interval_seconds||1800)/60)}" required></label><p class="field-hint">暂停会撤销本轮授权并暂停后续派发，原结果保留待核对。停止还会请求停止该目标正在执行的任务；是否已退出以实际进程记录为准。</p><p class="form-error" id="gov-clock-error" role="alert"></p></div><div class="dialog-footer"><button type="button" class="button secondary" data-gov-close>取消</button><button class="button primary" type="submit">保存周期</button></div></form>`);
    node.querySelector('[data-gov-close]').onclick=()=>node.close();
    el('gov-clock-form').onsubmit=async event=>{event.preventDefault();const button=event.currentTarget.querySelector('[type="submit"]');button.disabled=true;try{await api(`/api/governance/goals/${row.goal.id}/control`,{action:'configure',interval_seconds:Number(new FormData(event.currentTarget).get('interval'))*60});node.close();await refresh();}catch(error){el('gov-clock-error').textContent=error.message;}finally{button.disabled=false;}};
  }
  async function openProgress(goalId=''){
    const row=goalRow(goalId);if(!row){toast('此会话关联的目标不在当前项目记录中。',true);return;}
    const result=await api(`/api/governance/goals/${row.goal.id}/interactions`,{message:'/btw'}),p=result.progress;
    dialog('gov-progress-dialog','当前工作进展',`<div class="gov-form-scroll"><p class="gov-dialog-goal">${esc(row.goal.title)}</p><dl class="gov-progress"><div><dt>当前工作</dt><dd>计划 v${p.plan_revision} · ${p.running_count} 个执行中 · ${p.accepted_count}/${p.stage_count} 个阶段已验收</dd></div><div><dt>最新需求</dt><dd>${p.pending_requirement_count} 条待主脑处理</dd></div><div><dt>阻碍</dt><dd>${esc(p.blocker||'无')}</dd></div><div><dt>下一步</dt><dd>${esc(p.next_step)}</dd></div></dl><p class="field-hint">/btw 只读取当前记录，不暂停、重置或触发模型调用。</p></div><div class="dialog-footer"><button class="button secondary" data-gov-close>关闭</button></div>`);
  }
  function openRequirement(goalId=''){
    const row=goalRow(goalId);if(!row){toast('此会话关联的目标不在当前项目记录中。',true);return;}
    if(row.goal.status!=='active'){toast('此目标已经结束，不能再插入运行中需求。',true);return;}
    const node=dialog('gov-requirement-dialog','插入最新需求',`<form id="gov-requirement-form"><div class="gov-form-scroll"><p class="gov-dialog-goal">${esc(row.goal.title)}</p><fieldset class="gov-kind"><legend>类型</legend><label class="gov-kind-option"><input type="radio" name="kind" value="requirement" checked><span><strong>增量需求</strong><small>保留原目标和不冲突的要求；主脑在下一步改动前读取新版。</small></span></label><label class="gov-kind-option"><input type="radio" name="kind" value="direction_change"><span><strong>方向调整</strong><small>请求旧主脑和受影响任务停止，重排后续计划；等原进程退出、资源释放后再按新方向执行。</small></span></label></fieldset><label>需求内容<textarea name="message" rows="5" maxlength="20000" required placeholder="直接写最新要求。主脑会调整阶段、依赖和验收条件，并复用已有成果。"></textarea></label><p class="field-hint">保存后立即记录。增量需求在主脑下一步改动前生效；方向调整先请求旧主脑和受影响任务停止，等原进程退出、占用的执行资源释放后再按新方向继续。旧轮次不能把新需求标成已处理。暂停、停止和更换目标请用各自的按钮。</p><p class="form-error" id="gov-requirement-error" role="alert"></p></div><div class="dialog-footer"><button type="button" class="button secondary" data-gov-close>取消</button><button class="button primary" type="submit">插入需求</button></div></form>`);
    const requestId=crypto.randomUUID();
    const submit=node.querySelector('[type="submit"]');
    node.querySelectorAll('[name="kind"]').forEach(input=>input.onchange=()=>{submit.textContent=input.value==='direction_change'&&input.checked?'提交方向调整':'插入需求';});
    el('gov-requirement-form').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget,button=form.querySelector('[type="submit"]'),f=new FormData(form);button.disabled=true;try{const result=await api(`/api/governance/goals/${row.goal.id}/interactions`,{message:f.get('message'),kind:f.get('kind'),request_id:requestId});node.close();const requirement=result.requirement||{};toast(requirement.kind==='direction_change'?(requirement.interrupt_state==='incomplete'?'方向调整已保存；部分旧任务中断回执待核对。':'方向调整已保存；旧执行正在受控停止。'):'增量需求已保存；主脑会在下一步改动前读取。');await refresh();}catch(error){el('gov-requirement-error').textContent=error.message;}finally{button.disabled=false;}};
  }
  function openInbox(){
    if(!project){toast('请先选择项目，再查看待审核事项',true);return;}
    const items=pending(), rounds=recoveries();
    const recoveryMarkup=rounds.map(row=>`<article class="gov-decision recovery"><div class="gov-decision-heading"><span class="stag s-unknown"><span class="sdot s-unknown"></span>原轮次待核对</span><h3>${esc(row.goal.title)}</h3></div><p>${esc(row.clock.hold?.reason||'原授权已撤销，请核对进程与回执，再决定是否保留当前工作。')}</p><p class="field-hint">核对只结束原轮次，成果验收与人工暂停、停止设置保持原状。</p><button class="button secondary" data-recovery-goal="${esc(row.goal.id)}">核对原轮次…</button></article>`).join('');
    const node=dialog('gov-inbox-dialog','待你审核与核对',`<div class="gov-form-scroll gov-inbox-list">${recoveryMarkup}${items.length?items.map(v=>`<article class="gov-decision" data-decision="${esc(v.id)}"><div class="gov-decision-heading"><span class="stag s-review"><span class="sdot s-review"></span>${v.kind==='final'?'最终交付':'人工选择'}</span><h3>${esc(v.title||v.question||'需要你决定')}</h3></div><p>${esc(v.reason||v.question||'')}</p>${v.recommendation?`<p class="gov-recommend"><strong>主脑建议：</strong>${esc(v.recommendation)}</p>`:''}<details><summary>依据与影响范围</summary><ul>${(v.evidence_refs||v.evidence||[]).map(e=>`<li>${esc(typeof e==='string'?e:e.detail||e.text||JSON.stringify(e))}</li>`).join('')}</ul><p class="field-hint">${v.affected_stage_ids?.length?'影响 '+v.affected_stage_ids.length+' 个阶段；其他无关分支可继续。':'最终产物审核；其他无关分支可继续。'}</p></details><label>补充意见 <span class="optional">可选</span><textarea rows="2" data-decision-notes></textarea></label><div class="gov-decision-actions">${(v.options||[]).map(o=>`<button class="button secondary" data-decision-answer="${esc(o.id)}">${esc(o.label)}</button>`).join('')}</div></article>`).join(''):rounds.length?'':'<div class="gov-empty"><strong>暂无待处理事项</strong><p>主脑需要你的判断或原轮次需要核对时，会带着依据提交到这里。</p></div>'}</div><div class="dialog-footer"><span class="field-hint">回答绑定当前方案版本；旧审核不能批准新内容。</span><button class="button secondary" data-gov-close>关闭</button></div>`);
    node.querySelectorAll('[data-recovery-goal]').forEach(button=>button.onclick=()=>{const row=rounds.find(v=>v.goal.id===button.dataset.recoveryGoal);node.close();openRecovery(el('gov-inbox-open'),row);});
    node.querySelectorAll('[data-decision-answer]').forEach(button=>{const item=items.find(v=>v.id===button.closest('[data-decision]').dataset.decision);const option=item.options.find(v=>v.id===button.dataset.decisionAnswer);const approve=button.dataset.decisionAnswer===item.approval_option_id;button.classList.toggle('approve',approve);button.innerHTML=`<span>${esc(option.label)}</span><small>${approve?'允许当前提案继续':'退回主脑修订'}</small>`;button.onclick=()=>action(button,async()=>{const article=button.closest('[data-decision]');await api(`/api/governance/decisions/${item.id}/answer`,{subject_hash:item.subject_hash,authority_epoch:item.fingerprint.authority_epoch,option_id:button.dataset.decisionAnswer,notes:article.querySelector('textarea').value});await refresh();node.close();openInbox();});});
  }
  function openPolicy(){
    const p=data?.policy;if(!p)return;
    const loaded=current()?.policy_runtime;
    const loadedLabels={confirmed:'已加载并确认',captured:'已捕获 · 待回执',starting:'新轮次待加载',stale:'配置已更新 · 待下一轮',unreadable:'回执待核对',not_loaded:'尚未加载'};
    const loadedMarkup=project?`<div class="gov-policy-project"><strong>当前目标实际加载</strong><p class="field-hint">${esc(loadedLabels[loaded?.status]||'尚未加载')} · ${esc(loaded?.message||'主脑尚未加载工程习惯快照')}</p>${loaded?.loaded_snapshot_id?`<p class="field-hint">加载版本 ${esc(loaded.loaded_revision)} · 快照 ${esc(loaded.loaded_snapshot_id.slice(0,12))}</p>`:''}</div>`:'';
    const checks=(ids,group)=>(p.available_rules||[]).map(v=>`<label class="gov-rule"><input type="checkbox" name="${group}" value="${esc(v.id)}"${ids.includes(v.id)?' checked':''}><span><strong>${esc(v.title)}</strong><small>${esc(v.text)}</small></span></label>`).join('');
    const node=dialog('gov-policy-dialog','跨项目工程习惯',`<form id="gov-policy-form"><div class="gov-form-scroll"><p class="field-hint">将你已有的工程方法凝练为可选指导，注入新主脑轮次与执行任务。当前用户要求与项目约定优先；不会带入其他项目的路径、账户和预算。</p><label class="gov-checkbox"><input type="checkbox" name="global_enabled"${p.global_enabled?' checked':''}>全局嵌入：供所有继承全局设置的项目使用</label><details open><summary>全局方法类别</summary><div class="gov-rules">${checks(p.global_rule_ids||[],'global_rules')}</div><label>全局补充习惯<textarea name="global_custom_rules" rows="2">${esc(p.global_custom_rules)}</textarea></label></details>${project?`<div class="gov-policy-project"><label>当前项目<select name="mode"><option value="inherit"${p.configured_mode==='inherit'?' selected':''}>继承全局选择</option><option value="off"${p.configured_mode==='off'?' selected':''}>本项目关闭嵌入</option><option value="custom"${p.configured_mode==='custom'?' selected':''}>本项目自定义</option></select></label><div id="gov-project-rules"${p.configured_mode==='custom'?'':' hidden'}><div class="gov-rules">${checks(p.rule_ids||[],'project_rules')}</div><label>项目补充习惯<textarea name="custom_rules" rows="2">${esc(p.custom_rules)}</textarea></label></div></div>`:''}${loadedMarkup}<details><summary>生效与来源</summary><p class="field-hint">配置版本 ${p.revision} · 当前生效 ${p.effective_rules?.length||0} 类。每个实际任务记录独立加载快照；保存后不会改写在途任务。</p><ul>${[...new Set((p.available_rules||[]).map(v=>v.source))].map(v=>`<li class="field-hint gov-source">${esc(v)}</li>`).join('')}</ul></details><p class="form-error" id="gov-policy-error" role="alert"></p></div><div class="dialog-footer"><button type="button" class="button secondary" data-gov-close>取消</button><button class="button primary" type="submit">保存工程习惯</button></div></form>`);
    node.querySelector('[data-gov-close]').onclick=()=>node.close();
    node.querySelector('[name="mode"]')?.addEventListener('change',event=>{el('gov-project-rules').hidden=event.target.value!=='custom';});
    el('gov-policy-form').onsubmit=async event=>{event.preventDefault();const f=new FormData(event.currentTarget),button=event.currentTarget.querySelector('[type="submit"]');button.disabled=true;try{await api('/api/engineering-policy',{scope:'global',base_revision:p.global_revision,global_enabled:f.has('global_enabled'),global_rule_ids:f.getAll('global_rules'),global_custom_rules:f.get('global_custom_rules')});if(project)await api('/api/engineering-policy',{scope:'project',base_revision:p.project_revision,mode:f.get('mode'),rule_ids:f.getAll('project_rules'),custom_rules:f.get('custom_rules')});node.close();toast('已保存；新主脑轮次和执行任务将记录所选规则快照。');await refresh();}catch(error){el('gov-policy-error').textContent=error.message+' 已完成的独立范围不会回滚，请刷新核对。';}finally{button.disabled=false;}};
  }
  el('gov-policy-open').onclick=openPolicy;el('gov-inbox-open').onclick=openInbox;
  el('new-goal').onclick=()=>openGoal();
  function onWorkbenchClick(event){
    const button=event.target.closest('button');if(!button)return;
    if(button.hasAttribute('data-gov-new'))return openGoal();
    if(button.hasAttribute('data-gov-criteria'))return openGoal(true);
    if(button.hasAttribute('data-gov-clock-settings'))return openClock();
    if(button.hasAttribute('data-gov-progress'))return action(button,openProgress);
    if(button.hasAttribute('data-gov-requirement'))return openRequirement();
    if(button.hasAttribute('data-gov-handoff'))return action(button,openHandoff);
    if(button.hasAttribute('data-gov-inbox'))return openInbox();
    if(button.hasAttribute('data-gov-recovery'))return openRecovery(button);
    if(button.hasAttribute('data-gov-legacy')){document.body.classList.add('gov-legacy-visible');el('goals-detail').hidden=false;el('goals-toggle').setAttribute('aria-expanded','true');el('goals-panel').scrollIntoView({block:'start'});return;}
    const control=button.dataset.govControl,row=current();
    if(!control||!row)return;
    if(control==='stop'&&!confirm(`停止目标「${row.goal.title}」？会撤销授权并请求停止该目标正在执行的任务；之后需要重新授权才能启动。`))return;
    action(button,async()=>{await api(`/api/governance/goals/${row.goal.id}/control`,{action:control});toast(control==='stop'?'已撤销旧授权并请求停止；正在核对进程与回执。':control==='pause'?'已撤销本轮授权并暂停后续派发，原结果保留待核对。':'已提交；实际状态会在此更新。');});
  }
  el('governance-workbench').addEventListener('click',onWorkbenchClick);
  el('overview-goal').addEventListener('click',onWorkbenchClick);
  document.addEventListener('desk-state',()=>refresh(false));
  document.addEventListener('desk-project-changing',()=>{activeDialog?.close();selected='';project='';data=null;lastRead=0;lastMarkup='';document.body.classList.remove('gov-legacy-visible');el('governance-workbench').innerHTML='<p class="field-hint">正在切换项目…</p>';setMarkup(el('overview-goal'),'<p class="field-hint">正在读取目标…</p>');el('gov-inbox-count').textContent='0';el('gov-inbox-open').classList.remove('has-items');el('gov-inbox-open').disabled=true;el('gov-policy-open').disabled=true;});
  window.modelDeskGovernance={refresh,openProgress,openRequirement};
  refresh();
})();
