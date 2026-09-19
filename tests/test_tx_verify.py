"""Pre-signature transaction verification.

These tests build real Solana wire-format transactions byte by byte rather
than mocking the decoder, because the decoder is the part that must not be
wrong: everything downstream trusts what it reports.

The theme throughout is that every refusal path must refuse. A verifier that
returns "fine" when it could not read something is worse than no verifier,
because it converts an unknown into an assurance.
"""
import base64

from tests.harness import Suite
import tx_verify as tv

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}

JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
EVIL = "EviLPrxgram11111111111111111111111111111111"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_MINT = "So11111111111111111111111111111111111111112"


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        n = n * 58 + _B58_INDEX[ch]
    pad = len(text) - len(text.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + body


def shortvec(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def build_tx(*, fee_payer: str, programs, versioned=False, lookup_tables=0,
             required_signatures=1, signature_slots=1, extra_keys=(),
             instructions_beyond_keys=0) -> str:
    """Assemble a transaction the decoder must be able to read.

    `instructions_beyond_keys` adds instructions whose program index points
    past the static key list -- what an address-lookup-table route looks like
    on the wire.
    """
    keys = [b58decode(fee_payer)] + [b58decode(p) for p in programs] + [b58decode(k) for k in extra_keys]
    body = bytearray()
    body += shortvec(signature_slots) + b"\x00" * (64 * signature_slots)
    if versioned:
        body += bytes([0x80])
    body += bytes([required_signatures, 0, 1])
    body += shortvec(len(keys))
    for key in keys:
        body += key.rjust(32, b"\x00")
    body += b"\x11" * 32  # recent blockhash

    total_instructions = len(programs) + instructions_beyond_keys
    body += shortvec(total_instructions)
    for idx in range(len(programs)):
        body += shortvec(idx + 1)          # program index into static keys
        body += shortvec(1) + bytes([0])   # one account
        body += shortvec(2) + b"\xaa\xbb"  # opaque instruction data
    for extra in range(instructions_beyond_keys):
        body += shortvec(len(keys) + extra)  # index past the static keys
        body += shortvec(1) + bytes([0])
        body += shortvec(2) + b"\xcc\xdd"
    if versioned:
        body += shortvec(lookup_tables)
        for _ in range(lookup_tables):
            body += b"\x22" * 32 + shortvec(0) + shortvec(0)
    return base64.b64encode(bytes(body)).decode()


def sim(pre_in, post_in, pre_out, post_out, owner="OWNER", err=None):
    def row(mint, amount):
        return {"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(amount)}}
    return {"err": err,
            "preTokenBalances": [row(USDC, pre_in), row(TOKEN_MINT, pre_out)],
            "postTokenBalances": [row(USDC, post_in), row(TOKEN_MINT, post_out)]}


def run() -> Suite:
    s = Suite("tx verification")
    owner = tv.b58encode(b58decode(USDC))  # any valid 32-byte address as our wallet

    # --- decoding real wire format ---
    tx = build_tx(fee_payer=owner, programs=[COMPUTE_BUDGET, JUPITER, TOKEN_PROGRAM])
    d = tv.decode_transaction(tx)
    s.check("fee payer is read from the first account key", d.fee_payer, owner)
    s.check("every static program id is recovered", sorted(d.static_program_ids),
            sorted([COMPUTE_BUDGET, JUPITER, TOKEN_PROGRAM]))
    s.check("instruction count is read", d.instruction_count, 3)
    s.check_true("a legacy transaction reports no lookup tables", not d.uses_address_lookup_tables)

    v0 = build_tx(fee_payer=owner, programs=[JUPITER], versioned=True, lookup_tables=2)
    s.check_true("a v0 message with lookup tables is flagged",
                 tv.decode_transaction(v0).uses_address_lookup_tables)

    # --- malformed input must raise, never return a verdict ---
    for bad, label in [("", "empty string"), ("not base64!!", "invalid base64"),
                       (base64.b64encode(b"\x01\x02").decode(), "truncated body"),
                       (base64.b64encode(bytes([0])).decode(), "zero signature slots")]:
        try:
            tv.decode_transaction(bad)
            s.check_true(f"{label} must not decode", False)
        except tv.MalformedTransaction:
            s.check_true(f"{label} must raise MalformedTransaction", True)

    # Undecodable bytes must refuse at the top level rather than propagate.
    r = tv.verify_before_signing("not base64!!", expected_fee_payer=owner, sim_result=sim(100, 90, 0, 5),
                                 input_mint=USDC, output_mint=TOKEN_MINT, max_input_raw=100)
    s.check_true("an undecodable transaction refuses instead of crashing", not r.ok)

    # --- structural checks ---
    ok_struct = tv.verify_structure(tv.decode_transaction(tx), owner)
    s.check_true("a well-formed allow-listed transaction passes structurally", ok_struct.ok)
    s.check_true("a fully resolved transaction is not merely partial", not ok_struct.partially_verified)

    other = tv.b58encode(b58decode(TOKEN_MINT))
    s.check_true("a foreign fee payer must refuse",
                 not tv.verify_structure(tv.decode_transaction(tx), other).ok)
    s.check_true("an unsupplied expected fee payer must refuse",
                 not tv.verify_structure(tv.decode_transaction(tx), None).ok)

    evil = build_tx(fee_payer=owner, programs=[JUPITER, EVIL])
    r = tv.verify_structure(tv.decode_transaction(evil), owner)
    s.check_true("an unknown program must refuse", not r.ok)
    s.check_true("the refusal names the offending program", any(EVIL in x for x in r.reasons))

    multi = build_tx(fee_payer=owner, programs=[JUPITER], required_signatures=2, signature_slots=2)
    s.check_true("a transaction needing a second signer must refuse",
                 not tv.verify_structure(tv.decode_transaction(multi), owner).ok)

    empty = build_tx(fee_payer=owner, programs=[])
    s.check_true("a transaction with no instructions must refuse",
                 not tv.verify_structure(tv.decode_transaction(empty), owner).ok)

    # The honest limit: programs hidden behind lookup tables are reported as
    # partial, never as verified.
    alt = build_tx(fee_payer=owner, programs=[JUPITER], versioned=True,
                   lookup_tables=1, instructions_beyond_keys=2)
    r = tv.verify_structure(tv.decode_transaction(alt), owner)
    s.check_true("a lookup-table route still passes what can be checked", r.ok)
    s.check_true("but it is marked partially verified, not verified", r.partially_verified)

    # --- simulation checks ---
    s.check_true("a simulation spending within budget and receiving tokens passes",
                 tv.verify_simulation(sim(1000, 900, 0, 50), "OWNER", USDC, TOKEN_MINT, 100).ok)
    s.check_true("spending more than authorised must refuse",
                 not tv.verify_simulation(sim(1000, 500, 0, 50), "OWNER", USDC, TOKEN_MINT, 100).ok)
    s.check_true("receiving none of the requested token must refuse",
                 not tv.verify_simulation(sim(1000, 900, 0, 0), "OWNER", USDC, TOKEN_MINT, 100).ok)
    s.check_true("a transaction that increases our input balance is not a buy",
                 not tv.verify_simulation(sim(1000, 1100, 0, 50), "OWNER", USDC, TOKEN_MINT, 100).ok)
    s.check_true("an on-chain simulation error must refuse",
                 not tv.verify_simulation(sim(1000, 900, 0, 50, err={"InstructionError": 1}),
                                          "OWNER", USDC, TOKEN_MINT, 100).ok)
    s.check_true("a simulation that did not run must refuse",
                 not tv.verify_simulation(None, "OWNER", USDC, TOKEN_MINT, 100).ok)
    s.check_true("a simulation with no balance data must refuse",
                 not tv.verify_simulation({"err": None}, "OWNER", USDC, TOKEN_MINT, 100).ok)
    s.check_true("an unbounded spend must refuse",
                 not tv.verify_simulation(sim(1000, 900, 0, 50), "OWNER", USDC, TOKEN_MINT, None).ok)
    s.check_true("a missing mint must refuse",
                 not tv.verify_simulation(sim(1000, 900, 0, 50), "OWNER", None, TOKEN_MINT, 100).ok)
    # Balances belonging to somebody else must not be counted as ours.
    s.check_true("another wallet's balances must not satisfy the check",
                 not tv.verify_simulation(sim(1000, 900, 0, 50, owner="SOMEONE_ELSE"),
                                          "OWNER", USDC, TOKEN_MINT, 100).ok)
    # Spending exactly the authorised amount is allowed; one unit more is not.
    s.check_true("spending exactly the authorised amount is allowed",
                 tv.verify_simulation(sim(1000, 900, 0, 50), "OWNER", USDC, TOKEN_MINT, 100).ok)
    s.check_true("one raw unit above the authorised amount must refuse",
                 not tv.verify_simulation(sim(1000, 899, 0, 50), "OWNER", USDC, TOKEN_MINT, 100).ok)

    # --- combined gate ---
    good = build_tx(fee_payer=owner, programs=[JUPITER])
    owner2 = tv.decode_transaction(good).fee_payer
    r = tv.verify_before_signing(good, expected_fee_payer=owner2, sim_result=sim(1000, 900, 0, 50, owner=owner2),
                                 input_mint=USDC, output_mint=TOKEN_MINT, max_input_raw=100)
    s.check_true("structure and simulation both passing signs", r.ok)
    r = tv.verify_before_signing(good, expected_fee_payer=owner2, sim_result=None,
                                 input_mint=USDC, output_mint=TOKEN_MINT, max_input_raw=100)
    s.check_true("a good structure with no simulation must still refuse", not r.ok)

    return s
