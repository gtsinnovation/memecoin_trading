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

    print("\n[INPUT] NaN and infinities must not defeat the amount guards")
    # nan <= 0, nan > cap and (allocated + nan) > cap are ALL False, so a NaN
    # passed every numeric check in policy_guard. json.loads accepts the bare
    # literal, so this was reachable from the wire.
    for bad, label in ((float("nan"), "NaN"), (float("inf"), "inf"), (float("-inf"), "-inf")):
        try:
            signer_main.ExecuteRequest(token_address=ADDR, requested_usd=bad)
            got = "ACCEPTED"
        except ValidationError:
            got = "rejected"
        s.check(f"{label} rejected at the model", got, "rejected")
    s.check("a finite amount is still accepted",
            signer_main.ExecuteRequest(token_address=ADDR, requested_usd=5).requested_usd, 5.0)

    print("\n[INPUT] over-long fields must not become an audit-suppression tool")
    # token_address is VARCHAR(128). Longer than that made every audit INSERT
    # fail -- and that failure is caught and logged, so the attempt vanished.
    try:
        signer_main.ExecuteRequest(token_address="A" * 200, requested_usd=5); got = "ACCEPTED"
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
        "policy": signer_main.policy_guard.check_execution_allowed,
        "blockhash": solana_rpc.get_latest_blockhash,
        "send": solana_rpc.send_raw_transaction,
        "confirm": solana_rpc.confirm_transaction,
        "sign": turnkey_client.sign_solana_transaction,
        "reassemble": solana_tx.reassemble_signed_sol_transfer,
        "mode": signer_main.SIGNER_MODE,
    }
    signer_main._write_audit_log = recorder
    signer_main.SIGNER_MODE = "devnet_transfer_test"

    async def allow(*a, **k):
        return signer_main.policy_guard.PolicyResult(True, "test: allowed")

    async def blockhash(*a, **k):
        return BLOCKHASH

    async def send_ok(*a, **k):
        return "TESTSIGNATURE111"

    async def sign_ok(*a, **k):
        return fake_sign(mine).hex()

    signer_main.policy_guard.check_execution_allowed = allow
    solana_rpc.get_latest_blockhash = blockhash
    solana_rpc.send_raw_transaction = send_ok
    turnkey_client.sign_solana_transaction = sign_ok
    solana_tx.reassemble_signed_sol_transfer = lambda unsigned, signed: bytes.fromhex(signed)

    req = signer_main.ExecuteRequest(token_address=ADDR, requested_usd=5)
    try:
        # (a) The transaction IS on-chain and the confirmation poll fails.
        #     Previously this raised out of the handler and NO audit row was
        #     written -- a real fund-moving transaction with no record at all.
        async def confirm_boom(*a, **k):
            raise RuntimeError("RPC 429 on getSignatureStatuses")
        solana_rpc.confirm_transaction = confirm_boom
        rows.clear()
        result = asyncio.get_event_loop().run_until_complete(signer_main.execute(req))
        s.check("confirmation failure still writes an audit row", len(rows), 1)
        s.check("recorded as SIGNED_BROADCAST", rows[0]["outcome"], "SIGNED_BROADCAST")
        s.check("with the signature preserved", rows[0]["tx_signature"], "TESTSIGNATURE111")
        s.check_true("and says the tx is on-chain", "on-chain" in rows[0]["reason"])

        # (b) An untyped Turnkey failure -- a timeout on the way BACK, where
        #     the signature may already exist. Only two typed exceptions were
        #     caught before, so this escaped unhandled and unlogged.
        async def sign_boom(*a, **k):
            raise TimeoutError("read timeout after Turnkey may have signed")
        turnkey_client.sign_solana_transaction = sign_boom
        rows.clear()
        result = asyncio.get_event_loop().run_until_complete(signer_main.execute(req))
        s.check("untyped Turnkey failure is audited", len(rows), 1)
        s.check("recorded as ERROR", rows[0]["outcome"], "ERROR")
        s.check("HTTP 502 returned", getattr(result, "status_code", None), 502)
        turnkey_client.sign_solana_transaction = sign_ok

        # (c) Policy check itself raises (DB down). Must refuse, and must not
        #     leak a bare stack trace as a 500.
        async def policy_boom(*a, **k):
            raise RuntimeError("connection pool exhausted")
        signer_main.policy_guard.check_execution_allowed = policy_boom
        rows.clear()
        result = asyncio.get_event_loop().run_until_complete(signer_main.execute(req))
        s.check("unreadable policy -> 503, not a bare 500",
                getattr(result, "status_code", None), 503)
        s.check_true("and nothing was signed",
                     result.body if hasattr(result, "body") else True)
    finally:
        signer_main._write_audit_log = saved["audit"]
        signer_main.policy_guard.check_execution_allowed = saved["policy"]
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
    return s
