const el = id => document.getElementById(id);
let state = null, creating = false, pending = false;
const messages = {
  rate_limited: 'Too many attempts. Wait one minute and try again.',
  invalid_credentials: 'The username or password is incorrect.',
  invalid_setup_code: 'The code is invalid or has already been used. Follow the instructions below to generate a new one.',
  setup_code_expired: 'Your code has expired. Follow the instructions below to generate a new one.',
  setup_session_expired: 'Your setup session has expired. Generate a new code using the instructions below.',
  setup_closed: 'This installation is already configured. Reload this page.',
  local_access_requires_loopback: 'Access without an account is available only at a local address.',
  owner_access_unavailable: 'Access is temporarily unavailable. Retry shortly.',
  owner_access_not_configured: 'Complete the installation in the terminal.',
  invalid_auth_request: 'Check the fields and try again.'
};
async function request(path, value) {
  const response = await fetch(path, {method: value === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
    headers: value === undefined ? {} : {'Content-Type': 'application/json', 'X-LIC-Auth': '1'},
    ...(value === undefined ? {} : {body: JSON.stringify(value)})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.code || 'request_failed');
  return result;
}
function render() {
  const initial = !state.configured, verified = initial && state.setup_verified;
  el('heading').textContent = state.authenticated ? 'Your workspace' : initial ? 'Set up access' : 'Welcome back';
  el('intro').textContent = state.authenticated ? (state.access_mode === 'local' ? 'This installation opens without an account.' : `Signed in as ${state.username}.`) : initial ? 'First confirm your installation, then choose how to access it.' : 'Sign in to your workspace.';
  el('claim').hidden = !initial || verified;
  el('renew').hidden = !initial || verified;
  el('choice').hidden = !verified || creating;
  el('form').hidden = state.authenticated || (initial && (!verified || !creating));
  el('account').hidden = !state.authenticated;
  el('logout').hidden = state.access_mode === 'local';
  el('confirm-field').hidden = !creating; el('confirm').required = creating;
  el('password').autocomplete = creating ? 'new-password' : 'current-password';
  el('back').hidden = !creating;
  el('submit').textContent = creating ? 'Create account' : 'Sign in';
  if (state.setup_expired) el('message').textContent = messages.setup_code_expired;
}
function busy(value) {
  pending = value;
  for (const id of ['claim-submit','choose-password','choose-local','submit','back','logout']) el(id).disabled = value;
}
async function fail(error) {
  if (error.message === 'setup_session_expired') {creating = false; await load();}
  el('message').textContent = messages[error.message] || 'Unable to complete this request. Try again.';
}
async function load() {
  try {state = await request('/auth/session'); render();} catch (error) {el('message').textContent = messages[error.message] || 'Unable to load access settings.';}
}
el('claim').addEventListener('submit', async event => {
  event.preventDefault(); if (pending) return; busy(true); el('message').textContent = '';
  const body = {setup_code: el('setup').value.trim()};
  try {await request('/auth/claim', body); creating = false; await load();}
  catch (error) {await fail(error);}
  finally {body.setup_code = ''; el('setup').value = ''; busy(false);}
});
el('choose-password').addEventListener('click', () => {creating = true; render();});
el('back').addEventListener('click', () => {creating = false; el('password').value = el('confirm').value = ''; render();});
el('choose-local').addEventListener('click', async () => {
  if (pending) return; busy(true);
  try {await request('/auth/complete', {mode: 'local'}); window.location.replace('/setup');}
  catch (error) {await fail(error);} finally {busy(false);}
});
el('form').addEventListener('submit', async event => {
  event.preventDefault(); if (pending) return; el('message').textContent = '';
  if (creating && el('password').value !== el('confirm').value) {el('message').textContent = 'Passwords do not match.'; return;}
  const body = {username: el('username').value, password: el('password').value};
  if (creating) body.mode = 'password'; busy(true);
  try {await request(creating ? '/auth/complete' : '/auth/login', body); window.location.replace(creating ? '/setup' : '/app-explorer-v3');}
  catch (error) {await fail(error);}
  finally {body.password = ''; el('password').value = el('confirm').value = ''; busy(false);}
});
el('logout').addEventListener('click', async () => {
  if (pending) return; busy(true);
  try {await request('/auth/logout', {}); window.location.replace('/auth');}
  catch (error) {await fail(error);} finally {busy(false);}
});
load();
