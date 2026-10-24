/* Credentials belong to reusable accounts; model and task forms hold references. */
(() => {
  const el = id => document.getElementById(id), esc = value => escapeText(String(value ?? ''));
  let accounts = [], selected = '';
  const native = provider => ['codex_cli', 'claude_cli', 'dsh'].includes(provider);
  function available() {
    const known = new Set(accounts.map(row => row.id));
    return [...accounts, ...state.models.filter(row => !native(row.provider) && row.provider !== 'workbuddy'
      && !row.account_id && !known.has('legacy-' + row.id)).map(row => ({...row, id: 'legacy-' + row.id,
        name: row.name + ' · 原有账户', model_count: 1, legacy: true}))];
  }
  function sync(preferred = '') {
    const select = el('model-account');
    if (!select) return;
    const chosen = preferred || select.value, provider = el('model-provider').value;
    const rows = available().filter(row => row.provider === provider);
    select.innerHTML = '<option value="">请选择账户</option>' + rows.map(row =>
      `<option value="${esc(row.id)}">${esc(row.name)}${row.enabled === false ? '（已停用）' : ''}</option>`).join('');
    select.value = rows.some(row => row.id === chosen) ? chosen : rows[0]?.id || '';
    select.required = !native(provider); el('model-account-field').hidden = native(provider);
    applySelection();
  }
  function applySelection() {
    const account = available().find(row => row.id === el('model-account').value);
    if (account) { el('model-base').value = account.api_base; el('model-bypass').checked = !!account.bypass_proxy; }
  }
  function render() {
    el('api-account-list').innerHTML = available().map(row => {
      const balance = row.balance || {}, values = balance.balance_infos || [];
      return `<div class="desk-library-row"><div><strong>${esc(row.name)}</strong><p>${esc(row.provider)} · ${esc(row.api_base)}</p><p>${row.api_key_set ? '已保存密钥' : '未配置密钥'} · ${row.model_count || 0} 个模型引用${row.legacy ? ' · 原有配置' : ''}</p>${values.length ? `<p>${values.map(item => esc(item.currency) + ' ' + esc(item.total_balance)).join(' / ')}</p>` : balance.message ? `<p>${esc(balance.message)}</p>` : ''}</div><button type="button" class="button secondary" data-api-account-edit="${esc(row.id)}">编辑</button>${row.api_base?.startsWith('https://api.deepseek.com') ? `<button type="button" class="button secondary" data-api-account-balance="${esc(row.id)}">读取余额</button>` : ''}<button type="button" class="button secondary" data-api-account-delete="${esc(row.id)}" ${row.model_count ? 'disabled title="仍有模型引用此账户"' : ''}>移除</button></div>`;
    }).join('') || '<p class="field-hint">还没有 API 账户。可先保存密钥，再添加或扫描模型。</p>';
  }
  function edit(id = '') {
    selected = id; const form = el('api-account-form'); form.reset();
    const row = available().find(account => account.id === id);
    for (const name of ['name', 'provider', 'api_base', 'api_key_env']) form.elements[name].value = row?.[name] || (name === 'provider' ? 'deepseek' : name === 'api_base' ? 'https://api.deepseek.com' : '');
    form.elements.enabled.checked = row?.enabled ?? true; form.elements.bypass_proxy.checked = row?.bypass_proxy ?? false;
    form.elements.provider.disabled = !!row;
    el('api-account-key-note').textContent = row?.api_key_set ? '已有密钥。留空保留；输入新值替换。密钥不会显示在模型中。' : '可只保存账户；模型稍后添加。自建服务无认证时密钥可留空。';
    el('api-account-error').textContent = '';
  }
  async function open() {
    const data = await api('/api/connection-accounts'); accounts = data.accounts || []; render(); edit();
    if (!el('api-accounts-dialog').open) el('api-accounts-dialog').showModal();
  }
  const dialog = document.createElement('dialog'); dialog.id = 'api-accounts-dialog';
  dialog.setAttribute('aria-labelledby', 'api-accounts-title');
  dialog.innerHTML = `<div class="desk-library-modal"><div class="dialog-heading"><h2 id="api-accounts-title">API 账户与密钥</h2><button type="button" class="icon-button close-dialog" aria-label="关闭">×</button></div><p class="field-hint">账户保存接口与密钥；模型引用账户；运行参数按项目保存。新增账户不需要选择模型，也不会调用模型。</p><div id="api-account-list"></div><button type="button" class="button secondary" id="api-account-new">新增账户</button><form id="api-account-form" class="inspector-settings"><h3>账户配置</h3><div class="form-grid"><label>账户名称<input name="name" maxlength="100" required></label><label>服务商<select name="provider"><option value="deepseek">DeepSeek 官方 API</option><option value="local">本地 / 自建</option><option value="glm">GLM</option><option value="custom">其他兼容服务</option></select></label></div><label>API Base URL<input name="api_base" type="url" required spellcheck="false"></label><label>API Key<input name="api_key" type="password" autocomplete="new-password" placeholder="保存在系统安全存储"></label><p id="api-account-key-note" class="field-hint"></p><label>密钥环境变量名（可选）<input name="api_key_env" spellcheck="false"></label><div class="checkboxes"><label><input name="enabled" type="checkbox" checked>启用账户</label><label><input name="bypass_proxy" type="checkbox">绕过代理</label><label><input name="clear_key" type="checkbox">清除已保存密钥</label></div><p id="api-account-error" class="form-error" role="alert"></p><div class="dialog-footer"><button type="button" class="button secondary" id="api-account-close">关闭</button><button type="submit" class="button primary">保存账户</button></div></form></div>`;
  document.body.append(dialog);
  dialog.querySelector('.close-dialog').onclick = () => dialog.close();
  el('api-account-close').onclick = () => dialog.close(); el('api-account-new').onclick = () => edit();
  el('api-account-form').elements.provider.onchange = event => { const bases = {deepseek: 'https://api.deepseek.com', local: 'http://localhost:1234/v1', glm: 'https://open.bigmodel.cn/api/paas/v4', custom: ''}; el('api-account-form').elements.api_base.value = bases[event.target.value]; };
  el('api-account-form').onsubmit = async event => {
    event.preventDefault(); const button = event.submitter, form = event.target; button.disabled = true;
    try {
      const body = Object.fromEntries(['name', 'provider', 'api_base', 'api_key', 'api_key_env'].map(name => [name, form.elements[name].value]));
      for (const name of ['enabled', 'bypass_proxy', 'clear_key']) body[name] = form.elements[name].checked;
      if (selected) body.id = selected;
      const saved = await api('/api/connection-accounts', body);
      accounts = (await api('/api/connection-accounts')).accounts; render(); edit(saved.id); form.elements.api_key.value = '';
      if (el('model-dialog').open) { el('model-provider').value = saved.provider; sync(saved.id); }
      await refresh(); toast('账户已保存；可以随后添加模型');
    } catch (error) { el('api-account-error').textContent = error.message; }
    finally { button.disabled = false; }
  };
  dialog.onclick = async event => {
    const button = event.target.closest('[data-api-account-edit],[data-api-account-balance],[data-api-account-delete]'); if (!button) return;
    if (button.dataset.apiAccountEdit) { edit(button.dataset.apiAccountEdit); return; }
    button.disabled = true;
    try {
      if (button.dataset.apiAccountDelete) await api('/api/connection-accounts/delete', {id: button.dataset.apiAccountDelete});
      else await api('/api/connection-accounts/balance', {id: button.dataset.apiAccountBalance});
      accounts = (await api('/api/connection-accounts')).accounts; render(); sync();
    } catch (error) { el('api-account-error').textContent = error.message; }
    finally { if (button.isConnected) button.disabled = false; }
  };
  document.addEventListener('click', event => { if (event.target.closest('[data-open-api-accounts]')) open().catch(error => toast(error.message, true)); });
  document.addEventListener('change', event => { if (event.target.id === 'model-account') applySelection(); });
  window.modelDeskAccounts = {open, sync, set: rows => { accounts = rows || []; }, available};
})();
