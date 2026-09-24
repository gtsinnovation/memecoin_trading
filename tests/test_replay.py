"""Exit-policy replay: the ordering must matter, and the rules must be shared.

The defect this guards is not a crash. A replay that re-derives the exit rules
instead of importing them tests a strategy that does not exist -- and it would
diverge from production silently, the first time either side changed, with the
divergence looking like a result.
"""
from tests.harness import Suite

import paper_trading as pt
import replay_exits as rx


def run() -> Suite:
    s = Suite("exit replay")

    # --- the rules are SHARED, not reimplemented ---------------------------
    # The identity checks below are necessary and NOT sufficient: they prove
    # the names are bound, not that replay_one calls them. A reimplementation
    # that ignores the import passes both and still tests a strategy that does
    # not exist -- which is exactly what happened when this was mutation-
    # tested, so the BEHAVIOURAL check underneath is the one that bites.
    s.check_true("replay binds the production exit rule",
                 rx.pt.decide_exit is pt.decide_exit)
    s.check_true("and the production cost model",
                 rx.pt.net_pnl_percent is pt.net_pnl_percent)

    # A gap THROUGH the stop fills at the market, not at the stop. That
    # asymmetry is the hardest-won rule in this codebase: booking stop-outs at
    # the stop capped every loss at the stop distance, so a rug could not be
    # represented at all -- in the direction that flatters the strategy. Any
    # replay that re-derives its own exits will get this wrong and report a
    # tidy -7.53 where the truth is near total loss.
    rug = [(1.0, 0.01)]
    out = rx.replay_one(rug, 1.0, 7.53, 15.06, 0.0)
    s.check("a gap through the stop is still a stop-out", out[0], "STOPPED_OUT")
    s.check_true("and books near-total loss, not the stop distance",
                 out[1] < -90.0)
    # Belt and braces: the number must equal what production would book.
    _, _, expected = pt.net_pnl_percent(1.0, 0.01, 0.0)
    s.check("the replayed P&L equals the production P&L exactly",
            out[1], expected)

    # --- ORDER is what the whole exercise is for ---------------------------
    # Identical extremes, opposite sequences. min/max cannot tell these apart;
    # a replay that could not either would be pointless.
    basis = 1.0
    down_then_up = [(1.0, 0.90), (2.0, 1.20)]   # through the stop, then up
    up_then_down = [(1.0, 1.20), (2.0, 0.90)]   # through the target, then down

    a = rx.replay_one(down_then_up, basis, 7.53, 15.06, 0.0)
    b = rx.replay_one(up_then_down, basis, 7.53, 15.06, 0.0)
    s.check("falling first is a stop-out", a[0], "STOPPED_OUT")
    s.check("rising first is a target hit", b[0], "TARGET_HIT")
    s.check_true("and the two orderings give different P&L", a[1] != b[1])
    s.check_true("the stop-out loses", a[1] < 0)
    s.check_true("the target hit wins", b[1] > 0)

    # --- the barriers actually move ----------------------------------------
    # The same path under a tighter target must exit EARLIER and differently.
    path = [(1.0, 1.04), (2.0, 1.09), (3.0, 1.20)]
    tight = rx.replay_one(path, basis, 7.53, 3.0, 0.0)
    wide = rx.replay_one(path, basis, 7.53, 15.06, 0.0)
    s.check("a 3% target is hit", tight[0], "TARGET_HIT")
    s.check("a 15% target is also hit on this path", wide[0], "TARGET_HIT")
    s.check_true("but the wider target books more", wide[1] > tight[1])

    # A path that reaches neither barrier must not be scored as a win or a
    # loss at some invented level -- it exits where it actually was.
    flat = [(1.0, 1.01), (2.0, 1.02)]
    out = rx.replay_one(flat, basis, 7.53, 15.06, 0.0)
    s.check("neither barrier reached is reported as such", out[0], "OPEN_AT_END")
    _, _, expected = pt.net_pnl_percent(basis, 1.02, 0.0)
    s.check("and is priced at the LAST observed price", out[1], expected)

    # --- degenerate levels are refused, not silently traded ----------------
    s.check("a 100% stop puts the floor at zero and is refused",
            rx.replay_one(flat, basis, 100.0, 15.0, 0.0), None)
    s.check("a zero-width target is refused",
            rx.replay_one(flat, basis, 7.53, 0.0, 0.0), None)

    # --- costs are charged, and unmeasured slippage is not free ------------
    measured = rx.replay_one(up_then_down, basis, 7.53, 15.06, 0.0)
    unmeasured = rx.replay_one(up_then_down, basis, 7.53, 15.06, None)
    s.check_true("an unmeasured-slippage trade nets less than a measured zero",
                 unmeasured[1] < measured[1])

    # --- the grid includes what is actually running ------------------------
    # Without this the surface has no anchor and every cell is uncomparable.
    s.check_true("the live stop distance is one of the replayed stops",
                 any(abs(x - 7.53) < 0.01 for x in rx.STOPS))
    s.check_true("the live target distance is one of the replayed targets",
                 any(abs(x - 15.06) < 0.01 for x in rx.TARGETS))

    return s
