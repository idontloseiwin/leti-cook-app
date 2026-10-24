export const VISIBLE_READ_TIMEOUT_MS = 30_000;
export const VISIBLE_READ_RETRY_DELAYS_MS = [5_000, 15_000, 30_000] as const;

type Options<T> = {
  load: (signal: AbortSignal) => Promise<T>;
  visible: () => boolean;
  started: () => void;
  received: (value: T) => void;
  failed: (error: unknown) => void;
  incomplete: (value: T) => boolean;
};

/** One visible panel owns its request, deadline and finite recovery budget. */
export function createVisibleRead<T>(options: Options<T>) {
  let stopped = false;
  let request: AbortController | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let retries = 0;

  const clearTimer = () => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
  };
  const cancel = () => {
    clearTimer();
    const previous = request;
    request = undefined;
    previous?.abort();
  };
  const run = async () => {
    if (stopped || !options.visible() || request) return;
    clearTimer();
    const controller = new AbortController();
    request = controller;
    const current = () => !stopped && request === controller && options.visible();
    options.started();
    let retry = false;
    let removeAbort = () => {};
    const aborted = new Promise<never>((_resolve, reject) => {
      const onAbort = () => reject(new Error('The request timed out. Try again.'));
      controller.signal.addEventListener('abort', onAbort, { once: true });
      removeAbort = () => controller.signal.removeEventListener('abort', onAbort);
    });
    const deadline = setTimeout(() => controller.abort(), VISIBLE_READ_TIMEOUT_MS);
    try {
      const pending = Promise.resolve().then(() => {
        if (!current() || controller.signal.aborted) throw new Error('Read cancelled');
        return options.load(controller.signal);
      });
      const value = await Promise.race([pending, aborted]);
      if (!current() || controller.signal.aborted) return;
      options.received(value);
      retry = options.incomplete(value);
    } catch (error) {
      if (!current()) return;
      options.failed(error);
      retry = true;
    } finally {
      clearTimeout(deadline);
      removeAbort();
      // An old response cannot clear a replacement request or schedule its retry.
      if (request === controller) {
        request = undefined;
        if (!stopped && options.visible() && retry && retries < VISIBLE_READ_RETRY_DELAYS_MS.length) {
          timer = setTimeout(() => void run(), VISIBLE_READ_RETRY_DELAYS_MS[retries++]);
        }
      }
    }
  };
  return {
    refresh() {
      if (stopped || request || !options.visible()) return;
      retries = 0;
      void run();
    },
    visibilityChanged() {
      if (!options.visible()) cancel();
      else this.refresh();
    },
    stop() { stopped = true; cancel(); },
  };
}
