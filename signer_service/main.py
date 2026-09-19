# signer_service/main.py
"""Standalone signing microservice -- deliberately a SEPARATE application
from the main trading-pipeline app (main.py at the project root), with its
own Docker container, its own environment, and the ONLY place in this
codebase that ever holds Turnkey credentials. See README.md's "Real
market data / DEX / wallet architecture" section and STAGE3_SETUP.md for
why this is a separate service rather than code inside the pipeline: if
the pipeline app were ever compromised or simply had a bug, it still
cannot sign anything by itself -- it can only ASK this service to, and
this service independently re-checks the request before ever calling
Turnkey (see policy_guard.py for that second layer; Turnkey's own policy
engine, configured directly in your Turnkey org, is the third).

SIGNER_MODE controls what "execute" actually does on-chain:
  - "devnet_transfer_test" (the only mode implemented in Stage 3): signs
    and broadcasts a tiny SELF-transfer of devnet SOL (the Turnkey wallet
    sending a few thousand lamports to itself). This does NOT buy any
    token -- devnet has no real DEX liquidity for Jupiter to route
    through (see README.md), so there is no such thing as a real devnet
    trade to execute. What this proves is that policy_guard's checks, a
    real Turnkey signing round-trip, and a real Solana broadcast+
    confirmation all work together correctly -- the exact plumbing Stage
    4 (mainnet, real swaps) will reuse unchanged.
  - "mainnet_jupiter_swap": NOT implemented yet. Stage 4 work. Requests
    are refused with a clear message rather than half-implemented.

ENABLE_STAGE3_EXECUTION in the main app (main.py) is what decides whether
the pipeline ever calls this service at all -- it defaults to false. This
service refusing-by-default (empty ALLOWED_EXECUTION_TOKENS, unset
MAX_TRADE_USD) is a second, independent off-switch on this side too.
"""
import os
import math
import logging
import traceback
import sys
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
import asyncpg
import httpx

try:
    import policy_guard
    import solana_tx
    import solana_rpc
    import turnkey_client
except Exception as import_error:
    print("\n" + "!" * 50)
    print("CRITICAL IMPORT EXCEPTION IN signer_service:")
    traceback.print_exc(file=sys.stdout)
    print("!" * 50 + "\n")
    raise import_error

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("signer_service")
app = FastAPI(title="Trading Pipeline Signer Service (Stage 3)")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SIGNER_MODE = os.environ.get("SIGNER_MODE", "devnet_transfer_test")
DEVNET_TEST_TRANSFER_LAMPORTS = int(os.environ.get("DEVNET_TEST_TRANSFER_LAMPORTS", "1000"))
# The shipped .env.example default is 0, which refuses everything -- an
# operator who sets the allowlist but not the cap previously got no signal.
MAX_TRADE_USD_UNSET_WARNING = policy_guard.MAX_TRADE_USD <= 0

_pool: Optional[asyncpg.Pool] = None
_http_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def startup():
    global _pool, _http_client
    _pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=3)
    _http_client = httpx.AsyncClient()
    resolved = _resolve_network()
    logger.info(f"signer_service started. SIGNER_MODE={SIGNER_MODE} network={resolved}")

    # SIGNER_MODE did NOT gate the network, despite two comments claiming it
    # did. devnet_transfer_test never reads SOLANA_RPC_URL, so pointing that
    # variable at mainnet and leaving the mode alone signed and broadcast a
    # REAL mainnet transfer paying real fees. Refuse to start on that
    # combination rather than document it as safe.
    if SIGNER_MODE == "devnet_transfer_test" and resolved != "devnet":
        raise RuntimeError(
            f"SIGNER_MODE=devnet_transfer_test but the configured RPC resolves to "
            f"'{resolved}' (SOLANA_RPC_URL={solana_rpc.SOLANA_RPC_URL!r}). Refusing to "
            f"start: this mode signs and broadcasts a real transfer, and it must only "
            f"ever do so on devnet. Set SOLANA_RPC_URL to a devnet endpoint, or set "
            f"SOLANA_NETWORK explicitly if you are using a private devnet provider."
        )

    # Unbounded env input that policy_guard never sees -- it is not measured
    # in USD, so MAX_TRADE_USD does not constrain it. 0.1 SOL is already
    # 100,000x the 1000-lamport default.
    if DEVNET_TEST_TRANSFER_LAMPORTS > 100_000_000:
        raise RuntimeError(
            f"DEVNET_TEST_TRANSFER_LAMPORTS={DEVNET_TEST_TRANSFER_LAMPORTS} exceeds the "
            f"100000000 (0.1 SOL) ceiling. This value bypasses every USD-denominated "
            f"policy check; it exists to prove plumbing, not to move balances."
        )

    if MAX_TRADE_USD_UNSET_WARNING:
        logger.warning(
            "MAX_TRADE_USD is 0 or unset -- every /execute request will be refused "
            "until it's configured. See STAGE3_SETUP.md."
        )
    if not policy_guard.ALLOWED_EXECUTION_TOKENS:
        logger.warning("ALLOWED_EXECUTION_TOKENS is empty -- every /execute request will be refused until it's configured. See STAGE3_SETUP.md.")


@app.on_event("shutdown")
async def shutdown():
    if _http_client is not None:
        await _http_client.aclose()
    if _pool is not None:
        await _pool.close()


class ExecuteRequest(BaseModel):
    # Bounded to the audit columns' widths (VARCHAR(128)/VARCHAR(50)). Without
    # this, a 200-character token_address makes every execution_audit_log
    # INSERT fail with "value too long" -- and that failure is caught and
    # logged rather than raised, so the attempt vanishes from the audit trail
    # entirely. A caller could suppress its own audit rows on demand.
    token_address: str = Field(min_length=1, max_length=128)
    token_symbol: Optional[str] = Field(default=None, max_length=50)
    requested_usd: float

    @field_validator("requested_usd")
    @classmethod
    def _finite(cls, v: float) -> float:
        # NaN defeats EVERY numeric guard downstream: `nan <= 0`, `nan > cap`
        # and `allocated + nan > cap` are all False, so a NaN sails through the
        # positivity check, the per-trade cap AND the total-capital cap. Python
        # json.loads accepts the bare literal NaN, so this is reachable from
        # the wire. Reject it here, before policy_guard sees it.
        if not math.isfinite(v):
            raise ValueError("requested_usd must be a finite number")
        return v


async def _write_audit_log(token_address: str, token_symbol: Optional[str], requested_usd: float,
                             outcome: str, reason: str, tx_signature: Optional[str], network: str):
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO execution_audit_log
                   (token_address, token_symbol, requested_usd, outcome, reason, tx_signature, network)
                   VALUES ($1, $2, $3, $4, $5, $6, $7);""",
                token_address, token_symbol, requested_usd, outcome, reason, tx_signature, network,
            )
    except Exception as e:
        # The audit write failing must never be silently swallowed, but it
        # also must never block returning the real outcome to the caller
        # (an execution result the operator can't see because a logging
        # write failed would be worse than a slightly incomplete log).
        logger.error(f"Failed to write execution_audit_log: {e}")


@app.get("/health")
async def health():
    return {"status": "ok", "signer_mode": SIGNER_MODE}


@app.get("/whoami")
async def whoami():
    """Convenience endpoint for the STAGE3_SETUP.md smoke test -- confirms
    the configured Turnkey credentials actually authenticate, without
    attempting to sign anything."""
    try:
        result = await turnkey_client.get_whoami(_http_client)
        return result
    except turnkey_client.TurnkeyConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": f"Turnkey request failed: {e}"}, status_code=502)


def _resolve_network() -> str:
    """Which network the audit log records.

    Was: `"devnet" if "devnet" in SOLANA_RPC_URL else "mainnet"`. That is a
    substring test on an operator-supplied URL that routinely contains an
    opaque API key -- `https://mainnet.helius-rpc.com/?api-key=8f3devnet91`
    is labelled devnet, and a local validator at 127.0.0.1:8899 is labelled
    mainnet. This is the column an auditor reads to answer "did real money
    move", so a coin-flip on an API key's characters is not acceptable.

    SOLANA_NETWORK is authoritative when set. Otherwise the URL's HOST is
    inspected -- never the query string or path, where the secrets live.
    """
    explicit = os.environ.get("SOLANA_NETWORK", "").strip().lower()
    if explicit in ("devnet", "testnet", "mainnet"):
        return explicit
    try:
        host = (urlparse(solana_rpc.SOLANA_RPC_URL).hostname or "").lower()
    except Exception:
        return "unknown"
    if "devnet" in host:
        return "devnet"
    if "testnet" in host:
        return "testnet"
    if "mainnet" in host or "api.solana.com" in host:
        return "mainnet"
    # Refusing to guess is more useful than guessing "mainnet" for a local
    # validator, or "devnet" for a private provider whose host says neither.
    return "unknown"


@app.post("/execute")
async def execute(req: ExecuteRequest):
    network = _resolve_network()

    try:
        policy_result = await policy_guard.check_execution_allowed(_pool, req.token_address, req.requested_usd)
    except Exception as e:
        # Fails closed for FUNDS (nothing is signed) but previously failed OPEN
        # for AUDITING: a DB outage produced a stream of anonymous 500s with no
        # record of what was attempted. The audit log lives in the same
        # Postgres so it cannot record this either -- but the caller deserves a
        # structured refusal rather than a bare stack trace.
        reason = f"Policy re-validation could not run ({type(e).__name__}: {e}) -- refusing."
        logger.error(reason)
        return JSONResponse({"executed": False, "reason": reason}, status_code=503)

    if not policy_result.allowed:
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "REFUSED_POLICY", policy_result.reason, None, network)
        return JSONResponse({"executed": False, "reason": policy_result.reason}, status_code=403)

    if SIGNER_MODE == "mainnet_jupiter_swap":
        reason = "SIGNER_MODE=mainnet_jupiter_swap is not implemented yet -- that's Stage 4. Refusing rather than half-executing a real swap."
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "REFUSED_POLICY", reason, None, network)
        return JSONResponse({"executed": False, "reason": reason}, status_code=501)

    if SIGNER_MODE != "devnet_transfer_test":
        reason = f"Unknown SIGNER_MODE '{SIGNER_MODE}'."
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "ERROR", reason, None, network)
        return JSONResponse({"executed": False, "reason": reason}, status_code=500)

    # --- devnet_transfer_test: prove the custody+signing+broadcast path,
    # not a real trade (see module docstring). ---
    try:
        blockhash = await solana_rpc.get_latest_blockhash(_http_client)
        unsigned_tx = solana_tx.build_sol_transfer_tx(
            from_pubkey=turnkey_client.TURNKEY_SOLANA_WALLET_ADDRESS,
            to_pubkey=turnkey_client.TURNKEY_SOLANA_WALLET_ADDRESS,  # self-transfer -- see module docstring
            lamports=DEVNET_TEST_TRANSFER_LAMPORTS,
            recent_blockhash=blockhash,
        )
    except Exception as e:
        reason = f"Failed to build the devnet test transaction: {e}"
        logger.error(reason)
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "ERROR", reason, None, network)
        return JSONResponse({"executed": False, "reason": reason}, status_code=500)

    try:
        signed_hex = await turnkey_client.sign_solana_transaction(_http_client, unsigned_tx)
    except turnkey_client.TurnkeyConfigError as e:
        reason = str(e)
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "ERROR", reason, None, network)
        return JSONResponse({"executed": False, "reason": reason}, status_code=500)
    except turnkey_client.TurnkeySigningError as e:
        reason = str(e)
        logger.warning(f"Turnkey refused to sign: {reason}")
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "REFUSED_TURNKEY", reason, None, network)
        return JSONResponse({"executed": False, "reason": reason}, status_code=403)
    except Exception as e:
        # Only the two typed exceptions were caught. An httpx timeout, a
        # connect error, or a malformed JSON body is none of them -- so a
        # request that Turnkey may have COMPLETED (the signature exists; the
        # response was lost on the way back) escaped unhandled and unlogged,
        # leaving a "did we sign or not?" question with no record to settle it.
        reason = f"Turnkey call failed before a result was known: {type(e).__name__}: {e}"
        logger.error(reason)
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "ERROR", reason, None, network)
        return JSONResponse({"executed": False, "reason": reason}, status_code=502)

    try:
        signed_tx_bytes = solana_tx.reassemble_signed_sol_transfer(unsigned_tx, signed_hex)
        tx_signature = await solana_rpc.send_raw_transaction(_http_client, signed_tx_bytes)
    except Exception as e:
        reason = f"Signed successfully but broadcast failed: {e}"
        logger.error(reason)
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "ERROR", reason, None, network)
        return JSONResponse({"executed": False, "reason": reason}, status_code=502)

    # The transaction is ALREADY on-chain at this point. confirm_transaction
    # was the one await in this handler with no try/except: a 429 or a dropped
    # connection on the first getSignatureStatuses poll raised, FastAPI
    # returned a bare 500, and the audit row was never written -- leaving a
    # real fund-moving transaction with NO record of it anywhere, while the
    # caller was told the call failed.
    try:
        confirmed = await solana_rpc.confirm_transaction(_http_client, tx_signature)
        reason = "broadcast and confirmed" if confirmed else "broadcast, but not yet confirmed as of this response -- check tx_signature on an explorer"
    except Exception as e:
        confirmed = False
        reason = (f"broadcast, but the confirmation check failed ({e}) -- the transaction "
                  f"IS on-chain; check tx_signature on an explorer")
        logger.error(reason)
    await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                             "SIGNED_BROADCAST", reason, tx_signature, network)
    return {"executed": True, "confirmed": confirmed, "tx_signature": tx_signature, "network": network, "reason": reason}
