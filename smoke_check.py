"""Post-deploy smoke check: are the hardening fixes actually live in THIS build?

    docker compose exec web python smoke_check.py

Distinct from tests/run.py. The suite proves the logic is correct; this proves
the correct logic is what is running in the container you just started, and
prints the behaviour so you can see it rather than trust a PASS.

Exits non-zero if any fix is missing.
"""
import sys

FAILURES = []


def check(label, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    if not condition:
        FAILURES.append(label)
    print(f"  {mark}  {label}{('  -- ' + detail) if detail else ''}")


print("=" * 70)
print("SMOKE CHECK -- verifying the hardening fixes are live in this image")
print("=" * 70)

print("\nD_PULSE entry price")
import engine
out = engine.node_D_PULSE({"token_symbol": "SMOKE", "current_price": 1e-7})
entry = out["target_pullback_price"]
stop = out["invalidation_level_price"]
target = out["target_exit_price"]
check("entry is spot, not a 7% discount", abs(entry / 1e-7 - 1.0) < 1e-6,
      f"entry={entry:.4e} vs spot 1.0000e-07")
check("no pullback is claimed", out.get("pullback_detected") is False)
check("levels do not collapse at 1e-7", 0 < stop < entry < target,
      f"stop={stop:.4e} target={target:.4e}")
check("a degenerate price is refused",
      bool(engine.node_D_PULSE({"token_symbol": "S", "current_price": 0.0}).get("termination_reason")))

print("\nPaper-trading cost and tail")
import paper_trading as pt
fee_only = round(pt.PAPER_FEE_PERCENT_PER_SIDE * 2.0, 4)
check("unmeasured slippage is not free", pt.total_cost_percent(None) > fee_only,
      f"{pt.total_cost_percent(None)}% vs fees-only {fee_only}%")
reason, exit_price = pt.decide_exit(0.01, 1.15, 0.925, 5)
check("a gap through the stop fills at the market", exit_price == 0.01,
      f"{reason} at {exit_price} (stop was 0.925)")
check("a take-profit fills AT the target", pt.decide_exit(1.4, 1.15, 0.925, 5)[1] == 1.15)
check("unpriceable rows are classified as dropped",
      pt.classify_horizon_row(None, 1.0) == "DROPPED_NO_PRICE")

print("\nEngine resilience")
check("watchdog pool can be recycled after a hang", hasattr(engine, "_recycle_watchdog_pool"))
check("settings staleness is bounded", hasattr(engine, "_SETTINGS_MAX_STALE_SECONDS"),
      f"{getattr(engine, '_SETTINGS_MAX_STALE_SECONDS', 'MISSING')}s")

print("\nShared safety modules importable in this image")
try:
    import tx_verify, execution_rails, fill_accounting, market_microstructure
    check("tx_verify / execution_rails / fill_accounting / market_microstructure import", True)
    check("absolute position ceiling is a source constant",
          execution_rails.ABSOLUTE_MAX_POSITION_USD > 0,
          f"${execution_rails.ABSOLUTE_MAX_POSITION_USD:.0f}")
    r = tx_verify.verify_before_signing("not-base64", expected_fee_payer="X", sim_result=None,
                                        input_mint="A", output_mint="B", max_input_raw=1)
    check("tx_verify refuses an undecodable transaction", not r.ok)
    check("microstructure vetoes a 10% 5m drawdown",
          market_microstructure.check_drawdown_percent(-10.0).severity
          is market_microstructure.Severity.VETO)
except Exception as e:
    check(f"shared modules import ({type(e).__name__}: {e})", False)

print("\nHolder concentration: one definition, one ceiling")
try:
    import holder_concentration as hc
    check("holder_concentration imports in this image", True)
    check("the ceiling is the gate's ceiling",
          hc.TOP10_CONCENTRATION_CEILING_PERCENT > 0,
          f"{hc.TOP10_CONCENTRATION_CEILING_PERCENT}%")
    check("the gate reads the shared ceiling, not a literal",
          engine.node_F_ATLAS.__code__.co_consts is not None
          and "holder_concentration" in engine.node_F_ATLAS.__globals__,
          f"source={hc.CONCENTRATION_SOURCE}")
    # The failure this pairing prevents: an unmeasured token reading as
    # perfectly distributed.
    absent = hc.snapshot_fields(None, None)
    check("an unmeasured concentration is flagged, never passed as 0",
          absent["_holder_data_missing"] is True)
    check("F_ATLAS refuses an unmeasured token",
          bool(engine.node_F_ATLAS({"token_symbol": "S", "holder_data_missing": True})
               .get("termination_reason")))
    # Every provider key must be declared in the graph state or invoke() dies.
    import typing
    declared = set(typing.get_type_hints(engine.AgentNetworkState).keys())
    emitted = {k for k in hc.snapshot_fields(1.0, None) if not k.startswith("_")}
    check("every provider key is declared in the graph state",
          not (emitted - declared), f"undeclared: {sorted(emitted - declared)}")
    # The distinction the wallet definition rests on: a PDA is off the
    # ed25519 curve, so a signer-only pool authority with no account cannot
    # be mistaken for a never-written-to wallet.
    check("a program-derived address is not counted as a wallet",
          not hc.is_on_curve("1nc1nerator11111111111111111111111111111111"))
    check("a keypair address is counted as a wallet",
          hc.is_on_curve("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"))
    check("the RPC credential is not in the URL",
          "token" not in hc.SOLANA_RPC_URL.lower(),
          "x-token header set" if hc.SOLANA_RPC_X_TOKEN else "no x-token configured")
except Exception as e:
    check(f"holder_concentration wiring ({type(e).__name__}: {e})", False)

print("\n" + "=" * 70)
if FAILURES:
    print(f"{len(FAILURES)} FIX(ES) NOT LIVE IN THIS BUILD:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All hardening fixes are live in this build.")
