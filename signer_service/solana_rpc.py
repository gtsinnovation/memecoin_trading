# solana_rpc.py
"""Minimal Solana JSON-RPC helpers the signer service needs: fetching a
recent blockhash to build a transaction, broadcasting a signed one, and
polling for confirmation. Deliberately tiny and dependency-free (raw
JSON-RPC over httpx) rather than pulling in a full RPC client library --
the signer service only ever needs these three calls.
"""
import os
import re
import asyncio
import logging
from urllib.parse import urlsplit
from typing import Optional

import httpx

logger = logging.getLogger("signer_service.solana_rpc")

# Defaults to devnet, not mainnet -- Stage 3 only executes on devnet (see
# STAGE3_SETUP.md and README.md).
#
# An earlier version of this comment claimed that changing this to a mainnet
# URL "does NOT by itself enable real-money execution -- SIGNER_MODE gates
# that separately". That was FALSE. devnet_transfer_test never read this
# variable, so pointing it at mainnet and leaving the mode alone would sign
# and broadcast a real mainnet transfer paying real fees.
#
# main.startup() now refuses to start when SIGNER_MODE=devnet_transfer_test
# and this does not resolve to devnet, so the gate the old comment described
# now actually exists. Treat this variable with real caution regardless.
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.devnet.solana.com")


def safe_endpoint(url: str) -> str:
    """scheme://host ONLY -- never the path, query string or userinfo.

    Providers put the credential in different places: Helius and QuickNode in
    the query (?api-key=...), Triton in the path (/<token>), some in userinfo.
    The host alone is never secret and is all an operator needs to read a log.
    """
    try:
        parts = urlsplit(url or "")
        return f"{parts.scheme or 'https'}://{parts.hostname or '?'}"
    except Exception:
        return "<unparseable RPC URL>"


def redact(text) -> str:
    """Strip the configured RPC endpoint's path/query/userinfo from any text.

    httpx.HTTPStatusError renders the FULL request URL into its message, and
    the signer interpolates exception text into execution_audit_log rows, into
    the JSON body it returns, and -- via the web app -- into system_alerts. A
    429 on getLatestBlockhash therefore wrote the RPC key into three durable
    places. This is applied at the source, in _rpc_call, so no downstream
    `{e}` can re-leak it.
    """
    if text is None:
        return ""
    out = str(text)
    host = None
    try:
        host = urlsplit(SOLANA_RPC_URL or "").hostname
    except Exception:
        pass
    if SOLANA_RPC_URL:
        out = out.replace(SOLANA_RPC_URL, safe_endpoint(SOLANA_RPC_URL))
    if host:
        # Any rendering of a URL on the configured host keeps only scheme+host,
        # whatever normalisation httpx applied (trailing slash, re-encoding).
        out = re.sub(r"(https?://)(?:[^@/\s'\"]*@)?(" + re.escape(host) + r")[^\s'\"]*",
                     r"\1\2/<redacted>", out)
    return out


class SolanaRpcError(Exception):
    """Transport-level or node-level failure. For a broadcast, a transport
    failure is AMBIGUOUS: the node may have accepted the transaction before
    the connection dropped."""
    pass


class SolanaRpcRejected(SolanaRpcError):
    """The node answered with a JSON-RPC error: it definitively refused."""
    pass


# Expected genesis hashes (docs.anza.xyz/clusters/available). The network a
# node serves is a property of its GENESIS, not of the hostname an operator
# typed. A hostname check trusts the operator's label; this asks the chain.
GENESIS_HASHES = {
    "devnet": "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG",
    "testnet": "4uhcVJyU9pJkvQyS88uRDiswHXSCkY3zQawwpjk2NsNY",
    "mainnet": "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
}


def network_for_genesis(genesis_hash) -> str:
    for name, h in GENESIS_HASHES.items():
        if genesis_hash == h:
            return name
    return "unknown"


async def _rpc_call(client: httpx.AsyncClient, method: str, params: list):
    # Every transport failure is re-raised as SolanaRpcError with a REDACTED
    # message, and `from None` drops the original from the chain -- otherwise
    # a traceback would still print the httpx exception, full URL included.
    try:
        resp = await client.post(SOLANA_RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        }, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        raise SolanaRpcError(
            f"{method} failed: HTTP {e.response.status_code} from "
            f"{safe_endpoint(SOLANA_RPC_URL)}") from None
    except httpx.HTTPError as e:
        raise SolanaRpcError(
            f"{method} failed: {type(e).__name__}: {redact(e)}") from None
    except ValueError as e:
        raise SolanaRpcError(f"{method} failed: unparseable response ({redact(e)})") from None
    if not isinstance(data, dict):
        raise SolanaRpcError(f"{method} failed: response is not a JSON-RPC object")
    if "error" in data:
        raise SolanaRpcRejected(f"{method} failed: {redact(data['error'])}")
    if "result" not in data:
        raise SolanaRpcError(f"{method} failed: response carries no result")
    return data["result"]


async def get_genesis_hash(client: httpx.AsyncClient) -> str:
    result = await _rpc_call(client, "getGenesisHash", [])
    if not isinstance(result, str):
        raise SolanaRpcError("getGenesisHash returned a non-string result")
    return result


async def get_balance_lamports(client: httpx.AsyncClient, pubkey: str) -> int:
    result = await _rpc_call(client, "getBalance", [pubkey, {"commitment": "confirmed"}])
    value = (result or {}).get("value") if isinstance(result, dict) else None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SolanaRpcError("getBalance returned no usable lamport value")
    return value


async def get_latest_blockhash(client: httpx.AsyncClient) -> str:
    result = await _rpc_call(client, "getLatestBlockhash", [{"commitment": "finalized"}])
    return result["value"]["blockhash"]


async def send_raw_transaction(client: httpx.AsyncClient, signed_tx_bytes: bytes) -> str:
    """Broadcasts a fully signed transaction and returns its signature
    (the tx's own first signature, which IS its identifier on Solana --
    not a separate ID). skipPreflight is left False on purpose: preflight
    simulation catches an invalid transaction before it's actually
    submitted, which is exactly what you want for anything this
    security-sensitive, even on devnet."""
    import base64
    b64_tx = base64.b64encode(signed_tx_bytes).decode("ascii")
    result = await _rpc_call(client, "sendTransaction", [
        b64_tx, {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}
    ])
    return result  # the tx signature, base58-encoded


async def confirm_transaction(client: httpx.AsyncClient, signature: str,
                                max_attempts: int = 20, poll_interval_s: float = 1.5) -> bool:
    """Polls getSignatureStatuses until the transaction is confirmed (or
    finalized), fails, or max_attempts is exhausted. Returns True only on
    a confirmed/finalized status with no error -- everything else
    (timeout, or a status with a non-null err field) returns False, and
    the caller is expected to log that as a real "did this actually land
    on-chain?" unknown rather than assume success."""
    for attempt in range(max_attempts):
        result = await _rpc_call(client, "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        status = (result.get("value") or [None])[0]
        if status is not None:
            if status.get("err") is not None:
                logger.error(f"Transaction {signature} failed on-chain: {status['err']}")
                return False
            confirmation_status = status.get("confirmationStatus")
            if confirmation_status in ("confirmed", "finalized"):
                return True
        await asyncio.sleep(poll_interval_s)
    logger.warning(f"Transaction {signature} not confirmed after {max_attempts} attempts -- status unknown, not necessarily failed.")
    return False
