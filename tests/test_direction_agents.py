"""A_ORBIT and C_VECTOR.

Both nodes were hardcoded passes. A_ORBIT's was worse than inert: the provider
already computes onchain_volume_increasing and puts it in the state, so
returning True OVERWROTE a real measurement with a constant. These assertions
pin that it never does so again, and that direction degrades execution rather
than vetoing -- except the single 5-minute hard stop, which must veto through
the termination_reason path router_G actually honours.
"""
from tests.harness import Suite


def run(engine) -> Suite:
    s = Suite("direction agents")
    base = {"token_symbol": "TEST"}

    def orbit(**counts):
        return engine.node_A_ORBIT({**base, **counts})

    def vector(pct=None, **extra):
        return engine.node_A_ORBIT and engine.node_C_VECTOR({**base, "price_change_m5": pct, **extra})

    # --- A_ORBIT must not clobber the provider's measurement ---
    out = orbit(txns_m5_buys=40, txns_m5_sells=10, txns_h1_buys=400, txns_h1_sells=200)
    s.check_true("A_ORBIT must not return onchain_volume_increasing at all",
                 "onchain_volume_increasing" not in out)
    s.check_true("healthy two-window flow does not degrade execution", not out["execution_degraded"])

    out = orbit(txns_m5_buys=5, txns_m5_sells=40, txns_h1_buys=100, txns_h1_sells=300)
    s.check_true("sells beating buys in every window degrades execution", out["execution_degraded"])

    out = orbit(txns_m5_buys=5, txns_m5_sells=40, txns_h1_buys=400, txns_h1_sells=100)
    s.check_true("one adverse window alone is noise, not a trend", not out["execution_degraded"])

    s.check_true("absent counts degrade execution rather than reading as calm",
                 orbit()["execution_degraded"])
    s.check_true("a window with zero trades is not counted as a direction",
                 not orbit(txns_m5_buys=0, txns_m5_sells=0,
                           txns_h1_buys=400, txns_h1_sells=200)["execution_degraded"])
    s.check_true("A_ORBIT never vetoes on flow alone",
                 not orbit(txns_m5_buys=0, txns_m5_sells=99,
                           txns_h1_buys=0, txns_h1_sells=99).get("termination_reason"))

    # --- C_VECTOR: the one directional veto ---
    out = vector(2.0)
    s.check_true("a calm market passes without degrading", out["entry_conditions_met"] and not out["execution_degraded"])

    out = vector(-6.0)
    s.check_true("a 6% 5m fall degrades execution", out["execution_degraded"])
    s.check_true("a 6% 5m fall does not veto", not out.get("termination_reason"))

    for pct in (-10.0, -12.0, -40.0):
        out = vector(pct)
        s.check_true(f"a {abs(pct):.0f}% 5m fall must veto", not out["entry_conditions_met"])
        s.check_true(f"a {abs(pct):.0f}% 5m fall must set termination_reason",
                     bool(out.get("termination_reason")))

    out = vector(None)
    s.check_true("absent 5m data degrades execution", out["execution_degraded"])
    s.check_true("absent 5m data must NOT veto -- unread is not falling",
                 out["entry_conditions_met"] and not out.get("termination_reason"))

    s.check_true("an upward move never vetoes", not vector(45.0).get("termination_reason"))

    # Degradation raised upstream must survive, not be reset by a calm 5m read.
    s.check_true("A_ORBIT's degradation is carried through C_VECTOR",
                 vector(1.0, execution_degraded=True)["execution_degraded"])

    # The veto must use the mechanism the graph actually honours: router_G
    # routes on termination_reason, and I_ACCOUNTANT skips save_active_position.
    s.check_true("router_G routes a C_VECTOR veto away from H_FUSE",
                 engine.router_G({"termination_reason": vector(-20.0)["termination_reason"]}) == "I_ACCOUNTANT")
    s.check_true("router_G lets a clean read through to H_FUSE",
                 engine.router_G({"termination_reason": None}) == "H_FUSE")

    return s
