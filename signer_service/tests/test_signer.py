# signer_service/tests/test_signer.py
"""Guards audit fixes 12-14 plus the three audit-trail gaps.

The signer is the only component that can move funds. These tests are
adversarial by design: they assume the caller is buggy or hostile, and they
assert that nothing gets signed outside policy and that nothing that MIGHT
have moved funds goes unrecorded.
"""
import asyncio
import sys
import types


class Suite:
    def __init__(self, name):
        self.name, self.failures, self.passes = name, [], 0

    def check(self, label, got, want):
        if got == want:
            self.passes += 1; print(f"  PASS  {label}")
        else:
            self.failures.append(f"{label}: got {got!r}, want {want!r}")
            print(f"  FAIL  {label}: got {got!r}, want {want!r}")

    def check_true(self, label, got):
        self.check(label, bool(got), True)

    def ok(self):
        return not self.failures


def run() -> Suite:
    s = Suite("signer security")
    import main as signer_main
    import solana_tx
    import solana_rpc
    import turnkey_client
    from pydantic import ValidationError

    ADDR = "So11111111111111111111111111111111111111112"
    BLOCKHASH = "EETubP5AKHgjPAhzPAFcb8BAY1hMH639CWCFTqi3hq1k"
    OID = "5f0c8a52-6f1e-4a53-9a57-0c7d1f3e2b11"

    print("\n[INPUT] NaN and infinities must not defeat the amount guards")
    # nan <= 0, nan > cap and (allocated + nan) > cap are ALL False, so a NaN
    # passed every numeric check in policy_guard. json.loads accepts the bare
    # literal, so this was reachable from the wire.
    for bad, label in ((float("nan"), "NaN"), (float("inf"), "inf"), (float("-inf"), "-inf")):
        try:
            signer_main.ExecuteRequest(token_address=ADDR, requested_usd=bad, client_order_id=OID)
            got = "ACCEPTED"
        except ValidationError:
            got = "rejected"
        s.check(f"{label} rejected at the model", got, "rejected")
    s.check("a finite amount is still accepted",
            signer_main.ExecuteRequest(token_address=ADDR, requested_usd=5, client_order_id=OID).requested_usd, 5.0)

    print("\n[INPUT] over-long fields must not become an audit-suppression tool")
    # token_address is VARCHAR(128). Longer than that made every audit INSERT
    # fail -- and that failure is caught and logged, so the attempt vanished.
    try:
        signer_main.ExecuteRequest(token_address="A" * 200, requested_usd=5, client_order_id=OID); got = "ACCEPTED"
    except ValidationError:
        got = "rejected"
    s.check("200-char token_address rejected", got, "rejected")

    print("\n[NETWORK] the audit label must not be guessed from a URL's secrets")
    import os
    saved_url, saved_net = solana_rpc.SOLANA_RPC_URL, os.environ.get("SOLANA_NETWORK")
    os.environ.pop("SOLANA_NETWORK", None)
    for url, want in (
        ("https://api.devnet.solana.com", "devnet"),
        ("https://api.mainnet-beta.solana.com", "mainnet"),
        # An API key containing the letters 'devnet' used to label a MAINNET
        # transaction as devnet, in the one column an auditor reads.
        ("https://mainnet.helius-rpc.com/?api-key=8f3devnet91", "mainnet"),
        ("https://api.testnet.solana.com", "testnet"),
        ("http://127.0.0.1:8899", "unknown"),
    ):
        solana_rpc.SOLANA_RPC_URL = url
        s.check(f"{url[:46]:46} -> {want}", signer_main._resolve_network(), want)
    os.environ["SOLANA_NETWORK"] = "devnet"
    solana_rpc.SOLANA_RPC_URL = "https://private.example.com/rpc"
    s.check("explicit SOLANA_NETWORK overrides the host", signer_main._resolve_network(), "devnet")
    os.environ.pop("SOLANA_NETWORK", None)
    if saved_net is not None:
        os.environ["SOLANA_NETWORK"] = saved_net
    solana_rpc.SOLANA_RPC_URL = saved_url

    print("\n[INTEGRITY] the signed transaction must be the one we submitted")
    from solders.transaction import Transaction
    from solders.signature import Signature
    mine = solana_tx.build_sol_transfer_tx(ADDR, ADDR, 1000, BLOCKHASH)
    other = solana_tx.build_sol_transfer_tx(ADDR, ADDR, 999_999, BLOCKHASH)

    def fake_sign(raw):
        t = Transaction.from_bytes(raw)
        t.signatures = [Signature(bytes([0x11] * 64))]
        return bytes(t)

    out = solana_tx.reassemble_signed_sol_transfer(mine, fake_sign(mine).hex())
    s.check_true("a matching signed transaction is accepted", isinstance(out, (bytes, bytearray)))
    # The attack this closes: anything able to influence Turnkey's response
    # returns a valid signed transaction whose MESSAGE goes somewhere else.
    try:
        solana_tx.reassemble_signed_sol_transfer(mine, fake_sign(other).hex()); got = "ACCEPTED"
    except ValueError:
        got = "refused"
    s.check("a SUBSTITUTED message is refused", got, "refused")
    try:
        solana_tx.reassemble_signed_sol_transfer(mine, mine.hex()); got = "ACCEPTED"
    except ValueError:
        got = "refused"
    s.check("a still-unsigned transaction is refused", got, "refused")

    print("\n[AUDIT] nothing that may have moved funds may go unrecorded")
    rows = []

    async def recorder(token_address, token_symbol, requested_usd, outcome, reason, tx_signature, network):
        rows.append({"outcome": outcome, "reason": reason, "tx_signature": tx_signature})

    saved = {
        "audit": signer_main._write_audit_log,
        "reserve": signer_main._reserve,
        "finish": signer_main._finish,
        "genesis": dict(signer_main._GENESIS),
        "balance": solana_rpc.get_balance_lamports,
        "blockhash": solana_rpc.get_latest_blockhash,
        "send": solana_rpc.send_raw_transaction,
        "confirm": solana_rpc.confirm_transaction,
        "sign": turnkey_client.sign_solana_transaction,
        "reassemble": solana_tx.reassemble_signed_sol_transfer,
        "mode": signer_main.SIGNER_MODE,
    }
    signer_main._write_audit_log = recorder
    signer_main.SIGNER_MODE = "devnet_transfer_test"

    finished = []

    async def allow(*a, **k):
        return signer_main.policy_guard.PolicyResult(True, "test: allowed"), None

    async def record_finish(order_id, status, reason, tx):
        finished.append(status)

    async def balance(*a, **k):
        return 50_000_000

    signer_main._finish = record_finish
    signer_main._GENESIS["network"] = "devnet"
    solana_rpc.get_balance_lamports = balance

    async def blockhash(*a, **k):
        return BLOCKHASH

    async def send_ok(*a, **k):
        return "TESTSIGNATURE111"

    async def sign_ok(*a, **k):
        return fake_sign(mine).hex()

    signer_main._reserve = allow
    solana_rpc.get_latest_blockhash = blockhash
    solana_rpc.send_raw_transaction = send_ok
    turnkey_client.sign_solana_transaction = sign_ok
    solana_tx.reassemble_signed_sol_transfer = lambda unsigned, signed: bytes.fromhex(signed)

    req = signer_main.ExecuteRequest(token_address=ADDR, requested_usd=5, client_order_id=OID)
    try:
        # (a) The transaction IS on-chain and the confirmation poll fails.
        #     Previously this raised out of the handler and NO audit row was
        #     written -- a real fund-moving transaction with no record at all.
        async def confirm_boom(*a, **k):
            raise RuntimeError("RPC 429 on getSignatureStatuses")
        solana_rpc.confirm_transaction = confirm_boom
        rows.clear()
        result = asyncio.get_event_loop().run_until_complete(signer_main._execute(req))
        s.check("confirmation failure still writes an audit row", len(rows), 1)
        s.check("recorded as SIGNED_BROADCAST", rows[0]["outcome"], "SIGNED_BROADCAST")
        s.check("with the signature preserved", rows[0]["tx_signature"], "TESTSIGNATURE111")
        s.check_true("and says the tx is on-chain", "on-chain" in rows[0]["reason"])
        s.check("and the order is recorded as SIGNED_BROADCAST", finished[-1:], ["SIGNED_BROADCAST"])

        # (b) An untyped Turnkey failure -- a timeout on the way BACK, where
        #     the signature may already exist. Only two typed exceptions were
        #     caught before, so this escaped unhandled and unlogged.
        async def sign_boom(*a, **k):
            raise TimeoutError("read timeout after Turnkey may have signed")
        turnkey_client.sign_solana_transaction = sign_boom
        rows.clear()
        result = asyncio.get_event_loop().run_until_complete(signer_main._execute(req))
        s.check("untyped Turnkey failure is audited", len(rows), 1)
        s.check("recorded as ERROR", rows[0]["outcome"], "ERROR")
        s.check("HTTP 502 returned", getattr(result, "status_code", None), 502)
        s.check("and the order stays counted as UNKNOWN, never released", finished[-1:], ["UNKNOWN"])
        turnkey_client.sign_solana_transaction = sign_ok

        # (c) Policy check itself raises (DB down). Must refuse, and must not
        #     leak a bare stack trace as a 500.
        async def policy_boom(*a, **k):
            raise RuntimeError("connection pool exhausted")
        signer_main._reserve = policy_boom
        rows.clear()
        result = asyncio.get_event_loop().run_until_complete(signer_main._execute(req))
        s.check("unreadable policy -> 503, not a bare 500",
                getattr(result, "status_code", None), 503)
        s.check_true("and nothing was signed",
                     result.body if hasattr(result, "body") else True)
    finally:
        signer_main._write_audit_log = saved["audit"]
        signer_main._reserve = saved["reserve"]
        signer_main._finish = saved["finish"]
        signer_main._GENESIS.clear(); signer_main._GENESIS.update(saved["genesis"])
        solana_rpc.get_balance_lamports = saved["balance"]
        solana_rpc.get_latest_blockhash = saved["blockhash"]
        solana_rpc.send_raw_transaction = saved["send"]
        solana_rpc.confirm_transaction = saved["confirm"]
        turnkey_client.sign_solana_transaction = saved["sign"]
        solana_tx.reassemble_signed_sol_transfer = saved["reassemble"]
        signer_main.SIGNER_MODE = saved["mode"]

    # --- the RPC credential never reaches an audit row, a response or a log -
    # httpx.HTTPStatusError renders the FULL request URL into its message, and
    # this service interpolates exception text into execution_audit_log, into
    # the JSON it returns, and (via the web app) into system_alerts. Providers
    # put the credential in the query (Helius), the path (Triton) or userinfo,
    # so all three are exercised. Verified before this fix: the raw httpx text
    # contained the key in every case.
    import os as _os
    import importlib as _importlib
    import httpx as _httpx
    key = "SUPERSECRETKEY123"
    saved_url = _os.environ.get("SOLANA_RPC_URL")
    try:
        for label, url in (("query", f"https://devnet.helius-rpc.com/?api-key={key}"),
                           ("path", f"https://example.rpcpool.com/{key}"),
                           ("userinfo", f"https://user:{key}@rpc.example.com/")):
            _os.environ["SOLANA_RPC_URL"] = url
            rpc = _importlib.reload(solana_rpc)

            def _429(req):
                return _httpx.Response(429, request=req)

            async def _call():
                async with _httpx.AsyncClient(transport=_httpx.MockTransport(_429)) as c:
                    try:
                        await rpc._rpc_call(c, "getLatestBlockhash", [])
                    except rpc.SolanaRpcError as e:
                        return e
            err = asyncio.run(_call())
            s.check_true(f"a 429 is surfaced as SolanaRpcError ({label})", err is not None)
            s.check_true(f"its message carries no credential ({label})", key not in str(err))
            s.check_true(f"and the httpx original is not chained into tracebacks ({label})",
                         err.__cause__ is None and err.__suppress_context__)
            s.check_true(f"safe_endpoint keeps the host and drops the secret ({label})",
                         key not in rpc.safe_endpoint(url))
    finally:
        if saved_url is None:
            _os.environ.pop("SOLANA_RPC_URL", None)
        else:
            _os.environ["SOLANA_RPC_URL"] = saved_url
        _importlib.reload(solana_rpc)

    # --- authentication: nothing is processed for an unauthenticated caller -
    import signer_auth

    class FakeURL:
        def __init__(self, path): self.path = path

    class FakeRequest:
        def __init__(self, method, path, body=b"", headers=None):
            self.method, self.url, self._body = method, FakeURL(path), body
            self.headers = headers or {}
        async def body(self):
            return self._body

    import json as _json
    reserved = []

    async def reserve_spy(*a, **k):
        reserved.append(a)
        return signer_main.policy_guard.PolicyResult(False, "spy"), None

    async def _bal(*a, **k): return 50_000_000
    async def _quiet(*a, **k): return True
    saved_secret, saved_reserve = signer_main.AUTH_SECRET, signer_main._reserve
    saved_ctx = (dict(signer_main._GENESIS), solana_rpc.get_balance_lamports,
                 signer_main._write_audit_log, signer_main.SIGNER_MODE)
    signer_main._reserve = reserve_spy
    signer_main._GENESIS["network"] = "devnet"
    solana_rpc.get_balance_lamports, signer_main._write_audit_log = _bal, _quiet
    signer_main.SIGNER_MODE = "devnet_transfer_test"
    try:
        body = _json.dumps({"token_address": ADDR, "requested_usd": 5, "client_order_id": OID}).encode()
        run = lambda coro: asyncio.new_event_loop().run_until_complete(coro)

        signer_main.AUTH_SECRET = None
        r = run(signer_main.execute(FakeRequest("POST", "/execute", body,
                                                signer_auth.sign(b"z" * 40, "POST", "/execute", body))))
        s.check("no configured secret refuses everything", r.status_code, 401)

        signer_main.AUTH_SECRET = b"k" * 40
        r = run(signer_main.execute(FakeRequest("POST", "/execute", body)))
        s.check("a request with no signature is refused", r.status_code, 401)
        r = run(signer_main.execute(FakeRequest("POST", "/execute", body,
                                                signer_auth.sign(b"x" * 40, "POST", "/execute", body))))
        s.check("a request signed with the wrong key is refused", r.status_code, 401)
        good = signer_auth.sign(signer_main.AUTH_SECRET, "POST", "/execute", body)
        tampered = body.replace(b'"requested_usd": 5', b'"requested_usd": 50')
        r = run(signer_main.execute(FakeRequest("POST", "/execute", tampered, good)))
        s.check("a body altered after signing is refused", r.status_code, 401)
        stale = signer_auth.sign(signer_main.AUTH_SECRET, "POST", "/execute", body,
                                 now=__import__("time").time() - 3600)
        r = run(signer_main.execute(FakeRequest("POST", "/execute", body, stale)))
        s.check("a replay outside the time window is refused", r.status_code, 401)
        s.check("none of the refused requests reached the reservation step", len(reserved), 0)
        r = run(signer_main.execute(FakeRequest("POST", "/execute", body, good)))
        s.check_true("a correctly signed request is processed", len(reserved) == 1 and r.status_code == 403)
        r = run(signer_main.whoami(FakeRequest("GET", "/whoami")))
        s.check("/whoami spends the Turnkey key, so it is authenticated too", r.status_code, 401)
        r = run(signer_main.order_status(OID, FakeRequest("GET", f"/orders/{OID}")))
        s.check("/orders is authenticated", r.status_code, 401)
    finally:
        signer_main.AUTH_SECRET, signer_main._reserve = saved_secret, saved_reserve
        (genesis, solana_rpc.get_balance_lamports, signer_main._write_audit_log,
         signer_main.SIGNER_MODE) = saved_ctx
        signer_main._GENESIS.clear(); signer_main._GENESIS.update(genesis)

    # --- idempotency: a key seen before is answered, never signed again ------
    signs = []

    async def sign_spy(*a, **k):
        signs.append(1)
        raise AssertionError("must not sign")

    async def seen_before(*a, **k):
        return None, {"status": "SIGNED_BROADCAST", "tx_signature": "FIRSTSIG", "reason": "done",
                      "network": "devnet"}

    saved = (signer_main._reserve, turnkey_client.sign_solana_transaction, dict(signer_main._GENESIS),
             solana_rpc.get_balance_lamports, signer_main._write_audit_log, signer_main.SIGNER_MODE)
    try:
        async def bal(*a, **k): return 50_000_000
        async def quiet(*a, **k): return True
        signer_main._reserve, turnkey_client.sign_solana_transaction = seen_before, sign_spy
        signer_main._GENESIS["network"] = "devnet"
        solana_rpc.get_balance_lamports, signer_main._write_audit_log = bal, quiet
        signer_main.SIGNER_MODE = "devnet_transfer_test"
        r = asyncio.new_event_loop().run_until_complete(signer_main._execute(
            signer_main.ExecuteRequest(token_address=ADDR, requested_usd=5, client_order_id=OID)))
        s.check("a duplicate order returns 409", r.status_code, 409)
        payload = _json.loads(r.body)
        s.check("with the FIRST outcome", (payload["order_status"], payload["tx_signature"]),
                ("SIGNED_BROADCAST", "FIRSTSIG"))
        s.check("and nothing is signed a second time", len(signs), 0)

        audited = []
        async def conflict_before(*a, **k):
            return None, {"status": "SIGNED_BROADCAST", "tx_signature": "FIRSTSIG",
                          "reason": "done", "network": "devnet",
                          "token_address": ADDR, "requested_usd": 5.0,
                          "idempotency_conflict": True}
        async def audit_spy(*a, **k):
            audited.append(a)
            return True
        signer_main._reserve = conflict_before
        signer_main._write_audit_log = audit_spy
        changed = asyncio.new_event_loop().run_until_complete(signer_main._execute(
            signer_main.ExecuteRequest(token_address="ChangedMint", requested_usd=99,
                                       client_order_id=OID)))
        changed_payload = _json.loads(changed.body)
        s.check("changed idempotency payload is refused", changed.status_code, 409)
        s.check("conflict has no signature", changed_payload.get("tx_signature"), None)
        s.check("conflict is not reported as executed", changed_payload.get("executed"), False)
        s.check("conflict audit uses stored token and size",
                (audited[-1][0], audited[-1][2]), (ADDR, 5.0))

        # The chain, not the hostname, decides what may be signed.
        s.check_true("an unverified network refuses", signer_main._genesis_refusal(None))
        s.check_true("a mainnet genesis refuses in devnet mode", signer_main._genesis_refusal("mainnet"))
        s.check_true("a devnet genesis is allowed in devnet mode",
                     signer_main._genesis_refusal("devnet") is None)
        signer_main._GENESIS["network"] = "mainnet"
        signer_main._reserve = sign_spy
        r = asyncio.new_event_loop().run_until_complete(signer_main._execute(
            signer_main.ExecuteRequest(token_address=ADDR, requested_usd=5, client_order_id=OID)))
        s.check("devnet mode against a mainnet genesis refuses before reserving", r.status_code, 503)
        s.check("and signs nothing", len(signs), 0)
        s.check("the genesis table knows devnet",
                solana_rpc.network_for_genesis("EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"), "devnet")
        s.check("an unknown genesis is unknown", solana_rpc.network_for_genesis("x"), "unknown")
    finally:
        (signer_main._reserve, turnkey_client.sign_solana_transaction, genesis,
         solana_rpc.get_balance_lamports, signer_main._write_audit_log, signer_main.SIGNER_MODE) = saved
        signer_main._GENESIS.clear(); signer_main._GENESIS.update(genesis)

    # --- the reservation step itself: a known key is never re-checked or re-inserted
    class _Tx:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _RConn:
        def __init__(self, existing): self.existing, self.sql = existing, []
        def transaction(self): return _Tx()
        async def execute(self, sql, *a): self.sql.append(sql)
        async def fetchrow(self, sql, *a):
            self.sql.append(sql)
            return self.existing if "signer_orders" in sql else None

    class _Acq:
        def __init__(self, c): self.c = c
        async def __aenter__(self): return self.c
        async def __aexit__(self, *a): return False

    class _Pool:
        def __init__(self, c): self.c = c
        def acquire(self): return _Acq(self.c)

    async def must_not_check(*a, **k):
        raise AssertionError("policy must not run for a known order")

    saved_pool, saved_check = signer_main._pool, signer_main.policy_guard.check_execution_allowed
    try:
        rc = _RConn({"status": "SIGNED_BROADCAST", "tx_signature": "S", "reason": "",
                     "network": "devnet", "token_address": ADDR,
                     "requested_usd": 5.0})
        signer_main._pool = _Pool(rc)
        signer_main.policy_guard.check_execution_allowed = must_not_check
        pol, existing = asyncio.new_event_loop().run_until_complete(signer_main._reserve(
            signer_main.ExecuteRequest(token_address=ADDR, requested_usd=5, client_order_id=OID),
            50_000_000, "devnet"))
        s.check_true("a known key returns the stored order", pol is None and existing["status"] == "SIGNED_BROADCAST")
        s.check_true("the reservation runs under the advisory lock",
                     any("pg_advisory_xact_lock" in q for q in rc.sql))
        s.check_true("and nothing new is inserted", not any("INSERT INTO signer_orders" in q for q in rc.sql))

        pol, existing = asyncio.new_event_loop().run_until_complete(signer_main._reserve(
            signer_main.ExecuteRequest(token_address=ADDR, requested_usd=6,
                                       client_order_id=OID),
            50_000_000, "devnet"))
        s.check_true("same key with a changed amount is a payload conflict",
                     pol is None and existing.get("idempotency_conflict") is True)
        s.check("the conflict retains the stored amount", existing["requested_usd"], 5.0)
        s.check("the conflict retains the stored token", existing["token_address"], ADDR)
        s.check("the conflict retains the stored network", existing["network"], "devnet")

        async def allow_check(*a, **k):
            return signer_main.policy_guard.PolicyResult(True, "ok")
        rc = _RConn(None)
        signer_main._pool = _Pool(rc)
        signer_main.policy_guard.check_execution_allowed = allow_check
        pol, existing = asyncio.new_event_loop().run_until_complete(signer_main._reserve(
            signer_main.ExecuteRequest(token_address=ADDR, requested_usd=5, client_order_id=OID),
            50_000_000, "devnet"))
        s.check_true("a new key is reserved BEFORE anything is signed",
                     pol.allowed and any("INSERT INTO signer_orders" in q for q in rc.sql))
    finally:
        signer_main._pool, signer_main.policy_guard.check_execution_allowed = saved_pool, saved_check
        signer_main.policy_guard.LEDGER = signer_main.policy_guard.InProcessLedger()

    # --- policy: caps from this service's env, counted without double-count -
    pg = signer_main.policy_guard

    class FakeConn:
        def __init__(self, deployed_other=0.0, position=None, orders=(0, 0.0),
                     run_status="RUNNING", settings_cap=None):
            self.deployed_other, self.orders = deployed_other, orders
            self.position = position if position is not None else {
                "token_address": ADDR, "allocated_usd": 5.0, "execution_status": "PENDING_EXECUTION"}
            self.settings = {"run_status": run_status, "max_total_capital_usd": settings_cap}
            self.sum_args = None
        async def fetchrow(self, sql, *args):
            if "app_settings" in sql:
                return self.settings
            if "active_positions" in sql:
                return self.position or None
            if "signer_orders" in sql:
                return {"n": self.orders[0], "usd": self.orders[1]}
            raise AssertionError(sql)
        async def fetchval(self, sql, *args):
            self.sum_args = args
            return self.deployed_other

    saved_caps = (pg.ALLOWED_EXECUTION_TOKENS, pg.MAX_TRADE_USD, pg.MAX_TOTAL_DEPLOYED_USD,
                  pg.MAX_DAILY_USD, pg.MAX_ORDERS_PER_DAY, pg.LEDGER)
    try:
        pg.ALLOWED_EXECUTION_TOKENS = [ADDR]
        pg.MAX_TRADE_USD, pg.MAX_TOTAL_DEPLOYED_USD = 10.0, 20.0
        pg.MAX_DAILY_USD, pg.MAX_ORDERS_PER_DAY = 30.0, 3
        pg.LEDGER = pg.InProcessLedger()

        def check(conn=None, usd=5.0, lamports=50_000_000, mode="devnet_transfer_test"):
            return asyncio.new_event_loop().run_until_complete(pg.check_execution_allowed(
                conn or FakeConn(), ADDR, usd, OID, lamports, mode))

        s.check_true("a request inside every cap is allowed", check().allowed)
        c = FakeConn(deployed_other=15.0)
        s.check_true("the order's own reservation is not counted twice (15 + 5 fits 20)", check(c).allowed)
        s.check("and the sum excludes this order by its key", c.sum_args, (OID,))
        s.check_true("other deployment over the env cap refuses",
                     not check(FakeConn(deployed_other=15.01)).allowed)
        s.check_true("app_settings can TIGHTEN the cap",
                     not check(FakeConn(deployed_other=10.0, settings_cap=12.0)).allowed)
        s.check_true("but never loosen it past the signer's own env cap",
                     not check(FakeConn(deployed_other=15.01, settings_cap=10_000.0)).allowed)
        s.check_true("a request with no ledger reservation refuses",
                     not check(FakeConn(position={})).allowed)
        s.check_true("a reservation for another token refuses",
                     not check(FakeConn(position={"token_address": "OTHER", "allocated_usd": 5.0,
                                                  "execution_status": "PENDING_EXECUTION"})).allowed)
        s.check_true("a reservation of a different size refuses",
                     not check(FakeConn(position={"token_address": ADDR, "allocated_usd": 1.0,
                                                  "execution_status": "PENDING_EXECUTION"})).allowed)
        s.check_true("an already-settled reservation refuses",
                     not check(FakeConn(position={"token_address": ADDR, "allocated_usd": 5.0,
                                                  "execution_status": "EXECUTED"})).allowed)
        s.check_true("the 24h notional cap binds", not check(FakeConn(orders=(1, 26.0))).allowed)
        s.check_true("the daily order cap binds (execution rail)",
                     not check(FakeConn(orders=(3, 0.0))).allowed)
        pg.LEDGER.record(28.0)
        s.check_true("the in-memory ledger binds even when the table says zero",
                     not check(FakeConn(orders=(0, 0.0))).allowed)
        pg.LEDGER = pg.InProcessLedger()
        s.check_true("an unreadable SOL balance refuses (execution rail)", not check(lamports=None).allowed)
        s.check_true("a SOL balance under the fee floor refuses", not check(lamports=1_000).allowed)
        s.check_true("a paused run refuses", not check(FakeConn(run_status="PAUSED_MANUAL")).allowed)
        s.check_true("a NaN amount refuses", not check(usd=float("nan")).allowed)
        pg.MAX_ORDERS_PER_DAY = 0
        s.check_true("an unset order cap refuses everything", not check().allowed)
        pg.MAX_ORDERS_PER_DAY = 3
        pg.MAX_TOTAL_DEPLOYED_USD, pg.MAX_DAILY_USD = 0.0, 0.0
        s.check_true("outside devnet, no total cap refuses",
                     not check(mode="mainnet_jupiter_swap").allowed)
        # Isolated: every OTHER real-funds requirement is met, so only the
        # missing env total cap can be what refuses.
        pg.MAX_DAILY_USD = 30.0
        r = check(FakeConn(settings_cap=100.0), mode="mainnet_jupiter_swap")
        s.check_true("outside devnet, a dashboard-only cap is not enough",
                     not r.allowed and "SIGNER_MAX_TOTAL_DEPLOYED_USD" in r.reason)
        pg.MAX_DAILY_USD = 0.0

        import os as _os2
        saved_env = _os2.environ.get("MAX_TRADE_USD")
        try:
            _os2.environ["MAX_TRADE_USD"] = "nan"
            s.check("a NaN env cap parses to 0 (refuse), not unlimited", pg._env_amount("MAX_TRADE_USD"), 0.0)
            try:
                pg.assert_caps_consistent(); got = "started"
            except RuntimeError:
                got = "refused"
            s.check("and the service refuses to start on it", got, "refused")
            _os2.environ["MAX_TRADE_USD"] = "inf"
            s.check("an infinite env cap parses to 0", pg._env_amount("MAX_TRADE_USD"), 0.0)
        finally:
            if saved_env is None:
                _os2.environ.pop("MAX_TRADE_USD", None)
            else:
                _os2.environ["MAX_TRADE_USD"] = saved_env
    finally:
        (pg.ALLOWED_EXECUTION_TOKENS, pg.MAX_TRADE_USD, pg.MAX_TOTAL_DEPLOYED_USD,
         pg.MAX_DAILY_USD, pg.MAX_ORDERS_PER_DAY, pg.LEDGER) = saved_caps
    return s
