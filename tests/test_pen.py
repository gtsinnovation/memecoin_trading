"""Holding-pen suite.

The pen exists because Solana mints faster than any newest-first endpoint can
span, so "new" and "liquid" have to be joined in memory. Two properties make
that join trustworthy, and both fail silently if broken:

  1. AGE COMES FROM THE CHAIN, NOT OUR CLOCK. If capture stalls and rows are
     aged from when we noticed them, the whole backlog releases at once
     looking fifteen minutes old. Every downstream number stays plausible.

  2. THE FLOOR IS APPLIED AT RELEASE, NOT AT CAPTURE. A pool one minute old
     has almost no liquidity; the ones worth evaluating GREW into the floor.
     Filtering at capture discards exactly the population this exists to find.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from tests.harness import Suite, make_address


def run(asyncpg, discovery_pen, dsn) -> Suite:
    s = Suite("holding pen")
    pen = discovery_pen

    def now():
        return datetime.now(timezone.utc)

    def gt_pool(mint, created_at, page_of=None):
        return {"attributes": {"pool_created_at": created_at},
                "relationships": {"base_token": {"data": {"id": f"solana_{mint}"}}}}

    def capture_client(rows_by_page):
        def handler(request):
            page = int(dict(request.url.params).get("page", "1"))
            return httpx.Response(200, json={"data": rows_by_page.get(page, [])})
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def dex_client(liquidity_by_mint, fail=False):
        """DexScreener batch responder. Returns pairs keyed by baseToken."""
        def handler(request):
            if fail:
                return httpx.Response(500, json={})
            tail = str(request.url).rsplit("/", 1)[-1]
            out = []
            for mint in tail.split(","):
                if mint in liquidity_by_mint:
                    out.append({"baseToken": {"address": mint},
                                "quoteToken": {"address": "So11111111111111111111111111111111111111112"},
                                "liquidity": {"usd": liquidity_by_mint[mint]}})
            return httpx.Response(200, json=out)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def with_conn(fn):
        conn = await asyncpg.connect(dsn=dsn)
        try:
            return await fn(conn)
        finally:
            await conn.close()

    def run_async(fn):
        # asyncio.run(), NOT get_event_loop().run_until_complete().
        #
        # The suite that runs immediately before this one uses asyncio.run()
        # throughout, and asyncio.run() sets the thread's event loop back to
        # None when it finishes. On Python 3.11 get_event_loop() then raises
        # "There is no current event loop" -- so this suite passed or failed
        # depending on which suites had run before it, which is not a property
        # a test should have. asyncio.run() owns its own loop every call and
        # does not care about ordering.
        return asyncio.run(with_conn(fn))

    def clear():
        run_async(lambda c: c.execute("TRUNCATE discovery_pen;"))

    async def seed(conn, mint, age_minutes, first_seen_minutes_ago=None):
        """Insert directly, so age and observation time can differ."""
        await conn.execute(
            """
            INSERT INTO discovery_pen (token_address, pool_created_at, first_seen_at, source)
            VALUES ($1, NOW() - ($2 * INTERVAL '1 minute'),
                    NOW() - ($3 * INTERVAL '1 minute'), 'test')
            ON CONFLICT (token_address) DO NOTHING;
            """, mint, float(age_minutes),
            float(first_seen_minutes_ago if first_seen_minutes_ago is not None else age_minutes))

    # ------------------------------------------------------------- capture
    print("\n[PEN] capture stores the chain's timestamp, not ours")
    clear()
    fresh_mint = make_address(900)
    born = (now() - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    got = run_async(lambda c: pen.capture(c, capture_client({1: [gt_pool(fresh_mint, born)]})))
    s.check("one row seen", got["seen"], 1)
    s.check("and stored", got["stored"], 1)

    row = run_async(lambda c: c.fetchrow(
        "SELECT EXTRACT(EPOCH FROM (NOW() - pool_created_at))/60.0 AS age, "
        "EXTRACT(EPOCH FROM (NOW() - first_seen_at))/60.0 AS seen "
        "FROM discovery_pen WHERE token_address = $1;", fresh_mint))
    s.check_true("aged from the pool's creation time (~40 min), not from capture",
                 row is not None and 38.0 < float(row["age"]) < 42.0)
    s.check_true("first_seen_at is separately ~now, so an outage is visible",
                 row is not None and float(row["seen"]) < 2.0)

    print("\n[PEN] every timestamp shape a provider might ship")
    # asyncpg binds by TYPE: a timestamptz parameter wants a datetime and
    # raises on the provider's ISO string rather than coercing it. That is
    # how the first version stored zero of every forty rows it fetched while
    # reporting a clean sweep -- the insert error was caught at DEBUG.
    from datetime import datetime as _dt
    iso = (now() - timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    s.check_true("an ISO-8601 string parses", pen.parse_created(iso) is not None)
    s.check_true("and it is timezone-aware, so the DB comparison is unambiguous",
                 pen.parse_created(iso).tzinfo is not None)
    s.check_true("epoch seconds parse",
                 pen.parse_created(now().timestamp() - 600) is not None)
    s.check_true("epoch milliseconds parse",
                 pen.parse_created((now().timestamp() - 600) * 1000.0) is not None)
    s.check_true("a naive datetime is made aware rather than refused",
                 pen.parse_created(_dt(2026, 1, 1)).tzinfo is not None)
    for bad in (None, "", "not-a-date", 0, -5, [], {}):
        s.check(f"an unreadable timestamp {bad!r} yields no datetime",
                pen.parse_created(bad), None)

    print("\n[PEN] a sweep that stores nothing is reported as a defect")
    clear()
    # Rows that fetch fine but all fail to store is the exact shape of the
    # bug this guards: the pen stays empty, discovery keeps reporting healthy
    # counts from its other sources, and nothing says why.
    allbad = [{"attributes": {"pool_created_at": "garbage"},
               "relationships": {"base_token": {"data": {"id": f"solana_{make_address(903)}"}}}}]
    got = run_async(lambda c: pen.capture(c, capture_client({1: allbad})))
    s.check("rows were fetched", got["seen"], 1)
    s.check("none stored", got["stored"], 0)
    s.check("and the count of unusable rows is reported", got.get("undated"), 1)

    print("\n[PEN] a row that cannot be aged never enters")
    clear()
    undated = make_address(901)
    bad_mint = {"attributes": {"pool_created_at": born},
                "relationships": {"base_token": {"data": {"id": "solana_not-a-mint!"}}}}
    got = run_async(lambda c: pen.capture(c, capture_client({1: [
        {"attributes": {}, "relationships": {"base_token": {"data": {"id": f"solana_{undated}"}}}},
        bad_mint,
    ]})))
    s.check("both rows were seen", got["seen"], 2)
    s.check("neither was stored", got["stored"], 0)
    # Admitting an undated row is worse than dropping it: it would either
    # never release or release at the wrong age, and both are silent.
    held = run_async(lambda c: c.fetchval("SELECT COUNT(*) FROM discovery_pen;"))
    s.check("the pen stays empty", int(held), 0)

    print("\n[PEN] capture is idempotent across sweeps")
    clear()
    dup = make_address(902)
    payload = {1: [gt_pool(dup, born)]}
    run_async(lambda c: pen.capture(c, capture_client(payload)))
    run_async(lambda c: pen.capture(c, capture_client(payload)))
    s.check("the same pool is held once, not twice",
            int(run_async(lambda c: c.fetchval("SELECT COUNT(*) FROM discovery_pen;"))), 1)

    print("\n[PEN] a dead provider costs coverage, not the cycle")
    clear()
    def dead(request):
        return httpx.Response(503, json={})
    got = run_async(lambda c: pen.capture(
        c, httpx.AsyncClient(transport=httpx.MockTransport(dead))))
    s.check("nothing stored", got["stored"], 0)
    s.check_true("and it returned rather than raised", isinstance(got, dict))

    # ------------------------------------------------------------- release
    print("\n[PEN] only tokens inside the window are released")
    clear()
    too_new, ready, too_old = make_address(910), make_address(911), make_address(912)

    async def seed_window(conn):
        await seed(conn, too_new, 3)
        await seed(conn, ready, 30)
        await seed(conn, too_old, 400)
    run_async(seed_window)

    liq = {too_new: 50000.0, ready: 50000.0, too_old: 50000.0}
    out = run_async(lambda c: pen.release_due(
        c, dex_client(liq), min_age_minutes=15, max_age_minutes=90, floor_usd=8000))
    s.check("only the in-window token is offered", out, [ready])

    print("\n[PEN] THE invariant: age is read from the chain, not from capture")
    # The failure this guards: capture stalls ten minutes, resumes, and every
    # backlogged row is dated from when we noticed it. Everything releases at
    # once looking fifteen minutes old and no downstream number shows it.
    clear()
    late = make_address(913)
    run_async(lambda c: seed(c, late, 30, first_seen_minutes_ago=0.5))
    out = run_async(lambda c: pen.release_due(
        c, dex_client({late: 50000.0}), min_age_minutes=15, max_age_minutes=90, floor_usd=8000))
    s.check_true("a token captured seconds ago but born 30 min ago IS due", out == [late])

    clear()
    early = make_address(914)
    run_async(lambda c: seed(c, early, 2, first_seen_minutes_ago=60))
    out = run_async(lambda c: pen.release_due(
        c, dex_client({early: 50000.0}), min_age_minutes=15, max_age_minutes=90, floor_usd=8000))
    s.check("a token we have watched for an hour but born 2 min ago is NOT due", out, [])

    print("\n[PEN] the floor is applied at RELEASE, on grown liquidity")
    clear()
    grew, stayed_thin = make_address(920), make_address(921)

    async def seed_pair(conn):
        await seed(conn, grew, 30)
        await seed(conn, stayed_thin, 30)
    run_async(seed_pair)
    out = run_async(lambda c: pen.release_due(
        c, dex_client({grew: 42000.0, stayed_thin: 900.0}),
        min_age_minutes=15, max_age_minutes=90, floor_usd=8000))
    s.check("the one that grew into the floor is offered", out, [grew])

    print("\n[PEN] an unreadable liquidity is not a zero, and not a pass")
    clear()
    unread = make_address(922)
    run_async(lambda c: seed(c, unread, 30))
    out = run_async(lambda c: pen.release_due(
        c, dex_client({}), min_age_minutes=15, max_age_minutes=90, floor_usd=8000))
    s.check("a token with no reading is not offered", out, [])
    clear()
    run_async(lambda c: seed(c, unread, 30))
    out = run_async(lambda c: pen.release_due(
        c, dex_client({}, fail=True), min_age_minutes=15, max_age_minutes=90, floor_usd=8000))
    s.check("nor is one whose batch call failed", out, [])

    print("\n[PEN] an unreadable token is NOT burned -- it is left for retry")
    # One DexScreener 429 used to permanently consume every token in the
    # batch: marked released, never offered again, and recorded as
    # qualified=false with a NULL liquidity -- indistinguishable in aggregate
    # from a genuinely thin token, corrupting the distribution the marking
    # exists to collect.
    clear()
    unread_a, unread_b = make_address(980), make_address(981)

    async def seed_two(conn):
        await seed(conn, unread_a, 40)
        await seed(conn, unread_b, 40)
    run_async(seed_two)
    out = run_async(lambda c: pen.release_due(
        c, dex_client({}, fail=True), min_age_minutes=30, max_age_minutes=90, floor_usd=8000))
    s.check("nothing is offered when nothing could be read", out, [])
    still_due = run_async(lambda c: c.fetchval(
        "SELECT count(*) FROM discovery_pen WHERE released_at IS NULL;"))
    s.check("both remain unreleased, so the next refresh can retry them",
            int(still_due), 2)

    # A token that IS read, and fails the floor, is still marked -- otherwise
    # it would be re-examined forever and starve the queue behind it.
    clear()
    thin_read, no_read = make_address(982), make_address(983)

    async def seed_mixed(conn):
        await seed(conn, thin_read, 40)
        await seed(conn, no_read, 40)
    run_async(seed_mixed)
    run_async(lambda c: pen.release_due(
        c, dex_client({thin_read: 500.0}),
        min_age_minutes=30, max_age_minutes=90, floor_usd=8000))
    marked = run_async(lambda c: c.fetchval(
        "SELECT released_at IS NOT NULL FROM discovery_pen WHERE token_address=$1;", thin_read))
    unmarked = run_async(lambda c: c.fetchval(
        "SELECT released_at IS NULL FROM discovery_pen WHERE token_address=$1;", no_read))
    s.check_true("a token that was READ and failed the floor is marked", bool(marked))
    s.check_true("a token in the same batch that could not be read is not", bool(unmarked))

    print("\n[PEN] a token is examined once, not re-offered every refresh")
    clear()
    once = make_address(930)
    run_async(lambda c: seed(c, once, 30))
    first = run_async(lambda c: pen.release_due(
        c, dex_client({once: 50000.0}), min_age_minutes=15, max_age_minutes=90, floor_usd=8000))
    second = run_async(lambda c: pen.release_due(
        c, dex_client({once: 50000.0}), min_age_minutes=15, max_age_minutes=90, floor_usd=8000))
    s.check("offered on the first pass", first, [once])
    s.check("and not again on the second", second, [])

    # A thin token is marked too. Re-checking it every refresh would spend the
    # same slot repeatedly on the candidate least likely to deserve it, while
    # the ones arriving behind it wait.
    clear()
    thin = make_address(931)
    run_async(lambda c: seed(c, thin, 30))
    run_async(lambda c: pen.release_due(
        c, dex_client({thin: 100.0}), min_age_minutes=15, max_age_minutes=90, floor_usd=8000))
    row = run_async(lambda c: c.fetchrow(
        "SELECT released_at, liquidity_at_release, qualified "
        "FROM discovery_pen WHERE token_address = $1;", thin))
    s.check_true("a thin token is marked examined, not left to be re-checked",
                 row["released_at"] is not None)
    # The failures' liquidity is the half of the distribution that sets the
    # floor. Recording only the winners leaves "N of M passed at the floor we
    # already chose", which cannot tell a floor set too high from a thin
    # market.
    s.check("the measured liquidity is recorded even though it failed",
            float(row["liquidity_at_release"]), 100.0)
    s.check("and it is flagged as not qualifying", row["qualified"], False)

    print("\n[PEN] the supplier is called only when discovery refreshes")
    # Draining the pen is destructive: a token is examined once, forever. The
    # first wiring drained it on every pipeline tick while discovery served a
    # three-minute cache, so all but one batch in forty-five was marked
    # examined and then thrown away.
    clear()
    supplied = make_address(960)
    run_async(lambda c: seed(c, supplied, 40))
    calls = {"n": 0}

    async def supplier():
        calls["n"] += 1
        return [supplied]

    import token_discovery as td_mod
    td_mod._cache["candidates"] = []
    td_mod._cache["fetched_at"] = 0.0
    td_mod._cache["floors"] = None

    def dead_sources(request):
        return httpx.Response(200, json={"data": []})

    async def two_calls():
        client = httpx.AsyncClient(transport=httpx.MockTransport(dead_sources))
        first = await td_mod.discover_candidates(client, pen_supplier=supplier)
        second = await td_mod.discover_candidates(client, pen_supplier=supplier)
        return first, second

    first, second = asyncio.run(two_calls())
    s.check_true("the pen's token is offered on the refresh", supplied in first)
    s.check("the cached second call serves the same list", second, first)
    s.check("and the pen was drained ONCE, not twice", calls["n"], 1)

    print("\n[PEN] tokens are examined partway into the window, not at its edge")
    # A token measured the instant it turns 15 minutes old is being asked
    # whether it grew before it had time to. The examine age is a separate,
    # named constant for that reason.
    s.check_true("the examine age sits inside the evaluation window",
                 pen.PEN_EXAMINE_AGE_MINUTES > 15.0)
    s.check_true("and leaves most of the window for the agent to act in",
                 pen.PEN_EXAMINE_AGE_MINUTES < 60.0)
    clear()
    early, mature = make_address(970), make_address(971)

    async def seed_ages2(conn):
        await seed(conn, early, 20)
        await seed(conn, mature, 45)
    run_async(seed_ages2)
    out = run_async(lambda c: pen.release_due(
        c, dex_client({early: 50000.0, mature: 50000.0}),
        min_age_minutes=pen.PEN_EXAMINE_AGE_MINUTES, max_age_minutes=90, floor_usd=8000))
    s.check("a token below the examine age waits rather than being spent",
            out, [mature])

    print("\n[PEN] pruning keeps the table bounded")
    clear()
    keep, drop = make_address(940), make_address(941)

    async def seed_ages(conn):
        await seed(conn, keep, 60)
        await seed(conn, drop, pen.PEN_RETENTION_MINUTES + 120)
    run_async(seed_ages)
    run_async(lambda c: pen.prune(c))
    remaining = run_async(lambda c: c.fetch("SELECT token_address FROM discovery_pen;"))
    names = sorted(r["token_address"] for r in remaining)
    s.check("the aged-out row is gone", names, [keep])

    print("\n[PEN] health is reportable, so an empty pen is visible")
    clear()
    run_async(lambda c: seed(c, make_address(950), 30))
    info = run_async(lambda c: pen.stats(c))
    s.check("one row held", int(info.get("total", 0)), 1)
    s.check("and it is waiting", int(info.get("waiting", 0)), 1)
    # An empty pen and a quiet market look identical downstream -- the only
    # thing that separates them is this number being reported.
    clear()
    info = run_async(lambda c: pen.stats(c))
    s.check("an empty pen reports zero rather than failing", int(info.get("total", 0)), 0)

    clear()
    return s
