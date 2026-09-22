import i18n, { type Resource, type ResourceKey } from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';

export const NAMESPACES = [
  'common',
  'shell',
  'chat',
  'manage',
  'agents',
  'misc',
] as const;

export const FALLBACK_LANGUAGE = 'en';

/**
 * The languages this bundle ships, read from what is on disk.
 *
 * A language is a directory under `locales/`, so adding one is adding its
 * files. The switcher and `scripts/check_i18n.py` consume this discovered set,
 * so a new locale needs no second registry.
 */
const modules = import.meta.glob<{ default: ResourceKey }>(
  './locales/*/*.json',
  { eager: true },
);

const resources: Resource = {};
for (const [path, module] of Object.entries(modules)) {
  const match = /\.\/locales\/([^/]+)\/([^/]+)\.json$/.exec(path);
  if (!match) continue;
  const [, language, namespace] = match;
  (resources[language] ??= {})[namespace] = module.default;
}

/**
 * Each shipped language under its own name.
 *
 * `Intl.DisplayNames` asked in the language itself, because a chooser a reader
 * cannot read is no chooser: someone who only reads Japanese needs to find
 * 日本語, not "Japanese". The code is the fallback for a runtime that cannot
 * name it.
 */
export const SHIPPED_LANGUAGES: ReadonlyArray<{ code: string; label: string }> =
  Object.keys(resources)
    .sort()
    .map((code) => {
      let label = code;
      try {
        label = new Intl.DisplayNames([code], { type: 'language' }).of(code) ?? code;
      } catch {
        // A runtime without this language's data names it by its code.
      }
      return { code, label };
    });

/** The shipped language a detected or stored code resolves to. */
export function resolveLanguage(candidate: string | undefined): string {
  const base = (candidate ?? '').split('-')[0].toLowerCase();
  return SHIPPED_LANGUAGES.some((l) => l.code === base) ? base : FALLBACK_LANGUAGE;
}

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
    fallbackLng: FALLBACK_LANGUAGE,
    defaultNS: 'common',
    ns: [...NAMESPACES],
    detection: {
      // A Chinese-browser user auto-gets zh; everyone else en. A manual switch
      // persists to localStorage and wins on the next load.
      order: ['localStorage', 'navigator'],
      caches: ['localStorage'],
      lookupLocalStorage: 'astrabox-lang',
    },
    interpolation: {
      // React already escapes against XSS.
      escapeValue: false,
    },
    // Missing keys fall back to the key/fallbackLng instead of returning null,
    // so a missing string never renders as a literal `null`.
    returnNull: false,
  });

/**
 * Keep `<html lang>` on the language i18next resolved.
 *
 * The attribute is what a screen reader picks its voice and its pronunciation
 * rules from, and `index.html` can only state one language for a bundle that
 * ships two. i18next resolves the language after detection, so the value is
 * read here rather than written at build time.
 */
function syncDocumentLanguage(lng: string) {
  if (typeof document !== 'undefined' && lng) {
    document.documentElement.lang = lng.split('-')[0];
  }
}
syncDocumentLanguage(i18n.resolvedLanguage ?? i18n.language);
i18n.on('languageChanged', syncDocumentLanguage);

export default i18n;
