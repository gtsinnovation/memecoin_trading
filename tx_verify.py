"""Decode and check a Solana transaction before it is signed.

WHY THIS EXISTS
The swap transaction handed to the signer is built by a third party. We send
Jupiter a quote and it returns bytes; today those bytes go straight to the
signer and then to the network. Nothing between the two asks what they say.

A signing policy scoped to "signing only" does not help here. It constrains
what the key may be USED for, not what it may be used ON. A position ceiling
does not help either: it bounds what we ASK for, not what we SIGN. If the
response is ever not the swap we requested -- a compromised endpoint, a
response-tampering bug, a confused-deputy mistake in our own code -- the wallet
signs it and the network executes it.

TWO CHECKS, AND AN HONEST LIMIT
1. STRUCTURAL (pure, offline). Decode the wire format: who pays the fee, how
   many signatures the message expects, which programs it invokes. Refuses a
   transaction whose fee payer is not our wallet, or which invokes a program
   outside the allowlist.

   The limit: Jupiter v6 routes use address lookup tables, so some program ids
   live in on-chain tables rather than in the message. Those cannot be resolved
   offline. This module REPORTS that rather than papering over it -- a
   transaction with unresolvable programs is explicitly `partially_verified`,
   never quietly "verified". Anything that treats partial as full defeats the
   point of the module.

2. SIMULATION (pure function over an RPC result). Because of that limit,
   simulation is the primary defence, not a nice-to-have. Simulate before
   broadcasting and assert the resulting balance deltas: we spend no more of
   the quote currency than intended, and we receive the token we asked for.
   That check holds no matter which programs the route went through, because
   it reads outcomes rather than intentions.

Neither check is a substitute for the post-confirmation balance reconciliation
that records the real fill. This module stops a bad transaction from being
signed; that one establishes what a good transaction actually did.
"""
import base64
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Programs a Jupiter swap legitimately touches. Anything else in the static
# keys is grounds for refusal.
ALLOWED_PROGRAMS: Dict[str, str] = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter Aggregator v6",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "SPL Token",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "SPL Token-2022",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "Associated Token Account",
    "ComputeBudget111111111111111111111111111111": "Compute Budget",
    "11111111111111111111111111111111": "System",
}


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _B58[rem] + out
    pad = 0
    for byte in data:
        if byte == 0:
            pad += 1
        else:
            break
    return "1" * pad + out


class MalformedTransaction(ValueError):
    """The bytes are not a transaction we can read. Never signed."""


@dataclass
class DecodedTransaction:
    signature_slots: int
    required_signatures: int
    fee_payer: str
    static_account_keys: List[str]
    static_program_ids: List[str]
    instruction_count: int
    uses_address_lookup_tables: bool
    unresolvable_program_indexes: List[int] = field(default_factory=list)


@dataclass
class VerifyResult:
    ok: bool
    partially_verified: bool = False
    reasons: List[str] = field(default_factory=list)
    detail: Optional[str] = None


def _shortvec(buf: bytes, i: int) -> (int, int):
    """Solana's compact-u16. Returns (value, next index)."""
    value = 0
    shift = 0
    while True:
        if i >= len(buf):
            raise MalformedTransaction("truncated compact-u16 length prefix")
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift > 21:
            raise MalformedTransaction("compact-u16 length prefix too long")


def _take(buf: bytes, i: int, n: int) -> (bytes, int):
    if n < 0 or i + n > len(buf):
        raise MalformedTransaction("transaction truncated")
    return buf[i:i + n], i + n


def decode_transaction(base64_transaction: str) -> DecodedTransaction:
    """Parse the transaction wire format. Raises rather than guessing.

    Handles both legacy and v0 messages. A parse failure is a refusal, not a
    warning: bytes we cannot read are bytes we must not sign.
    """
    try:
        raw = base64.b64decode(base64_transaction, validate=True)
    except Exception as exc:
        raise MalformedTransaction(f"not valid base64: {exc}") from exc
    if not raw:
        raise MalformedTransaction("empty transaction")

    sig_count, i = _shortvec(raw, 0)
    if not 1 <= sig_count <= 16:
        raise MalformedTransaction(f"implausible signature count {sig_count}")
    _, i = _take(raw, i, sig_count * 64)

    if i >= len(raw):
        raise MalformedTransaction("no message after signatures")
    version_byte = raw[i]
    versioned = bool(version_byte & 0x80)
    if versioned:
        version = version_byte & 0x7F
        if version != 0:
            raise MalformedTransaction(f"unsupported message version {version}")
        i += 1

    header, i = _take(raw, i, 3)
    required_signatures = header[0]

    key_count, i = _shortvec(raw, i)
    if not 1 <= key_count <= 256:
        raise MalformedTransaction(f"implausible account key count {key_count}")
    keys: List[str] = []
    for _ in range(key_count):
        key_bytes, i = _take(raw, i, 32)
        keys.append(b58encode(key_bytes))

    _, i = _take(raw, i, 32)  # recent blockhash

    instruction_count, i = _shortvec(raw, i)
    program_ids: List[str] = []
    unresolvable: List[int] = []
    for _ in range(instruction_count):
        program_index, i = _shortvec(raw, i)
        account_count, i = _shortvec(raw, i)
        _, i = _take(raw, i, account_count)
        data_len, i = _shortvec(raw, i)
        _, i = _take(raw, i, data_len)
        if program_index < len(keys):
            program_ids.append(keys[program_index])
        else:
            # The program id lives in an address lookup table, so it is not in
            # these bytes and cannot be named offline.
            unresolvable.append(program_index)

    uses_alt = False
    if versioned:
        try:
            lookup_count, i = _shortvec(raw, i)
            uses_alt = lookup_count > 0
        except MalformedTransaction:
            uses_alt = bool(unresolvable)

    return DecodedTransaction(
        signature_slots=sig_count,
        required_signatures=required_signatures,
        fee_payer=keys[0],
        static_account_keys=keys,
        static_program_ids=program_ids,
        instruction_count=instruction_count,
        uses_address_lookup_tables=uses_alt or bool(unresolvable),
        unresolvable_program_indexes=unresolvable,
    )


def verify_structure(decoded: DecodedTransaction,
                     expected_fee_payer: Optional[str],
                     allowed_programs: Optional[Set[str]] = None) -> VerifyResult:
    """Offline checks. `partially_verified` when lookup tables hide programs."""
    allowed = set(allowed_programs or ALLOWED_PROGRAMS.keys())
    reasons: List[str] = []

    if not expected_fee_payer:
        return VerifyResult(False, reasons=["expected fee payer not supplied -- cannot verify ownership"])
    if decoded.fee_payer != expected_fee_payer:
        reasons.append(f"fee payer is {decoded.fee_payer}, expected {expected_fee_payer}")

    # More than one required signature means something other than our wallet
    # must also sign, which no swap we build should need.
    if decoded.required_signatures != 1:
        reasons.append(f"message requires {decoded.required_signatures} signatures, expected 1")

    if decoded.instruction_count == 0:
        reasons.append("transaction contains no instructions")

    for program_id in decoded.static_program_ids:
        if program_id not in allowed:
            reasons.append(f"invokes unexpected program {program_id}")

    if reasons:
        return VerifyResult(False, reasons=reasons)

    if decoded.unresolvable_program_indexes:
        return VerifyResult(
            True, partially_verified=True,
            detail=(f"{len(decoded.unresolvable_program_indexes)} program id(s) resolve through "
                    f"address lookup tables and cannot be checked offline -- simulation is required"))
    return VerifyResult(True, detail="all programs resolved and allow-listed")


def verify_simulation(sim_result: Optional[Dict[str, Any]],
                      owner: Optional[str],
                      input_mint: Optional[str],
                      output_mint: Optional[str],
                      max_input_raw: Optional[int]) -> VerifyResult:
    """Check what the transaction WOULD do, from an RPC simulation.

    Reads the simulated pre/post token balances for our own wallet and asserts
    two things: we spend no more of the input mint than authorised, and we
    actually receive some of the output mint. This holds regardless of which
    programs the route used, which is what makes it the real defence.

    Every argument is Optional and None refuses. A simulation that could not be
    run is not a simulation that passed.
    """
    if sim_result is None:
        return VerifyResult(False, reasons=["simulation did not run -- refusing to sign unsimulated"])
    if not owner or not input_mint or not output_mint:
        return VerifyResult(False, reasons=["owner or mint missing -- cannot check simulated balances"])
    if max_input_raw is None:
        return VerifyResult(False, reasons=["no authorised input amount -- cannot bound the spend"])

    err = sim_result.get("err")
    if err:
        return VerifyResult(False, reasons=[f"simulation failed on-chain: {err}"])

    pre = sim_result.get("preTokenBalances")
    post = sim_result.get("postTokenBalances")
    if pre is None or post is None:
        return VerifyResult(False, reasons=["simulation returned no token balances -- nothing to verify"])

    def amount_for(rows, mint) -> Optional[int]:
        total = None
        for row in rows or []:
            if row.get("owner") == owner and row.get("mint") == mint:
                raw = (row.get("uiTokenAmount") or {}).get("amount")
                if raw is None:
                    continue
                total = (total or 0) + int(raw)
        return total

    pre_in, post_in = amount_for(pre, input_mint), amount_for(post, input_mint)
    if pre_in is None or post_in is None:
        return VerifyResult(False, reasons=["simulation did not report our balance of the input mint"])
    spent = pre_in - post_in
    if spent < 0:
        return VerifyResult(False, reasons=[f"simulation increases our input-mint balance by {-spent} -- not a buy"])
    if spent > int(max_input_raw):
        return VerifyResult(False, reasons=[f"simulation spends {spent} raw units, above the authorised {int(max_input_raw)}"])

    pre_out, post_out = amount_for(pre, output_mint) or 0, amount_for(post, output_mint)
    if post_out is None:
        return VerifyResult(False, reasons=["simulation did not report our balance of the output mint"])
    if post_out - pre_out <= 0:
        return VerifyResult(False, reasons=["simulation receives none of the requested token"])

    return VerifyResult(True, detail=f"simulated spend {spent} raw, receive {post_out - pre_out} raw")


def verify_before_signing(base64_transaction: str, *,
                          expected_fee_payer: Optional[str],
                          sim_result: Optional[Dict[str, Any]],
                          input_mint: Optional[str],
                          output_mint: Optional[str],
                          max_input_raw: Optional[int]) -> VerifyResult:
    """Both checks. Either one refusing refuses the whole transaction."""
    try:
        decoded = decode_transaction(base64_transaction)
    except MalformedTransaction as exc:
        return VerifyResult(False, reasons=[f"could not decode transaction: {exc}"])

    structural = verify_structure(decoded, expected_fee_payer)
    if not structural.ok:
        return structural

    simulated = verify_simulation(sim_result, expected_fee_payer, input_mint, output_mint, max_input_raw)
    if not simulated.ok:
        return simulated

    return VerifyResult(True, partially_verified=structural.partially_verified,
                        detail="; ".join(d for d in (structural.detail, simulated.detail) if d))
