"""Session cookie, client address and alert-webhook signature helpers (KTD10).

The session id is a server-generated 128-bit hex string carried in the ``ic_sid`` cookie
(httpOnly, Secure unless disabled for plain-http localhost, SameSite=Lax, optional parent
Domain). Unknown or malformed cookies read as "no session"; the caller starts a new one.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress

from fastapi import Request, Response

from ic.config import Settings
from ic.sessions.manager import Session, SessionManager

COOKIE = "ic_sid"
SIGNATURE_HEADER = "X-IC-Signature"
_SIG_PREFIX = "sha256="


def set_session_cookie(response: Response, sid: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE,
        sid,
        max_age=int(settings.session_idle_ttl_s) * 2,
        path="/",
        domain=settings.cookie_domain or None,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def session_cookie(request: Request) -> str | None:
    return request.cookies.get(COOKIE)


def get_session_or_none(request: Request, sessions: SessionManager | None = None) -> Session | None:
    """The caller's session if the cookie names a live one, else None."""
    if sessions is None:
        sessions = request.app.state.ic.sessions
    sess = sessions.get(session_cookie(request))
    if sess is not None:
        sessions.touch(sess.sid)
    return sess


def _is_trusted(addr: str, trusted: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    for t in trusted:
        try:
            if ip in ipaddress.ip_network(t, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request, settings: Settings) -> str:
    """The socket peer, or, when the peer is a trusted proxy, the right-most address in
    ``X-Forwarded-For`` that is not itself a trusted proxy (left-most entries are
    client-controlled and never believed)."""
    peer = request.client.host if request.client else "unknown"
    if not _is_trusted(peer, settings.trusted_proxies):
        return peer
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return peer
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return peer  # garbage in the chain: fall back to the proxy itself
        if not _is_trusted(hop, settings.trusted_proxies):
            return hop
    return hops[0] if hops else peer


def sign(body: bytes, secret: str) -> str:
    return _SIG_PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, header: str | None, secret: str | None) -> bool:
    """Constant-time check of ``X-IC-Signature: sha256=<hex>`` over the raw body. With no
    secret configured, every request is rejected."""
    if not secret or not header or not header.startswith(_SIG_PREFIX):
        return False
    return hmac.compare_digest(sign(body, secret).encode(), header.strip().encode())
