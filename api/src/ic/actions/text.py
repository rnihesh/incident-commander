"""Neutralize visitor- or LLM-originated text before it reaches GitHub or Discord."""

from __future__ import annotations

import re
import unicodedata

ZWSP = "​"
ELLIPSIS = "…"
_ESCAPED = set("\\`[]()<>!*_~|")
_RUNS = re.compile(r"`+")


def _clean(s: str, *, keep_zwsp: bool = False) -> str:
    """Drop control and format chars (newlines and tabs become spaces), collapse spaces."""
    out = []
    for ch in str(s):
        cat = unicodedata.category(ch)
        if ch in "\n\r\t":
            out.append(" ")
        elif ch == ZWSP and keep_zwsp:
            out.append(ch)
        elif cat in ("Cc", "Cs", "Cf"):
            continue  # includes zero-width chars that could hide a mention
        else:
            out.append(ch)
    return re.sub(r" {2,}", " ", "".join(out)).strip()


def inert(s: str, max_len: int) -> str:
    """Make ``s`` safe to embed in markdown: no mentions, links, images or backticks,
    no control characters, at most ``max_len`` characters (ellipsis when cut)."""
    if max_len < 1:
        return ""
    pieces: list[str] = []
    for ch in _clean(s):
        if ch == "@":
            pieces.append("@" + ZWSP)
        elif ch in _ESCAPED:
            pieces.append("\\" + ch)
        else:
            pieces.append(ch)
    if sum(map(len, pieces)) <= max_len:
        return "".join(pieces)
    out: list[str] = []
    used = 0
    for p in pieces:
        if used + len(p) > max_len - 1:
            break
        out.append(p)
        used += len(p)
    return "".join(out) + (ELLIPSIS if max_len > 1 else "")


def code_span(s: str) -> str:
    """Wrap in a markdown code span whose fence is longer than any backtick run inside,
    so the content can never close the span."""
    s = _clean(s, keep_zwsp=True)
    longest = max((len(m) for m in _RUNS.findall(s)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if s.startswith("`") or s.endswith("`") else ""
    return f"{fence}{pad}{s}{pad}{fence}"


_PR_LINK = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def defang_urls(s: str) -> str:
    """Break bare URLs so Discord does not auto-link them (``inert`` leaves them intact)."""
    s = s.replace("://", f":{ZWSP}//")
    return re.sub(r"(?i)www\.", lambda m: m.group(0)[:3] + ZWSP + ".", s)


def inert_discord(s: str, max_len: int, *, allow_pr_link: bool = False) -> str:
    """``inert`` plus URL neutralization, still within ``max_len`` characters.

    ``allow_pr_link`` keeps a lone GitHub pull-request URL clickable (our own PR link)."""
    if allow_pr_link and _PR_LINK.fullmatch(str(s).strip()):
        return inert(s, max_len)
    budget = max_len
    while True:
        out = defang_urls(inert(s, budget))
        over = len(out) - max_len
        if over <= 0 or budget <= 1:
            return out[:max_len]
        budget -= over
