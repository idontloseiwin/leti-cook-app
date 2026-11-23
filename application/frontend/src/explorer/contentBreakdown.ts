let englishLanguageNames: Intl.DisplayNames | null | undefined;

function languageNames(): Intl.DisplayNames | null {
  if (englishLanguageNames !== undefined) return englishLanguageNames;
  try {
    englishLanguageNames = new Intl.DisplayNames(['en'], {
      type: 'language',
      fallback: 'none',
    });
  } catch {
    englishLanguageNames = null;
  }
  return englishLanguageNames;
}

export function formatOfficialCategoryLabel(label: string | null | undefined): string {
  const normalized = String(label || '').trim();
  return normalized && !/^(?:category\s+)?\d+$/i.test(normalized)
    ? normalized
    : 'Category name unavailable';
}

export function formatLanguageDisplayName(code: string, fallbackLabel: string): string {
  const normalizedCode = code.trim();
  const fallback = fallbackLabel.trim() || normalizedCode;
  if (!normalizedCode) return fallback;
  try {
    return languageNames()?.of(normalizedCode) || fallback;
  } catch {
    return fallback;
  }
}
