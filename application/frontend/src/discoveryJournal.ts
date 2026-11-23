/** Passive observations only. Receipt identity stays with the exact returned row.
 * No browser report changes freshness, collection state or server exposure. */
type Receipt = { read_id: string; index: number; expires: number; rendered?: boolean };
const receipts = new WeakMap<object, Receipt>();
const pending = new Map<string, { read_id: string; status: 'received' | 'rendered'; indices: Set<number> }>();
let sending = false;
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);

async function drain() {
  if (sending) return;
  sending = true;
  try {
    while (pending.size) {
      const [key, value] = pending.entries().next().value!;
      pending.delete(key);
      try {
        await fetch('/diagnostics/discovery-journal/browser', {
          method: 'POST', credentials: 'same-origin', cache: 'no-store',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...value, indices: [...value.indices].slice(0, 50) }),
          signal: AbortSignal.timeout(3000),
        });
      } catch { /* No retry or UI error: diagnostics never block a read/render. */ }
    }
  } finally { sending = false; }
}

function report(receipt: Receipt, status: 'received' | 'rendered') {
  if (Date.now() >= receipt.expires) return;
  const key = `${receipt.read_id}:${status}`;
  if (!pending.has(key) && pending.size >= 128) return;
  const group = pending.get(key) ?? { read_id: receipt.read_id, status, indices: new Set<number>() };
  group.indices.add(receipt.index);
  pending.set(key, group);
}

export function observeDiscoveryResponse(payload: unknown, headers: Headers): void {
  try {
    const read_id = headers.get('X-Leti-Discovery-Read');
    if (!read_id || !/^[a-f0-9]{32}$/.test(read_id) || !record(payload) || !record(payload.context)
      || !Array.isArray(payload.rows) || !payload.rows.length || payload.rows.length > 50) return;
    const expires = Date.parse(String(payload.context.valid_before));
    if (!Number.isFinite(expires) || expires <= Date.now()) return;
    payload.rows.forEach((row: unknown, index: number) => {
      if (!record(row) || typeof row.id !== 'string' || !/^[A-Za-z0-9_-]{11}$/.test(row.id)) return;
      const receipt = { read_id, index, expires };
      receipts.set(row, receipt);
      report(receipt, 'received');
    });
    void drain();
  } catch { /* Fail soft, including environments without optional APIs. */ }
}

export function transferDiscoveryReceipt(source: unknown, target: object): void {
  try {
    if (record(source)) {
      const receipt = receipts.get(source);
      if (receipt) receipts.set(target, receipt);
    }
  } catch { /* Never change parsing. */ }
}

export function reportDiscoveryRendered(videos: readonly object[]): void {
  try {
    for (const video of videos.slice(0, 50)) {
      const receipt = receipts.get(video);
      if (receipt && !receipt.rendered) {
        receipt.rendered = true;
        report(receipt, 'rendered');
      }
    }
    void drain();
  } catch { /* React committed these rows; reporting remains optional evidence. */ }
}
