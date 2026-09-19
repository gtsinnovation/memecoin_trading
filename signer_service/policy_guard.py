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
import logging
from dataclasses import dataclass
from typing import Optional

import asyncpg

logger = logging.getLogger("signer_service.policy_guard")

# NOTE: this module does NOT open its own connection -- check_execution_allowed
# receives the pool from main.py, which builds it from main.py's DATABASE_URL.
# Setting a separate read-only URL here would have no effect. The least-
# privilege role in STAGE3_SETUP.md Part 2b is configured through the
# service's single DATABASE_URL, not a second one.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

ALLOWED_EXECUTION_TOKENS = [
    addr.strip() for addr in os.environ.get("ALLOWED_EXECUTION_TOKENS", "").split(",") if addr.strip()
]
MAX_TRADE_USD = float(os.environ.get("MAX_TRADE_USD", "0"))


@dataclass
class PolicyResult:
    allowed: bool
    reason: str


async def check_execution_allowed(pool: asyncpg.Pool, token_address: str,
                                    requested_usd: float) -> PolicyResult:
    """The one function this module exists for. Every check below is
    read directly from Postgres or from this service's own env config --
    nothing here is taken on the caller's word. Returns as soon as any
    check fails (order doesn't matter for correctness, but cheapest/most
    obviously-wrong checks go first)."""

    if requested_usd <= 0:
        return PolicyResult(False, f"requested amount must be positive, got {requested_usd}")

    if not ALLOWED_EXECUTION_TOKENS:
        return PolicyResult(False, "ALLOWED_EXECUTION_TOKENS is not configured on the signer service -- refusing all execution until it is (see STAGE3_SETUP.md)")

    if token_address not in ALLOWED_EXECUTION_TOKENS:
        return PolicyResult(False, f"{token_address} is not on this signer's ALLOWED_EXECUTION_TOKENS allowlist")

    if MAX_TRADE_USD <= 0:
        return PolicyResult(False, "MAX_TRADE_USD is not configured (or is zero) on the signer service -- refusing all execution until it is")

    if requested_usd > MAX_TRADE_USD:
        return PolicyResult(False, f"requested ${requested_usd:.2f} exceeds this signer's MAX_TRADE_USD (${MAX_TRADE_USD:.2f})")

    async with pool.acquire() as conn:
        settings_row = await conn.fetchrow("SELECT run_status, max_total_capital_usd FROM app_settings WHERE id = 1;")
        if settings_row is None:
            return PolicyResult(False, "app_settings row is missing -- cannot verify run_status, refusing")

        run_status = settings_row["run_status"]
        if run_status != "RUNNING":
            return PolicyResult(False, f"run_status is '{run_status}', not RUNNING -- trading is paused or stopped")

        max_total_capital_usd = settings_row["max_total_capital_usd"]
        if max_total_capital_usd is None:
            # schema.sql seeds app_settings with this NULL, and NULL is
            # documented as "no cap" -- so on a stock database one of the four
            # checks this module is credited with simply does not run. That is
            # a legitimate operator choice on devnet, where the amount is a
            # fixed 1000 lamports and requested_usd is notional. It is NOT a
            # legitimate default once a real swap is sized from requested_usd,
            # so mainnet mode refuses rather than skipping.
            if os.environ.get("SIGNER_MODE", "devnet_transfer_test") != "devnet_transfer_test":
                return PolicyResult(False,
                    "max_total_capital_usd is NULL (no cap configured) -- refusing to execute "
                    "with real funds against an unbounded capital limit. Set it in app_settings.")
            logger.warning(
                "max_total_capital_usd is NULL -- the total-capital check is not running. "
                "Acceptable on devnet; set a cap before Stage 4."
            )
        if max_total_capital_usd is not None:
            currently_allocated = await conn.fetchval(
                "SELECT COALESCE(SUM(allocated_usd), 0.0)::float FROM active_positions;"
            )
            if (float(currently_allocated) + requested_usd) > float(max_total_capital_usd):
                return PolicyResult(
                    False,
                    f"requested ${requested_usd:.2f} would push total allocated capital to "
                    f"${float(currently_allocated) + requested_usd:.2f}, over the configured cap "
                    f"of ${float(max_total_capital_usd):.2f}"
                )

    return PolicyResult(True, "all checks passed")
