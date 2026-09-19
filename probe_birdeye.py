# probe_birdeye.py
"""Read-only shape probe for the Birdeye API. Changes nothing, writes nothing.

WHY A PROBE AND NOT JUST WRITING THE PARSER
Birdeye's reference pages document parameters but not response FIELD NAMES, so
the JSON path to a mint address is unknown for all three endpoints, and whether
/defi/v2/tokens/new_listing carries a liquidity figure is unknown too. That
second one is a design decision, not a detail: if new listings report liquidity,
the holding pen keeps applying a floor at capture; if they do not, the floor
moves entirely to min_liquidity on the token-list call.

This prints what each endpoint actually returns -- top-level keys, item keys,
and any field that looks like a Solana mint -- so the parser is written against
observed shape instead of an assumption. The last two attempts to estimate
provider behaviour from a glance were both wrong by a factor of five.

Usage, inside the web container (needs BIRDEYE_API_KEY in the environment):
    docker compose exec web python probe_birdeye.py
    docker compose exec web python probe_birdeye.py --burst   # also probe the rate limit
    docker compose exec web python probe_birdeye.py --sorts   # which sort_by values work
    docker compose exec web python probe_birdeye.py --window  # pen, or stateless?
"""
import os
import sys
import time
import json
import asyncio

import httpx

BASE = os.environ.get("BIRDEYE_API_BASE", "https://public-api.birdeye.so")
KEY = os.environ.get("BIRDEYE_API_KEY", "").strip()
SPACING_S = 1.2          # 60/min is enforced per-second, so stay under 1/s

B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def looks_like_mint(value):
    return (isinstance(value, str) and 32 <= len(value) <= 44
            and all(c in B58 for c in value))


def find_mint_paths(obj, prefix=""):
    """Every path in this object whose value looks like a Solana mint.

    Reported rather than assumed: 'address', 'mint', 'tokenAddress' and
    'base_token.id' are all plausible and only one is real.
    """
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            found += find_mint_paths(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list) and obj:
        found += find_mint_paths(obj[0], f"{prefix}[0]")
    elif looks_like_mint(obj):
        found.append((prefix, obj))
    return found


def rows_of(payload):
    """Birdeye wraps results differently per endpoint. Try the known shapes."""
    if not isinstance(payload, dict):
        return [], "(not a dict)"
    data = payload.get("data")
    if isinstance(data, list):
        return data, "data[]"
    if isinstance(data, dict):
        for key in ("items", "tokens", "updateUnixTime", "list"):
            if isinstance(data.get(key), list):
                return data[key], f"data.{key}[]"
        for key, value in data.items():
            if isinstance(value, list):
                return value, f"data.{key}[]"
    return [], f"(no list found; data keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__})"


async def probe(client, label, path, params):
    print(f"\n{'=' * 70}\n{label}\n  GET {path}\n  params {params}")
    t0 = time.monotonic()
    try:
        r = await client.get(f"{BASE}{path}", params=params, timeout=25.0)
    except Exception as e:
        print(f"  TRANSPORT FAILURE {type(e).__name__}: {e}")
        return None
    ms = (time.monotonic() - t0) * 1000.0
    print(f"  HTTP {r.status_code}  ({ms:.0f}ms)")

    if r.status_code == 401 or r.status_code == 403:
        print("  -> Key rejected. Check BIRDEYE_API_KEY is set in the ROOT .env AND")
        print("     listed in docker-compose.yml's web environment: block.")
        print(f"  body: {r.text[:300]}")
        return None
    if r.status_code == 429:
        print("  -> Rate limited. Free tier is 1 request/second, enforced per-second.")
        return None
    if r.status_code != 200:
        print(f"  body: {r.text[:400]}")
        return None

    try:
        payload = r.json()
    except Exception as e:
        print(f"  response was not JSON: {e}\n  body: {r.text[:200]}")
        return None

    print(f"  top-level keys: {sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__}")
    if isinstance(payload, dict) and "success" in payload:
        print(f"  success={payload.get('success')}")
    rows, where = rows_of(payload)
    print(f"  items found at: {where}   count={len(rows)}")
    if not rows:
        print(f"  raw (truncated): {json.dumps(payload)[:500]}")
        return payload

    first = rows[0]
    if isinstance(first, dict):
        print(f"  item keys ({len(first)}): {sorted(first.keys())}")
        mints = find_mint_paths(first)
        if mints:
            print("  mint-shaped fields:")
            for p, v in mints:
                print(f"    {p} = {v}")
        else:
            print("  NO mint-shaped field found -- the parser cannot be written from this")
        # the fields that decide the pen's design
        for key in ("liquidity", "liquidityAddedAt", "liquidity_added_at",
                    "v24hUSD", "volume_24h_usd", "marketCap", "market_cap",
                    "createdAt", "created_at", "blockUnixTime", "listingTime"):
            if key in first:
                print(f"  >> {key} = {first[key]!r}")
    else:
        print(f"  first item is {type(first).__name__}: {str(first)[:200]}")
    return payload


async def probe_sorts(client):
    """Which sort_by values does /defi/v3/token/list actually accept?

    This decides the architecture, not a parameter. Sorting by liquidity
    returns wrapped SOL and the stablecoins -- the opposite of a memecoin
    sample. But `recent_listing_time` is present in the item fields, and if it
    is also a valid sort key then

        sort_by=recent_listing_time & min_liquidity=25000

    returns the newest tokens that ALREADY carry real liquidity: precisely the
    population the holding pen was built to assemble, done server-side in one
    call. The docs promise 47 sort options without listing them, so the only
    way to know is to ask.
    """
    print(f"\n{'=' * 70}\nSORT KEYS -- does the provider do the pen's job for us?")
    candidates = [
        ("recent_listing_time", "newest first -- would replace the pen's assembly job"),
        ("last_trade_unix_time", "most recently traded"),
        ("volume_24h_usd", "busiest by turnover"),
        ("trade_5m_count", "busiest right now"),
        ("holder", "widest distribution"),
        ("liquidity", "known-good control -- must pass"),
    ]
    results = {}
    for key, why in candidates:
        await asyncio.sleep(SPACING_S)
        try:
            r = await client.get(f"{BASE}/defi/v3/token/list", timeout=25.0, params={
                "sort_by": key, "sort_type": "desc",
                "min_liquidity": 25000, "offset": 0, "limit": 10})
        except Exception as e:
            print(f"  {key:22} TRANSPORT {type(e).__name__}")
            continue
        rows, _ = rows_of(r.json()) if r.status_code == 200 else ([], "")
        ok = r.status_code == 200 and len(rows) > 0
        results[key] = ok
        note = ""
        if ok and isinstance(rows[0], dict):
            first = rows[0]
            note = (f"top={first.get('symbol')!r} liq=${(first.get('liquidity') or 0):,.0f}"
                    f" listed={first.get('recent_listing_time')}")
        print(f"  {key:22} HTTP {r.status_code} n={len(rows):<4} {note}")
        print(f"  {'':22} {why}")

    print("\n  VERDICT")
    if results.get("recent_listing_time"):
        print("  recent_listing_time IS a valid sort key. One call returns the newest")
        print("  tokens already above the liquidity floor -- the pen no longer has to")
        print("  assemble that population itself, only enforce the age hold that keeps")
        print("  a token from being evaluated before any provider has priced it.")
    else:
        print("  recent_listing_time is NOT accepted. The pen keeps assembling the")
        print("  population from /defi/v2/tokens/new_listing, as designed.")
    if not results.get("liquidity"):
        print("  WARNING: the control sort failed too -- treat every result above as")
        print("  suspect and re-run; this looks like a key or quota problem, not a")
        print("  statement about which sort keys exist.")


async def probe_window(client, hold_min=15.0, retain_min=60.0):
    """Can one call cover the whole evaluation window, or is state needed?

    The holding pen exists because GeckoTerminal could not be asked for
    "recent AND liquid" in one question: recency and liquidity came from
    different endpoints, so the two had to be joined in memory over time.
    Birdeye answers both at once, which makes a stateless design possible:

        fetch newest-first above the floor, keep rows aged hold..hold+retain

    No capture, no filing, no promotion, no expiry, no module state that
    empties on every restart. Strictly less machinery to be wrong.

    It only works if ONE page reaches back past hold+retain. If listings
    above the floor arrive faster than that, the tail of the window falls off
    the end of the page and the pen (or offset paging) is still required. So
    the deciding number is the age of the OLDEST row on the page.
    """
    print(f"\n{'=' * 70}\nWINDOW -- does one page cover {hold_min:.0f}-{hold_min + retain_min:.0f} minutes?")
    r = await client.get(f"{BASE}/defi/v3/token/list", timeout=30.0, params={
        "sort_by": "recent_listing_time", "sort_type": "desc",
        "min_liquidity": 25000, "offset": 0, "limit": 100})
    if r.status_code != 200:
        print(f"  HTTP {r.status_code} -- cannot decide the design from this")
        return
    rows, where = rows_of(r.json())
    now = time.time()
    ages = []
    for row in rows:
        listed = row.get("recent_listing_time") if isinstance(row, dict) else None
        if isinstance(listed, (int, float)) and listed > 0:
            ages.append((now - float(listed)) / 60.0)
    print(f"  {len(rows)} rows at {where}; {len(ages)} carry a listing time")
    if not ages:
        print("  No usable timestamps -- the stateless design cannot be evaluated.")
        return
    ages.sort()
    in_window = [a for a in ages if hold_min <= a <= hold_min + retain_min]
    print(f"  age span: newest {ages[0]:.1f} min, oldest {ages[-1]:.1f} min "
          f"({ages[-1] / 60.0:.1f} hours of listings)")
    print(f"  inside the {hold_min:.0f}-{hold_min + retain_min:.0f} min window right now: {len(in_window)}")
    print(f"  already too old to evaluate: {sum(1 for a in ages if a > hold_min + retain_min)}")
    print(f"  still too young to price:    {sum(1 for a in ages if a < hold_min)}")

    # Listings per hour above the floor, from the span we can see.
    rate = (len(ages) / (ages[-1] / 60.0)) if ages[-1] > 0 else 0.0
    print(f"\n  arrival rate above the floor: ~{rate:.0f} qualifying listings/hour")
    print("  VERDICT")
    if ages[-1] >= hold_min + retain_min:
        print(f"  One page reaches back {ages[-1]:.0f} min, past the {hold_min + retain_min:.0f} min")
        print("  window. A STATELESS source works: fetch, filter by age, done. The pen")
        print("  and its capture/promote/expire machinery can be deleted outright.")
        margin = ages[-1] / (hold_min + retain_min)
        print(f"  Headroom is {margin:.1f}x. Below ~2x, a busy hour could shorten the")
        print("  page's reach past the window, so the filter should log when its")
        print("  oldest row is younger than the window rather than silently truncate.")
    else:
        print(f"  One page reaches back only {ages[-1]:.0f} min, short of {hold_min + retain_min:.0f}.")
        print("  Listings arrive faster than one page covers, so the tail of the window")
        print("  falls off the end. Keep the pen, or page with offset.")


async def main(burst):
    if not KEY:
        print("BIRDEYE_API_KEY is empty inside this container.\n\n"
              "Add it to the ROOT .env:\n    BIRDEYE_API_KEY=...\n\n"
              "AND to docker-compose.yml under the web service's environment:\n"
              "    - BIRDEYE_API_KEY=${BIRDEYE_API_KEY:-}\n\n"
              "Compose only forwards variables it names, so the second step is not\n"
              "optional -- without it the key is invisible in here and this looks\n"
              "exactly like a bad key.")
        return 1

    print(f"Probing {BASE} with a {len(KEY)}-character key "
          f"(...{KEY[-4:]}), {SPACING_S}s between calls.")
    headers = {"X-API-KEY": KEY, "x-chain": "solana", "accept": "application/json"}

    async with httpx.AsyncClient(headers=headers) as client:
        new_listing = await probe(
            client, "1. NEW LISTINGS -- feeds the holding pen",
            "/defi/v2/tokens/new_listing",
            {"limit": 20, "meme_platform_enabled": "true"})
        await asyncio.sleep(SPACING_S)

        await probe(
            client, "2. TOKEN LIST v3 -- replaces the 4-page pool walk, filtered server-side",
            "/defi/v3/token/list",
            {"sort_by": "liquidity", "sort_type": "desc",
             "min_liquidity": 25000, "offset": 0, "limit": 100})
        await asyncio.sleep(SPACING_S)

        await probe(
            client, "3. TRENDING -- replaces the GeckoTerminal trending source",
            "/defi/token_trending",
            {"sort_by": "rank", "sort_type": "asc", "offset": 0, "limit": 20})

        if "--sorts" in sys.argv:
            await probe_sorts(client)

        if "--window" in sys.argv:
            await asyncio.sleep(SPACING_S)
            await probe_window(client)

        if burst:
            print(f"\n{'=' * 70}\n4. RATE LIMIT -- 5 calls with no spacing")
            print("   Expect 429s if the limit is per-second. This is the number that")
            print("   sets the throttle interval, so it is worth one deliberate test.")
            codes = []
            t0 = time.monotonic()
            for i in range(5):
                try:
                    r = await client.get(f"{BASE}/defi/token_trending",
                                         params={"limit": 1}, timeout=20.0)
                    codes.append(r.status_code)
                except Exception as e:
                    codes.append(type(e).__name__)
            elapsed = time.monotonic() - t0
            print(f"   {codes}  over {elapsed:.2f}s")
            ok = sum(1 for c in codes if c == 200)
            print(f"   {ok}/5 succeeded -> "
                  f"{'no per-second cap at this rate' if ok == 5 else 'per-second cap confirmed; throttle required'}")

    # The one answer that changes the design rather than the parser.
    print(f"\n{'=' * 70}\nDESIGN QUESTION")
    rows, _ = rows_of(new_listing or {})
    first = rows[0] if rows and isinstance(rows[0], dict) else {}
    liq_fields = [k for k in first if "liquid" in k.lower()]
    if liq_fields:
        print(f"new_listing DOES report liquidity ({liq_fields}).")
        print("-> The holding pen keeps its capture-time floor, unchanged in spirit.")
    elif first:
        print("new_listing does NOT report liquidity.")
        print("-> The capture-time floor cannot be applied, so the pen files every new")
        print("   mint and the liquidity frame moves to min_liquidity on the token-list")
        print("   call. That is a real change to what the sample means, not a detail.")
    else:
        print("Could not read new_listing -- rerun before we build anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--burst" in sys.argv)))
