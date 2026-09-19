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
    finally:
        conn.close()
    return s
