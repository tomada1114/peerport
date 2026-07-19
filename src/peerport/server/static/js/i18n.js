// i18n.js — locale catalog loading and t(key, params) lookup (#15).
//
// Both catalogs load at startup; the active one follows config.toml's
// `locale`. A key missing from the active catalog falls back to the
// English (en) fallback catalog, never a blank string or the raw key
// alone (the raw key is the last resort for keys missing everywhere,
// which the catalogs' identical key sets prevent).

let activeCatalog = {};
let fallbackCatalog = {};
let activeLocale = "en";

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`fetch failed: ${url} (${response.status})`);
  }
  return response.json();
}

export async function initI18n() {
  const config = await fetchJson("/api/config");
  activeLocale = config.locale ?? "en";
  fallbackCatalog = await fetchJson("/api/locales/en");
  activeCatalog =
    activeLocale === "en"
      ? fallbackCatalog
      : await fetchJson(`/api/locales/${activeLocale}`);
}

export function t(key, params = {}) {
  const template = activeCatalog[key] ?? fallbackCatalog[key] ?? key;
  return template.replace(/\{(\w+)\}/g, (match, name) =>
    name in params ? String(params[name]) : match,
  );
}

export function locale() {
  return activeLocale;
}

// Test hook: inject catalogs without network (also used by dev tooling).
export function _setCatalogs(active, fallback, localeName = "en") {
  activeCatalog = active;
  fallbackCatalog = fallback;
  activeLocale = localeName;
}
