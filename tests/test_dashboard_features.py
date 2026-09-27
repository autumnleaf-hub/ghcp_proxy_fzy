"""Static/offline dashboard checks. Run with the repo-local Python and Node.js.

No server is started, no user configuration is read, and every fetch is mocked.
"""
from html.parser import HTMLParser
from pathlib import Path
import json
import re
import shutil
import subprocess
import tempfile
import unittest

DASHBOARD = Path(__file__).resolve().parents[1] / 'dashboard.html'


class TemplateParser(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []
        self.expressions = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)
        for name, value in attrs:
            if not value:
                continue
            if name == 'v-for':
                match = re.match(r'(.+?)\s+(?:in|of)\s+(.+)', value)
                if match:
                    self.expressions.append(['expression', match[2]])
            elif name.startswith('@'):
                self.expressions.append(['statement', value])
            elif name.startswith(':') or name in {'v-if', 'v-else-if', 'v-show', 'v-model', 'v-model.number', 'v-model.trim'}:
                self.expressions.append(['expression', value])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append((self.getpos(), tag, self.stack[-3:]))
        else:
            self.stack.pop()

    def handle_data(self, data):
        if 'script' not in self.stack and 'style' not in self.stack:
            for expression in re.findall(r'{{(.*?)}}', data, re.S):
                self.expressions.append(['expression', expression.strip()])


OFFLINE_JS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'));
new vm.Script(script, { filename: 'dashboard-inline.js' });
let app, queue = [], requests = [], confirmDelete = false;
const sandbox = {
  console, URL,
  document: { getElementById: () => null },
  localStorage: { getItem: () => null },
  window: {
    location: { hash: '', pathname: '/', search: '' },
    setTimeout: () => 1, clearTimeout: () => {},
    confirm: () => confirmDelete,
    open: () => ({ location: {}, close() {} }),
  },
  Vue: { createApp(options) { app = options; return { mount() {} }; } },
};
sandbox.fetch = async (url, options = {}) => {
  requests.push({ url, ...options });
  assert.ok(queue.length, 'Unexpected request: ' + url);
  const next = queue.shift();
  assert.equal(url, next.url);
  if (next.method) assert.equal(options.method || 'GET', next.method);
  if (next.body) assert.deepEqual(JSON.parse(options.body), next.body);
  return { ok: next.status < 400, status: next.status, json: async () => {
    if (next.status === 204 || next.malformed) throw Error('No JSON');
    return next.payload;
  } };
};
vm.runInNewContext(script, sandbox, { filename: 'dashboard-inline.js' });
const response = (url, payload, extras = {}) => queue.push({ url, payload, status: 200, ...extras });
const state = () => {
  assert.equal(queue.length, 0, 'Unused mock responses');
  requests = [];
  const model = app.data();
  for (const [name, method] of Object.entries(app.methods)) model[name] = method.bind(model);
  for (const [name, getter] of Object.entries(app.computed)) {
    if (typeof getter === 'function') Object.defineProperty(model, name, { get: () => getter.call(model) });
  }
  return model;
};
const credential = (overrides = {}) => ({ id: 'acct /one', label: '主账号', enabled: true, source: 'oauth', expires_at: 4102444800, bps_verified: false, status: 'ready', error: '', ...overrides });
const list = (rows) => ({ credentials: rows, strategy: 'sticky_round_robin' });
const loadAccount = async (model, row = credential()) => {
  response('/api/credentials', list([row]));
  await model.loadCredentials();
  return model.credentialManager.credentials[0];
};
let count = 0;
function assertJsonPost(request, body) {
  assert.ok(request, 'Expected a captured fetch request');
  assert.equal(request.method, 'POST', request.url + ': method');
  assert.equal(request.headers?.['Content-Type'], 'application/json', request.url + ': Content-Type');
  assert.equal(typeof request.body, 'string', request.url + ': serialized JSON body');
  assert.deepEqual(JSON.parse(request.body), body, request.url + ': body');
}
async function check(name, run) {
  await run();
  // Assert outside dashboard try/catch: mocked fetch assertions must not be
  // swallowed as ordinary API errors by the UI under test.
  for (const request of requests) {
    if (/^\/api\/credentials\/[^/]+\/test$/.test(request.url) ||
        /^\/api\/config\/excel-oauth\/(start|cancel|test)$/.test(request.url)) {
      assertJsonPost(request, {});
      assert.equal(request.body, '{}', request.url + ': explicit empty object');
    } else if (request.method === 'POST') {
      assert.equal(request.headers?.['Content-Type'], 'application/json', request.url + ': Content-Type');
      assert.equal(typeof request.body, 'string', request.url + ': serialized JSON body');
      assert.ok(JSON.parse(request.body), request.url + ': JSON payload');
    }
  }
  assert.equal(queue.length, 0, name + ': unused mocks'); count++; console.log('PASS ' + name);
}
(async () => {
  await check('defaults and preserved onboarding methods', async () => {
    const s = state(); assert.equal(s.outboundProxy.enabled, false); assert.equal(s.outboundProxy.url, 'http://127.0.0.1:7890');
    for (const name of ['startExcelOAuth', 'loadExcelSessionStatus', 'clearExcelSession', 'testExcelOAuth']) assert.equal(typeof s[name], 'function');
  });
  await check('metadata-only loading preserves literal labels', async () => {
    const s = state(); const a = await loadAccount(s, credential({ label: '<img src=x onerror=alert(1)>', private_field: 'not exposed' }));
    assert.equal(a.label, '<img src=x onerror=alert(1)>'); assert.equal(a.private_field, undefined);
    a.labelDraft = '未保存标签'; await loadAccount(s); assert.equal(a.labelDraft, '未保存标签');
  });
  await check('label save uses exact API fields and escaped id', async () => {
    const s = state(); const a = await loadAccount(s); a.labelDraft = ' 新标签 ';
    response('/api/credentials/acct%20%2Fone', {}, { method: 'POST', body: { label: '新标签', enabled: true } });
    response('/api/credentials', list([credential({ label: '新标签' })])); await s.saveCredentialLabel(a);
    assert.equal(a.label, '新标签'); assert.equal(a.labelDraft, '新标签'); assert.equal(s.credentialManager.busy, false);
  });
  await check('empty labels never submit', async () => {
    const s = state(); const a = await loadAccount(s); const before = requests.length; a.labelDraft = ' '; await s.saveCredentialLabel(a);
    assert.equal(requests.length, before); assert.match(a.actionError, /标签/);
  });
  await check('toggle leaves unsaved label draft intact', async () => {
    const s = state(); const a = await loadAccount(s); a.labelDraft = '稍后保存';
    response('/api/credentials/acct%20%2Fone', {}, { method: 'POST', body: { label: '主账号', enabled: false } });
    response('/api/credentials', list([credential({ enabled: false })])); await s.toggleCredentialEnabled(a, { target: { checked: false } });
    assert.equal(a.enabled, false); assert.equal(a.labelDraft, '稍后保存');
  });
  await check('toggle failure rolls back and supports error objects', async () => {
    const s = state(); const a = await loadAccount(s); const event = { target: { checked: false } };
    response('/api/credentials/acct%20%2Fone', { error: { message: '服务不可用' } }, { status: 503 });
    await s.toggleCredentialEnabled(a, event); assert.equal(a.enabled, true); assert.equal(event.target.checked, true); assert.match(a.actionError, /服务不可用/);
  });
  await check('per-account validation handles pass and failed probes', async () => {
    for (const ok of [true, false]) {
      const s = state(); const a = await loadAccount(s);
      response('/api/credentials/acct%20%2Fone/test', { ok, status: ok ? 'verified' : 'failed', message: ok ? '通过' : '额度不足' }, { method: 'POST' });
      response('/api/credentials', list([credential({ bps_verified: ok })])); await s.testCredential(a);
      assertJsonPost(requests[1], {}); assert.equal(requests[1].body, '{}');
      assert.equal(a.probeResult.ok, ok); assert.equal(a.bps_verified, ok); assert.equal(a.pending, '');
    }
  });
  await check('credential JSON guard failure stays visible and releases busy state', async () => {
    const s = state(); const a = await loadAccount(s);
    response('/api/credentials/acct%20%2Fone/test', { detail: 'Use application/json.' }, { status: 415, method: 'POST', body: {} });
    await s.testCredential(a);
    assert.equal(a.actionError, '操作失败：Use application/json.');
    assert.equal(a.probeResult, null); assert.equal(a.pending, '');
    assert.equal(s.credentialManager.busy, false); assert.equal(requests.length, 2);
    assertJsonPost(requests[1], {});
  });
  for (const [action, method] of [['start', 'startCredentialLogin'], ['cancel', 'cancelCredentialLogin'], ['test', 'testExcelOAuth']]) {
    await check('OAuth ' + action + ' sends exact JSON fetch contract', async () => {
      const s = state();
      response('/api/config/excel-oauth/' + action, { status: 'waiting', authorization_url: 'https://example.invalid/offline', ok: true, message: 'offline probe' }, { method: 'POST', body: {} });
      if (action !== 'start') response('/api/config/excel-session', { configured: false, oauth: { status: 'idle' } });
      await s[method]();
      assertJsonPost(requests[0], {}); assert.equal(requests[0].body, '{}');
      assert.equal(s.excelOAuthError, ''); assert.equal(s.credentialManager.loginError, '');
      assert.equal(requests.length, action === 'start' ? 1 : 2);
    });
  }
  await check('FastAPI detail arrays and nested errors', async () => {
    const s = state(); const a = await loadAccount(s);
    response('/api/credentials/acct%20%2Fone/test', { detail: [{ msg: '缺少有效凭证' }] }, { status: 422 });
    await s.testCredential(a); assert.match(a.actionError, /缺少有效凭证/);
    assert.equal(s.dashboardErrorMessage({ error: { message: '嵌套错误' } }), '嵌套错误');
  });
  await check('deletion confirms and handles empty 204', async () => {
    const s = state(); const a = await loadAccount(s); const before = requests.length; confirmDelete = false;
    await s.deleteCredential(a); assert.equal(requests.length, before); confirmDelete = true;
    response('/api/credentials/acct%20%2Fone', null, { method: 'DELETE', status: 204 }); response('/api/credentials', list([]));
    await s.deleteCredential(a); assert.equal(s.credentialManager.credentials.length, 0);
  });
  await check('list errors retain the previously loaded rows', async () => {
    const s = state(); await loadAccount(s); response('/api/credentials', { detail: { message: '暂时无法读取' } }, { status: 503 });
    await s.loadCredentials(); assert.equal(s.credentialManager.credentials.length, 1); assert.match(s.credentialManager.error, /暂时无法读取/); assert.equal(s.credentialManager.loading, false);
  });
  await check('proxy config loads and saves only enabled/url', async () => {
    const s = state(); response('/api/config/outbound-proxy', { enabled: false, url: 'http://127.0.0.1:7890' }); await s.loadOutboundProxy();
    s.outboundProxy.enabled = true; s.outboundProxy.url = 'http://127.0.0.1:9999';
    response('/api/config/outbound-proxy', { enabled: true, url: s.outboundProxy.url }, { method: 'POST', body: { enabled: true, url: s.outboundProxy.url } });
    await s.saveOutboundProxy(); assert.equal(s.outboundProxy.enabled, true); assert.match(s.outboundProxy.statusMessage, /已保存/);
  });
  await check('proxy missing config and bad URLs cannot submit', async () => {
    const s = state(); await s.saveOutboundProxy(); assert.equal(requests.length, 0);
    response('/api/config/outbound-proxy', { enabled: false }); await s.loadOutboundProxy(); assert.equal(s.outboundProxy.loaded, false);
    s.outboundProxy.loaded = true; s.outboundProxy.enabled = true; s.outboundProxy.url = 'invalid address'; await s.saveOutboundProxy(); assert.match(s.outboundProxy.error, /代理地址/);
  });
  await check('proxy save errors remain visible without fallback', async () => {
    const s = state(); s.outboundProxy.loaded = true; s.outboundProxy.enabled = true;
    response('/api/config/outbound-proxy', { detail: '代理连接失败' }, { status: 502 }); await s.saveOutboundProxy();
    assert.match(s.outboundProxy.error, /代理连接失败/); assert.equal(s.outboundProxy.enabled, true); assert.equal(s.outboundProxy.saving, false); assert.equal(requests.length, 1);
  });
  await check('routing grouped aliases round trip and warnings render state', async () => {
    const s = state(); const payload = { enabled: true, mappings: [{ source_model: 'alias-a, alias-b', target_model: 'bps-test-excel' }], available_models: [{ provider: 'bps', model: 'bps-test-excel' }], warnings: ['旧规则已排除'] };
    response('/api/config/model-remapping', payload); await s.loadModelRoutingConfig(); assert.equal(s.modelRouting.mappings[0].source_model, 'alias-a, alias-b'); assert.equal(s.modelRouting.warnings[0], '旧规则已排除');
    response('/api/config/model-remapping', payload, { method: 'POST' }); await s.saveModelRoutingConfig();
    const sent = JSON.parse(requests[requests.length - 1].body); assert.equal(sent.mappings[0].source_model, 'alias-a, alias-b'); assert.equal(sent.mappings[0].target_model, 'bps-test-excel');
    s.addModelRoutingMapping(); assert.equal(s.modelRouting.mappings[1].source_model, ''); assert.equal(s.modelRouting.mappings[1].target_model, 'bps-test-excel');
  });
  await check('routing rejects blank aliases and unsupported targets', async () => {
    const s = state(); s.modelRouting.available_models = [{ model: 'bps-test-excel' }];
    s.modelRouting.mappings = [{ source_model: 'alias,', target_model: 'bps-test-excel' }]; await s.saveModelRoutingConfig(); assert.equal(requests.length, 0); assert.match(s.modelRouting.error, /别名/);
    s.modelRouting.mappings[0] = { source_model: 'alias', target_model: 'unsupported' }; await s.saveModelRoutingConfig(); assert.equal(requests.length, 0); assert.match(s.modelRouting.error, /BPS/);
  });
  await check('existing OAuth path refreshes credentials on completion', async () => {
    const s = state(); response('/api/config/excel-oauth/start', { status: 'waiting', authorization_url: 'https://example.invalid/mock-login' }, { method: 'POST' });
    await s.startCredentialLogin(); assert.equal(s.credentialManager.loginActive, true);
    response('/api/config/excel-session', { configured: true, configured_at: 42, source: 'oauth', oauth: { status: 'complete' } }); response('/api/credentials', list([credential()]));
    await s.loadExcelSessionStatus(); assert.equal(s.credentialManager.loginActive, false); assert.equal(s.credentialManager.credentials.length, 1); assert.match(s.credentialManager.loginMessage, /登录完成/);
  });
  await check('old single-session errors do not leak into manager', async () => {
    const s = state(); s.excelSessionStatus.oauth = { status: 'error', error: '旧会话错误' };
    await s.updateCredentialLoginStatus('error', false); assert.equal(s.credentialManager.loginError, ''); assert.equal(requests.length, 0);
  });
  await check('disabled expiry and cooldown presentation', async () => {
    const s = state(); assert.equal(s.credentialStatusLabel(credential({ enabled: false })), '已停用');
    assert.equal(s.credentialStatusLabel(credential({ expires_at: 1 })), '已过期');
    assert.equal(s.credentialStatusLabel(credential({ cooldown_until: 4102444800 })), '冷却中');
    assert.equal(s.credentialTimestamp('2030-01-01T00:00:00Z'), Date.parse('2030-01-01T00:00:00Z'));
  });
  await check('obsolete login status fetch cannot replace a new login', async () => {
    const s = state(); const originalFetch = sandbox.fetch;
    let release;
    sandbox.fetch = () => new Promise(resolve => { release = resolve; });
    const pending = s.loadExcelSessionStatus();
    s.excelOAuthRequestVersion++;
    s.excelSessionStatus.oauth = { status: 'waiting' };
    s.credentialManager.loginActive = true;
    release({ ok: true, json: async () => ({ oauth: { status: 'complete' } }) });
    await pending; sandbox.fetch = originalFetch;
    assert.equal(s.excelSessionStatus.oauth.status, 'waiting'); assert.equal(s.credentialManager.loginActive, true);
  });
  await check('newest credential refresh wins without erasing drafts', async () => {
    const s = state(); const originalFetch = sandbox.fetch;
    const releases = [];
    sandbox.fetch = () => new Promise(resolve => releases.push(resolve));
    const older = s.loadCredentials(); const newer = s.loadCredentials();
    releases[1]({ ok: true, json: async () => list([credential({ label: '新结果' })]) }); await newer;
    releases[0]({ ok: true, json: async () => list([credential({ label: '旧结果' })]) }); await older;
    sandbox.fetch = originalFetch; assert.equal(s.credentialManager.credentials[0].label, '新结果'); assert.equal(s.credentialManager.loading, false);
  });

  const builtinPayload = (rows, enabled = false) => ({ enabled, mappings: [], builtin_mappings: rows, available_models: [{provider:'bps', model:'bps-test-excel'}], approval_enabled:false, approval_mappings:[] });
  const builtinRow = (enabled = true) => ({source_model:'builtin-a, builtin-b',target_model:'bps-test-excel',enabled});
  await check('builtin list is independent of disabled custom routing', async () => {
    const s=state(); response('/api/config/model-remapping',builtinPayload([builtinRow()])); await s.loadModelRoutingConfig();
    assert.equal(s.modelRouting.enabled,false); assert.equal(s.modelRouting.builtinMappingsSupported,true); assert.equal(s.modelRouting.builtin_mappings.length,1);
    assert.equal(s.modelRouting.builtin_mappings[0].enabled,true);
    s.addBuiltinModelRoutingMapping(); assert.equal(s.modelRouting.builtin_mappings.length,2); assert.equal(s.modelRouting.builtin_mappings[1].enabled,true);
    s.removeBuiltinModelRoutingMapping(1); assert.equal(s.modelRouting.builtin_mappings.length,1);
    assert.match(html, /@click="addBuiltinModelRoutingMapping"/); assert.match(html, /@click="removeBuiltinModelRoutingMapping\(index\)"/);
  });
  await check('disabled builtin row saves explicit false', async () => {
    const s=state(); response('/api/config/model-remapping',builtinPayload([builtinRow()])); await s.loadModelRoutingConfig();
    s.modelRouting.builtin_mappings[0].enabled=false; response('/api/config/model-remapping',builtinPayload([builtinRow(false)]),{method:'POST'}); await s.saveModelRoutingConfig();
    const body=JSON.parse(requests.at(-1).body); assert.equal(body.enabled,false); assert.deepEqual(body.builtin_mappings,[builtinRow(false)]); assert.equal(s.modelRouting.builtin_mappings[0].enabled,false); assert.equal(s.modelRouting.error,'');
  });
  await check('explicit empty builtin list survives save and reload without resurrection', async () => {
    const s=state(); response('/api/config/model-remapping',builtinPayload([builtinRow()])); await s.loadModelRoutingConfig(); s.removeBuiltinModelRoutingMapping(0);
    response('/api/config/model-remapping',builtinPayload([]),{method:'POST'}); await s.saveModelRoutingConfig();
    assert.deepEqual(JSON.parse(requests.at(-1).body).builtin_mappings,[]);
    response('/api/config/model-remapping',builtinPayload([])); await s.loadModelRoutingConfig();
    assert.equal(s.modelRouting.builtinMappingsSupported,true); assert.equal(s.modelRouting.builtin_mappings.length,0); assert.equal(s.modelRouting.error,'');
  });
  await check('unsupported builtin API is not mistaken for explicit empty list', async () => {
    const s=state(); const payload=builtinPayload([]); delete payload.builtin_mappings;
    response('/api/config/model-remapping',payload); await s.loadModelRoutingConfig(); assert.equal(s.modelRouting.builtinMappingsSupported,false);
    s.addBuiltinModelRoutingMapping(); assert.equal(s.modelRouting.builtin_mappings.length,0);
    response('/api/config/model-remapping',payload,{method:'POST'}); await s.saveModelRoutingConfig();
    assert.equal(Object.hasOwn(JSON.parse(requests.at(-1).body),'builtin_mappings'),false);
  });
  await check('email remains public literal metadata and primary text interpolation', async () => {
    const s=state(); const attack='<img src=x onerror=globalThis.__emailXss=1>@example.invalid';
    const a=await loadAccount(s,credential({email:attack,label:'Secondary label',private_field:'DO_NOT_EXPOSE'}));
    assert.equal(a.email,attack); assert.equal(a.label,'Secondary label'); assert.equal(a.private_field,undefined);
    assert.match(html, /<strong[^>]*class="credential-email"[^>]*>\s*\{\{ account\.email \|\| '邮箱不可用' \}\}\s*<\/strong>/);
    const integrations=html.split(`<div v-show="activeView === 'integrations'"`)[1];
    assert.ok(integrations); assert.equal(integrations.split(`<div v-show="activeView === 'settings'"`)[0].includes('v-html'),false);
    await loadAccount(s,credential({email:null})); assert.equal(a.email,'');
  });
  await check('auto update set_enabled sends boolean and applies server success', async () => {
    for(const enabled of [true,false]) { const s=state(); s.autoUpdateStatus.enabled=!enabled; s.autoUpdateStatus.enabled_source='settings';
      response('/api/config/auto-update',{enabled,enabled_source:'settings'},{method:'POST',body:{action:'set_enabled',enabled}});
      const result=await s.setAutoUpdateEnabled(enabled); assert.equal(result.enabled,enabled); assert.equal(s.autoUpdateStatus.enabled,enabled); assert.equal(s.savingAutoUpdate,false); assert.equal(s.autoUpdateAction,''); assert.equal(s.autoUpdateStatus.error,''); assert.ok(s.autoUpdateStatus.statusMessage);
    }
  });
  await check('auto update failure preserves previous enabled state both directions', async () => {
    for(const enabled of [true,false]) { const s=state(); s.autoUpdateStatus.enabled=enabled; s.autoUpdateStatus.enabled_source='settings';
      response('/api/config/auto-update',{detail:'synthetic save refused'},{method:'POST',status:503,body:{action:'set_enabled',enabled:!enabled}});
      assert.equal(await s.setAutoUpdateEnabled(!enabled),null); assert.equal(s.autoUpdateStatus.enabled,enabled); assert.match(s.autoUpdateStatus.error,/synthetic save refused/); assert.equal(s.savingAutoUpdate,false); assert.equal(s.autoUpdateAction,''); assert.equal(s.autoUpdateStatus.statusMessage,'');
    }
  });
  await check('auto update transaction is nonoptimistic and prevents duplicate saves', async () => {
    const s=state(); s.autoUpdateStatus.enabled=false; s.autoUpdateStatus.enabled_source='settings'; const original=sandbox.fetch; let release, calls=0;
    sandbox.fetch=async(url,options)=>{calls++; assert.equal(url,'/api/config/auto-update'); assert.deepEqual(JSON.parse(options.body),{action:'set_enabled',enabled:true}); return new Promise(resolve=>{release=resolve;});};
    try { const pending=s.setAutoUpdateEnabled(true); assert.equal(s.autoUpdateStatus.enabled,false); assert.equal(s.savingAutoUpdate,true); assert.equal(s.autoUpdateAction,'set_enabled');
      assert.equal(await s.setAutoUpdateEnabled(true),null); assert.equal(calls,1); release({ok:true,status:200,json:async()=>({enabled:true,enabled_source:'settings'})}); await pending;
      assert.equal(s.autoUpdateStatus.enabled,true); assert.equal(s.savingAutoUpdate,false);
    } finally { sandbox.fetch=original; }
  });
  await check('auto update env lock blocks both transitions without fetch', async () => {
    for(const enabled of [true,false]) { const s=state(); s.autoUpdateStatus.enabled=enabled; s.autoUpdateStatus.enabled_source='env';
      assert.equal(await s.setAutoUpdateEnabled(!enabled),null); assert.equal(s.autoUpdateStatus.enabled,enabled); assert.equal(requests.length,0); assert.equal(s.savingAutoUpdate,false);
    }
    assert.match(html, /@click="setAutoUpdateEnabled\(!autoUpdateStatus\.enabled\)" :disabled="savingAutoUpdate \|\| autoUpdateStatus\.enabled_source === 'env'"/);
  });
  await check('builtin save missing acknowledgment retains edits and reports error', async () => {
    const s=state(); response('/api/config/model-remapping',builtinPayload([builtinRow(false)])); await s.loadModelRoutingConfig();
    const bad=builtinPayload([]); delete bad.builtin_mappings; response('/api/config/model-remapping',bad,{method:'POST'}); await s.saveModelRoutingConfig();
    assert.match(s.modelRouting.error,/未确认内置别名保存结果/); assert.equal(s.modelRouting.builtinMappingsSupported,true); assert.equal(s.modelRouting.builtin_mappings[0].enabled,false);
  });
  console.log('Offline scenarios passed: ' + count);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


class DashboardFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding='utf-8')
        cls.parser = TemplateParser()
        cls.parser.feed(cls.html)
        cls.clients = cls.html.split('<div v-show="activeView === \'integrations\'"', 1)[1].split('<div v-show="activeView === \'settings\'"', 1)[0]
        cls.routing = cls.html.split('<h3>模型映射</h3>', 1)[1].split('<h3>审批代理模型映射</h3>', 1)[0]

    def test_html_nesting(self):
        self.assertEqual(self.parser.errors, [])
        self.assertEqual(self.parser.stack, [])

    def test_credential_page_is_metadata_only(self):
        for obsolete in ('toggleClientProxy', 'enableAllClientProxy', 'disableAllClientProxy', 'excelSessionStatusLabel', 'clearExcelSession', 'testExcelOAuth', 'v-html'):
            self.assertNotIn(obsolete, self.clients)
        for required in ('会话粘性轮询', '尽量', '最多尝试 3 次', '已开始的流不会重放', '重新上传文件', 'account.labelDraft', 'account.error', 'account.probeResult', 'deleteCredential'):
            self.assertIn(required, self.clients)
        self.assertNotIn('固定使用', self.clients)

    def test_routing_inputs_and_warning_text(self):
        self.assertIn('<input type="text" v-model="mapping.source_model"', self.routing)
        self.assertNotIn('<select v-model="mapping.source_model"', self.routing)
        self.assertNotIn('Claude Code 默认模型槽位', self.routing)
        self.assertIn('modelRouting.available_models', self.routing)
        self.assertIn('{{ warning }}', self.routing)
        self.assertNotIn('v-html', self.routing)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for JavaScript checks')
    def test_vue_expression_syntax(self):
        script = "const fs=require('node:fs'); for (const [kind,value] of JSON.parse(fs.readFileSync(0,'utf8'))) { try { new Function('$event', kind === 'statement' ? value : 'return (' + value + ')'); } catch(e) { throw Error(value + ': ' + e.message); } }"
        result = subprocess.run([shutil.which('node'), '-e', script], input=json.dumps(self.parser.expressions), text=True, capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for JavaScript checks')
    def test_json_fetch_contract_rejects_regressions(self):
        # Mutate temporary HTML only; all fetch calls stay in the JS sandbox.
        contract = "method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'"
        methods = ('runCredentialAction', 'startExcelOAuth', 'cancelExcelOAuth', 'testExcelOAuth')
        mutations = {
            'method': contract.replace("method: 'POST'", "method: 'GET'"),
            'headers': contract.replace("headers: { 'Content-Type': 'application/json' }", 'headers: {}'),
            'body': contract.replace(", body: '{}'", ''),
        }
        with tempfile.TemporaryDirectory(prefix='dashboard-json-contract-') as directory:
            fixture = Path(directory) / 'dashboard.html'
            for method in methods:
                start = self.html.index('        async ' + method + '(')
                end = self.html.index(chr(10) + '        async ', start + 1)
                block = self.html[start:end]
                self.assertEqual(block.count(contract), 1, method)
                for field, broken in mutations.items():
                    with self.subTest(method=method, field=field):
                        fixture.write_text(self.html[:start] + block.replace(contract, broken) + self.html[end:], encoding='utf-8')
                        result = subprocess.run([shutil.which('node'), '-', str(fixture)], input=OFFLINE_JS, text=True, capture_output=True, encoding='utf-8', timeout=30)
                        self.assertNotEqual(result.returncode, 0, 'Regression escaped: ' + method + '/' + field)
                        self.assertNotIn('Offline scenarios passed:', result.stdout)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for JavaScript checks')
    def test_javascript_and_mocked_api_behaviour(self):
        result = subprocess.run([shutil.which('node'), '-', str(DASHBOARD)], input=OFFLINE_JS, text=True, capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Offline scenarios passed: 34', result.stdout)
        print(result.stdout.strip())


if __name__ == '__main__':
    unittest.main(verbosity=2)
