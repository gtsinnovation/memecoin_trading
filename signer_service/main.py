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
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
import asyncpg
import httpx

try:
    import policy_guard
    import solana_tx
    import solana_rpc
    import turnkey_client
    import signer_auth
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
# HMAC key shared with the web app (signer_auth.py). None refuses everything.
AUTH_SECRET = signer_auth.load_secret()
# Serialises reservation across every signer process: checks + insert happen
# under this transaction-scoped lock, so two concurrent orders cannot both
# pass a cap that only one of them fits under.
RESERVATION_LOCK_KEY = 7_311_993_001

_pool: Optional[asyncpg.Pool] = None
_http_client: Optional[httpx.AsyncClient] = None
# The network the RPC endpoint actually serves, established from its genesis
# hash. None until verified; nothing is signed while it is None.
_GENESIS = {"network": None}


async def _verify_genesis() -> Optional[str]:
    """Asks the chain which network this is. Returns the name, or None when
    the RPC could not be reached (which is not the same as a mismatch)."""
    try:
        genesis = await solana_rpc.get_genesis_hash(_http_client)
    except Exception as e:
        logger.warning(f"getGenesisHash failed ({e}) -- network not yet verified.")
        return None
    _GENESIS["network"] = solana_rpc.network_for_genesis(genesis)
    return _GENESIS["network"]


def _genesis_refusal(network: Optional[str]) -> Optional[str]:
    """Why this mode may not sign on this network, or None."""
    if network is None:
        return "the RPC's network could not be verified (getGenesisHash unreachable) -- refusing to sign blind"
    if SIGNER_MODE == "devnet_transfer_test" and network != "devnet":
        return (f"SIGNER_MODE=devnet_transfer_test but the RPC's genesis hash is {network}'s -- "
                f"refusing: this mode must only ever sign on devnet")
    return None


@app.on_event("startup")
async def startup():
    global _pool, _http_client
    # Bounded: a policy query stuck on a lock must fail (and refuse), not hold
    # the request -- and the reservation lock -- forever.
    _pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=3,
                                      timeout=10, command_timeout=15)
    _http_client = httpx.AsyncClient()
    resolved = _resolve_network()
    logger.info(f"signer_service started. SIGNER_MODE={SIGNER_MODE} network(host)={resolved}")

    # Refuse to start on an inconsistent cap set before anything can be signed.
    policy_guard.assert_caps_consistent()

    if AUTH_SECRET is None:
        logger.error(f"{signer_auth.SECRET_ENV} is not set (or shorter than "
                     f"{signer_auth.MIN_SECRET_LEN} characters) -- every authenticated "
                     f"endpoint will refuse until it is. See STAGE3_SETUP.md.")

    # SIGNER_MODE did NOT gate the network, despite two comments claiming it
    # did. The hostname test below is kept as a fast first refusal; the
    # GENESIS HASH check after it is the authoritative one, because a hostname
    # is only what the operator typed.
    if SIGNER_MODE == "devnet_transfer_test" and resolved not in ("devnet", "unknown"):
        raise RuntimeError(
            f"SIGNER_MODE=devnet_transfer_test but the configured RPC resolves to "
            f"'{resolved}' (endpoint {solana_rpc.safe_endpoint(solana_rpc.SOLANA_RPC_URL)}). Refusing to "
            f"start: this mode signs and broadcasts a real transfer, and it must only "
            f"ever do so on devnet. Set SOLANA_RPC_URL to a devnet endpoint."
        )
    network = await _verify_genesis()
    if network is not None and _genesis_refusal(network):
        raise RuntimeError(_genesis_refusal(network) + " Refusing to start.")

    # A signer holding a superuser connection can rewrite its own order
    # ledger, audit log and the positions it cross-checks. STAGE3_SETUP.md
    # Part 2b creates a scoped role; with real funds it is mandatory.
    try:
        async with _pool.acquire() as conn:
            is_super = await conn.fetchval(
                "SELECT rolsuper FROM pg_roles WHERE rolname = current_user;")
    except Exception as e:
        is_super = None
        logger.warning(f"Could not read this service's database role: {e}")
    if is_super:
        if SIGNER_MODE != "devnet_transfer_test":
            raise RuntimeError("The signer is connected as a database SUPERUSER. Refusing to start "
                               "outside devnet -- create the scoped signer_svc role (STAGE3_SETUP.md "
                               "Part 2b) and point DATABASE_URL at it.")
        logger.warning("The signer is connected as a database superuser. Acceptable on devnet; "
                       "use the scoped signer_svc role (STAGE3_SETUP.md Part 2b) before Stage 4.")

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
    if policy_guard.MAX_ORDERS_PER_DAY <= 0:
        logger.warning("SIGNER_MAX_ORDERS_PER_DAY is 0 or unset -- every /execute request will be refused.")
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
    # The idempotency key (active_positions.client_order_id, a UUID). One key
    # is one order, ever: signer_orders' primary key makes a replay or retry
    # return the first outcome instead of signing again.
    client_order_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")

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
                             outcome: str, reason: str, tx_signature: Optional[str], network: str) -> bool:
    """Returns True when the audit row was written, False when it was not."""
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO execution_audit_log
                   (token_address, token_symbol, requested_usd, outcome, reason, tx_signature, network)
                   VALUES ($1, $2, $3, $4, $5, $6, $7);""",
                token_address, token_symbol, requested_usd, outcome, reason, tx_signature, network,
            )
        return True
    except Exception as e:
        # The audit write failing must never be silently swallowed, but it
        # also must never block returning the real outcome to the caller
        # (an execution result the operator can't see because a logging
        # write failed would be worse than a slightly incomplete log).
        #
        # It must also not be reported as success. A confirmed on-chain
        # transaction with no audit row is exactly the state an operator
        # needs to know about, so the outcome is returned to the caller and
        # surfaced in the response rather than living only in a log line.
        logger.error(f"Failed to write execution_audit_log: {e}")
        return False


async def _reserve(req: ExecuteRequest, sol_lamports: Optional[int], network: str):
    """Policy checks and the order reservation, as ONE locked transaction.

    Returns (policy_result, None) for a new order, or (None, existing_row)
    for a previously seen key. Exact payload retries return its first outcome;
    a changed token, amount, or network is marked as an idempotency conflict.
    """
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1);", RESERVATION_LOCK_KEY)
            existing = await conn.fetchrow(
                "SELECT status, tx_signature, reason, network, token_address, requested_usd "
                "FROM signer_orders "
                "WHERE client_order_id = $1;", req.client_order_id)
            if existing is not None:
                existing = dict(existing)
                # A key identifies one immutable order payload. Returning the
                # first order's successful outcome for a retry that changes
                # mint, size, or network can bind that outcome to a different
                # pipeline reservation and create a misleading audit record.
                # Compare the canonical stored payload before treating this as
                # an idempotent retry; a mismatch must never return a signature.
                try:
                    same_amount = Decimal(str(existing["requested_usd"])) == Decimal(
                        str(req.requested_usd))
                except (InvalidOperation, TypeError, ValueError):
                    same_amount = False
                same_payload = (
                    existing["token_address"] == req.token_address
                    and same_amount
                    and existing["network"] == network
                )
                if not same_payload:
                    existing["idempotency_conflict"] = True
                return None, existing
            policy = await policy_guard.check_execution_allowed(
                conn, req.token_address, req.requested_usd, req.client_order_id,
                sol_lamports, SIGNER_MODE)
            await conn.execute(
                "INSERT INTO signer_orders (client_order_id, token_address, requested_usd, "
                "status, reason, network) VALUES ($1, $2, $3, $4, $5, $6);",
                req.client_order_id, req.token_address, req.requested_usd,
                "RESERVED" if policy.allowed else "REFUSED", policy.reason, network)
    if policy.allowed:
        policy_guard.LEDGER.record(req.requested_usd)
    return policy, None


async def _finish(client_order_id: str, status: str, reason: str,
                  tx_signature: Optional[str]) -> None:
    """Records the order's outcome. A failure here leaves it RESERVED, which
    still counts against every cap -- the safe direction."""
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE signer_orders SET status = $2, reason = $3, tx_signature = $4, "
                "updated_at = CURRENT_TIMESTAMP WHERE client_order_id = $1;",
                client_order_id, status, reason, tx_signature)
    except Exception as e:
        logger.critical(f"Could not record outcome {status} for order {client_order_id}: {e} "
                        f"-- it stays RESERVED and counted.")


def _auth_or_401(request: Request, body: bytes):
    ok, why = signer_auth.verify(
        AUTH_SECRET, request.method, request.url.path, body,
        request.headers.get(signer_auth.HEADER_TS), request.headers.get(signer_auth.HEADER_SIG))
    if ok:
        return None
    # Logged, deliberately NOT audited: an unauthenticated caller must not be
    # able to write rows into the audit log.
    logger.warning(f"Refused unauthenticated {request.method} {request.url.path}: {why}")
    return JSONResponse({"executed": False, "order_status": None,
                         "reason": f"unauthenticated: {why}"}, status_code=401)


@app.get("/health")
async def health():
    return {"status": "ok", "signer_mode": SIGNER_MODE,
            "network_verified": _GENESIS["network"]}


@app.get("/whoami")
async def whoami(request: Request):
    """Convenience endpoint for the STAGE3_SETUP.md smoke test -- confirms
    the configured Turnkey credentials actually authenticate, without
    attempting to sign anything. Authenticated: it spends the Turnkey key."""
    denied = _auth_or_401(request, b"")
    if denied is not None:
        return denied
    try:
        result = await turnkey_client.get_whoami(_http_client)
        return result
    except turnkey_client.TurnkeyConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": f"Turnkey request failed: {e}"}, status_code=502)


@app.get("/orders/{client_order_id}")
async def order_status(client_order_id: str, request: Request):
    """Read-only outcome lookup for the web app's reconciliation. It can
    never cause a signature, which is why reconciliation uses it instead of
    re-sending /execute."""
    denied = _auth_or_401(request, b"")
    if denied is not None:
        return denied
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, tx_signature, reason, network FROM signer_orders "
                "WHERE client_order_id = $1;", client_order_id)
    except Exception as e:
        return JSONResponse({"found": None, "reason": f"order ledger unreadable: {type(e).__name__}"},
                            status_code=503)
    if row is None:
        return JSONResponse({"found": False, "order_status": None}, status_code=404)
    return {"found": True, "order_status": row["status"], "tx_signature": row["tx_signature"],
            "reason": row["reason"], "network": row["network"]}


def _resolve_network() -> str:
    """Which network the RPC URL's HOST names -- a first, cheap check only.

    Was: `"devnet" if "devnet" in SOLANA_RPC_URL else "mainnet"`. That is a
    substring test on an operator-supplied URL that routinely contains an
    opaque API key -- `https://mainnet.helius-rpc.com/?api-key=8f3devnet91`
    is labelled devnet, and a local validator at 127.0.0.1:8899 is labelled
    mainnet. SOLANA_NETWORK is authoritative for the LABEL when set; the
    genesis hash (_verify_genesis) is authoritative for what may be SIGNED.
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


def _reply(status_code: int, order_status: Optional[str], reason: str, **extra):
    body = {"executed": False, "order_status": order_status, "reason": reason}
    body.update(extra)
    return JSONResponse(body, status_code=status_code)


@app.post("/execute")
async def execute(request: Request):
    """Authenticate, then parse. The HMAC covers the RAW body, so it is
    verified before a single byte is interpreted."""
    raw = await request.body()
    denied = _auth_or_401(request, raw)
    if denied is not None:
        return denied
    try:
        req = ExecuteRequest.model_validate_json(raw)
    except ValidationError as e:
        return _reply(422, None, f"invalid request: {e.error_count()} field error(s)")
    return await _execute(req)


async def _execute(req: ExecuteRequest):
    # The label an auditor reads: the chain's own answer when we have it.
    network = _GENESIS["network"] or _resolve_network()

    # Mode first: an unimplemented mode never reserves anything.
    if SIGNER_MODE == "mainnet_jupiter_swap":
        reason = "SIGNER_MODE=mainnet_jupiter_swap is not implemented yet -- that's Stage 4. Refusing rather than half-executing a real swap."
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "REFUSED_POLICY", reason, None, network)
        return _reply(501, None, reason)
    if SIGNER_MODE != "devnet_transfer_test":
        reason = f"Unknown SIGNER_MODE '{SIGNER_MODE}'."
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "ERROR", reason, None, network)
        return _reply(501, None, reason)

    # Which chain is this, really? Verified once, from the genesis hash.
    if _GENESIS["network"] is None:
        await _verify_genesis()
    refusal = _genesis_refusal(_GENESIS["network"])
    if refusal:
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "REFUSED_POLICY", refusal, None, network)
        return _reply(503, None, refusal)
    network = _GENESIS["network"]

    # The wallet's SOL balance, for the fee rail. Unreadable is None, which
    # the rail refuses -- never an assumed sufficient balance.
    try:
        sol_lamports = await solana_rpc.get_balance_lamports(
            _http_client, turnkey_client.TURNKEY_SOLANA_WALLET_ADDRESS)
    except Exception as e:
        logger.warning(f"getBalance failed: {e}")
        sol_lamports = None

    try:
        policy_result, existing = await _reserve(req, sol_lamports, network)
    except Exception as e:
        # Fails closed for FUNDS (nothing is reserved or signed). The audit
        # log lives in the same Postgres, so it cannot record this either --
        # but the caller deserves a structured refusal, not a stack trace.
        reason = f"Policy re-validation could not run ({type(e).__name__}: {e}) -- refusing."
        logger.error(reason)
        return _reply(503, None, reason)

    if existing is not None:
        if existing.get("idempotency_conflict"):
            reason = ("IDEMPOTENCY_CONFLICT: client_order_id was already bound to "
                      f"token={existing['token_address']}, "
                      f"requested_usd={existing['requested_usd']}, "
                      f"network={existing['network']}; refusing the changed payload")
            # Audit the canonical stored order, never the caller's replacement
            # values. The conflict response deliberately carries no signature.
            await _write_audit_log(existing["token_address"], None,
                                   float(existing["requested_usd"]),
                                   "IDEMPOTENCY_CONFLICT", reason, None,
                                   existing["network"] or "unknown")
            return _reply(409, "IDEMPOTENCY_CONFLICT", reason,
                          executed=False, client_order_id=req.client_order_id)
        # IDEMPOTENCY. Same key, same answer -- never a second signature.
        reason = (f"duplicate client_order_id: this order was already processed "
                  f"({existing['status']}) -- returning its outcome, not signing again")
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "REFUSED_DUPLICATE", reason, existing["tx_signature"], network)
        return _reply(409, existing["status"], reason, tx_signature=existing["tx_signature"],
                      executed=existing["status"] == "SIGNED_BROADCAST",
                      client_order_id=req.client_order_id)

    if not policy_result.allowed:
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "REFUSED_POLICY", policy_result.reason, None, network)
        return _reply(403, "REFUSED", policy_result.reason, client_order_id=req.client_order_id)

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
        await _finish(req.client_order_id, "FAILED", reason, None)
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "ERROR", reason, None, network)
        return _reply(500, "FAILED", reason, client_order_id=req.client_order_id)

    try:
        signed_hex = await turnkey_client.sign_solana_transaction(_http_client, unsigned_tx)
    except turnkey_client.TurnkeyConfigError as e:
        reason = str(e)
        await _finish(req.client_order_id, "FAILED", reason, None)
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "ERROR", reason, None, network)
        return _reply(500, "FAILED", reason, client_order_id=req.client_order_id)
    except turnkey_client.TurnkeySigningError as e:
        reason = str(e)
        logger.warning(f"Turnkey refused to sign: {reason}")
        await _finish(req.client_order_id, "REFUSED", reason, None)
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "REFUSED_TURNKEY", reason, None, network)
        return _reply(403, "REFUSED", reason, client_order_id=req.client_order_id)
    except Exception as e:
        # Only the two typed exceptions were caught. An httpx timeout, a
        # connect error, or a malformed JSON body is none of them -- so a
        # request that Turnkey may have COMPLETED (the signature exists; the
        # response was lost on the way back) escaped unhandled and unlogged,
        # leaving a "did we sign or not?" question with no record to settle it.
        # UNKNOWN keeps it counted against every cap.
        reason = f"Turnkey call failed before a result was known: {type(e).__name__}: {e}"
        logger.error(reason)
        await _finish(req.client_order_id, "UNKNOWN", reason, None)
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "ERROR", reason, None, network)
        return _reply(502, "UNKNOWN", reason, client_order_id=req.client_order_id)

    try:
        signed_tx_bytes = solana_tx.reassemble_signed_sol_transfer(unsigned_tx, signed_hex)
    except Exception as e:
        # The integrity check refused what Turnkey returned: nothing is sent.
        reason = f"Refusing to broadcast what Turnkey returned: {e}"
        logger.error(reason)
        await _finish(req.client_order_id, "FAILED", reason, None)
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "ERROR", reason, None, network)
        return _reply(502, "FAILED", reason, client_order_id=req.client_order_id)

    try:
        tx_signature = await solana_rpc.send_raw_transaction(_http_client, signed_tx_bytes)
    except solana_rpc.SolanaRpcRejected as e:
        # The node answered and refused (e.g. preflight failure): definitive.
        reason = f"Signed, but the node rejected the broadcast: {e}"
        logger.error(reason)
        await _finish(req.client_order_id, "FAILED", reason, None)
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "ERROR", reason, None, network)
        return _reply(502, "FAILED", reason, client_order_id=req.client_order_id)
    except Exception as e:
        # A transport failure on sendTransaction is AMBIGUOUS: the node may
        # have accepted it before the connection dropped. It used to be
        # reported as "broadcast failed", i.e. as if nothing had happened.
        reason = (f"Signed, but the broadcast outcome is unknown ({e}) -- the transaction "
                  f"may be on-chain until its blockhash expires")
        logger.error(reason)
        await _finish(req.client_order_id, "UNKNOWN", reason, None)
        await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                                 "ERROR", reason, None, network)
        return _reply(502, "UNKNOWN", reason, client_order_id=req.client_order_id)

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
    await _finish(req.client_order_id, "SIGNED_BROADCAST", reason, tx_signature)
    audited = await _write_audit_log(req.token_address, req.token_symbol, req.requested_usd,
                             "SIGNED_BROADCAST", reason, tx_signature, network)
    if not audited:
        reason += " -- WARNING: the execution audit row could not be written; this transaction is on-chain with no local record"
    return {"executed": True, "order_status": "SIGNED_BROADCAST", "confirmed": confirmed,
            "tx_signature": tx_signature, "network": network, "reason": reason,
            "audit_logged": audited, "client_order_id": req.client_order_id}
