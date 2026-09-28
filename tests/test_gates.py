# tests/test_gates.py
"""Money gates must FAIL CLOSED. Guards audit fixes 1-6.

Every gate here previously passed on data it could not measure, because an
unmeasurable value was coerced to 0.0 and 0.0 is the most permissive input a
`>` comparison can receive. The tests below assert the distinction that fix
restored: "measured zero" and "never measured" must produce opposite outcomes.
"""
import sys

from .harness import Suite, make_address


def run(psycopg2, engine, dsn) -> Suite:
    s = Suite("money gates fail closed")
    TOK = make_address(1)

    def state(**kw):
        d = {"token_symbol": "T", "token_address": TOK, "current_price": 1.0,
             "top_10_holder_percentage": 0.0, "estimated_slippage_percent": 0.0,
             "tradeable_depth_usd": 50000.0, "max_safe_position_usd": 500.0,
             "holder_data_missing": False, "slippage_data_missing": False}
        d.update(kw)
        return d

    print("\n[F_ATLAS] unmeasured holder concentration")
    r = engine.node_F_ATLAS(state(holder_data_missing=True))
    s.check("refuses when concentration was never measured", "termination_reason" in r, True)
    s.check("and reports data not fresh", r.get("holder_data_fresh"), False)
    # The distinction that matters: a real 0% must NOT be refused.
    r = engine.node_F_ATLAS(state(top_10_holder_percentage=0.0))
    s.check("a MEASURED 0% still passes", r.get("holder_data_fresh"), True)
    r = engine.node_F_ATLAS(state(top_10_holder_percentage=82.0))
    s.check("a real 82% concentration is refused", "termination_reason" in r, True)

    print("\n[G_ANCHOR] unmeasured slippage")
    r = engine.node_G_ANCHOR(state(slippage_data_missing=True))
    s.check("refuses when slippage was never measured", "termination_reason" in r, True)
    s.check("and sizes no position", r.get("max_safe_position_usd"), None)
    r = engine.node_G_ANCHOR(state(estimated_slippage_percent=0.0))
    s.check("a MEASURED 0% slippage still passes", r.get("max_safe_position_usd"), 500.0)
    r = engine.node_G_ANCHOR(state(estimated_slippage_percent=9.0))
    s.check("9% slippage is refused", "termination_reason" in r, True)
    r = engine.node_G_ANCHOR(state(tradeable_depth_usd=0.0))
    s.check("zero tradeable depth is refused", "termination_reason" in r, True)

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE active_positions;")
            cur.execute("UPDATE app_settings SET run_status='RUNNING', "
                        "run_status_reason=NULL, max_total_capital_usd=1000 WHERE id=1;")
        engine._settings_cache["data"] = None

        print("\n[I_ACCOUNTANT] capital that cannot be read")
        real = engine.get_total_deployed_capital
        engine.get_total_deployed_capital = lambda: (_ for _ in ()).throw(
            engine.CapitalReadError("simulated outage"))
        try:
            r = engine.node_I_ACCOUNTANT(state())
        finally:
            engine.get_total_deployed_capital = real
        s.check("refuses when deployed capital is unreadable", r.get("position_logged"), False)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM active_positions;")
            s.check("and opens nothing", int(cur.fetchone()[0]), 0)

        print("\n[I_ACCOUNTANT] position_logged must reflect reality")
        engine._settings_cache["data"] = None
        r = engine.node_I_ACCOUNTANT(state())
        s.check("a real entry reports logged", r.get("position_logged"), True)
        # This one gates Stage 3 execution. Reporting True here asked the
        # signer to buy a token already held, every tick.
        r = engine.node_I_ACCOUNTANT(state())
        s.check("a duplicate reports NOT logged", r.get("position_logged"), False)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM active_positions WHERE token_address=%s;", (TOK,))
            s.check("still exactly one open position", int(cur.fetchone()[0]), 1)

        print("\n[I_ACCOUNTANT] pause must hold even when settings are unreadable")
        engine.pause_trading()
        engine._settings_cache["data"] = None
        real_settings = engine.get_app_settings
        engine.get_app_settings = lambda *a, **k: None
        try:
            r = engine.node_I_ACCOUNTANT(state(token_address=make_address(2)))
        finally:
            engine.get_app_settings = real_settings
        s.check("unreadable settings while paused -> refuses", r.get("position_logged"), False)
        engine.resume_trading()

        print("\n[KILL SWITCH] a zero threshold is 'not configured', not 'trip now'")
        with conn.cursor() as cur:
            cur.execute("TRUNCATE closed_positions;")
            cur.execute("UPDATE app_settings SET run_status='RUNNING', run_status_reason=NULL, "
                        "kill_switch_max_consecutive_losses=0, kill_switch_max_loss_usd=NULL, "
                        "kill_switch_max_drawdown_pct=NULL, run_duration_minutes=NULL WHERE id=1;")
        engine._settings_cache["data"] = None
        engine.check_kill_switch()
        with conn.cursor() as cur:
            cur.execute("SELECT run_status FROM app_settings WHERE id=1;")
            s.check("streak=0 with no trades does not pause", cur.fetchone()[0], "RUNNING")
        with conn.cursor() as cur:
            cur.execute("UPDATE app_settings SET kill_switch_max_consecutive_losses=2 WHERE id=1;")
            for _ in range(2):
                cur.execute(
                    "INSERT INTO closed_positions (token_symbol, token_address, allocated_usd, "
                    "entry_trigger, exit_price, realized_pnl_usd, realized_pnl_percent, "
                    "exit_reason, opened_at) VALUES ('L',%s,100,1,0.9,-10,-10,'STOPPED_OUT',"
                    "CURRENT_TIMESTAMP);", (TOK,))
        engine._settings_cache["data"] = None
        engine.check_kill_switch()
        with conn.cursor() as cur:
            cur.execute("SELECT run_status FROM app_settings WHERE id=1;")
            s.check("streak=2 with 2 real losses does pause", cur.fetchone()[0], "PAUSED_KILL_SWITCH")
        with conn.cursor() as cur:
            cur.execute("UPDATE app_settings SET run_status='RUNNING', run_status_reason=NULL, "
                        "kill_switch_max_consecutive_losses=NULL WHERE id=1;")
        engine._settings_cache["data"] = None

        print("\n[SETTINGS] a pause during an in-flight read is never cached over")
        # The read that started BEFORE the pause must not write its RUNNING
        # row into the cache after it.
        rows = iter([{"run_status": "RUNNING"}, {"run_status": "PAUSED_MANUAL"}])

        class _Cur:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql):
                self.row = next(rows)
                if self.row["run_status"] == "RUNNING":
                    engine.invalidate_settings_cache()   # the pause lands mid-read
            def fetchone(self): return self.row

        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self, **k): return _Cur()
            def close(self): pass

        real_connect = engine.db_connect
        engine.db_connect = lambda: _Conn()
        try:
            engine.invalidate_settings_cache()
            got = engine.get_app_settings()
        finally:
            engine.db_connect = real_connect
        s.check("the caller sees the post-pause row", (got or {}).get("run_status"), "PAUSED_MANUAL")
        s.check("and the cache holds it, not the stale RUNNING",
                (engine._settings_cache["data"] or {}).get("run_status"), "PAUSED_MANUAL")
        engine.invalidate_settings_cache()

        print("\n[LEDGER] live stop-outs fill at the market, net of costs")
        # The kill switch sums closed_positions. Booking a gap-down AT the
        # stop capped every live loss at the stop distance, so a rug read as
        # -7.5% and a streak of them could never reach a max-loss threshold.
        real_fetch = engine._fetch_position_prices
        GAP, NANT, OK, PEND = make_address(31), make_address(32), make_address(33), make_address(34)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE closed_positions; TRUNCATE active_positions;")
            for tok, slip, status in ((GAP, 1.0, "PAPER"), (NANT, None, "PAPER"),
                                      (OK, None, "EXECUTED"), (PEND, None, "PENDING_EXECUTION")):
                cur.execute(
                    "INSERT INTO active_positions (token_symbol, token_address, allocated_usd, "
                    "entry_trigger, target_exit_price, invalidation_level_price, "
                    "last_simulated_price, entry_slippage_percent, execution_status) "
                    "VALUES ('P', %s, 100, 1.0, 1.1506, 0.9247, 1.0, %s, %s);", (tok, slip, status))
        engine._fetch_position_prices = lambda addrs: {
            GAP: 0.30, NANT: float("nan"), OK: 0.95, PEND: 0.10}
        try:
            closed = engine.evaluate_open_positions()
        finally:
            engine._fetch_position_prices = real_fetch
        with conn.cursor() as cur:
            cur.execute("SELECT exit_price, realized_pnl_percent, gross_pnl_percent, cost_percent, "
                        "realized_pnl_usd FROM closed_positions WHERE token_address=%s;", (GAP,))
            row = cur.fetchone()
            s.check_true("a gap through the stop closes the position", row is not None)
            if row:
                s.check_true("the gap fills at the market (0.30), not the stop (0.9247)",
                             abs(float(row[0]) - 0.30) < 1e-9)
                s.check_true("gross P&L is the real -70%", abs(float(row[2]) + 70.0) < 1e-6)
                s.check_true("costs are charged: 2 x 0.25 fee + 2 x 1.0 slippage",
                             abs(float(row[3]) - 2.5) < 1e-9)
                s.check_true("realized (what the kill switch sums) is NET",
                             abs(float(row[1]) + 72.5) < 1e-6 and abs(float(row[4]) + 72.5) < 1e-6)
            cur.execute("SELECT last_simulated_price::text FROM active_positions WHERE token_address=%s;", (NANT,))
            nan_row = cur.fetchone()
            s.check_true("a NaN price leaves the position open and unmarked",
                         nan_row is not None and nan_row[0] != "NaN")
            cur.execute("SELECT count(*) FROM active_positions WHERE token_address=%s;", (OK,))
            s.check("a price between the barriers keeps the position open", cur.fetchone()[0], 1)
        s.check("only the barrier breach is reported closed", len(closed), 1)
        with conn.cursor() as cur:
            cur.execute("SELECT execution_status, last_simulated_price FROM active_positions "
                        "WHERE token_address=%s;", (PEND,))
            row = cur.fetchone()
        s.check_true("a PENDING_EXECUTION reservation is neither closed nor marked "
                     "(the signer has not confirmed it exists)",
                     row is not None and row[0] == "PENDING_EXECUTION" and float(row[1]) == 1.0)
        s.check_true("an unmeasured entry slippage is stored as NULL, not 0.0",
                     engine.entry_slippage_for({"slippage_data_missing": True,
                                                "estimated_slippage_percent": 0.0}) is None)
        s.check_true("an absent missing-flag is treated as unmeasured",
                     engine.entry_slippage_for({"estimated_slippage_percent": 0.4}) is None)
        s.check_true("a measured slippage is kept",
                     engine.entry_slippage_for({"slippage_data_missing": False,
                                                "estimated_slippage_percent": 0.4}) == 0.4)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE closed_positions; TRUNCATE active_positions;")
    finally:
        conn.close()
    return s
