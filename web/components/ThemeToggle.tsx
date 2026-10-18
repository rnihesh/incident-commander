"use client";

import { useEffect, useSyncExternalStore } from "react";
import { THEME_KEY, applyTheme, currentTheme, resolveTheme, subscribeTheme } from "../lib/theme";

/** Sun / moon switch in the page header. Follows the system setting until pressed. */
export function ThemeToggle() {
  const theme = useSyncExternalStore(subscribeTheme, currentTheme, () => null);

  // With no saved pick, keep following the system setting while the page is open.
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => {
      let stored: string | null = null;
      try { stored = window.localStorage.getItem(THEME_KEY); } catch { /* blocked */ }
      if (stored === null) applyTheme(resolveTheme(null, mq.matches), false);
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const next = theme === "light" ? "dark" : "light";
  const label = `Switch to ${next} theme`;
  return (
    <button
      type="button"
      onClick={() => applyTheme(next, true)}
      aria-label={label}
      title={label}
      className="inline-flex size-7 shrink-0 items-center justify-center rounded-ctl text-dim hover:bg-raise hover:text-ink"
    >
      {/* Before hydration the theme is unknown on the server: render an empty box of the same size. */}
      {theme === "light" ? <Moon /> : theme === "dark" ? <Sun /> : null}
    </button>
  );
}

function Sun() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden>
      <circle cx="8" cy="8" r="3" />
      <path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1" />
    </svg>
  );
}

function Moon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" aria-hidden>
      <path d="M13.5 9.6A5.6 5.6 0 0 1 6.4 2.5a5.6 5.6 0 1 0 7.1 7.1z" />
    </svg>
  );
}
