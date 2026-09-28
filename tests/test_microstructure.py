"""Microstructure + execution-rail suite.

Guards the two behaviours these modules exist for:
  1. an unmeasurable input never reads as a permissive value;
  2. directional signals degrade execution, they do not veto -- except the
     single 5-minute-drawdown hard stop.

Neither module touches the database or the network, so this suite needs no
fixtures and runs standalone.
"""
from tests.harness import Suite

import market_microstructure as ms
import execution_rails as rails
from market_microstructure import Severity


def run() -> Suite:
    s = Suite("microstructure + rails")

    # --- 5m drawdown: the one directional veto ---
    f = ms.check_drawdown(90.0, 100.0)
    s.check_true("10% 5m drawdown must veto", f.severity is Severity.VETO)
    s.check_true("a measured drawdown must be flagged measured", f.measured)
    # Exactly on the threshold: this is the case binary floating point loses.
    s.check_true("a drawdown of exactly 10% must veto, not round to degraded",
                 ms.check_drawdown(90.0, 100.0).severity is Severity.VETO)
    s.check_true("a drawdown of exactly 5% must degrade",
                 ms.check_drawdown(95.0, 100.0).severity is Severity.DEGRADE)
    s.check_true("6% drawdown degrades execution, never vetoes", ms.check_drawdown(94.0, 100.0).severity is Severity.DEGRADE)
    s.check_true("a rising price is informational", ms.check_drawdown(101.0, 100.0).severity is Severity.INFO)

    # Absent history must not read as calm, and must not veto either.
    f = ms.check_drawdown(None, None)
    s.check_true("missing 5m history degrades execution and is flagged unmeasured", f.severity is Severity.DEGRADE and not f.measured)
    s.check_true("a zero prior price is unmeasurable, not a 100% gain", ms.check_drawdown(100.0, 0.0).severity is Severity.DEGRADE)

    # price_5m is a PRICE, not a percentage -- the arithmetic must live here.
    s.check_true("drawdown must be computed from two prices, not read as a change", abs(ms.drawdown_5m_percent(83.5, 100.0) + 16.5) < 1e-9)

    # --- net flow: every window must agree before calling sustained selling ---
    all_neg = {"5m": {"buy": 1.0, "sell": 5.0}, "1h": {"buy": 2.0, "sell": 9.0},
               "24h": {"buy": 10.0, "sell": 40.0}}
    s.check_true("sell pressure in every window degrades execution", ms.check_net_flow(all_neg).severity is Severity.DEGRADE)
    mixed = {"5m": {"buy": 9.0, "sell": 1.0}, "1h": {"buy": 2.0, "sell": 9.0},
             "24h": {"buy": 10.0, "sell": 40.0}}
    s.check_true("one positive window means selling is not sustained", ms.check_net_flow(mixed).severity is Severity.INFO)
    f = ms.check_net_flow({})
    s.check_true("no flow data degrades execution and is flagged unmeasured", f.severity is Severity.DEGRADE and not f.measured)
    f = ms.check_net_flow({"24h": {"buy": 1.0, "sell": 9.0}})
    s.check_true("a single window is not enough to call a trend", f.severity is Severity.INFO)
    # A window missing one side must be skipped, not counted as balanced.
    f = ms.check_net_flow({"5m": {"buy": 1.0, "sell": 5.0}, "1h": {"buy": None, "sell": 9.0}})
    s.check_true("a half-present window is skipped, leaving too few to call a trend", f.severity is Severity.INFO)

    # --- turnover: banded, and a high ratio is not automatically wash ---
    s.check_true("4x turnover on a small pool is inside the band", ms.check_turnover(200_000.0, 50_000.0, 1_000).severity is Severity.INFO)
    s.check_true("turnover below the floor is a dead market", ms.check_turnover(1_000.0, 50_000.0, 1_000).severity is Severity.VETO)
    s.check_true("100x turnover with 1,600 holders is a stampede, not wash trading", ms.check_turnover(5_000_000.0, 50_000.0, 1_600).severity is Severity.DEGRADE)
    s.check_true("100x turnover on 12 holders is wash trading", ms.check_turnover(5_000_000.0, 50_000.0, 12).severity is Severity.VETO)
    f = ms.check_turnover(5_000_000.0, 50_000.0, None)
    s.check_true("high turnover with no holder count cannot be adjudicated -- refuse", f.severity is Severity.VETO and not f.measured)
    f = ms.check_turnover(None, 50_000.0, 500)
    s.check_true("absent volume must refuse, not read as zero turnover", f.severity is Severity.VETO and not f.measured)
    s.check_true("zero liquidity must refuse rather than divide by zero", ms.check_turnover(100.0, 0.0, 500).severity is Severity.VETO)
    # Band boundaries must move with pool size.
    s.check_true("0.075x on a $2M pool is inside the deep-pool band", ms.check_turnover(150_000.0, 2_000_000.0, 5_000).severity is Severity.INFO)
    s.check_true("the same 0.075x ratio is below the floor for a mid-size pool", ms.check_turnover(37_500.0, 500_000.0, 5_000).severity is Severity.VETO)

    # --- shell ---
    s.check_true("$2M market cap on 40 holders is a shell", ms.check_shell(2_000_000.0, 40).severity is Severity.VETO)
    s.check_true("a real holder base behind a large cap is fine", ms.check_shell(2_000_000.0, 5_000).severity is Severity.INFO)
    s.check_true("absent market cap is unmeasured, not a shell verdict", not ms.check_shell(None, 40).measured)

    # --- dynamic slippage ---
    # Deep pool, zero tax, but violently moving: the volatility term is the
    # whole point. Without it this token computes ~2% and the order fails.
    calm = ms.dynamic_slippage_percent(0.0, 0.0)
    violent = ms.dynamic_slippage_percent(0.0, 16.5)
    s.check_true("a still, zero-impact market computes the 2% base", abs(calm - 2.0) < 1e-9)
    s.check_true("a 16.5% 5m move must widen tolerance past 10%", violent > 10.0)
    s.check_true("volatility must widen slippage, never narrow it", violent > calm)
    # Both directions need headroom.
    s.check_true("the volatility term takes the absolute move, so both directions add", abs(ms.dynamic_slippage_percent(0.0, -16.5) - violent) < 1e-9)
    s.check_true("unknown price impact must return None, never a guessed tolerance", ms.dynamic_slippage_percent(None, 5.0) is None)
    s.check_true("a listing under an hour old earns extra headroom", ms.dynamic_slippage_percent(0.0, 0.0, token_age_hours=0.5) > calm)
    s.check_true("slippage is capped rather than allowed to run away", ms.dynamic_slippage_percent(50.0, 90.0) <= ms.SLIPPAGE_CEILING_PERCENT)
    s.check_true("slippage never falls below the floor", ms.dynamic_slippage_percent(0.0, 0.0, tax_percent=0.0) >= ms.SLIPPAGE_FLOOR_PERCENT)

    # --- verdict aggregation ---
    v = ms.evaluate({"price_usd": 90.0, "price_5m_ago": 100.0, "liquidity_usd": 50_000.0,
                     "volume_24h_usd": 200_000.0, "holder_count": 1_000,
                     "market_cap_usd": 500_000.0})
    s.check_true("a vetoing finding blocks the verdict", v.blocked and "5m" in (v.reason() or ""))
    v = ms.evaluate({"price_usd": 96.0, "price_5m_ago": 100.0, "liquidity_usd": 50_000.0,
                     "volume_24h_usd": 200_000.0, "holder_count": 1_000,
                     "market_cap_usd": 500_000.0})
    s.check_true("a degrading finding forces a limit order without blocking", not v.blocked and v.must_use_limit_order)
    # An empty snapshot must never come back clean.
    s.check_true("an empty snapshot must not evaluate as tradeable", ms.evaluate({}).blocked)

    # --- execution rails ---
    ok = dict(mode="LIVE", run_status="RUNNING", requested_usd=5.0,
              live_max_position_usd=5.0, max_position_usd=50.0,
              orders_sent_today=0, max_orders_per_day=3,
              sol_lamports=20_000_000, quote_balance_usd=100.0)
    s.check_true("a fully satisfied rail set approves", rails.check_entry_rails(**ok).ok)

    for field, bad, label in [
        ("mode", "PAPER", "PAPER mode must refuse a live entry"),
        ("mode", None, "unreadable mode must refuse"),
        ("run_status", "PAUSED_KILL_SWITCH", "a tripped kill switch must refuse"),
        ("run_status", None, "unreadable run status must refuse"),
        ("requested_usd", None, "missing size must refuse"),
        ("requested_usd", 0.0, "a zero size must refuse"),
        ("live_max_position_usd", None, "an unreadable live ceiling must refuse"),
        ("max_orders_per_day", None, "an unreadable daily cap must refuse, not mean unlimited"),
        ("max_orders_per_day", 0, "a zero daily cap must refuse"),
        ("orders_sent_today", None, "an unreadable order count must refuse"),
        ("orders_sent_today", 3, "reaching the daily cap must refuse"),
        ("sol_lamports", None, "an unreadable SOL balance must refuse"),
        ("sol_lamports", 1_000, "SOL below the fee floor must refuse"),
        ("quote_balance_usd", None, "an unreadable quote balance must refuse"),
        ("quote_balance_usd", 1.0, "a balance below the size must refuse"),
    ]:
        s.check_true(label, not rails.check_entry_rails(**{**ok, field: bad}).ok)

    # NaN: every comparison against it is False, so a NaN ceiling, count or
    # balance used to read as the most permissive value possible.
    nan, inf = float("nan"), float("inf")
    for field, bad in [("requested_usd", nan), ("requested_usd", inf),
                       ("live_max_position_usd", nan), ("max_position_usd", nan),
                       ("max_orders_per_day", nan), ("orders_sent_today", nan),
                       ("sol_lamports", nan), ("quote_balance_usd", nan)]:
        s.check_true(f"a {bad} {field} must refuse",
                     not rails.check_entry_rails(**{**ok, field: bad}).ok)
    s.check_true("a NaN ceiling collapses to zero, not to unlimited",
                 rails.position_ceiling_usd(nan, 50.0) == 0.0)
    s.check_true("the devnet self-transfer may skip the quote balance",
                 rails.check_entry_rails(**{**ok, "quote_balance_usd": None,
                                             "quote_balance_required": False}).ok)
    s.check_true("but the default still requires it",
                 not rails.check_entry_rails(**{**ok, "quote_balance_usd": None}).ok)

    # The source constant must bound a database row that tries to exceed it.
    s.check_true("the hard-coded ceiling must cap oversized config values",
                 rails.position_ceiling_usd(10_000.0, 10_000.0) == rails.ABSOLUTE_MAX_POSITION_USD)
    s.check_true("the smallest configured ceiling wins when it is under the constant", rails.position_ceiling_usd(5.0, 50.0) == 5.0)
    s.check_true("a missing ceiling must collapse to zero, which refuses", rails.position_ceiling_usd(None, 50.0) == 0.0)
    over = rails.check_entry_rails(**{**ok, "requested_usd": 5_000.0,
                                      "live_max_position_usd": 10_000.0,
                                      "max_position_usd": 10_000.0,
                                      "quote_balance_usd": 99_999.0})
    s.check_true("a size above the hard ceiling must refuse even with config raised and funds present", not over.ok)

    # --- re-quote before signing ---
    s.check_true("an improved route signs", rails.requote_still_acceptable(1.0, 2.0).ok)
    s.check_true("a degraded route must refuse", not rails.requote_still_acceptable(9.0, 2.0).ok)
    s.check_true("a re-quote with no impact figure must refuse rather than sign blind", not rails.requote_still_acceptable(None, 2.0).ok)
    s.check_true("an unreadable tolerance must refuse", not rails.requote_still_acceptable(1.0, None).ok)
    s.check_true("a NaN impact must refuse", not rails.requote_still_acceptable(float("nan"), 2.0).ok)
    s.check_true("a NaN tolerance must refuse", not rails.requote_still_acceptable(1.0, float("nan")).ok)

    return s
