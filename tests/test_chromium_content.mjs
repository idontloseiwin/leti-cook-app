import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source = readFileSync(new URL('../extensions/chromium/content.js', import.meta.url), 'utf8');
const VIDEO = 'abcdefghijk';
const NEXT_VIDEO = 'lmnopqrstuv';
const CHANNEL = 'UC' + 'a'.repeat(22);

// Small independent DOM fixture: selectors traverse a tree, rather than map
// production selector strings directly to canned answers. No browser/API I/O.
function simpleMatch(node, part) {
  const attributes = [...part.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)];
  if (attributes.some(([, name, value]) => !node.attributes.has(name)
      || (value !== undefined && node.getAttribute(name) !== value))) return false;
  const bare = part.replace(/\[[^\]]+\]/g, '');
  const tag = /^[\w-]+/.exec(bare)?.[0];
  if (tag && node.tagName !== tag) return false;
  const id = /#([\w-]+)/.exec(bare)?.[1];
  if (id && node.getAttribute('id') !== id) return false;
  for (const [, name] of bare.matchAll(/\.([\w-]+)/g)) {
    if (!node.className.split(/\s+/).includes(name)) return false;
  }
  return true;
}
function matches(node, selector) {
  const parts = selector.trim().split(/\s+/);
  if (!simpleMatch(node, parts.pop())) return false;
  let ancestor = node.parentElement;
  while (parts.length) {
    const part = parts.pop();
    while (ancestor && !simpleMatch(ancestor, part)) ancestor = ancestor.parentElement;
    if (!ancestor) return false;
    ancestor = ancestor.parentElement;
  }
  return true;
}
class Element {
  constructor(tag, attributes = {}) {
    this.tagName = tag;
    this.attributes = new Map(Object.entries(attributes));
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
    this.style = {};
    this.textContent = '';
    this.disabled = false;
    this.hidden = false;
    this.box = {top: 0, left: 0, bottom: 40, right: 100};
  }
  get textContent() { return this._textContent + this.children.map(child => child.textContent).join(''); }
  set textContent(value) {
    this._textContent = value;
    for (const child of this.children) child.parentElement = null;
    this.children = [];
  }
  get className() { return this.getAttribute('class') || ''; }
  set className(value) { this.setAttribute('class', value); }
  get isConnected() { return this.isRoot || Boolean(this.parentElement?.isConnected); }
  getAttribute(key) { return this.attributes.get(key) ?? null; }
  setAttribute(key, value) { this.attributes.set(key, String(value)); }
  removeAttribute(key) { this.attributes.delete(key); }
  append(...children) { for (const child of children) { child.remove(); child.parentElement = this; this.children.push(child); } }
  attachShadow() {
    this.shadowRoot = new Element('#shadow-root');
    this.shadowRoot.parentElement = this;
    return this.shadowRoot;
  }
  remove() {
    if (this.parentElement) this.parentElement.children.splice(this.parentElement.children.indexOf(this), 1);
    this.parentElement = null;
  }
  insertAdjacentElement(position, element) {
    element.remove();
    if (position === 'afterbegin') {
      element.parentElement = this;
      this.children.unshift(element);
    } else {
      assert.equal(position, 'afterend');
      element.parentElement = this.parentElement;
      this.parentElement.children.splice(this.parentElement.children.indexOf(this) + 1, 0, element);
    }
  }
  getClientRects() { return this.isConnected && !this.hidden && this.style.display !== 'none' ? [this.box] : []; }
  getBoundingClientRect() { return this.box; }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (matches(node, selector)) return node; return null; }
  contains(other) { for (let node = other; node; node = node.parentElement) if (node === this) return true; return false; }
  querySelectorAll(selector) {
    const alternatives = selector.split(',').map(value => value.trim());
    const found = [];
    function walk(node) {
      for (const child of [...node.children, ...(node.shadowRoot ? [node.shadowRoot] : [])]) {
        if (alternatives.some(value => matches(child, value))) found.push(child);
        walk(child);
      }
    }
    walk(this);
    return found;
  }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  async click(trusted = true) { await this.listeners.get('click')?.({isTrusted: trusted, currentTarget: this}); }
}
function el(tag, attributes, ...children) { const node = new Element(tag, attributes); node.append(...children); return node; }
const subscribe = () => el('ytd-subscribe-button-renderer', {}, el('button'));
const author = name => el('div', {id: 'channel-name'}, el('a', {href: `/${name}`}));
function channelPage() {
  return el('ytd-browse', {'page-subtype': 'channels'}, el('yt-page-header-renderer', {}, subscribe()));
}
function watchPage(name = '@owner', video = VIDEO) {
  return el('ytd-watch-flexy', {'video-id': video},
    el('ytd-watch-metadata', {}, el('div', {id: 'owner'},
      el('ytd-video-owner-renderer', {}, author(name)), subscribe())));
}
function shortPage(name = '@shortowner', video = VIDEO) {
  return el('ytd-reel-video-renderer', {'is-active': '', 'video-id': video},
    el('yt-reel-channel-bar-view-model', {}, author(name), subscribe()),
    el('reel-action-bar-view-model', {}, el('button', {'aria-label': 'Like'})));
}
function fixture(url, ...children) {
  const options = children.at(-1) instanceof Element ? {} : children.pop() || {};
  const document = el('document', {}, el('html', {}, el('body', {}, ...children)));
  document.isRoot = true;
  document.documentElement = document.children[0];
  document.body = document.documentElement.children[0];
  document.createElement = tag => new Element(tag);
  document.createElementNS = (_namespace, tag) => new Element(tag);
  const requests = [];
  const lookups = [];
  const timers = new Map();
  const observers = [];
  const resizeObservers = [];
  let timerId = 0;
  let clock = 100000;
  const windowListeners = new Map();
  const environment = {
    document, location: {href: url}, URL, innerHeight: 900, innerWidth: 1400,
    getComputedStyle: node => ({display: node.style.display || 'block', visibility: node.style.visibility || 'visible'}),
    setTimeout: callback => { timers.set(++timerId, callback); return timerId; },
    MutationObserver: class {
      constructor(callback) { this.callback = callback; observers.push(this); }
      observe() {}
    },
    ResizeObserver: class {
      constructor(callback) { this.callback = callback; this.nodes = new Set(); resizeObservers.push(this); }
      observe(node) { this.nodes.add(node); }
      disconnect() { this.nodes.clear(); }
    },
    chrome: {runtime: {sendMessage: async message => {
      if (message.type === 'channel-status') { lookups.push(JSON.parse(JSON.stringify(message))); return environment.lookupReply(message); }
      requests.push(JSON.parse(JSON.stringify(message))); return environment.reply(message);
    }}},
    lookupReply: options.lookupReply || (async () => ({status: 'unknown'})),
    reply: async () => ({status: 'added'}),
    fetch: () => { throw new Error('Content must never make network requests'); },
    Date: class extends Date { static now() { return clock; } },
    addEventListener: (type, listener) => windowListeners.set(type, listener),
  };
  environment.window = environment;
  vm.runInNewContext(source, environment, {filename: 'content.js'});
  return {
    document, requests, lookups, environment, resizeObservers,
    focus: () => windowListeners.get('focus')?.(),
    advance: ms => { clock += ms; },
    wrapper: () => document.querySelectorAll('.leti-cook-channel-control')[0],
    button: () => document.querySelectorAll('.leti-cook-add-channel')[0],
    message: () => document.querySelectorAll('.leti-cook-channel-message')[0]?.textContent,
    mutate(target = document.body) { for (const observer of observers) observer.callback([{target}]); },
    resize() { for (const observer of resizeObservers) if (observer.nodes.size) observer.callback([]); },
    flush() { const batch = [...timers.values()]; timers.clear(); for (const callback of batch) callback(); },
    get timerCount() { return timers.size; },
  };
}

test('channel URL wins over recommended channels, queries and tab suffixes are removed', async () => {
  const page = channelPage();
  page.append(author('@recommendation'));
  const f = fixture(`https://www.youtube.com/channel/${CHANNEL}/videos?view=0`, page);
  assert.equal(f.requests.length, 0, 'Page observation never adds a channel.');
  await f.button().click(false);
  assert.equal(f.requests.length, 0, 'Page-dispatched clicks cannot add a channel.');
  await f.button().click();
  assert.deepEqual(f.requests, [{type: 'add-channel', channelUrl: `https://www.youtube.com/channel/${CHANNEL}`}]);
  assert.equal(f.button().textContent, 'Added');
  assert.equal(f.button().disabled, true);
  f.mutate(); f.flush();
  assert.equal(f.document.querySelectorAll('.leti-cook-add-channel').length, 1);
  assert.equal(f.requests.length, 1);
});

test('watch reads only the current video owner, not comments or recommendations', async () => {
  const page = watchPage();
  page.append(author('@commenter'));
  const f = fixture(`https://www.youtube.com/watch?v=${VIDEO}`, page, author('@recommendation'));
  await f.button().click();
  assert.equal(f.requests[0].channelUrl, 'https://www.youtube.com/@owner');
});

test('ambiguous author, old player, malformed and unrelated URLs fail closed', () => {
  const ambiguous = watchPage();
  ambiguous.querySelectorAll('ytd-video-owner-renderer')[0].append(author('@coauthor'));
  assert.equal(fixture(`https://www.youtube.com/watch?v=${VIDEO}`, ambiguous).button(), undefined);
  assert.equal(fixture(`https://www.youtube.com/watch?v=${NEXT_VIDEO}`, watchPage()).button(), undefined);
  for (const url of ['https://www.youtube.com/results?search_query=channel',
    `https://www.youtube.com/watch?v=${VIDEO}&v=${NEXT_VIDEO}`, 'https://youtube.com.evil/@owner',
    'https://www.youtube.com/c/oldname', 'https://www.youtube.com/@%00bad']) {
    assert.equal(fixture(url, channelPage(), watchPage()).button(), undefined, url);
  }
  const badOwner = watchPage();
  badOwner.querySelectorAll('a[href]')[0].setAttribute('href', 'https://www.youtube.com.evil/@owner');
  assert.equal(fixture(`https://www.youtube.com/watch?v=${VIDEO}`, badOwner).button(), undefined);
});

test('hidden previous owners and duplicate links to one author do not create ambiguity', async () => {
  const page = watchPage();
  const owner = page.querySelectorAll('ytd-video-owner-renderer')[0];
  owner.append(author('@owner'));
  const hiddenOwner = el('ytd-video-owner-renderer', {'aria-hidden': 'true'}, author('@old'), subscribe());
  page.append(hiddenOwner);
  const f = fixture(`https://www.youtube.com/watch?v=${VIDEO}`, page);
  await f.button().click();
  assert.equal(f.requests[0].channelUrl, 'https://www.youtube.com/@owner');
});

test('SPA navigation rejects the stale click, coalesces mutations and recreates exactly one button', async () => {
  const page = watchPage();
  const f = fixture(`https://www.youtube.com/watch?v=${VIDEO}`, page);
  const oldButton = f.button();
  f.environment.location.href = `https://www.youtube.com/watch?v=${NEXT_VIDEO}`;
  await oldButton.click();
  assert.equal(f.requests.length, 0);
  assert.equal(f.button(), undefined);
  page.setAttribute('video-id', NEXT_VIDEO);
  page.querySelectorAll('a[href]')[0].setAttribute('href', '/@newowner');
  for (let i = 0; i < 20; i++) f.mutate();
  assert.equal(f.timerCount, 1);
  f.flush();
  assert.equal(f.document.querySelectorAll('.leti-cook-add-channel').length, 1);
  await oldButton.click();
  assert.equal(f.requests.length, 0, 'A detached former button cannot submit for the new owner.');
  await f.button().click();
  assert.equal(f.requests[0].channelUrl, 'https://www.youtube.com/@newowner');
});

test('replacing the Subscribe element repairs the control without replaying a request', async () => {
  const page = watchPage();
  const f = fixture(`https://www.youtube.com/watch?v=${VIDEO}`, page);
  await f.button().click();
  const owner = page.querySelectorAll('#owner')[0];
  owner.querySelectorAll('ytd-subscribe-button-renderer')[0].remove();
  owner.append(subscribe());
  f.mutate(); f.flush();
  assert.equal(f.document.querySelectorAll('.leti-cook-add-channel').length, 1);
  assert.equal(f.button().textContent, 'Added');
  assert.equal(f.requests.length, 1);
});

test('Shorts requires one active visible card whose explicit ID matches the URL', async () => {
  const current = shortPage();
  const inactive = shortPage('@other', NEXT_VIDEO);
  inactive.removeAttribute('is-active');
  const f = fixture(`https://www.youtube.com/shorts/${VIDEO}`, inactive, current);
  await f.button().click();
  assert.equal(f.requests[0].channelUrl, 'https://www.youtube.com/@shortowner');
  const withoutIdentity = shortPage(); withoutIdentity.removeAttribute('video-id');
  assert.equal(fixture(`https://www.youtube.com/shorts/${VIDEO}`, withoutIdentity).button(), undefined);
  assert.equal(fixture(`https://www.youtube.com/shorts/${VIDEO}`, shortPage(), shortPage('@other')).button(), undefined);
  const outside = shortPage(); outside.box = {top: 1000, left: 0, bottom: 1200, right: 100};
  assert.equal(fixture(`https://www.youtube.com/shorts/${VIDEO}`, outside).button(), undefined);
});

test('one pending request; late response after navigation cannot label a new owner Added', async () => {
  const page = watchPage();
  const f = fixture(`https://www.youtube.com/watch?v=${VIDEO}`, page);
  let finish;
  f.environment.reply = () => new Promise(resolve => { finish = resolve; });
  const first = f.button().click();
  await f.button().click();
  assert.equal(f.requests.length, 1);
  page.querySelectorAll('a[href]')[0].setAttribute('href', '/@newowner');
  f.mutate(); f.flush();
  finish({status: 'added'});
  await first;
  assert.equal(f.button().textContent, 'Leti Cook');
  assert.equal(f.button().disabled, false);
});

test('success statuses and hydration attention use bounded messages, never raw payloads', async () => {
  for (const [status, label] of [['added', 'Added'], ['exists', 'Already added'], ['restored', 'Restored']]) {
    const f = fixture('https://www.youtube.com/@owner', channelPage());
    f.environment.reply = async () => ({status, requiresAttention: true, title: '<script>payload</script>'});
    await f.button().click();
    assert.equal(f.button().textContent, label);
    assert.equal(f.message(), 'Open Leti Cook to finish loading this channel.');
    assert.equal(f.button().disabled, true);
  }
});

test('known refusals explain setup/login/retry; none triggers an automatic repeat', async () => {
  for (const status of ['setup_required', 'login_required', 'busy', 'rate_limited', 'unavailable', 'rejected']) {
    const f = fixture('https://www.youtube.com/@owner', channelPage());
    f.environment.reply = async () => ({status});
    await f.button().click();
    assert.ok(f.message().length > 10, status);
    assert.equal(f.button().disabled, false, status);
    f.mutate(); f.flush();
    assert.equal(f.requests.length, 1, status);
  }
});

test('unknown response and lost acknowledgement stay unconfirmed, with no retry', async () => {
  for (const reply of [async () => null, async () => ({status: 'surprise'}),
    async () => ({status: 'unconfirmed'}), async () => { throw new Error('lost acknowledgement'); }]) {
    const f = fixture('https://www.youtube.com/@owner', channelPage());
    f.environment.reply = reply;
    await f.button().click();
    assert.match(f.message(), /result is unknown/);
    assert.equal(f.button().disabled, true);
    await f.button().click();
    f.mutate(); f.flush();
    assert.equal(f.requests.length, 1);
  }
});

test('current generic header Subscribe, known localized labels and nested legacy controls', async () => {
  for (const label of ['Subscribe', 'Subscribed', 'Подписаться', 'Вы подписаны', 'Підписатися']) {
    const page = channelPage();
    const header = page.querySelectorAll('yt-page-header-renderer')[0];
    header.querySelectorAll('ytd-subscribe-button-renderer')[0].remove();
    const action = el('div', {class: 'ytFlexibleActionsViewModelAction'},
      el('button-view-model', {}, el('button', {'aria-label': label})));
    header.append(el('yt-flexible-actions-view-model', {}, action,
      el('button', {'aria-label': 'Join'})));
    const f = fixture('https://www.youtube.com/@owner', page);
    await f.button().click();
    assert.equal(f.requests[0].channelUrl, 'https://www.youtube.com/@owner');
  }
  const nested = channelPage();
  nested.querySelectorAll('ytd-subscribe-button-renderer')[0].append(el('yt-subscribe-button-view-model'));
  assert.ok(fixture('https://www.youtube.com/@owner', nested).button());
  const ambiguous = channelPage();
  const header = ambiguous.querySelectorAll('yt-page-header-renderer')[0];
  header.querySelectorAll('ytd-subscribe-button-renderer')[0].remove();
  header.append(el('yt-flexible-actions-view-model', {},
    el('button', {'aria-label': 'Subscribe'}), el('button', {'aria-label': 'Subscribed'})));
  assert.equal(fixture('https://www.youtube.com/@owner', ambiguous).button(), undefined);
});

test('channel navigation hides the old header button until navigation completes', async () => {
  const page = channelPage();
  const f = fixture('https://www.youtube.com/@oldowner', page);
  const oldButton = f.button();
  f.document.listeners.get('yt-navigate-start')();
  f.environment.location.href = 'https://www.youtube.com/@newowner';
  f.mutate(); f.flush();
  assert.equal(f.button(), undefined);
  await oldButton.click();
  assert.equal(f.requests.length, 0);
  f.document.listeners.get('yt-navigate-finish')();
  f.flush();
  await f.button().click();
  assert.equal(f.requests[0].channelUrl, 'https://www.youtube.com/@newowner');
});

test('watch control joins the owner row beside the Subscribe container without restyling YouTube', () => {
  const page = watchPage();
  const owner = page.querySelectorAll('#owner')[0];
  owner.style.display = 'flex';
  const renderer = owner.querySelectorAll('ytd-subscribe-button-renderer')[0];
  const container = el('div', {id: 'subscribe-button'}, renderer);
  container.style.display = 'block';
  owner.append(container);
  const f = fixture(`https://www.youtube.com/watch?v=${VIDEO}`, page);
  const wrapper = f.wrapper();
  assert.equal(wrapper.parentElement, owner);
  assert.equal(owner.children.indexOf(wrapper), owner.children.indexOf(container) + 1);
  assert.deepEqual(container.children, [renderer]);
  assert.deepEqual(owner.style, {display: 'flex'});
  assert.deepEqual(container.style, {display: 'block'});
  assert.equal(f.requests.length, 0);
});


test('signed-in channel action stays one row beside other extension controls', async () => {
  const subscription = el('div', {class: 'ytFlexibleActionsViewModelAction'},
    el('yt-subscribe-button-view-model', {}, el('button', {'aria-label': 'Subscribed'})));
  const neighbour = el('div', {class: 'other-extension'}, el('button', {'aria-label': 'View channel stats'}));
  const row = el('yt-flexible-actions-view-model', {}, subscription, neighbour);
  row.style.display = 'flex'; subscription.style.display = 'block';
  const page = el('ytd-browse', {'page-subtype': 'channels'}, el('yt-page-header-renderer', {}, row));
  const f = fixture('https://www.youtube.com/@owner', page);
  assert.equal(f.wrapper().parentElement, row);
  assert.deepEqual(row.children, [subscription, f.wrapper(), neighbour]);
  assert.deepEqual(subscription.style, {display: 'block'});
  assert.deepEqual(neighbour.style, {});
  const button = f.button();
  subscription.children[0].remove();
  subscription.append(el('button-view-model', {}, el('button', {'aria-label': 'Subscribe'})));
  f.mutate(); f.flush();
  assert.equal(f.document.querySelectorAll('.leti-cook-add-channel').length, 1);
  assert.equal(f.button(), button);
  assert.equal(f.wrapper().parentElement, row);
  assert.equal(f.requests.length, 0);
});

function modernShort(name = '@shortowner') {
  const subscribeBox = el('div', {class: 'ytReelChannelBarViewModelReelSubscribeButton'},
    el('button-view-model', {}, el('button', {'aria-label': 'Subscribe'})));
  const bar = el('yt-reel-channel-bar-view-model', {}, el('a', {href: `/${name}/shorts`}), subscribeBox);
  const rail = el('reel-action-bar-view-model', {}, el('button', {'aria-label': 'Like'}));
  const card = el('ytd-reel-video-renderer', {}, bar, rail);
  return {card, bar, subscribeBox, rail};
}

test('current Shorts uses the visible author and its own right-hand action rail', async () => {
  const {card, bar, rail} = modernShort();
  const page = el('ytd-shorts', {role: 'main'}, card);
  const f = fixture(`https://www.youtube.com/shorts/${VIDEO}`, page);
  const wrapper = f.wrapper();
  assert.equal(wrapper.parentElement, rail);
  assert.equal(rail.children.indexOf(wrapper), 0);
  assert.equal(wrapper.getAttribute('data-placement'), 'shorts');
  assert.equal(bar.querySelectorAll('.leti-cook-channel-control').length, 0);
  await f.button().click();
  assert.equal(f.requests[0].channelUrl, 'https://www.youtube.com/@shortowner');
});

test('Shorts tooltip adapts to right-side room without remounting or moving the rail', () => {
  const {card, rail} = modernShort();
  const f = fixture(`https://www.youtube.com/shorts/${VIDEO}`, el('ytd-shorts', {}, card));
  const wrapper = f.wrapper();
  const button = f.button();
  const message = wrapper.shadowRoot.querySelectorAll('.leti-cook-channel-message')[0];
  wrapper.box = {top: 800, left: 1100, bottom: 848, right: 1148, width: 48, height: 48};
  message.box = {height: 120};
  f.resize(); f.flush();
  assert.equal(message.style.maxWidth, '234px');
  assert.equal(message.style.top, '-28px', 'Long feedback stays inside the bottom edge.');
  assert.equal(wrapper.getAttribute('data-tooltip-room'), 'available');
  f.environment.innerWidth = 1230;
  f.resize(); f.flush();
  assert.equal(message.style.maxWidth, '64px', 'Wrap to the actual right-side room.');
  f.environment.innerWidth = 1200;
  f.resize(); f.flush();
  assert.equal(wrapper.getAttribute('data-tooltip-room'), 'unavailable');
  assert.ok(button.getAttribute('aria-label'), 'No-room fallback retains accessible feedback.');
  f.environment.innerWidth = 1600;
  f.resize(); f.flush();
  assert.equal(wrapper.getAttribute('data-tooltip-room'), 'available');
  assert.equal(message.style.maxWidth, '240px');
  assert.equal(f.wrapper(), wrapper);
  assert.equal(f.button(), button);
  assert.equal(wrapper.parentElement, rail);
  assert.deepEqual(rail.style, {}, 'Other controls and their rail are not repositioned.');
  assert.equal(f.requests.length, 0);
  assert.equal(f.lookups.length, 1);
});

test('Shorts keeps one rail control when Subscribe is absent, hidden or reappears', async () => {
  const {card, bar, subscribeBox, rail} = modernShort();
  subscribeBox.remove();
  const f = fixture(`https://www.youtube.com/shorts/${VIDEO}`, el('ytd-shorts', {}, card),
    {lookupReply: async () => ({status: 'exists'})});
  await new Promise(setImmediate);
  const original = f.wrapper();
  assert.equal(original.parentElement, rail);
  assert.equal(f.button().disabled, true);
  assert.equal(original.getAttribute('data-state'), 'success');
  bar.append(subscribeBox);
  f.mutate(bar); f.flush();
  assert.equal(f.wrapper(), original, 'Subscribe returning does not remount the control.');
  subscribeBox.hidden = true;
  f.mutate(bar); f.flush();
  assert.equal(f.wrapper(), original);
  assert.equal(f.document.querySelectorAll('.leti-cook-channel-control').length, 1);
  assert.equal(f.requests.length, 0);
  assert.equal(f.lookups.length, 1);
  assert.equal(f.lookups[0].videoId, VIDEO);
});

test('Shorts missing Subscribe still adds the correct author only on a trusted click', async () => {
  const {card, subscribeBox} = modernShort();
  subscribeBox.remove();
  const f = fixture(`https://www.youtube.com/shorts/${VIDEO}`, el('ytd-shorts', {}, card));
  await f.button().click(false);
  assert.equal(f.requests.length, 0);
  await f.button().click();
  assert.deepEqual(f.requests, [{type: 'add-channel', channelUrl: 'https://www.youtube.com/@shortowner'}]);
});

test('Shorts waits for its visible rail and rejects ambiguous rails or conflicting authors', () => {
  const {card, bar, rail} = modernShort();
  rail.remove();
  const f = fixture(`https://www.youtube.com/shorts/${VIDEO}`, el('ytd-shorts', {}, card));
  assert.equal(f.button(), undefined);
  card.append(rail);
  f.mutate(card); f.flush();
  assert.equal(f.wrapper().parentElement, rail);
  const other = el('reel-action-bar-view-model'); card.append(other);
  f.mutate(card); f.flush();
  assert.equal(f.button(), undefined);
  other.hidden = true;
  f.mutate(card); f.flush();
  assert.ok(f.button());
  bar.append(el('a', {href: '/@different'}));
  f.mutate(bar); f.flush();
  assert.equal(f.button(), undefined);
  assert.equal(f.requests.length, 0);
});

test('Shorts rail replacement and late extension controls do not duplicate or trigger Add', async () => {
  const {card, rail} = modernShort();
  const f = fixture(`https://www.youtube.com/shorts/${VIDEO}`, el('ytd-shorts', {}, card));
  rail.insertAdjacentElement('afterbegin', el('button', {'aria-label': 'Other extension'}));
  f.mutate(rail); f.flush();
  assert.equal(f.wrapper().parentElement, rail);
  const old = f.button();
  rail.remove();
  const replacement = el('reel-action-bar-view-model'); card.append(replacement);
  await old.click();
  assert.equal(f.requests.length, 0, 'A detached old rail cannot add.');
  assert.equal(f.wrapper().parentElement, replacement);
  assert.equal(f.document.querySelectorAll('.leti-cook-channel-control').length, 1);
});

test('Shorts scrolling refuses mixed cards, stale clicks, conflicting IDs and clipped cards', async () => {
  const first = modernShort('@first'), second = modernShort('@second');
  const page = el('ytd-shorts', {role: 'main'}, first.card, second.card);
  page.box = {top: 0, left: 0, bottom: 400, right: 400};
  first.card.box = {top: 0, left: 0, bottom: 400, right: 400};
  second.card.box = {top: 400, left: 0, bottom: 800, right: 400};
  const f = fixture(`https://www.youtube.com/shorts/${VIDEO}`, page);
  const old = f.button();
  assert.ok(old, 'The next card is clipped by the Shorts container.');
  first.card.box = {top: -200, left: 0, bottom: 200, right: 400};
  second.card.box = {top: 200, left: 0, bottom: 600, right: 400};
  await old.click();
  assert.equal(f.requests.length, 0, 'Fresh click checks visibility before debounce.');
  assert.equal(f.button(), undefined, 'Two partial cards are ambiguous.');
  first.card.box = {top: -400, left: 0, bottom: 0, right: 400};
  second.card.box = {top: 0, left: 0, bottom: 400, right: 400};
  f.environment.location.href = `https://www.youtube.com/shorts/${NEXT_VIDEO}`;
  f.document.listeners.get('scroll')(); f.flush();
  await old.click();
  assert.equal(f.requests.length, 0);
  second.card.setAttribute('video-id', VIDEO);
  await f.button().click();
  assert.equal(f.requests.length, 0, 'An explicit conflicting ID is never ignored.');
  assert.equal(f.button(), undefined);
  second.card.removeAttribute('video-id'); f.mutate(); f.flush();
  await f.button().click();
  assert.equal(f.requests[0].channelUrl, 'https://www.youtube.com/@second');
});

test('passive confirmed status displays a check, disables add and ignores unrelated data', async () => {
  const f = fixture(`https://www.youtube.com/watch?v=${VIDEO}`, watchPage(),
    {lookupReply: async () => ({status: 'exists', title: '<script>ignored</script>'})});
  await new Promise(setImmediate);
  assert.equal(f.lookups.length, 1);
  assert.equal(f.lookups[0].videoId, VIDEO);
  assert.equal(f.button().getAttribute('data-state'), 'success');
  assert.equal(f.button().disabled, true);
  await f.button().click();
  assert.equal(f.requests.length, 0);
  assert.equal(f.button().textContent, 'Already added');
  assert.ok(f.wrapper().shadowRoot, 'Control internals are isolated from site CSS.');
  for (let i=0; i<20; i++) { f.mutate(); f.flush(); }
  assert.equal(f.lookups.length, 1, 'Mutation traffic does not poll the backend.');
});

test('read failure is unknown, not proof of absence; a late read cannot erase successful add', async () => {
  let finish;
  const f = fixture(`https://www.youtube.com/watch?v=${VIDEO}`, watchPage(),
    {lookupReply: () => new Promise(resolve => {finish=resolve;})});
  await f.button().click();
  finish({status:'not_tracked'});
  await new Promise(setImmediate);
  assert.equal(f.button().textContent,'Added');
  const failed = fixture('https://www.youtube.com/@owner',channelPage(),
    {lookupReply: async () => { throw new Error('offline'); }});
  await new Promise(setImmediate);
  assert.match(failed.message(), /Could not check tracking/);
  assert.equal(failed.button().disabled,false);
});

test('channel canonical ID is usable on load but stale metadata cannot name the next channel', async () => {
  const canonical = el('link',{rel:'canonical',href:`https://www.youtube.com/channel/${CHANNEL}`});
  const f = fixture('https://www.youtube.com/@owner',canonical,channelPage());
  assert.equal(f.lookups[0].channelUrl,`https://www.youtube.com/channel/${CHANNEL}`);
  f.document.listeners.get('yt-navigate-start')();
  f.environment.location.href='https://www.youtube.com/@next';
  f.document.listeners.get('yt-navigate-finish')(); f.flush();
  assert.equal(f.lookups.at(-1).channelUrl,'https://www.youtube.com/@next');
  canonical.setAttribute('href',`https://www.youtube.com/channel/UC${'b'.repeat(22)}`);
  f.mutate(); f.flush();
  assert.equal(f.lookups.at(-1).channelUrl,`https://www.youtube.com/channel/UC${'b'.repeat(22)}`);
});

test('an explicit channel URL wins over stale canonical metadata', () => {
  const canonical = el('link', {rel: 'canonical', href: `https://www.youtube.com/channel/UC${'b'.repeat(22)}`});
  const f = fixture(`https://www.youtube.com/channel/${CHANNEL}`, canonical, channelPage());
  assert.equal(f.lookups[0].channelUrl, `https://www.youtube.com/channel/${CHANNEL}`);
});

test('a cramped author row moves only our control out of flow and restores it when room returns', () => {
  const page = watchPage();
  const owner = page.querySelectorAll('#owner')[0];
  const authorNode = owner.querySelectorAll('ytd-video-owner-renderer')[0];
  const info = el('div', {id: 'upload-info'});
  authorNode.append(info);
  owner.box.height = 43;
  let cramped = true;
  info.getBoundingClientRect = () => ({width: cramped && owner.querySelectorAll('.leti-cook-channel-control')[0]?.getAttribute('data-placement') === 'inline' ? 15 : 100});
  const f = fixture(`https://www.youtube.com/watch?v=${VIDEO}`, page);
  assert.equal(f.wrapper().getAttribute('data-placement'), 'floating');
  assert.deepEqual(owner.style, {});
  assert.deepEqual(authorNode.style, {});
  cramped = false; f.mutate(); f.flush();
  assert.equal(f.wrapper().getAttribute('data-placement'), 'inline');
  assert.equal(f.requests.length, 0);
});

function watchRow() {
  const page = watchPage();
  const metadata = page.querySelectorAll('ytd-watch-metadata')[0];
  const owner = page.querySelectorAll('#owner')[0];
  const authorNode = owner.querySelectorAll('ytd-video-owner-renderer')[0];
  const info = authorNode.querySelectorAll('#channel-name')[0];
  owner.box = {left: 20, right: 640, top: 0, bottom: 43, width: 620, height: 43};
  info.box = {width: 180, height: 43};
  const actions = el('div', {id: 'actions'}, el('button'));
  const actionButton = actions.children[0];
  actionButton.box = {left: 1000, right: 1380, top: 0, bottom: 40, width: 380, height: 40};
  const row = el('div', {id: 'top-row'}, owner, actions);
  row.box = {left:20, right:1380, top:0, bottom:43, width:1360, height:43};
  metadata.append(row);
  const values = new Map(), priorities = new Map();
  Object.assign(owner.style, {
    getPropertyValue: key => values.get(key) || '',
    getPropertyPriority: key => priorities.get(key) || '',
    setProperty: (key, value, priority='') => {values.set(key, value); priorities.set(key, priority);},
    removeProperty: key => {values.delete(key); priorities.delete(key);},
  });
  return {page, owner, info, actions, actionButton, row};
}

test('late vidIQ cap uses real action width, observes reflow and restores its owned override on navigation', () => {
  const {page,owner,info,actions,actionButton,row} = watchRow();
  owner.style.setProperty('--leti-cook-owner-room','720px','important');
  const f = fixture(`https://www.youtube.com/watch?v=${VIDEO}`, page);
  assert.equal(owner.getAttribute('data-leti-cook-owner-room'),null);
  owner.className = 'vidiq-scope'; owner.style.maxWidth = '620px';
  f.mutate(); f.flush();
  assert.equal(owner.style.getPropertyValue('--leti-cook-owner-room'),'948px');
  assert.equal(owner.getAttribute('data-leti-cook-owner-room'),'1');
  assert.equal(owner.style.maxWidth,'620px', 'Never rewrite the other extension inline value.');
  assert.ok(f.resizeObservers[0].nodes.has(actions));
  // CSS/font changes can resize controls without a DOM mutation.
  actionButton.box.left=900; f.resize(); f.resize();
  assert.equal(f.timerCount,1, 'Resize traffic is coalesced.');
  f.flush();
  assert.equal(owner.style.getPropertyValue('--leti-cook-owner-room'),'848px');
  row.box.right=800; f.resize(); f.flush();
  assert.equal(owner.style.getPropertyValue('--leti-cook-owner-room'),'620px');
  assert.equal(f.lookups.length,1); assert.equal(f.requests.length,0);
  f.document.listeners.get('yt-navigate-start')();
  assert.equal(owner.style.getPropertyValue('--leti-cook-owner-room'),'720px');
  assert.equal(owner.style.getPropertyPriority('--leti-cook-owner-room'),'important');
  assert.equal(owner.getAttribute('data-leti-cook-owner-room'),null);
  assert.equal(f.resizeObservers[0].nodes.size,0);
});

test('compatibility override is limited to the observed vidIQ cap and does not clobber later owners', () => {
  const {page,owner} = watchRow();
  owner.className='vidiq-scope'; owner.style.maxWidth='700px';
  const f=fixture(`https://www.youtube.com/watch?v=${VIDEO}`,page);
  assert.equal(owner.getAttribute('data-leti-cook-owner-room'),null);
  owner.style.maxWidth='620px'; f.mutate(); f.flush();
  assert.equal(owner.getAttribute('data-leti-cook-owner-room'),'1');
  owner.style.setProperty('--leti-cook-owner-room','950px');
  owner.style.maxWidth='650px'; f.mutate(); f.flush();
  assert.equal(owner.style.maxWidth,'650px');
  assert.equal(owner.style.getPropertyValue('--leti-cook-owner-room'),'950px');
  assert.equal(owner.getAttribute('data-leti-cook-owner-room'),null);
});

test('an inline control that adds a name line moves out of flow even when width is unchanged', () => {
  const {page,owner,info}=watchRow();
  info.getBoundingClientRect=()=>({width:180,height:
    owner.querySelectorAll('.leti-cook-channel-control')[0]?.getAttribute('data-placement')==='inline'?66:43});
  const f=fixture(`https://www.youtube.com/watch?v=${VIDEO}`,page);
  assert.equal(f.wrapper().getAttribute('data-placement'),'floating');
});

test('lookup failures show distinct help and one tooltip without pretending the channel is absent', async () => {
  for (const [status, message] of [
    ['app_closed', /Open Leti Cook in this browser/],
    ['login_required', /Sign in to Leti Cook/],
    ['setup_required', /Connect the extension/],
    ['unknown', /could not be identified/],
    ['unavailable', /Could not check tracking/],
  ]) {
    const f=fixture(`https://www.youtube.com/watch?v=${VIDEO}`,watchPage(),{lookupReply:async()=>({status})});
    await new Promise(setImmediate);
    assert.equal(f.button().getAttribute('data-state'),'unknown');
    assert.match(f.message(),message);
    assert.equal(f.button().title,undefined);
    assert.equal(f.button().getAttribute('title'),null);
    assert.equal(f.button().disabled,false);
    assert.equal(f.requests.length,0);
  }
});

test('refresh failure preserves a qualified last confirmation, while absence or logout clears it', async () => {
  const f=fixture(`https://www.youtube.com/watch?v=${VIDEO}`,watchPage(),{lookupReply:async()=>({status:'exists'})});
  await new Promise(setImmediate);
  f.environment.lookupReply=async()=>({status:'app_closed'});
  f.advance(60001); f.focus(); await new Promise(setImmediate);
  assert.equal(f.button().getAttribute('data-state'),'stale');
  assert.match(f.message(),/Previously confirmed/);
  assert.equal(f.button().disabled,true);
  f.environment.lookupReply=async()=>({status:'not_tracked'});
  f.advance(5001); f.focus(); await new Promise(setImmediate);
  assert.equal(f.button().getAttribute('data-state'),'idle');
  assert.equal(f.button().disabled,false);
  f.environment.lookupReply=async()=>({status:'exists'});
  f.advance(60001); f.focus(); await new Promise(setImmediate);
  f.environment.lookupReply=async()=>({status:'login_required'});
  f.advance(60001); f.focus(); await new Promise(setImmediate);
  assert.equal(f.button().getAttribute('data-state'),'unknown');
  assert.match(f.message(),/Sign in/);
  assert.equal(f.requests.length,0);
});

test('after opening the app, focus retries a failed lookup without DOM-driven polling', async () => {
  const f=fixture(`https://www.youtube.com/watch?v=${VIDEO}`,watchPage(),{lookupReply:async()=>({status:'app_closed'})});
  await new Promise(setImmediate);
  f.environment.lookupReply=async()=>({status:'exists'});
  f.focus(); await new Promise(setImmediate);
  assert.equal(f.lookups.length,1);
  f.advance(5001);
  for(let n=0;n<20;n++){f.mutate();f.flush();}
  assert.equal(f.lookups.length,1);
  f.focus(); await new Promise(setImmediate);
  assert.equal(f.lookups.length,2);
  assert.equal(f.button().getAttribute('data-state'),'success');
  assert.equal(f.requests.length,0);
});

test('only allowlisted connection codes reach the tooltip and clear on recovery', async () => {
  const f=fixture(`https://www.youtube.com/watch?v=${VIDEO}`,watchPage(),
    {lookupReply:async()=>({status:'unavailable',diagnostic:'B07'})});
  await new Promise(setImmediate);
  assert.match(f.message(),/LC-B07/);
  assert.equal(f.wrapper().getAttribute('data-diagnostic'),'B07');
  f.environment.lookupReply=async()=>({status:'unavailable',diagnostic:'secret-user-data'});
  f.advance(5001);f.focus();await new Promise(setImmediate);
  assert.doesNotMatch(f.message(),/secret-user-data/);
  assert.equal(f.wrapper().getAttribute('data-diagnostic'),'');
  f.environment.lookupReply=async()=>({status:'exists'});
  f.advance(5001);f.focus();await new Promise(setImmediate);
  assert.equal(f.button().getAttribute('data-state'),'success');
  assert.equal(f.wrapper().getAttribute('data-diagnostic'),'');
});
