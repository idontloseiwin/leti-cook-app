import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { randomUUID } from 'node:crypto';

const html = readFileSync(new URL('../application/self_hosted_ui/setup.html', import.meta.url), 'utf8');
const script = readFileSync(new URL('../application/self_hosted_ui/setup.js', import.meta.url), 'utf8');
const ready = {configured: true, prepared: true, phase: 'ready', revision: 1, error: null,
  progress: {languages_done: 83, languages_total: 83, requests_sent: 84},
  reference: {status: 'current', languages: 83}, workspace: {phase: 'choosing', channels: {}}, collection: {state: 'running'}};
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve};};
const added = {status: 'added', channel: {channel_id: 'UC' + 'a'.repeat(22)}};

const complete = {status: 'completed', action_required: null, all_initial_checks_complete: true,
  current_profile_ready: false, job: {status: 'completed', phase: 'verify', errors: 0}};
const id = i => 'UC' + String(i).repeat(22);
const url = 'https://www.youtube.com/@MrBeast';
const loading = (channels = {[url]: id(1)}) => ({...structuredClone(ready), workspace: {phase: 'loading', channels}});
async function harness({status = ready, hot = {status: 'ready'}, add, hydrations = {}, finish, continuation, prepare, statusRead, localValues = new Map()} = {}) {
  const makeElement = (tag = 'div') => ({
    tag, textContent: '', hidden: false, disabled: false, checked: false, attributes: {}, handlers: {}, children: [],
    addEventListener(type, fn) {this.handlers[type] = fn;},
    setAttribute(key, value) {this.attributes[key] = value;},
    removeAttribute(key) {delete this.attributes[key];},
    append(child) {this.children.push(child);}, replaceChildren() {this.children = [];},
  });
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, {...makeElement(), id}]));
  const inputs = [...html.matchAll(/<input id="([^"]+)" name="starter-channel" type="checkbox" value="([^"]+)"/g)]
    .map(([, id, value]) => Object.assign(elements.get(id), {value}));
  const requests = [], navigation = [];
  const state = {status: structuredClone(status), hot, add, hydrations, finish, continuation, prepare, statusRead};
  const context = vm.createContext({
    AbortController, crypto: {randomUUID},
    localStorage: {getItem: key => localValues.get(key) || null, setItem: (key, value) => localValues.set(key, value)},
    document: {hidden: false, getElementById(id) {assert.ok(elements.has(id), `Missing DOM id ${id}`); return elements.get(id);},
      querySelectorAll: () => inputs, addEventListener() {}, createElement: makeElement},
    window: {addEventListener() {}}, location: {assign: path => navigation.push(path), replace: path => navigation.push(path)},
    setTimeout: () => 1, clearTimeout() {},
    fetch: async (path, options) => {
      const body = options.body ? JSON.parse(options.body) : null;
      requests.push({path, body});
      let value;
      if (path === '/setup/status') value = state.statusRead ? await state.statusRead(state) : state.status;
      else if (path === '/system/hot-runtime') value = state.hot;
      else if (path === '/setup/prepare') {
        state.status = state.prepare ? state.prepare(state.status) : {...state.status, phase: 'preparing', error: null, revision: state.status.revision + 1};
        value = state.status;
      } else if (path === '/setup/workspace/start') {
        state.status.workspace.phase = 'loading';
        for (const url of body.channels) state.status.workspace.channels[url] ??= null;
        value = state.status;
      } else if (path === '/starter-channels/add' && state.add) {
        state.status.workspace.channels[body.url] ??= null;
        value = await state.add(body);
        if (value.channel?.channel_id) state.status.workspace.channels[body.url] = value.channel.channel_id;
      } else if (/^\/channels\/UC.*\/hydration$/.test(path)) {
        const channel = path.split('/')[2];
        value = body && state.continuation ? await state.continuation(channel, body) : state.hydrations[channel] || {};
      } else if (path === '/starter-channels/finish') {
        value = state.finish ? await state.finish(state) : {ready: true};
        if (value.ready) state.status.workspace = {phase: 'complete', channels: {}};
      } else throw new Error(`Unexpected request ${path}`);
      return {ok: true, status: 200, json: async () => structuredClone(value)};
    },
  });
  vm.runInContext(script, context);
  await flush();
  const event = () => ({preventDefault() {this.prevented = true;}});
  const screens = ['key', 'choice', 'progress', 'attention'];
  return {elements, inputs, requests, navigation, state, context, localValues,
    async click(id) {return elements.get(id).handlers.click(event());},
    reload() {return vm.runInContext('load()', context);},
    screen() {const shown = screens.filter(name => !elements.get(name + '-screen').hidden); assert.equal(shown.length, 1, shown.join()); return shown[0];},
    posts(path) {return requests.filter(r => r.path === path && r.body);},
    rowText() {return elements.get('loading-channels').children.flatMap(row => row.children.map(node => node.textContent)).join(' ');},
    rowButton() {return elements.get('loading-channels').children.flatMap(row => row.children).find(node => node.tag === 'button');}};
}

{
  // The progress screen is a title, one bar and one status line; everything else lives on the other screens.
  const progress = html.match(/<section id="progress-screen"[^>]*>([\s\S]*?)<\/section>/)[1];
  assert.deepEqual([...progress.matchAll(/<(\w+)/g)].map(match => match[1]), ['h1', 'progress', 'p']);
  assert.match(progress, /<p id="progress-message" role="status" aria-live="polite" hidden><\/p>/);
  assert.match(progress, /<h1 id="progress-title">Preparing Leti Cook for use<\/h1>/);
  assert.ok(!/<button/.test(progress));
}
{
  const h = await harness();
  assert.equal(h.screen(), 'choice');
  assert.equal(h.inputs.length, 5);
  assert.deepEqual(h.inputs.map(i => i.id), ['starter-MrBeast','starter-MarkRober','starter-veritasium','starter-kurzgesagt','starter-mkbhd']);
  assert.equal(h.elements.get('workspace').attributes.href, '/app-explorer-v3');
  assert.equal(h.elements.get('notice').hidden, true);
  assert.ok(!html.includes('starter-add'));
}
{
  // Without a key, or with one YouTube refused, only the key screen shows; nothing is prepared.
  const h = await harness({status: {...ready, configured: false, prepared: false, phase: 'needs_key'}});
  assert.equal(h.screen(), 'key');
  assert.equal(h.elements.get('key-error').hidden, true);
  h.state.status = {...ready, configured: false, prepared: false, phase: 'needs_key', error: 'provider_key_rejected'};
  await h.reload();
  assert.equal(h.screen(), 'key');
  assert.match(h.elements.get('key-error').textContent, /rejected the saved key/);
  assert.equal(h.posts('/setup/prepare').length, 0);
}
{
  // A saved key is checked before offering channels; preparation never flashes a choice.
  const saved = {...ready, prepared: false, phase: 'saved', progress: {languages_done: 0, languages_total: null, requests_sent: 0}};
  const h = await harness({status: saved});
  assert.equal(h.posts('/setup/prepare').length, 1);
  assert.equal(h.screen(), 'progress');
  assert.equal(h.elements.get('workspace').attributes['aria-disabled'], 'true');
  assert.ok(h.inputs.every(input => input.disabled));
  h.state.status = {...h.state.status, progress: {languages_done: 42, languages_total: 84, requests_sent: 43}};
  await h.reload();
  await h.click('starter-all');
  await h.click('workspace');
  assert.equal(h.screen(), 'progress');
  assert.ok(h.inputs.every(input => !input.checked));
  assert.equal(h.elements.get('progress').max, 2); // reference + stored data, before choosing channels
  assert.equal(h.elements.get('progress').value, 1.5); // Runtime readiness is independent of reference preparation.
  assert.equal(h.posts('/setup/workspace/start').length, 0); // Adds wait for reference data.
  h.state.status = {...ready, revision: h.state.status.revision + 1, workspace: structuredClone(ready.workspace)};
  h.state.add = () => added;
  await h.reload();
  assert.equal(h.screen(), 'choice');
  assert.equal(h.posts('/setup/workspace/start').length, 0);
  await h.click('starter-all');
  await h.click('workspace');
  assert.equal(h.posts('/setup/workspace/start').length, 1);
  assert.equal(h.posts('/starter-channels/add').length, 5);
}
{
  // Refusal discovered by preparation leads straight to the key screen, with no channel writes.
  const h = await harness({status: {...ready, prepared: false, phase: 'saved'}});
  assert.equal(h.screen(), 'progress');
  h.state.status = {...h.state.status, phase: 'error', error: 'provider_key_rejected', revision: h.state.status.revision + 1};
  await h.reload();
  assert.equal(h.screen(), 'key');
  await h.click('workspace');
  assert.equal(h.posts('/setup/workspace/start').length, 0);
  assert.equal(h.posts('/starter-channels/add').length, 0);
}
{
  // Short outages are retried quietly; then the reason shows on the channel screen, not the progress screen.
  const failed = revision => ({...ready, prepared: false, phase: 'error', error: 'provider_unavailable', revision,
    progress: {languages_done: 3, languages_total: 84, requests_sent: 4}});
  const h = await harness({status: failed(1), prepare: status => failed(status.revision + 1)});
  for (let i = 0; i < 5; i++) await h.reload();
  assert.equal(h.posts('/setup/prepare').length, 3);
  assert.equal(h.screen(), 'choice');
  assert.equal(h.elements.get('notice').hidden, false);
  assert.match(h.elements.get('notice').textContent, /could not be reached/);
  assert.equal(h.elements.get('prepare').hidden, false);
  await h.click('workspace'); // Start after a failure is also its retry, with fresh quiet retries.
  assert.ok(h.posts('/setup/prepare').length >= 4);
}
{
  const h = await harness({status: {...ready, prepared: false, phase: 'error', error: 'provider_quota_exceeded'}});
  assert.equal(h.posts('/setup/prepare').length, 0);
  assert.equal(h.screen(), 'choice');
  assert.match(h.elements.get('notice').textContent, /quota/);
}
{
  // Stored data not ready: Start waits on the progress screen, says why, and adds nothing until it is.
  const h = await harness({hot: {status: 'blocked', api_read: {allowed: false, reason: 'startup_cleanup'}, collection: {allowed: true}, maintenance_failed: false}});
  await h.click('workspace');
  assert.equal(h.screen(), 'progress');
  assert.equal(h.elements.get('progress-message').hidden, false);
  assert.match(h.elements.get('progress-message').textContent, /Removing expired YouTube data/);
  assert.equal(h.posts('/setup/workspace/start').length, 0);
  assert.equal(h.navigation.length, 0);
  h.state.hot = {status: 'blocked', api_read: {allowed: false, reason: 'database_unavailable'}, maintenance_failed: true}; await h.reload();
  assert.match(h.elements.get('progress-message').textContent, /Reconnecting/);
  h.state.hot = {status: 'ready'}; await h.reload();
  assert.equal(h.elements.get('progress-message').hidden, true);
  assert.deepEqual(h.posts('/setup/workspace/start').map(r => r.body.channels), [[]]);
  assert.deepEqual(h.navigation, ['/app-explorer-v3']); // An empty workspace is ready at once.
}
{
  // Cleaner failure and unverified clock warn without changing the preparation screen or stopping it.
  const h = await harness({status: loading(), hot: {status: 'ready', api_read: {allowed: true}, collection: {allowed: true},
    maintenance_failed: true, time: {state: 'unverified'}}, hydrations: {[id(1)]: {status: 'running'}}});
  assert.equal(h.screen(), 'progress');
  const messages = h.elements.get('runtime-notices').children.map(row => row.children[0].textContent).join(' ');
  assert.match(messages, /cleanup needs attention/);
  assert.match(messages, /still working/);
  h.state.hydrations[id(1)] = complete;
  await h.reload();
  assert.deepEqual(h.navigation, ['/app-explorer-v3']);
}
{
  // No clock response yet is informational; a failed verification remains a warning.
  const h = await harness({hot: {status: 'ready', time: {state: 'unverified', reason: 'not_checked_yet'}}});
  const messages = () => h.elements.get('runtime-notices').children.map(row => row.children[0].textContent).join(' ');
  assert.match(messages(), /has not been checked yet/);
  assert.doesNotMatch(messages(), /could not be verified/);
  h.state.hot = {status: 'ready', time: {state: 'unverified', reason: 'check_failed'}};
  await h.reload();
  assert.match(messages(), /could not be verified/);
  assert.equal(h.screen(), 'choice');
}
{
  // A temporary collection pause waits without dispatching initial adds or calling it a user-configured pause.
  const h = await harness({hot: {status: 'ready', api_read: {allowed: true}, collection: {allowed: false}}});
  h.inputs[0].checked = true;
  await h.click('workspace');
  assert.equal(h.screen(), 'progress');
  assert.equal(h.posts('/starter-channels/add').length, 0);
  assert.match(h.elements.get('runtime-notices').children[0].children[0].textContent, /resume automatically/);
  h.state.hot.collection.allowed = true; h.state.add = () => added;
  await h.reload();
  assert.equal(h.posts('/starter-channels/add').length, 1);
}
{
  const noticeId = 'b'.repeat(32);
  const hot = {status: 'ready', startup_notice: {id: noticeId, records: 1234, complete: true}};
  const h = await harness({hot});
  const row = h.elements.get('runtime-notices').children[0];
  assert.match(row.children[0].textContent, /1,234 stored records expired/);
  await row.children[1].handlers.click();
  assert.equal(h.elements.get('runtime-notices').hidden, true);
  await h.reload();
  assert.equal(h.elements.get('runtime-notices').hidden, true);
  const reloaded = await harness({hot, localValues: h.localValues});
  assert.equal(reloaded.elements.get('runtime-notices').hidden, true);
  reloaded.state.hot = {...hot, startup_notice: {...hot.startup_notice, id: 'c'.repeat(32), complete: false}};
  await reloaded.reload();
  assert.equal(reloaded.elements.get('runtime-notices').hidden, true);
  reloaded.state.hot.startup_notice.complete = true;
  await reloaded.reload();
  assert.equal(reloaded.elements.get('runtime-notices').hidden, false);
}
{
  // Start with five channels saves the selection first, sends all five adds at once,
  // and never opens the app merely because adds returned.
  const calls = [];
  const h = await harness({add: () => {const d = deferred(); calls.push(d); return d.promise;}});
  await h.click('starter-all');
  const clicked = h.click('workspace'); await flush(); await flush();
  assert.equal(calls.length, 5);
  assert.equal(Object.keys(h.state.status.workspace.channels).length, 5);
  assert.equal(h.screen(), 'progress');
  await h.click('workspace'); assert.equal(calls.length, 5);
  calls.forEach((call, i) => call.resolve({status: 'added', channel: {channel_id: id(i)}}));
  await clicked; assert.equal(h.navigation.length, 0);
  assert.equal(h.screen(), 'progress'); // Successful adds must not flash five unconfirmed errors.
  assert.equal(h.rowText(), '');
  const stale = structuredClone(h.state.status);
  for (const url of Object.keys(stale.workspace.channels)) stale.workspace.channels[url] = null;
  h.state.statusRead = () => stale; // A read started before acknowledgements may still arrive.
  await h.reload();
  assert.equal(h.screen(), 'progress');
  assert.equal(h.rowText(), '');
  h.state.statusRead = null;
  for (let i = 0; i < 5; i++) h.state.hydrations[id(i)] = complete;
  await h.reload(); assert.deepEqual(h.navigation, ['/app-explorer-v3']);
}
{
  // An unknown add result is never retried on its own: it waits on the attention screen.
  const calls = [];
  const h = await harness({add: () => {const d = deferred(); calls.push(d); return d.promise;}});
  await h.click('starter-all');
  const clicked = h.click('workspace'); await flush(); await flush();
  calls[0].resolve({error: 'unknown acknowledgement'}); await flush();
  for (const call of calls.slice(1)) call.resolve(added);
  await clicked;
  assert.equal(calls.length, 5);
  assert.equal(h.screen(), 'attention');
  assert.match(h.rowText(), /was not confirmed/);
  assert.equal(h.elements.get('loading-channels').children.length, 1); // Only the genuinely unknown add.
  assert.equal(h.posts('/starter-channels/add').length, 5);
  assert.equal(h.inputs[0].disabled, true);
  assert.equal(h.navigation.length, 0);
}
{
  const h = await harness({status: loading(), hydrations: {[id(1)]: {status: 'running', phase: 'hydrate_videos', progress: {processed: 50, total: 100}}}});
  assert.equal(h.screen(), 'progress');
  assert.equal(h.elements.get('progress').max, 3);
  assert.equal(h.elements.get('progress').value, 2);
  assert.equal(h.navigation.length, 0);
  h.state.hydrations[id(1)] = {...complete, all_initial_checks_complete: false, current_profile_ready: true};
  await h.reload(); assert.equal(h.navigation.length, 0); // Long ready does not mean all initial checks complete.
  h.state.hydrations[id(1)] = {...complete, job: {status: 'running', phase: 'verify', errors: 0}};
  await h.reload(); assert.equal(h.navigation.length, 0);
  h.state.hydrations[id(1)] = complete;
  h.state.hot = {status: 'blocked'};
  await h.reload(); assert.equal(h.navigation.length, 0);
  assert.equal(h.requests.filter(r => r.path === '/starter-channels/finish').length, 0);
  h.state.hot = {status: 'ready'};
  await h.reload(); assert.deepEqual(h.navigation, ['/app-explorer-v3']);
}
{
  // Reload resumes saved setup; an unknown add is never replayed automatically.
  const h = await harness({status: loading({[url]: null})});
  assert.equal(h.screen(), 'attention');
  assert.match(h.rowText(), /was not confirmed/);
  assert.equal(h.requests.filter(r => r.body).length, 0);
  await h.reload(); assert.equal(h.requests.filter(r => r.body).length, 0);
  assert.equal(h.navigation.length, 0);
}
{
  const failed = {status: 'failed', action_required: 'retry', estimate: {items: 100, pages: 2, quota_units: 6},
    job: {id: 'one-job', updated_at: 'one-version', error_code: 'initial_hydration_quota_exhausted'}};
  const h = await harness({status: loading(), hydrations: {[id(1)]: failed}, continuation: () => {throw new Error('lost response');}});
  assert.equal(h.screen(), 'attention');
  assert.match(h.rowText(), /quota is unavailable/);
  assert.equal(h.navigation.length, 0);
  await h.rowButton().handlers.click();
  await h.rowButton().handlers.click();
  const retries = h.requests.filter(r => r.path.endsWith('/hydration') && r.body);
  assert.equal(retries.length, 2);
  assert.equal(retries[0].body.retry_request_id, retries[1].body.retry_request_id);
  assert.equal(retries[0].body.expected_items, 100);
  assert.equal(h.navigation.length, 0);
}
{
  const h = await harness({status: {...loading(), collection: {state: 'paused'}}});
  assert.equal(h.screen(), 'attention');
  assert.match(h.elements.get('loading-message').textContent, /Collection is paused/);
  assert.equal(h.navigation.length, 0);
}
{
  const h = await harness({status: loading(), hydrations: {[id(1)]: complete}, finish: state => {
    state.status.workspace = {phase: 'complete', channels: {}};
    throw new Error('response lost after saved completion');
  }});
  assert.equal(h.navigation.length, 0);
  await h.reload(); assert.deepEqual(h.navigation, ['/app-explorer-v3']);
  assert.equal(h.requests.filter(r => r.path === '/starter-channels/finish').length, 1);
}
{
  // Polls share one request. A newer prepare response must not be replaced by
  // the earlier status response, including its old error and required action.
  const failed = {...ready, prepared: false, phase: 'error', error: 'provider_quota_exceeded'};
  const h = await harness({status: failed});
  assert.equal(h.elements.get('notice').hidden, false);
  const oldRead = deferred();
  h.state.statusRead = () => oldRead.promise;
  const before = h.requests.filter(r => r.path === '/setup/status').length;
  const first = h.reload(), second = h.reload();
  await flush();
  assert.equal(h.requests.filter(r => r.path === '/setup/status').length, before + 1);
  await h.click('prepare'); await flush();
  assert.equal(h.elements.get('notice').hidden, true);
  oldRead.resolve(failed);
  await Promise.all([first, second]);
  assert.equal(h.elements.get('notice').hidden, true);
  assert.equal(h.posts('/setup/prepare').length, 1);
  assert.equal(vm.runInContext('snapshot.revision', h.context), h.state.status.revision);
}
console.log('Setup UI: key, channel, progress and attention screens; concurrent adds, job/HOT gates, recovery, quota errors and stale status protection passed.');
