"""Request authentication between the web app and the signer service.

WHY
The signer's /execute was unauthenticated. "Only reachable on the compose
network" is a property of today's compose file, not of the service: any other
container added to that network, any SSRF in the web app, and anything that
can reach the port on a misconfigured host could ask it to sign. The signer's
policy checks bound WHAT it signs; nothing established WHO was asking.

HOW
HMAC-SHA256 over (timestamp, method, path, SHA-256 of the body) with a shared
secret that lives in both services' environments and never crosses the wire.
Compared in constant time. A timestamp outside +/-MAX_SKEW_S is refused, which
bounds replay; /execute is additionally idempotent on client_order_id, so a
replay inside the window cannot sign twice either.

Standard library only: this file is copied into BOTH images unchanged, like
tx_verify.py and execution_rails.py.
"""
import hashlib
import hmac
import os
import time
from typing import Dict, Optional, Tuple

HEADER_TS = "X-Signer-Timestamp"
HEADER_SIG = "X-Signer-Signature"
MAX_SKEW_S = 30
# 32 characters of hex is 128 bits. Anything shorter is refused outright:
# a guessable shared secret is an unauthenticated endpoint with extra steps.
MIN_SECRET_LEN = 32
SECRET_ENV = "SIGNER_SHARED_SECRET"


def load_secret(env: str = SECRET_ENV) -> Optional[bytes]:
    """The shared secret, or None when it is unset or too short to trust."""
    value = os.environ.get(env, "").strip()
    if len(value) < MIN_SECRET_LEN:
        return None
    return value.encode("utf-8")


def _signing_base(ts: str, method: str, path: str, body: bytes) -> bytes:
    return b"\n".join([
        ts.encode("ascii"),
        method.upper().encode("ascii"),
        path.encode("utf-8"),
        hashlib.sha256(body or b"").hexdigest().encode("ascii"),
    ])


def sign(secret: bytes, method: str, path: str, body: bytes,
         now: Optional[float] = None) -> Dict[str, str]:
    """Headers authenticating one request."""
    ts = str(int(time.time() if now is None else now))
    mac = hmac.new(secret, _signing_base(ts, method, path, body), hashlib.sha256).hexdigest()
    return {HEADER_TS: ts, HEADER_SIG: mac}


def verify(secret: Optional[bytes], method: str, path: str, body: bytes,
           ts: Optional[str], signature: Optional[str],
           now: Optional[float] = None) -> Tuple[bool, str]:
    """(ok, reason). Every failure refuses; the reason never echoes secrets."""
    if secret is None:
        return False, (f"{SECRET_ENV} is not configured (or shorter than {MIN_SECRET_LEN} "
                       f"characters) -- refusing every request until it is")
    if not ts or not signature:
        return False, "missing authentication headers"
    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return False, "malformed timestamp"
    current = time.time() if now is None else now
    if abs(current - ts_int) > MAX_SKEW_S:
        return False, "request timestamp outside the allowed window"
    expected = hmac.new(secret, _signing_base(str(ts_int), method, path, body),
                        hashlib.sha256).hexdigest().encode("ascii")
    # Bytes on both sides: compare_digest raises on non-ASCII str, and a
    # header is attacker-controlled.
    if not hmac.compare_digest(expected, signature.strip().lower().encode("utf-8", "replace")):
        return False, "bad signature"
    return True, "ok"
