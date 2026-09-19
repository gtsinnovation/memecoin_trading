# turnkey_client.py
"""Thin client for Turnkey's signing API (turnkey.com), used to sign
Solana transactions inside their secure enclave. This is the ONLY file in
this codebase that ever holds Turnkey API credentials, and it never logs
or persists the private key material -- only the public key, org ID, and
per-request activity IDs.

Design choice: we use Turnkey's official `turnkey-api-key-stamper`
package for request stamping (the cryptographic request-signing step --
see docs.turnkey.com/developer-reference/api-overview/stamps) since that
is exactly the kind of hand-rolled crypto code you don't want to write
yourself. Everything else -- the actual HTTP call and the JSON request
shape -- is written directly against Turnkey's documented REST API rather
than through the higher-level generated `turnkey-http` client, because
this SDK is very new (v0.1.0 as of when this was written) and its exact
generated method names for sign_transaction/create_wallet_accounts
weren't independently confirmable at the time this was written -- a
direct, visible HTTP call is easier for you to debug and verify against
the docs than a call into SDK internals whose exact shape wasn't
confirmed. Revisit this if a later, more mature SDK version makes the
generated client clearly preferable.

See STAGE3_SETUP.md for how to create a Turnkey org, API key pair, and
Solana wallet account before any of this can actually be used.
"""
import os
import json
import time
import logging
from typing import Any, Dict, Optional, Tuple

import httpx
from turnkey_api_key_stamper import ApiKeyStamper, ApiKeyStamperConfig

logger = logging.getLogger("signer_service.turnkey_client")

TURNKEY_API_BASE = os.environ.get("TURNKEY_API_BASE", "https://api.turnkey.com")
TURNKEY_ORGANIZATION_ID = os.environ.get("TURNKEY_ORGANIZATION_ID", "")
TURNKEY_API_PUBLIC_KEY = os.environ.get("TURNKEY_API_PUBLIC_KEY", "")
TURNKEY_API_PRIVATE_KEY = os.environ.get("TURNKEY_API_PRIVATE_KEY", "")
TURNKEY_SOLANA_WALLET_ADDRESS = os.environ.get("TURNKEY_SOLANA_WALLET_ADDRESS", "")


class TurnkeyConfigError(Exception):
    """Raised when required Turnkey env vars are missing -- fail loudly
    and early rather than attempting a request that can't succeed."""


class TurnkeySigningError(Exception):
    """Raised when Turnkey rejects or fails to complete a signing
    activity -- includes cases where its OWN policy engine refuses the
    transaction (the third, innermost layer of this app's defense-in-depth
    -- see policy_guard.py for the other two)."""


def _require_config():
    missing = [name for name, val in [
        ("TURNKEY_ORGANIZATION_ID", TURNKEY_ORGANIZATION_ID),
        ("TURNKEY_API_PUBLIC_KEY", TURNKEY_API_PUBLIC_KEY),
        ("TURNKEY_API_PRIVATE_KEY", TURNKEY_API_PRIVATE_KEY),
        ("TURNKEY_SOLANA_WALLET_ADDRESS", TURNKEY_SOLANA_WALLET_ADDRESS),
    ] if not val]
    if missing:
        raise TurnkeyConfigError(
            f"Missing required Turnkey configuration: {', '.join(missing)}. "
            f"See STAGE3_SETUP.md."
        )


def _normalize_stamp(stamp: Any) -> str:
    """Return the X-Stamp header VALUE, whatever shape the stamper returns.

    turnkey-api-key-stamper is v0.1.0 and its published docs show the call
    but not the return type. The TypeScript SDK's equivalent returns an
    object carrying a header name and value, so the Python one may return
    either that or a bare string. Handling both is a few lines; guessing
    wrong is a 502 that looks like an auth failure.
    """
    if isinstance(stamp, str):
        return stamp
    if isinstance(stamp, dict):
        for key in ("stampHeaderValue", "value", "stamp"):
            if isinstance(stamp.get(key), str):
                return stamp[key]
    for attr in ("stamp_header_value", "stampHeaderValue", "value", "stamp"):
        val = getattr(stamp, attr, None)
        if isinstance(val, str):
            return val
    raise TurnkeyConfigError(
        f"Could not read the stamp value from {type(stamp).__name__}. "
        f"Check turnkey-api-key-stamper's return type and update "
        f"_normalize_stamp() in turnkey_client.py."
    )


def _stamped(stamper: ApiKeyStamper, body: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
    """Serialize ONCE, stamp that exact string, and send that exact string.

    Two bugs live here if you're not careful, and this project hit both:

    1. stamper.stamp() takes a JSON STRING, not a dict. Passing a dict
       fails with "'dict' object has no attribute 'encode'".

    2. The stamp is a signature over the EXACT request body. Passing
       `json=body` to httpx makes httpx serialize the dict a SECOND time,
       independently -- and if its output differs from what was stamped by
       even one byte (spacing, key order, unicode escaping), Turnkey sees a
       signature that doesn't match the body and rejects the request. That
       failure looks exactly like bad credentials, which is a miserable
       thing to debug.

    So the caller must use `content=payload`, never `json=body`. Returning
    both together makes it hard to get that wrong.
    """
    payload = json.dumps(body, separators=(",", ":"))
    headers = {
        "X-Stamp": _normalize_stamp(stamper.stamp(payload)),
        "Content-Type": "application/json",
    }
    return payload, headers


def _get_stamper() -> ApiKeyStamper:
    _require_config()
    return ApiKeyStamper(ApiKeyStamperConfig(
        api_public_key=TURNKEY_API_PUBLIC_KEY,
        api_private_key=TURNKEY_API_PRIVATE_KEY,
    ))


async def sign_solana_transaction(client: httpx.AsyncClient, unsigned_tx_bytes: bytes) -> str:
    """Submits an unsigned Solana transaction to Turnkey for signing
    inside its enclave and returns the fully signed transaction,
    hex-encoded.

    Raises TurnkeySigningError if Turnkey's own policy engine refuses the
    transaction, or if the activity otherwise fails -- callers must treat
    that as a hard stop (do not fall back to signing another way; there is
    no other way in this codebase, by design).
    """
    _require_config()
    stamper = _get_stamper()

    body = {
        "type": "ACTIVITY_TYPE_SIGN_TRANSACTION_V2",
        "timestampMs": str(int(time.time() * 1000)),
        "organizationId": TURNKEY_ORGANIZATION_ID,
        "parameters": {
            "signWith": TURNKEY_SOLANA_WALLET_ADDRESS,
            "unsignedTransaction": unsigned_tx_bytes.hex(),
            "type": "TRANSACTION_TYPE_SOLANA",
        },
    }
    payload, headers = _stamped(stamper, body)

    url = f"{TURNKEY_API_BASE}/public/v1/submit/sign_transaction"
    # content=, NOT json= -- the stamp signs these exact bytes. See _stamped().
    resp = await client.post(url, content=payload, headers=headers, timeout=30.0)

    if resp.status_code != 200:
        raise TurnkeySigningError(
            f"Turnkey sign_transaction returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    data = resp.json()
    activity = data.get("activity") or {}
    status = activity.get("status")
    if status != "ACTIVITY_STATUS_COMPLETED":
        # Covers ACTIVITY_STATUS_REJECTED (this is where Turnkey's policy
        # engine refusing the tx shows up -- e.g. a program not on the
        # allowlist, an amount over a policy cap, or a destination not on
        # an allowlist -- see STAGE3_SETUP.md for how those policies are
        # configured) as well as _PENDING/_CONSENSUS_NEEDED (this
        # single-operator setup doesn't use multi-user approval quorums,
        # so either of those indicates a Turnkey-side configuration
        # mismatch, not a normal outcome to poll through).
        raise TurnkeySigningError(
            f"Turnkey activity did not complete (status={status}): "
            f"{activity.get('failure') or data}"
        )

    result = (activity.get("result") or {}).get("signTransactionResult") or {}
    signed_hex = result.get("signedTransaction")
    if not signed_hex:
        raise TurnkeySigningError(f"Turnkey activity completed but returned no signedTransaction: {data}")
    return signed_hex


async def get_whoami(client: httpx.AsyncClient) -> dict:
    """Confirms the configured API key + org ID actually authenticate --
    used by the standalone smoke test in STAGE3_SETUP.md, and a good
    first call to make against any new Turnkey setup before trying to
    sign anything."""
    _require_config()
    stamper = _get_stamper()
    body = {"organizationId": TURNKEY_ORGANIZATION_ID}
    payload, headers = _stamped(stamper, body)
    url = f"{TURNKEY_API_BASE}/public/v1/query/whoami"
    # content=, NOT json= -- see _stamped().
    resp = await client.post(url, content=payload, headers=headers, timeout=15.0)
    resp.raise_for_status()
    return resp.json()
