"""Holder-concentration suite.

Guards the two things this module was created to stop:

  1. THREE DEFINITIONS, ONE CEILING. Raw chain concentration counts the AMM
     pool as a holder; RugCheck's figure does not. Both used to be compared
     against the same 30% literal depending on an environment variable. The
     resolver must never substitute one definition for another, even when
     the configured one is unavailable -- an absence is an absence.

  2. AN UNMEASURABLE INPUT READING AS A PERMISSIVE NUMBER. Every failure
     path must produce None, because F_ATLAS's missing-data branch is the
     only thing stopping "nobody could measure this" from being treated as
     "perfectly distributed".

No database and no network: the RPC client is a stub.
"""
import asyncio

from tests.harness import Suite

import holder_concentration as hc


SYSTEM = hc.SYSTEM_PROGRAM
BURN = hc.INCINERATOR
# These two are REAL structural cases, not decorative strings. WALLET decodes
# to a point on the ed25519 curve (a keypair address); POOL_OWNER does not (a
# program-derived address). The classifier now depends on that difference, so
# a fixture of invented base58 would test nothing -- every invented string is
# off-curve about half the time and by accident.
# Found by scanning random 32-byte values; verified by the assertions below.
WALLET = "AUH6c4QLMr2qQr9N5Kkpz5astDM9gBNroXCSxQiFTGQv"        # on curve
POOL_OWNER = "6anbDQNCcVh2f6okexjaX1VGj6tEnizJ1kV5UTBS8Zhi"    # off curve
# On the curve, but its account is owned by a program rather than the System
# Program. The curve test alone would call this a wallet; the account-state
# test is what refuses it. Both tests are load-bearing, in opposite cases.
CURVE_BUT_OWNED = "F6Uuvee6Nof2Lxn7BLNYvoU1Pjm6odP2LqFXGdaUuekF"
AMM_PROGRAM = "AmmProgram111111111111111111111111111111"
ACC_A = "TokenAcctA111111111111111111111111111111"
ACC_B = "TokenAcctB111111111111111111111111111111"
ACC_C = "TokenAcctC111111111111111111111111111111"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class StubRPC:
    """Minimal async stand-in for httpx.AsyncClient.

    `overrides` maps a method name to a payload (or to an Exception to
    raise), so a single failure can be injected without rebuilding the
    whole fixture.
    """

    def __init__(self, *, supply="1000", accounts=None, owners=None,
                 owner_programs=None, overrides=None):
        self.supply = supply
        # (address, raw amount in base units)
        self.accounts = accounts if accounts is not None else [
            (ACC_A, "400"),   # the pool
            (ACC_B, "200"),   # a real wallet
            (ACC_C, "100"),   # burned
        ]
        self.owners = owners if owners is not None else {
            ACC_A: POOL_OWNER, ACC_B: WALLET, ACC_C: BURN}
        # Who owns each OWNER account. System Program => a real wallet.
        self.owner_programs = owner_programs if owner_programs is not None else {
            POOL_OWNER: AMM_PROGRAM, WALLET: SYSTEM}
        self.overrides = overrides or {}
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        method = json["method"]
        params = json["params"]
        self.calls.append((method, params))
        if method in self.overrides:
            payload = self.overrides[method]
            if isinstance(payload, Exception):
                raise payload
            return _Resp(payload)

        if method == "getTokenSupply":
            return _Resp({"result": {"value": {"amount": self.supply,
                                               "decimals": 6,
                                               "uiAmount": 0.001}}})
        if method == "getTokenLargestAccounts":
            return _Resp({"result": {"value": [
                {"address": a, "amount": amt} for a, amt in self.accounts]}})
        if method == "getMultipleAccounts":
            addresses, opts = params[0], params[1]
            if opts.get("encoding") == "jsonParsed":
                return _Resp({"result": {"value": [
                    {"data": {"parsed": {"info": {"owner": self.owners.get(a)}}}}
                    for a in addresses]}})
            # base64: classify each OWNER address
            out = []
            for a in addresses:
                prog = self.owner_programs.get(a, SYSTEM)
                out.append(None if prog is None else {"owner": prog})
            return _Resp({"result": {"value": out}})
        raise AssertionError(f"unexpected RPC method {method}")


def run() -> Suite:
    s = Suite("holder concentration")

    # ----------------------------------------------------- the curve test
    # A PDA is off the ed25519 curve by construction -- that is what makes it
    # unsignable, and it is the only offline way to tell a signer-only PDA
    # from a keypair nobody has written to yet. Both look identical by
    # account state, and calling the first one a wallet puts an entire
    # liquidity pool into the "wallet concentration" bucket.
    s.check_true("a known off-curve address is not a wallet",
                 not hc.is_on_curve(BURN))
    s.check_true("the token program address parses as a curve point",
                 hc.is_on_curve("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"))
    s.check_true("a non-base58 string is not a wallet", not hc.is_on_curve("0OIl"))
    s.check_true("an empty address is not a wallet", not hc.is_on_curve(""))
    s.check_true("a short address is not a wallet", not hc.is_on_curve("abc"))
    s.check_true("None is not a wallet", not hc.is_on_curve(None))
    s.check("base58 round-trips through the decoder",
            len(hc.b58decode("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")), 32)
    # Roughly half of random 32-byte values are curve points. A decoder that
    # accepted everything, or nothing, would not land anywhere near that.
    import os as _os
    def _b58(b):
        n = int.from_bytes(b, "big"); out = ""
        while n:
            n, r = divmod(n, 58); out = hc._B58[r] + out
        return "1" * (len(b) - len(b.lstrip(b"\x00"))) + out
    hits = sum(1 for _ in range(600) if hc.is_on_curve(_b58(_os.urandom(32))))
    s.check_true(f"about half of random keys are curve points (got {hits}/600)",
                 150 < hits < 450)

    s.check_true("the fixture's wallet address really is a curve point",
                 hc.is_on_curve(WALLET))
    s.check_true("the fixture's pool authority really is off the curve",
                 not hc.is_on_curve(POOL_OWNER))

    # ---------------------------------------------------------------- chain
    chain = asyncio.run(hc.fetch_chain_concentration(StubRPC(), "MINT"))
    s.check_true("raw counts every top-10 account including the pool",
                 abs(chain.raw_percent - 70.0) < 1e-9)
    s.check_true("wallet counts only System-Program-owned owners",
                 abs(chain.wallet_percent - 20.0) < 1e-9)
    s.check_true("a program-owned token account is program supply, not wallet supply",
                 abs(chain.program_percent - 40.0) < 1e-9)
    s.check_true("the incinerator is burn, neither wallet nor program",
                 abs(chain.burn_percent - 10.0) < 1e-9)
    s.check_true("the three buckets must reconstruct raw",
                 abs((chain.wallet_percent + chain.program_percent
                      + chain.burn_percent) - chain.raw_percent) < 1e-9)
    s.check_true("no error is reported on a clean measurement", chain.error is None)

    # The distinction the whole module rests on: an owner with no account at
    # all is an ordinary keypair wallet, not an unknown.
    never_written = StubRPC(owner_programs={POOL_OWNER: AMM_PROGRAM, WALLET: None})
    chain = asyncio.run(hc.fetch_chain_concentration(never_written, "MINT"))
    s.check_true("an owner address with no account on chain is a plain wallet",
                 abs(chain.wallet_percent - 20.0) < 1e-9)

    # The case the curve test exists for: a pool authority PDA that has no
    # account on chain. By account state it is indistinguishable from a
    # never-written-to keypair, and the old rule called it a wallet -- which
    # reported a freshly-graduated token as 99% held by ten wallets when
    # almost all of it was sitting in the liquidity pool.
    signer_only_pda = StubRPC(owner_programs={POOL_OWNER: None, WALLET: SYSTEM})
    chain = asyncio.run(hc.fetch_chain_concentration(signer_only_pda, "MINT"))
    s.check_true("a signer-only PDA with NO account is not a wallet",
                 abs(chain.wallet_percent - 20.0) < 1e-9)
    s.check_true("its supply is counted as program-held",
                 abs(chain.program_percent - 40.0) < 1e-9)

    # The mirror case: on the curve, but the account is program-owned. The
    # curve test passes it and the account-state test must not.
    owned = StubRPC(owners={ACC_A: CURVE_BUT_OWNED, ACC_B: WALLET, ACC_C: BURN},
                    owner_programs={CURVE_BUT_OWNED: AMM_PROGRAM, WALLET: SYSTEM})
    chain = asyncio.run(hc.fetch_chain_concentration(owned, "MINT"))
    s.check_true("a curve point whose account is program-owned is not a wallet",
                 abs(chain.wallet_percent - 20.0) < 1e-9)
    s.check_true("both tests must pass, not either one",
                 abs(chain.program_percent - 40.0) < 1e-9)

    # Supply must come from `amount` (base units), not `uiAmount`. The stub's
    # uiAmount is deliberately inconsistent with amount: if the code reads
    # uiAmount the percentages come out absurd rather than subtly wrong.
    s.check_true("supply is read in base units, not the RPC's pre-divided uiAmount",
                 abs(chain.raw_percent - 70.0) < 1e-9)

    # --- every failure path is an absence, never a zero ---
    dead = StubRPC(overrides={"getTokenSupply": {"error": {"code": -32000}}})
    chain = asyncio.run(hc.fetch_chain_concentration(dead, "MINT"))
    s.check_true("an RPC error yields no measurement at all", chain.raw_percent is None)
    s.check_true("an RPC error is reported, not swallowed", bool(chain.error))

    zero_supply = StubRPC(supply="0")
    chain = asyncio.run(hc.fetch_chain_concentration(zero_supply, "MINT"))
    s.check_true("zero total supply is unmeasurable, not 100% concentration",
                 chain.raw_percent is None)

    raised = StubRPC(overrides={"getTokenLargestAccounts": RuntimeError("boom")})
    chain = asyncio.run(hc.fetch_chain_concentration(raised, "MINT"))
    s.check_true("a transport exception is caught and reported as absence",
                 chain.raw_percent is None and bool(chain.error))

    no_accounts = StubRPC(accounts=[])
    chain = asyncio.run(hc.fetch_chain_concentration(no_accounts, "MINT"))
    s.check_true("a mint with no token accounts is unmeasurable",
                 chain.raw_percent is None)

    # Classification failure keeps RAW but must not invent a wallet figure:
    # a partial classification would push program supply into the wallet
    # bucket, which biases DOWN -- the direction that turns a concentrated
    # token into a passing one.
    class _ClassifyFails(StubRPC):
        async def post(self, url, json=None, headers=None, timeout=None):
            if (json["method"] == "getMultipleAccounts"
                    and json["params"][1].get("encoding") != "jsonParsed"):
                return _Resp({"error": {"code": -32000, "message": "nope"}})
            return await StubRPC.post(self, url, json=json, headers=headers, timeout=timeout)

    chain = asyncio.run(hc.fetch_chain_concentration(_ClassifyFails(), "MINT"))
    s.check_true("raw survives an owner-classification failure",
                 chain.raw_percent is not None)
    s.check_true("wallet is absent when owners could not be classified",
                 chain.wallet_percent is None)
    s.check_true("a classification failure is reported", bool(chain.error))

    mismatched = StubRPC(overrides={"getMultipleAccounts": {"result": {"value": []}}})
    chain = asyncio.run(hc.fetch_chain_concentration(mismatched, "MINT"))
    s.check_true("an owner list of the wrong length is refused, not zipped short",
                 chain.wallet_percent is None)

    # ------------------------------------------------- the bucketing itself
    # Every wrong answer in this arithmetic is still a plausible percentage,
    # so it is tested directly rather than only through the RPC path.
    amounts = [400.0, 200.0, 100.0]
    owners = [POOL_OWNER, WALLET, BURN]
    classified = {POOL_OWNER: False, WALLET: True}
    s.check("pool / wallet / burn are bucketed apart",
            hc.bucket_amounts(amounts, owners, classified), (200.0, 400.0, 100.0))
    s.check_true("an owner missing from the classification map is refused, "
                 "never defaulted to wallet",
                 hc.bucket_amounts(amounts, owners, {WALLET: True}) is None)
    s.check("a None owner is burn, not an unclassified refusal",
            hc.bucket_amounts([50.0], [None], {}), (0.0, 0.0, 50.0))
    s.check("the incinerator needs no classification entry",
            hc.bucket_amounts([50.0], [BURN], {}), (0.0, 0.0, 50.0))
    s.check("a program-owned account never lands in the wallet bucket",
            hc.bucket_amounts([100.0], [POOL_OWNER], {POOL_OWNER: False}),
            (0.0, 100.0, 0.0))

    # ------------------------------------------------------------- resolver
    full = hc.ChainConcentration(raw_percent=70.0, wallet_percent=20.0,
                                 program_percent=40.0, burn_percent=10.0)

    r = hc.resolve_concentration(12.5, full, source="provider")
    s.check_true("provider source gates on the provider's number", r.percent == 12.5)
    s.check_true("a present provider number is not missing", r.missing is False)

    r = hc.resolve_concentration(12.5, full, source="chain_wallet")
    s.check_true("chain_wallet gates on the wallet figure", r.percent == 20.0)
    r = hc.resolve_concentration(12.5, full, source="chain_raw")
    s.check_true("chain_raw gates on the raw figure", r.percent == 70.0)

    # THE anti-divergence assertion. Falling back across definitions is how a
    # ceiling calibrated for one quantity ends up applied to another.
    no_wallet = hc.ChainConcentration(raw_percent=70.0, wallet_percent=None)
    r = hc.resolve_concentration(12.5, no_wallet, source="chain_wallet")
    s.check_true("an unavailable wallet figure must NOT fall back to the provider's",
                 r.percent is None)
    s.check_true("an unavailable wallet figure must NOT fall back to raw",
                 r.percent != 70.0)
    s.check_true("an unavailable configured measurement is flagged missing",
                 r.missing is True)

    r = hc.resolve_concentration(None, full, source="provider")
    s.check_true("an absent provider reading is missing even with chain present",
                 r.percent is None and r.missing is True)

    r = hc.resolve_concentration(12.5, None, source="chain_wallet")
    s.check_true("no chain measurement at all is an absence, not the provider's number",
                 r.percent is None and r.missing is True)

    r = hc.resolve_concentration(12.5, full, source="not_a_real_source")
    s.check_true("an unknown source falls back to provider, not to whatever is first",
                 r.percent == 12.5 and r.source == "provider")

    # ------------------------------------------------------- snapshot fields
    fields = hc.snapshot_fields(12.5, full, source="provider")
    s.check_true("the gate key keeps its historic name",
                 fields["top_10_holder_percentage"] == 12.5)
    s.check_true("a measured reading is not flagged missing",
                 fields["_holder_data_missing"] is False)
    s.check_true("the source is recorded so a row can say what it was measured as",
                 fields["holder_concentration_source"] == "provider")
    for key, want in (("holder_concentration_provider_pct", 12.5),
                      ("holder_concentration_raw_pct", 70.0),
                      ("holder_concentration_wallet_pct", 20.0),
                      ("holder_concentration_program_pct", 40.0),
                      ("holder_concentration_burn_pct", 10.0)):
        s.check(f"snapshot carries {key} for later calibration", fields[key], want)

    absent = hc.snapshot_fields(None, None, source="provider")
    s.check_true("an unmeasured reading is flagged missing",
                 absent["_holder_data_missing"] is True)
    s.check_true("the 0.0 the gate key carries is PAIRED with the missing flag, "
                 "never offered alone", absent["top_10_holder_percentage"] == 0.0
                 and absent["_holder_data_missing"] is True)
    for key in ("holder_concentration_raw_pct", "holder_concentration_wallet_pct"):
        s.check_true(f"{key} is None, not 0.0, when unmeasured", absent[key] is None)

    # ------------------------------------------------------------- observing
    s.check_true("observation never raises on a dead client",
                 asyncio.run(hc.observe_chain(
                     StubRPC(overrides={"getTokenSupply": RuntimeError("x")}),
                     "MINT")).raw_percent is None)

    saved = hc.OBSERVE_CHAIN, hc.CONCENTRATION_SOURCE
    try:
        hc.OBSERVE_CHAIN, hc.CONCENTRATION_SOURCE = False, "provider"
        s.check_true("observation off means no RPC traffic at all",
                     asyncio.run(hc.observe_chain(StubRPC(), "MINT")) is None)
        hc.OBSERVE_CHAIN, hc.CONCENTRATION_SOURCE = False, "chain_wallet"
        s.check_true("a gate that depends on chain measures even with observation off",
                     asyncio.run(hc.observe_chain(StubRPC(), "MINT")) is not None)
    finally:
        hc.OBSERVE_CHAIN, hc.CONCENTRATION_SOURCE = saved

    # ------------------------------------------------------------- the gate
    s.check_true("the ceiling is a named constant, not a literal in the gate",
                 hc.TOP10_CONCENTRATION_CEILING_PERCENT == 30.0)
    s.check_true("the credential is a header, never in the URL",
                 "token" not in hc.SOLANA_RPC_URL.lower()
                 and ("x-token" in hc.rpc_headers() or not hc.SOLANA_RPC_X_TOKEN))

    # --------------------------------------------- the key that crashes a tick
    # main.py hands the provider snapshot straight to agent_network.invoke(),
    # so every non-underscore key a provider emits has to be declared in
    # AgentNetworkState or LangGraph rejects the input -- and that failure is
    # not a wrong number, it is every tick dying. Read statically so this
    # assertion needs neither langgraph nor a database.
    import ast
    import os

    engine_src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "engine.py"), encoding="utf-8").read()
    declared = set()
    for node in ast.walk(ast.parse(engine_src)):
        if isinstance(node, ast.ClassDef) and node.name == "AgentNetworkState":
            declared = {t.target.id for t in node.body
                        if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)}
    s.check_true("AgentNetworkState was found and is non-empty", len(declared) > 10)
    emitted = {k for k in hc.snapshot_fields(1.0, full) if not k.startswith("_")}
    s.check("every key the provider emits is declared in the graph state",
            sorted(emitted - declared), [])

    return s
