"""Regression cover for the hardening pass.

Each assertion names a defect that shipped. None of them were crashes -- every
one produced a plausible number or a plausible-looking healthy state, which is
why they survived until an audit rather than until the next run.

Pure functions only: no database, no network, no container.
"""
from tests.harness import Suite

import paper_trading as pt
import execution_rails as rails


def run() -> Suite:
    s = Suite("hardening")

    # --- unmeasured slippage must never be free ---
    fee_only = round(pt.PAPER_FEE_PERCENT_PER_SIDE * 2.0, 4)
    s.check_true("a measured zero slippage costs fees only",
                 pt.total_cost_percent(0.0) == fee_only)
    s.check_true("UNMEASURED slippage costs more than fees alone",
                 pt.total_cost_percent(None) > fee_only)
    s.check_true("unmeasured is charged at the documented stand-in",
                 pt.total_cost_percent(None)
                 == round(fee_only + 2 * pt.PAPER_UNMEASURED_SLIPPAGE_PERCENT, 4))
    s.check_true("a measured 1% still costs less than an unmeasured one",
                 pt.total_cost_percent(1.0) < pt.total_cost_percent(None))
    s.check_true("negative slippage is charged as its magnitude",
                 pt.total_cost_percent(-1.0) == pt.total_cost_percent(1.0))
    # The bias this removes: unmeasured impact is commonest on thin tokens,
    # so charging it zero manufactured part of the cohort gap.
    s.check_true("net P&L on an unmeasured trade is worse than on a measured zero",
                 pt.net_pnl_percent(1.0, 1.1, None)[2] < pt.net_pnl_percent(1.0, 1.1, 0.0)[2])

    # --- the left tail must be representable ---
    # decide_exit is where the asymmetry lives: a take-profit is a limit sell
    # and fills AT the level; a stop triggers at the level and fills at the
    # market, which on this asset class is routinely far below.
    reason, exit_price = pt.decide_exit(0.01, 1.15, 0.925, 5)
    s.check("a gap through the stop is still a stop-out", reason, "STOPPED_OUT")
    s.check_true("a gap through the stop fills at the market, not the stop",
                 exit_price == 0.01)
    reason, exit_price = pt.decide_exit(0.90, 1.15, 0.925, 5)
    s.check_true("a normal stop-out fills at the stop", exit_price == 0.90)
    reason, exit_price = pt.decide_exit(1.40, 1.15, 0.925, 5)
    s.check("trading through the target is a target hit", reason, "TARGET_HIT")
    s.check_true("a take-profit fills AT the target, never better",
                 exit_price == 1.15)
    s.check_true("no barrier and no timeout means no exit",
                 pt.decide_exit(1.0, 1.15, 0.925, 5)[0] is None)
    s.check("an aged-out trade times out",
            pt.decide_exit(1.0, 1.15, 0.925, pt.MAX_HOLD_MINUTES + 1)[0], "TIMEOUT")
    s.check_true("a missing stop cannot trigger a stop-out",
                 pt.decide_exit(0.01, 1.15, None, 5)[0] is None)
    # The realised loss must actually reach the tail.
    s.check_true("a rug books near total loss, not the stop distance",
                 pt.net_pnl_percent(1.0, pt.decide_exit(0.01, 1.15, 0.925, 5)[1], 0.0)[0] < -90.0)

    # --- dropout classification ---
    s.check("an unpriceable token is classified as dropped",
            pt.classify_horizon_row(None, 1.0), "DROPPED_NO_PRICE")
    s.check("a zero basis is classified as dropped",
            pt.classify_horizon_row(1.0, 0.0), "DROPPED_NO_BASIS")
    s.check("a missing basis is classified as dropped",
            pt.classify_horizon_row(1.0, None), "DROPPED_NO_BASIS")
    s.check("a usable row is markable", pt.classify_horizon_row(1.0, 2.0), "MARKABLE")
    s.check_true("a price of zero is not treated as a valid mark",
                 pt.classify_horizon_row(None, 2.0) != "MARKABLE")

    # --- horizon dropout must be counted, not silent ---
    d = pt.horizon_dropout_summary({30: 5, pt.HORIZON_DROPPED_NO_PRICE: 4,
                                    pt.HORIZON_DROPPED_NO_BASIS: 1,
                                    pt.HORIZON_DUE_TOTAL: 10})
    s.check("half the sample dropping is reported as 50%", d["dropout_percent"], 50.0)
    s.check("unpriceable tokens are counted", d["dropped_no_price"], 4)
    s.check_true("a clean tick reports zero dropout, not None",
                 pt.horizon_dropout_summary({30: 10, pt.HORIZON_DUE_TOTAL: 10})["dropout_percent"] == 0.0)
    s.check_true("nothing due reports None rather than a fabricated 0%",
                 pt.horizon_dropout_summary({})["dropout_percent"] is None)

    # --- the signer's cap may not exceed the pipeline's hard ceiling ---
    s.check_true("the absolute ceiling is a source constant, not config",
                 isinstance(rails.ABSOLUTE_MAX_POSITION_USD, float))
    s.check_true("config above the ceiling is clamped to it",
                 rails.position_ceiling_usd(10_000.0, 10_000.0) == rails.ABSOLUTE_MAX_POSITION_USD)

    return s
