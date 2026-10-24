/* Human preferences guide decomposition; runtime gates continue to own authority. */
(() => {
  const esc = value => escapeText(String(value ?? ''));
  function markup(policy = {}) {
    return `<form id="desk-collaboration-form" class="inspector-settings"><h2>多 agent 分工策略 <span class="scope-tag">此安装的所有项目</span></h2><p class="field-hint">主脑根据目标拆分并选择模型；下面的提示会进入真实规划上下文。默认执行模型只是候选缺省值。同一模型可启动多个独立实例，无依赖且文件不冲突的任务可以并行。</p><label>总分工提示<textarea name="guidance" rows="3" maxlength="12000">${esc(policy.guidance || '')}</textarea></label><label>Claude 分工提示<textarea name="claude_hint" rows="2" maxlength="6000">${esc(policy.claude_hint || '')}</textarea></label><label>DSH 分工提示<textarea name="dsh_hint" rows="2" maxlength="6000">${esc(policy.dsh_hint || '')}</textarea></label><label>Codex 分工与验收提示<textarea name="codex_hint" rows="2" maxlength="6000">${esc(policy.codex_hint || '')}</textarea></label><div class="form-grid"><label>同一账户的执行实例上限<input name="account_concurrency" type="number" min="1" max="64" step="1" value="${policy.account_concurrency || 8}" required></label><label>工程习惯的加载方式<select name="skill_mode"><option value="native" ${(!policy.skill_mode || policy.skill_mode === 'native') ? 'selected' : ''}>优先使用原生 TCAUTO skill</option><option value="inline" ${policy.skill_mode === 'inline' ? 'selected' : ''}>内嵌已选择的工程习惯</option><option value="off" ${policy.skill_mode === 'off' ? 'selected' : ''}>不附加技能提示</option></select></label></div><p class="field-hint">账户上限约束跨项目执行任务；主脑轮次仍按每目标唯一入口管理，项目与模型执行实例上限分别设置。原生模式提示 CLI 读取自身已安装的 tcauto，不重复嵌入整套模板，不另建续接时钟。尚未加载技能时须如实记录；技能不授予费用或执行权限。</p><p id="desk-collaboration-error" class="form-error" role="alert"></p><div class="inspector-save-row"><span id="desk-collaboration-state">保存后供新任务与新主脑轮次使用。</span><button type="submit" class="button primary">保存协作策略</button></div></form>`;
  }
  document.addEventListener('input', event => {
    if (event.target.closest('#desk-collaboration-form')) {
      document.getElementById('desk-collaboration-state').textContent = '有未保存的修改';
    }
  });
  document.addEventListener('submit', async event => {
    if (event.target.id !== 'desk-collaboration-form') return;
    event.preventDefault(); const form = event.target, button = event.submitter; button.disabled = true;
    try {
      const body = Object.fromEntries(['guidance', 'codex_hint', 'claude_hint', 'dsh_hint', 'skill_mode'].map(name => [name, form.elements[name].value]));
      body.account_concurrency = Number(form.elements.account_concurrency.value);
      const saved = await api('/api/collaboration-policy', body);
      document.dispatchEvent(new CustomEvent('desk-collaboration-saved', {detail: saved}));
      document.getElementById('desk-collaboration-state').textContent = '已保存 · 用于新任务和主脑轮次';
      toast('分工与并行策略已保存');
    } catch (error) { document.getElementById('desk-collaboration-error').textContent = error.message; }
    finally { button.disabled = false; }
  });
  window.modelDeskCollaboration = {markup};
})();
