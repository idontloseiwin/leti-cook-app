const countryNames = typeof Intl.DisplayNames === 'function'
  ? new Intl.DisplayNames(['en'], { type: 'region' })
  : undefined;

export function formatCountryName(value?: string | null): string {
  const code = String(value || '').trim().toUpperCase();
  if (!/^[A-Z]{2}$/.test(code)) return 'Country not reported';
  return countryNames?.of(code) || code;
}

export function formatChannelAge(
  value?: string | null,
  now = new Date(),
): string {
  if (!value) return 'Age not reported';
  const createdAt = new Date(value);
  if (Number.isNaN(createdAt.getTime()) || Number.isNaN(now.getTime())) {
    return 'Age not reported';
  }
  if (createdAt.getTime() > now.getTime()) return 'Age not reported';
  let years = now.getUTCFullYear() - createdAt.getUTCFullYear();
  const anniversary = Date.UTC(
    now.getUTCFullYear(),
    createdAt.getUTCMonth(),
    createdAt.getUTCDate(),
    createdAt.getUTCHours(),
    createdAt.getUTCMinutes(),
    createdAt.getUTCSeconds(),
    createdAt.getUTCMilliseconds(),
  );
  if (now.getTime() < anniversary) years -= 1;
  if (years < 1) return '<1 year on YouTube';
  return `${years} ${years === 1 ? 'year' : 'years'} on YouTube`;
}
