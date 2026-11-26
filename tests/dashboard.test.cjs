// Local integration checks for the separately hosted website; no browser or network used.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const website = path.resolve(__dirname, '../../mayques.com/calculator');
const html = fs.readFileSync(path.join(website, 'calculator.html'), 'utf8');
const script = fs.readFileSync(path.join(website, 'account-dashboard.js'), 'utf8');
const csvScript = fs.readFileSync(path.join(website, 'csv-files.js'), 'utf8');
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
    rows: [], files: [], calls: [], usage: {storage: {usedBytes: 1000, reservedBytes: 1000, limitBytes: 25e9}, cpu: {usedSlots: 0, totalSlots: 2, enabled: true}, tools: {calculator: {savedBytes: 1000}}},
    specs: [
      {id: 'calculator', name: 'Calculator', execution: 'inline', inputs: {type: 'object'}},
      {id: 'probe', name: 'Test tool', description: '<img src=x onerror=alert(1)>', execution: 'job', inputs: {type: 'object', required: ['n'], properties: {n: {type: 'integer'}, confirm: {type: 'boolean', enum: [true, false]}}}},
    ],
  };
  w.fetch = async (url, options) => {
    const route = new URL(url).pathname;
    assert.equal(options.headers.Authorization, 'Bearer test-access-token');
    const raw = options.headers['Content-Type'] === 'application/octet-stream';
    const body = options.body && (raw ? options.body : JSON.parse(options.body));
    state.calls.push({route, body, url});
    if (state.hold) await state.hold;
    if (state.error) return {status: state.error, ok: false, json: async () => ({detail: 'Unavailable'})};
    let response;
    if (route === '/tools') response = {tools: state.specs};
    else if (route === '/account/usage') response = state.usage;
    else if (route === '/jobs') response = {jobs: state.rows};
    else if (route === '/uploads' && !body) response = {uploads: state.files, maxBytes: 500_000_000, chunkBytes: 1_048_576};
    else if (route === '/uploads') {
      state.files.push({id: 'file-1', name: body.name, size_bytes: body.size, state: 'receiving'});
      response = state.files.at(-1);
    } else if (route === '/uploads/file-1/write') {
      assert.ok(body.size <= 1_048_576);
      if (state.chunkHold) await state.chunkHold;
      response = {written: body.size};
    } else if (route === '/uploads/file-1/complete') {
      state.files[0].state = 'uploaded'; response = state.files[0];
    } else if (route === '/uploads/file-1/validate') {
      if (state.validationBusy) return {status: 429, ok: false, json: async () => ({detail: 'Compute is busy'})};
      state.files[0].state = 'validating'; response = {id: 'validation-1'};
    } else if (route === '/uploads/file-1/remove') {
      state.files = []; response = {removed: true};
    } else if (route === '/downloads') response = {token: 'single-use-download-token'};
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
  w.eval(csvScript);
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
  await until(() => state.calls.length === 4);
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

function choose(app, file) {
  Object.defineProperty(app.d.querySelector('#csv-file'), 'files', {value: [file], configurable: true});
  app.d.querySelector('#csv-file').dispatchEvent(new app.w.Event('change'));
}

test('CSV upload sends bounded chunks in order, then seals and validates once', async t => {
  const app = setup(t), {w, d, state} = app;
  await login(app);
  choose(app, new w.File(['a\n' + '1\n'.repeat(530000)], 'numbers.csv', {type: 'text/csv'}));
  await until(() => state.files[0]?.state === 'validating' && d.querySelector('#csv-cancel').hidden);
  const writes = state.calls.filter(call => call.route.endsWith('/write'));
  assert.equal(writes.length, 2);
  assert.equal(new URL(writes[0].url).searchParams.get('offset'), '0');
  assert.equal(new URL(writes[1].url).searchParams.get('offset'), '1048576');
  assert.equal(state.calls.filter(call => call.route.endsWith('/validate')).length, 1);
  assert.equal(d.querySelector('#calculator').hidden, true);
  assert.equal(d.querySelector('#csv-file').disabled, true);
});

test('oversized uploads are blocked before reserving storage', async t => {
  const app = setup(t);
  await login(app);
  choose(app, {name: 'too-large.csv', size: 500_000_001});
  assert.match(app.d.querySelector('#csv-status').textContent, /500 MB/);
  assert.equal(app.state.calls.filter(call => call.route === '/uploads' && call.body).length, 0);
});

test('busy validation leaves an explicit manual validation button without retries', async t => {
  const app = setup(t), {w, d, state} = app;
  state.validationBusy = true;
  await login(app);
  choose(app, new w.File(['a\n1\n'], 'data.csv'));
  await until(() => d.querySelector('#csv-status').textContent.includes('Compute is busy'));
  await until(() => d.querySelector('#csv-list').textContent.includes('Validate CSV'));
  assert.equal(state.calls.filter(call => call.route.endsWith('/validate')).length, 1);
  assert.equal(state.files[0].state, 'uploaded');
});

test('cancel upload stops additional chunks and removes its reservation', async t => {
  const app = setup(t), {w, d, state} = app;
  let release;
  state.chunkHold = new Promise(resolve => {release = resolve;});
  await login(app);
  choose(app, new w.File(['a'.repeat(1_048_580)], 'data.csv'));
  await until(() => state.calls.some(call => call.route.endsWith('/write')));
  d.querySelector('#csv-cancel').click();
  release();
  await until(() => state.calls.some(call => call.route.endsWith('/remove')));
  assert.equal(state.calls.filter(call => call.route.endsWith('/write')).length, 1);
  assert.equal(state.calls.filter(call => call.route.endsWith('/validate')).length, 0);
});

test('downloads use a scoped POST and filenames remain text; logout clears files', async t => {
  const app = setup(t), {w, d, state} = app;
  state.files = [{id: 'file-1', name: '<img src=x onerror=alert(1)>.csv', size_bytes: 5, state: 'ready', rows: 1, columns: 1}];
  const forms = [];
  w.HTMLFormElement.prototype.submit = function () {
    forms.push({action: this.action, method: this.method, token: this.querySelector('[name=token]').value});
  };
  await login(app);
  assert.equal(d.querySelector('#csv-list img'), null);
  d.querySelector('#csv-list button').click();
  await until(() => forms.length === 1);
  assert.deepEqual(forms[0], {action: 'https://api.mayques.com/downloads/consume', method: 'post', token: 'single-use-download-token'});
  assert.deepEqual(state.calls.find(call => call.route === '/downloads').body, {kind: 'upload', id: 'file-1'});
  w.accountDashboard.setAuthenticated(false);
  assert.equal(d.querySelector('#csv-list').children.length, 0);
  assert.equal(d.querySelector('[data-csv-download]'), null);
});

test('new tools get a selector of ready CSVs and preserve selection during refresh', async t => {
  const app = setup(t), {w, d, state} = app;
  state.specs[1].inputs.properties.dataset = {type: 'string', format: 'csv-file'};
  state.files = [
    {id: 'ready', name: 'Ready.csv', state: 'ready', size_bytes: 5, rows: 1, columns: 1},
    {id: 'pending', name: 'Pending.csv', state: 'validating', size_bytes: 5},
  ];
  await login(app);
  const select = d.querySelector('[data-csv-input]');
  assert.deepEqual(Array.from(select.options, option => option.value), ['', 'ready']);
  select.value = 'ready';
  await w.accountDashboard.refresh();
  assert.equal(select.value, 'ready');
  state.files = [];
  await w.accountDashboard.refresh();
  assert.equal(select.value, '');
});
