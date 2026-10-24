/* Real Chromium smoke with a disposable profile and synthetic pages only.
 * LIC_PLAYWRIGHT_MODULE and LIC_CHROMIUM_EXECUTABLE must be absolute local paths.
 * No dependency/browser downloads; no user profile or live installation access.
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const crypto = require('node:crypto');

if (process.argv.includes('--help')) {
  console.log('Set LIC_PLAYWRIGHT_MODULE to an absolute installed playwright package path and LIC_CHROMIUM_EXECUTABLE to a local Chromium/Chrome executable; run this file with Node. Uses only a disposable profile and loopback fixture.');
  process.exit(0);
}

const root = path.resolve(__dirname, '..');
const extensionSource = path.join(root, 'extensions', 'chromium');
const CHANNEL_ID = `UC${'a'.repeat(22)}`;
const WATCH_ID = 'fixture0001';
const SHORTS_ID = 'fixture0002';
const FIXTURE_STYLE = `<style>
body{margin:40px;font:14px Arial,sans-serif;background:#fafafa;color:#222}
h1{font-size:22px} p{color:#555} section{margin-top:28px;padding:24px;border:1px solid #ccc;border-radius:12px;background:white}
ytd-browse,yt-page-header-renderer,ytd-watch-flexy,ytd-watch-metadata{display:block}
yt-flexible-actions-view-model,#owner{display:flex;align-items:center}
.ytFlexibleActionsViewModelAction,button-view-model,#subscribe-button{display:block}
ytd-subscribe-button-renderer{display:flex}
ytd-video-owner-renderer{display:block;margin-right:24px}
button[aria-label="Subscribe"]{box-sizing:border-box;height:36px;border:0;border-radius:18px;padding:0 16px;background:#111;color:white;font:500 14px Arial,sans-serif}
</style>`;
const YOUTUBE_HTML = `<!doctype html><meta charset="utf-8"><title>Synthetic channel fixture</title>${FIXTURE_STYLE}
<h1>Synthetic channel fixture</h1><p>Offline layout test. This is not a live YouTube page.</p><section>
<ytd-browse page-subtype="channels"><yt-page-header-renderer><h2>Fixture channel</h2>
<yt-flexible-actions-view-model><div class="ytFlexibleActionsViewModelAction"><button-view-model>
<button aria-label="Subscribe">Subscribe</button></button-view-model></div></yt-flexible-actions-view-model>
</yt-page-header-renderer></ytd-browse></section>`;
const WATCH_HTML = `<!doctype html><meta charset="utf-8"><title>Synthetic watch fixture</title>${FIXTURE_STYLE}
<h1>Synthetic watch fixture</h1><p>Offline layout test. This is not a live YouTube page.</p><section>
<ytd-watch-flexy video-id="${WATCH_ID}"><ytd-watch-metadata><h2>Fixture video</h2><div id="owner">
<ytd-video-owner-renderer><div id="channel-name"><a href="/@fixture-watch">Fixture author</a></div></ytd-video-owner-renderer>
<div id="subscribe-button"><ytd-subscribe-button-renderer><button aria-label="Subscribe">Subscribe</button></ytd-subscribe-button-renderer></div>
</div></ytd-watch-metadata></ytd-watch-flexy></section>`;
const SIGNED_CHANNEL_HTML = `<!doctype html><meta charset="utf-8"><title>Synthetic subscribed channel fixture</title>${FIXTURE_STYLE}
<style>
yt-flexible-actions-view-model{align-items:stretch}
yt-subscribe-button-view-model{display:flex}
button[aria-label="Subscribed"]{box-sizing:border-box;height:40px;border:0;border-radius:20px;padding:0 16px;background:#eee;color:#111;font:500 14px Arial,sans-serif}
#vidiq-action{margin-left:8px} #vidiq-button{box-sizing:border-box;height:100%;min-height:40px;border:0;border-radius:20px;padding:0 16px;background:#3779d9;color:white}
</style><h1>Synthetic subscribed channel fixture</h1><p>Offline regression test. This is not a live YouTube page.</p><section>
<ytd-browse page-subtype="channels"><yt-page-header-renderer><h2>Fixture subscribed channel</h2>
<yt-flexible-actions-view-model><div class="ytFlexibleActionsViewModelAction"><yt-subscribe-button-view-model>
<button aria-label="Subscribed">Subscribed</button></yt-subscribe-button-view-model></div>
<div id="vidiq-action" class="ytFlexibleActionsViewModelAction"><button id="vidiq-button">vidIQ fixture</button></div>
</yt-flexible-actions-view-model></yt-page-header-renderer></ytd-browse></section>
<script>
window.__fixtureBaseline = {
  beforeInjection: !document.querySelector('.leti-cook-channel-control'),
  subscribeHeight: document.querySelector('button[aria-label="Subscribed"]').getBoundingClientRect().height,
  vidiqHeight: document.querySelector('#vidiq-button').getBoundingClientRect().height,
  headerHeight: document.querySelector('yt-page-header-renderer').getBoundingClientRect().height
};
</script>`;
const SHORTS_HTML = `<!doctype html><meta charset="utf-8"><title>Synthetic Shorts fixture</title>${FIXTURE_STYLE}
<style>
ytd-shorts,ytd-reel-video-renderer{display:block}
yt-reel-channel-bar-view-model{display:flex;align-items:center}
yt-reel-channel-bar-view-model>a{margin-right:20px}
.ytReelChannelBarViewModelReelSubscribeButton{display:block}
reel-action-bar-view-model{display:flex;flex-direction:column;align-items:center;width:48px;margin-left:auto}
</style><h1>Synthetic Shorts fixture</h1><p>Offline owner and layout test. This is not a live YouTube page.</p><section>
<ytd-shorts role="main"><ytd-reel-video-renderer><yt-reel-channel-bar-view-model>
<a href="/@fixture-shorts/shorts">Fixture Shorts author</a>
<div class="ytReelChannelBarViewModelReelSubscribeButton"><button-view-model>
<button aria-label="Subscribe">Subscribe</button></button-view-model></div>
</yt-reel-channel-bar-view-model><reel-action-bar-view-model><button aria-label="Like">Like</button>
</reel-action-bar-view-model></ytd-reel-video-renderer></ytd-shorts></section>`;

function themedChannel(dark) {
  const variables = dark
    ? '--yt-spec-badge-chip-background:#272727;--yt-spec-text-primary:#f1f1f1;--yt-spec-button-chip-background-hover:#3f3f3f'
    : '--yt-spec-badge-chip-background:#f2f2f2;--yt-spec-text-primary:#0f0f0f;--yt-spec-button-chip-background-hover:#e5e5e5';
  const theme = `<style>:root{${variables}}body{background:${dark ? '#0f0f0f' : '#fafafa'};color:var(--yt-spec-text-primary)}
section{background:${dark ? '#181818' : '#fff'};border-color:${dark ? '#444' : '#ccc'}}
p{color:${dark ? '#aaa' : '#555'}}button[aria-label="Subscribed"]{background:var(--yt-spec-badge-chip-background);color:var(--yt-spec-text-primary)}</style>`;
  return SIGNED_CHANNEL_HTML.replace('<!doctype html>', `<!doctype html><html${dark ? ' dark' : ''}>`).replace('<script>', `${theme}<script>`);
}

async function visualState(page) {
  return page.evaluate(() => {
    const button = document.querySelector('.leti-cook-channel-control').shadowRoot.querySelector('button');
    const check = button.querySelector('.leti-cook-check');
    const plus = button.querySelector('.leti-cook-plus');
    return {
      state: button.dataset.state,
      label: button.querySelector('.leti-cook-button-label')?.textContent,
      height: button.getBoundingClientRect().height,
      background: getComputedStyle(button).backgroundColor,
      color: getComputedStyle(button).color,
      checkOpacity: check ? Number(getComputedStyle(check).opacity) : null,
      plusTransform: plus ? getComputedStyle(plus).transform : null,
      buttonTransform: getComputedStyle(button).transform,
      animations: button.getAnimations({ subtree: true }).map(animation => ({
        playState: animation.playState,
        duration: animation.effect.getTiming().duration,
        iterations: animation.effect.getTiming().iterations,
      })),
      neighbors: {
        subscribeHeight: document.querySelector('button[aria-label="Subscribed"]').getBoundingClientRect().height,
        vidiqHeight: document.querySelector('#vidiq-button').getBoundingClientRect().height,
        headerHeight: document.querySelector('yt-page-header-renderer').getBoundingClientRect().height,
      },
    };
  });
}

function sha(bytes) { return crypto.createHash('sha256').update(bytes).digest('hex'); }
function delay(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }
async function until(predicate, label, timeout = 7000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await delay(30);
  }
  throw new Error(`timeout:${label}`);
}
function requiredLocalPath(name) {
  const value = process.env[name];
  assert(value && path.isAbsolute(value) && fs.existsSync(value), `${name}:absolute_existing_path_required`);
  return value;
}
function copyExtension(source, destination) {
  const files = [];
  for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
    const from = path.join(source, entry.name);
    const to = path.join(destination, entry.name);
    assert(!entry.isSymbolicLink(), 'extension_symlink_refused');
    if (entry.isDirectory()) {
      fs.mkdirSync(to);
      files.push(...copyExtension(from, to));
    } else {
      assert(entry.isFile(), 'extension_special_file_refused');
      fs.copyFileSync(from, to);
      assert.equal(sha(fs.readFileSync(from)), sha(fs.readFileSync(to)));
      files.push({ source: from, copy: to, hash: sha(fs.readFileSync(from)) });
    }
  }
  return files;
}

async function main() {
  const playwright = require(requiredLocalPath('LIC_PLAYWRIGHT_MODULE'));
  const executablePath = requiredLocalPath('LIC_CHROMIUM_EXECUTABLE');
  const artifactDirectory = process.env.LIC_EXTENSION_SMOKE_ARTIFACT_DIR;
  if (artifactDirectory) {
    assert(path.isAbsolute(artifactDirectory), 'artifact_directory_absolute_required');
    fs.mkdirSync(artifactDirectory, { recursive: true });
  }
  for (const name of ['manifest.json', 'background.js', 'bridge.js', 'shared.js', 'content.js', 'options.js']) {
    assert(fs.existsSync(path.join(extensionSource, name)), `extension_file_missing:${name}`);
  }
  const tempRoot = fs.realpathSync(os.tmpdir());
  const temporary = fs.mkdtempSync(path.join(tempRoot, 'leti-cook-extension-test-'));
  const extension = path.join(temporary, 'extension');
  const profile = path.join(temporary, 'profile');
  fs.mkdirSync(extension);
  const exactFiles = copyExtension(extensionSource, extension);
  const manifestPath = path.join(extension, 'manifest.json');
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  // Headless Chrome cannot accept its native optional-host permission prompt.
  // Only the temporary manifest grants loopback access; runtime bytes stay exact.
  manifest.host_permissions = ['http://127.0.0.1/*'];
  fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);

  const records = [];
  const blocked = [];
  const cases = [];
  const state = { mode: 'local', result: 'added', delayMs: 0 };
  let context;
  let origin;
  let timer;
  const server = http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url, origin);
      let raw = '';
      for await (const chunk of request) {
        raw += chunk.toString('utf8');
        if (raw.length > 4096) throw new Error('fixture_body_bound');
      }
      const row = { method: request.method, path: url.pathname, observedAt: Date.now(), socketPort: request.socket.remotePort,
        origin: request.headers.origin || null, cookie: request.headers.cookie || '',
        fetchSite: request.headers['sec-fetch-site'] || null, body: raw ? JSON.parse(raw) : null };
      records.push(row);
      response.setHeader('Cache-Control', 'no-store');
      response.setHeader('Content-Type', 'application/json');
      if (url.pathname === '/auth/session') {
        response.end(JSON.stringify({ configured: true, authenticated: state.mode === 'local' || row.cookie.includes('fixture_session=ok'), access_mode: state.mode }));
      } else if (url.pathname === '/add-channel') {
        if (row.origin !== origin || row.fetchSite !== 'same-origin') {
          response.writeHead(403); response.end(JSON.stringify({ code: 'cross_origin_denied' }));
        } else if (state.mode === 'password' && !row.cookie.includes('fixture_session=ok')) {
          response.writeHead(401); response.end(JSON.stringify({ code: 'authentication_required' }));
        } else if (state.result === 'rate_limited') {
          response.writeHead(429); response.end(JSON.stringify({ code: 'channel_add_rate_limited' }));
        } else if (state.result === 'lost_ack') {
          // Headers/body reached the browser, but the full acknowledgement did not.
          response.setHeader('Content-Length', '128');
          response.flushHeaders();
          response.write('{"status":');
          setTimeout(() => response.destroy(), 20);
        } else if (state.result === 'lost_ack_early') {
          // Chrome itself may retry when no response headers reached the browser.
          response.destroy();
        } else {
          if (state.delayMs) await delay(state.delayMs);
          response.end(JSON.stringify({ status: state.result, channel: { channel_id: CHANNEL_ID } }));
        }
      } else if (url.pathname === '/fixture-login') {
        response.setHeader('Set-Cookie', 'fixture_session=ok; Path=/; SameSite=Strict; HttpOnly');
        response.end('{}');
      } else if (url.pathname === '/trap.js') {
        response.setHeader('Content-Type', 'application/javascript');
        response.end('window.__mainFetchCalls=0;window.fetch=()=>{window.__mainFetchCalls++;throw new Error("main_world_fetch_must_not_be_used")};');
      } else {
        response.setHeader('Content-Type', 'text/html; charset=utf-8');
        response.setHeader('Content-Security-Policy', "default-src 'none'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'");
        response.end('<!doctype html><title>Synthetic Leti Cook</title><script src="/trap.js"></script><h1>Synthetic Leti Cook</h1>');
      }
    } catch {
      response.writeHead(500); response.end('{}');
    }
  });
  try {
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
    origin = `http://127.0.0.1:${server.address().port}`;
    context = await playwright.chromium.launchPersistentContext(profile, {
      executablePath, headless: true, timeout: 15000,
      ignoreDefaultArgs: ['--disable-extensions'],
      args: ['--enable-unsafe-extension-debugging', '--disable-background-networking', '--disable-component-update',
        '--disable-domain-reliability', '--no-first-run', '--no-default-browser-check',
        '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'],
    });
    timer = setTimeout(() => { void context.close(); server.closeAllConnections(); server.close(); }, 60000);
    timer.unref();
    context.setDefaultTimeout(7000);
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin === origin) return route.continue();
      if (url.origin === 'https://www.youtube.com' && url.pathname.startsWith('/@fixture-') && route.request().isNavigationRequest()) {
        const body = url.pathname === '/@fixture-signed-in-layout' ? themedChannel(false)
          : url.pathname === '/@fixture-dark-reduced-motion' ? themedChannel(true) : YOUTUBE_HTML;
        return route.fulfill({ status: 200, contentType: 'text/html', body });
      }
      if (url.origin === 'https://www.youtube.com' && url.pathname === '/watch' && url.searchParams.get('v') === WATCH_ID && route.request().isNavigationRequest()) {
        return route.fulfill({ status: 200, contentType: 'text/html', body: WATCH_HTML });
      }
      if (url.origin === 'https://www.youtube.com' && url.pathname === `/shorts/${SHORTS_ID}` && route.request().isNavigationRequest()) {
        return route.fulfill({ status: 200, contentType: 'text/html', body: SHORTS_HTML });
      }
      if (url.protocol === 'chrome-extension:') return route.continue();
      blocked.push({ method: route.request().method(), origin: url.origin });
      return route.abort('blockedbyclient');
    });
    const cdp = await context.browser().newBrowserCDPSession();
    const installed = await cdp.send('Extensions.loadUnpacked', { path: extension });
    assert.match(installed.id, /^[a-p]{32}$/);
    const listed = await cdp.send('Extensions.getExtensions');
    assert(listed.extensions.some(item => item.id === installed.id && item.enabled));
    const options = await context.newPage();
    await options.goto(`chrome-extension://${installed.id}/options.html`);
    await options.locator('#app-origin').fill(origin);
    await options.locator('#save').click();
    await until(async () => (await options.locator('#status').innerText()).startsWith('Connected.'), 'options_connected');
    await until(() => context.pages().some(page => page.url().startsWith(`${origin}/`)), 'app_tab_opened');
    if (artifactDirectory) await options.screenshot({ path: path.join(artifactDirectory, 'synthetic-options.png') });

    async function scenario(name, result, expectedText, expectedPosts = 1) {
      state.result = result;
      const visual = name === 'signed-in-layout' || name === 'dark-reduced-motion';
      const reduced = name === 'dark-reduced-motion';
      state.delayMs = visual ? 1800 : 0;
      const before = records.filter(row => row.path === '/add-channel').length;
      const beforeAuth = records.filter(row => row.path === '/auth/session').length;
      const page = await context.newPage();
      if (visual) await page.emulateMedia({ reducedMotion: reduced ? 'reduce' : 'no-preference', colorScheme: reduced ? 'dark' : 'light' });
      const pageUrl = name === 'watch' ? `https://www.youtube.com/watch?v=${WATCH_ID}`
        : name === 'shorts' ? `https://www.youtube.com/shorts/${SHORTS_ID}` : `https://www.youtube.com/@fixture-${name}`;
      await page.goto(pageUrl);
      const button = page.locator('.leti-cook-add-channel');
      await button.waitFor({ state: 'visible' });
      await until(async () => (await button.getAttribute('title')).includes('tracking status unavailable'), `${name}:passive_lookup_settled`);
      assert.equal(records.filter(row => row.path === '/add-channel').length, before, `${name}:no_automatic_add`);
      const subscribeBox = await page.locator('button[aria-label="Subscribe"],button[aria-label="Subscribed"]').boundingBox();
      const addBox = await button.boundingBox();
      assert(subscribeBox && addBox, `${name}:visible_controls`);
      let preservedLayout;
      let design;
      if (visual) {
        preservedLayout = await page.evaluate(() => ({
          before: window.__fixtureBaseline,
          after: {
            subscribeHeight: document.querySelector('button[aria-label="Subscribed"]').getBoundingClientRect().height,
            vidiqHeight: document.querySelector('#vidiq-button').getBoundingClientRect().height,
            headerHeight: document.querySelector('yt-page-header-renderer').getBoundingClientRect().height,
          },
        }));
        assert(preservedLayout.before.beforeInjection, `${name}:baseline_before_injection`);
        if (artifactDirectory) {
          fs.writeFileSync(path.join(artifactDirectory, `${name}-measurements.json`), `${JSON.stringify(preservedLayout, null, 2)}\n`);
        }
        for (const dimension of ['subscribeHeight', 'vidiqHeight', 'headerHeight']) {
          assert.equal(preservedLayout.after[dimension], preservedLayout.before[dimension], `${name}:${dimension}_unchanged`);
        }
        design = { theme: reduced ? 'dark' : 'light', reducedMotion: reduced, idle: await visualState(page) };
        assert.equal(design.idle.label, 'Leti Cook', `${name}:idle_label`);
        assert.equal(design.idle.state, 'idle', `${name}:idle_state`);
        assert.equal(design.idle.checkOpacity, 0, `${name}:no_success_before_request`);
        assert.equal(design.idle.height, 34, `${name}:circle_height`);
        assert.equal(await button.locator('svg.leti-cook-glyph[aria-hidden="true"]').count(), 1, `${name}:inline_status_glyph`);
        if (artifactDirectory) await page.screenshot({ path: path.join(artifactDirectory, `${design.theme}-idle.png`) });
        await button.hover();
        await delay(200);
        const hovered = await visualState(page);
        if (reduced) assert.equal(hovered.buttonTransform, 'none', `${name}:no_hover_motion`);
        else assert.notEqual(hovered.buttonTransform, 'none', `${name}:hover_lift`);
      }
      if (name === 'shorts') {
        const rail = await page.locator('reel-action-bar-view-model').boundingBox();
        assert.equal(await page.locator('reel-action-bar-view-model > .leti-cook-channel-control').count(), 1);
        assert(Math.abs(addBox.x + addBox.width / 2 - rail.x - rail.width / 2) <= 1, 'shorts:centered_in_rail');
      } else {
        assert(addBox.x > subscribeBox.x + subscribeBox.width, `${name}:button_after_subscribe_horizontally`);
        assert(Math.abs(addBox.y + addBox.height / 2 - subscribeBox.y - subscribeBox.height / 2) <= 2, `${name}:buttons_same_row`);
      }
      await button.click();
      if (visual) {
        await until(async () => await button.getAttribute('data-state') === 'pending', `${name}:pending_state`);
        design.pending = await visualState(page);
        assert.equal(design.pending.checkOpacity, 0, `${name}:no_success_before_ack`);
        assert.equal(design.pending.label, 'Adding…', `${name}:pending_label`);
        assert(await button.isDisabled(), `${name}:pending_disabled`);
        if (reduced) {
          assert.equal(design.pending.plusTransform, 'none', `${name}:pending_no_transform`);
          assert.equal(design.pending.animations.length, 0, `${name}:pending_no_animation`);
        } else {
          assert(design.pending.animations.some(animation => animation.duration === 1100 && animation.iterations === Infinity), `${name}:pending_animation`);
        }
        if (artifactDirectory) await page.screenshot({ path: path.join(artifactDirectory, `${design.theme}-pending.png`) });
        await delay(750);
        const settledPending = await visualState(page);
        assert.equal(settledPending.state, 'pending', `${name}:still_waiting_for_fixture_ack`);
        assert.equal(settledPending.height, 34, `${name}:pending_size_stable`);
      }
      await until(async () => (await button.textContent()) === expectedText || (await page.locator('.leti-cook-channel-message').textContent()).includes(expectedText), name);
      await delay(250);
      if (visual) {
        design.success = await visualState(page);
        assert.equal(design.success.state, 'success', `${name}:success_after_ack`);
        assert.equal(design.success.checkOpacity, 1, `${name}:success_check`);
        for (const phase of ['idle', 'pending', 'success']) {
          assert.equal(design[phase].height, 34, `${name}:${phase}_height`);
          assert.deepEqual(design[phase].neighbors, preservedLayout.after, `${name}:${phase}_neighbors_unchanged`);
        }
        if (artifactDirectory) await page.screenshot({ path: path.join(artifactDirectory, `${design.theme}-success.png`) });
      }
      const additions = records.filter(row => row.path === '/add-channel').slice(before);
      const authRequests = records.filter(row => row.path === '/auth/session').length - beforeAuth;
      assert.equal(authRequests, 2, `${name}:passive_read_and_explicit_command_auth_checks`);
      if (expectedPosts === null) assert(additions.length >= 1, `${name}:transport_attempt_observed`);
      else assert.equal(additions.length, expectedPosts, `${name}:post_count`);
      for (const row of additions) {
        assert.equal(row.method, 'POST');
        assert.equal(row.origin, origin, `${name}:same_origin`);
        assert.equal(row.fetchSite, 'same-origin', `${name}:same_origin_fetch`);
        assert.deepEqual(row.body, { url: `https://www.youtube.com/@fixture-${name}` }, `${name}:channel_payload`);
      }
      if (result.startsWith('lost_ack')) {
        assert(await button.isDisabled(), 'uncertain_result_disables_replay');
        await delay(500);
        assert.equal(records.filter(row => row.path === '/add-channel').length, before + additions.length,
          `${name}:no_application_retry_after_uncertain_result`);
      }
      for (const app of context.pages().filter(item => item.url().startsWith(`${origin}/auth`))) {
        assert.equal(await app.evaluate(() => window.__mainFetchCalls), 0, `${name}:isolated_world`);
      }
      cases.push({ name, trustedClicks: 1, authRequests, posts: additions.length,
        sameOrigin: additions.every(row => row.origin === origin),
        horizontalBesideSubscribe: name !== 'shorts',
        ...(preservedLayout ? { preservedLayout } : {}),
        ...(design ? { design } : {}),
        cookieSent: additions.some(row => row.cookie.includes('fixture_session=ok')),
        ...(result.startsWith('lost_ack') ? { result: 'unconfirmed', automaticApplicationRetry: false,
          transportAttemptOffsetsMs: additions.map(row => row.observedAt - additions[0].observedAt),
          distinctConnections: new Set(additions.map(row => row.socketPort)).size } : {}) });
      if (artifactDirectory && name === 'local') await page.screenshot({ path: path.join(artifactDirectory, 'synthetic-channel-button.png') });
      if (artifactDirectory && name === 'watch') await page.screenshot({ path: path.join(artifactDirectory, 'synthetic-watch-button.png') });
      if (artifactDirectory && name === 'shorts') await page.screenshot({ path: path.join(artifactDirectory, 'synthetic-shorts-button.png') });
      await page.close();
      return additions;
    }

    await scenario('signed-in-layout', 'added', 'Added');
    await scenario('dark-reduced-motion', 'added', 'Added');
    assert.equal(cases[0].design.idle.background, 'rgb(99, 223, 198)', 'turquoise_surface');
    assert.equal(cases[0].design.idle.background, cases[1].design.idle.background, 'brand_surface_in_both_themes');
    assert.equal(cases[0].design.idle.color, cases[1].design.idle.color, 'contrasting_icon_in_both_themes');
    const local = await scenario('local', 'added', 'Added');
    assert(local.every(row => !row.cookie), 'local_mode_does_not_require_password_cookie');
    await scenario('watch', 'added', 'Added');
    await scenario('shorts', 'added', 'Added');
    state.mode = 'password';
    await scenario('signed-out', 'added', 'Sign in to Leti Cook', 0);
    const login = await context.newPage();
    await login.goto(`${origin}/fixture-login`);
    await login.close();
    const password = await scenario('password', 'exists', 'Already added');
    assert(password.every(row => row.cookie.includes('fixture_session=ok')), 'password_cookie_sent');
    await scenario('rate-limit', 'rate_limited', 'Too many additions');
    await scenario('lost-ack', 'lost_ack', 'The result is unknown');
    // Reset only this disposable extension's synthetic pending intent between
    // independent transport-fault scenarios; production runtime files stay exact.
    await until(() => context.serviceWorkers().some(worker => worker.url().startsWith(`chrome-extension://${installed.id}/`)), 'extension_worker');
    const worker = context.serviceWorkers().find(item => item.url().startsWith(`chrome-extension://${installed.id}/`));
    await worker.evaluate(() => chrome.storage.session.remove('channelAddIntent'));
    await scenario('lost-ack-before-headers', 'lost_ack_early', 'The result is unknown', null);
    for (const item of exactFiles.filter(item => path.basename(item.source) !== 'manifest.json')) {
      assert.equal(sha(fs.readFileSync(item.copy)), item.hash, 'runtime_file_changed');
    }
    const result = { status: 'passed', cases, realBrowser: context.browser().version(),
      nativePermissionPromptTested: false, fixtureGrantedLoopbackPermission: true,
      exactlyOnceHttpGuaranteed: false,
      runtimeFilesUnchanged: true, pageExternalRequestsSent: 0, blockedExternalRequests: blocked.length,
      liveInstallationUsed: false, youtubeRequestsSent: 0 };
    if (artifactDirectory) fs.writeFileSync(path.join(artifactDirectory, 'browser-smoke.json'), `${JSON.stringify(result, null, 2)}\n`);
    console.log(JSON.stringify(result, null, 2));
  } finally {
    clearTimeout(timer);
    if (context) await context.close().catch(() => {});
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    // Never remove any path other than this exact fresh temporary directory.
    assert.equal(path.dirname(temporary), tempRoot);
    assert(path.basename(temporary).startsWith('leti-cook-extension-test-'));
    assert.equal(fs.realpathSync(temporary), temporary);
    assert(!fs.lstatSync(temporary).isSymbolicLink());
    fs.rmSync(temporary, { recursive: true, force: false, maxRetries: 3, retryDelay: 200 });
  }
}

main().catch(error => { console.error(`chromium_extension_smoke_failed: ${error.message}`); process.exitCode = 1; });
