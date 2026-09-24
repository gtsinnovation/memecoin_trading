# tests/test_prices.py
"""Which side of a pair a token sits on decides its price.

THE DEFECT THESE GUARD
DexScreener's `priceUsd` is always the BASE token's price, and both of this
project's price endpoints return pairs in which a requested token appears on
EITHER side. Two functions read those responses and both ignored the side:

  fetch_dex_pair_data()      took priceUsd unconditionally -- even though the
                             code three lines above had already worked out
                             which side we were on, in order to pick the
                             symbol. The side check existed; it was applied to
                             the label and not to the number.

  fetch_current_prices_sync() keyed the result by baseToken.address rather
                             than by the address requested, so a quote-side
                             token's price was filed under a DIFFERENT token
                             and ours was absent entirely.

Neither failed loudly. The first recorded another asset's price as ours; the
second produced "No current price for X this tick", which reads as a missing
token rather than a misfiled one.

WHAT IT COST
63 tokens and 390 horizon rows corrupted. Because the wrong values were
enormous rather than merely wrong, they destroyed the mean of the entire
REJECTED cohort -- 970,987% at the 30-minute horizon against a median of
0.00 -- which made the cohort comparison, the point of the whole experiment,
unreadable.

The SNDK fixtures below are the real observed numbers.
"""
import asyncio

import httpx

from .harness import Suite, make_address


def run(market_data) -> Suite:
    s = Suite("price side resolution")
    md = market_data

    SNDK = "SNDKbwMUQvZhnLnxLduradgLHG5KrPuKwpnrkkGRhfH"
    USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    OTHER = make_address(901)

    def pair(base_addr, base_sym, quote_addr, quote_sym,
             price_usd, price_native, liq):
        return {"baseToken": {"address": base_addr, "symbol": base_sym},
                "quoteToken": {"address": quote_addr, "symbol": quote_sym},
                "priceUsd": str(price_usd), "priceNative": str(price_native),
                "liquidity": {"usd": liq},
                "volume": {"h1": 1000.0, "h24": 24000.0},
                "txns": {"h1": {"buys": 10, "sells": 5},
                         "m5": {"buys": 2, "sells": 1}},
                "priceChange": {"h1": 1.0, "m5": 0.5}}

    # ------------------------------------------------------------ the unit
    print("\n[SIDE] the price of OUR token, not the pair's headline price")

    base_side = pair(SNDK, "SNDK", USDC, "USDC", 1637.0056, 1637.005604, 99139.43)
    s.check("base side takes priceUsd directly",
            md._price_for_side(base_side, SNDK), 1637.0056)

    # The same pair asked about from the other side: USDC is the quote, and
    # its price is priceUsd / priceNative = 1637.0056 / 1637.005604 = ~$1.
    quote_price = md._price_for_side(base_side, USDC)
    s.check_true("quote side derives ~$1 for USDC, not $1637",
                 quote_price is not None and abs(quote_price - 1.0) < 0.01)

    print("\n[SIDE] the exact shape that corrupted the data")
    # A pool where our token is the QUOTE and the base is a cheap token.
    # Before the fix this returned 0.001367 as SNDK's price.
    trap = pair(OTHER, "CHEAP", SNDK, "SNDK", 0.001367, 0.000000835, 250000.0)
    got = md._price_for_side(trap, SNDK)
    s.check_true("SNDK is NOT given the base token's $0.001367",
                 got is None or abs(got - 0.001367) > 0.001)
    s.check("the base token still gets its own price",
            md._price_for_side(trap, OTHER), 0.001367)

    print("\n[SIDE] a pair we did not ask about is attributed to nobody")
    # The batch endpoint returns pairs for every requested token; a pair whose
    # sides are both strangers must not be filed anywhere.
    stranger = pair(make_address(902), "A", make_address(903), "B", 5.0, 1.0, 1000.0)
    s.check("neither side matches -> None",
            md._price_for_side(stranger, SNDK), None)

    print("\n[SIDE] unusable numbers are refused, never coerced to zero")
    # Zero would mark an open position to a total loss and trip its stop.
    for label, bad in (("zero", 0), ("negative", -3), ("empty", ""),
                       ("non-numeric", "abc"), ("missing", None)):
        p = pair(SNDK, "SNDK", USDC, "USDC", bad, 1.0, 1000.0)
        s.check(f"{label} priceUsd -> None", md._price_for_side(p, SNDK), None)
    # Quote side with no priceNative cannot be derived. Returning priceUsd
    # here IS the defect, so None is the only acceptable answer.
    p = {"baseToken": {"address": OTHER, "symbol": "X"},
         "quoteToken": {"address": SNDK, "symbol": "SNDK"},
         "priceUsd": "1637.0", "liquidity": {"usd": 1.0}}
    s.check("quote side without priceNative -> None", md._price_for_side(p, SNDK), None)

    # ---------------------------------------- absent counts are not zeroes
    # `int(bucket.get("buys") or 0)` collapsed "DexScreener says zero trades"
    # (a dead token -- a real, damning measurement) into "DexScreener sent no
    # txns block" (we don't know). Both arrived downstream as a hard 0, so
    # section 5b's liveness filter silently binned every unmeasured token with
    # the dead ones, and staleness_report counted them as having no
    # counterparty. The two facts must stay distinguishable.
    s.check("a reported zero stays zero", md._opt_count({"buys": 0}, "buys"), 0)
    s.check("an absent key is unknown, not zero", md._opt_count({}, "buys"), None)
    s.check("an absent bucket is unknown, not zero", md._opt_count(None, "buys"), None)
    s.check("an explicit null is unknown, not zero",
            md._opt_count({"buys": None}, "buys"), None)
    s.check("garbage is unknown, not zero", md._opt_count({"buys": "x"}, "buys"), None)
    s.check("a real count survives", md._opt_count({"buys": "37"}, "buys"), 37)

    # -------------------------------------------------- entry path (async)
    print("\n[ENTRY] fetch_dex_pair_data prices the token it was asked about")

    def entry_client(pairs):
        def handler(request):
            return httpx.Response(200, json=pairs)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def fetch(pairs, addr):
        return asyncio.run(md.fetch_dex_pair_data(entry_client(pairs), addr))

    # The deepest pool is the trap: our token is the quote side there.
    data = fetch([base_side, trap], SNDK)
    s.check_true("a result was returned", data is not None)
    if data:
        s.check_true("priced as SNDK (~$1637), not as the base token ($0.0014)",
                     abs(data["price_usd"] - 1637.0056) < 1.0)
        s.check("the symbol is still our token's", data["token_symbol"], "SNDK")
        # Side-independent fields come from the deepest pool, unchanged. This
        # matters: B_SENTINEL and E_BREADTH read depth and txn counts, and the
        # fix must not perturb inputs that were already correct.
        s.check("liquidity still taken from the deepest pool",
                data["liquidity_usd"], 250000.0)
        s.check("txn counts still taken from the deepest pool",
                data["txns_h1_buys"], 10)

    print("\n[ENTRY] an unpriceable deepest pool is refused, not guessed")
    unpriceable = {"baseToken": {"address": OTHER, "symbol": "X"},
                   "quoteToken": {"address": SNDK, "symbol": "SNDK"},
                   "priceUsd": "0.001367", "liquidity": {"usd": 999999.0}}
    s.check("no usable price -> None rather than another pool's number",
            fetch([unpriceable], SNDK), None)

    s.check("no pairs at all -> None", fetch([], SNDK), None)

    # --------------------------------------------------- mark path (sync)
    print("\n[MARK] fetch_current_prices_sync keys by the REQUESTED address")

    class _StubClient:
        """Minimal stand-in for httpx.Client.

        The response MUST carry a request: raise_for_status() throws a
        RuntimeError without one, and market_data catches broad exceptions
        around the whole batch -- so a malformed stub silently produces an
        empty result that looks exactly like the bug under test passing.
        """
        def __init__(self, payload):
            self._payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url):
            return httpx.Response(200, json=self._payload,
                                  request=httpx.Request("GET", url))

    def batch(payload, addresses):
        real = md.httpx.Client
        md.httpx.Client = lambda *a, **k: _StubClient(payload)
        try:
            return md.fetch_current_prices_sync(addresses)
        finally:
            md.httpx.Client = real

    # Our token is the quote side of the only pair returned. Before the fix
    # this filed $0.001367 under OTHER and left SNDK absent -- the "No current
    # price" warning.
    prices = batch([trap], [SNDK])
    s.check_true("the requested token is present", SNDK in prices)
    s.check_true("and is NOT given the base token's price",
                 SNDK not in prices or abs(prices[SNDK] - 0.001367) > 0.001)
    s.check_true("a token we did not ask about is not returned", OTHER not in prices)

    print("\n[MARK] base-side lookups still work, and the deepest pool wins")
    shallow = pair(SNDK, "SNDK", USDC, "USDC", 1600.0, 1600.0, 10.0)
    deep = pair(SNDK, "SNDK", USDC, "USDC", 1637.0, 1637.0, 500000.0)
    prices = batch([shallow, deep], [SNDK])
    s.check("the deepest pool's price is kept", prices.get(SNDK), 1637.0)
    prices = batch([deep, shallow], [SNDK])
    s.check("order does not change which pool wins", prices.get(SNDK), 1637.0)

    print("\n[MARK] several tokens in one batch, on different sides")
    prices = batch([base_side, trap], [SNDK, OTHER])
    s.check_true("the base-side token is priced", OTHER in prices)
    s.check_true("the quote-side token is priced too", SNDK in prices)
    s.check_true("neither got the other's price",
                 abs(prices.get(SNDK, 0) - prices.get(OTHER, 0)) > 1.0)

    print("\n[CHANGE] priceChange is the BASE token's move and is not ours to copy")
    # The one field on a pair that is still side-dependent. Unlike price there
    # is no derivation for the quote side -- the quote's own USD move is not
    # recoverable from the base's percentage -- so the only honest answer is
    # None. Copying it across puts a momentum figure belonging to a
    # neighbouring asset onto our token, and feature_correlations() then
    # regresses that invented feature against the real return.
    moved = pair(SNDK, "SNDK", USDC, "USDC", 1637.0, 1637.0, 99139.0)
    moved["priceChange"] = {"h1": 400.0, "m5": 55.0}
    data = fetch([moved], SNDK)
    s.check("base side keeps its own 1h change", data and data["price_change_h1"], 400.0)
    s.check("base side keeps its own 5m change", data and data["price_change_m5"], 55.0)

    trap_moved = pair(OTHER, "CHEAP", SNDK, "SNDK", 0.001367, 0.000000835, 250000.0)
    trap_moved["priceChange"] = {"h1": 400.0, "m5": 55.0}
    data = fetch([trap_moved, base_side], SNDK)
    s.check_true("a result was still returned", data is not None)
    s.check("quote side reports NO 1h change rather than the base token's",
            data and data["price_change_h1"], None)
    s.check("quote side reports NO 5m change either",
            data and data["price_change_m5"], None)

    print("\n[SLIPPAGE] a reported impact of exactly zero is unmeasured, not free")
    # Jupiter returns "0" when it routes through an AMM that reports no
    # impact. Left as 0.0 it set _slippage_data_missing = False, skipping
    # G_ANCHOR's fail-closed branch and storing a zero round-trip cost for
    # exactly the thin tokens whose real costs are largest.
    def impact(payload):
        def handler(request):
            return httpx.Response(200, json=payload)
        return asyncio.run(
            md.fetch_price_impact_pct(
                httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                make_address(950 + impact.n), 500.0))
    impact.n = 0

    for label, raw in (("string zero", "0"), ("numeric zero", 0), ("float zero", 0.0)):
        impact.n += 1
        s.check(f"{label} impact -> unmeasured (None)", impact({"priceImpactPct": raw}), None)
    impact.n += 1
    s.check("a real impact is still measured",
            impact({"priceImpactPct": "0.0123"}), 1.23)
    impact.n += 1
    s.check("a very deep pool still measures, not refused",
            impact({"priceImpactPct": "0.0000123"}), 0.001)
    impact.n += 1
    s.check("a missing field is unmeasured", impact({}), None)

    print("\n[HOLDERS] concentration is unmeasured unless every top-10 entry reports it")
    # F_ATLAS refuses when holder data is missing and evaluates when it is
    # present. That guard is only as good as the flag feeding it: summing
    # `_as_float(h.get("pct")) or 0.0` turned absent percentages into a zero
    # contribution, so a populated topHolders array with no pct fields yielded
    # 0.0 -- a MEASURED zero -- and the gate passed a token where one wallet
    # may hold 82% of supply as perfectly distributed. The fail-closed branch
    # was added downstream; this line kept handing it a number.
    #
    # Exercised through the real fetch, not a reimplementation of the rule --
    # a test that recomputes the logic it is checking proves nothing.
    import free_market_data as fmd

    def rugcheck(holders):
        payload = {"topHolders": holders, "totalHolders": 312, "price": 4.1e-06,
                   "totalMarketLiquidity": 41000.0}

        def handler(request):
            return httpx.Response(200, json=payload,
                                  request=httpx.Request("GET", str(request.url)))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return asyncio.run(
            fmd.fetch_rugcheck_report(client, make_address(960)))

    def holder(pct):
        row = {"address": make_address(970), "amount": 1e14}
        if pct is not None:
            row["pct"] = pct
        return row

    report = rugcheck([holder(5.0) for _ in range(10)])
    s.check("all ten reporting -> measured sum",
            report and report.get("top_10_holder_percentage"), 50.0)

    report = rugcheck([holder(None) for _ in range(10)])
    s.check_true("a report was still returned", report is not None)
    s.check("none reporting -> unmeasured (None), never 0.0",
            report and report.get("top_10_holder_percentage"), None)

    report = rugcheck([holder(8.0), holder(4.0), holder(2.0)] + [holder(None)] * 7)
    s.check("partial reporting -> unmeasured, not an understated 14.0",
            report and report.get("top_10_holder_percentage"), None)

    report = rugcheck([holder(0.0)] + [holder(5.0) for _ in range(9)])
    s.check("a genuine zero holder still counts as measured",
            report and report.get("top_10_holder_percentage"), 45.0)

    report = rugcheck([holder(82.0)] + [holder(1.0) for _ in range(9)])
    s.check("a real concentration is reported so the gate can refuse it",
            report and report.get("top_10_holder_percentage"), 91.0)

    print("\n[MARK] an unpriceable token is omitted, never zero")
    # A missing entry means "unknown" and callers leave the position alone.
    # A zero would instantly trigger its stop-loss and book a fabricated loss.
    prices = batch([stranger], [SNDK])
    s.check("unknown token omitted entirely", prices.get(SNDK), None)
    s.check("and no internal bookkeeping keys leak out",
            [k for k in prices if k.startswith("__")], [])
    return s
