# engine.py

import os
import time
import logging
import random
import concurrent.futures
import threading
from typing import TypedDict, Optional, Dict, Any, List
import psycopg2
from psycopg2.extras import RealDictCursor
from langgraph.graph import StateGraph, START, END

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("graph_engine")
from fill_accounting import round_significant
import holder_concentration
import market_microstructure

DB_DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:secret@localhost:5432/memecoin_trading")


class AgentUnresponsiveError(Exception):
    """Raised when a single agent node fails to return within its configured
    timeout, so the caller can tell one hung agent apart from an ordinary
    error inside the graph."""
    def __init__(self, agent_name: str, timeout_seconds: float):
        self.agent_name = agent_name
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Agent '{agent_name}' did not respond within {timeout_seconds}s.")


# 1. State Matrix Model
class AgentNetworkState(TypedDict):
    token_symbol: str
    token_address: str
    current_price: float
    pool_liquidity_usd: float          # total value locked -- BOTH pool sides
    tradeable_depth_usd: float         # one side, i.e. what a trade eats into
    # Breadth inputs -- see node_E_BREADTH.
    volume_h1_usd: float
    txns_h1_buys: Optional[int]
    txns_h1_sells: Optional[int]
    total_holders: Optional[int]
    holders_added_per_hour: Optional[float]
    capital_per_participant_usd: Optional[float]
    # Retained as the hook for a real social feed (twitterapi.io) if one is
    # ever wired in. Nothing reads it today -- E_BREADTH replaced the gate
    # that used to -- and it stays flagged as unavailable rather than
    # quietly passing a placeholder off as a measurement.
    social_volume_score: float

    onchain_flow_velocity: float       
    # Short-window direction. The free provider already fetches all four of
    # these; until now nothing carried them into the network, so A_ORBIT and
    # C_VECTOR had nothing to judge and returned a hardcoded pass. Optional
    # because "the provider did not report it" is a real state that must not
    # arrive as a permissive zero -- see market_microstructure.
    price_change_m5: Optional[float]
    price_change_h1: Optional[float]
    txns_m5_buys: Optional[int]
    txns_m5_sells: Optional[int]
    top_10_holder_percentage: float    
    estimated_slippage_percent: float  
    # Explicit "we could not measure this" flags. The numeric fields above
    # cannot carry None (trading_sessions declares them NOT NULL), so a
    # failed measurement arrives as 0.0 -- which is the MOST PERMISSIVE value
    # for every gate that reads it. These booleans are how a gate tells
    # "measured zero" apart from "never measured".
    holder_data_missing: bool
    # Holder concentration, every way it was measured. MEASUREMENT ONLY --
    # no node reads these; they are declared because the provider snapshot IS
    # the graph input, so a key the state does not declare is a key LangGraph
    # rejects. paper_trading.record_candidate persists them, and section 8 of
    # stage2_check.sql is what they exist for: choosing which definition the
    # concentration ceiling should be applied to. See holder_concentration.py.
    holder_concentration_source: Optional[str]
    holder_concentration_provider_pct: Optional[float]
    holder_concentration_raw_pct: Optional[float]
    holder_concentration_wallet_pct: Optional[float]
    holder_concentration_program_pct: Optional[float]
    holder_concentration_burn_pct: Optional[float]
    slippage_data_missing: bool
    onchain_volume_increasing: bool
    is_liquidity_safe: bool
    entry_conditions_met: bool
    pullback_detected: bool
    target_pullback_price: float
    invalidation_level_price: float
    target_exit_price: float
    is_breadth_healthy: bool
    holder_data_fresh: bool
    # Set when direction or volatility says a naive market order should not be
    # used. Advisory: it never blocks a trade, it changes how one is placed.
    execution_degraded: bool
    flow_detail: Optional[str]
    drawdown_detail: Optional[str]
    max_safe_position_usd: float
    # Rug/security signals, populated only by the "free" provider
    # (RugCheck + Jupiter). None means unknown, which is NOT the same as
    # safe -- any future gate reading these must treat None as "no
    # information" rather than as a pass.
    rug_score: Optional[float]
    rugged: Optional[bool]
    mint_authority_renounced: Optional[bool]
    freeze_authority_renounced: Optional[bool]
    token_age_hours: Optional[float]
    launchpad: Optional[str]
    final_briefing_compiled: str
    position_logged: bool
    session_closed: bool
    termination_reason: Optional[str]


# 2. Database Connection Adapters
def write_system_alert(level: str, agent: str, message: str):
    """Logs system alerts into the outbox buffer table."""
    conn = None
    try:
        conn = psycopg2.connect(DB_DSN)
        with conn:
            with conn.cursor() as cur:
                query = """
                    INSERT INTO system_alerts (log_level, agent_name, message)
                    VALUES (%s, %s, %s);
                """
                cur.execute(query, (level, agent, message))
    except Exception as e:
        logger.error(f"Failed to record framework system alert log rows: {e}")
    finally:
        if conn is not None:
            conn.close()
def save_trading_session(state: AgentNetworkState):
    """Saves structural session records to the tracking ledger table."""
    conn = None
    try:
        conn = psycopg2.connect(DB_DSN)
        with conn:
            with conn.cursor() as cur:
                query = """
                    INSERT INTO trading_sessions (
                        token_symbol, token_address, current_price, pool_liquidity_usd,
                        social_volume_score, onchain_flow_velocity, top_10_holder_percentage,
                        max_position_size_usd, target_pullback_price, invalidation_level_price,
                        session_status, final_briefing
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """
                status = "REJECTED" if state.get("termination_reason") else "APPROVED"
                brief = state.get("final_briefing_compiled") or state.get("termination_reason", "Unknown termination.")
                cur.execute(query, (
                    state["token_symbol"], state["token_address"], state["current_price"],
                    state["pool_liquidity_usd"], state["social_volume_score"], state["onchain_flow_velocity"],
                    state["top_10_holder_percentage"], state.get("max_safe_position_usd"),
                    state.get("target_pullback_price"), state.get("invalidation_level_price"),
                    status, brief
                ))
    except Exception as e:
        logger.error(f"Failed to save system operational metrics record: {e}")
    finally:
        if conn is not None:
            conn.close()


def save_active_position(state: AgentNetworkState) -> bool:
    """Appends successful entries into the asset exposure position table.

    Returns True only if a row was actually written. False means either the
    duplicate guard declined (a position is already open for this token) or
    the write failed. The caller MUST NOT report success on a False: with
    Stage 3 enabled, "position_logged" is what decides whether the signer is
    asked to execute, so a silent False would ask for a real trade whose
    position row does not exist -- unstoppable, unmarkable, with no
    take-profit or stop-loss attached to it.

    Also records the take-profit (target_exit_price) and stop-loss
    (invalidation_level_price) levels, and seeds last_simulated_price at the
    entry price, so evaluate_open_positions() has what it needs to later
    close this position and compute realized P&L.
    """
    conn = None
    try:
        entry_price = state.get("target_pullback_price", 0.0)
        conn = psycopg2.connect(DB_DSN)
        with conn:
            with conn.cursor() as cur:
                # ONE OPEN POSITION PER TOKEN.
                #
                # Without this, an approved token opens a fresh position on
                # every tick for as long as it keeps passing the gates. The
                # dashboard showed the result plainly: five identical $STONK
                # rows at +$150.5x, and $USEFUL held twice at $845.98 with
                # trigger floors one decimal apart. Those were never five
                # trades -- they were one trade counted five times, inflating
                # both realized P&L and capital deployed, and making the win
                # rate a report about whichever coin happened to recur most.
                #
                # A real trader cannot buy the same setup fifty times in
                # ninety seconds. The unique partial index in migrate.sql is
                # the backstop for two ticks racing past this check.
                cur.execute(
                    "SELECT 1 FROM active_positions WHERE token_address = %s LIMIT 1;",
                    (state["token_address"],))
                if cur.fetchone():
                    logger.debug(
                        f"Position already open for {state['token_symbol']}; not opening a second."
                    )
                    return False
                query = """
                    INSERT INTO active_positions (
                        token_symbol, token_address, allocated_usd, entry_trigger,
                        target_exit_price, invalidation_level_price, last_simulated_price
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s);
                """
                cur.execute(query, (
                    state["token_symbol"], state["token_address"],
                    state.get("max_safe_position_usd", 0.0), entry_price,
                    state.get("target_exit_price"), state.get("invalidation_level_price"),
                    entry_price
                ))
        return True
    except Exception as e:
        logger.error(f"Failed to log entry position to accounting ledger: {e}")
        return False
    finally:
        if conn is not None:
            conn.close()


def evaluate_open_positions() -> List[Dict[str, Any]]:
    """Marks every open position to its REAL current price and closes any
    that has hit its take-profit or stop-loss level, moving it into
    closed_positions with a realized P&L.

    Stage 2 changed this from a bounded random walk to real prices. That
    distinction is the difference between a demo and an experiment: every
    P&L figure this project produced before now was generated by that walk
    and measured nothing.

    A position whose price can't be fetched this tick is SKIPPED, not
    marked. An unknown price is not zero, and treating it as zero would
    trip the stop-loss and book a fabricated total loss.
    """
    conn = None
    closed_summaries: List[Dict[str, Any]] = []
    # Staged here and only merged after the transaction commits.
    pending_summaries = []
    try:
        conn = psycopg2.connect(DB_DSN)
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, token_symbol, token_address, allocated_usd, entry_trigger,
                           target_exit_price, invalidation_level_price, last_simulated_price, captured_at
                    FROM active_positions;
                """)
                open_positions = cur.fetchall()

                prices = _fetch_position_prices([p["token_address"] for p in open_positions])

                for pos in open_positions:
                    next_price = prices.get(pos["token_address"])
                    if next_price is None:
                        logger.warning(
                            f"No current price for {pos['token_symbol']} this tick -- "
                            f"leaving the position untouched rather than marking it to an assumed value."
                        )
                        continue
                    next_price = float(next_price)

                    exit_reason = None
                    if pos["target_exit_price"] is not None and next_price >= float(pos["target_exit_price"]):
                        exit_reason = "TARGET_HIT"
                        next_price = float(pos["target_exit_price"])
                    elif pos["invalidation_level_price"] is not None and next_price <= float(pos["invalidation_level_price"]):
                        exit_reason = "STOPPED_OUT"
                        next_price = float(pos["invalidation_level_price"])

                    if exit_reason is None:
                        cur.execute(
                            # Column name kept for schema continuity; the value is
                            # now a real market price, not a simulated one.
                            "UPDATE active_positions SET last_simulated_price = %s WHERE id = %s;",
                            (round(next_price, 8), pos["id"])
                        )
                        continue

                    entry_price = float(pos["entry_trigger"])
                    allocated_usd = float(pos["allocated_usd"])
                    pnl_percent = ((next_price / entry_price) - 1.0) * 100.0 if entry_price else 0.0
                    pnl_usd = allocated_usd * (pnl_percent / 100.0)

                    cur.execute("""
                        INSERT INTO closed_positions (
                            token_symbol, token_address, allocated_usd, entry_trigger,
                            exit_price, exit_reason, realized_pnl_usd, realized_pnl_percent, opened_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """, (
                        pos["token_symbol"], pos["token_address"], allocated_usd, entry_price,
                        round(next_price, 8), exit_reason, round(pnl_usd, 2), round(pnl_percent, 2),
                        pos["captured_at"]
                    ))
                    cur.execute("DELETE FROM active_positions WHERE id = %s;", (pos["id"],))

                    pending_summaries.append({
                        "token_symbol": pos["token_symbol"],
                        "exit_reason": exit_reason,
                        "realized_pnl_usd": round(pnl_usd, 2),
                    })
        # Only now. `with conn` commits on a clean exit and rolls back on an
        # exception, so appending to the returned list INSIDE the block
        # reported closes that the database then discarded -- operators saw
        # exits that never happened, and the same positions closed again on
        # the next tick because the DELETE was rolled back too.
        closed_summaries.extend(pending_summaries)
    except Exception as e:
        logger.error(f"Failed to evaluate open position exits: {e}")
        # pending_summaries is deliberately dropped here: the transaction
        # rolled back, so none of those closes exist.
    finally:
        if conn is not None:
            conn.close()

    for summary in closed_summaries:
        level = "INFO" if summary["realized_pnl_usd"] >= 0 else "WARN"
        write_system_alert(
            level, "K_SETTLE",
            f"Closed ${summary['token_symbol']}: {summary['exit_reason']}, "
            f"realized P&L ${summary['realized_pnl_usd']}."
        )

    return closed_summaries


def _fetch_position_prices(token_addresses: List[str]) -> Dict[str, float]:
    """Real current prices for open positions, batched into one request.

    This replaced a bounded random walk (_simulate_next_price). Every P&L
    number this project produced before Stage 2 was generated by that walk
    and was therefore noise -- see paper_trading.py. Imported lazily so
    engine.py keeps working in test harnesses that stub the market-data
    layer.
    """
    try:
        from market_data import fetch_current_prices_sync
        return fetch_current_prices_sync(token_addresses)
    except Exception as e:
        logger.error(f"Could not fetch position prices: {e}")
        return {}


# 2b. Operator Settings (capital limits, run duration, wallet labels,
# watchdog behavior, kill-switch thresholds) -- a single row in app_settings.
_settings_cache: Dict[str, Any] = {"data": None, "fetched_at": 0.0}
_SETTINGS_CACHE_TTL_SECONDS = 3.0
# How long a cached copy may still be served once the database has stopped
# answering. Past this the reader returns None and every caller must fail
# closed, because the alternative is serving a run_status from before an
# operator paused the agent -- for the entire length of the outage.
_SETTINGS_MAX_STALE_SECONDS = 60.0


def get_app_settings(force_refresh: bool = False) -> Optional[Dict[str, Any]]:
    """Reads the single app_settings row, cached briefly since several agent
    nodes consult it on every tick (capital limit, watchdog timeout, run
    gate) and a DB round-trip per node per tick would add needless load."""
    now = time.monotonic()
    if not force_refresh and _settings_cache["data"] is not None and (now - _settings_cache["fetched_at"]) < _SETTINGS_CACHE_TTL_SECONDS:
        return _settings_cache["data"]

    conn = None
    try:
        conn = psycopg2.connect(DB_DSN)
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM app_settings WHERE id = 1;")
                row = cur.fetchone()
                _settings_cache["data"] = dict(row) if row else None
                _settings_cache["fetched_at"] = now
                return _settings_cache["data"]
    except Exception as e:
        # Serving the last good row through a brief blip is useful. Serving it
        # forever is not: a pause set during the outage would never be seen,
        # and the agent would keep trading on a stale RUNNING.
        age = now - _settings_cache["fetched_at"]
        if _settings_cache["data"] is not None and age <= _SETTINGS_MAX_STALE_SECONDS:
            logger.error(f"Failed to load app settings ({e}); serving cached copy {age:.0f}s old.")
            return _settings_cache["data"]
        logger.error(
            f"Failed to load app settings ({e}) and the cached copy is {age:.0f}s old "
            f"(limit {_SETTINGS_MAX_STALE_SECONDS:.0f}s) -- returning None so callers fail closed."
        )
        _settings_cache["data"] = None
        return None
    finally:
        if conn is not None:
            conn.close()


def set_run_status(status: str, reason: Optional[str] = None):
    """Updates the run gate (RUNNING / PAUSED_MANUAL / PAUSED_KILL_SWITCH /
    PAUSED_DURATION_ELAPSED / SHUTDOWN_WATCHDOG) that I_ACCOUNTANT consults before opening new positions."""
    conn = None
    try:
        conn = psycopg2.connect(DB_DSN)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app_settings SET run_status = %s, run_status_reason = %s, updated_at = CURRENT_TIMESTAMP WHERE id = 1;",
                    (status, reason)
                )
    except Exception as e:
        logger.error(f"Failed to update run_status to {status}: {e}")
    finally:
        if conn is not None:
            conn.close()
    _settings_cache["data"] = None  # force a fresh read next time anything checks


def resume_trading():
    """Manually clears a pause (whether from a manual Pause, a kill-switch
    trip, or an elapsed run-duration timer) and restarts the run timer.
    Called from the dashboard's 'Resume Trading' action."""
    conn = None
    try:
        conn = psycopg2.connect(DB_DSN)
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE app_settings
                    SET run_status = 'RUNNING', run_status_reason = NULL, run_started_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1;
                """)
    except Exception as e:
        logger.error(f"Failed to resume trading: {e}")
    finally:
        if conn is not None:
            conn.close()
    _settings_cache["data"] = None


def pause_trading(reason: Optional[str] = None):
    """Manually pauses new entries at the operator's request (the dashboard's
    'Pause Trading' button). Same effect as an automatic kill-switch trip --
    only NEW entries are blocked, via the run_status gate I_ACCOUNTANT checks
    before every save_active_position() call. Already-open positions are
    completely untouched and keep resolving normally through
    evaluate_open_positions() (their own take-profit / stop-loss levels).
    Does not stop the pipeline loop itself -- only resume_trading() (the
    'Resume Trading' button) lifts this."""
    reason = reason or "Paused manually by operator."
    set_run_status("PAUSED_MANUAL", reason)
    write_system_alert("INFO", "OPERATOR", f"{reason} New entries blocked until resumed.")


class CapitalReadError(Exception):
    """Raised when deployed capital cannot be read.

    Deliberately an exception rather than a sentinel: this feeds the capital
    cap, and 0.0 -- the obvious "safe" default -- is the single value that
    GUARANTEES the cap check passes. A caller that cannot tell "nothing
    deployed" from "could not read" will happily open a position while
    already over the limit.
    """


def get_total_deployed_capital() -> float:
    """Sum of allocated_usd across every currently open position.

    Raises CapitalReadError rather than returning a number it isn't sure of.
    """
    conn = None
    try:
        conn = psycopg2.connect(DB_DSN)
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COALESCE(SUM(allocated_usd), 0.0) FROM active_positions;")
                return float(cur.fetchone()[0])
    except Exception as e:
        logger.error(f"Failed to compute total deployed capital: {e}")
        raise CapitalReadError(str(e)) from e
    finally:
        if conn is not None:
            conn.close()


def check_kill_switch() -> Dict[str, Any]:
    """Evaluates the configured run-duration and kill-switch thresholds
    against realized P&L in closed_positions, and flips run_status to a
    paused state if one is breached. Per the configured behavior, this only
    ever blocks NEW entries (enforced by I_ACCOUNTANT reading run_status) --
    it never touches already-open positions, which keep resolving normally
    through evaluate_open_positions().

    Fails closed: if settings can't be read, callers should treat the gate
    as not-open rather than silently keep trading blind.
    """
    conn = None
    try:
        conn = psycopg2.connect(DB_DSN)
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM app_settings WHERE id = 1;")
                settings = cur.fetchone()
                if not settings:
                    return {"gate_open": False, "reason": "SETTINGS_UNAVAILABLE"}

                # Run-duration expiry
                if settings["run_status"] == "RUNNING" and settings["run_duration_minutes"]:
                    cur.execute("SELECT EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - run_started_at)) / 60.0 AS elapsed_minutes FROM app_settings WHERE id = 1;")
                    elapsed_minutes = float(cur.fetchone()["elapsed_minutes"])
                    if elapsed_minutes >= float(settings["run_duration_minutes"]):
                        reason = f"Configured run duration of {settings['run_duration_minutes']} minutes elapsed."
                        cur.execute(
                            "UPDATE app_settings SET run_status = 'PAUSED_DURATION_ELAPSED', run_status_reason = %s, updated_at = CURRENT_TIMESTAMP WHERE id = 1;",
                            (reason,)
                        )
                        _settings_cache["data"] = None
                        write_system_alert("CRITICAL", "RUN_TIMER", reason + " New entries paused.")
                        return {"gate_open": False, "reason": "PAUSED_DURATION_ELAPSED"}

                if settings["run_status"] != "RUNNING":
                    return {"gate_open": False, "reason": settings["run_status"]}

                # Kill-switch thresholds, evaluated against realized P&L only
                cur.execute("SELECT COALESCE(SUM(realized_pnl_usd), 0.0)::float AS total FROM closed_positions;")
                total_realized_pnl = float(cur.fetchone()["total"])

                if settings["kill_switch_max_loss_usd"] is not None and total_realized_pnl <= -abs(float(settings["kill_switch_max_loss_usd"])):
                    reason = f"Realized loss of ${round(total_realized_pnl, 2)} breached the configured max loss of ${settings['kill_switch_max_loss_usd']}."
                    cur.execute(
                        "UPDATE app_settings SET run_status = 'PAUSED_KILL_SWITCH', run_status_reason = %s, updated_at = CURRENT_TIMESTAMP WHERE id = 1;",
                        (reason,)
                    )
                    _settings_cache["data"] = None
                    write_system_alert("CRITICAL", "KILL_SWITCH", reason + " New entries paused.")
                    return {"gate_open": False, "reason": "PAUSED_KILL_SWITCH"}

                if settings["kill_switch_max_drawdown_pct"] is not None and settings["max_total_capital_usd"]:
                    drawdown_pct = max(0.0, (-total_realized_pnl / float(settings["max_total_capital_usd"])) * 100.0)
                    if drawdown_pct >= float(settings["kill_switch_max_drawdown_pct"]):
                        reason = f"Drawdown of {round(drawdown_pct, 2)}% breached the configured {settings['kill_switch_max_drawdown_pct']}% limit."
                        cur.execute(
                            "UPDATE app_settings SET run_status = 'PAUSED_KILL_SWITCH', run_status_reason = %s, updated_at = CURRENT_TIMESTAMP WHERE id = 1;",
                            (reason,)
                        )
                        _settings_cache["data"] = None
                        write_system_alert("CRITICAL", "KILL_SWITCH", reason + " New entries paused.")
                        return {"gate_open": False, "reason": "PAUSED_KILL_SWITCH"}

                # n <= 0 must be treated as "not configured". With n = 0,
                # LIMIT 0 returns [], len([]) == 0 is True, and all([]) is
                # VACUOUSLY True -- so the kill switch trips instantly, on a
                # database with zero closed trades, reporting "0 consecutive
                # losing trades breached the limit". The dashboard sends 0
                # for an empty field, so this is reachable by leaving a box
                # blank rather than by any deliberate action.
                _streak = settings["kill_switch_max_consecutive_losses"]
                if _streak is not None and int(_streak) > 0:
                    n = int(_streak)
                    cur.execute("SELECT realized_pnl_usd FROM closed_positions ORDER BY id DESC LIMIT %s;", (n,))
                    recent = cur.fetchall()
                    if len(recent) == n and all(float(r["realized_pnl_usd"]) < 0 for r in recent):
                        reason = f"{n} consecutive losing trades breached the configured kill-switch limit."
                        cur.execute(
                            "UPDATE app_settings SET run_status = 'PAUSED_KILL_SWITCH', run_status_reason = %s, updated_at = CURRENT_TIMESTAMP WHERE id = 1;",
                            (reason,)
                        )
                        _settings_cache["data"] = None
                        write_system_alert("CRITICAL", "KILL_SWITCH", reason + " New entries paused.")
                        return {"gate_open": False, "reason": "PAUSED_KILL_SWITCH"}

                return {"gate_open": True, "reason": None}
    except Exception as e:
        logger.error(f"Failed to evaluate kill-switch/run-duration gate: {e}")
        return {"gate_open": False, "reason": "GATE_CHECK_FAILED"}
    finally:
        if conn is not None:
            conn.close()


# 2c. Agent Watchdog -- wraps every node with a timeout so one hung agent
# (an unresponsive external call, an infinite loop, a DB stall) is detected
# and reported by name instead of hanging the whole pipeline forever.
# A hung agent cannot be killed -- Python has no way to interrupt a thread
# blocked in a socket read. The timeout only stops US waiting; the thread
# keeps the worker forever.
#
# With max_workers=1 that was terminal: one wedged agent held the only
# worker, so every subsequent tick queued behind it and timed out in turn.
# The pipeline died permanently after a single hang, emitting one CRITICAL
# per tick, and only a container restart recovered it.
#
# The fix is to ABANDON the wedged pool rather than wait on it. The stuck
# thread is left to finish or leak (it is daemon-threaded, so it cannot hold
# up shutdown), and the next tick gets a clean worker.
_watchdog_pool_lock = threading.Lock()


def _new_watchdog_pool() -> concurrent.futures.ThreadPoolExecutor:
    return concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="agent-watchdog")


_watchdog_pool = _new_watchdog_pool()
# Pools abandoned after a timeout. Kept referenced only so they are not
# garbage-collected mid-call; never waited on.
_abandoned_pools: List[concurrent.futures.ThreadPoolExecutor] = []


def _recycle_watchdog_pool(old_pool) -> None:
    """Replace the pool after a hang so the next tick is not queued behind it."""
    global _watchdog_pool
    with _watchdog_pool_lock:
        if _watchdog_pool is not old_pool:
            return  # another thread already recycled it
        _abandoned_pools.append(old_pool)
        if len(_abandoned_pools) > 8:
            _abandoned_pools.pop(0)
        _watchdog_pool = _new_watchdog_pool()
    # wait=False: never block on the wedged thread, which is the whole point.
    try:
        old_pool.shutdown(wait=False)
    except Exception:
        pass


# Real per-agent execution times, in milliseconds, from the most recent tick.
# The dashboard used to display random.uniform(10, 55) here -- ten invented
# numbers per broadcast, on a chart labelled "AGENT LATENCY DISTRIBUTION (MS)".
# A fabricated measurement is worse than no measurement: it looks like
# evidence, so nobody goes looking for the real figure. These are timed by
# with_watchdog(), which already wraps every node, so the cost is one
# perf_counter pair per agent per tick.
#
# A node that times out records the timeout duration rather than nothing --
# an unresponsive agent is exactly the case the chart should make visible.
AGENT_LATENCY_MS: Dict[str, float] = {}


def get_agent_latencies() -> Dict[str, float]:
    """Snapshot of the last tick's real per-agent timings."""
    return dict(AGENT_LATENCY_MS)


def with_watchdog(agent_name: str):
    def decorator(fn):
        def wrapped(state: AgentNetworkState) -> Dict[str, Any]:
            settings = get_app_settings()
            timeout = float(settings["agent_timeout_seconds"]) if settings and settings.get("agent_timeout_seconds") else 15.0
            started = time.perf_counter()
            pool = _watchdog_pool
            future = pool.submit(fn, state)
            try:
                result = future.result(timeout=timeout)
                AGENT_LATENCY_MS[agent_name] = round((time.perf_counter() - started) * 1000.0, 1)
                return result
            except concurrent.futures.TimeoutError:
                AGENT_LATENCY_MS[agent_name] = round((time.perf_counter() - started) * 1000.0, 1)
                # The thread is unkillable, so abandon the pool instead of
                # leaving the next tick to queue behind it.
                _recycle_watchdog_pool(pool)
                write_system_alert(
                    "CRITICAL", agent_name,
                    f"No response within {timeout}s -- flagged unresponsive by the network "
                    f"watchdog. Worker pool recycled; the hung thread was abandoned."
                )
                raise AgentUnresponsiveError(agent_name, timeout)
            except Exception:
                AGENT_LATENCY_MS[agent_name] = round((time.perf_counter() - started) * 1000.0, 1)
                raise
        return wrapped
    return decorator


# 3. 10-Agent Logic Formulations
def node_A_ORBIT(state: AgentNetworkState) -> Dict[str, Any]:
    """Volume direction.

    This node used to return a hardcoded True, which did more than fail to
    measure: free_market_data already computes onchain_volume_increasing as
    `volume_h1 * 24 > volume_h24` and puts it in the state, so returning True
    OVERWROTE a real measurement with a constant. The provider's value is now
    left alone.

    What this node adds is direction. Volume magnitude says how much is
    trading, not which way it is going -- a token can clear every depth and
    breadth gate on enormous volume while it is being distributed into. Flow is
    read window by window because a single window is noise, and it is inferred
    from trade COUNTS because DexScreener publishes counts rather than a
    buy/sell volume split (see check_flow_from_counts on that limitation).

    Adverse flow never vetoes here. It degrades execution, which router_G and
    the order layer act on; refusing a token because five minutes looked bad
    would make the same token flip verdicts hourly.
    """
    flow = market_microstructure.check_flow_from_counts({
        "5m": {"buys": state.get("txns_m5_buys"), "sells": state.get("txns_m5_sells")},
        "1h": {"buys": state.get("txns_h1_buys"), "sells": state.get("txns_h1_sells")},
    })
    level = "WARN" if flow.severity is market_microstructure.Severity.DEGRADE else "DEBUG"
    write_system_alert(level, "A_ORBIT",
                       f"${state['token_symbol']} flow: {flow.detail}")
    return {"execution_degraded": flow.severity is market_microstructure.Severity.DEGRADE,
            "flow_detail": flow.detail}
def node_B_SENTINEL(state: AgentNetworkState) -> Dict[str, Any]:
    # Rule validation: minimum TRADEABLE DEPTH (one side of the pool), not
    # total value locked. Providers report TVL -- both sides summed -- which
    # is roughly double the depth a trade actually executes against. This
    # threshold is expressed in one-sided terms and is deliberately set to
    # half the old TVL-based 40000, so the effective strictness is unchanged
    # while the units are now honest. See free_market_data.py's docstring.
    min_depth = 20000.0
    depth = state.get("tradeable_depth_usd") or 0.0
    if depth <= 0:
        # No depth figure at all -- fail closed rather than treat unknown
        # depth as acceptable depth.
        reason = "B_SENTINEL: Short-circuit. No tradeable-depth figure available for this token."
        write_system_alert("WARN", "B_SENTINEL", reason)
        return {"is_liquidity_safe": False, "termination_reason": reason}
    if depth < min_depth:
        reason = (f"B_SENTINEL: Short-circuit. Tradeable depth (${depth}) falls below "
                  f"limit (${min_depth}). [pool TVL was ${state.get('pool_liquidity_usd', 0.0)}]")
        write_system_alert("WARN", "B_SENTINEL", reason)
        return {"is_liquidity_safe": False, "termination_reason": reason}
    write_system_alert("INFO", "B_SENTINEL", f"Tradeable depth verified safe at ${depth}.")
    return {"is_liquidity_safe": True}
def node_C_VECTOR(state: AgentNetworkState) -> Dict[str, Any]:
    """Short-window drawdown -- the only directional veto in the network.

    Previously returned a hardcoded True and measured nothing.

    A 5-minute fall of 10% or more is refused, and the reason is executability
    rather than forecasting: a market order placed into that move does not fill
    near the quote, whatever the longer-horizon thesis says. A shallower fall
    is not enough to refuse the trade, only to insist on a limit order.

    An absent 5m figure degrades execution but does not veto -- failing to read
    the tape is not evidence the token is falling. The refusal sets
    termination_reason, which router_G honours by routing past H_FUSE so
    I_ACCOUNTANT never opens the position.
    """
    finding = market_microstructure.check_drawdown_percent(state.get("price_change_m5"))

    if finding.severity is market_microstructure.Severity.VETO:
        reason = f"C_VECTOR: Short-circuit. {finding.detail}"
        write_system_alert("WARN", "C_VECTOR", reason)
        return {"entry_conditions_met": False, "termination_reason": reason}

    degraded = finding.severity is market_microstructure.Severity.DEGRADE
    write_system_alert("WARN" if degraded else "DEBUG", "C_VECTOR",
                       f"${state['token_symbol']} 5m: {finding.detail}")
    return {"entry_conditions_met": True,
            "execution_degraded": bool(state.get("execution_degraded")) or degraded,
            "drawdown_detail": finding.detail}
# The original geometry set entry at 0.93x spot and the stop at 0.86x spot,
# i.e. the stop sat 7.53% BELOW THE ENTRY and the target 2:1 beyond that.
# Those relative distances are the strategy; the 0.93 was an entry-price
# assumption, and it is the part that was wrong. Expressed relative to the
# entry, the risk/reward is unchanged by the fix below.
STOP_DISTANCE_PERCENT = 7.53
REWARD_RISK_MULTIPLE = 2.0


def node_D_PULSE(state: AgentNetworkState) -> Dict[str, Any]:
    """Entry, stop and target for the setup.

    ENTRY IS THE PRICE WE CAN ACTUALLY TRANSACT AT.

    This node used to set the entry 7% below spot and return
    pullback_detected=True unconditionally. Nothing ever waited for that
    pullback and nothing ever checked whether it happened: I_ACCOUNTANT
    opened the position on the same tick, at a price the market had not
    traded. Every position therefore opened 7% in profit against reality,
    and that 7% flowed into entry price, stop distance, target distance,
    realised P&L and every cohort statistic built on them. It is the single
    largest reason the accumulated results flatter the strategy.

    paper_trading.py already models this correctly and is the reference: it
    writes TWO rows per candidate -- an IMMEDIATE fill at the evaluation
    price, and a LIMIT order at the pullback which waits and may EXPIRE
    unfilled. The live path was taking the LIMIT price with IMMEDIATE
    semantics, which is the one combination that cannot happen in a market:
    the better fill without the risk of not getting it.

    The live path is a market buy, so it books at spot. Confirming a genuine
    pullback needs intrabar data this pipeline does not have, so
    pullback_detected reports False rather than asserting something
    unobserved -- the field is kept for schema continuity, not as a claim.
    When Stage 4 executes for real, fill_accounting.reconstruct_fill replaces
    this estimate with the actual on-chain fill.

    Levels are rounded to SIGNIFICANT FIGURES, not decimal places. Fixed
    decimals collapse on this asset class: at five decimals a token at 1e-4
    rounds its entry and stop to the same number, and below ~5e-6 all three
    round to 0.0. A zero target makes evaluate_open_positions' exit test
    true for every possible price, so the position books an instant
    take-profit that never happened. A set that is not strictly ordered is
    refused via termination_reason, which router_G honours by routing past
    H_FUSE so I_ACCOUNTANT never calls save_active_position.
    """
    price = state.get("current_price")
    empty_levels = {
        "target_pullback_price": 0.0,
        "invalidation_level_price": 0.0,
        "target_exit_price": 0.0,
    }

    if not price or price <= 0:
        reason = "D_PULSE: Short-circuit. No usable current price -- cannot derive entry, stop or target."
        write_system_alert("WARN", "D_PULSE", reason)
        return {"pullback_detected": False, "termination_reason": reason, **empty_levels}

    # Entry at spot. The field name is kept because schema and dashboard
    # read it; it no longer holds a pullback.
    entry = round_significant(float(price))
    invalidation_floor = round_significant(entry * (1.0 - STOP_DISTANCE_PERCENT / 100.0))
    risk_per_unit = entry - invalidation_floor
    target_exit = round_significant(entry + (risk_per_unit * REWARD_RISK_MULTIPLE))

    if not (0 < invalidation_floor < entry < target_exit):
        reason = (f"D_PULSE: Short-circuit. Degenerate levels at price {price} -- "
                  f"stop={invalidation_floor}, entry={entry}, target={target_exit}.")
        write_system_alert("WARN", "D_PULSE", reason)
        return {"pullback_detected": False, "termination_reason": reason, **empty_levels}

    write_system_alert(
        "INFO", "D_PULSE",
        f"Market entry at {entry} (spot). Invalidation Floor={invalidation_floor}, "
        f"Target Exit={target_exit}.")
    return {
        # No pullback was observed -- this pipeline cannot observe one. The
        # entry is a market fill at spot, which is what actually happens.
        "pullback_detected": False,
        "target_pullback_price": entry,
        "invalidation_level_price": invalidation_floor,
        "target_exit_price": target_exit
    }


MIN_CAPITAL_PER_PARTICIPANT_USD = 15.0


def compute_capital_per_participant(volume_h1_usd: Optional[float],
                                      txns_h1_buys: Optional[int],
                                      txns_h1_sells: Optional[int],
                                      holders_added_per_hour: Optional[float]) -> Optional[float]:
    """Average USD committed per participation event in the last hour.

    BREADTH is the count of participation events -- transactions, plus any
    net new holders. DEPTH is the capital that actually moved. Dividing
    gives the average size of a participant's commitment, which is the one
    number that separates "a swarm of dust trades" from "real money".

    Returns None when there is nothing to divide by, so callers can tell
    "no participation data" from "participation was tiny". Those must not
    collapse: a token with no data is unknown, a token with $2 average
    trades is a bot farm, and treating the first as the second (or vice
    versa) is exactly the kind of silent miscategorisation this project
    keeps having to dig out.
    """
    if volume_h1_usd is None or volume_h1_usd <= 0:
        return None
    events = 0.0
    if txns_h1_buys:
        events += float(txns_h1_buys)
    if txns_h1_sells:
        events += float(txns_h1_sells)
    if holders_added_per_hour and holders_added_per_hour > 0:
        events += float(holders_added_per_hour)
    if events <= 0:
        return None
    return round(volume_h1_usd / events, 4)


def node_E_BREADTH(state: AgentNetworkState) -> Dict[str, Any]:
    """Breadth-vs-depth gate. Rejects a swarm of tiny participants moving
    almost no real capital -- the documented pump-and-dump/bot signature.

    THIS IS NOT A HYPE GATE. It used to be (node_E_SIGNAL), comparing a
    social-volume score against on-chain flow. That score was never real:
    no free data source covers a memecoin minted an hour ago, and the
    placeholder meant the gate passed everything while looking alive.
    Rather than wire a "social" feed that returns 0 for ~95% of tokens --
    which would have made the gate pass everything just as reliably, but
    with the logs now claiming the input was genuine -- the numerator was
    replaced with something we can actually measure.

    What it measures now is participation breadth against committed
    capital. Two 2026 papers (MemeTrans; Catching the Rug) predicted
    memecoin outcomes using only on-chain market-activity features --
    transaction counts, unique buyers, holder numbers -- with no social
    data at all, and ranked those features highest. So this is not a
    consolation prize for lacking a Twitter feed.

    UNVALIDATED: the threshold below is a judgment call, not a fitted
    parameter. Nothing in this project has yet been backtested. Stage 2
    paper trading is what would tell you whether this gate earns its
    place; until then treat it as a plausible heuristic, not a proven one.
    """
    capital_per_participant = compute_capital_per_participant(
        state.get("volume_h1_usd"),
        state.get("txns_h1_buys"),
        state.get("txns_h1_sells"),
        state.get("holders_added_per_hour"),
    )

    if capital_per_participant is None:
        # No participation data. Fail OPEN here, unlike the liquidity and
        # sizing gates: those decide how much real money to commit, so
        # unknown must mean stop. This one only filters setup quality, and
        # a provider that doesn't supply transaction counts (GMGN) would
        # otherwise reject every token it ever sees.
        write_system_alert(
            "INFO", "E_BREADTH",
            f"No participation data for ${state['token_symbol']} -- breadth gate skipped, not failed."
        )
        return {"is_breadth_healthy": True, "capital_per_participant_usd": None}

    if capital_per_participant < MIN_CAPITAL_PER_PARTICIPANT_USD:
        reason = (f"E_BREADTH: Short-circuit. Average participant committed only "
                  f"${capital_per_participant} (floor ${MIN_CAPITAL_PER_PARTICIPANT_USD}) -- "
                  f"broad participation with negligible capital behind it.")
        write_system_alert("WARN", "E_BREADTH", reason)
        return {"is_breadth_healthy": False,
                "capital_per_participant_usd": capital_per_participant,
                "termination_reason": reason}

    return {"is_breadth_healthy": True, "capital_per_participant_usd": capital_per_participant}


def node_F_ATLAS(state: AgentNetworkState) -> Dict[str, Any]:
    # The ceiling comes from holder_concentration, which is also where the
    # thing being thresholded is DEFINED. They used to live in different
    # files: the number 30.0 was a literal here while three providers
    # computed three incompatible quantities to compare against it (raw
    # chain including the LP pool, RugCheck's wallet figure, and GMGN's).
    # A threshold is meaningless apart from its definition, so they are now
    # one import away from each other.
    max_concentration = holder_concentration.TOP10_CONCENTRATION_CEILING_PERCENT

    # FAIL CLOSED on unmeasured concentration. free_market_data's own
    # fetch_rugcheck_report() docstring spells out why: a token RugCheck has
    # no record of returns None, and treating that as 0% would read as
    # "perfectly distributed, safe". It correctly returns None -- and the
    # snapshot then coerced it back to 0.0, producing exactly the outcome
    # that docstring says must be avoided. A token where one wallet holds
    # 82% of supply looks identical to a token nobody could measure.
    if state.get("holder_data_missing"):
        reason = ("F_ATLAS: Short-circuit. Holder-concentration data unavailable -- "
                  "refusing rather than treating an unmeasured token as well distributed.")
        write_system_alert("WARN", "F_ATLAS", reason)
        return {"holder_data_fresh": False, "termination_reason": reason}

    current_concentration = state.get("top_10_holder_percentage", 0.0)
    if current_concentration > max_concentration:
        reason = f"F_ATLAS: Short-circuit. Top 10 wallets hold {current_concentration}%, violating the {max_concentration}% ceiling."
        write_system_alert("WARN", "F_ATLAS", reason)
        return {"holder_data_fresh": False, "termination_reason": reason}
    return {"holder_data_fresh": True}
def node_G_ANCHOR(state: AgentNetworkState) -> Dict[str, Any]:
    # Rule validation: Price impact protection threshold
    max_slippage = 2.5

    # FAIL CLOSED on unmeasured slippage, for the same reason the depth check
    # below refuses an absent depth figure. fetch_price_impact_pct() returns
    # None on ANY Jupiter failure -- and Jupiter's free tier rate-limits
    # aggressively, so this is the common case, not the rare one. Coerced to
    # 0.0 it reads as "zero slippage, best possible token" and this cap
    # silently stops functioning across the whole pipeline.
    if state.get("slippage_data_missing"):
        reason = ("G_ANCHOR: Short-circuit. Slippage/price-impact could not be measured -- "
                  "refusing rather than sizing a position against an unknown execution cost.")
        write_system_alert("WARN", "G_ANCHOR", reason)
        return {"termination_reason": reason}

    slippage = state.get("estimated_slippage_percent", 0.0)
    if slippage > max_slippage:
        reason = f"G_ANCHOR: Short-circuit. Expected execution slippage ({slippage}%) violates the {max_slippage}% cap."
        write_system_alert("WARN", "G_ANCHOR", reason)
        return {"termination_reason": reason}
    # Size against TRADEABLE DEPTH, not total value locked. This was the
    # bug: pool_liquidity_usd is both sides of the pool summed, so sizing at
    # "1% of liquidity" was really taking ~2% of the side being traded into,
    # and eating roughly double the slippage this gate assumes it capped.
    # No default here on purpose -- an absent depth figure must not silently
    # become a 50000 assumption that sizes a real position.
    depth = state.get("tradeable_depth_usd") or 0.0
    if depth <= 0:
        reason = "G_ANCHOR: Short-circuit. No tradeable-depth figure available, cannot size a position safely."
        write_system_alert("WARN", "G_ANCHOR", reason)
        return {"termination_reason": reason}

    safe_size = round(min(depth * 0.01, 1000.0), 2)
    return {"max_safe_position_usd": safe_size}
def node_H_FUSE(state: AgentNetworkState) -> Dict[str, Any]:
    brief = f"Approved setup for ${state['token_symbol']}. Position Size Cap: ${state['max_safe_position_usd']}. Target Entry Trigger: {state['target_pullback_price']}."
    write_system_alert("INFO", "H_FUSE", "Unified trade signal brief compiled successfully.")
    return {"final_briefing_compiled": brief}
def node_I_ACCOUNTANT(state: AgentNetworkState) -> Dict[str, Any]:
    if state.get("termination_reason"):
        write_system_alert("INFO", "I_ACCOUNTANT", "Session aborted early. Skipping wallet transaction ledger insertions.")
        return {"position_logged": False}

    settings = get_app_settings()

    # FAIL CLOSED on unreadable settings. This is a money gate: run_status
    # (the pause / kill-switch / watchdog state) and the capital cap both
    # live in app_settings, so no settings row means BOTH are unknown.
    #
    # The previous `if settings and ...` skipped every check when settings
    # were None, which opened positions while PAUSED. That was reachable,
    # not theoretical: pause_trading() deliberately clears the settings
    # cache, so a DB blip on the very next tick returns None -- and the
    # operator's pause silently stopped applying at exactly the moment they
    # had reached for the brake.
    #
    # Refusing an entry costs one missed opportunity. Opening one during an
    # unreadable pause costs real money against an operator's explicit
    # instruction. Those are not symmetric.
    if not settings:
        write_system_alert(
            "CRITICAL", "I_ACCOUNTANT",
            f"Cannot read run state or capital limits -- refusing entry for "
            f"${state['token_symbol']}. Entries stay blocked until settings are readable."
        )
        return {"position_logged": False}

    if settings.get("run_status") != "RUNNING":
        write_system_alert(
            "WARN", "I_ACCOUNTANT",
            f"Trading is paused ({settings.get('run_status')}). Skipping entry for ${state['token_symbol']}."
        )
        return {"position_logged": False}

    # NULL here legitimately means "no cap configured", which is why this
    # stays an `is not None` test rather than a truthiness one.
    if settings.get("max_total_capital_usd") is not None:
        cap = float(settings["max_total_capital_usd"])
        position_size = float(state.get("max_safe_position_usd", 0.0))
        try:
            deployed = get_total_deployed_capital()
        except CapitalReadError as e:
            write_system_alert(
                "CRITICAL", "I_ACCOUNTANT",
                f"Cannot read deployed capital ({e}) -- refusing entry for "
                f"${state['token_symbol']} rather than risk opening over the cap."
            )
            return {"position_logged": False}
        if deployed + position_size > cap:
            write_system_alert(
                "WARN", "I_ACCOUNTANT",
                f"Capital allocation limit reached (${round(deployed, 2)} deployed of ${cap} cap). "
                f"Skipping entry for ${state['token_symbol']}."
            )
            return {"position_logged": False}

    # position_logged gates Stage 3 execution (see main.maybe_execute_via_signer),
    # so it must reflect whether a row was ACTUALLY written -- not merely that
    # we tried. Reporting True on the duplicate-guard path would ask the signer
    # to buy a token we already hold, every tick, which is precisely the
    # duplicate-trade bug the guard exists to prevent.
    if not save_active_position(state):
        write_system_alert(
            "WARN", "I_ACCOUNTANT",
            f"No position row written for ${state['token_symbol']} (already open, or the "
            f"write failed) -- reporting not-logged so nothing downstream executes."
        )
        return {"position_logged": False}

    write_system_alert("INFO", "I_ACCOUNTANT", f"Active ledger holding metrics appended for ${state['token_symbol']}.")
    return {"position_logged": True}
def node_Z_CLOSER(state: AgentNetworkState) -> Dict[str, Any]:
    save_trading_session(state)
    if state.get("termination_reason"):

        write_system_alert("INFO", "Z_CLOSER", "Session pipeline tracking metrics written to historical tables.")
    else:
        write_system_alert("CRITICAL", "Z_CLOSER", f"Session #12H cleared and logged for ${state['token_symbol']}. Dispatched.")
    return {"session_closed": True}


# 4. Routing Flow Map Controllers
def router_B(state: AgentNetworkState) -> str:
    return "I_ACCOUNTANT" if not state.get("is_liquidity_safe") else "C_VECTOR"
def router_E(state: AgentNetworkState) -> str:
    return "I_ACCOUNTANT" if not state.get("is_breadth_healthy") else "F_ATLAS"
def router_F(state: AgentNetworkState) -> str:
    return "I_ACCOUNTANT" if not state.get("holder_data_fresh") else "G_ANCHOR"
def router_G(state: AgentNetworkState) -> str:
    return "I_ACCOUNTANT" if state.get("termination_reason") else "H_FUSE"
# 5. Graph Wiring Compilation

builder = StateGraph(AgentNetworkState)
builder.add_node("A_ORBIT", with_watchdog("A_ORBIT")(node_A_ORBIT))
builder.add_node("B_SENTINEL", with_watchdog("B_SENTINEL")(node_B_SENTINEL))
builder.add_node("C_VECTOR", with_watchdog("C_VECTOR")(node_C_VECTOR))
builder.add_node("D_PULSE", with_watchdog("D_PULSE")(node_D_PULSE))
builder.add_node("E_BREADTH", with_watchdog("E_BREADTH")(node_E_BREADTH))
builder.add_node("F_ATLAS", with_watchdog("F_ATLAS")(node_F_ATLAS))
builder.add_node("G_ANCHOR", with_watchdog("G_ANCHOR")(node_G_ANCHOR))
builder.add_node("H_FUSE", with_watchdog("H_FUSE")(node_H_FUSE))
builder.add_node("I_ACCOUNTANT", with_watchdog("I_ACCOUNTANT")(node_I_ACCOUNTANT))
builder.add_node("Z_CLOSER", with_watchdog("Z_CLOSER")(node_Z_CLOSER))

builder.add_edge(START, "A_ORBIT")
builder.add_edge("A_ORBIT", "B_SENTINEL")
builder.add_conditional_edges("B_SENTINEL", router_B)
builder.add_edge("C_VECTOR", "D_PULSE")
builder.add_edge("D_PULSE", "E_BREADTH")
builder.add_conditional_edges("E_BREADTH", router_E)
builder.add_conditional_edges("F_ATLAS", router_F)

builder.add_conditional_edges("G_ANCHOR", router_G)
builder.add_edge("H_FUSE", "I_ACCOUNTANT")
builder.add_edge("I_ACCOUNTANT", "Z_CLOSER")
builder.add_edge("Z_CLOSER", END)
agent_network = builder.compile()


