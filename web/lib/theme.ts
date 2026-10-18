// Light / dark theme: the system preference until the visitor picks one, then their pick from
// localStorage. The <html data-theme> attribute is the single source of truth; globals.css
// swaps the colour tokens on it and the graph canvas re-reads them when it changes.

export type Theme = "light" | "dark";

export const THEME_KEY = "ic.theme";

/** Inline in <head> so data-theme is set before first paint (no flash of the wrong theme). */
export const THEME_BOOT_SCRIPT = `(function(){try{var t=localStorage.getItem("${THEME_KEY}");if(t!=="light"&&t!=="dark")t=window.matchMedia("(prefers-color-scheme: light)").matches?"light":"dark";document.documentElement.dataset.theme=t}catch(e){document.documentElement.dataset.theme="dark"}})()`;

export function isTheme(v: unknown): v is Theme {
  return v === "light" || v === "dark";
}

/** The stored pick, or the system preference when there is none. */
export function resolveTheme(stored: string | null, prefersLight: boolean): Theme {
  return isTheme(stored) ? stored : prefersLight ? "light" : "dark";
}

export function currentTheme(): Theme {
  if (typeof document === "undefined") return "dark";
  const t = document.documentElement.dataset.theme;
  return isTheme(t) ? t : "dark";
}

export function applyTheme(t: Theme, persist: boolean) {
  document.documentElement.dataset.theme = t;
  if (!persist) return;
  try {
    window.localStorage.setItem(THEME_KEY, t);
  } catch {
    /* storage blocked: the pick lasts for this page only */
  }
}

/** Calls back whenever <html data-theme> changes. */
export function subscribeTheme(cb: () => void): () => void {
  const mo = new MutationObserver(cb);
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return () => mo.disconnect();
}
