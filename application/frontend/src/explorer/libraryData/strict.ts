// Strict readers for shared-backend responses. Every Library
// adapter parses through these: an unknown key, a wrong type or a broken
// invariant is an ExplorerContractError, which the page shows as "incompatible".
// Nothing is shown partially. Pure TypeScript: no React, window or network.
import { ExplorerContractError } from '../contracts.ts';

export type JsonRecord = Record<string, unknown>;

export const UINT64_MAX = 18446744073709551615n;
const INSTANT = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$/;

export function contractError(path: string, message: string): never {
  throw new ExplorerContractError(`${path}: ${message}`);
}

function plainObject(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
    && [Object.prototype, null].includes(Object.getPrototypeOf(value));
}

/** An object with exactly the required keys plus any of the optional ones. */
export function closedObject(
  value: unknown,
  path: string,
  required: readonly string[],
  optional: readonly string[] = [],
): JsonRecord {
  if (!plainObject(value)) return contractError(path, 'expected an object.');
  for (const key of Object.keys(value)) {
    if (!required.includes(key) && !optional.includes(key)) contractError(`${path}.${key}`, 'unknown key.');
  }
  for (const key of required) {
    if (!Object.hasOwn(value, key)) contractError(`${path}.${key}`, 'missing key.');
  }
  return value;
}

/** An object whose keys are open (for example `coverage.reasons`); values are checked by the caller. */
export function openObject(value: unknown, path: string): JsonRecord {
  if (!plainObject(value)) return contractError(path, 'expected an object.');
  return value;
}

export function array(value: unknown, path: string, maximum = Number.MAX_SAFE_INTEGER): unknown[] {
  if (!Array.isArray(value) || value.length > maximum) return contractError(path, 'expected a bounded list.');
  return value;
}

export function text(value: unknown, path: string, maximum = 4096): string {
  if (typeof value !== 'string' || value.length > maximum) return contractError(path, 'expected text.');
  return value;
}

export function bool(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') return contractError(path, 'expected true or false.');
  return value;
}

/** A closed enumeration: an unknown value is a contract error, never a default. */
export function enumValue<const T extends string>(value: unknown, path: string, allowed: readonly T[]): T {
  if (typeof value !== 'string' || !(allowed as readonly string[]).includes(value)) {
    return contractError(path, `expected one of ${allowed.join(', ')}.`);
  }
  return value as T;
}

/** Unsigned decimal string, at most 2^64 - 1. Stays a string until display (BigInt in format.ts). */
export function uintString(value: unknown, path: string): string {
  if (typeof value !== 'string' || !/^(?:0|[1-9][0-9]{0,19})$/.test(value) || BigInt(value) > UINT64_MAX) {
    return contractError(path, 'expected an unsigned decimal string.');
  }
  return value;
}

/** Signed decimal string with an optional fraction (deltas, rates). */
export function signedDecimal(value: unknown, path: string, maximumFraction = 6): string {
  const pattern = new RegExp(`^-?(?:0|[1-9][0-9]{0,19})(?:\\.[0-9]{1,${maximumFraction}})?$`);
  if (typeof value !== 'string' || !pattern.test(value) || value === '-0' || /^-0(?:\.0+)?$/.test(value)) {
    return contractError(path, 'expected a signed decimal string.');
  }
  return value;
}

/**
 * A count that fits a JavaScript number: totals, page indexes, bucket counts.
 * Use only where the contract bounds the value (for example at most 50,000 rows).
 */
export function count(value: unknown, path: string, maximum = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0 || value > maximum) {
    return contractError(path, 'expected a non-negative whole number.');
  }
  return value;
}

/** A UTC instant: `Z`, at most microseconds, a real calendar time. */
export function utcInstant(value: unknown, path: string): string {
  const match = typeof value === 'string' ? INSTANT.exec(value) : null;
  if (!match) return contractError(path, 'expected a UTC timestamp.');
  const [, year, month, day, hour, minute, second] = match;
  // setUTCFullYear keeps years 1–99 literal (Date.UTC would read them as 19xx).
  const check = new Date(0);
  check.setUTCFullYear(Number(year), Number(month) - 1, Number(day));
  check.setUTCHours(Number(hour), Number(minute), Number(second));
  if (Number(year) < 1 || check.getUTCFullYear() !== Number(year) || check.getUTCMonth() !== Number(month) - 1
    || check.getUTCDate() !== Number(day) || check.getUTCHours() !== Number(hour)
    || check.getUTCMinutes() !== Number(minute) || check.getUTCSeconds() !== Number(second)) {
    return contractError(path, 'expected a real UTC time.');
  }
  return value as string;
}

/** Milliseconds of a checked UTC instant (sub-millisecond digits dropped). */
export function instantMilliseconds(value: string): number {
  return Date.parse(value.replace(/(\.\d{3})\d+Z$/, '$1Z'));
}

/** `^[A-Za-z0-9_-]{11}$`: the only video id shape the Library shows or links. */
export function videoId(value: unknown, path: string): string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{11}$/.test(value)) return contractError(path, 'expected a video id.');
  return value;
}

/** Cell states of the shared result (closed). */
export const CELL_STATES = ['known', 'unknown', 'expired', 'unavailable'] as const;
export type CellState = typeof CELL_STATES[number];

/** How a cell reaches the page. */
export type CellPresentation =
  | { state: 'known'; value: unknown }
  | { state: 'unknown' }      // provider did not report: "Not reported" as today
  | { state: 'expired' }      // value is stale, the row stays: "—" + "Needs refresh"
  | { state: 'unavailable' }; // "—" + "Unavailable"

/** Cell → value or state. Only a known cell carries a value; any other state carries none. */
export function cellPresentation(value: unknown, path: string): CellPresentation {
  const cell = openObject(value, path);
  const state = enumValue(cell.state, `${path}.state`, CELL_STATES);
  if (state === 'known') {
    if (!Object.hasOwn(cell, 'value')) contractError(`${path}.value`, 'a known cell needs a value.');
    return { state, value: cell.value };
  }
  if (cell.value !== null && cell.value !== undefined) contractError(`${path}.value`, `a ${state} cell carries no value.`);
  return { state };
}
