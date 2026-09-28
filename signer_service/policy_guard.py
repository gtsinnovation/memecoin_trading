# policy_guard.py
"""Independent re-validation, layer 2 of this app's defense-in-depth
around real signing (see README.md's DEX/wallet architecture section):

  Layer 1: the main pipeline's own I_ACCOUNTANT gate (app_settings /
           run_status, checked in engine.py) -- decides whether to ASK
           the signer service to execute anything at all.
  Layer 2: THIS FILE -- re-derives the same checks independently, from
           its own database read, inside the signer service. It does not
           trust anything the caller (the main pipeline) claims about
           run_status, the token, or the amount -- if the pipeline were
           compromised, buggy, or simply lying, this layer still refuses.
  Layer 3: Turnkey's own policy engine, enforced inside its enclave
           (program allowlist / token-mint allowlist / destination
           allowlist / amount caps -- configured directly in Turnkey, not
           by this codebase -- see STAGE3_SETUP.md), which signs or
           refuses independently of both of the above.

This module only ever READS the database -- it has no code path that
writes to active_positions or any pipeline table. Give it a database role
scoped to SELECT only if you want a hard guarantee of that at the DB
level too (see STAGE3_SETUP.md for the suggested GRANT statements);
DATABASE_URL defaults to the same connection main.py uses if you don't
set up a separate role, but a scoped one is stronger defense-in-depth.
"""
import os
import math
import time
import logging
import collections
from dataclasses import dataclass
from typing import Optional

import asyncpg

logger = logging.getLogger("signer_service.policy_guard")

# NOTE: this module does NOT open its own connection -- main.py passes the
# connection it holds inside the reservation transaction, so the checks and
# the order reservation are one atomic step under one advisory lock.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

ALLOWED_EXECUTION_TOKENS = [
    addr.strip() for addr in os.environ.get("ALLOWED_EXECUTION_TOKENS", "").split(",") if addr.strip()
]


def _env_amount(name: str) -> float:
    """A cap from this service's own environment. Anything that is not a
    finite, non-negative number becomes 0.0 -- which REFUSES.

    float("nan") parses, and a NaN cap passes every comparison against it:
    `requested > nan` is False, so MAX_TRADE_USD=nan meant "no per-trade cap"
    while assert_caps_consistent (`nan > ceiling`, also False) waved it
    through at startup.
    """
    raw = os.environ.get(name, "0")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) and v >= 0 else 0.0


# CAPS COME FROM THIS SERVICE'S ENVIRONMENT, NOT FROM THE DATABASE.
#
# app_settings is written by the web app -- by its settings form. A cap the
# signer read from there was a cap the web app could raise, so a compromised
# or buggy web app could lift the very limit this layer exists to hold
# against it. app_settings may now only TIGHTEN these (see _effective_total_cap).
MAX_TRADE_USD = _env_amount("MAX_TRADE_USD")
MAX_TOTAL_DEPLOYED_USD = _env_amount("SIGNER_MAX_TOTAL_DEPLOYED_USD")
MAX_DAILY_USD = _env_amount("SIGNER_MAX_DAILY_USD")
MAX_ORDERS_PER_DAY = int(_env_amount("SIGNER_MAX_ORDERS_PER_DAY"))

# The pipeline carries its own hard ceiling as a source constant
# (execution_rails.ABSOLUTE_MAX_POSITION_USD). These caps, plus Turnkey's own
# enclave policy, are independent limits on the same quantity; a signer cap
# above the source ceiling is refused at startup.
try:
    import execution_rails
    from execution_rails import ABSOLUTE_MAX_POSITION_USD
except Exception:  # pragma: no cover - the constant is mirrored if unavailable
    execution_rails = None
    ABSOLUTE_MAX_POSITION_USD = 250.0

DEVNET_MODE = "devnet_transfer_test"


def assert_caps_consistent() -> None:
    """Refuse to start on a cap set that cannot be what the operator meant."""
    for name in ("MAX_TRADE_USD", "SIGNER_MAX_TOTAL_DEPLOYED_USD", "SIGNER_MAX_DAILY_USD",
                 "SIGNER_MAX_ORDERS_PER_DAY"):
        raw = os.environ.get(name)
        if raw is None or raw.strip() == "":
            continue
        try:
            ok = math.isfinite(float(raw)) and float(raw) >= 0
        except ValueError:
            ok = False
        if not ok:
            raise RuntimeError(f"{name} is set to a value that is not a finite, non-negative "
                               f"number. Refusing to start rather than treat it as unlimited.")
    if MAX_TRADE_USD > ABSOLUTE_MAX_POSITION_USD:
        raise RuntimeError(
            f"MAX_TRADE_USD (${MAX_TRADE_USD:.2f}) exceeds the pipeline's absolute ceiling "
            f"ABSOLUTE_MAX_POSITION_USD (${ABSOLUTE_MAX_POSITION_USD:.2f}). Refusing to start: "
            f"a signer that would sign more than the pipeline believes is possible is not a "
            f"safety layer. Lower MAX_TRADE_USD, or raise the source constant in a reviewed commit."
        )
    if execution_rails is None:
        raise RuntimeError("execution_rails.py is missing from this image -- the pre-signature "
                           "rails cannot run. Rebuild the signer image.")


class InProcessLedger:
    """Reservations this PROCESS has made in the last 24 hours.

    The daily caps are counted from signer_orders, and a database role with
    DELETE on that table could reset them. This counter lives only in the
    signer's memory, which nothing outside the container can touch; the cap
    applies to the LARGER of the two. A restart clears it, so it narrows the
    gap rather than closing it -- the database count and Turnkey's own policy
    remain.
    """
    WINDOW_S = 86_400.0

    def __init__(self):
        self._events = collections.deque()

    def _trim(self, now: float) -> None:
        while self._events and now - self._events[0][0] > self.WINDOW_S:
            self._events.popleft()

    def record(self, usd: float, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self._trim(now)
        self._events.append((now, float(usd)))

    def totals(self, now: Optional[float] = None):
        now = time.monotonic() if now is None else now
        self._trim(now)
        return len(self._events), sum(u for _, u in self._events)


LEDGER = InProcessLedger()

# Statuses that consume a daily allowance: anything that was, or may have
# been, signed. REFUSED and FAILED never reached a signature.
COUNTED_STATUSES = ("RESERVED", "SIGNED_BROADCAST", "UNKNOWN")


@dataclass
class PolicyResult:
    allowed: bool
    reason: str


def _effective_total_cap(env_cap: float, settings_cap) -> Optional[float]:
    """The total-deployment cap: the SMALLER of this service's env cap and the
    app_settings value. None when neither is set (no cap)."""
    caps = [env_cap] if env_cap > 0 else []
    try:
        if settings_cap is not None:
            v = float(settings_cap)
            if math.isfinite(v) and v > 0:
                caps.append(v)
    except (TypeError, ValueError):
        pass
    return min(caps) if caps else None


async def check_execution_allowed(conn: "asyncpg.Connection", token_address: str,
                                  requested_usd: float, client_order_id: str,
                                  sol_lamports: Optional[int],
                                  mode: str = DEVNET_MODE) -> PolicyResult:
    """Every check before an order may be reserved. Nothing here is taken on
    the caller's word: caps come from this service's environment, counts from
    its own order ledger and memory, state from the database. Must run inside
    main._reserve's transaction, under its advisory lock."""
    devnet = mode == DEVNET_MODE
    try:
        requested = float(requested_usd)
    except (TypeError, ValueError):
        return PolicyResult(False, "requested amount is not a number")
    if not (math.isfinite(requested) and requested > 0):
        return PolicyResult(False, f"requested amount must be a positive finite number, got {requested_usd}")

    if not ALLOWED_EXECUTION_TOKENS:
        return PolicyResult(False, "ALLOWED_EXECUTION_TOKENS is not configured on the signer service -- refusing all execution until it is (see STAGE3_SETUP.md)")
    if token_address not in ALLOWED_EXECUTION_TOKENS:
        return PolicyResult(False, f"{token_address} is not on this signer's ALLOWED_EXECUTION_TOKENS allowlist")

    if MAX_TRADE_USD <= 0:
        return PolicyResult(False, "MAX_TRADE_USD is not configured (or is zero) on the signer service -- refusing all execution until it is")
    if requested > MAX_TRADE_USD:
        return PolicyResult(False, f"requested ${requested:.2f} exceeds this signer's MAX_TRADE_USD (${MAX_TRADE_USD:.2f})")
    if MAX_ORDERS_PER_DAY <= 0:
        return PolicyResult(False, "SIGNER_MAX_ORDERS_PER_DAY is not configured (or is zero) -- refusing all execution until it is")

    settings_row = await conn.fetchrow(
        "SELECT run_status, max_total_capital_usd FROM app_settings WHERE id = 1;")
    if settings_row is None:
        return PolicyResult(False, "app_settings row is missing -- cannot verify run_status, refusing")
    run_status = settings_row["run_status"]
    if run_status != "RUNNING":
        return PolicyResult(False, f"run_status is '{run_status}', not RUNNING -- trading is paused or stopped")

    # The request must describe a reservation the pipeline actually wrote:
    # same token, same size, still pending. A request with no ledger row is
    # an order nothing will ever mark, close or count.
    position = await conn.fetchrow(
        "SELECT token_address, allocated_usd::float AS allocated_usd, execution_status "
        "FROM active_positions WHERE client_order_id::text = $1;", client_order_id)
    if position is None:
        return PolicyResult(False, f"no ledger reservation exists for order {client_order_id}")
    if position["execution_status"] != "PENDING_EXECUTION":
        return PolicyResult(False, f"order {client_order_id} is '{position['execution_status']}', not PENDING_EXECUTION")
    if position["token_address"] != token_address:
        return PolicyResult(False, f"order {client_order_id} reserved a different token")
    if abs(float(position["allocated_usd"]) - requested) > 0.01:
        return PolicyResult(False, f"order {client_order_id} reserved ${float(position['allocated_usd']):.2f}, "
                                   f"not the ${requested:.2f} requested")

    if not devnet and MAX_TOTAL_DEPLOYED_USD <= 0:
        # Required from THIS service's env with real funds. A cap present
        # only in app_settings is one the web app can raise.
        return PolicyResult(False,
            "SIGNER_MAX_TOTAL_DEPLOYED_USD is not configured -- refusing real-funds execution "
            "(a cap set only in the dashboard is not a limit the signer can rely on)")
    total_cap = _effective_total_cap(MAX_TOTAL_DEPLOYED_USD, settings_row["max_total_capital_usd"])
    if total_cap is None:
        if not devnet:
            return PolicyResult(False,
                "no total-deployment cap is configured (SIGNER_MAX_TOTAL_DEPLOYED_USD) -- refusing "
                "to execute with real funds against an unbounded limit")
        logger.warning("No total-deployment cap configured -- acceptable on devnet only.")
    else:
        # EXCLUDING this order's own row. The pipeline writes the reservation
        # before calling, so counting it AND adding requested_usd charged every
        # order twice -- a $50 cap refused a $30 order with $0 deployed.
        deployed_other = await conn.fetchval(
            "SELECT COALESCE(SUM(allocated_usd), 0)::float FROM active_positions "
            "WHERE client_order_id::text <> $1;", client_order_id)
        deployed_other = float(deployed_other or 0.0)
        if not math.isfinite(deployed_other):
            return PolicyResult(False, "deployed capital is not a finite number -- refusing")
        if deployed_other + requested > total_cap:
            return PolicyResult(False,
                f"requested ${requested:.2f} would push deployed capital to "
                f"${deployed_other + requested:.2f}, over the cap of ${total_cap:.2f}")

    row = await conn.fetchrow(
        "SELECT COUNT(*)::int AS n, COALESCE(SUM(requested_usd), 0)::float AS usd "
        "FROM signer_orders WHERE created_at > CURRENT_TIMESTAMP - INTERVAL '24 hours' "
        "AND status = ANY($1::text[]);", list(COUNTED_STATUSES))
    mem_n, mem_usd = LEDGER.totals()
    orders_24h = max(int(row["n"]), mem_n)
    usd_24h = max(float(row["usd"]), mem_usd)
    if MAX_DAILY_USD > 0:
        if usd_24h + requested > MAX_DAILY_USD:
            return PolicyResult(False,
                f"24h signed notional would reach ${usd_24h + requested:.2f}, over "
                f"SIGNER_MAX_DAILY_USD (${MAX_DAILY_USD:.2f})")
    elif not devnet:
        return PolicyResult(False, "SIGNER_MAX_DAILY_USD is not configured -- refusing real-funds execution")

    # The pre-signature rails (execution_rails.py): account-level refusals
    # that do not depend on the token. They existed, fully tested, and were
    # called from nowhere.
    verdict = execution_rails.check_entry_rails(
        mode="LIVE", run_status=run_status, requested_usd=requested,
        live_max_position_usd=MAX_TRADE_USD, max_position_usd=MAX_TRADE_USD,
        orders_sent_today=orders_24h, max_orders_per_day=MAX_ORDERS_PER_DAY,
        sol_lamports=sol_lamports, quote_balance_usd=None,
        # The devnet self-transfer spends lamports only. Every swap mode must
        # prove a quote balance.
        quote_balance_required=not devnet)
    if not verdict.ok:
        return PolicyResult(False, f"execution rail: {verdict.reason}")

    return PolicyResult(True, "all checks passed")
