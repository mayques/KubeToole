// Local integration checks for the separately hosted website; no browser or network used.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const website = path.resolve(__dirname, '../../mayques.com/calculator');
const html = fs.readFileSync(path.join(website, 'calculator.html'), 'utf8');
const script = fs.readFileSync(path.join(website, 'account-dashboard.js'), 'utf8');
const authScript = fs.readFileSync(path.join(website, 'auth.js'), 'utf8');

async function until(check) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (check()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.fail('Dashboard did not reach the expected state');
}

function setup(t) {
  const dom = new JSDOM(html, { url: 'https://mayques.com/calculator/calculator.html', runScripts: 'outside-only', pretendToBeVisual: true });
  t.after(() => dom.window.close());
  const w = dom.window, d = w.document;
  const state = {
    rows: [], calls: [], usage: {storage: {usedBytes: 1000, reservedBytes: 1000, limitBytes: 25e9}, cpu: {usedSlots: 0, totalSlots: 2, enabled: true}, tools: {calculator: {savedBytes: 1000}}},
    specs: [
      {id: 'calculator', name: 'Calculator', execution: 'inline', inputs: {type: 'object'}},
      {id: 'probe', name: 'Test tool', description: '<img src=x onerror=alert(1)>', execution: 'job', inputs: {type: 'object', required: ['n'], properties: {n: {type: 'integer'}, confirm: {type: 'boolean', enum: [true, false]}}}},
    ],
  };
  w.fetch = async (url, options) => {
    const route = new URL(url).pathname;
    assert.equal(options.headers.Authorization, 'Bearer test-access-token');
    const body = options.body && JSON.parse(options.body);
    state.calls.push({route, body});
    if (state.hold) await state.hold;
    if (state.error) return {status: state.error, ok: false, json: async () => ({detail: 'Unavailable'})};
    let response;
    if (route === '/tools') response = {tools: state.specs};
    else if (route === '/account/usage') response = state.usage;
    else if (route === '/jobs') response = {jobs: state.rows};
    else if (route === '/tools/calculator/run') response = {kind: 'inline', result: {result: 4}};
    else if (route === '/tools/probe/run') {
      assert.deepEqual(body, {inputs: {n: 7, confirm: true}});
      state.rows = [{id: 'job-1', tool_id: 'probe', state: 'running', artifacts: []}];
      response = {kind: 'job', job: state.rows[0]};
    } else if (route === '/jobs/job-1/cancel') {
      state.rows[0].state = 'cancelling';
      response = state.rows[0];
    } else assert.fail('Unexpected request ' + route);
    return {status: 200, ok: true, json: async () => response};
  };
  w.eval(script);
  w.accountDashboard.configure({getUser: async () => ({access_token: 'test-access-token'}), onExpired: () => w.accountDashboard.setAuthenticated(false)});
  return {w, d, state};
}

async function login(app) {
  app.w.accountDashboard.setAuthenticated(true);
  await until(() => app.d.querySelectorAll('[data-tool]').length === 2);
}

test('usage, capacity and tools stay separate from the empty workspace', async t => {
  const app = setup(t), {d, state} = app;
  state.usage.cpu.usedSlots = 2;
  await login(app);
  assert.equal(d.querySelector('#calculator').hidden, true);
  assert.equal(d.querySelector('#tool-probe').hidden, true);
  assert.equal(d.querySelector('#cpu-card').dataset.state, 'full');
  assert.match(d.querySelector('#storage-summary').textContent, /free of 25 GB/);
  assert.match(d.querySelector('[data-tool="calculator"]').textContent, /Data saved: 1 kB/);
  assert.equal(d.querySelector('#tool-probe img'), null); // Tool metadata is text, never HTML.
  d.querySelector('[data-tool="calculator"]').click();
  assert.equal(d.querySelector('#calculator').hidden, false);
  d.querySelector('#expression').value = '2+2';
  d.querySelector('#calculator-form').dispatchEvent(new app.w.Event('submit', {cancelable: true}));
  await until(() => d.querySelector('#result').textContent === '4');
});

test('generic tool launch and cancel show stopping until confirmed stopped', async t => {
  const app = setup(t), {w, d, state} = app;
  await login(app);
  d.querySelector('[data-tool="probe"]').click();
  d.querySelector('#tool-probe input').value = '7';
  d.querySelector('#tool-probe form').dispatchEvent(new w.Event('submit', {cancelable: true}));
  await until(() => d.querySelector('.cancel-job'));
  assert.equal(d.querySelector('.cancel-job').textContent, 'Cancel job');
  d.querySelector('.cancel-job').click();
  await until(() => state.rows[0].state === 'cancelling');
  await until(() => d.querySelector('.cancel-job')?.disabled);
  assert.equal(d.querySelector('.cancel-job').textContent, 'Stopping…');
  assert.equal(state.calls.filter(x => x.route.endsWith('/cancel')).length, 1);
  await until(() => state.calls.filter(x => x.route === '/jobs').length >= 3);
  state.rows[0].state = 'cancelled';
  await w.accountDashboard.refresh();
  assert.equal(d.querySelector('.cancel-job'), null);
  assert.match(d.querySelector('#job-list').textContent, /Cancelled/);
});

test('logout ignores late responses and clears user data', async t => {
  const app = setup(t), {w, d, state} = app;
  let release;
  state.hold = new Promise(resolve => {release = resolve;});
  w.accountDashboard.setAuthenticated(true);
  await until(() => state.calls.length === 3);
  w.accountDashboard.setAuthenticated(false);
  release();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(d.querySelector('#account-dashboard').hidden, true);
  assert.equal(d.querySelector('#tool-menu').children.length, 0);
  assert.equal(d.querySelector('#job-list').children.length, 0);
  assert.equal(d.querySelector('#calculator').hidden, true);
});

test('expired authentication clears dashboard without retrying work', async t => {
  const app = setup(t), {w, d, state} = app;
  await login(app);
  state.error = 401;
  await w.accountDashboard.refresh();
  assert.equal(d.querySelector('#account-dashboard').hidden, true);
  assert.equal(d.querySelector('#tool-menu').children.length, 0);
  assert.equal(state.calls.filter(x => x.route.endsWith('/run')).length, 0);
});

test('login and registration keep ZITADEL redirects and logout flow', async t => {
  const app = setup(t), {w, d} = app;
  const calls = [];
  w.oidc = {UserManager: class {
    constructor(settings) {this.settings = settings; calls.push(settings);}
    async getUser() {return null;}
    async signinRedirect(options) {calls.push(options || 'login');}
    async signoutRedirect() {calls.push('logout');}
  }};
  w.eval(authScript);
  await until(() => d.querySelector('#page-title').textContent === 'Login');
  assert.equal(calls[0].redirect_uri, 'https://mayques.com/calculator/calculator.html');
  assert.match(calls[0].scope, /400874651212903737:aud/);
  d.querySelector('#login-button').click();
  d.querySelector('#register-button').click();
  d.querySelector('#logout-button').click();
  assert.equal(calls[1], 'login');
  assert.equal(calls[2].prompt, 'create');
  assert.equal(calls[3], 'logout');
});
