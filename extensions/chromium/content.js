/* The page supplies a locator, never an API destination, account or command. */
(() => {
  'use strict';
  const HOSTS = new Set(['www.youtube.com', 'youtube.com']);
  const TABS = new Set(['videos', 'shorts', 'streams', 'featured', 'about', 'playlists', 'community']);
  const CHANNEL_ID = /^UC[A-Za-z0-9_-]{22}$/;
  const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
  const WRAPPER_CLASS = 'leti-cook-channel-control';
  const SUBSCRIBE = 'ytd-subscribe-button-renderer, yt-subscribe-button-view-model';
  const records = new Map();
  const checks = new Map();
  let control = null;
  let navigating = false;
  let scheduled = null;
  const canonicalChannels = new Map();
  let canonicalBeforeNavigation = null;
  let initialChannel = channelLocator(location.href);

  function canonicalLink() {
    const links = [...document.querySelectorAll('link[rel="canonical"]')];
    const url = links.length === 1 ? channelLocator(links[0].getAttribute('href')) : null;
    return url?.includes('/channel/') ? url : null;
  }
  function channelPageIdentity(locator) {
    if (locator?.includes('/channel/')) return locator;
    const canonical = canonicalLink();
    // Head metadata can lag behind a SPA navigation. Reuse only a bound pair
    // or canonical evidence that changed after this navigation began.
    if (canonical && ((locator === initialChannel && !canonicalBeforeNavigation)
        || (canonicalBeforeNavigation && canonical !== canonicalBeforeNavigation))) {
      canonicalChannels.set(locator, canonical);
      if (canonicalChannels.size > 100) canonicalChannels.delete(canonicalChannels.keys().next().value);
      initialChannel = null;
      canonicalBeforeNavigation = canonical;
    }
    return canonical && canonicalChannels.get(locator) === canonical ? canonical : locator;
  }

  function channelLocator(raw) {
    try {
      const url = new URL(raw, 'https://www.youtube.com');
      if (url.protocol !== 'https:' || !HOSTS.has(url.hostname) || url.port || url.username || url.password) return null;
      const parts = decodeURIComponent(url.pathname).replace(/^\/+|\/+$/g, '').split('/');
      if (parts[0] === 'channel' && CHANNEL_ID.test(parts[1] || '')
          && (parts.length === 2 || (parts.length === 3 && TABS.has(parts[2])))) {
        return `https://www.youtube.com/channel/${parts[1]}`;
      }
      if (parts[0]?.startsWith('@') && (parts.length === 1 || (parts.length === 2 && TABS.has(parts[1])))) {
        const handle = parts[0].slice(1);
        if (handle.length >= 1 && handle.length <= 100 && /^[\p{L}\p{N}\p{M}_.·-]+$/u.test(handle)
            && !/^[_.·-]|[_.·-]$/.test(handle)) {
          return `https://www.youtube.com/@${encodeURIComponent(handle)}`;
        }
      }
    } catch { /* A malformed page link is not a channel. */ }
    return null;
  }

  function visible(node, inViewport = false) {
    if (!node?.isConnected || !node.getClientRects().length) return false;
    for (let ancestor = node; ancestor; ancestor = ancestor.parentElement) {
      if (ancestor.hidden || ancestor.getAttribute('aria-hidden') === 'true') return false;
      const style = getComputedStyle(ancestor);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
    }
    if (!inViewport) return true;
    const box = node.getBoundingClientRect();
    return box.bottom > 0 && box.right > 0 && box.top < innerHeight && box.left < innerWidth;
  }

  function onlyVisible(root, selector, inViewport = false) {
    const matches = [...root.querySelectorAll(selector)].filter(node => visible(node, inViewport));
    return matches.length === 1 ? matches[0] : null;
  }

  function subscribeAnchor(root, fallbackSelector) {
    const components = [...root.querySelectorAll(SUBSCRIBE)].filter(node => visible(node));
    // Some layouts wrap the new view model in the older renderer. They are
    // one control, whereas two independent controls are ambiguous.
    const outer = components.filter(node => !components.some(other => other !== node && other.contains(node)));
    if (outer.length) return outer.length === 1 ? outer[0] : null;
    // The current channel header uses a generic button-view-model. It exposes
    // no stable subscription ID: accept only explicit, known accessible labels.
    const labels = new Set(['subscribe', 'subscribed', 'подписаться', 'вы подписаны',
      'підписатися', 'ви підписалися']);
    const buttons = [...root.querySelectorAll(fallbackSelector)].filter(button => visible(button)
      && labels.has((button.getAttribute('aria-label') || '').trim().toLowerCase()));
    if (buttons.length !== 1) return null;
    const button = buttons[0];
    const wrapper = button.closest('.ytFlexibleActionsViewModelAction') || button.closest('button-view-model');
    return wrapper && root.contains(wrapper) ? wrapper : button;
  }
  function rowItem(row, node) {
    if (!row || !node || row === node || !row.contains(node)) return node;
    let item = node;
    while (item.parentElement !== row) item = item.parentElement;
    return item;
  }

  function ownerChannel(owner, selector) {
    const anchors = [...owner.querySelectorAll(selector)].filter(node => visible(node));
    const identities = new Set(anchors.map(anchor => channelLocator(anchor.getAttribute('href'))).filter(Boolean));
    return identities.size === 1 ? [...identities][0] : null;
  }

  function pageTarget() {
    if (navigating) return null;
    const url = new URL(location.href);
    if (url.protocol !== 'https:' || !HOSTS.has(url.hostname) || url.port) return null;
    const channelUrl = channelLocator(url.href);
    if (channelUrl) {
      const page = onlyVisible(document, 'ytd-browse[page-subtype="channels"]');
      if (!page) return null;
      const header = onlyVisible(page, 'yt-page-header-renderer, ytd-c4-tabbed-header-renderer');
      const subscription = header && subscribeAnchor(header, 'yt-flexible-actions-view-model button[aria-label]');
      const actions = subscription?.closest('yt-flexible-actions-view-model');
      // Both generic Subscribe and the signed-in renderer live inside an action.
      // Join the row, not that action's block: otherwise neighbouring buttons
      // (including other extensions) stretch to the resulting two-line height.
      const anchor = actions && header.contains(actions) ? rowItem(actions, subscription) : subscription;
      return anchor ? { channelUrl: channelPageIdentity(channelUrl), anchor, route: url.href } : null;
    }
    const shorts = /^\/shorts\/([A-Za-z0-9_-]{11})\/?$/.exec(url.pathname);
    const videos = url.searchParams.getAll('v');
    const live = /^\/live\/([A-Za-z0-9_-]{11})\/?$/.exec(url.pathname);
    const videoId = url.pathname === '/watch' && videos.length === 1 && VIDEO_ID.test(videos[0])
      ? videos[0] : live?.[1];
    if (videoId) {
      const page = onlyVisible(document, 'ytd-watch-flexy');
      if (!page || page.getAttribute('video-id') !== videoId) return null;
      const metadata = onlyVisible(page, 'ytd-watch-metadata');
      const owner = metadata && onlyVisible(metadata, '#owner');
      const author = owner && onlyVisible(owner, 'ytd-video-owner-renderer');
      if (!author) return null;
      const locator = ownerChannel(author, '#channel-name a[href], #upload-info a[href]');
      const subscription = subscribeAnchor(owner, 'button[aria-label]');
      const container = subscription?.closest('#subscribe-button');
      // Watch places its renderer inside a block container. Join the owner's
      // row beside that container instead of stacking inside the button block.
      const anchor = container?.parentElement === owner ? container : subscription;
      const row = owner.closest('#top-row');
      const actions = row && onlyVisible(row, '#actions');
      return locator && anchor ? { channelUrl: locator, anchor, owner, author, row, actions, route: url.href } : null;
    }
    if (shorts) {
      const shortsPage = onlyVisible(document, 'ytd-shorts');
      const cards = shortsPage ? [...shortsPage.querySelectorAll('ytd-reel-video-renderer')].filter(node => {
        if (!visible(node, true)) return false;
        const cardBox = node.getBoundingClientRect();
        const pageBox = shortsPage.getBoundingClientRect();
        return Math.min(cardBox.bottom, pageBox.bottom, innerHeight) > Math.max(cardBox.top, pageBox.top, 0)
          && Math.min(cardBox.right, pageBox.right, innerWidth) > Math.max(cardBox.left, pageBox.left, 0);
      }) : [];
      const card = shortsPage ? (cards.length === 1 ? cards[0] : null)
        : onlyVisible(document, 'ytd-reel-video-renderer[is-active]', true);
      if (!card) return null;
      const explicitId = card.getAttribute('video-id');
      if (explicitId !== null && explicitId !== shorts[1]) return null;
      // Current Shorts no longer publishes is-active/video-id attributes. In
      // that layout the owner must belong to the single visible Shorts card;
      // mixed cards during scrolling are ambiguous. Recheck this at the click.
      if (explicitId === null && !shortsPage) return null;
      const owner = onlyVisible(card, 'yt-reel-channel-bar-view-model');
      if (!owner) return null;
      const locator = ownerChannel(owner, 'a[href]');
      // The Shorts action rail is independent of Subscribe, which YouTube can
      // hide. Read identity from this card's author, but mount only in its rail.
      const anchor = onlyVisible(card, 'reel-action-bar-view-model', true);
      return locator && anchor ? { channelUrl: locator, anchor, placement: 'shorts', route: url.href } : null;
    }
    return null;
  }

  function remember(channelUrl, value) {
    records.set(channelUrl, value);
    if (records.size > 100) records.delete(records.keys().next().value);
  }

  function videoIdentity(route) {
    const url = new URL(route);
    const id = url.pathname === '/watch' ? url.searchParams.get('v') : /^\/(?:shorts|live)\/([^/]+)$/.exec(url.pathname)?.[1];
    return VIDEO_ID.test(id || '') ? id : null;
  }

  async function checkChannel(target) {
    const locator = target.channelUrl;
    const state = records.get(locator);
    if (['pending', 'unconfirmed'].includes(state?.status)) return;
    const key = `${locator}|${videoIdentity(target.route) || ''}`;
    if (Date.now() < (checks.get(key) || 0)) return;
    checks.set(key, Date.now() + 60000);
    if (checks.size > 100) checks.delete(checks.keys().next().value);
    // A late GET must never overwrite a click, another route or a newer GET.
    const ticket = {};
    remember(locator, { ...state, lookup: 'checking', diagnostic: null, ticket });
    render();
    const failed = (reason, diagnostic) => {
      const known = ['exists', 'added', 'restored'].includes(state?.status);
      const preserve = known && ['unknown', 'unavailable', 'app_closed'].includes(reason);
      const code = /^(?:A(?:0[0-6]|1[123]|2[123]|6[0-3])|B(?:0[1-9]|9[0-7])|C01)$/.test(diagnostic || '') ? diagnostic : null;
      remember(locator, { ...(preserve ? state : {}), lookup: reason, stale: preserve, diagnostic: code });
      checks.set(key, Date.now() + 5000);
    };
    try {
      const result = await chrome.runtime.sendMessage({ type: 'channel-status', channelUrl: locator,
        videoId: videoIdentity(target.route) });
      if (records.get(locator)?.ticket !== ticket) return;
      if (result?.status === 'exists') remember(locator, { status: 'exists' });
      else if (result?.status === 'not_tracked') remember(locator, { lookup: 'not_tracked' });
      else failed(['app_closed', 'login_required', 'setup_required', 'unknown'].includes(result?.status)
        ? result.status : 'unavailable', result?.diagnostic);
    } catch {
      if (records.get(locator)?.ticket === ticket) failed('unavailable', 'C01');
    }
    render();
  }

  function render() {
    if (!control) return;
    const state = records.get(control.channelUrl) || {};
    const messages = {
      setup_required: 'Finish setup in the opened tab, then try again.',
      login_required: 'Sign in to Leti Cook, then try again.',
      busy: 'Another request is in progress. Try again shortly.',
      rate_limited: 'Too many additions. Try again later.',
      unconfirmed: 'The result is unknown. Check Leti Cook, then reload this page before trying again.',
      unavailable: 'Leti Cook is unavailable. Check its tab and try again.',
      rejected: 'This channel could not be added. Check Leti Cook.',
    };
    const success = { added: 'Added', exists: 'Already added', restored: 'Restored' }[state.status];
    const lookupMessages = {
      checking: 'Checking whether this channel is already in Leti Cook…',
      app_closed: 'Open Leti Cook in this browser to check tracking. Click here to open it and check or add the channel.',
      login_required: 'Sign in to Leti Cook, then return here to check tracking.',
      setup_required: 'Connect the extension and finish Leti Cook setup, then reload this page.',
      unavailable: 'Could not check tracking. Check the Leti Cook tab and extension access, then return here.',
      unknown: 'This channel could not be identified in your saved library. Click to check or add it.',
    };
    control.label.textContent = state.status === 'pending' ? 'Adding…' : success || 'Leti Cook';
    const visualState = success ? (state.stale ? 'stale' : 'success') : state.status === 'pending' ? 'pending'
      : state.status === 'unconfirmed' ? 'unconfirmed' : state.status ? 'error'
        : state.lookup === 'checking' ? 'checking' : lookupMessages[state.lookup] ? 'unknown' : 'idle';
    control.button.setAttribute('data-state', visualState);
    control.button.disabled = state.status === 'pending' || Boolean(success) || state.status === 'unconfirmed';
    const description = state.status === 'pending' ? 'Adding channel to Leti Cook'
      : success ? (state.stale ? 'Previously confirmed in Leti Cook' : `${success} in Leti Cook`)
        : lookupMessages[state.lookup] || 'Add this channel to Leti Cook';
    control.button.setAttribute('aria-label', description);
    control.wrapper.setAttribute('aria-busy', String(state.status === 'pending'));
    control.message.textContent = state.stale
      ? 'Previously confirmed in Leti Cook; current status could not be refreshed. Open Leti Cook and return here to verify.'
      : success && state.requiresAttention
      ? 'Open Leti Cook to finish loading this channel.' : messages[state.status]
        || lookupMessages[state.lookup] || description;
    if (state.diagnostic) control.message.textContent += ` (LC-${state.diagnostic})`;
    control.wrapper.setAttribute('data-diagnostic', state.diagnostic || '');
    control.wrapper.setAttribute('data-state', visualState);
    fitShortsTooltip();
  }

  async function addChannel(event) {
    if (!event.isTrusted || !control || event.currentTarget !== control.button || control.button.disabled) return;
    const target = pageTarget();
    if (!target || target.route !== control.route || target.channelUrl !== control.channelUrl
        || target.anchor !== control.anchor) {
      refresh();
      return;
    }
    const locator = target.channelUrl;
    remember(locator, { status: 'pending' });
    render();
    try {
      const result = await chrome.runtime.sendMessage({ type: 'add-channel', channelUrl: locator });
      const statuses = new Set(['added', 'exists', 'restored', 'setup_required', 'login_required',
        'busy', 'rate_limited', 'unconfirmed', 'unavailable', 'rejected']);
      remember(locator, statuses.has(result?.status)
        ? { status: result.status, requiresAttention: result.requiresAttention === true }
        : { status: 'unconfirmed' });
    } catch {
      // The request may already have reached the application. Never replay it.
      remember(locator, { status: 'unconfirmed' });
    }
    render();
  }

  function statusGlyph() {
    // Static vector geometry only: no page markup or external image requests.
    const node = (tag, attributes) => {
      const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
      for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
      return element;
    };
    const glyph = node('svg', { class: 'leti-cook-glyph', viewBox: '0 0 24 24',
      'aria-hidden': 'true', focusable: 'false', fill: 'none', stroke: 'currentColor',
      'stroke-width': '1.7', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' });
    const check = node('path', { class: 'leti-cook-check', d: 'm5 12 4.5 4.5L19 7', pathLength: '1' });
    glyph.append(check);
    return glyph;
  }

  // Shadow DOM keeps site/other-extension button and span rules out of the control.
  const CONTROL_STYLE = `
    :host { color-scheme: light dark; }
    *, *::before, *::after { box-sizing: border-box; }
    button { all: initial; box-sizing: border-box; position: relative; display: grid;
      place-items: center; width: 34px; height: 34px; padding: 0; border: 0;
      border-radius: 50%; background: #63dfc6; color: #073e38; cursor: pointer;
      isolation: isolate; transition: background 180ms ease, transform 180ms ease; }
    button:hover:not(:disabled) { background: #83eed8; transform: translateY(-1px); }
    button:active:not(:disabled) { transform: scale(.92); }
    button:focus-visible { outline: 2px solid #00a991; outline-offset: 3px; }
    button:disabled { cursor: default; }
    .leti-cook-plus { position: absolute; width: 18px; height: 18px;
      background: linear-gradient(115deg,#093b52,#5c25b5,#b72660,#114e72,#093b52);
      background-size: 300% 100%; animation: leti-cook-colors 5s ease-in-out infinite alternate;
      clip-path: polygon(42% 0,58% 0,58% 42%,100% 42%,100% 58%,58% 58%,58% 100%,42% 100%,42% 58%,0 58%,0 42%,42% 42%);
      border-radius: 2px; transition: opacity 160ms ease; }
    .leti-cook-glyph { width: 20px; height: 20px; pointer-events: none; }
    .leti-cook-check { opacity: 0; stroke-width: 2.4; }
    .leti-cook-unknown { display: none; font: 600 19px/1 Arial,sans-serif; position: absolute; }
    .leti-cook-unknown::before { content: '?'; }
    button[data-state="success"] .leti-cook-plus, button[data-state="stale"] .leti-cook-plus,
    button[data-state="unknown"] .leti-cook-plus, button[data-state="checking"] .leti-cook-plus { opacity: 0; animation: none; }
    button[data-state="success"] .leti-cook-check, button[data-state="stale"] .leti-cook-check { opacity: 1; }
    button[data-state="stale"] { background: #a4c8c1; }
    button[data-state="unknown"] .leti-cook-unknown, button[data-state="checking"] .leti-cook-unknown { display: block; }
    button[data-state="checking"] .leti-cook-unknown { font-size: 0; width: 13px; height: 13px;
      border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%;
      animation: leti-cook-spin 1s linear infinite; }
    button[data-state="pending"] { cursor: progress; }
    button[data-state="pending"] .leti-cook-plus { animation: leti-cook-pending 1.1s ease-in-out infinite; }
    button[data-state="error"], button[data-state="unconfirmed"] { background: #f3cc83; }
    button[data-state="unconfirmed"] .leti-cook-plus { opacity: .45; animation: none; }
    .leti-cook-button-label { position: absolute; width: 1px; height: 1px; overflow: hidden;
      clip-path: inset(50%); white-space: nowrap; }
    .leti-cook-channel-message { position: absolute; z-index: 2147483647; top: 42px;
      inset-inline-start: 0; width: max-content; max-width: min(240px,75vw); padding: 9px 12px;
      border-radius: 10px; background: #16312e; color: #fff; box-shadow: 0 4px 18px #0003;
      font: 500 12px/1.5 Arial,sans-serif; overflow-wrap: anywhere;
      opacity: 0; visibility: hidden; pointer-events: none; transition: opacity 120ms ease; }
    :host(:hover) .leti-cook-channel-message, :host(:focus-within) .leti-cook-channel-message {
      opacity: 1; visibility: visible; }
    :host([data-placement="floating"]) .leti-cook-channel-message { top: auto; bottom: 42px;
      inset-inline-start: auto; right: 0; }
    :host([data-placement="shorts"]) button { width: 48px; height: 48px; }
    :host([data-placement="shorts"]) .leti-cook-plus { width: 24px; height: 24px; }
    :host([data-placement="shorts"]) .leti-cook-glyph { width: 28px; height: 28px; }
    :host([data-placement="shorts"]) button[data-state="unknown"] .leti-cook-unknown { font-size: 24px; }
    :host([data-placement="shorts"]) button[data-state="checking"] .leti-cook-unknown { width: 18px; height: 18px; }
    :host([data-placement="shorts"]) .leti-cook-channel-message { top: 0;
      inset-inline-start: auto; left: calc(100% + 10px); right: auto; max-height: calc(100vh - 16px);
      overflow: auto; pointer-events: auto; }
    :host([data-placement="shorts"][data-tooltip-room="unavailable"]) .leti-cook-channel-message {
      display: none; }
    @keyframes leti-cook-colors { to { background-position: 100% 50%; } }
    @keyframes leti-cook-pending { 50% { transform: rotate(90deg) scale(.75); opacity: .6; } }
    @keyframes leti-cook-spin { to { transform: rotate(360deg); } }
    @media(prefers-reduced-motion:reduce) { *, *::before, *::after { animation: none!important;
      transition: none!important; transform: none!important; } }
    @media(forced-colors:active) { button { border: 1px solid ButtonText; }
      .leti-cook-plus { background: ButtonText; } }
  `;

  function releaseOwnerRoom() {
    const patch = control?.roomPatch;
    if (!patch) return;
    const { owner } = control;
    // Restore only our still-owned override, preserving concurrent site changes.
    if (owner.style.getPropertyValue('--leti-cook-owner-room') === patch.applied) {
      if (patch.value) owner.style.setProperty('--leti-cook-owner-room', patch.value, patch.priority);
      else owner.style.removeProperty('--leti-cook-owner-room');
    }
    if (owner.getAttribute('data-leti-cook-owner-room') === '1') {
      if (patch.attribute === null) owner.removeAttribute('data-leti-cook-owner-room');
      else owner.setAttribute('data-leti-cook-owner-room', patch.attribute);
    }
    control.roomPatch = null;
  }

  function fitOwnerRoom() {
    const { owner, row, actions } = control;
    // vidIQ's watch controls cap the entire author row at 620px. On a wide
    // page this wraps the name despite empty space before the native actions.
    // Override only that observed cap, and only within measured available room.
    if (!row || !owner.className.split(/\s+/).includes('vidiq-scope')
        || owner.style.maxWidth !== '620px') { releaseOwnerRoom(); return; }
    const ownerBox = owner.getBoundingClientRect();
    const rowBox = row.getBoundingClientRect();
    // The actions container stretches across unused space. Use the actual
    // visible controls, otherwise its left edge keeps us stuck at the old cap.
    const actionBoxes = actions ? [...actions.querySelectorAll('button')]
      .filter(button => visible(button)).map(button => button.getBoundingClientRect())
      .filter(box => box.top < ownerBox.bottom && box.bottom > ownerBox.top) : [];
    const actionWidth = actionBoxes.length ? Math.max(...actionBoxes.map(box => box.right))
      - Math.min(...actionBoxes.map(box => box.left)) + 32 : 0;
    const edge = Math.min(rowBox.right, innerWidth) - actionWidth;
    const available = Math.floor(edge - ownerBox.left);
    // Preserve the provider's cap when there is no extra room. The marker also
    // restores native one-line ellipsis after vidIQ sets the author to wrap.
    const value = `${Number.isFinite(available) && available > 622 ? available : 620}px`;
    if (!control.roomPatch) control.roomPatch = {
      value: owner.style.getPropertyValue('--leti-cook-owner-room'),
      priority: owner.style.getPropertyPriority('--leti-cook-owner-room'),
      attribute: owner.getAttribute('data-leti-cook-owner-room'),
    };
    if (owner.style.getPropertyValue('--leti-cook-owner-room') !== value) owner.style.setProperty('--leti-cook-owner-room', value);
    if (owner.getAttribute('data-leti-cook-owner-room') !== '1') owner.setAttribute('data-leti-cook-owner-room', '1');
    control.roomPatch.applied = value;
  }

  function fitShortsTooltip() {
    if (control?.placement !== 'shorts') return;
    const { wrapper, message } = control;
    const box = wrapper.getBoundingClientRect();
    const left = Math.ceil(box.right + 10);
    const width = Math.min(240, Math.floor(innerWidth - left - 8));
    // Never move feedback over the video or the action rail. If the viewport
    // leaves no readable space on the right, retain the button's accessible
    // label and wait for space instead of displaying an overflowing tooltip.
    wrapper.setAttribute('data-tooltip-room', width >= 48 ? 'available' : 'unavailable');
    message.style.maxWidth = `${Math.max(0, width)}px`;
    const height = message.getBoundingClientRect().height || 0;
    message.style.top = `${Math.max(8, Math.min(box.top, innerHeight - height - 8)) - box.top}px`;
  }

  function fitControl() {
    fitShortsTooltip();
    if (!control?.owner) return;
    fitOwnerRoom();
    const { wrapper, owner, author } = control;
    const info = author.querySelectorAll('#upload-info')[0] || author.querySelectorAll('#channel-name')[0];
    if (!info) return;
    // Compare the row with our slot excluded, including text height. A fixed
    // width threshold misses names that start wrapping after late controls load.
    wrapper.setAttribute('data-placement', 'measuring');
    const nativeHeight = owner.getBoundingClientRect().height;
    const nativeInfo = info.getBoundingClientRect();
    wrapper.setAttribute('data-placement', 'inline');
    const withControl = info.getBoundingClientRect();
    const cramped = withControl.width < Math.min(100, nativeInfo.width) - 1
      || withControl.height > nativeInfo.height + 2;
    const stretched = owner.getBoundingClientRect().height > nativeHeight + 2;
    if (cramped || stretched || nativeInfo.width < 80) wrapper.setAttribute('data-placement', 'floating');
  }

  function observeLayout() {
    if ((!control.owner && control.placement !== 'shorts') || typeof ResizeObserver !== 'function') return;
    control.resizeObserver = new ResizeObserver(scheduleRefresh);
    for (const node of new Set([control.owner, control.author, control.anchor, control.row, control.actions,
      ...(control.placement === 'shorts' ? [control.wrapper] : [])])) {
      if (node) control.resizeObserver.observe(node);
    }
  }

  function refresh() {
    const target = pageTarget();
    if (control && (!target || target.channelUrl !== control.channelUrl || target.anchor !== control.anchor
        || target.owner !== control.owner || target.author !== control.author
        || target.row !== control.row || target.actions !== control.actions
        || target.route !== control.route || !control.wrapper.isConnected)) {
      control.resizeObserver?.disconnect();
      releaseOwnerRoom();
      control.wrapper.remove();
      control = null;
    }
    if (!target || control) { if (control) fitControl(); return; }
    const wrapper = document.createElement('span');
    wrapper.className = WRAPPER_CLASS;
    const shadow = wrapper.attachShadow({ mode: 'open' });
    const style = document.createElement('style');
    style.textContent = CONTROL_STYLE;
    shadow.append(style);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'leti-cook-add-channel';
    button.addEventListener('click', addChannel);
    const label = document.createElement('span');
    label.className = 'leti-cook-button-label';
    const plus = document.createElement('span');
    plus.className = 'leti-cook-plus';
    plus.setAttribute('aria-hidden', 'true');
    const unknown = document.createElement('span');
    unknown.className = 'leti-cook-unknown';
    unknown.setAttribute('aria-hidden', 'true');
    button.append(plus, statusGlyph(), unknown, label);
    const message = document.createElement('span');
    message.className = 'leti-cook-channel-message';
    message.setAttribute('role', 'status');
    message.setAttribute('aria-live', 'polite');
    shadow.append(button, message);
    if (target.placement === 'shorts') {
      wrapper.setAttribute('data-placement', 'shorts');
      target.anchor.insertAdjacentElement('afterbegin', wrapper);
    } else target.anchor.insertAdjacentElement('afterend', wrapper);
    control = { ...target, wrapper, button, label, message };
    fitControl();
    observeLayout();
    render();
    void checkChannel(target);
  }

  function scheduleRefresh() {
    if (scheduled !== null) return;
    // Coalescing rather than a reset-on-every-mutation timer avoids starvation
    // when YouTube keeps changing captions or counters during playback.
    scheduled = setTimeout(() => { scheduled = null; refresh(); }, 150);
  }
  const observer = new MutationObserver(changes => {
    if (changes.some(change => !change.target.closest?.(`.${WRAPPER_CLASS}`))) scheduleRefresh();
  });
  observer.observe(document.documentElement, {
    subtree: true, childList: true, attributes: true,
    attributeFilter: ['href', 'video-id', 'hidden', 'is-active', 'aria-hidden', 'class', 'style'],
  });
  document.addEventListener('yt-navigate-start', () => {
    canonicalBeforeNavigation = canonicalLink(); initialChannel = null;
    navigating = true; refresh();
  });
  document.addEventListener('yt-navigate-finish', () => { navigating = false; scheduleRefresh(); });
  window.addEventListener('popstate', scheduleRefresh);
  // Shorts can scroll its internal container before its route/attributes change.
  document.addEventListener('scroll', scheduleRefresh, { capture: true, passive: true });
  window.addEventListener('resize', scheduleRefresh);
  window.addEventListener('focus', () => { if (control) void checkChannel(control); });
  refresh();
})();
