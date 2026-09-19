# main.py
import os
import secrets
import asyncio
import json
import random
import logging
import sys
import traceback
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
import asyncpg
import httpx
# The async pool serves the dashboard queries; psycopg2 is used only by the
# sync helpers below, which run in the pipeline's worker thread alongside
# engine.py's own psycopg2 work rather than mixing drivers in one path.
import psycopg2

# Diagnostic block to print detailed local code tracebacks if an import fails.
try:
    from engine import (
        agent_network, DB_DSN, evaluate_open_positions, check_kill_switch,
        get_app_settings, resume_trading, pause_trading, set_run_status,
        write_system_alert, AgentUnresponsiveError, get_agent_latencies,
    )
    import market_data
    import token_discovery
    import paper_trading
    import app_time
except Exception as import_error:
    print("\n" + "!" * 50)
    print("CRITICAL IMPORT EXCEPTION DETECTED IN YOUR LOCAL PROJECT FILES:")

    traceback.print_exc(file=sys.stdout)
    print("!" * 50 + "\n")
    raise import_error

# Real Solana token addresses the pipeline evaluates each tick (Stage 1 of
# the DEX/wallet integration path -- see README.md). Comma-separated in the
# WATCHLIST_TOKEN_ADDRESSES env var. We deliberately do NOT ship any
# hardcoded default addresses here -- a wrong or stale address baked into
# the code is worse than an empty watchlist with a clear warning, so you
# choose the real tokens yourself (see README.md for where to find them).
WATCHLIST_TOKEN_ADDRESSES = [
    addr.strip() for addr in os.environ.get("WATCHLIST_TOKEN_ADDRESSES", "").split(",") if addr.strip()
]

# Stage 3 (real signing -- see STAGE3_SETUP.md). This app never has any
# Turnkey credentials -- it only ever ASKS the separate signer service to
# act, over plain HTTP on the internal Docker network, and the signer
# independently re-validates every request before doing anything (see
# signer_service/policy_guard.py). Defaults to off; the signer service
# itself also doesn't start unless explicitly enabled (see
# docker-compose.yml's "stage3" profile) -- this is a second, independent
# off-switch, not the only one.
# Stage 2 paper trading. With no watchlist configured the pipeline used to
# warn and idle; it now discovers live candidates instead, which is what
# makes the experiment produce an independent sample rather than the same
# few tokens over and over. Set a watchlist to override discovery entirely.
ENABLE_TOKEN_DISCOVERY = os.environ.get("ENABLE_TOKEN_DISCOVERY", "true").strip().lower() == "true"

ENABLE_STAGE3_EXECUTION = os.environ.get("ENABLE_STAGE3_EXECUTION", "false").strip().lower() == "true"
SIGNER_SERVICE_URL = os.environ.get("SIGNER_SERVICE_URL", "http://signer:8100")

# Log in the operator's zone as well, so a log line and the dashboard clock
# agree. converter takes a struct_time, so this goes through localtime of the
# configured zone rather than the container's UTC.
# force=True matters: engine.py runs its own basicConfig at import time, which
# is BEFORE this line, and basicConfig is a no-op once the root logger has a
# handler. Without force, this format is silently ignored and every log line
# keeps the default no-timestamp layout.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    force=True,
)
logging.Formatter.converter = lambda *args: app_time.now_local().timetuple()
logger = logging.getLogger("main_server")
app = FastAPI(title="Integrated Production Analytics and Telemetry Control Node")

# --- Authentication (single authorized Gmail address, no Google OAuth) ------
# By design this is a simple email-match gate, not real Google identity
# verification: the visitor just types the one authorized email address and
# is granted a session if it matches AUTHORIZED_GOOGLE_EMAIL. There is no
# OAuth handshake, no Google client ID/secret, and nothing confirms the
# visitor actually controls that Gmail account. That's an intentional
# trade-off for a single-operator setup with no external client secrets to
# manage -- treat AUTHORIZED_GOOGLE_EMAIL itself as the shared secret here
# (don't publish it), and note that anyone who knows/guesses that address can
# sign in. If real Google sign-in verification is ever needed, this is the
# spot to reintroduce an OAuth flow.
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY", "")
if not SESSION_SECRET_KEY:
    logger.warning(
        "SESSION_SECRET_KEY is not set -- generating a temporary key for this "
        "process only. Sessions will not survive a restart. Set SESSION_SECRET_KEY "
        "in your .env for a real deployment."
    )
    SESSION_SECRET_KEY = secrets.token_hex(32)

# https_only: the deployment guide exposes this dashboard on a public port,
# and without Secure the session cookie travels in clear over plain HTTP --
# a cookie that grants capital limits, the kill switch and pause/resume.
# Defaults to on; set SESSION_COOKIE_INSECURE=1 only for local http testing.
_COOKIE_INSECURE = os.environ.get("SESSION_COOKIE_INSECURE", "").strip() in ("1", "true", "yes")
if _COOKIE_INSECURE:
    logger.warning(
        "SESSION_COOKIE_INSECURE is set -- the session cookie will be sent over plain "
        "HTTP. Acceptable on localhost only; never set this on a deployed host."
    )
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    same_site="lax",
    https_only=not _COOKIE_INSECURE,
    max_age=int(os.environ.get("SESSION_MAX_AGE_SECONDS", "43200")),
)

# Single-operator allowlist: only this Gmail address may sign in. See
# authorized_users in schema.sql if this ever needs to grow into a real
# multi-user allowlist -- not wired up yet, on purpose, to keep this simple.
AUTHORIZED_GOOGLE_EMAIL = os.environ.get("AUTHORIZED_GOOGLE_EMAIL", "").strip().lower()


def _json_default(obj):
    """Fallback encoder for types json.dumps can't handle natively.

    Postgres NUMERIC columns come back from asyncpg as decimal.Decimal, which
    the stdlib json module refuses to serialize on its own.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

class WebSocketConnectionManager:
    """Orchestrates active client subscriber socket pools to broadcast system telemetry updates."""
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):

        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        payload = json.dumps(message, default=_json_default)
        for connection in self.active_connections:
            try:
                await connection.send_text(payload)
            except Exception:
                pass

ws_manager = WebSocketConnectionManager()


# --- Auth routes --------------------------------------------------------
def _login_page_html(error: bool = False) -> str:
    error_html = (
        "<p style='color:#fca5a5;font-size:0.75rem;margin:0.75rem 0 0'>"
        "That email isn't authorized for this dashboard.</p>"
        if error else ""
    )
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Sign in</title>
    </head>
    <body style="margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
                 background:#020617;color:#e2e8f0;font-family:system-ui,-apple-system,sans-serif;">
        <div style="background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:2.25rem;
                     width:100%;max-width:340px;box-sizing:border-box;">
            <h1 style="font-size:0.9rem;font-weight:700;margin:0 0 0.25rem;color:#fff;">
                10-Agent Production Network Matrix
            </h1>
            <p style="font-size:0.75rem;color:#94a3b8;margin:0 0 1.5rem;">
                Sign in with your Gmail account to continue.
            </p>
            <form method="post" action="/auth/login">
                <label style="display:block;font-size:0.65rem;font-weight:700;color:#94a3b8;
                               text-transform:uppercase;letter-spacing:0.05em;margin-bottom:0.35rem;">
                    Gmail Address
                </label>
                <input type="email" name="email" required autofocus placeholder="you@gmail.com"
                       style="display:block;width:100%;box-sizing:border-box;background:#020617;
                              border:1px solid #334155;border-radius:8px;padding:0.6rem 0.75rem;
                              color:#e2e8f0;font-size:0.85rem;">
                <button type="submit"
                        style="margin-top:1rem;width:100%;background:#6366f1;color:#fff;border:none;
                               border-radius:8px;padding:0.65rem;font-size:0.8rem;font-weight:700;
                               cursor:pointer;">
                    Sign In
                </button>
                {error_html}
            </form>
        </div>
    </body>
    </html>
    """


@app.get("/auth/login")
async def auth_login(request: Request):
    if not AUTHORIZED_GOOGLE_EMAIL:
        return HTMLResponse(
            "<body style='background:#020617;color:#e2e8f0;font-family:sans-serif;padding:3rem'>"
            "<h1>Sign-in is not configured</h1>"
            "<p>Set AUTHORIZED_GOOGLE_EMAIL in your .env file (see .env.example), "
            "then restart the container.</p>"
            "</body>",
            status_code=500,
        )
    return HTMLResponse(_login_page_html())


@app.post("/auth/login")
async def auth_login_submit(request: Request):
    if not AUTHORIZED_GOOGLE_EMAIL:
        return HTMLResponse(
            "<body style='background:#020617;color:#e2e8f0;font-family:sans-serif;padding:3rem'>"
            "<h1>Sign-in is not configured</h1>"
            "<p>Set AUTHORIZED_GOOGLE_EMAIL in your .env file (see .env.example), "
            "then restart the container.</p>"
            "</body>",
            status_code=500,
        )

    form = await request.form()
    email = str(form.get("email") or "").strip().lower()

    if not email or email != AUTHORIZED_GOOGLE_EMAIL:
        logger.warning(f"Rejected sign-in attempt from unauthorized account: {email!r}")
        return HTMLResponse(_login_page_html(error=True), status_code=403)

    request.session["user"] = {"email": email}
    return RedirectResponse(url="/", status_code=303)


@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/auth/login")


# --- Settings API ---------------------------------------------------------
# Uses one-off asyncpg connections rather than the pipeline worker's pool,
# since these are infrequent user-triggered edits and this keeps the
# settings endpoints independent of the background worker's connection
# lifecycle (which recycles its pool on error -- see pipeline_executor_worker).
NUMERIC_SETTINGS_FIELDS = {
    "max_total_capital_usd", "kill_switch_max_drawdown_pct",
    "kill_switch_max_loss_usd", "agent_timeout_seconds",
}
INT_SETTINGS_FIELDS = {"run_duration_minutes", "kill_switch_max_consecutive_losses"}


class SettingsUpdate(BaseModel):
    max_total_capital_usd: Optional[float] = None
    run_duration_minutes: Optional[int] = None
    capital_wallet_label: Optional[str] = None
    pnl_wallet_label: Optional[str] = None
    show_realtime_balances: Optional[bool] = None
    agent_unresponsive_action: Optional[str] = None
    agent_timeout_seconds: Optional[float] = None
    kill_switch_max_drawdown_pct: Optional[float] = None
    kill_switch_max_loss_usd: Optional[float] = None
    kill_switch_max_consecutive_losses: Optional[int] = None
    clear_max_total_capital_usd: bool = False
    clear_run_duration_minutes: bool = False
    clear_kill_switch_max_drawdown_pct: bool = False
    clear_kill_switch_max_loss_usd: bool = False
    clear_kill_switch_max_consecutive_losses: bool = False


def _serialize_settings_row(row: dict) -> dict:
    out = dict(row)
    for k, v in out.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
    return out


async def _fetch_settings_row() -> Optional[dict]:
    conn = await asyncpg.connect(dsn=DB_DSN)
    try:
        row = await conn.fetchrow("SELECT * FROM app_settings WHERE id = 1;")
        return dict(row) if row else None
    finally:
        await conn.close()


def _require_user(request: Request):
    """The signed-in operator, or None.

    Re-checks the session against the CURRENT allowlist on every request.
    Rotating AUTHORIZED_GOOGLE_EMAIL used to revoke nothing: the old address
    kept full access until its cookie happened to expire, because the
    allowlist was consulted only at sign-in. Only rotating SESSION_SECRET_KEY
    actually ejected anyone, and nothing said so.
    """
    user = request.session.get("user")
    if not user:
        return None
    email = (user.get("email") if isinstance(user, dict) else str(user)).strip().lower()
    if not AUTHORIZED_GOOGLE_EMAIL or email != AUTHORIZED_GOOGLE_EMAIL:
        request.session.clear()
        logger.warning("Session rejected: %r is no longer the authorised address.", email)
        return None
    return user


@app.get("/api/settings")
async def api_get_settings(request: Request):
    if not _require_user(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    row = await _fetch_settings_row()
    if not row:
        return JSONResponse({"error": "settings not found"}, status_code=404)
    return JSONResponse(_serialize_settings_row(row))


@app.post("/api/settings")
async def api_update_settings(request: Request, payload: SettingsUpdate):
    if not _require_user(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if payload.agent_unresponsive_action is not None and payload.agent_unresponsive_action not in ("RESTART_ALL", "SHUTDOWN"):
        return JSONResponse({"error": "agent_unresponsive_action must be RESTART_ALL or SHUTDOWN"}, status_code=400)

    data = payload.dict()
    fields = {}
    # explicit "clear_*" flags let a limit be reset to NULL (disabled) --
    # otherwise there would be no way to distinguish "leave unchanged" from
    # "set to zero/None" in a partial JSON update.
    clear_map = {
        "clear_max_total_capital_usd": "max_total_capital_usd",
        "clear_run_duration_minutes": "run_duration_minutes",
        "clear_kill_switch_max_drawdown_pct": "kill_switch_max_drawdown_pct",
        "clear_kill_switch_max_loss_usd": "kill_switch_max_loss_usd",
        "clear_kill_switch_max_consecutive_losses": "kill_switch_max_consecutive_losses",
    }
    cleared_fields = set()
    for clear_flag, target_field in clear_map.items():
        if data.get(clear_flag):
            fields[target_field] = None
            cleared_fields.add(target_field)

    settable_fields = [
        "max_total_capital_usd", "run_duration_minutes", "capital_wallet_label",
        "pnl_wallet_label", "show_realtime_balances", "agent_unresponsive_action",
        "agent_timeout_seconds", "kill_switch_max_drawdown_pct",
        "kill_switch_max_loss_usd", "kill_switch_max_consecutive_losses",
    ]
    for f in settable_fields:
        if f in cleared_fields:
            continue
        if data.get(f) is not None:
            fields[f] = data[f]

    if not fields:
        return JSONResponse({"error": "no fields to update"}, status_code=400)

    conn = await asyncpg.connect(dsn=DB_DSN)
    try:
        set_clauses = ", ".join(f"{k} = ${i + 1}" for i, k in enumerate(fields.keys()))
        values = list(fields.values())
        await conn.execute(
            f"UPDATE app_settings SET {set_clauses}, updated_at = CURRENT_TIMESTAMP WHERE id = 1;",
            *values,
        )
        row = await conn.fetchrow("SELECT * FROM app_settings WHERE id = 1;")
    finally:
        await conn.close()

    # engine.py caches settings briefly; force the pipeline worker to see
    # this change on its very next tick rather than up to ~3s later.
    await asyncio.get_running_loop().run_in_executor(None, lambda: get_app_settings(force_refresh=True))

    return JSONResponse(_serialize_settings_row(dict(row)))


@app.post("/api/settings/resume")
async def api_resume_trading(request: Request):
    if not _require_user(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    await asyncio.get_running_loop().run_in_executor(None, resume_trading)
    row = await _fetch_settings_row()
    return JSONResponse(_serialize_settings_row(row) if row else {"ok": True})


@app.post("/api/settings/pause")
async def api_pause_trading(request: Request):
    if not _require_user(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    await asyncio.get_running_loop().run_in_executor(None, lambda: pause_trading("Paused manually from the dashboard."))
    row = await _fetch_settings_row()
    return JSONResponse(_serialize_settings_row(row) if row else {"ok": True})


def _invalidation_proximity_percent(price, invalidation_level):
    """How far the price sits above its stop, as a percentage of price.

    Returns None when either figure is missing. That matters: the previous
    implementation returned random.uniform(10, 100) unconditionally, so the
    gauge always showed a comfortable-looking number regardless of whether
    a stop level existed at all. None renders as "n/a" -- an honest gap is
    more useful than a confident fabrication.
    """
    try:
        price = float(price)
        invalidation_level = float(invalidation_level)
    except (TypeError, ValueError):
        return None
    if price <= 0 or invalidation_level <= 0:
        return None
    return round(((price - invalidation_level) / price) * 100.0, 1)


def _record_paper_candidate(snapshot: dict, final_state: dict) -> None:
    """Sync wrapper: paper_trading uses psycopg2 like the rest of the
    engine, and this runs in the pipeline's worker thread. Best-effort --
    a failure to record the experiment must never take down a tick."""
    conn = None
    try:
        conn = psycopg2.connect(DB_DSN)
        with conn:
            paper_trading.record_candidate(conn, snapshot, final_state)
    except Exception as e:
        logger.warning(f"Could not record paper-trade candidate: {e}")
    finally:
        if conn is not None:
            conn.close()


def _mark_paper_trades() -> None:
    """Marks every live paper trade to its real current price."""
    conn = None
    try:
        conn = psycopg2.connect(DB_DSN)
        with conn:
            addresses = paper_trading.open_token_addresses(conn)
            if not addresses:
                return
            prices = market_data.fetch_current_prices_sync(addresses)
            stats = paper_trading.mark_to_market(conn, prices)
            # Independent of the barrier trades above: records what each token
            # actually did at fixed elapsed times. Must run even when
            # mark_to_market() changed nothing, because a token whose barrier
            # trade closed long ago still owes its later horizons.
            horizons = paper_trading.mark_horizons(conn, prices)
            if any(stats.values()):
                logger.info(f"Paper trades: {stats}")
            if horizons:
                logger.info(f"Horizon marks recorded: {horizons}")
    except Exception as e:
        logger.warning(f"Could not mark paper trades to market: {e}")
    finally:
        if conn is not None:
            conn.close()


async def record_holder_sample_and_get_velocity(conn, token_address: str,
                                                  total_holders: Optional[int]) -> Optional[float]:
    """Records this tick's holder count and returns holders-gained-per-hour.

    Holder velocity needs two observations, and a single API response only
    ever gives one -- so the history lives in token_holder_samples and this
    is where it accumulates. Persistence rather than an in-process cache
    is deliberate: a restart would otherwise silently reset every token's
    momentum to unknown, and the pipeline restarts on watchdog trips.

    Returns None when there isn't yet a second observation far enough
    apart to divide by, which E_BREADTH treats as "no data" rather than
    as zero growth. Best-effort: a failure here degrades the breadth gate,
    it must never take down a tick.
    """
    if total_holders is None or total_holders < 0:
        return None
    try:
        await conn.execute(
            "INSERT INTO token_holder_samples (token_address, total_holders) VALUES ($1, $2);",
            token_address, int(total_holders),
        )
        row = await conn.fetchrow("""
            SELECT total_holders,
                   EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - sampled_at)) / 3600.0 AS hours_ago
            FROM token_holder_samples
            WHERE token_address = $1
              AND sampled_at < CURRENT_TIMESTAMP - INTERVAL '10 minutes'
            ORDER BY sampled_at DESC
            LIMIT 1;
        """, token_address)
        if not row or not row["hours_ago"] or float(row["hours_ago"]) <= 0:
            return None
        delta = int(total_holders) - int(row["total_holders"])
        return round(delta / float(row["hours_ago"]), 2)
    except Exception as e:
        logger.warning(f"Holder-velocity sampling failed for {token_address}: {e}")
        return None


async def maybe_execute_via_signer(http_client: httpx.AsyncClient, final_state: dict) -> None:
    """Stage 3 (see STAGE3_SETUP.md): if I_ACCOUNTANT opened a position
    this tick AND ENABLE_STAGE3_EXECUTION is on, ask the separate signer
    service to act on it. This is strictly additive and best-effort --
    the position was already recorded in active_positions exactly the way
    every prior stage recorded it (this function runs after that, never
    instead of it), and nothing here can roll that back or block the next
    tick. A signer failure/refusal is logged and written to
    system_alerts, not raised -- Stage 3 must never turn an unreachable or
    disagreeing signer into a pipeline outage.
    """
    if not ENABLE_STAGE3_EXECUTION:
        return
    if not final_state.get("position_logged"):
        return  # I_ACCOUNTANT didn't open anything this tick -- nothing to execute

    token_address = final_state.get("token_address")
    token_symbol = final_state.get("token_symbol")
    requested_usd = final_state.get("max_safe_position_usd")
    if not token_address or requested_usd is None:
        logger.warning("Stage 3: position_logged was True but token_address/max_safe_position_usd missing from final_state -- skipping signer call.")
        return

    try:
        resp = await http_client.post(
            f"{SIGNER_SERVICE_URL}/execute",
            json={"token_address": token_address, "token_symbol": token_symbol, "requested_usd": float(requested_usd)},
            timeout=60.0,  # signing + broadcast + confirmation polling can take a while
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("executed"):
            msg = f"Stage 3: signer executed for ${token_symbol} -- tx {data.get('tx_signature')} on {data.get('network')}."
            logger.info(msg)
            await asyncio.get_running_loop().run_in_executor(None, lambda: write_system_alert("INFO", "SIGNER", msg))
        else:
            msg = f"Stage 3: signer did not execute for ${token_symbol}: {data.get('reason')}"
            logger.warning(msg)
            await asyncio.get_running_loop().run_in_executor(None, lambda: write_system_alert("WARN", "SIGNER", msg))
    except Exception as e:
        msg = f"Stage 3: signer service call failed for ${token_symbol}: {e}"
        logger.error(msg)
        await asyncio.get_running_loop().run_in_executor(None, lambda: write_system_alert("ERROR", "SIGNER", msg))


async def pipeline_executor_worker():
    """
    Continuous background loop that fetches real Solana market data for a
    token from the configured watchlist, executes the core agent network
    against it, and broadcasts metrics payloads to chart endpoints.
    """
    await asyncio.sleep(4.0)

    pool = None
    http_client = httpx.AsyncClient()
    warned_empty_watchlist = False
    while True:
        try:
            candidates = WATCHLIST_TOKEN_ADDRESSES
            if not candidates and ENABLE_TOKEN_DISCOVERY:
                candidates = await token_discovery.discover_candidates(http_client)
            if not candidates:
                if not warned_empty_watchlist:
                    logger.warning(
                        "No tokens to evaluate: WATCHLIST_TOKEN_ADDRESSES is empty and "
                        "token discovery is disabled or returned nothing. Set a watchlist "
                        "in .env, or set ENABLE_TOKEN_DISCOVERY=true. See README.md."
                    )
                    await asyncio.get_running_loop().run_in_executor(
                        None, lambda: write_system_alert(
                            "CRITICAL", "MARKET_DATA",
                            "No tokens to evaluate -- watchlist empty and discovery unavailable."
                        )
                    )
                    warned_empty_watchlist = True
                await asyncio.sleep(30.0)
                continue

            if pool is None:
                pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=2)

            loop = asyncio.get_running_loop()

            # Settle any open positions that have hit their take-profit or
            # stop-loss level *before* reading this tick's stats, so realized
            # P&L reflects the latest state rather than lagging a cycle.
            await loop.run_in_executor(None, evaluate_open_positions)

            # Advance every live paper trade against real prices. Same tick
            # as position settlement so both see the same market snapshot.
            await loop.run_in_executor(None, _mark_paper_trades)

            # Re-evaluate the run-duration timer and kill-switch thresholds.
            # This only ever flips run_status -- I_ACCOUNTANT is what actually
            # refuses new entries while paused; already-open positions keep
            # resolving normally above.
            # check_kill_switch() writes run_status itself when a threshold
            # trips, but it also has two paths -- settings unreadable, and the
            # row missing entirely -- where it can only REPORT a closed gate
            # and has nothing to write to. Its docstring says callers must
            # treat those as not-open; this caller used to discard the return
            # value entirely, so "I cannot tell whether trading is allowed"
            # silently meant "carry on".
            kill_switch_verdict = await loop.run_in_executor(None, check_kill_switch)
            settings_snapshot = await loop.run_in_executor(None, get_app_settings)

            gate_open = bool((kill_switch_verdict or {}).get("gate_open", False))
            gate_reason = (kill_switch_verdict or {}).get("reason") or "UNKNOWN"
            if not gate_open and gate_reason in ("SETTINGS_UNAVAILABLE", "UNKNOWN"):
                # Nothing was written to the database, so nothing else will
                # stop entries. Pause explicitly rather than trusting a
                # run_status we could not read.
                await loop.run_in_executor(
                    None, pause_trading,
                    f"Kill-switch state unreadable ({gate_reason}) -- pausing rather than "
                    f"trading on an unverified gate.")
                settings_snapshot = await loop.run_in_executor(None, get_app_settings)

            async with pool.acquire() as conn:
                totals = await conn.fetchrow("""
                    SELECT COUNT(*)::int as total,
                           COUNT(*) FILTER (WHERE session_status = 'APPROVED')::int as approved,
                           COUNT(*) FILTER (WHERE session_status = 'REJECTED')::int as rejected
                    FROM trading_sessions;
                """)
                funds = await conn.fetchval("SELECT COALESCE(SUM(allocated_usd), 0.0)::float FROM active_positions;")
                positions = await conn.fetch("SELECT token_symbol, allocated_usd::float AS allocated_usd, entry_trigger::float AS entry_trigger FROM active_positions ORDER BY id DESC LIMIT 4;")
                funnel_counts = await conn.fetch("""
                    SELECT final_briefing, COUNT(*)::int as count

                    FROM trading_sessions
                    WHERE session_status = 'REJECTED'
                    GROUP BY final_briefing;
                """)
                alert_rows = await conn.fetch("SELECT agent_name, message FROM system_alerts WHERE log_level IN ('WARN', 'ERROR', 'CRITICAL') ORDER BY id DESC LIMIT 3;")
                pnl_row = await conn.fetchrow("""
                    SELECT COUNT(*)::int AS closed_trades,
                           COALESCE(SUM(realized_pnl_usd), 0.0)::float AS total_realized_pnl,
                           COALESCE(SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END), 0)::int AS wins
                    FROM closed_positions;
                """)
                recent_closed = await conn.fetch("""
                    SELECT token_symbol, exit_reason,
                           realized_pnl_usd::float AS realized_pnl_usd,
                           realized_pnl_percent::float AS realized_pnl_percent,
                           closed_at
                    FROM closed_positions ORDER BY id DESC LIMIT 5;
                """)

            funnel_data = {"B_SENTINEL": 0, "E_BREADTH": 0, "F_ATLAS": 0, "G_ANCHOR": 0}
            for row in funnel_counts:
                brief = row["final_briefing"] or ""
                if "B_SENTINEL" in brief: funnel_data["B_SENTINEL"] += row["count"]
                # "E_SIGNAL" is the pre-rename name of this gate. Historical
                # trading_sessions rows still carry it in final_briefing, so
                # match both or every past rejection silently drops off the chart.
                elif "E_BREADTH" in brief or "E_SIGNAL" in brief: funnel_data["E_BREADTH"] += row["count"]
                elif "F_ATLAS" in brief: funnel_data["F_ATLAS"] += row["count"]
                elif "G_ANCHOR" in brief: funnel_data["G_ANCHOR"] += row["count"]

            target_address = random.choice(candidates)
            snapshot = await market_data.get_snapshot(http_client, target_address)
            if snapshot is None:
                # No indexed trading pair for this token right now (e.g. a
                # pre-graduation pump.fun token, or a bad address) -- skip
                # it this tick rather than inventing fake numbers.
                logger.warning(f"No market data available for {target_address} this tick; skipping.")
                await asyncio.sleep(3.0)
                continue

            target_token = snapshot["token_symbol"]

            # These two are POPPED into real state fields rather than logged
            # and discarded. The numeric columns they describe are NOT NULL in
            # trading_sessions, so an unmeasured value arrives as 0.0 -- the
            # most permissive input both F_ATLAS and G_ANCHOR could receive.
            # The booleans are the only way those gates can tell "measured
            # zero" from "never measured", and both now refuse on the latter.
            holder_missing = bool(snapshot.pop("_holder_data_missing", False))
            slippage_missing = bool(snapshot.pop("_slippage_data_missing", False))
            snapshot["holder_data_missing"] = holder_missing
            snapshot["slippage_data_missing"] = slippage_missing
            if holder_missing:
                logger.warning(
                    f"Holder-concentration data unavailable for ${target_token} -- "
                    f"F_ATLAS will refuse this token rather than read it as well distributed."
                )
            if slippage_missing:
                logger.warning(
                    f"Slippage/price-impact data unavailable for ${target_token} -- "
                    f"G_ANCHOR will refuse this token rather than assume zero execution cost."
                )
            if snapshot.pop("_social_data_missing", False):
                logger.warning(
                    f"Social/attention data unavailable for ${target_token} -- defaulted to 0. "
                    f"Nothing reads it today -- E_BREADTH replaced the gate that used to."
                )
            if snapshot.pop("_depth_data_missing", False):
                logger.warning(
                    f"No tradeable-depth figure for ${target_token} -- B_SENTINEL and G_ANCHOR "
                    f"will refuse this token rather than size a position against an unknown pool."
                )
            if snapshot.pop("_price_disagreement", False):
                logger.warning(
                    f"Independent price sources disagree on ${target_token} by more than the "
                    f"configured tolerance -- one may be stale or quoting a different pool."
                )

            # Holder velocity is the one breadth input that needs history
            # rather than a single API response, so it's computed here
            # where the DB pool lives, not in the provider (which stays
            # pure HTTP). Injected into inputs before the agents run.
            async with pool.acquire() as conn:
                inputs_holder_velocity = await record_holder_sample_and_get_velocity(
                    conn, target_address, snapshot.get("total_holders")
                )
            snapshot["holders_added_per_hour"] = inputs_holder_velocity

            inputs = snapshot

            try:
                final_state = await loop.run_in_executor(None, lambda: agent_network.invoke(inputs))
            except AgentUnresponsiveError as watchdog_err:
                action = (settings_snapshot or {}).get("agent_unresponsive_action") or "RESTART_ALL"
                logger.critical(f"Agent watchdog: {watchdog_err} Configured action: {action}.")

                if action == "SHUTDOWN":
                    await loop.run_in_executor(
                        None, lambda: set_run_status(
                            "SHUTDOWN_WATCHDOG", f"{watchdog_err.agent_name} was unresponsive; pipeline shut down per configured watchdog action."
                        )
                    )
                    await ws_manager.broadcast({
                        "timestamp": app_time.format_local(),  # operator's zone, not the container's UTC
                        "watchdog_shutdown": True,
                        "latest_log": f"[WATCHDOG] {watchdog_err.agent_name} unresponsive -- pipeline shut down. The dashboard stays up; restart the container to resume.",
                    })
                    logger.critical("Pipeline worker stopping (watchdog SHUTDOWN action). The web server and dashboard remain up.")
                    await http_client.aclose()
                    if pool is not None:
                        await pool.close()
                    return  # stop the loop entirely; FastAPI/uvicorn keep serving the dashboard

                # RESTART_ALL: drop the pool defensively (in case the hang was
                # DB-related) and retry fresh on the next tick.
                if pool is not None:
                    await pool.close()
                    pool = None
                await asyncio.sleep(3.0)
                continue

            log_msg = final_state.get("termination_reason") or final_state.get("final_briefing_compiled")
            agents = ["A_ORBIT", "B_SENTINEL", "C_VECTOR", "D_PULSE", "E_BREADTH", "F_ATLAS", "G_ANCHOR", "H_FUSE", "I_ACCOUNTANT", "Z_CLOSER"]

            # Stage 2: record this evaluation as paper trades -- BOTH entry
            # models, and regardless of whether the gates approved it. The
            # rejected rows are the control group; without them the approved
            # cohort's return has nothing to be compared against.
            await loop.run_in_executor(None, lambda: _record_paper_candidate(snapshot, final_state))

            # Stage 3 (see STAGE3_SETUP.md) -- strictly additive, see
            # maybe_execute_via_signer()'s own docstring for why this can
            # never block or roll back the tick above it.
            await maybe_execute_via_signer(http_client, final_state)

            closed_trades = pnl_row["closed_trades"] if pnl_row else 0
            wins = pnl_row["wins"] if pnl_row else 0

            run_duration_remaining_minutes = None
            if settings_snapshot and settings_snapshot.get("run_duration_minutes") and settings_snapshot.get("run_started_at"):
                elapsed_min = (datetime.now(timezone.utc) - settings_snapshot["run_started_at"]).total_seconds() / 60.0
                run_duration_remaining_minutes = max(0.0, round(float(settings_snapshot["run_duration_minutes"]) - elapsed_min, 1))

            broadcast_payload = {
                "timestamp": app_time.format_local(),  # operator's zone, not the container's UTC
                "summary": {
                    "total_sessions": (totals["total"] if totals else 0) + 1,
                    "approved_sessions": totals["approved"] if totals else 0,
                    "rejected_sessions": totals["rejected"] if totals else 0,
                    "total_capital": funds or 0.0
                },
                "pnl_summary": {
                    "total_realized_pnl": pnl_row["total_realized_pnl"] if pnl_row else 0.0,
                    "closed_trades": closed_trades,
                    "win_rate": round((wins / closed_trades) * 100, 1) if closed_trades else 0.0
                },
                # The clock above is in APP_TIMEZONE. Sending the label with it
                # so a bare "23:02:48" can't be mistaken for UTC again -- and
                # so it stays correct across the EDT/EST switch.
                "timezone_label": app_time.tz_label(),
                "latest_log": f"[{target_token}] {log_msg}",
                "funnel_rejections": funnel_data,
                # REAL per-agent execution times, measured by engine's watchdog
                # wrapper. This used to be random.uniform(10, 55) -- ten
                # invented numbers per broadcast on a chart labelled
                # "AGENT LATENCY DISTRIBUTION (MS)". An agent that hasn't run
                # this tick is omitted rather than given a plausible number.
                "latencies": {ag: get_agent_latencies().get(ag) for ag in agents},
                "divergence": {
                    # social_volume_score is hardcoded 0.0 -- no free source
                    # exists. Sent as null, not 0.0, so the chart can say
                    # "not measured" instead of drawing a flat line at zero
                    # that reads as "no hype detected".
                    "social_velocity": None,
                    "onchain_flow": inputs["onchain_flow_velocity"]
                },
                # REAL distance from the current price down to the stop, as a
                # percentage of price. Was random.uniform(10, 100).
                "invalidation_proximity": _invalidation_proximity_percent(
                    inputs.get("current_price"), final_state.get("invalidation_level_price")),

                "active_positions": [dict(r) for r in positions],
                "recent_closed_trades": [dict(r) for r in recent_closed],
                "alerts": [dict(a) for a in alert_rows],
                "run_state": {
                    # Defaulting to "RUNNING" painted a green, healthy dashboard
                    # over a pipeline whose state could not be read at all -- a
                    # tripped kill switch plus one transient database error was
                    # enough to show an operator that everything was fine.
                    # Unknown is now reported as unknown.
                    "run_status": (settings_snapshot or {}).get("run_status") or "UNKNOWN",
                    "run_status_known": settings_snapshot is not None,
                    "run_status_reason": (settings_snapshot or {}).get("run_status_reason"),
                    "run_duration_minutes": (settings_snapshot or {}).get("run_duration_minutes"),
                    "run_duration_remaining_minutes": run_duration_remaining_minutes,
                    "max_total_capital_usd": (settings_snapshot or {}).get("max_total_capital_usd"),
                    "capital_wallet_label": (settings_snapshot or {}).get("capital_wallet_label"),
                    "pnl_wallet_label": (settings_snapshot or {}).get("pnl_wallet_label"),
                    "show_realtime_balances": (settings_snapshot or {}).get("show_realtime_balances", True),
                }
            }

            await ws_manager.broadcast(broadcast_payload)
        except Exception as e:
            logger.error(f"Error handling network execution loops: {e}")
            if pool is not None:
                await pool.close()
                pool = None

        await asyncio.sleep(3.0)

@app.on_event("startup")
def start_pipeline_loops():
    asyncio.create_task(pipeline_executor_worker())

@app.websocket("/ws/metrics")
async def websocket_route(websocket: WebSocket):
    if not websocket.session.get("user"):
        await websocket.close(code=1008)  # policy violation: not authenticated
        return
    await ws_manager.connect(websocket)
    try:
        while True:

            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)

@app.get("/", response_class=HTMLResponse)
async def get_dashboard_interface(request: Request):
    user = request.session.get("user")
    if not user:
        return RedirectResponse(url="/auth/login")
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Production Network Control Panel</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script>
        <style>
            @keyframes pulse-glow { 0%, 100% { transform: scale(1); opacity: 1; } 50% { transform: scale(1.05); opacity: 0.7; } }
            .active-glow { animation: pulse-glow 2s infinite ease-in-out; }
            .chart-frame { position: relative; width: 100%; height: 180px; }
            body.balance-hidden .balance-value { filter: blur(6px); user-select: none; }
            .modal-overlay { background: rgba(2, 6, 23, 0.8); }
        </style>
    </head>
    <body class="bg-slate-950 text-slate-100 font-sans min-h-screen">
        <header class="border-b border-slate-800 bg-slate-900/40 backdrop-blur px-6 py-4 sticky top-0 z-50">
            <div class="max-w-7xl mx-auto flex justify-between items-center gap-4">
                <div>
                    <h1 class="text-sm font-bold text-white flex items-center gap-2">
                        <span class="h-2 w-2 rounded-full bg-emerald-400 active-glow"></span>
                        10-Agent Production Network Matrix
                    </h1>
                    <div class="flex items-center gap-2 mt-1">
                        <p id="run-status-badge" class="text-[10px] font-bold text-emerald-400">RUNNING</p>
                        <button id="pause-btn" class="px-2 py-0.5 rounded text-[9px] font-bold bg-rose-500/10 text-rose-400 border border-rose-500/30 hover:bg-rose-500/20">PAUSE TRADING</button>
                        <button id="resume-btn" class="hidden px-2 py-0.5 rounded text-[9px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 hover:bg-emerald-500/20">RESUME TRADING</button>
                        <span id="tz-label" class="text-[9px] font-bold text-slate-500 uppercase tracking-wider" title="All times on this dashboard are shown in this zone. Stored timestamps remain UTC."></span>
                    </div>
                </div>
                <div class="flex items-center gap-3">
                    <button id="balance-toggle-btn" class="px-2.5 py-1 rounded-lg text-[10px] font-bold bg-slate-800 text-slate-300 border border-slate-700 hover:bg-slate-700">HIDE BALANCES</button>
                    <button id="settings-btn" class="px-2.5 py-1 rounded-lg text-[10px] font-bold bg-slate-800 text-slate-300 border border-slate-700 hover:bg-slate-700">SETTINGS</button>
                    <div id="ws-badge" class="px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-rose-500/10 text-rose-400 border border-rose-500/20">
                        DISCONNECTED
                    </div>
                    <div class="flex items-center gap-2 pl-3 border-l border-slate-800">
                        <span class="text-[10px] text-slate-400">__USER_EMAIL__</span>
                        <a href="/auth/logout" class="text-[10px] font-bold text-rose-400 hover:text-rose-300">SIGN OUT</a>
                    </div>
                </div>
            </div>
        </header>

        <main class="max-w-7xl mx-auto p-6 space-y-6">
            <section class="grid grid-cols-2 lg:grid-cols-3 gap-4">

                <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                    <p class="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Total Pipelines</p>
                    <p id="stat-total" class="text-xl font-bold mt-1">--</p>
                </div>
                <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                    <p class="text-[10px] font-bold text-emerald-400 uppercase tracking-wider">Approved Transits</p>
                    <p id="stat-approved" class="text-xl font-bold text-emerald-400 mt-1">--</p>
                </div>
                <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                    <p class="text-[10px] font-bold text-rose-400 uppercase tracking-wider">Gating Rejections</p>
                    <p id="stat-rejected" class="text-xl font-bold text-rose-400 mt-1">--</p>
                </div>
                <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                    <p class="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Capital Deployed</p>
                    <p id="stat-capital" class="text-xl font-bold text-slate-300 mt-1 balance-value">$--</p>
                    <p class="text-[9px] text-slate-600 mt-0.5">Open exposure, not P&amp;L</p>
                </div>
                <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                    <p class="text-[10px] font-bold text-indigo-400 uppercase tracking-wider">Net Realized P&amp;L</p>
                    <p id="stat-pnl" class="text-xl font-bold text-slate-300 mt-1 balance-value">$--</p>
                    <p id="stat-pnl-trades" class="text-[9px] text-slate-600 mt-0.5">0 closed trades</p>
                </div>
                <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                    <p class="text-[10px] font-bold text-amber-400 uppercase tracking-wider">Win Rate</p>
                    <p id="stat-winrate" class="text-xl font-bold text-amber-400 mt-1">--</p>
                </div>
            </section>

            <section class="grid grid-cols-1 lg:grid-cols-2 gap-6">

                <div class="bg-slate-900 border border-slate-800 p-5 rounded-xl flex flex-col">
                    <h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">Parameter Rejection Funnel</h3>
                    <div class="chart-frame flex-1"><canvas id="chart-funnel"></canvas></div>
                </div>

                <div class="bg-slate-900 border border-slate-800 p-5 rounded-xl flex flex-col">
                    <h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">Agent Latency, Last Tick (ms)</h3>
                    <div class="chart-frame flex-1"><canvas id="chart-latency"></canvas></div>
                </div>

                <div class="bg-slate-900 border border-slate-800 p-5 rounded-xl flex flex-col">
                    <h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">Hype Velocity vs Onchain Inflows</h3>
                    <div class="chart-frame flex-1"><canvas id="chart-divergence"></canvas></div>
                </div>

                <div class="bg-slate-900 border border-slate-800 p-5 rounded-xl flex flex-col">
                    <h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">Accounting Ledger Holdings</h3>
                    <div id="positions-box" class="flex-1 space-y-2 text-xs overflow-y-auto pt-1">
                        <p class="text-slate-500 italic">Syncing asset data tables...</p>

                    </div>
                </div>

                <div class="lg:col-span-2 bg-slate-900 border border-slate-800 p-5 rounded-xl flex flex-col">
                    <h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">Realized Trade History</h3>
                    <div id="closed-trades-box" class="flex-1 space-y-2 text-xs overflow-y-auto pt-1">
                        <p class="text-slate-500 italic">No closed trades yet.</p>
                    </div>
                </div>
            </section>

            <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                <section class="lg:col-span-2 bg-slate-900 border border-slate-800 p-5 rounded-xl">
                    <h3 class="text-xs font-bold text-slate-300 uppercase mb-2">Telemetry Trace Console</h3>
                    <div id="console-log" class="bg-slate-950 p-4 font-mono text-[11px] h-28 overflow-y-auto space-y-1 rounded-lg border border-slate-800">
                        <div class="text-slate-500">// Processing metrics feeds...</div>
                    </div>
                </section>
                
                <section class="bg-slate-900 border border-slate-800 p-5 rounded-xl">
                    <h3 class="text-xs font-bold text-rose-400 uppercase mb-2">Framework Alerts Outbox Buffer</h3>
                    <div id="alerts-box" class="space-y-1.5 h-28 overflow-y-auto text-[10px] font-mono">
                        <p class="text-slate-500 italic">No warnings active.</p>
                    </div>
                </section>
            </div>

        </main>

        <div id="settings-modal" class="hidden fixed inset-0 z-[100] modal-overlay flex items-center justify-center p-4">
            <div class="bg-slate-900 border border-slate-800 rounded-xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
                <div class="flex justify-between items-center p-5 border-b border-slate-800">
                    <h2 class="text-sm font-bold text-white uppercase tracking-wider">Run Configuration</h2>
                    <button id="settings-close-btn" class="text-slate-400 hover:text-white text-lg leading-none">&times;</button>
                </div>
                <div class="p-5 space-y-5 text-xs">

                    <div>
                        <label class="block text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1">Capital Allocation Limit</label>
                        <div class="flex items-center gap-2">
                            <span class="text-slate-500">$</span>
                            <input id="set-max-capital" type="number" min="0" step="1" class="flex-1 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-slate-100" placeholder="No limit">
                            <label class="flex items-center gap-1 text-slate-400 whitespace-nowrap"><input id="set-max-capital-nolimit" type="checkbox" class="accent-indigo-500"> No limit</label>
                        </div>
                        <p class="text-[9px] text-slate-600 mt-1">Blocks new entries once total deployed capital would exceed this.</p>
                    </div>

                    <div>
                        <label class="block text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1">Run Duration</label>
                        <div class="flex items-center gap-2">
                            <input id="set-run-duration" type="number" min="0" step="1" class="flex-1 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-slate-100" placeholder="Indefinite">
                            <select id="set-run-duration-unit" class="bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-slate-100">
                                <option value="minutes">Minutes</option>
                                <option value="hours">Hours</option>
                            </select>
                            <label class="flex items-center gap-1 text-slate-400 whitespace-nowrap"><input id="set-run-duration-nolimit" type="checkbox" class="accent-indigo-500"> Indefinite</label>
                        </div>
                        <p class="text-[9px] text-slate-600 mt-1">Pauses new entries once elapsed. Hit Resume Trading to restart the timer.</p>
                    </div>

                    <div>
                        <label class="block text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1">Capital Source Wallet</label>
                        <input id="set-capital-wallet" type="text" class="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-slate-100" placeholder="0x... (label only)">
                    </div>

                    <div>
                        <label class="block text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1">P&amp;L Destination Wallet</label>
                        <input id="set-pnl-wallet" type="text" class="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-slate-100" placeholder="0x... (label only)">
                        <p class="text-[9px] text-slate-600 mt-1">Both wallet fields are metadata for your own records only — this project has no wallet connection or transaction signing.</p>
                    </div>

                    <div class="flex items-center justify-between">
                        <label class="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Show Real-Time Balances by Default</label>
                        <input id="set-show-balances" type="checkbox" class="accent-indigo-500 h-4 w-4">
                    </div>

                    <div>
                        <label class="block text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1">If an Agent Is Unresponsive to the Others</label>
                        <select id="set-watchdog-action" class="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-slate-100">
                            <option value="RESTART_ALL">Restart all agents</option>
                            <option value="SHUTDOWN">Shut down the pipeline</option>
                        </select>
                        <div class="flex items-center gap-2 mt-2">
                            <span class="text-slate-500">Timeout</span>
                            <input id="set-agent-timeout" type="number" min="1" step="1" class="w-20 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-slate-100">
                            <span class="text-slate-500">seconds before an agent is flagged unresponsive</span>
                        </div>
                    </div>

                    <div class="border-t border-slate-800 pt-4">
                        <label class="block text-[10px] font-bold text-rose-400 uppercase tracking-wider mb-2">Kill-Switch Conditions</label>
                        <p class="text-[9px] text-slate-600 mb-3">Any condition met pauses new entries; already-open positions still resolve normally.</p>

                        <div class="mb-3">
                            <label class="block text-[10px] text-slate-400 mb-1">Max Drawdown (% of capital limit)</label>
                            <div class="flex items-center gap-2">
                                <input id="set-kill-drawdown" type="number" min="0" step="1" class="flex-1 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-slate-100" placeholder="Disabled">
                                <label class="flex items-center gap-1 text-slate-400 whitespace-nowrap"><input id="set-kill-drawdown-nolimit" type="checkbox" class="accent-indigo-500"> Off</label>
                            </div>
                        </div>
                        <div class="mb-3">
                            <label class="block text-[10px] text-slate-400 mb-1">Max Total Realized Loss ($)</label>
                            <div class="flex items-center gap-2">
                                <input id="set-kill-loss" type="number" min="0" step="1" class="flex-1 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-slate-100" placeholder="Disabled">
                                <label class="flex items-center gap-1 text-slate-400 whitespace-nowrap"><input id="set-kill-loss-nolimit" type="checkbox" class="accent-indigo-500"> Off</label>
                            </div>
                        </div>
                        <div>
                            <label class="block text-[10px] text-slate-400 mb-1">Max Consecutive Losing Trades</label>
                            <div class="flex items-center gap-2">
                                <input id="set-kill-streak" type="number" min="1" step="1" class="flex-1 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-slate-100" placeholder="Disabled">
                                <label class="flex items-center gap-1 text-slate-400 whitespace-nowrap"><input id="set-kill-streak-nolimit" type="checkbox" class="accent-indigo-500"> Off</label>
                            </div>
                        </div>
                    </div>

                    <div id="settings-error" class="hidden text-rose-400 text-[10px]"></div>
                </div>
                <div class="flex justify-end gap-2 p-5 border-t border-slate-800">
                    <button id="settings-cancel-btn" class="px-3 py-1.5 rounded-lg text-[10px] font-bold bg-slate-800 text-slate-300 hover:bg-slate-700">CANCEL</button>
                    <button id="settings-save-btn" class="px-3 py-1.5 rounded-lg text-[10px] font-bold bg-indigo-500 text-white hover:bg-indigo-400">SAVE</button>
                </div>
            </div>
        </div>

        <script>
            // SECURITY: every string that arrives in a broadcast payload has to be
            // treated as attacker-controlled before it goes anywhere near innerHTML.
            // A token's symbol is chosen by whoever minted that token on-chain, we
            // read it straight off a public indexer, and it then flows into log
            // lines, alert messages, position rows and closed-trade rows. Somebody
            // can (and eventually will) mint a token whose "symbol" is an <img
            // onerror=...> payload, and this dashboard is an authenticated session
            // that can move capital limits and pause/resume trading -- so script
            // execution here is a real compromise, not a cosmetic bug.
            //
            // esc() is applied at EVERY interpolation of server-supplied data below.
            // The server sanitizes this data too (see sanitize_external_text() in
            // market_data.py), but that is defense in depth, not a substitute:
            // escape here regardless of what you think the server already stripped.
            // innerText/textContent/.value/.title assignments do not parse HTML and
            // are safe as-is -- this is only needed for innerHTML.
            const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
            const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ESC_MAP[c]);

            let cFunnel, cLatency, cDivergence;
            let lastTimezoneLabel = '';
            const labelsBuffer = [];
            const streamSocial = [];
            const streamFlow = [];
            let currentSettings = null;
            let balanceVisibilityTouched = false;

            function applyBalanceVisibility(visible) {
                document.body.classList.toggle('balance-hidden', !visible);
                document.getElementById('balance-toggle-btn').innerText = visible ? 'HIDE BALANCES' : 'SHOW BALANCES';
            }

            function updateRunStatusBadge(runState) {
                if (!runState) return;
                const badge = document.getElementById('run-status-badge');
                const resumeBtn = document.getElementById('resume-btn');
                const pauseBtn = document.getElementById('pause-btn');
                const status = runState.run_status || 'RUNNING';
                const labels = {
                    RUNNING: 'RUNNING',
                    PAUSED_MANUAL: 'PAUSED — MANUAL',
                    PAUSED_KILL_SWITCH: 'PAUSED — KILL SWITCH',
                    PAUSED_DURATION_ELAPSED: 'PAUSED — DURATION ELAPSED',
                    SHUTDOWN_WATCHDOG: 'SHUT DOWN — AGENT UNRESPONSIVE',
                };
                badge.innerText = labels[status] || status;
                badge.className = status === 'RUNNING' ? 'text-[10px] font-bold text-emerald-400' : 'text-[10px] font-bold text-rose-400';
                badge.title = runState.run_status_reason || '';
                // Name the zone the clock is in. A bare "23:02:48" next to a
                // machine clock reading the same is fine; next to one five
                // hours off it is a bug report waiting to happen.
                const tzEl = document.getElementById('tz-label');
                if (tzEl && lastTimezoneLabel) tzEl.innerText = lastTimezoneLabel;
                resumeBtn.classList.toggle('hidden', status === 'RUNNING');
                pauseBtn.classList.toggle('hidden', status !== 'RUNNING');
            }

            async function fetchSettings() {
                const res = await fetch('/api/settings');
                if (!res.ok) return null;
                currentSettings = await res.json();
                return currentSettings;
            }

            function populateSettingsForm(s) {
                document.getElementById('set-max-capital').value = s.max_total_capital_usd ?? '';
                document.getElementById('set-max-capital-nolimit').checked = s.max_total_capital_usd == null;

                const durationMinutes = s.run_duration_minutes;
                if (durationMinutes && durationMinutes % 60 === 0) {
                    document.getElementById('set-run-duration').value = durationMinutes / 60;
                    document.getElementById('set-run-duration-unit').value = 'hours';
                } else {
                    document.getElementById('set-run-duration').value = durationMinutes ?? '';
                    document.getElementById('set-run-duration-unit').value = 'minutes';
                }
                document.getElementById('set-run-duration-nolimit').checked = durationMinutes == null;

                document.getElementById('set-capital-wallet').value = s.capital_wallet_label || '';
                document.getElementById('set-pnl-wallet').value = s.pnl_wallet_label || '';
                document.getElementById('set-show-balances').checked = !!s.show_realtime_balances;
                document.getElementById('set-watchdog-action').value = s.agent_unresponsive_action || 'RESTART_ALL';
                document.getElementById('set-agent-timeout').value = s.agent_timeout_seconds ?? 15;
                document.getElementById('set-kill-drawdown').value = s.kill_switch_max_drawdown_pct ?? '';
                document.getElementById('set-kill-drawdown-nolimit').checked = s.kill_switch_max_drawdown_pct == null;
                document.getElementById('set-kill-loss').value = s.kill_switch_max_loss_usd ?? '';
                document.getElementById('set-kill-loss-nolimit').checked = s.kill_switch_max_loss_usd == null;
                document.getElementById('set-kill-streak').value = s.kill_switch_max_consecutive_losses ?? '';
                document.getElementById('set-kill-streak-nolimit').checked = s.kill_switch_max_consecutive_losses == null;
            }

            function openSettingsModal() {
                document.getElementById('settings-error').classList.add('hidden');
                fetchSettings().then(s => { if (s) populateSettingsForm(s); });
                document.getElementById('settings-modal').classList.remove('hidden');
            }

            function closeSettingsModal() {
                document.getElementById('settings-modal').classList.add('hidden');
            }

            async function saveSettings() {
                const errEl = document.getElementById('settings-error');
                errEl.classList.add('hidden');

                const durationVal = document.getElementById('set-run-duration').value;
                const durationUnit = document.getElementById('set-run-duration-unit').value;
                const durationMinutes = durationVal === '' ? null : (durationUnit === 'hours' ? Math.round(parseFloat(durationVal) * 60) : Math.round(parseFloat(durationVal)));
                const noDuration = document.getElementById('set-run-duration-nolimit').checked;
                const noCapital = document.getElementById('set-max-capital-nolimit').checked;
                const noDrawdown = document.getElementById('set-kill-drawdown-nolimit').checked;
                const noLoss = document.getElementById('set-kill-loss-nolimit').checked;
                const noStreak = document.getElementById('set-kill-streak-nolimit').checked;

                const payload = {
                    max_total_capital_usd: noCapital ? null : parseFloat(document.getElementById('set-max-capital').value || '0'),
                    clear_max_total_capital_usd: noCapital,
                    run_duration_minutes: noDuration ? null : durationMinutes,
                    clear_run_duration_minutes: noDuration,
                    capital_wallet_label: document.getElementById('set-capital-wallet').value || null,
                    pnl_wallet_label: document.getElementById('set-pnl-wallet').value || null,
                    show_realtime_balances: document.getElementById('set-show-balances').checked,
                    agent_unresponsive_action: document.getElementById('set-watchdog-action').value,
                    agent_timeout_seconds: parseFloat(document.getElementById('set-agent-timeout').value || '15'),
                    kill_switch_max_drawdown_pct: noDrawdown ? null : parseFloat(document.getElementById('set-kill-drawdown').value || '0'),
                    clear_kill_switch_max_drawdown_pct: noDrawdown,
                    kill_switch_max_loss_usd: noLoss ? null : parseFloat(document.getElementById('set-kill-loss').value || '0'),
                    clear_kill_switch_max_loss_usd: noLoss,
                    kill_switch_max_consecutive_losses: noStreak ? null : parseInt(document.getElementById('set-kill-streak').value || '0', 10),
                    clear_kill_switch_max_consecutive_losses: noStreak,
                };

                try {
                    const res = await fetch('/api/settings', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload),
                    });
                    const body = await res.json();
                    if (!res.ok) {
                        errEl.innerText = body.error || 'Failed to save settings.';
                        errEl.classList.remove('hidden');
                        return;
                    }
                    currentSettings = body;
                    balanceVisibilityTouched = true;
                    applyBalanceVisibility(!!body.show_realtime_balances);
                    closeSettingsModal();
                } catch (e) {
                    errEl.innerText = 'Failed to save settings: ' + e;
                    errEl.classList.remove('hidden');
                }
            }

            async function resumeTrading() {
                try {
                    await fetch('/api/settings/resume', { method: 'POST' });
                } catch (e) { /* the next broadcast tick's run_state will reflect whatever happened */ }
            }

            async function pauseTrading() {
                try {
                    await fetch('/api/settings/pause', { method: 'POST' });
                } catch (e) { /* the next broadcast tick's run_state will reflect whatever happened */ }
            }

            function wireSettingsControls() {
                document.getElementById('settings-btn').addEventListener('click', openSettingsModal);
                document.getElementById('settings-close-btn').addEventListener('click', closeSettingsModal);
                document.getElementById('settings-cancel-btn').addEventListener('click', closeSettingsModal);
                document.getElementById('settings-save-btn').addEventListener('click', saveSettings);
                document.getElementById('resume-btn').addEventListener('click', resumeTrading);
                document.getElementById('pause-btn').addEventListener('click', pauseTrading);
                document.getElementById('balance-toggle-btn').addEventListener('click', () => {
                    const makeVisible = document.body.classList.contains('balance-hidden');
                    balanceVisibilityTouched = true;
                    applyBalanceVisibility(makeVisible);
                    fetch('/api/settings', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ show_realtime_balances: makeVisible }),
                    }).catch(() => {});
                });
            }

            function initCharts() {
                cFunnel = new Chart(document.getElementById('chart-funnel').getContext('2d'), {
                    type: 'bar',
                    data: {
                        labels: ['B_SENTINEL (Depth)', 'E_BREADTH (Breadth vs Depth)', 'F_ATLAS (Concentration)', 'G_ANCHOR (Slippage)'],
                        datasets: [{ data: [0, 0, 0, 0], backgroundColor: ['#f43f5e', '#f59e0b', '#ec4899', '#6366f1'] }]
                    },
                    options: { indexAxis: 'y', responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } }
                });

                cLatency = new Chart(document.getElementById('chart-latency').getContext('2d'), {

                    type: 'bar',
                    data: {
                        labels: ['ORB', 'SNT', 'VEC', 'PLS', 'SIG', 'ATL', 'ANC', 'FUS', 'ACC', 'CLS'],
                        datasets: [{ data: Array(10).fill(0), backgroundColor: '#10b981' }]
                    },
                    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } }
                });

                cDivergence = new Chart(document.getElementById('chart-divergence').getContext('2d'), {
                    type: 'line',
                    data: {
                        labels: labelsBuffer,
                        datasets: [
                            // Nulls, not zeros. social_volume_score has no free
                            // data source, and a flat orange line at zero reads
                            // as "no hype detected" rather than "never measured".
                            // spanGaps:false makes Chart.js draw nothing instead.
                            { label: 'Social Momentum (not measured)', data: streamSocial,
                              borderColor: '#f59e0b', borderDash: [4, 4], tension: 0.15, spanGaps: false },
                            { label: 'Onchain Flow', data: streamFlow, borderColor: '#06b6d4', tension: 0.15 }
                        ]
                    },
                    options: { responsive: true, maintainAspectRatio: false }
                });

            }

            function initWS() {
                const badge = document.getElementById('ws-badge');
                const consoleLog = document.getElementById('console-log');
                const alertsBox = document.getElementById('alerts-box');
                const positionsBox = document.getElementById('positions-box');
                const closedTradesBox = document.getElementById('closed-trades-box');

                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                const socket = new WebSocket(`${protocol}//${window.location.host}/ws/metrics`);

                socket.onopen = () => {
                    badge.className = "px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20";
                    badge.innerText = "CHANNELS OPENED";
                };

                socket.onmessage = (event) => {
                    const data = JSON.parse(event.data);

                    if (data.watchdog_shutdown) {
                        updateRunStatusBadge({ run_status: 'SHUTDOWN_WATCHDOG', run_status_reason: data.latest_log });
                        if (data.latest_log) {
                            const line = document.createElement('div');
                            line.className = "text-rose-400 leading-normal font-bold";
                            line.innerHTML = `<span class="text-slate-600">[${esc(data.timestamp)}]</span> ` + esc(data.latest_log);
                            consoleLog.appendChild(line);
                            consoleLog.scrollTop = consoleLog.scrollHeight;
                        }
                        return;
                    }

                    document.getElementById('stat-total').innerText = data.summary.total_sessions;
                    document.getElementById('stat-approved').innerText = data.summary.approved_sessions;
                    document.getElementById('stat-rejected').innerText = data.summary.rejected_sessions;
                    document.getElementById('stat-capital').innerText = `$` + data.summary.total_capital.toLocaleString();

                    const pnl = data.pnl_summary || { total_realized_pnl: 0, closed_trades: 0, win_rate: 0 };
                    const pnlEl = document.getElementById('stat-pnl');
                    const pnlPositive = pnl.total_realized_pnl >= 0;
                    pnlEl.innerText = (pnlPositive ? '+$' : '-$') + Math.abs(pnl.total_realized_pnl).toLocaleString(undefined, { maximumFractionDigits: 2 });
                    pnlEl.className = "text-xl font-bold mt-1 balance-value " + (pnlPositive ? "text-emerald-400" : "text-rose-400");
                    document.getElementById('stat-pnl-trades').innerText = `${pnl.closed_trades} closed trade${pnl.closed_trades === 1 ? '' : 's'}`;
                    document.getElementById('stat-winrate').innerText = pnl.closed_trades > 0 ? `${pnl.win_rate.toFixed(1)}%` : '--';

                    cFunnel.data.datasets[0].data = [
                        data.funnel_rejections.B_SENTINEL,
                        data.funnel_rejections.E_BREADTH,
                        data.funnel_rejections.F_ATLAS,
                        data.funnel_rejections.G_ANCHOR
                    ];
                    cFunnel.update('none');

                    // Nulls stay null: an agent that did not run this tick draws
                    // no bar, rather than a zero-height one that reads as "instant".
                    cLatency.data.datasets[0].data = Object.values(data.latencies)
                        .map(v => (v === null || v === undefined) ? null : v);
                    cLatency.update('none');

                    if (labelsBuffer.length >= 12) {
                        labelsBuffer.shift(); streamSocial.shift(); streamFlow.shift();
                    }

                    if (data.timezone_label) lastTimezoneLabel = data.timezone_label;
                    labelsBuffer.push(data.timestamp);
                    streamSocial.push(data.divergence.social_velocity);
                    streamFlow.push(data.divergence.onchain_flow);
                    cDivergence.update('none');

                    if (data.active_positions.length === 0) {
                        positionsBox.innerHTML = '<p class="text-slate-500 italic">No asset exposure logged currently...</p>';
                    } else {
                        positionsBox.innerHTML = data.active_positions.map(p => `
                            <div class="flex justify-between p-2 bg-slate-950 border border-slate-800 rounded">
                                <div><span class="font-bold text-white">$${esc(p.token_symbol)}</span><p class="text-[9px] text-slate-500">Trigger Floor: ${esc(p.entry_trigger)}</p></div>
                                <div class="text-right text-indigo-400 font-bold balance-value">$${esc(p.allocated_usd)}</div>
                            </div>
                        `).join('');
                    }

                    const closedTrades = data.recent_closed_trades || [];
                    if (closedTrades.length === 0) {
                        closedTradesBox.innerHTML = '<p class="text-slate-500 italic">No closed trades yet.</p>';
                    } else {
                        closedTradesBox.innerHTML = closedTrades.map(t => {
                            const positive = t.realized_pnl_usd >= 0;
                            const color = positive ? 'text-emerald-400' : 'text-rose-400';
                            const sign = positive ? '+' : '';
                            const reasonColor = t.exit_reason === 'TARGET_HIT' ? 'text-emerald-500' : 'text-rose-500';
                            return `
                                <div class="flex justify-between p-2 bg-slate-950 border border-slate-800 rounded">
                                    <div><span class="font-bold text-white">$${esc(t.token_symbol)}</span><p class="text-[9px] ${reasonColor}">${esc(t.exit_reason)}</p></div>
                                    <div class="text-right ${color} font-bold balance-value">${sign}$${t.realized_pnl_usd.toFixed(2)}<p class="text-[9px] font-normal ${color}">${sign}${t.realized_pnl_percent.toFixed(1)}%</p></div>
                                </div>
                            `;
                        }).join('');
                    }

                    if (data.alerts.length === 0) {
                        alertsBox.innerHTML = '<p class="text-slate-500 italic">No warnings active.</p>';
                    } else {

                        alertsBox.innerHTML = data.alerts.map(a => `
                            <div class="p-1.5 rounded bg-amber-500/10 border border-amber-500/20 text-amber-300">
                                <span class="font-bold text-rose-400">[${esc(a.agent_name)}]</span> ${esc(a.message)}
                            </div>
                        `).join('');
                    }

                    if (data.latest_log) {
                        const line = document.createElement('div');
                        line.className = "text-slate-300 leading-normal";
                        line.innerHTML = `<span class="text-slate-600">[${esc(data.timestamp)}]</span> ` + esc(data.latest_log);
                        consoleLog.appendChild(line);
                        consoleLog.scrollTop = consoleLog.scrollHeight;
                    }

                    updateRunStatusBadge(data.run_state);
                    if (!balanceVisibilityTouched && data.run_state) {
                        // Before the user has manually toggled it or saved Settings,
                        // keep following the server's configured default.
                        applyBalanceVisibility(!!data.run_state.show_realtime_balances);
                    }
                };

                socket.onclose = () => {
                    badge.className = "px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-rose-500/10 text-rose-400 border border-rose-500/20";
                    badge.innerText = "DISCONNECTED";

                };
            }

            window.onload = () => { initCharts(); initWS(); wireSettingsControls(); };
        </script>
    </body>
    </html>
    """
    html_content = html_content.replace("__USER_EMAIL__", user.get("email", ""))
    return HTMLResponse(content=html_content)
