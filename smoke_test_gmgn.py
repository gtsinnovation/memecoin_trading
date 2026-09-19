#!/usr/bin/env python3
"""
Live smoke test for the GMGN market-data provider.

WHY THIS EXISTS
gmgn_market_data.py was written against GMGN's own client source (endpoint
paths, auth mechanics, response envelope, field names and units), but it
was built in an environment with no network access to GMGN, so not one
line of it has ever executed against the live API. Field mappings written
from documentation are exactly the kind of thing that looks right and
isn't. This script is how you find out, before the pipeline depends on it.

It deliberately needs NO database, NO watchlist and NO Docker stack -- it
tests the data layer alone, so a failure here is unambiguously a data-layer
failure. It calls the REAL functions in gmgn_market_data.py rather than
reimplementing them, so passing here means the shipped module works.

HOW TO RUN

  Inside the container (recommended -- same environment the app runs in;
  --no-deps because this needs no database):

    docker compose run --rm --no-deps web python3 smoke_test_gmgn.py

  (GMGN_API_KEY is picked up automatically from your .env.)

  Or on your host, if you have python3 + httpx:

    GMGN_API_KEY=your-key python3 smoke_test_gmgn.py

OPTIONS
  --token <mint address>   Test against a specific Solana token instead of
                           auto-discovering one from GMGN's own hot list.

WHAT IT DOES NOT DO
No trading, no signing, no wallet, no key beyond the read-only API key.
It only issues GET/POST requests to GMGN's unsigned read endpoints and one
Jupiter quote. Nothing it does can move funds.
"""
import os
import sys
import json
import time
import asyncio
import argparse

try:
    import httpx
except ImportError:
    print("ERROR: httpx is not installed.\n")
    print("Either run this inside the container, which already has it:")
    print("  docker compose run --rm --no-deps web python3 smoke_test_gmgn.py\n")
    print("or install it for a direct run:")
    print("  pip install httpx")
    sys.exit(2)


def _load_dotenv_if_present():
    """Reads .env from the script's own directory when the variables aren't
    already in the environment.

    Docker Compose loads .env for you; running `python smoke_test_gmgn.py`
    directly does not, and that difference is a confusing way to get a
    "GMGN_API_KEY is not set" failure while staring at a .env file that
    plainly contains it. Parsed by hand rather than with python-dotenv so
    this works on a bare Python install with only httpx present.

    Real environment variables always win over the file.
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return

    # Parse the whole file first, last occurrence winning, THEN apply. Doing
    # it in one pass would let an earlier placeholder line (.env.example
    # ships `GMGN_API_KEY=` empty) shadow a real value set further down,
    # which produces the maddening "not set" error while you're looking
    # straight at the key in the file.
    parsed = {}
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                # An empty assignment is a placeholder, not a configured
                # value -- don't let it count as "set".
                if key and value:
                    parsed[key] = value
    except Exception as e:
        print(f"(Could not read .env: {e} -- falling back to environment variables only.)")
        return

    loaded = [k for k, v in parsed.items() if k not in os.environ]
    for key in loaded:
        os.environ[key] = parsed[key]
    if loaded:
        print(f"(Loaded {len(loaded)} variable(s) from .env)")


_load_dotenv_if_present()

# Force the provider before importing, so the dispatcher test at the end
# exercises the gmgn path rather than whatever your .env happens to say.
os.environ["MARKET_DATA_PROVIDER"] = "gmgn"

import gmgn_market_data
import market_data


PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results = []


def record(step: str, status: str, detail: str = ""):
    results.append((step, status, detail))
    icon = {"PASS": "  [PASS]", "FAIL": "  [FAIL]", "WARN": "  [WARN]"}[status]
    print(f"{icon} {step}")
    if detail:
        for line in detail.splitlines():
            print(f"         {line}")


def explain_gmgn_error(error_code: str, message: str) -> str:
    """Maps GMGN's documented error codes to the specific thing to go fix,
    rather than making you search their docs mid-debug."""
    guidance = {
        "AUTH_KEY_INVALID":
            "GMGN_API_KEY is wrong or revoked. Re-copy it from https://gmgn.ai/ai.",
        "AUTH_INVALID":
            "GMGN rejected the credentials. Confirm GMGN_API_KEY is the API key\n"
            "itself, not the public key you pasted into their form when creating it.",
        "AUTH_IP_BLOCKED":
            "Your IP is not on the API key's whitelist. Add this machine's public\n"
            "IP in the GMGN dashboard -- note that a server's egress IP is usually\n"
            "NOT the IP you see on your laptop.",
        "AUTH_TIMESTAMP_EXPIRED":
            "Your system clock is more than ~5 seconds off from GMGN's. Fix clock\n"
            "sync (on the host, not in the container: `sudo timedatectl set-ntp true`\n"
            "on most Linux, or restart Docker Desktop, whose VM clock drifts after\n"
            "the machine sleeps). This is a genuinely common one -- see the clock\n"
            "check in step 2 above.",
        "AUTH_CLIENT_ID_REPLAYED":
            "A client_id UUID was reused within 7s. Each request must generate a\n"
            "fresh one -- if you see this, _auth_query() in gmgn_market_data.py is\n"
            "being cached somewhere it shouldn't be.",
        "RATE_LIMIT_EXCEEDED":
            "Rate limited. STOP and wait -- do not re-run immediately. GMGN extends\n"
            "the ban by 5s for each request made before the reset time.",
        "RATE_LIMIT_BANNED":
            "Temporarily banned for exceeding the rate limit (typically 5 minutes).\n"
            "Wait it out. Re-running now makes it longer, not shorter.",
        "CHAIN_NOT_SUPPORTED":
            "GMGN rejected chain='sol', which should not happen -- check GMGN_CHAIN\n"
            "in gmgn_market_data.py hasn't been changed.",
    }
    return guidance.get(error_code, f"Unrecognized error code. GMGN said: {message!r}")


async def step_1_config():
    print("\n" + "=" * 70)
    print("STEP 1: Configuration")
    print("=" * 70)
    key = gmgn_market_data.GMGN_API_KEY
    if not key:
        record("GMGN_API_KEY is set", FAIL,
               "Not set. Add GMGN_API_KEY to your .env (get a key at\n"
               "https://gmgn.ai/ai -- you need ONLY the API key for this;\n"
               "do not create a signing key or bind a wallet).")
        return False
    masked = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "(too short to mask)"
    record("GMGN_API_KEY is set", PASS, f"Using key {masked} ({len(key)} chars)")
    record("Base URL", PASS, gmgn_market_data.GMGN_API_BASE)
    return True


async def step_2_auth_and_clock(client: httpx.AsyncClient):
    """One raw request, bypassing the module's error swallowing, so we can
    surface GMGN's exact error envelope and check clock skew. Every later
    step uses the real module functions instead."""
    print("\n" + "=" * 70)
    print("STEP 2: Authentication, reachability and clock skew")
    print("=" * 70)

    url = f"{gmgn_market_data.GMGN_API_BASE}/v1/market/hot_searches"
    body = {"params": [{"label": "hot-search", "chain": "sol",
                          "interval": gmgn_market_data.HOT_SEARCH_INTERVAL,
                          "limit": 50, "filters": []}]}
    try:
        resp = await client.post(url, params=gmgn_market_data._auth_query(),
                                   json=body, headers=gmgn_market_data._headers(),
                                   timeout=20.0)
    except gmgn_market_data.GmgnConfigError as e:
        record("Reach GMGN", FAIL, str(e))
        return None
    except Exception as e:
        record("Reach GMGN", FAIL,
               f"Could not connect: {e}\n"
               "Check outbound network access from wherever you ran this. If you're\n"
               "in the container, the container needs egress to openapi.gmgn.ai.")
        return None

    # A response arriving is not the same as a good response -- an earlier
    # version of this script reported "HTTP 403" as a PASS, which is exactly
    # the kind of green tick that wastes an afternoon.
    if resp.status_code == 200:
        record("Reach GMGN", PASS, f"HTTP {resp.status_code}")
    else:
        body_head = resp.text[:200].lstrip().lower()
        looks_like_html = body_head.startswith("<!doctype html") or body_head.startswith("<html")
        if resp.status_code == 403 and looks_like_html:
            record("Reach GMGN", FAIL,
                   f"HTTP 403 with an HTML body -- this is a Cloudflare block page, not\n"
                   f"a GMGN response. The request was rejected at their edge before it\n"
                   f"reached the API, which is why there's no JSON error code to read.\n\n"
                   f"Usual cause: the User-Agent. Cloudflare rejects default HTTP-library\n"
                   f"agents like 'python-httpx/0.27.0'. gmgn_market_data.py sends\n"
                   f"GMGN_USER_AGENT (default 'memecoin-trading-agent/1.0') -- if you are\n"
                   f"still seeing this, override it in .env, e.g.:\n"
                   f"  GMGN_USER_AGENT=gmgn-cli/1.6.1\n\n"
                   f"Currently sending: {gmgn_market_data.GMGN_USER_AGENT!r}\n\n"
                   f"Less likely: your IP has poor reputation with Cloudflare (try another\n"
                   f"network), or GMGN geo-blocks your region.")
        else:
            record("Reach GMGN", FAIL,
                   f"HTTP {resp.status_code}\nBody (truncated): {resp.text[:300]}")
        return None

    # Clock skew: AUTH_TIMESTAMP_EXPIRED is one of the most confusing
    # failures to debug, so check for it proactively rather than waiting
    # for it to bite.
    server_date = resp.headers.get("date")
    if server_date:
        try:
            from email.utils import parsedate_to_datetime
            server_ts = parsedate_to_datetime(server_date).timestamp()
            skew = abs(time.time() - server_ts)
            if skew > 5:
                record("Clock skew vs GMGN", FAIL,
                       f"Your clock is {skew:.1f}s off from GMGN's server.\n"
                       "GMGN rejects any request more than ~5s out. Fix clock sync\n"
                       "before anything else -- every signed request will fail.")
            elif skew > 2:
                record("Clock skew vs GMGN", WARN,
                       f"{skew:.1f}s off. Under the ~5s limit but close enough to\n"
                       "cause intermittent AUTH_TIMESTAMP_EXPIRED failures.")
            else:
                record("Clock skew vs GMGN", PASS, f"{skew:.1f}s -- well within tolerance")
        except Exception:
            record("Clock skew vs GMGN", WARN, "Could not parse the server Date header.")

    try:
        payload = resp.json()
    except Exception:
        record("Response is JSON", FAIL, f"Body was not JSON: {resp.text[:300]}")
        return None

    code = payload.get("code")
    if code != 0:
        err = payload.get("error") or "(no error code)"
        msg = payload.get("message") or ""
        record("Authenticated", FAIL,
               f"GMGN returned code={code} error={err}\n\n{explain_gmgn_error(err, msg)}")
        return None

    record("Authenticated", PASS, "API key accepted (code=0)")

    # Note: a non-zero code can arrive with HTTP 200, which is why the
    # module checks `code` rather than the status. Confirm that's holding.
    if resp.status_code == 200 and code == 0:
        record("Envelope contract (code==0 on success)", PASS)

    return payload


async def step_3_hot_searches(payload):
    print("\n" + "=" * 70)
    print("STEP 3: Attention data (hot searches)")
    print("=" * 70)

    data = payload.get("data")
    blocks = data if isinstance(data, list) else []
    if not blocks:
        record("Ranking returned", FAIL,
               f"Expected a list of blocks in `data`, got: {type(data).__name__}\n"
               f"Raw (truncated): {json.dumps(data)[:300]}\n"
               "The response shape has changed -- _fetch_hot_search_ranking() in\n"
               "gmgn_market_data.py needs updating to match.")
        return None

    tokens = []
    for b in blocks:
        if isinstance(b, dict):
            tokens.extend(b.get("tokens") or [])
    if not tokens:
        record("Ranking returned", FAIL, "Blocks present but contained no `tokens`.")
        return None

    record("Ranking returned", PASS,
           f"{len(tokens)} tokens across {len(blocks)} block(s)")

    sample = tokens[0]
    missing = [f for f in ("address", "rank") if f not in sample]
    if missing:
        record("Ranking rows have address+rank", FAIL,
               f"Missing {missing} -- rank scoring cannot work.\n"
               f"Row keys present: {sorted(sample.keys())[:15]}")
        return None
    record("Ranking rows have address+rank", PASS)

    if "visiting_count" in sample:
        record("visiting_count present", PASS, "(informational -- we score by rank)")
    else:
        record("visiting_count present", WARN,
               "Absent. Not fatal -- we score by rank position, not raw count.")

    print("\n         Top 3 by search heat:")
    for t in tokens[:3]:
        sym = market_data.sanitize_external_text(t.get("symbol", "?"))
        print(f"           #{t.get('rank')}  ${sym}  {t.get('address')}")

    return tokens


async def step_4_token_info(client, token_address, discovered_from):
    print("\n" + "=" * 70)
    print("STEP 4: Token fundamentals (the field mapping that matters most)")
    print("=" * 70)
    print(f"  Testing token: {token_address}")
    print(f"  ({discovered_from})\n")

    info = await gmgn_market_data.fetch_token_info(client, token_address)
    if info is None:
        record("token/info returned data", FAIL,
               "Got None. Either GMGN has no record of this token (it returns an\n"
               "empty symbol for unknown tokens), or the request failed -- check the\n"
               "log lines above this for the reason.")
        return None

    record("token/info returned data", PASS)

    checks_ok = True

    sym = info["token_symbol"]
    if sym and sym != "?":
        record("Symbol parsed", PASS, f"${sym}")
    else:
        record("Symbol parsed", FAIL, f"Got {sym!r}")
        checks_ok = False

    price = info["price_usd"]
    if price > 0:
        record("Price parsed", PASS, f"${price}")
    else:
        record("Price parsed", FAIL,
               f"Got {price}. Price arrives as a STRING under price.price --\n"
               "if this is 0, that nesting or name has changed.")
        checks_ok = False

    liq = info["liquidity_usd"]
    if liq > 0:
        record("Liquidity parsed", PASS, f"${liq:,.2f}")
    else:
        record("Liquidity parsed", WARN,
               "Got 0. We read top-level `liquidity` and fall back to\n"
               "`pool.liquidity` when it's 0. Both being 0 is possible for a very\n"
               "illiquid token, but on a top-ranked token it means the mapping is wrong.")

    v1, v24 = info["volume_h1"], info["volume_h24"]
    if v24 > 0:
        record("Volume parsed", PASS, f"1h ${v1:,.0f} / 24h ${v24:,.0f}")
    else:
        record("Volume parsed", WARN, f"1h={v1} 24h={v24} -- both zero is suspicious.")

    top10 = info["top_10_holder_percentage"]
    if top10 is None:
        record("Top-10 holder concentration", WARN,
               "Not available for this token (stat block unpopulated -- GMGN\n"
               "populates it per-token, not per-chain). The pipeline treats this as\n"
               "missing and defaults to 0, which its F_ATLAS gate will read as\n"
               "'not concentrated'. Try another token before concluding it's broken.")
    elif 0 < top10 <= 100:
        record("Top-10 holder concentration", PASS,
               f"{top10}%  <-- sanity-check this: it should be a PERCENT (0-100),\n"
               "not a fraction. If you see something like 0.17 for a token whose\n"
               "top 10 hold 17%, the 0-1 -> percent conversion has broken.")
    else:
        record("Top-10 holder concentration", FAIL,
               f"Got {top10}, which is outside 0-100. Unit conversion is wrong.")
        checks_ok = False

    return info if checks_ok else None


async def step_5_attention_score(client, token_address):
    print("\n" + "=" * 70)
    print("STEP 5: Attention score (rank -> 0-100)")
    print("=" * 70)

    score = await gmgn_market_data.fetch_social_volume_score(client, token_address)
    if score is None:
        record("Attention score", WARN,
               "None -- this token isn't in the top ranking. That's 'unknown', not\n"
               "'zero hype', and it's the normal case for most tokens. If you're\n"
               "testing an auto-discovered top token, though, None means the address\n"
               "lookup is failing.")
        return
    if 0.0 <= score <= 100.0:
        record("Attention score", PASS,
               f"{score}/100\n"
               "Sanity-check: a #1-ranked token should score 100. A mid-list token\n"
               "should land somewhere in the middle -- if everything scores 0.0,\n"
               "the rank normalization is broken.")
    else:
        record("Attention score", FAIL, f"{score} is outside 0-100.")


async def step_6_full_snapshot(client, token_address):
    print("\n" + "=" * 70)
    print("STEP 6: End-to-end snapshot through the real dispatcher")
    print("=" * 70)
    print("  This is what the pipeline actually calls each tick.\n")

    snapshot = await market_data.get_snapshot(client, token_address)
    if snapshot is None:
        record("get_snapshot() returned data", FAIL,
               "Got None -- the pipeline would skip this token every tick.")
        return

    record("get_snapshot() returned data", PASS)

    expected = {
        "token_symbol", "token_address", "current_price", "pool_liquidity_usd",
        "social_volume_score", "onchain_flow_velocity", "top_10_holder_percentage",
        "estimated_slippage_percent", "onchain_volume_increasing",
        "_holder_data_missing", "_slippage_data_missing", "_social_data_missing",
    }
    actual = set(snapshot.keys())
    if actual == expected:
        record("Snapshot contract matches the DexScreener provider", PASS)
    else:
        record("Snapshot contract matches the DexScreener provider", FAIL,
               f"Extra: {actual - expected or '(none)'}\n"
               f"Missing: {expected - actual or '(none)'}\n"
               "The two providers must return identical keys or switching between\n"
               "them will break the pipeline.")

    if snapshot.get("_slippage_data_missing"):
        record("Slippage (via Jupiter)", WARN,
               "Unavailable. Jupiter is a separate service from GMGN -- this means\n"
               "Jupiter specifically failed or has no route for this token, not that\n"
               "your GMGN setup is wrong. The G_ANCHOR gate will see 0% slippage.")
    else:
        record("Slippage (via Jupiter)", PASS,
               f"{snapshot['estimated_slippage_percent']}%")

    print("\n         Full snapshot the pipeline would act on:")
    for k, v in snapshot.items():
        print(f"           {k:32} {v}")


async def main():
    parser = argparse.ArgumentParser(description="Live smoke test for the GMGN data provider.")
    parser.add_argument("--token", help="Solana mint address to test against "
                                          "(default: auto-discover from GMGN's hot list)")
    args = parser.parse_args()

    print("\nGMGN LIVE SMOKE TEST")
    print("Read-only: no trading, no signing, no wallet. Nothing here can move funds.")

    if not await step_1_config():
        summarize()
        return 1

    async with httpx.AsyncClient() as client:
        payload = await step_2_auth_and_clock(client)
        if payload is None:
            summarize()
            return 1

        tokens = await step_3_hot_searches(payload)

        if args.token:
            token_address = args.token
            source = "supplied with --token"
        elif tokens:
            token_address = tokens[0].get("address")
            source = "auto-discovered: currently #1 by search heat on GMGN"
        else:
            print("\nNo token to test with. Re-run with --token <mint address>.")
            summarize()
            return 1

        await step_4_token_info(client, token_address, source)
        await step_5_attention_score(client, token_address)
        await step_6_full_snapshot(client, token_address)

    return summarize()


def summarize():
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    failed = [r for r in results if r[1] == FAIL]
    warned = [r for r in results if r[1] == WARN]
    passed = [r for r in results if r[1] == PASS]
    print(f"  {len(passed)} passed, {len(warned)} warnings, {len(failed)} failed")

    if failed:
        print("\n  FAILED:")
        for step, _, _ in failed:
            print(f"    - {step}")
        print("\n  Do NOT set MARKET_DATA_PROVIDER=gmgn until these pass -- the")
        print("  pipeline would be trading on data it cannot actually read.")
        return 1

    if warned:
        print("\n  Warnings are usually fine (a token missing optional data, or")
        print("  Jupiter having no route). Read them, then decide.")

    print("\n  The data layer works. Set MARKET_DATA_PROVIDER=gmgn in .env and")
    print("  restart: docker compose up -d --build")
    print("\n  Then confirm it in the running pipeline:")
    print("    docker compose logs -f web")
    print("  You should see ticks naming real symbols with non-zero prices, and")
    print("  no 'Social/attention data unavailable' warnings for ranked tokens.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
