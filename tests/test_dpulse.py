"""D_PULSE level geometry.

Nothing guarded this node before. The defect it now carries a fix for was not
a crash: at five decimal places a token priced at 1e-4 had its entry and stop
round to the same number, and anything under roughly 5e-6 had all three levels
round to 0.0. evaluate_open_positions exits on `next_price >= target_exit_price`,
so a zero target closes the position on its first mark and books the result as
a take-profit -- a fabricated win, at a price that never traded, in the
majority of tokens this agent looks at.

These assertions exist so that a future change cannot quietly reintroduce
fixed-decimal rounding here.
"""
from tests.harness import Suite


def run(engine) -> Suite:
    s = Suite("D_PULSE levels")

    def levels(price):
        return engine.node_D_PULSE({"token_symbol": "T", "current_price": price})

    # Ordering must hold at every magnitude this asset class produces.
    for price in [1e-1, 1e-2, 1e-4, 5e-6, 1e-6, 1e-7, 2e-9, 5e-11]:
        out = levels(price)
        stop = out["invalidation_level_price"]
        entry = out["target_pullback_price"]
        target = out["target_exit_price"]
        s.check_true(f"levels strictly ordered and non-zero at {price:.0e}",
                     not out.get("termination_reason") and 0 < stop < entry < target)

    # ENTRY IS SPOT. The node used to set entry 7% below spot and report
    # pullback_detected=True without anything ever waiting for that pullback,
    # so every position opened 7% in profit against a price the market had
    # not traded. Risk and reward are still measured RELATIVE to the entry,
    # so the strategy's geometry is unchanged -- only the fabricated discount
    # is gone.
    out = levels(1e-7)
    entry, stop, target = (out["target_pullback_price"], out["invalidation_level_price"],
                           out["target_exit_price"])
    s.check_true("entry is spot, not a discount to it", abs(entry / 1e-7 - 1.0) < 1e-6)
    s.check_true("no pullback is claimed, because none is observed",
                 out.get("pullback_detected") is False)
    s.check_true("stop sits 7.53% below the entry", abs((stop / entry) - (1 - 0.0753)) < 1e-6)
    s.check_true("target is a 2:1 multiple of the entry-to-stop distance",
                 abs((target - entry) / (entry - stop) - 2.0) < 1e-6)
    # The specific defect: an entry better than the price we could transact at.
    for price in [1e-2, 1e-4, 1e-7]:
        s.check_true(f"entry at {price:.0e} is never below spot",
                     levels(price)["target_pullback_price"] >= price * (1 - 1e-9))

    # The specific values the old fixed-decimal rounding destroyed.
    s.check_true("a 1e-4 token no longer collapses entry onto stop",
                 levels(1e-4)["invalidation_level_price"] != levels(1e-4)["target_pullback_price"])
    s.check_true("a 1e-6 token no longer produces a zero target",
                 levels(1e-6)["target_exit_price"] > 0)

    # A zero target is the specific value that makes the exit test fire on the
    # first mark, so no path may emit one alongside an approved setup.
    for price in [1e-6, 1e-7, 2e-9]:
        out = levels(price)
        s.check_true(f"an approved setup at {price:.0e} never carries a zero exit level",
                     not (not out.get("termination_reason") and
                          (out["target_exit_price"] == 0 or out["invalidation_level_price"] == 0)))

    # The ordering guard is unreachable at any realistic price once rounding is
    # correct -- which is the point of it. It becomes reachable the moment
    # rounding regresses, and directly at the floating-point floor, where the
    # three levels collapse onto one denormal regardless of how they are
    # rounded. Pinned here so the guard itself is exercised, not just trusted.
    out = levels(5e-324)
    s.check_true("a price at the denormal floor collapses and must be refused",
                 bool(out.get("termination_reason")))
    s.check_true("a collapsed level set leaves no non-zero target behind",
                 out["target_exit_price"] == 0.0)

    # Unusable prices must refuse, and the refusal must be the kind router_G
    # and I_ACCOUNTANT actually honour.
    for bad, label in [(0.0, "a zero price"), (None, "a missing price"), (-1.0, "a negative price")]:
        out = levels(bad)
        s.check_true(f"{label} must refuse rather than emit levels",
                     bool(out.get("termination_reason")))
        s.check_true(f"{label} must not leave a non-zero target behind",
                     out["target_exit_price"] == 0.0)

    return s
