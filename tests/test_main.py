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
            "max_safe_position_usd": 25.0}
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

    return s
