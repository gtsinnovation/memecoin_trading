"""main.py -- the runtime layer.

WHY THIS SUITE EXISTS
main.py is the largest file in the project and had no test coverage at all.
It holds the kill-switch gate, the signer gate, session authentication and
everything the operator actually looks at to decide whether the system is
healthy. Every defect found in it during the September audit had the same
shape: a failure that rendered as a reassuring value rather than an error --
a dashboard reading RUNNING because the database was unreachable, a session
that stayed valid after its account was revoked, a kill switch whose verdict
was computed and discarded.

None of those raise. All of them look fine.
"""
from tests.harness import Suite


class _FakeSession(dict):
    def clear(self):
        super().clear()


class _FakeRequest:
    """Enough of a Starlette Request for _require_user."""
    def __init__(self, session=None):
        self.session = _FakeSession(session or {})


def run(main) -> Suite:
    s = Suite("main.py runtime")

    # --- kill-switch gate: an unverifiable gate must pause ---
    s.check_true("an open gate does not pause",
                 main.kill_switch_pause_reason({"gate_open": True, "reason": None}) is None)
    s.check_true("unreadable settings must pause",
                 bool(main.kill_switch_pause_reason({"gate_open": False, "reason": "SETTINGS_UNAVAILABLE"})))
    s.check_true("an unknown reason must pause",
                 bool(main.kill_switch_pause_reason({"gate_open": False, "reason": "UNKNOWN"})))
    s.check_true("a missing verdict entirely must pause",
                 bool(main.kill_switch_pause_reason(None)))
    s.check_true("an empty verdict must pause", bool(main.kill_switch_pause_reason({})))
    # A gate the DB already recorded needs no second pause -- run_status is
    # the source of truth and re-pausing would overwrite its reason.
    for recorded in ("PAUSED_KILL_SWITCH", "PAUSED_DURATION_ELAPSED", "PAUSED_MANUAL"):
        s.check_true(f"{recorded} is already recorded, so no second pause",
                     main.kill_switch_pause_reason({"gate_open": False, "reason": recorded}) is None)
    s.check_true("the pause reason says why, not just that",
                 "unreadable" in (main.kill_switch_pause_reason({"gate_open": False}) or ""))

    # --- signer gate: four independent refusals ---
    live = {"position_logged": True, "token_address": "MintAddr111", "token_symbol": "T",
            "max_safe_position_usd": 25.0, "client_order_id": "0b9d2c1e-order-key"}
    payload, refusal = main.signer_request_or_reason(live, True)
    s.check_true("a complete live state produces a payload", payload is not None and refusal is None)
    s.check("the payload carries the size", payload["requested_usd"], 25.0)
    s.check("the payload carries the mint", payload["token_address"], "MintAddr111")

    s.check_true("execution disabled sends nothing",
                 main.signer_request_or_reason(live, False)[0] is None)
    s.check_true("no position opened sends nothing",
                 main.signer_request_or_reason({**live, "position_logged": False}, True)[0] is None)
    s.check_true("a missing mint sends nothing",
                 main.signer_request_or_reason({**live, "token_address": None}, True)[0] is None)
    s.check_true("a missing size sends nothing",
                 main.signer_request_or_reason({**live, "max_safe_position_usd": None}, True)[0] is None)
    s.check_true("a non-numeric size sends nothing",
                 main.signer_request_or_reason({**live, "max_safe_position_usd": "lots"}, True)[0] is None)
    s.check_true("a zero size sends nothing",
                 main.signer_request_or_reason({**live, "max_safe_position_usd": 0}, True)[0] is None)
    s.check_true("a negative size sends nothing",
                 main.signer_request_or_reason({**live, "max_safe_position_usd": -5}, True)[0] is None)
    s.check("the payload carries the idempotency key", payload["client_order_id"], "0b9d2c1e-order-key")
    s.check_true("an unkeyed order sends nothing",
                 main.signer_request_or_reason({**live, "client_order_id": None}, True)[0] is None)
    s.check_true("an infinite size sends nothing",
                 main.signer_request_or_reason({**live, "max_safe_position_usd": float("inf")}, True)[0] is None)
    s.check_true("a NaN size sends nothing",
                 main.signer_request_or_reason({**live, "max_safe_position_usd": float("nan")}, True)[0] is None)
    s.check_true("an empty state sends nothing", main.signer_request_or_reason({}, True)[0] is None)
    s.check_true("a None state sends nothing", main.signer_request_or_reason(None, True)[0] is None)
    s.check_true("every refusal explains itself",
                 all(main.signer_request_or_reason(st, en)[1]
                     for st, en in [(live, False), ({}, True), (None, True)]))

    # --- dashboard must not report health it cannot verify ---
    known = main.run_state_payload({"run_status": "RUNNING", "run_status_reason": None})
    s.check("a readable row reports its status", known["run_status"], "RUNNING")
    s.check_true("and is flagged as known", known["run_status_known"])

    unknown = main.run_state_payload(None)
    s.check("an unreadable row must NOT report RUNNING", unknown["run_status"], "UNKNOWN")
    s.check_true("and must be flagged as unknown", not unknown["run_status_known"])
    s.check_true("an empty row is also unknown, not RUNNING",
                 main.run_state_payload({})["run_status"] == "UNKNOWN")
    s.check("a paused row reports the pause, not RUNNING",
            main.run_state_payload({"run_status": "PAUSED_KILL_SWITCH"})["run_status"],
            "PAUSED_KILL_SWITCH")
    s.check_true("the pause reason is carried through",
                 main.run_state_payload({"run_status": "PAUSED_KILL_SWITCH",
                                         "run_status_reason": "loss limit"})["run_status_reason"] == "loss limit")

    # --- session: the allowlist is re-checked on every request ---
    authorised = main.AUTHORIZED_GOOGLE_EMAIL or "operator@example.com"
    main.AUTHORIZED_GOOGLE_EMAIL = authorised
    try:
        s.check_true("no session means no user", main._require_user(_FakeRequest()) is None)
        s.check_true("the authorised address is accepted",
                     main._require_user(_FakeRequest({"user": {"email": authorised}})) is not None)
        s.check_true("a DIFFERENT address is rejected even with a valid cookie",
                     main._require_user(_FakeRequest({"user": {"email": "someone.else@example.com"}})) is None)
        # Rotating the allowlist must eject the old holder immediately.
        req = _FakeRequest({"user": {"email": authorised}})
        main.AUTHORIZED_GOOGLE_EMAIL = "new.operator@example.com"
        s.check_true("rotating the authorised address revokes the old session",
                     main._require_user(req) is None)
        s.check_true("and the stale session is cleared, not merely refused", not req.session)
        # An empty allowlist must refuse everyone rather than admit anyone.
        main.AUTHORIZED_GOOGLE_EMAIL = ""
        s.check_true("an unset allowlist admits nobody",
                     main._require_user(_FakeRequest({"user": {"email": authorised}})) is None)
        # The case an equality check alone misses: when BOTH the allowlist and
        # the session's address are empty they compare equal, so a guard
        # written as `email != AUTHORIZED` admits a session carrying no
        # address at all. Misconfiguration must never become an open door.
        for empty, label in [({"user": {"email": ""}}, "an empty address"),
                             ({"user": {}}, "a session with no address field"),
                             ({"user": None}, "a null user")]:
            s.check_true(f"an unset allowlist refuses {label}",
                         main._require_user(_FakeRequest(empty)) is None)
        main.AUTHORIZED_GOOGLE_EMAIL = authorised
        s.check_true("a session storing a bare string is handled, not crashed",
                     main._require_user(_FakeRequest({"user": "not-a-dict"})) is None)
        s.check_true("address comparison ignores case and whitespace",
                     main._require_user(_FakeRequest({"user": {"email": f"  {authorised.upper()}  "}})) is not None)
    finally:
        main.AUTHORIZED_GOOGLE_EMAIL = authorised

    # --- the stop-proximity gauge must not fabricate comfort ---
    s.check("a real gap is measured", main._invalidation_proximity_percent(100.0, 86.0), 14.0)
    for price, stop, label in [
        (None, 86.0, "a missing price"), (100.0, None, "a missing stop"),
        (0.0, 86.0, "a zero price"), (100.0, 0.0, "a zero stop"),
        (-1.0, 86.0, "a negative price"), ("x", 86.0, "a non-numeric price"),
    ]:
        s.check_true(f"{label} yields None, not a comfortable number",
                     main._invalidation_proximity_percent(price, stop) is None)
    s.check_true("a price at its stop reads as zero proximity",
                 main._invalidation_proximity_percent(86.0, 86.0) == 0.0)

    # --- the session cookie must not travel in clear by default ---
    s.check_true("SESSION_COOKIE_INSECURE defaults to off",
                 hasattr(main, "_COOKIE_INSECURE"))
    s.check_true("only an explicit opt-in disables Secure",
                 main._COOKIE_INSECURE in (True, False))

    print("\n[KILL SWITCH] a gate that could not be EVALUATED must pause")
    # GATE_CHECK_FAILED is what check_kill_switch returns from its own except
    # handler: the thresholds were never computed and no run_status was
    # written. Treating it as "shut and already recorded" meant a statement
    # timeout silently disabled the loss limiter while positions kept opening.
    s.check_true("GATE_CHECK_FAILED pauses rather than proceeding",
                 bool(main.kill_switch_pause_reason(
                     {"gate_open": False, "reason": "GATE_CHECK_FAILED"})))
    s.check_true("and the reason says the state was unreadable, not that a limit tripped",
                 "unreadable" in (main.kill_switch_pause_reason(
                     {"gate_open": False, "reason": "GATE_CHECK_FAILED"}) or "").lower())

    # THE INVARIANT. Every reason check_kill_switch can produce must either be
    # in the unverified tuple, or be a PAUSED_* that wrote its own run_status.
    # A new failure reason added there and not here reopens this hole silently.
    import ast as _ast, os as _os, re as _re
    engine_src = open(_os.path.join(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))), "engine.py"), encoding="utf-8").read()
    fn_start = engine_src.index("def check_kill_switch")
    fn_end = engine_src.index("\ndef ", fn_start + 10)
    reasons = set(_re.findall(r'"reason": "([A-Z_]+)"', engine_src[fn_start:fn_end]))
    s.check_true(f"check_kill_switch's reasons were found ({sorted(reasons)})", len(reasons) >= 2)
    unhandled = sorted(r for r in reasons
                       if r not in main.UNVERIFIED_GATE_REASONS and not r.startswith("PAUSED_"))
    s.check("every kill-switch reason is either unverified-and-paused, or self-recording",
            unhandled, [])


    # --- a NaN or inf anywhere in a snapshot means it is not evaluated -----
    # float("NaN") parses cleanly and every comparison against it is False, so
    # it passes "refuse if <= 0" AND "refuse if above the ceiling" at once.
    # Traced through the gates it sizes a position at NaN and poisons the
    # SUM()s behind the capital cap and the kill switch permanently.
    nf = main.non_finite_fields
    s.check("a clean snapshot has no bad fields",
            nf({"price_usd": 1.2, "tradeable_depth_usd": 50000, "token_symbol": "X"}), [])
    s.check("NaN slippage is caught", nf({"estimated_slippage_percent": float("nan")}),
            ["estimated_slippage_percent"])
    s.check("+inf depth is caught", nf({"tradeable_depth_usd": float("inf")}),
            ["tradeable_depth_usd"])
    s.check("-inf is caught too", nf({"price_change_m5": float("-inf")}), ["price_change_m5"])
    s.check("every offending field is named, sorted",
            nf({"b": float("nan"), "a": float("inf"), "c": 1.0}), ["a", "b"])
    s.check("a measured zero is fine -- zero is a number, NaN is not",
            nf({"estimated_slippage_percent": 0.0}), [])
    s.check("booleans are not numbers here (flags must never be flagged)",
            nf({"_slippage_data_missing": True, "holder_data_missing": False}), [])
    s.check("None is an absence, handled by the gates' own rules, not this one",
            nf({"estimated_slippage_percent": None}), [])
    s.check("a non-dict is not crashed on", nf(None), [])

    # --- /health: a stalled pipeline must LOOK stalled ----------------------
    # Collection stopped for ~2 days and nothing noticed. The heartbeat is
    # stamped at the top of every pipeline iteration, so a dead worker and a
    # hung one both leave it stale -- which is what this reports.
    hb = main._PIPELINE_HEARTBEAT
    saved = hb["at"]
    try:
        hb["at"] = None
        ok, d = main.pipeline_health(now=1000.0)
        s.check("no tick yet reports starting, and is NOT healthy",
                (ok, d["status"]), (False, "starting"))
        hb["at"] = 1000.0
        ok, d = main.pipeline_health(now=1005.0)
        s.check("a recent tick is healthy", (ok, d["status"]), (True, "ok"))
        ok, d = main.pipeline_health(now=1000.0 + main.HEALTH_MAX_TICK_AGE_S + 1)
        s.check("a tick older than the limit is STALLED", (ok, d["status"]), (False, "stalled"))
        s.check_true("and says how stale, so the log line is actionable",
                     d["last_tick_age_s"] > main.HEALTH_MAX_TICK_AGE_S)
        ok, _ = main.pipeline_health(now=1000.0 + main.HEALTH_MAX_TICK_AGE_S)
        s.check_true("exactly at the limit is still healthy (strictly greater fails)", ok)
    finally:
        hb["at"] = saved

    # --- watchdog: a stall or a dead worker must RECOVER, not just show -----
    hb = main._PIPELINE_HEARTBEAT
    saved, saved_deaths = hb["at"], list(main._WORKER_DEATHS)
    try:
        main._WORKER_DEATHS.clear()
        hb["at"] = None
        s.check_true("no iteration yet, inside the startup grace: no exit",
                     main.watchdog_verdict(now=100.0, started_at=0.0) is None)
        s.check_true("no iteration ever, past the grace: exit",
                     main.watchdog_verdict(now=main.WATCHDOG_STARTUP_GRACE_S + 1, started_at=0.0))
        hb["at"] = 1000.0
        s.check_true("a recent iteration: no exit",
                     main.watchdog_verdict(now=1010.0, started_at=0.0) is None)
        s.check_true("a stale heartbeat: exit",
                     main.watchdog_verdict(now=1000.0 + main.WATCHDOG_EXIT_AFTER_S + 1, started_at=0.0))
        s.check_true("the exit threshold is past the health threshold, so a stall is seen first",
                     main.WATCHDOG_EXIT_AFTER_S > main.HEALTH_MAX_TICK_AGE_S)
        main._WORKER_DEATHS.append("pipeline")
        s.check_true("a dead worker: exit even with a fresh heartbeat",
                     "pipeline" in (main.watchdog_verdict(now=1001.0, started_at=0.0) or ""))
    finally:
        hb["at"] = saved
        main._WORKER_DEATHS[:] = saved_deaths

    # --- a slow dashboard viewer must not stall the trading loop -----------
    import asyncio as _aio0, time as _t0

    class _WS:
        def __init__(self, mode): self.mode, self.got, self.closed = mode, [], False
        async def send_text(self, p):
            if self.mode == "hang":
                await _aio0.sleep(3600)
            if self.mode == "boom":
                raise RuntimeError("socket gone")
            self.got.append(p)
        async def close(self): self.closed = True

    mgr = main.WebSocketConnectionManager()
    mgr.SEND_TIMEOUT_S = 0.2
    good, hang, boom = _WS("ok"), _WS("hang"), _WS("boom")
    mgr.active_connections[:] = [good, hang, boom]
    t0 = _t0.monotonic()

    async def _bounded():
        try:
            await _aio0.wait_for(mgr.broadcast({"x": 1}), timeout=5.0)
            return True
        except _aio0.TimeoutError:
            return False
    finished = _aio0.run(_bounded())
    s.check_true("a hung client cannot hold the broadcast past its timeout",
                 finished and _t0.monotonic() - t0 < 2.0)
    s.check("the healthy client still got the message", len(good.got), 1)
    s.check_true("hung and failed clients are dropped",
                 mgr.active_connections == [good])
    mgr.disconnect(hang)   # already gone -- must not raise
    s.check_true("disconnect is idempotent", True)

    # --- Stage 3 ledger lifecycle -------------------------------------------
    f = main.execution_fate
    s.check("a broadcast order is EXECUTED", f(200, {"order_status": "SIGNED_BROADCAST"}), "EXECUTED")
    s.check("a refused order is VOID", f(403, {"order_status": "REFUSED"}), "VOID")
    s.check("a pre-reservation refusal is VOID", f(401, {"executed": False}), "VOID")
    s.check("a policy outage before reserving is VOID", f(503, {}), "VOID")
    s.check("a reserved-but-unfinished order is UNKNOWN", f(502, {"order_status": "UNKNOWN"}), "UNKNOWN")
    s.check("a 500 with no order status is UNKNOWN, never VOID", f(500, {}), "UNKNOWN")
    s.check("an unparseable body is UNKNOWN", f(200, None), "UNKNOWN")
    s.check("order_status outranks the HTTP code", f(403, {"order_status": "SIGNED_BROADCAST"}), "EXECUTED")

    import asyncio as _aio, json as _json, httpx as _httpx
    import signer_auth as _sa
    conn = main.db_connect()
    conn.autocommit = True

    def reserve(addr):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO active_positions (token_symbol, token_address, allocated_usd, "
                        "entry_trigger, execution_status) VALUES ('S', %s, 10, 1.0, "
                        "'PENDING_EXECUTION') RETURNING client_order_id::text;", (addr,))
            return cur.fetchone()[0]

    def status_of(order_id):
        with conn.cursor() as cur:
            cur.execute("SELECT execution_status, tx_signature FROM active_positions "
                        "WHERE client_order_id::text = %s;", (order_id,))
            return cur.fetchone()

    saved_secret, saved_enabled = main.SIGNER_SHARED_SECRET, main.ENABLE_STAGE3_EXECUTION
    seen = []
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE active_positions;")
        main.SIGNER_SHARED_SECRET = b"k" * 40
        main.ENABLE_STAGE3_EXECUTION = True

        def signer(reply_status, reply_body):
            def handler(req):
                body = req.content
                ok, why = _sa.verify(main.SIGNER_SHARED_SECRET, req.method, req.url.path, body,
                                     req.headers.get(_sa.HEADER_TS), req.headers.get(_sa.HEADER_SIG))
                seen.append((req.method, req.url.path, ok, _json.loads(body) if body else None))
                return _httpx.Response(reply_status, json=reply_body)
            return _httpx.AsyncClient(transport=_httpx.MockTransport(handler))

        async def go(client, order_id, addr):
            async with client:
                await main.maybe_execute_via_signer(client, {
                    "position_logged": True, "token_address": addr, "token_symbol": "S",
                    "max_safe_position_usd": 10.0, "client_order_id": order_id})

        oid = reserve("LIFE1")
        _aio.run(go(signer(200, {"executed": True, "order_status": "SIGNED_BROADCAST",
                                 "tx_signature": "SIG1", "network": "devnet"}), oid, "LIFE1"))
        s.check_true("the request reached the signer HMAC-authenticated", seen and seen[-1][2])
        s.check("and carried the idempotency key", seen[-1][3]["client_order_id"], oid)
        s.check("an executed order becomes EXECUTED with its signature", status_of(oid), ("EXECUTED", "SIG1"))

        oid = reserve("LIFE2")
        _aio.run(go(signer(403, {"executed": False, "order_status": "REFUSED", "reason": "cap"}), oid, "LIFE2"))
        s.check("a refused order leaves NO phantom position", status_of(oid), None)

        oid = reserve("LIFE3")
        _aio.run(go(signer(502, {"executed": False, "order_status": "UNKNOWN"}), oid, "LIFE3"))
        s.check("an unknown outcome is held, not dropped and not assumed",
                status_of(oid), ("EXECUTION_UNKNOWN", None))

        def boom(req):
            raise _httpx.ConnectError("signer down")
        oid4 = reserve("LIFE4")
        _aio.run(go(_httpx.AsyncClient(transport=_httpx.MockTransport(boom)), oid4, "LIFE4"))
        s.check("a transport failure is UNKNOWN (the request may have landed)",
                status_of(oid4)[0], "EXECUTION_UNKNOWN")

        main.SIGNER_SHARED_SECRET = None
        oid5 = reserve("LIFE5")
        n = len(seen)
        _aio.run(go(signer(200, {"order_status": "SIGNED_BROADCAST"}), oid5, "LIFE5"))
        s.check("with no shared secret nothing is sent", len(seen), n)
        s.check("and the reservation is released", status_of(oid5), None)
        main.SIGNER_SHARED_SECRET = b"k" * 40

        # Reconciliation asks read-only and settles by the answer.
        def recon_handler(req):
            ok, _ = _sa.verify(main.SIGNER_SHARED_SECRET, req.method, req.url.path, b"",
                               req.headers.get(_sa.HEADER_TS), req.headers.get(_sa.HEADER_SIG))
            seen.append((req.method, req.url.path, ok, None))
            if req.url.path.endswith(oid):
                return _httpx.Response(200, json={"order_status": "SIGNED_BROADCAST", "tx_signature": "SIG3"})
            return _httpx.Response(404, json={"found": False})

        async def recon():
            async with _httpx.AsyncClient(transport=_httpx.MockTransport(recon_handler)) as c:
                return await main.reconcile_pending_executions(c, now=1e9)
        main._RECONCILE_STATE["at"] = None
        settled = _aio.run(recon())
        s.check("reconciliation settles both unknown orders", settled, 2)
        s.check_true("using GET only -- it can never cause a signature",
                     all(m == "GET" for m, p, ok, b in seen[-2:]) and all(ok for m, p, ok, b in seen[-2:]))
        s.check("a signer-confirmed order becomes EXECUTED", status_of(oid), ("EXECUTED", "SIG3"))
        s.check("an order the signer never recorded is released", status_of(oid4), None)
        s.check("a second pass inside the interval does nothing", _aio.run(recon()), 0)
    finally:
        main.SIGNER_SHARED_SECRET, main.ENABLE_STAGE3_EXECUTION = saved_secret, saved_enabled
        with conn.cursor() as cur:
            cur.execute("TRUNCATE active_positions;")
        conn.close()
    return s
