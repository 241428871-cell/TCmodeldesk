/* On-demand original-round evidence. A human disposition never accepts model work. */
(() => {
  const esc = value => escapeText(String(value ?? ''));
  const processNames = {running:'仍在运行', exited:'已确认退出', unknown:'身份或退出状态待核对', not_started:'已确认未启动', not_applicable:'无原生子进程'};
  const taskNames = {queued:'等待执行', running:'执行中', completed:'执行结束', failed:'执行失败', cancelled:'已停止', recovery_pending:'待恢复核对'};
  const phaseNames = {snapshot:'上下文快照', native:'原生主脑', actions:'方案与派发', settle:'轮次结算'};
  const phaseStates = {done:'回执已保存', inflight:'结果待核对', failed:'执行失败'};
  const requestNames = {plan:'计划修订', dispatch:'执行派发', review:'成果审查', handover:'工作交接', question:'人工事项', obligation:'收尾义务', complete:'目标结项', batch:'动作清单'};
  const controlNames = {RUN:'允许续跑', USER_PAUSE:'人工暂停', STOP:'人工停止'};
  const drafts = new Map();
  let node, issue = null, goal = '', title = '', trigger = null, onResolved = null;
  let generation = 0, busy = false, settled = false, suspended = false, boundProject = '';
  const find = id => node.querySelector('#recon-' + id);

  function create() {
    node = document.createElement('dialog');
    node.id = 'reconciliation-dialog'; node.className = 'gov-dialog recon-dialog';
    node.setAttribute('aria-labelledby', 'recon-title');
    node.innerHTML = `<div class="gov-modal"><div class="dialog-heading"><h2 id="recon-title" tabindex="-1">核对原轮次</h2><button type="button" class="icon-button" data-recon-close aria-label="关闭核对"><svg class="i" aria-hidden="true"><use href="#i-close"/></svg></button></div><form id="recon-form"><div class="gov-form-scroll"><p id="recon-goal" class="recon-goal"></p><p class="field-hint">先核对原进程与工作目录。保留当前工作只结束原轮次，成果仍须单独验收；暂停、停止和续跑周期保持原状。</p><div class="recon-tools"><button type="button" class="button secondary" id="recon-refresh">刷新核对证据</button><button type="button" class="text-button" id="recon-flow">查看消息流与原生输出</button></div><div id="recon-evidence" aria-busy="false"></div><p id="recon-status" role="status" class="field-hint"></p><div id="recon-decision"><label for="recon-notes">实际核对依据<textarea id="recon-notes" name="notes" rows="3" maxlength="4000" required placeholder="例如：已核对原输出与项目文件，保留当前修改，交由后续轮次重新验收。"></textarea></label><label class="gov-checkbox"><input id="recon-reviewed" type="checkbox"><span>我已核对当前工作目录与原始记录，决定保留当前工作。</span></label><p class="field-hint">“暂不处理”保留本页未提交意见；切换项目或关闭页面后清除。</p></div><p id="recon-error" class="form-error" role="alert"></p></div><div class="dialog-footer"><button type="button" class="button secondary" data-recon-close>暂不处理</button><button type="submit" class="button primary" id="recon-submit" disabled>保留工作，结束原轮次</button></div></form></div>`;
    document.body.append(node);
    node.querySelectorAll('[data-recon-close]').forEach(button => button.onclick = () => node.close());
    node.addEventListener('close', () => {
      ++generation; busy = false;
      if (!suspended) (trigger?.isConnected ? trigger : document.getElementById('gov-inbox-open'))?.focus({preventScroll:true});
    });
    find('refresh').onclick = refresh;
    find('flow').onclick = openFlow;
    find('notes').oninput = () => { drafts.set(goal, find('notes').value); readiness(); };
    find('reviewed').onchange = readiness;
    find('form').onsubmit = submit;
    find('evidence').onclick = event => {
      const button = event.target.closest('[data-recon-stop]');
      if (button) stopTask(button);
    };
  }

  function readiness() {
    find('submit').disabled = busy || settled || !issue?.can_resolve || !find('reviewed').checked || !find('notes').value.trim();
    find('refresh').disabled = busy || settled;
    find('flow').disabled = busy || !window.modelDeskFlow;
    find('notes').disabled = busy || settled;
    find('reviewed').disabled = busy || settled || !issue?.can_resolve;
    node.querySelectorAll('[data-recon-stop]').forEach(button => button.disabled = busy || settled);
  }

  function evidence(view) {
    const facts = view.facts;
    const tasks = facts.tasks.map(task => `<li class="recon-task"><div><strong>${esc(task.title || '关联任务')}</strong><p>${esc(taskNames[task.status] || '状态待核对')} · ${esc(processNames[task.process_state])}</p>${task.blocker ? `<p class="recon-blocker">${esc(task.blocker)}</p>` : ''}<small class="recon-id">${esc(task.task_id)}</small></div>${['queued','running','recovery_pending'].includes(task.status) ? `<button type="button" class="button secondary" data-recon-stop="${esc(task.task_id)}">停止此任务</button>` : ''}</li>`).join('');
    const phases = facts.phases.map(phase => `<li>${esc(phaseNames[phase.step] || phase.step)} · ${esc(phaseStates[phase.status] || '状态待核对')}</li>`).join('');
    const requests = facts.requests.map(request => `<li>${esc(requestNames[request.kind] || '原始请求')} · ${esc(request.state)}<small class="recon-id">${esc(request.request_id)}${request.task_id ? ' → ' + esc(request.task_id) : ''}</small></li>`).join('');
    return `<div class="recon-state"><strong>原主脑：${esc(processNames[facts.process_state])}</strong><p>当前目标：${esc(controlNames[facts.user_intent] || '控制状态待核对')}${facts.goal_status === 'completed' ? ' · 已完成' : ''} · 计划 v${facts.plan_revision}</p></div>${view.can_resolve ? '<p class="field-hint">关联进程已满足结束原轮次的条件，请继续核对文件并填写依据。</p>' : `<div class="recon-blocked"><strong>处理前还需核对</strong><ul>${view.blockers.map(item => `<li>${esc(item)}</li>`).join('')}</ul><p>停止任务后，请刷新证据；停止请求本身不等于进程已退出。</p></div>`}<section aria-label="关联任务"><h3>关联任务 <span class="count">${facts.tasks.length}</span></h3>${tasks ? `<ul class="recon-tasks">${tasks}</ul>` : '<p class="field-hint">原记录中没有关联执行任务。</p>'}</section><details><summary>原轮次与回执</summary><dl class="recon-identities"><dt>原轮次</dt><dd>${esc(facts.scope.operation_id)}</dd><dt>原持有者</dt><dd>${esc(facts.scope.owner_id)}</dd><dt>当前授权版本</dt><dd>${facts.authority_epoch}</dd></dl>${phases ? `<ul>${phases}</ul>` : '<p class="field-hint">没有可读取的图阶段回执。</p>'}${requests ? `<ul>${requests}</ul>` : '<p class="field-hint">没有关联动作请求。</p>'}</details><details><summary>阶段声明文件 <span class="count">${view.declared_files.length}${view.declared_files_omitted ? '＋' : ''}</span></summary><p class="field-hint">${esc(view.workspace_note)}</p>${view.declared_files.length ? `<ul class="recon-files">${view.declared_files.map(file => `<li>${esc(file)}</li>`).join('')}</ul>` : '<p class="field-hint">当前阶段没有声明文件，仍需核对实际工作目录。</p>'}${view.declared_files_omitted ? `<p class="field-hint">另有 ${view.declared_files_omitted} 个声明路径未在此展开，请核对项目阶段记录。</p>` : ''}</details>`;
  }

  async function refresh() {
    if (busy || settled || !node.open) return;
    const own = ++generation;
    busy = true; issue = null; readiness();
    find('evidence').setAttribute('aria-busy', 'true');
    find('status').textContent = '正在核对原进程与项目回执…';
    find('error').textContent = '';
    try {
      const next = await api(`/api/governance/goals/${encodeURIComponent(goal)}/recovery`);
      if (own !== generation || !node.open) return;
      const changed = find('evidence').dataset.subject && find('evidence').dataset.subject !== next.subject_hash;
      issue = next;
      find('evidence').innerHTML = evidence(next);
      find('evidence').dataset.subject = next.subject_hash;
      if (changed) find('reviewed').checked = false;
      find('status').textContent = changed ? '任务、授权或回执已变化。核对意见已保留，请重新确认当前证据。' : '已读取当前证据。打开此视图不会启动模型或更改目标。';
    } catch (error) {
      if (own === generation && !error.stale) {
        find('error').textContent = error.message;
        find('status').textContent = '未取得完整证据，暂不能结束原轮次。请刷新重试。';
      }
    } finally {
      if (own === generation) { busy = false; find('evidence').setAttribute('aria-busy', 'false'); readiness(); }
    }
  }

  async function stopTask(button) {
    if (busy || settled) return;
    const own = generation;
    busy = true; readiness(); find('error').textContent = '';
    find('status').textContent = '正在请求停止此任务，原结果继续保留…';
    try {
      await api(`/api/tasks/${encodeURIComponent(button.dataset.reconStop)}/cancel`, {});
      if (own !== generation || !node.open) return;
      busy = false; await refresh();
    } catch (error) {
      if (own === generation && !error.stale) find('error').textContent = error.message;
    } finally {
      if (own === generation) { busy = false; readiness(); }
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (find('submit').disabled || !issue) return;
    const own = generation;
    const body = {intent_id:issue.facts.scope.operation_id, authority_epoch:issue.facts.authority_epoch, subject_hash:issue.subject_hash, action:'retain_state', notes:find('notes').value.trim(), reviewed_workspace:true};
    busy = true; readiness(); find('error').textContent = '';
    find('status').textContent = '正在保存本次核对决定…';
    try {
      const result = await api(`/api/governance/goals/${encodeURIComponent(goal)}/reconcile`, body);
      if (boundProject === projectView.project) await onResolved?.();
      if (own !== generation || !node.open) return;
      settled = true; drafts.delete(goal); find('decision').hidden = true;
      find('evidence').innerHTML = `<div class="recon-state"><strong>核对决定已保存</strong><p>当前目标：${esc(controlNames[result.clock.user_intent] || '控制状态待核对')} · 原轮次已结束</p><p>当前工作保留，成果仍待独立验收。</p></div><details><summary>已保存的人工核对依据</summary><p>${esc(result.receipt.notes)}</p><dl class="recon-identities"><dt>原轮次</dt><dd>${esc(result.receipt.intent_id)}</dd><dt>核对时间</dt><dd>${esc(new Date(result.receipt.at).toLocaleString('zh-CN'))}</dd></dl></details>`;
      find('status').textContent = '原轮次已结束，当前工作与成果验收状态已保留。续跑仍遵循原来的运行、暂停或停止设置。';
      find('submit').hidden = true;
      node.querySelector('.dialog-footer [data-recon-close]').textContent = '完成核对';
    } catch (error) {
      if (own === generation && !error.stale) {
        find('error').textContent = error.message;
        busy = false; find('reviewed').checked = false;
        const refreshed = generation + 1; await refresh();
        if (generation === refreshed && node.open) find('error').textContent = error.message;
      }
    } finally {
      if (own === generation && node.open) { busy = false; readiness(); }
    }
  }

  function openFlow() {
    if (busy || !window.modelDeskFlow) return;
    suspended = true; node.close();
    const origin = boundProject;
    window.modelDeskFlow.open({recordId:issue?.facts.scope.operation_id, onClose:() => {
      suspended = false;
      if (origin !== projectView.project || projectView.mode !== 'project') return;
      node.showModal(); find('title').focus(); refresh();
    }});
  }

  function open(goalId, goalTitle, source, afterResolve) {
    if (!node) create();
    if (node.open || busy) return;
    goal = goalId; title = goalTitle; trigger = source; onResolved = afterResolve;
    boundProject = projectView.project; settled = false; suspended = false; issue = null;
    find('goal').textContent = title; find('notes').value = drafts.get(goal) || '';
    find('reviewed').checked = false; find('submit').hidden = false; find('decision').hidden = false;
    node.querySelector('.dialog-footer [data-recon-close]').textContent = '暂不处理';
    find('evidence').innerHTML = ''; delete find('evidence').dataset.subject;
    node.showModal(); find('title').focus(); refresh();
  }
  document.addEventListener('desk-project-changing', () => {
    ++generation; busy = false; suspended = false; drafts.clear(); issue = null; boundProject = '';
    if (node?.open) node.close();
  });
  window.modelDeskReconciliation = {open};
})();
