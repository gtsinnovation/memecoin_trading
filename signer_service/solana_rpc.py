# solana_rpc.py
"""Minimal Solana JSON-RPC helpers the signer service needs: fetching a
recent blockhash to build a transaction, broadcasting a signed one, and
polling for confirmation. Deliberately tiny and dependency-free (raw
JSON-RPC over httpx) rather than pulling in a full RPC client library --
the signer service only ever needs these three calls.
"""
import os
import asyncio
import logging
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


class SolanaRpcError(Exception):
    pass


async def _rpc_call(client: httpx.AsyncClient, method: str, params: list):
    resp = await client.post(SOLANA_RPC_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    }, timeout=15.0)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise SolanaRpcError(f"{method} failed: {data['error']}")
    return data["result"]


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
