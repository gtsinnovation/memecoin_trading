"""Regression cover for the hardening pass.

Each assertion names a defect that shipped. None of them were crashes -- every
one produced a plausible number or a plausible-looking healthy state, which is
why they survived until an audit rather than until the next run.

Pure functions only: no database, no network, no container.
"""
from tests.harness import Suite

import paper_trading as pt
import execution_rails as rails


def _ledger_uses_barrier_exit() -> bool:
    """engine.evaluate_open_positions calls paper_trading.barrier_exit and
    never assigns the stop level as the exit price. Parsed, not imported:
    engine needs psycopg2 and langgraph, and this suite must run without."""
    import ast as _ast, os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    tree = _ast.parse(open(_os.path.join(root, "engine.py"), encoding="utf-8").read())
    fn = next((n for n in tree.body if isinstance(n, _ast.FunctionDef)
               and n.name == "evaluate_open_positions"), None)
    if fn is None:
        return False
    calls = {n.func.attr for n in _ast.walk(fn)
             if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)}
    stop_as_fill = any(
        isinstance(n, _ast.Assign) and "invalidation_level_price" in _ast.unparse(n.value)
        and any("next_price" in _ast.unparse(t) for t in n.targets)
        and "barrier_exit" not in _ast.unparse(n.value)
        for n in _ast.walk(fn))
    return "barrier_exit" in calls and "net_pnl_percent" in calls and not stop_as_fill


def _deploy_hardening() -> list:
    """Container and credential settings that must not quietly regress.

    Each was a real exposure: a public default Postgres password, root
    processes in both images, and a signing endpoint anyone on the network
    could call. They are configuration, so nothing else would catch a revert.
    """
    import os as _os, re as _re
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    read = lambda rel: open(_os.path.join(root, rel), encoding="utf-8").read()
    compose, web_df, signer_df = (read("docker-compose.yml"), read("Dockerfile"),
                                  read("signer_service/Dockerfile"))
    problems = []
    if _re.search(r"POSTGRES_PASSWORD:-", compose):
        problems.append("compose gives POSTGRES_PASSWORD a fallback value")
    if compose.count("POSTGRES_PASSWORD:?") < 2:
        problems.append("POSTGRES_PASSWORD is not required in both db and web")
    if not _re.search(r'"127\.0\.0\.1:5432:5432"', compose):
        problems.append("Postgres is published beyond loopback")
    for name, df in (("web", web_df), ("signer", signer_df)):
        if not _re.search(r"^USER\s+\S+", df, _re.M):
            problems.append(f"{name} image runs as root")
    if "signer_auth.py" not in signer_df:
        problems.append("signer image does not ship signer_auth.py")
    if compose.count("no-new-privileges:true") < 2:
        problems.append("web and signer must both set no-new-privileges")
    if "max-size" not in compose:
        problems.append("container logs are not rotated")
    return problems


def _stray_percent_signs():
    """Percent signs in a query string that psycopg2 will read as parameters.

    psycopg2 scans the WHOLE query for parameter syntax -- SQL comments
    included -- and C-style format flags are legal, so a percent sign followed
    by a space and an "s" parses as a space-flagged placeholder. Writing
    "62 percent staleness" as digits and a symbol inside an explanatory comment
    therefore added a THIRD parameter to a two-parameter query, and the only
    symptom was "IndexError: tuple index out of range" from a function whose
    SQL looked obviously correct. Spell percentages out in words.

    Only a bare percent sign is a problem: "%s" is a placeholder and "%%" is an
    escaped literal. Everything else is flagged.
    """
    import ast as _ast, re as _re, glob as _glob, os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    bad = []
    for path in sorted(_glob.glob(_os.path.join(root, "*.py"))):
        try:
            tree = _ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in _ast.walk(tree):
            if not (isinstance(node, _ast.Call)
                    and isinstance(node.func, _ast.Attribute)
                    and node.func.attr == "execute" and node.args):
                continue
            q = node.args[0]
            if not (isinstance(q, _ast.Constant) and isinstance(q.value, str)):
                continue
            for m in _re.finditer(r"%(.)", q.value):
                if m.group(1) not in ("s", "%"):
                    bad.append(f"{_os.path.basename(path)}:{node.lineno}")
    return sorted(set(bad))

def _schema_parity():
    """(indexes only in schema.sql, indexes only in migrate.sql, bad tables).

    The test database is built from schema.sql. The LIVE database is only ever
    upgraded by migrate.sql. Anything one defines and the other does not is a
    difference between what the tests exercise and what production runs -- and
    no test that builds from schema.sql can ever see it. Two retention indexes
    lived only in schema.sql for exactly this reason, and every live retention
    sweep ran as a sequential scan while the suite stayed green.
    """
    import re as _re, os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

    def read(name):
        return open(_os.path.join(root, name), encoding="utf-8").read()

    idx = _re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)", _re.I)
    tbl = _re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)", _re.I)
    schema, migrate = read("schema.sql"), read("migrate.sql")
    a, b = set(idx.findall(schema)), set(idx.findall(migrate))
    # The original base tables predate migrate.sql, which only ALTERs them.
    # Every table added SINCE must be creatable by migrate.sql.
    base = {"active_positions", "system_alerts", "trading_sessions"}
    missing_tables = sorted(set(tbl.findall(schema)) - set(tbl.findall(migrate)) - base)
    return sorted(a - b), sorted(b - a), missing_tables

def _compose_coverage():
    """Every knob the RUNTIME modules read, minus what compose forwards."""
    import ast as _ast, os as _os, glob as _glob
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    compose = open(_os.path.join(root, "docker-compose.yml"), encoding="utf-8").read()
    # Probes, smoke scripts and one-off calibration tools are not shipped in
    # the running container, so their knobs are not compose's business.
    skip_prefix = ("probe_", "smoke", "explore_", "calibrate_", "gmgn_diagnose",
                   "test_provider")
    missing = []
    for path in sorted(_glob.glob(_os.path.join(root, "*.py"))):
        name = _os.path.basename(path)
        if name.startswith(skip_prefix):
            continue
        text = open(path, encoding="utf-8").read()
        # AST, not a regex. The regex required the opening quote to sit
        # immediately after the paren, so a call wrapped across lines --
        #     os.environ.get(
        #         "REPLAY_STOPS", "...")
        # -- was invisible to it. Two of three new knobs slipped past that way
        # while the check still reported a clean sweep, which is worse than no
        # check: it is a check that says yes.
        try:
            tree = _ast.parse(text)
        except SyntaxError:
            continue
        for node in _ast.walk(tree):
            if not (isinstance(node, _ast.Call)
                    and isinstance(node.func, _ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, _ast.Attribute)
                    and node.func.value.attr == "environ"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, _ast.Constant)
                    and isinstance(first.value, str)):
                continue
            knob = first.value
            if not knob or not knob[0].isupper() or knob.upper() != knob:
                continue
            if knob not in compose:
                missing.append(f"{name}:{knob}")
    return sorted(set(missing))


def run() -> Suite:
    s = Suite("hardening")

    # --- schema.sql and migrate.sql must describe the same database --------
    only_schema, only_migrate, missing_tables = _schema_parity()
    s.check("no index exists only in schema.sql (the live DB would lack it)",
            only_schema, [])
    s.check("no index exists only in migrate.sql (the tests would lack it)",
            only_migrate, [])
    s.check("every post-baseline table can be created by migrate.sql",
            missing_tables, [])

    # --- no bare percent signs inside query strings ------------------------
    s.check("no query string contains a percent sign psycopg2 would misread",
            _stray_percent_signs(), [])

    # --- an exit needs a counterparty, not just a quote --------------------
    # A take-profit is a LIMIT SELL. On a token that has not traded in five
    # minutes the quote is the last print, not a price anyone will pay, so a
    # lone stale or wicked figure crossing the target was booked as a clean
    # win at the target with no trade behind it. The error is not symmetric:
    # the gates select thin tokens, where one print moves the quote furthest,
    # so the fabricated wins land disproportionately in APPROVED.
    s.check("transactions at the mark confirm the exit", pt.exit_is_confirmed(4), True)
    s.check("zero transactions refute it", pt.exit_is_confirmed(0), False)
    s.check("no data at all is unknown, not refuted", pt.exit_is_confirmed(None), None)
    # h1 may only REFUTE. An hour-old count is weak evidence about the last
    # five minutes -- but if nothing traded all hour, nothing traded just now.
    s.check("a silent hour refutes when m5 is missing",
            pt.exit_is_confirmed(None, 0), False)
    s.check("a busy hour does NOT confirm a silent five minutes",
            pt.exit_is_confirmed(0, 900), False)
    s.check("a busy hour alone stays unknown", pt.exit_is_confirmed(None, 900), None)
    # Unknown must not be laundered into confirmed anywhere downstream: the
    # analysis filters on `IS TRUE`, so both None and False have to fall out.
    s.check_true("unknown is not truthy", pt.exit_is_confirmed(None) is not True)

    # A bare price carries no evidence and must land as unknown -- never as
    # confirmed, which would restore the defect for every legacy caller.
    s.check("a bare price yields no evidence", pt._as_mark(1.5), (1.5, None, None))
    s.check("a full mark is unpacked",
            pt._as_mark({"price": 2.0, "txns_m5": 7, "txns_h1": 40}), (2.0, 7, 40))
    s.check("a mark missing its counts is unknown, not zero",
            pt._as_mark({"price": 2.0}), (2.0, None, None))

    # --- compose forwards every knob, across the WHOLE codebase ------------
    # The existing check covered token_discovery.py only, and passed while
    # sixteen knobs elsewhere were unforwarded -- among them the horizon set,
    # the LIMIT fill window and both abandonment clocks. Compose forwards ONLY
    # what it names: an unlisted variable does not error, it silently keeps
    # its code default. So the knob can be set in .env, verified by eye, and
    # change nothing -- indistinguishable from the knob not working, which is
    # the kind of thing that gets "fixed" by setting it again.
    s.check("every knob every shipped module reads is forwarded by compose",
            _compose_coverage(), [])
    s.check("deployment hardening holds (no default password, non-root images, signer auth shipped)",
            _deploy_hardening(), [])

    # --- unmeasured slippage must never be free ---
    fee_only = round(pt.PAPER_FEE_PERCENT_PER_SIDE * 2.0, 4)
    s.check_true("a measured zero slippage costs fees only",
                 pt.total_cost_percent(0.0) == fee_only)
    s.check_true("UNMEASURED slippage costs more than fees alone",
                 pt.total_cost_percent(None) > fee_only)
    s.check_true("unmeasured is charged at the documented stand-in",
                 pt.total_cost_percent(None)
                 == round(fee_only + 2 * pt.PAPER_UNMEASURED_SLIPPAGE_PERCENT, 4))
    s.check_true("a measured 1% still costs less than an unmeasured one",
                 pt.total_cost_percent(1.0) < pt.total_cost_percent(None))
    s.check_true("negative slippage is charged as its magnitude",
                 pt.total_cost_percent(-1.0) == pt.total_cost_percent(1.0))
    # The bias this removes: unmeasured impact is commonest on thin tokens,
    # so charging it zero manufactured part of the cohort gap.
    s.check_true("net P&L on an unmeasured trade is worse than on a measured zero",
                 pt.net_pnl_percent(1.0, 1.1, None)[2] < pt.net_pnl_percent(1.0, 1.1, 0.0)[2])

    # --- the left tail must be representable ---
    # decide_exit is where the asymmetry lives: a take-profit is a limit sell
    # and fills AT the level; a stop triggers at the level and fills at the
    # market, which on this asset class is routinely far below.
    reason, exit_price = pt.decide_exit(0.01, 1.15, 0.925, 5)
    s.check("a gap through the stop is still a stop-out", reason, "STOPPED_OUT")
    s.check_true("a gap through the stop fills at the market, not the stop",
                 exit_price == 0.01)
    reason, exit_price = pt.decide_exit(0.90, 1.15, 0.925, 5)
    s.check_true("a normal stop-out fills at the stop", exit_price == 0.90)
    reason, exit_price = pt.decide_exit(1.40, 1.15, 0.925, 5)
    s.check("trading through the target is a target hit", reason, "TARGET_HIT")
    s.check_true("a take-profit fills AT the target, never better",
                 exit_price == 1.15)
    s.check_true("no barrier and no timeout means no exit",
                 pt.decide_exit(1.0, 1.15, 0.925, 5)[0] is None)
    s.check("an aged-out trade times out",
            pt.decide_exit(1.0, 1.15, 0.925, pt.MAX_HOLD_MINUTES + 1)[0], "TIMEOUT")
    s.check_true("a missing stop cannot trigger a stop-out",
                 pt.decide_exit(0.01, 1.15, None, 5)[0] is None)
    # The realised loss must actually reach the tail.
    s.check_true("a rug books near total loss, not the stop distance",
                 pt.net_pnl_percent(1.0, pt.decide_exit(0.01, 1.15, 0.925, 5)[1], 0.0)[0] < -90.0)
    # The live ledger uses barrier_exit (no time exit). It must be the SAME
    # rule, and engine.evaluate_open_positions must actually call it: the
    # ledger used to book gaps AT the stop, hiding them from the kill switch.
    s.check("barrier_exit books a gap at the market",
            pt.barrier_exit(0.01, 1.15, 0.925), ("STOPPED_OUT", 0.01))
    s.check("barrier_exit fills a take-profit AT the target",
            pt.barrier_exit(1.40, 1.15, 0.925), ("TARGET_HIT", 1.15))
    s.check_true("barrier_exit has no time exit",
                 pt.barrier_exit(1.0, 1.15, 0.925)[0] is None)
    for px in (0.01, 0.93, 1.0, 1.2):
        s.check_true(f"decide_exit and barrier_exit agree at {px} before the timeout",
                     pt.decide_exit(px, 1.15, 0.925, 5) == pt.barrier_exit(px, 1.15, 0.925))
    s.check_true("the live ledger exits through the shared barrier rule",
                 _ledger_uses_barrier_exit())

    # --- dropout classification ---
    s.check("an unpriceable token is classified as dropped",
            pt.classify_horizon_row(None, 1.0), "DROPPED_NO_PRICE")
    s.check("a zero basis is classified as dropped",
            pt.classify_horizon_row(1.0, 0.0), "DROPPED_NO_BASIS")
    s.check("a missing basis is classified as dropped",
            pt.classify_horizon_row(1.0, None), "DROPPED_NO_BASIS")
    s.check("a usable row is markable", pt.classify_horizon_row(1.0, 2.0), "MARKABLE")
    s.check_true("a price of zero is not treated as a valid mark",
                 pt.classify_horizon_row(None, 2.0) != "MARKABLE")

    # --- horizon dropout must be counted, not silent ---
    d = pt.horizon_dropout_summary({30: 5, pt.HORIZON_DROPPED_NO_PRICE: 4,
                                    pt.HORIZON_DROPPED_NO_BASIS: 1,
                                    pt.HORIZON_DUE_TOTAL: 10})
    s.check("half the sample dropping is reported as 50%", d["dropout_percent"], 50.0)
    s.check("unpriceable tokens are counted", d["dropped_no_price"], 4)
    s.check_true("a clean tick reports zero dropout, not None",
                 pt.horizon_dropout_summary({30: 10, pt.HORIZON_DUE_TOTAL: 10})["dropout_percent"] == 0.0)
    s.check_true("nothing due reports None rather than a fabricated 0%",
                 pt.horizon_dropout_summary({})["dropout_percent"] is None)

    # --- both paper arms must test the SAME strategy ---
    # compute_levels used to build one absolute level set around the 0.93
    # pullback entry and give it to both arms. LIMIT filled at 0.93 and got
    # the intended 2:1; IMMEDIATE filled at spot and inherited a stop 14%
    # below and a target 7% above its own fill -- 0.5:1. The two-arm design
    # exists to test entry TIMING, and it was silently testing risk/reward
    # instead, with a foregone answer.
    def rr(model, price=1.0):
        entry, stop, target = pt.compute_levels(price, model)
        fill = price if model == "IMMEDIATE" else entry
        return (target - fill) / (fill - stop)

    s.check_true("the IMMEDIATE arm has 2:1 reward:risk, not 0.5:1",
                 abs(rr("IMMEDIATE") - pt.REWARD_RISK_MULTIPLE) < 1e-6)
    s.check_true("the LIMIT arm has the same 2:1", abs(rr("LIMIT") - pt.REWARD_RISK_MULTIPLE) < 1e-6)
    s.check_true("both arms carry identical geometry, so only entry timing differs",
                 abs(rr("IMMEDIATE") - rr("LIMIT")) < 1e-9)
    imm_e, _, _ = pt.compute_levels(1.0, "IMMEDIATE")
    lim_e, _, _ = pt.compute_levels(1.0, "LIMIT")
    s.check_true("the IMMEDIATE arm enters at spot", abs(imm_e - 1.0) < 1e-9)
    s.check_true("the LIMIT arm still waits for a pullback below spot", lim_e < 1.0)
    # And the IMMEDIATE arm must model the live path exactly.
    imm_e, imm_s, imm_t = pt.compute_levels(1.0, "IMMEDIATE")
    s.check_true("the IMMEDIATE arm's stop matches live D_PULSE",
                 abs((imm_s / imm_e) - (1 - 7.53 / 100)) < 1e-6)
    # Ordering must still hold at the magnitudes that broke fixed rounding.
    for price in [1e-2, 1e-4, 1e-7, 2e-9]:
        for model in ("IMMEDIATE", "LIMIT"):
            e, st, tg = pt.compute_levels(price, model)
            s.check_true(f"{model} levels ordered at {price:.0e}", 0 < st < e < tg)

    # --- the dropout denominator must mean something ---
    # It counted every row still missing any horizon, including rows too young
    # to have been marked at all. A real dropout divided by that denominator
    # looked like noise.
    s.check_true("a row younger than the shortest horizon cannot be a missed mark",
                 not pt.horizon_elapsed(min(pt.HORIZONS_MINUTES) - 1))
    s.check_true("a row exactly at the shortest horizon counts",
                 pt.horizon_elapsed(min(pt.HORIZONS_MINUTES)))
    s.check_true("an older row counts", pt.horizon_elapsed(min(pt.HORIZONS_MINUTES) * 3))
    s.check_true("an unknown age cannot count as a missed mark",
                 not pt.horizon_elapsed(None))

    # --- the signer's cap may not exceed the pipeline's hard ceiling ---
    s.check_true("the absolute ceiling is a source constant, not config",
                 isinstance(rails.ABSOLUTE_MAX_POSITION_USD, float))
    s.check_true("config above the ceiling is clamped to it",
                 rails.position_ceiling_usd(10_000.0, 10_000.0) == rails.ABSOLUTE_MAX_POSITION_USD)

    return s
