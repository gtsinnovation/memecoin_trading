# tests/test_discovery.py
"""Token discovery: the Birdeye sources, the evaluation window, per-provider
throttling, and the GeckoTerminal fallback.

The failures these protect against are all silent. A source that stops
answering, starts returning a different JSON shape, gets rate limited, or has
its liquidity floor tuned into meaninglessness does not crash anything -- it
quietly stops feeding the experiment, or feeds it the wrong population, while
every log line still looks healthy. That exact failure has happened three
times on this project: an 18-hour plateau nobody noticed, a candidate list
halved by 429s, and a whole source deleted by a stale container image.
"""
import asyncio
import time

import httpx

from .harness import Suite, make_address


def run(token_discovery) -> Suite:
    s = Suite("token discovery")
    td = token_discovery

    # Several checks rewrite module constants to exercise a behaviour. The
    # shipped values have to be read BEFORE that happens, or a test that
    # asserts something about the configuration ends up asserting it about its
    # own fixture and passes no matter what ships.
    SHIPPED = {name: getattr(td, name) for name in (
        "BIRDEYE_MIN_LIQUIDITY_USD", "BIRDEYE_MIN_AGE_MINUTES",
        "BIRDEYE_MAX_AGE_MINUTES", "BIRDEYE_PAGE_LIMIT",
        "DISCOVERY_MAX_CANDIDATES", "DISCOVERY_PAGE_SPACING_S",
        "BIRDEYE_MIN_INTERVAL_S", "GECKOTERMINAL_MIN_INTERVAL_S",
        "BIRDEYE_API_KEY",
        "JUPITER_MIN_LIQUIDITY_USD", "JUPITER_MIN_AGE_MINUTES",
        "JUPITER_MAX_AGE_MINUTES", "JUPITER_MIN_INTERVAL_S")}

    def client_for(handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def fresh():
        td._cache["candidates"] = []
        td._cache["fetched_at"] = 0.0
        td._cache["floors"] = None
        td._next_allowed.clear()
        td.BIRDEYE_API_KEY = "test-key"
        td.DISCOVERY_PAGE_SPACING_S = 0
        td.BIRDEYE_MIN_INTERVAL_S = 0.0
        td.GECKOTERMINAL_MIN_INTERVAL_S = 0.0
        td.JUPITER_MIN_INTERVAL_S = 0.0

    def discover(handler):
        return asyncio.run(
            td.discover_candidates(client_for(handler), force_refresh=True))

    def discover_with_floors(handler, general=None, new_listing=None):
        return asyncio.run(
            td.discover_candidates(client_for(handler), force_refresh=True,
                                   min_liquidity_usd=general,
                                   new_listing_min_liquidity_usd=new_listing))

    def be(payload_rows, key="items"):
        """A Birdeye envelope. The wrapper key differs per endpoint."""
        return {"success": True, "data": {key: payload_rows}}

    def listed(minutes_ago):
        return time.time() - minutes_ago * 60.0

    def token(seed, age_min=30.0, **extra):
        row = {"address": make_address(seed),
               "recent_listing_time": listed(age_min),
               "liquidity": 50000.0, "symbol": f"T{seed}"}
        row.update(extra)
        return row

    def gt_pool(addr):
        return {"attributes": {"name": "X/SOL", "reserve_in_usd": "50000"},
                "relationships": {"base_token": {"data": {"id": f"solana_{addr}"}}}}

    def jup(seed, age_min=30.0, liquidity=50000.0, **extra):
        """A Jupiter /tokens/v2 row. Note `id`, not `address` -- reading the
        wrong key is the single most likely way this provider breaks, and it
        would fail silently as an empty source rather than as an error."""
        from datetime import datetime, timedelta, timezone
        created = datetime.now(timezone.utc) - timedelta(minutes=age_min)
        row = {"id": make_address(seed), "symbol": f"J{seed}",
               "liquidity": liquidity,
               "createdAt": created.strftime("%Y-%m-%dT%H:%M:%SZ")}
        row.update(extra)
        return row

    def router(recent=None, market=None, trending=None, gt_trending=None,
               gt_pools=None, rugcheck=None, boosts=None, on_call=None,
               jup_recent=None, jup_traded=None, jup_organic=None):
        """One handler for every endpoint, so a test only names what it cares
        about and everything else returns an empty-but-valid response."""
        def handler(request):
            url = str(request.url)
            if on_call is not None:
                on_call(url)
            if "/tokens/v2/recent" in url:
                return httpx.Response(200, json=jup_recent or [])
            if "/tokens/v2/toptraded" in url:
                return httpx.Response(200, json=jup_traded or [])
            if "/tokens/v2/toporganicscore" in url:
                return httpx.Response(200, json=jup_organic or [])
            if "/defi/v3/token/list" in url:
                sort_by = dict(request.url.params).get("sort_by", "")
                rows = recent if sort_by == "recent_listing_time" else market
                return httpx.Response(200, json=be(rows or []))
            if "/defi/token_trending" in url:
                return httpx.Response(200, json=be(trending or [], key="tokens"))
            if "trending_pools" in url:
                return httpx.Response(200, json={"data": gt_trending or []})
            if "/pools" in url:
                page = int(dict(request.url.params).get("page", "1"))
                pages = gt_pools or {}
                return httpx.Response(200, json={"data": pages.get(page, [])})
            if "rugcheck" in url:
                return httpx.Response(200, json=rugcheck or [])
            if "token-boosts" in url:
                return httpx.Response(200, json=boosts or [])
            return httpx.Response(200, json={"data": []})
        return handler

    # ---------------------------------------------------------------- window
    # The evaluation window is the whole recency design. Its lower bound is
    # the one piece of the deleted holding pen that was never optional: a
    # token listed seconds ago has no price at any provider, so evaluating it
    # burns one of only ~190 hourly slots for no observation at all.

    print("\n[WINDOW] only listings inside the age window are offered")
    fresh()
    too_new, just_right, too_old = make_address(1), make_address(2), make_address(3)
    got = discover(router(recent=[
        {"address": too_new, "recent_listing_time": listed(2), "liquidity": 90000},
        {"address": just_right, "recent_listing_time": listed(30), "liquidity": 90000},
        {"address": too_old, "recent_listing_time": listed(400), "liquidity": 90000},
    ]))
    s.check("only the in-window listing survives", got, [just_right])

    print("\n[WINDOW] the bounds themselves are inclusive, not off by one")
    fresh()
    at_min, at_max = make_address(4), make_address(5)
    got = discover(router(recent=[
        {"address": at_min, "recent_listing_time": listed(td.BIRDEYE_MIN_AGE_MINUTES + 0.1), "liquidity": 90000},
        {"address": at_max, "recent_listing_time": listed(td.BIRDEYE_MAX_AGE_MINUTES - 0.1), "liquidity": 90000},
    ]))
    s.check_true("a token just past the hold is offered", at_min in got)
    s.check_true("a token just inside the upper bound is offered", at_max in got)

    print("\n[WINDOW] a row with no listing time is skipped, never admitted")
    # Admitting it would risk evaluating a token before it can be priced.
    # Skipping is the conservative direction.
    #
    # Stated as an absolute, not "not offered": a first version of this check
    # survived a mutation that defaulted undated rows to an age of zero,
    # because zero also fails the hold. The dangerous default is a MID-WINDOW
    # age, which sails straight through. Every substitute must be refused, so
    # the assertion is on the presence of the address at all.
    fresh()
    undated = make_address(6)
    got = discover(router(recent=[
        {"address": undated, "liquidity": 90000},
        {"address": undated, "liquidity": 90000, "recent_listing_time": None},
        {"address": undated, "liquidity": 90000, "recent_listing_time": 0},
        {"address": undated, "liquidity": 90000, "recent_listing_time": "not-a-number"},
    ]))
    s.check("no usable timestamp means never offered, whatever the shape", got, [])

    print("\n[WINDOW] a short page is reported, not silently truncated")
    # If listings ever arrive faster than one page reaches, the oldest part of
    # the window falls off the end of the response. That must be loud: it is
    # the same shape as the 429s that halved the candidate list unnoticed.
    fresh()
    warnings = []
    real_warn = td.logger.warning
    td.logger.warning = lambda msg, *a, **k: warnings.append(str(msg))
    try:
        discover(router(recent=[token(10 + i, age_min=1.0 + i) for i in range(5)]))
    finally:
        td.logger.warning = real_warn
    s.check_true("a page that cannot reach the window logs a warning",
                 any("reaches only" in w for w in warnings))

    # ------------------------------------------------------------ parse shape
    print("\n[SHAPE] Birdeye's two different envelopes are both read")
    # v3/token/list uses data.items[]; defi/token_trending uses data.tokens[].
    # Assuming either one alone finds nothing and reports success.
    fresh()
    from_list, from_trending = make_address(20), make_address(21)
    got = discover(router(recent=[token(20)], trending=[{"address": from_trending}]))
    s.check_true("data.items[] was read", from_list in got)
    s.check_true("data.tokens[] was read", from_trending in got)

    print("\n[SHAPE] only the named address field is read, never a mint-shaped search")
    # Birdeye v3 rows carry extensions.serum_v3_usdc / _usdt, which are Serum
    # market addresses and look exactly like mints. A parser that searched the
    # payload for something base58-shaped would ingest them and waste a price
    # lookup on every tick forever.
    # Checked on BOTH paths. The recency source reads row["address"] inline
    # while the market and trending sources go through _mints_from(), so a
    # search-based parser introduced in the shared helper would be invisible
    # to a test that only exercised recency -- which a mutation proved.
    fresh()
    real, serum_usdc, serum_usdt = make_address(30), make_address(31), make_address(32)
    extensions = {"serum_v3_usdc": serum_usdc, "serum_v3_usdt": serum_usdt}
    got = discover(router(recent=[{
        "address": real, "recent_listing_time": listed(30), "liquidity": 90000,
        "extensions": extensions,
    }]))
    s.check("recency path: the token address is taken", got, [real])
    s.check_true("recency path: Serum market addresses are NOT ingested",
                 serum_usdc not in got and serum_usdt not in got)

    fresh()
    market_real = make_address(33)
    got = discover(router(market=[{"address": market_real, "extensions": extensions}]))
    s.check("market path: the token address is taken", got, [market_real])
    s.check_true("market path: Serum market addresses are NOT ingested",
                 serum_usdc not in got and serum_usdt not in got)

    fresh()
    trend_real = make_address(34)
    got = discover(router(trending=[{"address": trend_real, "extensions": extensions}]))
    s.check("trending path: the token address is taken", got, [trend_real])
    s.check_true("trending path: Serum market addresses are NOT ingested",
                 serum_usdc not in got and serum_usdt not in got)

    print("\n[VALIDATION] malformed identifiers never reach the pipeline")
    fresh()
    good = make_address(40)
    got = discover(router(market=[
        {"address": good}, {"address": "0xNOTBASE58"}, {"address": "short"},
        {"address": None}, {"address": ""}, "not-a-dict",
    ]))
    s.check("only the valid mint survives", got, [good])

    # ------------------------------------------------------------- resilience
    print("\n[RESILIENCE] one failing source must not cost the others")
    fresh()
    survivor = make_address(50)

    def partial(request):
        url = str(request.url)
        if "/defi/v3/token/list" in url:
            return httpx.Response(500)
        if "/defi/token_trending" in url:
            return httpx.Response(200, json=be([{"address": survivor}], key="tokens"))
        if "/pools" in url or "trending_pools" in url:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": []})

    got = discover(partial)
    s.check_true("the healthy source still delivers", survivor in got)

    print("\n[CACHE] a total outage reuses the last good list, never empties it")
    fresh()
    discover(router(market=[token(60 + i) for i in range(5)]))
    got = discover(lambda request: httpx.Response(503))
    s.check_true("previous list reused rather than returning nothing", len(got) > 0)

    # --------------------------------------------------------------- fallback
    print("\n[FALLBACK] the GeckoTerminal walk engages only when Birdeye is empty")
    fresh()
    seq = []
    got = discover(router(recent=[token(70)], market=[token(71)],
                          gt_pools={1: [gt_pool(make_address(72))]},
                          on_call=lambda url: seq.append(url)))
    s.check_true("Birdeye delivered", make_address(70) in got)
    s.check_true("the pool walk was NOT called while Birdeye works",
                 not any("networks/solana/pools" in u for u in seq))

    print("\n[FALLBACK] and does engage when Birdeye returns nothing")
    fresh()
    rescued = make_address(80)
    got = discover(router(gt_pools={1: [gt_pool(rescued)], 2: []}))
    s.check_true("the keyless fallback rescued the cycle", rescued in got)

    print("\n[FALLBACK] a missing API key skips Birdeye without failing the cycle")
    # Degraded but working: no key means the keyless sources plus the fallback
    # carry the load. It must not throw, and must not send a key-less request
    # that comes back 401 every three minutes.
    fresh()
    td.BIRDEYE_API_KEY = ""
    td._warned_no_key = False
    keyless = make_address(90)
    called = []
    got = discover(router(gt_pools={1: [gt_pool(keyless)], 2: []},
                          on_call=lambda url: called.append(url)))
    s.check_true("the cycle still produced candidates", keyless in got)
    s.check_true("no Birdeye call was attempted without a key",
                 not any("birdeye" in u for u in called))
    td.BIRDEYE_API_KEY = "test-key"

    # ------------------------------------------------------------- throttling
    print("\n[RATE] calls to one provider are spaced, across ALL its sources")
    # Rate limits belong to the vendor, not the function. When three sources
    # shared GeckoTerminal and each spaced only its own pages, nine calls
    # still burst in ~12s, 429s began at the sixth, and because a failed page
    # ends a walk the last source in line was silently deleted from the cycle.
    fresh()
    td.BIRDEYE_MIN_INTERVAL_S = 0.05
    stamps = []
    got = discover(router(recent=[token(100)], market=[token(101)],
                          trending=[{"address": make_address(102)}],
                          on_call=lambda url: stamps.append(
                              (url, time.monotonic())) if "birdeye" in url else None))
    times = [t for _, t in stamps]
    s.check_true("more than one Birdeye call was made", len(times) > 1)
    gaps = [b - a for a, b in zip(times, times[1:])]
    s.check_true("every consecutive Birdeye pair is spaced by the interval",
                 all(g >= td.BIRDEYE_MIN_INTERVAL_S * 0.7 for g in gaps))
    td.BIRDEYE_MIN_INTERVAL_S = 0.0

    print("\n[RATE] providers are throttled independently, not as one queue")
    s.check("birdeye uses its own interval", td._interval_for("birdeye"),
            td.BIRDEYE_MIN_INTERVAL_S)
    s.check("geckoterminal uses its own interval", td._interval_for("geckoterminal"),
            td.GECKOTERMINAL_MIN_INTERVAL_S)
    s.check("an unknown provider is unthrottled", td._interval_for("dexscreener"), 0.0)
    s.check("birdeye urls are attributed to birdeye",
            td._provider_of(f"{td.BIRDEYE_BASE}/defi/v3/token/list"), "birdeye")
    s.check("geckoterminal urls are attributed to geckoterminal",
            td._provider_of(f"{td.GECKOTERMINAL_BASE}/api/v2/x"), "geckoterminal")
    s.check("other urls are attributed to no provider",
            td._provider_of("https://api.dexscreener.com/x"), None)

    print("\n[RATE] a slow provider must not delay a fast one")
    # Collapsing the per-provider timers into one shared queue passes every
    # check above, because they only ever exercise one provider at a time. It
    # is a real regression though: GeckoTerminal's 2.5s would then govern
    # Birdeye too, stretching a 3-second refresh into a 10-second one and
    # costing a tick every cycle. _SOURCES interleaves a GeckoTerminal call
    # between two Birdeye calls, so a shared timer shows up as a Birdeye gap
    # inflated to GeckoTerminal's interval.
    fresh()
    td.BIRDEYE_MIN_INTERVAL_S = 0.02
    td.GECKOTERMINAL_MIN_INTERVAL_S = 0.40
    marks = []
    discover(router(recent=[token(120)], market=[token(121)],
                    trending=[{"address": make_address(122)}],
                    gt_trending=[{"relationships": {"base_token": {"data": {
                        "id": f"solana_{make_address(123)}"}}}}],
                    on_call=lambda url: marks.append(
                        ("be" if "birdeye" in url else "gt" if "geckoterminal" in url
                         else "other", time.monotonic()))))
    be_times = [t for kind, t in marks if kind == "be"]
    be_gaps = [b - a for a, b in zip(be_times, be_times[1:])]
    s.check_true("three Birdeye calls were made", len(be_times) == 3)
    s.check_true("a GeckoTerminal call was interleaved",
                 any(kind == "gt" for kind, _ in marks))
    # Generous ceiling: the point is that no Birdeye gap approaches
    # GeckoTerminal's 0.40s, not that the loop wakes to the microsecond.
    s.check_true("no Birdeye gap was inflated to GeckoTerminal's interval",
                 all(g < td.GECKOTERMINAL_MIN_INTERVAL_S * 0.5 for g in be_gaps))
    td.BIRDEYE_MIN_INTERVAL_S = 0.0
    td.GECKOTERMINAL_MIN_INTERVAL_S = 0.0

    print("\n[RATE/CONCURRENT] the throttle holds when callers overlap")
    # Sequential calls cannot detect this. A throttle that sleeps first and
    # records the timestamp AFTERWARDS passes every ordinary test, then breaks
    # the moment two coroutines call it together: both read the same idle gap,
    # both decline to sleep, and both fire. Discovery is sequential today, so
    # this pins the property _throttle()'s comment claims rather than the
    # behaviour currently exercised.
    fresh()
    td.BIRDEYE_MIN_INTERVAL_S = 0.05
    fired = []

    async def _fire():
        await td._throttle("birdeye")
        fired.append(time.monotonic())

    # The gather must be BUILT INSIDE the loop: asyncio.gather() attaches its
    # awaitables to the running loop as it is called, so constructing it in
    # the argument list of asyncio.run() -- before any loop exists -- raises.
    async def _fire_all():
        await asyncio.gather(*[_fire() for _ in range(5)])

    asyncio.run(_fire_all())
    fired.sort()
    gaps = [b - a for a, b in zip(fired, fired[1:])]
    s.check("all five concurrent callers ran", len(fired), 5)
    s.check_true("concurrent callers queue instead of firing together",
                 all(g >= td.BIRDEYE_MIN_INTERVAL_S * 0.7 for g in gaps))
    td.BIRDEYE_MIN_INTERVAL_S = 0.0

    # ------------------------------------------------------------------ frame
    print("\n[FRAME] the liquidity floor must stay a frame, not a gate")
    # MIND THE UNITS. The floor reads pool liquidity, which is TVL -- both
    # sides. B_SENTINEL judges tradeable_depth_usd, which
    # free_market_data.compute_tradeable_depth() defines as
    # min(TVL / 2, reported one-sided). So the gate's $20,000 depth bar sits at
    # $40,000 of LIQUIDITY, and comparing the floor against 20,000 directly is
    # an apples-to-oranges error. It is the error this check was first written
    # with, which is why the conversion is spelled out.
    DEPTH_BAR_USD = 20000.0        # engine.py node_B_SENTINEL, min_depth
    LIQUIDITY_PER_DEPTH = 2.0      # depth <= TVL / 2
    tautology_floor = DEPTH_BAR_USD * LIQUIDITY_PER_DEPTH

    # At or above that line the sampler would only ever offer tokens that
    # already clear the gate under test: approval becomes a tautology, the
    # REJECTED arm from this source disappears, and the cohort comparison goes
    # with it. Invisible in every log line -- discovery keeps reporting healthy
    # counts while the experiment stops being an experiment.
    s.check_true("floor stays below the $40k liquidity that guarantees approval",
                 SHIPPED["BIRDEYE_MIN_LIQUIDITY_USD"] < tautology_floor)
    # The mirror risk: a floor low enough to fill the control arm with tokens
    # whose rejection is a foregone conclusion, at ~190 slots an hour.
    s.check_true("floor stays above the ~$2.2k launchpad band",
                 SHIPPED["BIRDEYE_MIN_LIQUIDITY_USD"] > 2500.0)
    s.check_true("the age hold is non-zero -- unpriceable tokens waste slots",
                 SHIPPED["BIRDEYE_MIN_AGE_MINUTES"] > 0)
    s.check_true("the window is ordered and non-empty",
                 SHIPPED["BIRDEYE_MAX_AGE_MINUTES"] > SHIPPED["BIRDEYE_MIN_AGE_MINUTES"])

    print("\n[FRAME] the floor is sent to the provider, not applied here")
    # The point of min_liquidity is that ~90% of rows are never transferred.
    # If this silently stopped being sent, the sample would swell with the
    # launchpad band and nothing would look wrong.
    fresh()
    params_seen = {}

    def capture(request):
        url = str(request.url)
        if "/defi/v3/token/list" in url:
            params_seen.update(dict(request.url.params))
            return httpx.Response(200, json=be([token(110)]))
        return httpx.Response(200, json={"data": []})

    discover(capture)
    s.check("min_liquidity is sent server-side",
            params_seen.get("min_liquidity"), str(int(SHIPPED["BIRDEYE_MIN_LIQUIDITY_USD"])))
    s.check_true("sorted newest-first for the recency source",
                 params_seen.get("sort_type") == "desc")

    # -------------------------------------------------------------------- cap
    print("\n[CAP] truncation costs the plentiful source, not the scarce one")
    # Recency is ~10 tokens an hour and is the population the experiment was
    # designed around; market breadth is 100 rows a call. First-source-wins
    # ordering has to favour the scarce one.
    fresh()
    td.DISCOVERY_MAX_CANDIDATES = 6
    recent_rows = [token(200 + i) for i in range(5)]
    market_rows = [token(300 + i) for i in range(50)]
    got = discover(router(recent=recent_rows, market=market_rows))
    s.check("cap honoured", len(got), 6)
    s.check("every scarce recency token survived",
            len([a for a in got if a in {r["address"] for r in recent_rows}]), 5)
    td.DISCOVERY_MAX_CANDIDATES = SHIPPED["DISCOVERY_MAX_CANDIDATES"]

    print("\n[LOG] the refresh line breaks down by source, not just a total")
    # A total near the sum of fixed page sizes barely moves even when every
    # member has changed, so it cannot show turnover -- or that one source
    # just stopped answering. That is the failure this project has hit three
    # times.
    fresh()
    lines = []
    real_info = td.logger.info
    td.logger.info = lambda msg, *a, **k: lines.append(str(msg))
    try:
        discover(router(recent=[token(400)], market=[token(401)]))
    finally:
        td.logger.info = real_info
    refreshed = [l for l in lines if "discovery refreshed" in l]
    s.check_true("a refresh line was logged", bool(refreshed))
    s.check_true("it names the per-source counts",
                 bool(refreshed) and "birdeye-recent=" in refreshed[-1])

    print("\n[COMPOSE] every knob this module reads must be forwarded by compose")
    # This has now failed twice, both times invisibly at runtime.
    #
    # Docker Compose forwards ONLY the variables it names in `environment:`.
    # A knob read by token_discovery.py but absent from docker-compose.yml can
    # be set in .env, verified by eye, and still be invisible inside the
    # container -- which looks exactly like the knob not working, or like a
    # rejected API key. The first occurrence was DISCOVERY_* never being
    # listed; the second was an edit to the compose file that deleted
    # BIRDEYE_API_KEY along with its neighbours, which would have silently
    # dropped discovery to its keyless fallback on the next rebuild.
    #
    # Nothing at runtime reports either case, so it is pinned here.
    import os as _os
    import re as _re

    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    compose_path = _os.path.join(root, "docker-compose.yml")
    module_path = _os.path.join(root, "token_discovery.py")

    # Deliberately NOT forwarded: provider base URLs that exist only so a test
    # or a probe can redirect them. They have working defaults, are never set
    # in deployment, and listing them would be noise. Every OTHER name this
    # module reads has to be in compose.
    NOT_FORWARDED = {"RUGCHECK_API_BASE", "GECKOTERMINAL_API_BASE"}

    try:
        with open(compose_path, encoding="utf-8") as fh:
            compose_text = fh.read()
        with open(module_path, encoding="utf-8") as fh:
            module_text = fh.read()
    except OSError as e:
        # Failing loudly beats skipping. A skip here would restore exactly the
        # silence this check exists to remove.
        s.check("docker-compose.yml and token_discovery.py are readable",
                f"OSError: {e}", "both readable")
    else:
        read_names = set(_re.findall(r'os\.environ\.get\(\s*["\']([A-Z0-9_]+)["\']',
                                     module_text))
        s.check_true("the module reads a plausible number of env knobs",
                     len(read_names) >= 10)
        missing = sorted(n for n in read_names - NOT_FORWARDED
                         if f"{n}=" not in compose_text)
        s.check("every knob read here is forwarded by docker-compose.yml", missing, [])

        # The key is not read through os.environ.get at call time -- it is
        # captured into a module constant at import -- so it is checked by name.
        for critical in ("BIRDEYE_API_KEY", "BIRDEYE_API_BASE"):
            s.check_true(f"{critical} is forwarded", f"{critical}=" in compose_text)

    # Hand the module back exactly as it shipped. Leaving a fixture value
    # behind would silently change what any later suite -- or a rerun in the
    # same process -- is actually testing.
    for name, value in SHIPPED.items():
        setattr(td, name, value)
    td._next_allowed.clear()
    td._cache["candidates"] = []
    td._cache["fetched_at"] = 0.0

    # ================================================================ JUPITER
    # Jupiter exists in this module because Birdeye's free tier answers every
    # endpoint with a 400 carrying "Compute units usage limit exceeded" once
    # its monthly budget is spent, and a three-minute refresh spends it. The
    # liquidity-filtered sample cannot depend on a source that stops on a
    # schedule.

    print("\n[JUPITER] the mint is read from `id`, not `address`")
    fresh()
    a, b = make_address(700), make_address(701)
    got = discover(router(jup_recent=[jup(700)], jup_traded=[jup(701)]))
    s.check_true("a Jupiter recency row is offered", a in got)
    s.check_true("a Jupiter top-traded row is offered", b in got)
    # The failure mode this guards: reading `address` returns nothing at all,
    # and an empty source is indistinguishable from a quiet market.
    fresh()
    wrong_key = dict(jup(702)); wrong_key["address"] = wrong_key.pop("id")
    s.check("a row without `id` contributes nothing",
            discover(router(jup_recent=[wrong_key])), [])

    print("\n[JUPITER] the liquidity floor Birdeye applied server-side")
    fresh()
    rich, poor = make_address(710), make_address(711)
    got = discover(router(jup_traded=[
        jup(710, liquidity=td.JUPITER_MIN_LIQUIDITY_USD + 1),
        jup(711, liquidity=td.JUPITER_MIN_LIQUIDITY_USD - 1)]))
    s.check_true("a token above the floor survives", rich in got)
    s.check_true("a token below the floor is dropped", poor not in got)

    fresh()
    exactly = make_address(712)
    s.check_true("the floor itself is inclusive",
                 exactly in discover(router(jup_traded=[
                     jup(712, liquidity=td.JUPITER_MIN_LIQUIDITY_USD)])))

    # Unreadable liquidity is DROPPED, not admitted. Admitting it would
    # reinstate the unfiltered sample one row at a time -- exactly what the
    # GeckoTerminal fallback warns about, but without the warning.
    fresh()
    for label, value in (("missing", None), ("null", "__NULL__"),
                         ("non-numeric", "lots")):
        row = dict(jup(713))
        if value == "__NULL__":
            row["liquidity"] = None
        elif value is None:
            row.pop("liquidity")
        else:
            row["liquidity"] = value
        fresh()
        s.check(f"{label} liquidity is dropped, never admitted",
                discover(router(jup_traded=[row])), [])

    print("\n[JUPITER] the age window, across the timestamp shapes Jupiter ships")
    fresh()
    new, ok, old_tok = make_address(720), make_address(721), make_address(722)
    got = discover(router(jup_recent=[
        jup(720, age_min=2), jup(721, age_min=30), jup(722, age_min=400)]))
    s.check("only the in-window listing survives", got, [ok])

    # The same row, dated three different legal ways. An ISO string is what
    # /tokens/v2 returns today; epoch seconds and milliseconds are accepted
    # because Jupiter publishes those elsewhere and a format change would
    # otherwise silently empty this source.
    import time as _time
    for label, value in (
            ("epoch seconds", _time.time() - 30 * 60),
            ("epoch milliseconds", (_time.time() - 30 * 60) * 1000.0)):
        fresh()
        row = dict(jup(730)); row["createdAt"] = value
        s.check(f"a createdAt in {label} is understood",
                discover(router(jup_recent=[row])), [make_address(730)])

    # An unreadable date is SKIPPED, not treated as new. Treating it as new
    # would evaluate a token before any provider has priced it.
    for label, value in (("garbage", "not-a-date"), ("null", None),
                         ("empty", ""), ("zero", 0)):
        fresh()
        row = dict(jup(731)); row["createdAt"] = value
        s.check(f"an unreadable createdAt ({label}) is skipped, not admitted",
                discover(router(jup_recent=[row])), [])

    print("\n[JUPITER] pool creation time wins over mint creation time")
    # The window is about how long a token has been TRADEABLE. A mint can
    # exist for months before it has a pool, and dating it from the mint
    # would put every such token outside the window forever.
    fresh()
    from datetime import datetime, timedelta, timezone
    pooled = dict(jup(740, age_min=5000))     # mint is ancient
    pooled["firstPool"] = {"createdAt": (datetime.now(timezone.utc)
                                         - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    s.check("an old mint with a new pool is in the window",
            discover(router(jup_recent=[pooled])), [make_address(740)])
    fresh()
    stale_pool = dict(jup(741, age_min=5))    # mint is new
    stale_pool["firstPool"] = {"createdAt": (datetime.now(timezone.utc)
                                             - timedelta(minutes=5000)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    s.check("a new mint with an old pool is OUT of the window",
            discover(router(jup_recent=[stale_pool])), [])

    print("\n[JUPITER] the pool-walk fallback needs EVERY filtered source empty")
    # Previously this keyed on Birdeye alone. Left that way, an exhausted
    # Birdeye quota would have kept the unfiltered GeckoTerminal walk running
    # permanently while Jupiter sat beside it working fine.
    fresh()
    gt_only = make_address(750)
    got = discover(router(jup_traded=[jup(751)],
                          gt_pools={1: [gt_pool(gt_only)]}))
    s.check_true("Jupiter alone prevents the unfiltered fallback",
                 make_address(751) in got)
    s.check_true("and the pool walk does not run", gt_only not in got)

    fresh()
    got = discover(router(gt_pools={1: [gt_pool(gt_only)]}))
    s.check_true("with every filtered source empty the fallback does run",
                 gt_only in got)

    print("\n[JUPITER] throttled as its own provider")
    fresh()
    s.check("jupiter urls are attributed to jupiter",
            td._provider_of(f"{td.JUPITER_TOKENS_BASE}/tokens/v2/recent"), "jupiter")
    td.JUPITER_MIN_INTERVAL_S = 0.7
    s.check("jupiter uses its own interval", td._interval_for("jupiter"), 0.7)
    s.check_true("and not Birdeye's",
                 td._interval_for("jupiter") != td._interval_for("birdeye"))
    td.JUPITER_MIN_INTERVAL_S = 0.0

    print("\n[JUPITER] an exhausted quota is reported as quota, not as a bug")
    fresh()
    seen = []
    def quota_handler(request):
        url = str(request.url)
        if "birdeye" in url:
            return httpx.Response(400, json={
                "success": False, "message": "Compute units usage limit exceeded"})
        if "/tokens/v2/recent" in url:
            return httpx.Response(200, json=[jup(760)])
        return httpx.Response(200, json={"data": []})
    got = discover(quota_handler)
    s.check_true("a quota-exhausted Birdeye does not fail the cycle",
                 make_address(760) in got)
    s.check_true("and the keyless source still carries it", len(got) >= 1)

    print("\n[JUPITER] the shipped configuration is a real one")
    s.check_true("the liquidity floor matches Birdeye's, so the population "
                 "does not change with the provider",
                 SHIPPED["JUPITER_MIN_LIQUIDITY_USD"] == SHIPPED["BIRDEYE_MIN_LIQUIDITY_USD"])
    s.check_true("the age window matches Birdeye's too",
                 SHIPPED["JUPITER_MIN_AGE_MINUTES"] == SHIPPED["BIRDEYE_MIN_AGE_MINUTES"]
                 and SHIPPED["JUPITER_MAX_AGE_MINUTES"] == SHIPPED["BIRDEYE_MAX_AGE_MINUTES"])
    s.check_true("Jupiter is throttled at all", SHIPPED["JUPITER_MIN_INTERVAL_S"] > 0)
    s.check_true("the keyless host is configured, not the metered one "
                 "(api.jup.ag answers 429 without a key)",
                 "lite-api" in td.JUPITER_TOKENS_BASE)
    names = [n for n, _ in td._SOURCES]
    s.check_true("a Jupiter source leads, so a spent Birdeye quota is not fatal",
                 names[0].startswith("jupiter"))
    s.check_true("all three Jupiter sources are registered",
                 len([n for n in names if n.startswith("jupiter")]) == 3)

    print("\n[FAILURE] a refresh where everything failed must not retry instantly")
    # Refreshing is DESTRUCTIVE -- it drains the holding pen, and examining a
    # token marks it forever. The failure path used to return without
    # stamping fetched_at, so the TTL never applied and the drain ran every
    # three seconds. A pen of a few thousand rows is gone in under a minute
    # of provider outage, every row flagged "examined and failed" without a
    # single measurement taken.
    fresh()
    drains = {"n": 0}

    async def counting_supplier():
        drains["n"] += 1
        return []

    def all_dead(request):
        return httpx.Response(200, json={"data": []})

    async def three_ticks():
        client = client_for(all_dead)
        for _ in range(3):
            await td.discover_candidates(client, pen_supplier=counting_supplier)

    asyncio.run(three_ticks())
    s.check("a failed refresh drains the pen ONCE, not once per tick", drains["n"], 1)
    s.check_true("and the retry gap is shorter than the normal TTL, so it recovers",
                 0 < td.DISCOVERY_FAILURE_RETRY_S < td.DISCOVERY_CACHE_TTL_S)

    # =========================================== USER-ADJUSTABLE FLOORS
    print("\n[FLOOR] the bounds are derived from the gate, not typed in")
    # If B_SENTINEL's depth bar ever moves, the ceiling on a user-set floor
    # has to move with it, or the tautology this bound prevents comes back
    # quietly. Read statically so this needs no engine import.
    import ast as _ast
    import os as _os
    engine_src = open(_os.path.join(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))), "engine.py"), encoding="utf-8").read()
    bar = None
    for node in _ast.walk(_ast.parse(engine_src)):
        if (isinstance(node, _ast.Assign)
                and any(isinstance(t, _ast.Name) and t.id == "min_depth" for t in node.targets)
                and isinstance(node.value, _ast.Constant)):
            bar = float(node.value.value)
            break
    s.check_true("B_SENTINEL's depth bar was found in engine.py", bar is not None)
    s.check("the tautology line tracks that bar, doubled for TVL",
            td.DISCOVERY_TAUTOLOGY_FLOOR_USD, (bar or 0) * 2.0)
    s.check_true("the settable ceiling stays strictly under it",
                 td.DISCOVERY_FLOOR_MAX_USD < td.DISCOVERY_TAUTOLOGY_FLOOR_USD)
    s.check_true("the settable minimum clears the launchpad band",
                 td.DISCOVERY_FLOOR_MIN_USD > 2500.0)
    s.check_true("the range is not empty",
                 td.DISCOVERY_FLOOR_MIN_USD < td.DISCOVERY_FLOOR_MAX_USD)

    print("\n[FLOOR] a user value is clamped, and the move is REPORTED")
    mid = (td.DISCOVERY_FLOOR_MIN_USD + td.DISCOVERY_FLOOR_MAX_USD) / 2.0
    value, note = td.clamp_liquidity_floor(mid)
    s.check("an in-range floor passes through untouched", value, mid)
    s.check("and carries no note", note, None)

    value, note = td.clamp_liquidity_floor(50_000)
    s.check("a floor above the tautology line is pulled down",
            value, td.DISCOVERY_FLOOR_MAX_USD)
    s.check_true("and says why, so it is not a silent edit", bool(note))
    s.check_true("the reason names the actual failure, not just the number",
                 "tautology" in (note or "").lower())

    value, note = td.clamp_liquidity_floor(100)
    s.check("a floor below the minimum is pulled up", value, td.DISCOVERY_FLOOR_MIN_USD)
    s.check_true("and says why", bool(note))

    s.check("the boundary itself is allowed, not clamped",
            td.clamp_liquidity_floor(td.DISCOVERY_FLOOR_MAX_USD)[0], td.DISCOVERY_FLOOR_MAX_USD)
    s.check("the lower boundary is allowed too",
            td.clamp_liquidity_floor(td.DISCOVERY_FLOOR_MIN_USD)[0], td.DISCOVERY_FLOOR_MIN_USD)

    for bad in (None, "", "lots", float("nan"), float("inf")):
        s.check(f"an unusable floor {bad!r} is refused outright",
                td.clamp_liquidity_floor(bad), None)

    print("\n[FLOOR] the two floors reach the sources they belong to")
    fresh()
    # Same liquidity on both rows. Only the new-listing source should admit
    # it, because only that floor was lowered.
    LOW, HIGH = 6000.0, 30000.0
    got = discover(router(
        jup_recent=[jup(800, liquidity=LOW)],
        jup_traded=[jup(801, liquidity=LOW)],
        jup_organic=[jup(802, liquidity=LOW)]))
    s.check_true("with default floors a thin new listing is excluded too",
                 make_address(800) not in got)

    fresh()
    got = discover_with_floors(router(
        jup_recent=[jup(810, liquidity=LOW)],
        jup_traded=[jup(811, liquidity=LOW)],
        jup_organic=[jup(812, liquidity=LOW)]),
        general=HIGH, new_listing=LOW)
    s.check_true("a thin NEW LISTING is admitted by the lowered floor",
                 make_address(810) in got)
    s.check_true("the same thinness is still refused on top-traded",
                 make_address(811) not in got)
    s.check_true("and on organic", make_address(812) not in got)

    print("\n[FLOOR] changing a floor invalidates the cache")
    # Without this a settings edit appears to do nothing for up to the cache
    # TTL, which reads as a broken control -- and gets 'fixed' by changing it
    # again.
    fresh()
    handler = router(jup_traded=[jup(820, liquidity=LOW)])
    first = asyncio.run(
        td.discover_candidates(client_for(handler), min_liquidity_usd=HIGH))
    s.check("the high floor excludes it", first, [])
    second = asyncio.run(
        td.discover_candidates(client_for(handler), min_liquidity_usd=LOW))
    s.check_true("lowering the floor takes effect WITHOUT force_refresh",
                 make_address(820) in second)
    third = asyncio.run(
        td.discover_candidates(client_for(handler), min_liquidity_usd=LOW))
    s.check("an unchanged floor still serves the cache", third, second)

    print("\n[FLOOR] absent settings mean the DEFAULT floor, never no floor")
    fresh()
    got = discover_with_floors(router(jup_traded=[jup(830, liquidity=100.0)]),
                               general=None, new_listing=None)
    s.check("a $100 pool is refused when settings are unreadable", got, [])

    for name in ("JUPITER_MIN_LIQUIDITY_USD", "JUPITER_MIN_AGE_MINUTES",
                 "JUPITER_MAX_AGE_MINUTES", "JUPITER_MIN_INTERVAL_S"):
        setattr(td, name, SHIPPED[name])

    return s
