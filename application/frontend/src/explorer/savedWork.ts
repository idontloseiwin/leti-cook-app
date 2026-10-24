/** Shared saved-object primitives. No browser storage or fetched YouTube values. */
export class SavedWorkContractError extends Error {
  constructor(message: string) { super(`Saved work response is incompatible: ${message}`); this.name = 'SavedWorkContractError'; }
}

export function savedRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new SavedWorkContractError('expected an object.');
  return value as Record<string, unknown>;
}
export function savedId(value: unknown): string {
  if (typeof value !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
    throw new SavedWorkContractError('expected a UUIDv4 identity.');
  }
  return value.toLowerCase();
}
export function savedName(value: unknown): string {
  if (typeof value !== 'string') throw new Error('Enter a name between 1 and 120 characters.');
  const name = value.trim();
  const invalidUnicode = [...value].some((character) => {
    const point = character.codePointAt(0)!;
    return point >= 0xd800 && point <= 0xdfff;
  });
  if (!name || [...name].length > 120 || /[\u0000-\u001f\u007f]/.test(value) || invalidUnicode) throw new Error('Enter a name between 1 and 120 characters, without control characters.');
  return name;
}
export function newSavedWorkId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  // getRandomValues is also available on trusted-network HTTP entries.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
export function savedRevision(value: unknown): number {
  if (!Number.isSafeInteger(value) || Number(value) < 1) throw new SavedWorkContractError('invalid revision.');
  return value as number;
}
export function savedTimestamp(value: unknown): string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) || !Number.isFinite(Date.parse(value))) {
    throw new SavedWorkContractError('invalid timestamp.');
  }
  return value;
}
export function savedFailure(error: unknown): { message: string; uncertain: boolean; conflict: boolean; deleted: boolean } {
  const status = error && typeof error === 'object' && 'status' in error ? Number(error.status) : undefined;
  return {
    message: error instanceof Error ? error.message.slice(0, 360) : 'Saved work is unavailable.',
    uncertain: status === undefined || status === 408 || status >= 500,
    conflict: status === 409,
    deleted: status === 410,
  };
}

/** Fence for replaceable reads. Refs also use it to reject duplicate in-flight work. */
export class SavedWorkRequestFence {
  private generation = 0;
  private active: object | null = null;
  reset(): void { this.generation += 1; this.active = null; }
  begin(): { generation: number } | null {
    if (this.active) return null;
    const ticket = { generation: this.generation };
    this.active = ticket;
    return ticket;
  }
  accepts(ticket: { generation: number }): boolean { return this.active === ticket && ticket.generation === this.generation; }
  finish(ticket: { generation: number }): void { if (this.accepts(ticket)) this.active = null; }
}
