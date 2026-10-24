import { normalizeAppOrigin, permissionPattern } from './shared.js';

const form = document.querySelector('#connection-form');
const input = document.querySelector('#app-origin');
const save = document.querySelector('#save');
const open = document.querySelector('#open-app');
const status = document.querySelector('#status');
let savedOrigin = null;

function tell(message, error = false) {
  status.textContent = message;
  status.dataset.error = String(error);
}

async function openApp(origin) {
  await chrome.tabs.create({ url: `${origin}/auth` });
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const origin = normalizeAppOrigin(input.value);
  if (!origin) {
    tell('Enter a local address such as http://127.0.0.1:8002, without a page path.', true);
    return;
  }
  save.disabled = true;
  try {
    // Request in this explicit user gesture; no permission is granted on install.
    const granted = await chrome.permissions.request({ origins: [permissionPattern(origin)] });
    if (!granted) {
      tell('Access was not allowed. Your saved address has not changed.', true);
      return;
    }
    await chrome.storage.local.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' });
    await chrome.storage.local.set({ appOrigin: origin });
    savedOrigin = origin;
    input.value = origin;
    open.hidden = false;
    tell('Address saved. Keep the Leti Cook tab open, sign in if needed, then return to YouTube and reload the page.');
    await openApp(origin);
  } catch {
    tell('Could not finish connecting. Check the address and browser permissions, then try again.', true);
  } finally {
    save.disabled = false;
  }
});

open.addEventListener('click', async () => {
  if (!savedOrigin) return;
  try { await openApp(savedOrigin); }
  catch { tell('Could not open Leti Cook. Open the saved address in a browser tab.', true); }
});

try {
  const data = await chrome.storage.local.get('appOrigin');
  savedOrigin = normalizeAppOrigin(data.appOrigin);
  if (savedOrigin) {
    input.value = savedOrigin;
    open.hidden = false;
  }
} catch {
  tell('Could not read the saved address. Enter it again.', true);
}
