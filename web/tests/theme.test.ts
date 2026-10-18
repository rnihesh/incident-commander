import { describe, expect, it } from "vitest";
import { THEME_BOOT_SCRIPT, THEME_KEY, isTheme, resolveTheme } from "../lib/theme";

describe("theme", () => {
  it("prefers the stored pick over the system setting", () => {
    expect(resolveTheme("dark", true)).toBe("dark");
    expect(resolveTheme("light", false)).toBe("light");
  });

  it("falls back to the system setting when nothing valid is stored", () => {
    expect(resolveTheme(null, true)).toBe("light");
    expect(resolveTheme(null, false)).toBe("dark");
    expect(resolveTheme("sepia", true)).toBe("light");
  });

  it("only accepts the two themes", () => {
    expect(isTheme("light")).toBe(true);
    expect(isTheme("dark")).toBe(true);
    expect(isTheme("")).toBe(false);
    expect(isTheme(undefined)).toBe(false);
  });

  it("boot script reads the same storage key and sets data-theme", () => {
    expect(THEME_BOOT_SCRIPT).toContain(`localStorage.getItem("${THEME_KEY}")`);
    expect(THEME_BOOT_SCRIPT).toContain("dataset.theme");
    expect(THEME_BOOT_SCRIPT).toContain("prefers-color-scheme: light");
  });
});
