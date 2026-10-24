'use strict';

// Explicit metadata probe only. Never creates an agent, a session or a prompt.
function apply(ctx, config) {
  const timer = setTimeout(async () => {
    const result = {models: [], account: {status: 'unavailable', wallets: []}};
    for (const provider of ctx.llm.listProviders()) {
      try {
        for (const model of await ctx.llm.listModels(provider.id)) {
          const resolved = await ctx.llm.resolveModelInfo(provider.id, model.id);
          result.models.push({provider: provider.id, id: model.id, name: model.name || model.id,
            context_window: resolved.context?.contextWindow, max_output_tokens: resolved.defaultMaxTokens,
            effort_levels: resolved.reasoning?.efforts?.map(effort => effort.id) || [],
            default_effort: resolved.reasoning?.defaultEffort || ''});
        }
      } catch { /* One unavailable provider must not hide the other routes. */ }
    }
    if (config.balance) {
      const account = ctx.get('accountController');
      try {
        const state = await account?.getState();
        if (state?.status === 'signed-out') result.account.status = 'signed-out';
        else if (state?.status === 'credential-stored') {
          const balance = await account.getBalance({version: config.version,
            locale: 'zh-CN', timezoneOffsetSeconds: -new Date().getTimezoneOffset() * 60});
          result.account.status = balance?.status === 'ready' ? 'ready' : 'error';
          if (balance?.status === 'ready') {
            for (const [kind, rows] of [['normal', balance.value], ['bonus', balance.bonusWallets || []]]) {
              for (const row of rows || []) {
                if (['CNY', 'USD'].includes(row.currency) && typeof row.balance === 'string'
                    && /^-?(?:\d+(?:\.\d*)?|\.\d+)$/.test(row.balance)) {
                  result.account.wallets.push({kind, currency: row.currency, balance: row.balance});
                }
              }
            }
          }
        }
      } catch { result.account = {status: 'error', wallets: []}; }
    }
    process.stdout.write('MODEL_DESK_METADATA:' + JSON.stringify(result) + '\n');
  }, 150);
  ctx.effect(() => () => clearTimeout(timer), 'metadata timer');
}
apply.inject = ['llm', 'accountController'];
module.exports = apply;
