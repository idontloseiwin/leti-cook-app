"use strict";
// Four screens. The key and channel screens hold every choice and check; the
// progress screen shows only a title and one bar while real preparation runs;
// anything that needs the user leaves it for the attention screen, or for the
// key or channel screen with the reason.
const $ = id => document.getElementById(id);
const errors = {
  provider_key_rejected: "YouTube rejected the saved key. Run set-key again with a working key.",
  provider_access_denied: "YouTube denied access with the saved key. Enable YouTube Data API v3 for its project, allow this server in its restrictions, then run set-key again.",
  provider_quota_exceeded: "Google reported a quota or rate limit. Try again after it becomes available.",
  provider_unavailable: "YouTube could not be reached. Check the connection and try again.",
  provider_response_invalid: "The complete reference could not be validated. No incomplete reference was installed.",
  preparation_interrupted: "Preparation was interrupted. Your settings are saved; try again when ready.",
  preparation_timeout: "Preparation reached its time or request limit. Your settings are saved.",
  maintenance_not_ready: "Data cleanup is not ready yet. Wait for recovery, then try again.",
  configuration_changed_reload: "Settings changed in another request. Reload this page.",
  collection_busy: "A collection pass is running. Let it finish, then try again.",
  preparation_busy: "Reference preparation is already running.",
  api_key_required: "Add your YouTube API key in the installation terminal.",
  storage_unavailable: "Private configuration storage is unavailable. Check the installation."
};
const KEY_ERRORS = ['provider_key_rejected', 'provider_access_denied'];
// Short outages are retried without showing anything; others go to the channel screen.
const TRANSIENT = ['provider_unavailable', 'maintenance_not_ready', 'preparation_interrupted', 'preparation_timeout'];
const AUTOMATIC_RETRIES = 3;
const START_KEY = 'lic-setup-start';
const DISMISSED_NOTICES_KEY = 'lic-runtime-dismissed-notices-v1';
const NOTICE_ID = /^[a-f0-9]{32}$/;
let dismissedNotices = [];
try {
  const saved = JSON.parse(localStorage.getItem(DISMISSED_NOTICES_KEY) || '[]');
  if (Array.isArray(saved)) dismissedNotices = saved.filter(id => typeof id === 'string' && NOTICE_ID.test(id)).slice(-20);
} catch {}
let snapshot = null, pending = false, timer = null, addingChannels = false;
let runtime = null, enteringWorkspace = false, checkingWorkspace = false, actionBusy = false;
let finishAttempted = false, loadingSeen = false, retries = 0, retriedRevision = null, noticeError = null;
let memoryStart = null, startFailure = '', loadInFlight = null;
const hydration = new Map(), retryRequests = new Map();

// The chosen channels wait here until reference data and stored data are ready.
function savedStart() {
  try {
    const value = JSON.parse(sessionStorage.getItem(START_KEY) || 'null');
    return Array.isArray(value) ? value : null;
  } catch {return memoryStart;}
}
function saveStart(urls) {
  memoryStart = urls;
  try {urls ? sessionStorage.setItem(START_KEY, JSON.stringify(urls)) : sessionStorage.removeItem(START_KEY);} catch {}
}

async function request(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), path === '/starter-channels/add' ? 120000 : 30000);
  try {
    const response = await fetch(path, {signal: controller.signal, method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
      headers: body ? {'Content-Type': 'application/json', 'X-LIC-Setup': '1'} : {},
      ...(body ? {body: JSON.stringify(body)} : {})});
    if (response.status === 401) {location.replace('/auth'); throw new Error('authentication_required');}
    const value = await response.json();
    if (!response.ok) throw new Error(value.code || value.detail?.code || 'setup_unavailable');
    return value;
  } finally {clearTimeout(timeout);}
}

function initialComplete(value) {
  if (!value || value.format_readiness_error || value.action_required) return false;
  if (Object.hasOwn(value, 'all_initial_checks_complete')) {
    return value.all_initial_checks_complete === true && value.job?.status === 'completed'
      && value.job?.phase === 'verify' && value.job?.errors === 0;
  }
  return value.status === 'ready' && value.current_profile_ready === true;
}
function count(value) {return Number.isSafeInteger(value) && value >= 0 ? value : null;}
function element(tag, text, parent) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  parent.append(node); return node;
}
function actionButton(row, label, action) {
  const button = element('button', label, row);
  button.type = 'button'; button.disabled = addingChannels || actionBusy;
  button.addEventListener('click', action);
}
function apiReady() {return runtime?.api_read?.allowed ?? runtime?.status === 'ready';}
function collectionReady() {return runtime?.collection?.allowed ?? runtime?.status === 'ready';}
function runtimeBlockedText() {
  const reason = runtime?.api_read?.reason || runtime?.reason;
  if (runtime?.time?.state === 'mismatch' || ['time_mismatch', 'clock_changed'].includes(reason)) return 'YouTube data and collection are paused. Check the server clock.';
  if (reason === 'schema_invalid') return 'The database needs attention. Check the installation.';
  if (reason === 'database_unavailable') return 'The database is unavailable. Reconnecting…';
  if (reason === 'startup_cleanup') return 'Removing expired YouTube data. Current data is being checked.';
  return 'YouTube data is temporarily unavailable while the application gets ready.';
}
function renderRuntimeNotices(screen) {
  const list = $('runtime-notices'); list.replaceChildren();
  const add = text => {const row = element('div', undefined, list); row.className = 'runtime-notice'; row.setAttribute('role', 'status'); element('p', text, row); return row;};
  if (runtime && !apiReady() && screen !== 'progress') add(runtimeBlockedText());
  if (runtime?.collection?.allowed === false && apiReady()) add('Collection is temporarily paused. It will resume automatically.');
  if (runtime?.maintenance_failed) add('Data cleanup needs attention. It will retry automatically; expired data stays hidden.');
  if (runtime?.time?.state === 'unverified') add(runtime.time.reason === 'not_checked_yet'
    ? 'The server clock has not been checked yet. The application is working; verification will run automatically.'
    : 'The server clock could not be verified. The application is still working; verification will retry.');
  const notice = runtime?.startup_notice;
  if (notice?.complete === true && typeof notice.id === 'string' && NOTICE_ID.test(notice.id)
      && Number.isSafeInteger(notice.records) && notice.records >= 0 && !dismissedNotices.includes(notice.id)) {
    const row = add(`${notice.records.toLocaleString('en-US')} stored ${notice.records === 1 ? 'record expired' : 'records expired'} while the application was offline and ${notice.records === 1 ? 'was' : 'were'} removed.`);
    const button = element('button', 'Dismiss', row); button.type = 'button';
    button.setAttribute('aria-label', 'Dismiss expired records notice');
    button.addEventListener('click', () => {
      dismissedNotices = [...dismissedNotices.filter(id => id !== notice.id), notice.id].slice(-20);
      try {localStorage.setItem(DISMISSED_NOTICES_KEY, JSON.stringify(dismissedNotices));} catch {}
      if (snapshot) render(snapshot);
    });
  }
  list.hidden = !list.children.length;
}
const starterInputs = [...document.querySelectorAll('input[name="starter-channel"]')];
const attemptedChannels = new Set();
const names = {MrBeast: 'MrBeast', MarkRober: 'Mark Rober', veritasium: 'Veritasium', kurzgesagt: 'Kurzgesagt - In a Nutshell', mkbhd: 'Marques Brownlee'};

// What the loading channels need from the user, if anything: [message, rows].
function loadingAttention() {
  const channels = Object.entries(snapshot?.workspace?.channels || {});
  const hotReady = apiReady();
  const rows = [];
  let message = '';
  for (const [url, id] of channels) {
    const input = starterInputs.find(input => input.value === url);
    const name = names[url.split('@')[1]] || url;
    if (!id) {
      if (!addingChannels && !enteringWorkspace) rows.push({name, text: 'Adding this channel was not confirmed. Retry it, or check again first.', action: ['Retry adding channel', () => retryAdd(input)]});
      continue;
    }
    const value = hydration.get(id);
    if (!hotReady || !value || initialComplete(value)) continue;
    const code = value.job?.error_code || '';
    const failed = ['failed', 'error', 'cancelled'].includes(value.status) || !!value.format_readiness_error;
    const action = value.action_required;
    let text = null;
    if (code.includes('quota')) text = 'YouTube quota is unavailable. Continue after it resets.';
    else if (action === 'confirm') text = 'This channel needs your confirmation before its full collection.';
    else if (failed || action === 'retry' || action === 'start') text = 'Loading stopped. Completed work is saved.';
    if (!text) continue;
    const estimate = value.estimate;
    const row = {name, text};
    if (['retry', 'start', 'confirm'].includes(action) && estimate && ['items', 'pages', 'quota_units'].every(key => count(estimate[key]) !== null)) {
      row.action = [`Continue loading (${estimate.items} videos · about ${estimate.quota_units} quota units)`, () => continueChannel(id, value)];
    }
    rows.push(row);
  }
  const collection = snapshot?.collection?.state;
  if (collection === 'paused' && runtime?.collection?.allowed !== false) message = 'Collection is paused. Enable automatic_collection in config/config.ini and restart the application to continue.';
  else if (['worker_failed', 'scheduler_unavailable'].includes(collection)) message = 'The collection worker needs attention. Check the installation; completed work is saved.';
  else if (rows.length) message = rows.length === 1 ? 'One channel needs your attention.' : `${rows.length} channels need your attention.`;
  else if (startFailure) message = startFailure;
  return message ? [message, rows] : null;
}

function screenFor(value) {
  if (!value.configured || KEY_ERRORS.includes(value.error)) return 'key';
  if (enteringWorkspace || value.workspace?.phase === 'loading') return loadingAttention() ? 'attention' : 'progress';
  if (savedStart() && !noticeError) return 'progress';
  if (!value.prepared && !noticeError) return 'progress';
  return 'choice';
}

function render(value) {
  if (snapshot && value.revision < snapshot.revision) value = snapshot;
  if (snapshot?.workspace?.phase === 'loading' && value.workspace?.phase === 'loading') {
    // A status read may predate an acknowledged add. Confirmed IDs cannot be
    // cleared while this saved selection is loading; unknown adds stay null.
    const channels = {...value.workspace.channels};
    for (const url of Object.keys(channels)) if (!channels[url] && snapshot.workspace.channels[url]) {
      channels[url] = snapshot.workspace.channels[url];
    }
    value = {...value, workspace: {...value.workspace, channels}};
  }
  snapshot = value;
  const screen = screenFor(value);
  for (const name of ['key', 'choice', 'progress', 'attention']) $(name + '-screen').hidden = name !== screen;
  if (screen === 'progress' || screen === 'attention') loadingSeen = loadingSeen || value.workspace?.phase === 'loading';
  $('key-error').hidden = !KEY_ERRORS.includes(value.error);
  $('key-error').textContent = KEY_ERRORS.includes(value.error) ? errors[value.error] : '';
  $('notice').hidden = !noticeError;
  $('notice').textContent = noticeError ? errors[noticeError] || 'Preparation could not finish. Try again.' : '';
  $('prepare').hidden = !noticeError;
  $('prepare').disabled = pending;
  renderProgress();
  renderAttention();
  renderRuntimeNotices(screen);
  updateStarterControls();
}

// One bar covers reference preparation and the work selected with Start.
function renderProgress() {
  const value = snapshot;
  const channels = Object.entries(value?.workspace?.channels || {});
  const chosen = value?.workspace?.phase === 'loading' ? channels.length : (savedStart() || []).length;
  const p = value?.progress || {};
  const reference = value?.prepared ? 1 : p.languages_total ? Math.min(p.languages_done / p.languages_total, 1) : 0;
  const complete = apiReady() ? channels.filter(([, id]) => id && initialComplete(hydration.get(id))).length : 0;
  $('progress').max = 2 + chosen;
  $('progress').value = reference + (apiReady() ? 1 : 0) + complete;
  const message = storedDataCheck();
  $('progress-message').textContent = message;
  $('progress-message').hidden = !message;
}

// Only the API switch delays display. A cleanup failure alone does not stop preparation.
function storedDataCheck() {
  if (!snapshot?.prepared || !runtime || apiReady()) return '';
  return runtimeBlockedText();
}

function renderAttention() {
  const list = $('loading-channels'); list.replaceChildren();
  const attention = snapshot && (enteringWorkspace || snapshot.workspace?.phase === 'loading') ? loadingAttention() : null;
  $('loading-message').textContent = attention ? attention[0] : '';
  for (const row of attention ? attention[1] : []) {
    const node = element('div', undefined, list); node.className = 'loading-channel';
    element('strong', row.name, node);
    element('p', row.text, node);
    if (row.action) actionButton(node, ...row.action);
  }
  $('loading-recheck').disabled = addingChannels || actionBusy || checkingWorkspace;
}

async function prepare() {
  if (pending || !snapshot || snapshot.phase === 'preparing') return;
  pending = true;
  try {noticeError = null; render(await request('/setup/prepare', {revision: snapshot.revision}));}
  catch (error) {if (!TRANSIENT.includes(error.message)) noticeError = error.message;}
  finally {pending = false; if (snapshot) render(snapshot);}
}

function load() {
  if (!loadInFlight) loadInFlight = loadStatus().finally(() => {loadInFlight = null;});
  return loadInFlight;
}

async function loadStatus() {
  try {
    const value = await request('/setup/status');
    const nextRuntime = await request('/system/hot-runtime').catch(() => null);
    if (snapshot && value.revision < snapshot.revision) return;
    runtime = nextRuntime;
    // Reference data starts as soon as a key is saved; short outages are retried quietly.
    if (value.configured && !value.prepared && value.phase === 'error' && !KEY_ERRORS.includes(value.error)) {
      if (TRANSIENT.includes(value.error) && retries < AUTOMATIC_RETRIES && retriedRevision !== value.revision) {
        retries++; retriedRevision = value.revision; snapshot = value; await prepare(); return;
      }
      noticeError = value.error;
    } else if (value.phase !== 'error') noticeError = null;
    if (noticeError) saveStart(null);
    render(value);
    if (value.configured && !value.prepared && value.phase === 'saved') await prepare();
    const start = savedStart();
    if (start && value.prepared && apiReady() && (!start.length || collectionReady()) && value.workspace?.phase !== 'loading' && !enteringWorkspace) {
      await beginWorkspace(start); return;
    }
    if (value.prepared && value.workspace?.phase === 'loading' && !addingChannels && !actionBusy) await checkWorkspace();
    else if (loadingSeen && value.workspace?.phase === 'complete' && apiReady()) location.assign('/app-explorer-v3');
  } catch (error) {runtime = null; if (snapshot) render(snapshot);}
}

function updateStarterControls() {
  const busy = addingChannels || pending || enteringWorkspace || !snapshot || screenFor(snapshot) !== 'choice';
  const saved = snapshot?.workspace?.channels || {};
  for (const input of starterInputs) if (Object.hasOwn(saved, input.value)) {
    attemptedChannels.add(input.value);
    if (saved[input.value]) {input.checked = false; $('result-' + input.id.slice(8)).textContent = 'Added.';}
  }
  const available = starterInputs.filter(input => !attemptedChannels.has(input.value));
  for (const input of starterInputs) input.disabled = busy || attemptedChannels.has(input.value);
  $('starter-all').disabled = busy || !available.length;
  $('starter-all').textContent = available.length && available.every(input => input.checked) ? 'Clear selection' : 'Select all';
  // Offer a choice only after preparation has succeeded or needs a deliberate retry.
  const canStart = !!snapshot?.configured && !busy;
  $('workspace').setAttribute('aria-disabled', String(!canStart));
  if (canStart) $('workspace').setAttribute('href', '/app-explorer-v3');
  else $('workspace').removeAttribute('href');
}
$('starter-all').addEventListener('click', () => {
  if (addingChannels || pending || enteringWorkspace || !snapshot || screenFor(snapshot) !== 'choice') return;
  const available = starterInputs.filter(input => !attemptedChannels.has(input.value));
  const select = !available.every(input => input.checked);
  for (const input of available) input.checked = select;
  updateStarterControls();
});
for (const input of starterInputs) input.addEventListener('change', updateStarterControls);

function recordAddedChannel(url, result) {
  if (result.error || result.detail || !['added','exists','restored'].includes(result.status)
      || typeof result.channel?.channel_id !== 'string' || !/^UC[A-Za-z0-9_-]{22}$/.test(result.channel.channel_id)) {
    throw new Error('channel_result_unconfirmed');
  }
  if (snapshot?.workspace?.phase === 'loading' && Object.hasOwn(snapshot.workspace.channels, url)) {
    snapshot = {...snapshot, workspace: {...snapshot.workspace,
      channels: {...snapshot.workspace.channels, [url]: result.channel.channel_id}}};
  }
}

async function addSelectedChannels(selected) {
  if (!selected.length || selected.length > 5) return;
  addingChannels = true; render(snapshot);
  let next = 0, stopped = false;
  async function addNext() {
    while (!stopped && next < selected.length) {
      const input = selected[next++];
      attemptedChannels.add(input.value);
      try {
        const result = await request('/starter-channels/add', {url: input.value});
        recordAddedChannel(input.value, result);
        input.checked = false;
      } catch (error) {stopped = true;}
    }
  }
  try {
    // Start all selected suggestions (at most five), then await every result.
    // Never retry an unknown write or leave other in-flight results unconfirmed.
    await Promise.all(selected.map(() => addNext()));
    runtime = await request('/system/hot-runtime').catch(() => null);
  } finally {addingChannels = false; render(snapshot);}
}

$('workspace').addEventListener('click', async event => {
  event.preventDefault();
  if (addingChannels || pending || enteringWorkspace || !snapshot?.configured || screenFor(snapshot) !== 'choice') return;
  if (snapshot.workspace?.phase === 'loading') return;
  saveStart(starterInputs.filter(input => input.checked && !attemptedChannels.has(input.value)).map(input => input.value));
  startFailure = '';
  // Start after a failed reference preparation is also its retry.
  if (noticeError) {retries = 0; retriedRevision = null; noticeError = null; await prepare();}
  render(snapshot);
  await load();
});

async function beginWorkspace(urls) {
  const selected = urls.map(url => starterInputs.find(input => input.value === url)).filter(Boolean);
  enteringWorkspace = true; render(snapshot);
  try {
    // Save the chosen work before dispatching adds. A reload never blindly replays them.
    render(await request('/setup/workspace/start', {channels: selected.map(input => input.value)}));
    saveStart(null);
    await addSelectedChannels(selected);
  } catch (error) {startFailure = 'The start was not confirmed. Check again before retrying.';}
  finally {enteringWorkspace = false; render(snapshot);}
  if (snapshot?.workspace?.phase === 'loading') await checkWorkspace();
}

async function checkWorkspace() {
  if (checkingWorkspace) return;
  checkingWorkspace = true;
  try {
    const entries = Object.entries(snapshot.workspace.channels);
    hydration.clear();
    if (apiReady()) {
      await Promise.all(entries.filter(([, id]) => id).map(async ([, id]) => {
        hydration.set(id, await request(`/channels/${id}/hydration`).catch(() => null));
      }));
    }
    render(snapshot);
    if (apiReady() && !finishAttempted && !addingChannels
        && entries.every(([, id]) => id && initialComplete(hydration.get(id)))) {
      finishAttempted = true;
      const result = await request('/starter-channels/finish', {});
      if (result.ready === true) location.assign('/app-explorer-v3');
      else finishAttempted = false;
    }
  } catch (error) {startFailure = 'Completion was not confirmed. Check again; your saved work is unchanged.';}
  finally {checkingWorkspace = false; if (snapshot) render(snapshot);}
}
async function retryAdd(input) {
  if (!input || actionBusy || addingChannels) return;
  actionBusy = true; render(snapshot);
  try {recordAddedChannel(input.value, await request('/starter-channels/add', {url: input.value}));}
  catch (error) {startFailure = 'Adding the channel was not confirmed. Check again before retrying.';}
  finally {actionBusy = false; await load();}
}
async function continueChannel(id, value) {
  if (actionBusy || addingChannels) return;
  actionBusy = true; render(snapshot);
  const key = `${id}:${value.job?.job_id || value.job?.id || ''}:${value.job?.updated_at || ''}`;
  if (retryRequests.get(id)?.key !== key) retryRequests.set(id, {key, token: crypto.randomUUID()});
  try {
    await request(`/channels/${id}/hydration`, {confirmed: true, expected_items: value.estimate.items,
      expected_pages: value.estimate.pages, expected_quota_units: value.estimate.quota_units,
      retry_request_id: retryRequests.get(id).token});
  } catch (error) {startFailure = 'Continuing was not confirmed. Check again before trying again.';}
  finally {actionBusy = false; await load();}
}
$('loading-recheck').addEventListener('click', async () => {
  if (addingChannels || actionBusy || checkingWorkspace) return;
  finishAttempted = false; startFailure = ''; await load();
});
$('prepare').addEventListener('click', () => {retries = 0; retriedRevision = null; noticeError = null; void prepare();});
async function poll() {
  clearTimeout(timer);
  if (!document.hidden && !pending && !addingChannels) await load();
  const busy = snapshot?.workspace?.phase === 'loading' || snapshot?.phase === 'preparing' || savedStart()
    || (snapshot?.prepared && !apiReady()) || !snapshot?.configured;
  timer = setTimeout(poll, busy ? 3000 : 10000);
}
document.addEventListener('visibilitychange', () => {if (!document.hidden) poll();});
window.addEventListener('pagehide', () => clearTimeout(timer));
poll();
