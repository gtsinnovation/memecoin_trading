"""Find out which Jupiter token endpoints exist, and what they return.

    docker compose exec web python probe_jupiter_sources.py

WHY THIS EXISTS RATHER THAN AN ADAPTER

Birdeye is returning 400 on all three of its sources, so discovery is running
on the GeckoTerminal pool walk -- which is explicitly NOT liquidity-filtered
at the source. Jupiter is the obvious replacement: it is keyless, it is
already a dependency of this project (price, token age, slippage), and its
token lists cover the same three jobs Birdeye did (recently listed, most
traded, trending).

What it does NOT do is publish a schema this codebase can assume. Writing a
provider against a guessed field name is how `mode`, `reject_reason` and the
verify_solders constants each became a wasted round trip in this project. So
this asks the API instead: which URLs answer, how many rows, what the fields
are actually called, and one real record per endpoint.

It also re-reads Birdeye's 400 body, because the status code alone cannot
tell a malformed parameter from an endpoint that is not on your plan, and
those want opposite fixes.

Changes nothing. Reads only.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BIRDEYE_KEY = os.environ.get("BIRDEYE_API_KEY", "").strip()

# Candidates, broadest first. Several of these will 404 -- that IS the result:
# it tells us which family of endpoints is live right now rather than which
# one was live when some blog post was written.
JUPITER = [
    ("lite v2 recent",        "https://lite-api.jup.ag/tokens/v2/recent"),
    ("lite v2 toptraded 24h", "https://lite-api.jup.ag/tokens/v2/toptraded/24h"),
    ("lite v2 toporganic 24h","https://lite-api.jup.ag/tokens/v2/toporganicscore/24h"),
    ("lite v2 tag verified",  "https://lite-api.jup.ag/tokens/v2/tag?query=verified"),
    ("api v2 recent",         "https://api.jup.ag/tokens/v2/recent"),
    ("api v2 toptraded 24h",  "https://api.jup.ag/tokens/v2/toptraded/24h"),
    ("v1 legacy tokens",      "https://tokens.jup.ag/tokens?tags=verified"),
    ("datapi recent",         "https://datapi.jup.ag/v1/pools/recent"),
    ("datapi toptraded 24h",  "https://datapi.jup.ag/v1/pools/toptraded/24h"),
    ("datapi toptrending 24h","https://datapi.jup.ag/v1/pools/toptrending/24h"),
]

BIRDEYE = [
    ("birdeye-recent",
     "https://public-api.birdeye.so/defi/v3/token/list"
     "?sort_by=recent_listing_time&sort_type=desc&min_liquidity=25000&offset=0&limit=100"),
    ("birdeye-trending",
     "https://public-api.birdeye.so/defi/token_trending?sort_by=rank&sort_type=asc&offset=0&limit=20"),
    ("birdeye-market",
     "https://public-api.birdeye.so/defi/v3/token/list"
     "?sort_by=volume_24h_usd&sort_type=desc&min_liquidity=25000&offset=0&limit=100"),
]

# Fields discovery actually needs from whatever replaces Birdeye. Reported
# per endpoint so the answer is "this one can do the job" rather than "this
# one returned 200".
WANTED = ("mint id address symbol name liquidity usdPrice mcap marketCap "
          "volume24h stats24h holderCount organicScore audit createdAt "
          "firstPool tags decimals").split()


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {
        "Accept": "application/json", "User-Agent": "jupiter-source-probe/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read(), None
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return e.code, body, None
    except Exception as e:
        return None, b"", f"{type(e).__name__}: {e}"


def rows_of(payload):
    """Jupiter has used a bare list, {data: [...]} and {pools: [...]}."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "pools", "tokens", "items", "result"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return None


def describe(label, url):
    status, raw, err = fetch(url)
    if err:
        print(f"  {label:<24} NETWORK  {err}")
        return
    if status != 200:
        snippet = raw[:200].decode("utf-8", "replace").replace("\n", " ")
        print(f"  {label:<24} {status}      {snippet}")
        return
    try:
        payload = json.loads(raw)
    except Exception:
        print(f"  {label:<24} 200      (not JSON, {len(raw)} bytes)")
        return
    rows = rows_of(payload)
    if rows is None:
        keys = list(payload)[:12] if isinstance(payload, dict) else type(payload).__name__
        print(f"  {label:<24} 200      no row list found; top-level keys: {keys}")
        return
    if not rows:
        print(f"  {label:<24} 200      0 rows")
        return
    first = rows[0] if isinstance(rows[0], dict) else {}
    present = [f for f in WANTED if f in first]
    print(f"  {label:<24} 200      {len(rows):>4} rows | useful fields: "
          f"{', '.join(present) if present else '(none of the expected names)'}")
    print(f"  {'':<24}          all keys: {sorted(first)}")
    print(f"  {'':<24}          sample: {json.dumps(first)[:420]}")
    print()


def main():
    print("=" * 100)
    print("JUPITER TOKEN SOURCES -- what exists, and what it actually returns")
    print("=" * 100)
    for label, url in JUPITER:
        describe(label, url)

    print("=" * 100)
    print("BIRDEYE -- the 400 body, which says whether it is parameters or plan")
    print(f"BIRDEYE_API_KEY: {'set (' + str(len(BIRDEYE_KEY)) + ' chars)' if BIRDEYE_KEY else 'NOT SET'}")
    print("=" * 100)
    headers = {"Accept": "application/json", "User-Agent": "jupiter-source-probe/1.0",
               "x-chain": "solana"}
    if BIRDEYE_KEY:
        headers["X-API-KEY"] = BIRDEYE_KEY
    for label, url in BIRDEYE:
        status, raw, err = fetch(url, headers)
        body = raw[:300].decode("utf-8", "replace").replace("\n", " ") if raw else "(empty)"
        print(f"  {label:<20} {err or status}  {body}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
