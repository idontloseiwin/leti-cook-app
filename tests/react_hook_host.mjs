// A minimal React hook host for offline node tests (not a test itself): state,
// refs, memo/callback, effects after each render with cleanup by dependencies,
// and a new render on every state change. Pass `host.React` as the 'react'
// import of a module compiled for the test, then `host.mount(useHook, props)`.
// No DOM, no scheduler: effects run synchronously after the render that
// queued them, like React's effects after commit.
export function reactHookHost() {
  const slots = [];
  let index = 0, queue = [], rendering = false, again = false, hook, props, result;
  const changed = (before, after) => !before || !after || before.length !== after.length
    || before.some((value, position) => !Object.is(value, after[position]));
  const render = () => {
    if (rendering) { again = true; return; }
    rendering = true;
    try {
      do {
        again = false; index = 0;
        result = hook(props);
        const jobs = queue; queue = [];
        for (const job of jobs) job();
      } while (again);
    } finally { rendering = false; }
  };
  const React = {
    useState(initial) {
      const at = index++;
      if (!slots[at]) {
        const slot = { value: typeof initial === 'function' ? initial() : initial };
        slot.set = (next) => {
          const value = typeof next === 'function' ? next(slot.value) : next;
          if (Object.is(value, slot.value)) return;
          slot.value = value;
          render();
        };
        slots[at] = slot;
      }
      return [slots[at].value, slots[at].set];
    },
    useRef(initial) {
      const at = index++;
      slots[at] ??= { current: initial };
      return slots[at];
    },
    useMemo(make, deps) {
      const at = index++;
      if (!slots[at] || changed(slots[at].deps, deps)) slots[at] = { value: make(), deps };
      return slots[at].value;
    },
    useCallback(callback, deps) { return React.useMemo(() => callback, deps); },
    useEffect(effect, deps) {
      const at = index++;
      const previous = slots[at];
      if (previous && deps && !changed(previous.deps, deps)) return;
      queue.push(() => {
        previous?.cleanup?.();
        const cleanup = effect();
        slots[at] = { deps, cleanup: typeof cleanup === 'function' ? cleanup : undefined };
      });
    },
  };
  React.useLayoutEffect = React.useEffect;
  return {
    React,
    mount(useHook, initialProps) { hook = useHook; props = initialProps; render(); return this; },
    /** Render again with new props (a parent re-render). */
    update(nextProps) { props = nextProps; render(); },
    get current() { return result; },
    unmount() { for (const slot of slots) slot?.cleanup?.(); },
  };
}

/** Let pending promise callbacks run. */
export const flush = async () => { for (let step = 0; step < 20; step += 1) await Promise.resolve(); };
