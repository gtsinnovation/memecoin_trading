# solana_tx.py
"""Solana transaction construction and (de)serialization for the signer
service. This module builds unsigned transactions and reassembles signed
ones -- it never holds a private key or calls Turnkey itself (that's
turnkey_client.py).

IMPORTANT -- read before relying on this file:
The exact solders/solana-py calls used below (Transaction.new_unsigned,
VersionedTransaction.populate, etc.) are written from the well-established
solders API shape, but this sandbox has no network access to pip-install
solders or solana-py and verify them against the actual installed version
(see STAGE3_SETUP.md's "before you trust this" section). Before relying on
this in even a devnet test, run the verification snippet in
STAGE3_SETUP.md and confirm it matches what your installed solders version
actually exposes -- library APIs do shift between versions, and pinning a
version in requirements.txt does not substitute for actually running it
once yourself.

Two transaction paths, deliberately kept separate:
  - build_sol_transfer_tx() -- a plain native-SOL transfer. This is the
    ONLY path exercised end-to-end on devnet in Stage 3 (see README.md /
    STAGE3_SETUP.md): devnet has no real DEX liquidity, so it's the
    simplest way to prove the custody+signing+broadcast plumbing actually
    works before anything more complex touches it.
  - decode_jupiter_swap_transaction() -- unpacks the already-built
    transaction Jupiter's /swap endpoint returns. Jupiter has no devnet
    (see README.md), so this path is unit-tested with mocked Jupiter
    responses only in this stage, never executed against a live network.
"""
import base64
import logging
from typing import List, Optional

from solders.transaction import Transaction, VersionedTransaction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.hash import Hash as SolanaHash
from solders.system_program import transfer, TransferParams
from solders.signature import Signature

logger = logging.getLogger("signer_service.solana_tx")

LAMPORTS_PER_SOL = 1_000_000_000


def build_sol_transfer_tx(from_pubkey: str, to_pubkey: str, lamports: int,
                            recent_blockhash: str) -> bytes:
    """Builds an unsigned, single-instruction native-SOL transfer
    transaction and returns its serialized bytes (ready to hex-encode for
    Turnkey). This is the devnet-testable path -- see module docstring.

    Raises ValueError on a non-positive amount or invalid base58 address;
    callers (policy_guard.py) are expected to have already validated the
    amount against policy limits before this is ever called -- this is
    just a sanity check against malformed input, not a policy check.
    """
    if lamports <= 0:
        raise ValueError(f"transfer amount must be positive lamports, got {lamports}")

    from_pk = Pubkey.from_string(from_pubkey)
    to_pk = Pubkey.from_string(to_pubkey)
    blockhash = SolanaHash.from_string(recent_blockhash)

    ix = transfer(TransferParams(from_pubkey=from_pk, to_pubkey=to_pk, lamports=lamports))
    message = Message.new_with_blockhash([ix], from_pk, blockhash)
    # new_unsigned() produces a Transaction whose signature slots are
    # correctly sized (one per required signer) but filled with the
    # all-zero default Signature -- this is the "placeholder signature"
    # serialization Turnkey's sign_transaction endpoint expects (mirrors
    # the requireAllSignatures:false / verifySignatures:false convention
    # used by the official @turnkey/solana TS SDK -- see module docstring
    # on why the Python side needs your own verification).
    unsigned_tx = Transaction.new_unsigned(message)
    return bytes(unsigned_tx)


def reassemble_signed_sol_transfer(unsigned_tx_bytes: bytes, signature_hex: str) -> bytes:
    """Takes the original unsigned transfer tx bytes and the hex-encoded
    signature Turnkey's sign_transaction endpoint returns, and produces
    the final signed transaction bytes ready to broadcast.

    Turnkey's ACTIVITY_TYPE_SIGN_TRANSACTION_V2 for TRANSACTION_TYPE_SOLANA
    returns the FULL signed transaction (hex-encoded), not a bare
    signature -- so in practice turnkey_client.py hands this function the
    decoded bytes directly and this is mostly a pass-through/sanity check.
    Kept as a separate function (rather than inlined in turnkey_client.py)
    so the "what does a valid signed Solana tx look like" logic lives in
    one place, next to build_sol_transfer_tx().
    """
    tx = Transaction.from_bytes(signature_hex if isinstance(signature_hex, bytes) else bytes.fromhex(signature_hex))
    if not tx.signatures or tx.signatures[0] == Signature.default():
        raise ValueError("Turnkey returned a transaction with no real signature attached")

    # VERIFY IT IS THE SAME TRANSACTION. The unsigned_tx_bytes parameter was
    # previously accepted and never read: the only check was "a non-zero
    # signature is present", which says nothing about WHAT was signed.
    #
    # Anything able to influence the response body -- a spoofed
    # TURNKEY_API_BASE, a TLS-intercepting proxy, a mis-set signWith, a
    # Turnkey-side bug -- could return a perfectly valid signed transaction
    # whose MESSAGE drains the wallet somewhere else, and this function would
    # have handed it straight to the broadcaster. Layers 1 and 2 would have
    # validated a transaction that is not the one sent.
    #
    # On devnet the payload is a self-transfer, so the blast radius is small.
    # At Stage 4 the message carries a real destination and amount, which is
    # exactly when this check stops being theoretical.
    # Compared by RE-SERIALIZING the returned transaction as unsigned and
    # diffing it against the exact bytes we sent. This deliberately uses only
    # Transaction.new_unsigned() and bytes(Transaction) -- the two calls
    # verify_solders.py already proves against the installed solders. Reaching
    # into `.message` would add an unverified API to the one code path whose
    # failure mode is broadcasting someone else's transaction.
    try:
        rebuilt_unsigned = bytes(Transaction.new_unsigned(tx.message))
    except Exception as e:
        raise ValueError(f"Could not re-serialize the returned transaction for comparison: {e}")
    if rebuilt_unsigned != bytes(unsigned_tx_bytes):
        raise ValueError(
            "Turnkey returned a signed transaction whose message differs from the one "
            "submitted -- refusing to broadcast it."
        )
    return bytes(tx)


def decode_jupiter_swap_transaction(swap_transaction_b64: str) -> bytes:
    """Unpacks the base64-encoded, already-built VersionedTransaction that
    Jupiter's POST /swap endpoint returns (Jupiter builds the full
    instruction set server-side from a prior /quote response -- there is
    no instruction-building work for us to do here, just decode+validate).

    NOT exercised against a live network in Stage 3 -- Jupiter has no
    devnet (see README.md's "Real market data" section and
    STAGE3_SETUP.md). Unit-tested with a mocked base64 payload shaped like
    a real Jupiter response.
    """
    try:
        raw = base64.b64decode(swap_transaction_b64)
    except Exception as e:
        raise ValueError(f"swapTransaction is not valid base64: {e}")
    # Round-trip through VersionedTransaction to fail loudly on a
    # malformed payload rather than handing Turnkey garbage bytes.
    vtx = VersionedTransaction.from_bytes(raw)
    return bytes(vtx)


def verify_jupiter_swap_transaction(swap_transaction_b64: str, *,
                                      expected_fee_payer: str,
                                      sim_result: Optional[dict],
                                      input_mint: str,
                                      output_mint: str,
                                      max_input_raw: int):
    """Check an inbound Jupiter transaction BEFORE it is sent for signing.

    This closes the other half of the trust loop. reassemble_signed_*
    already proves Turnkey returned the same message we submitted --
    the OUTBOUND boundary. Nothing checked the INBOUND one: Jupiter
    builds the whole instruction set server-side and we signed whatever
    came back, so a compromised endpoint, an intercepting proxy or a
    response-tampering bug produced a transaction that was faithfully
    signed and faithfully broadcast.

        Jupiter --?--> us ------> Turnkey --OK--> us ---> network
                 ^^^^^                    ^^^^
              this check            already checked

    Delegates to tx_verify, the same dependency-free module the pipeline
    uses, so there is exactly one implementation of the wire format in
    the codebase rather than one here and one there.

    Raises ValueError on refusal -- callers must not catch and continue.
    """
    import tx_verify

    result = tx_verify.verify_before_signing(
        swap_transaction_b64,
        expected_fee_payer=expected_fee_payer,
        sim_result=sim_result,
        input_mint=input_mint,
        output_mint=output_mint,
        max_input_raw=max_input_raw,
    )
    if not result.ok:
        raise ValueError(
            "Refusing to sign the Jupiter swap transaction: " + "; ".join(result.reasons))
    if result.partially_verified:
        logger.warning(
            "Jupiter swap only PARTIALLY verified offline (%s). The simulation check "
            "passed, which is what bounds the spend.", result.detail)
    return result


def reassemble_signed_versioned_tx(unsigned_tx_bytes: bytes, signed_tx_hex: str) -> bytes:
    """Same idea as reassemble_signed_sol_transfer() but for the
    VersionedTransaction Jupiter swaps use. Turnkey returns the full
    signed transaction hex-encoded regardless of transaction shape, so
    this is again primarily a sanity check that a real signature is
    present before this is ever broadcast."""
    raw = signed_tx_hex if isinstance(signed_tx_hex, bytes) else bytes.fromhex(signed_tx_hex)
    vtx = VersionedTransaction.from_bytes(raw)
    if not vtx.signatures or vtx.signatures[0] == Signature.default():
        raise ValueError("Turnkey returned a versioned transaction with no real signature attached")
    # Same message-identity check as reassemble_signed_sol_transfer(). This is
    # the Stage 4 path, where the message contains a real swap -- a real
    # destination, a real amount, a real mint. Verifying that what came back
    # is what went out matters more here than anywhere else in the codebase.
    # VersionedTransaction has no new_unsigned(), so this compares the message
    # objects directly. bytes(message) is NOT covered by verify_solders.py, so
    # it is attempted with a to_bytes() fallback and FAILS CLOSED if neither
    # works -- an unverifiable comparison must refuse, not wave the
    # transaction through. Extend verify_solders.py before Stage 4 uses this.
    def _msg_bytes(m):
        for attempt in (lambda: bytes(m), lambda: m.to_bytes()):
            try:
                out = attempt()
                if isinstance(out, (bytes, bytearray)):
                    return bytes(out)
            except Exception:
                continue
        raise ValueError(
            "Could not serialize the transaction message for comparison -- refusing to "
            "broadcast a transaction whose contents cannot be verified against what was sent."
        )

    try:
        submitted = VersionedTransaction.from_bytes(bytes(unsigned_tx_bytes))
    except Exception as e:
        raise ValueError(f"Could not parse the versioned transaction we submitted for comparison: {e}")
    if _msg_bytes(submitted) != _msg_bytes(vtx):
        raise ValueError(
            "Turnkey returned a signed versioned transaction whose message differs from "
            "the one submitted -- refusing to broadcast it."
        )
    return bytes(vtx)


def sol_to_lamports(amount_sol: float) -> int:
    return int(round(amount_sol * LAMPORTS_PER_SOL))
