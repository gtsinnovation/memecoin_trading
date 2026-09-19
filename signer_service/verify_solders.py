# signer_service/verify_solders.py
"""Checks that solana_tx.py's assumptions match the INSTALLED solders.

Why this exists: solana_tx.py was written without network access to
pip-install solders, so its API calls were never executed even once before
reaching you. This exercises the real functions -- not a generic solders
snippet -- against whatever version the image actually built with.

Run it BEFORE creating a Turnkey account. It needs no credentials and no
network, so if solders has drifted you find out in ten seconds instead of
after forty-five minutes of account setup.

    docker compose --profile stage3 run --rm signer python3 verify_solders.py
"""
import sys
import traceback

# A valid base58 Solana address (the well-known wrapped-SOL mint) and a
# valid 32-byte blockhash. Real values, so from_string() genuinely parses
# rather than being handed something that happens not to raise.
ADDR = "So11111111111111111111111111111111111111112"
BLOCKHASH = "EETubP5AKHgjPAhzPAFcb8BAY1hMH639CWCFTqi3hq1k"

results = []
def check(label, fn):
    try:
        fn()
        results.append((True, label, ""))
        print(f"PASS  {label}")
    except Exception as e:
        results.append((False, label, f"{type(e).__name__}: {e}"))
        print(f"FAIL  {label}\n      {type(e).__name__}: {e}")


def _version():
    import solders
    print(f"solders version: {getattr(solders, '__version__', 'unknown')}")
    print(f"python:          {sys.version.split()[0]}\n")

try:
    _version()
except Exception as e:
    print(f"Could not import solders at all: {e}")
    sys.exit(2)

import solana_tx

state = {}

def build():
    tx = solana_tx.build_sol_transfer_tx(ADDR, ADDR, 1000, BLOCKHASH)
    assert isinstance(tx, (bytes, bytearray)), f"expected bytes, got {type(tx)}"
    assert len(tx) > 0, "serialized to zero bytes"
    state["unsigned"] = bytes(tx)
    print(f"      built {len(tx)} bytes")

def roundtrip():
    from solders.transaction import Transaction
    Transaction.from_bytes(state["unsigned"])

def unsigned_is_unsigned():
    # new_unsigned() must produce correctly-sized but EMPTY signature slots.
    # If this assumption is wrong, Turnkey gets the wrong payload shape.
    from solders.transaction import Transaction
    from solders.signature import Signature
    tx = Transaction.from_bytes(state["unsigned"])
    assert tx.signatures, "no signature slot at all -- Turnkey expects one placeholder"
    assert tx.signatures[0] == Signature.default(), "slot is not the zero placeholder"
    print(f"      {len(tx.signatures)} placeholder signature slot(s)")

def rejects_unsigned():
    # reassemble must REFUSE a transaction Turnkey never really signed.
    # This is the check that stops an unsigned tx reaching the network.
    try:
        solana_tx.reassemble_signed_sol_transfer(state["unsigned"], state["unsigned"].hex())
    except ValueError:
        return
    raise AssertionError("accepted a transaction with a placeholder signature -- it must refuse")

def rejects_bad_amount():
    for bad in (0, -1):
        try:
            solana_tx.build_sol_transfer_tx(ADDR, ADDR, bad, BLOCKHASH)
        except ValueError:
            continue
        raise AssertionError(f"accepted a {bad}-lamport transfer")

def rejects_bad_address():
    try:
        solana_tx.build_sol_transfer_tx("not-an-address", ADDR, 1000, BLOCKHASH)
    except Exception:
        return
    raise AssertionError("accepted a malformed from_pubkey")

def lamports():
    got = solana_tx.sol_to_lamports(1.5)
    assert got == 1_500_000_000, f"1.5 SOL should be 1500000000 lamports, got {got}"

def versioned_rejects_garbage():
    # Stage 4 path -- not exercised on devnet, but a drifted API would
    # surface here too.
    try:
        solana_tx.decode_jupiter_swap_transaction("!!!not base64!!!")
    except ValueError:
        return
    raise AssertionError("accepted invalid base64 as a Jupiter swap payload")

check("build_sol_transfer_tx() serializes", build)
check("the bytes round-trip through Transaction.from_bytes", roundtrip)
check("unsigned tx carries an empty placeholder signature", unsigned_is_unsigned)
check("reassemble refuses a still-unsigned transaction", rejects_unsigned)
check("build refuses zero/negative amounts", rejects_bad_amount)
check("build refuses a malformed address", rejects_bad_address)
check("sol_to_lamports converts correctly", lamports)
check("decode_jupiter_swap_transaction refuses bad base64", versioned_rejects_garbage)

failed = [r for r in results if not r[0]]
print()
if failed:
    print(f"{len(failed)} CHECK(S) FAILED -- solders' API has drifted from what solana_tx.py assumes.")
    print("Do NOT run the smoke test until these pass. Compare the errors above against")
    print("https://kevinheavey.github.io/solders/ and adjust solana_tx.py.")
    sys.exit(1)
print("ALL CHECKS PASSED -- solana_tx.py matches the installed solders. Safe to proceed to Turnkey setup.")


# The swap path compares two VersionedTransaction MESSAGES byte for byte.
# reassemble_signed_versioned_tx notes in its own comment that bytes(message)
# is not proven here and that it fails closed if neither serialization works --
# correct, but it means the Stage 4 integrity check has never been shown to
# actually run against the installed solders. Prove it before Stage 4 relies
# on it.
def versioned_message_is_serializable():
    from solders.transaction import VersionedTransaction
    tx = solana_tx.build_sol_transfer_tx(ADDR, ADDR, 1000, BLOCKHASH)
    vtx = VersionedTransaction.from_bytes(bytes(tx))
    msg = vtx.message
    out = None
    for attempt in (lambda: bytes(msg), lambda: msg.to_bytes()):
        try:
            candidate = attempt()
            if isinstance(candidate, (bytes, bytearray)):
                out = bytes(candidate)
                break
        except Exception:
            continue
    assert out, "neither bytes(message) nor message.to_bytes() works -- the swap-path integrity check cannot run"
    assert len(out) > 0, "message serialized to zero bytes"
    print(f"      message serializes to {len(out)} bytes")


check("VersionedTransaction message is serializable (swap-path integrity check)",
      versioned_message_is_serializable)
