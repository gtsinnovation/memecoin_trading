# token_discovery.py
"""Finds live Solana memecoins for the pipeline to evaluate.

WHY THIS EXISTS
WATCHLIST_TOKEN_ADDRESSES is a hand-curated list, which is fine for a demo
and useless for an experiment: the same handful of tokens evaluated
repeatedly gives a thin, heavily correlated sample, and memecoin outcomes
are variable enough that you need a large number of INDEPENDENT tokens
before any difference between cohorts means anything. Stage 2 paper trading
needs a continuous stream of genuinely different tokens.

WHO ANSWERS WHAT
Birdeye does the substantive work, because it is the only provider here that
can be asked for "recent AND liquid" in a single question:

  recency   v3/token/list sorted by recent_listing_time, min_liquidity applied
            SERVER-SIDE. This is the experiment's population.
  market    v3/token/list sorted by 24h volume -- breadth below the top.
  trending  defi/token_trending -- what Birdeye's own ranking surfaces.

Three keyless sources stay alongside it, each a different slice: RugCheck
trending (what people are researching), DexScreener boosts (what someone
paid to promote), GeckoTerminal trending pools (a second opinion on
"trending" from an unrelated vendor).

GeckoTerminal's paginated pool walk is kept but is FALLBACK ONLY -- see
_from_geckoterminal_pools().

WHAT THE HOLDING PEN WAS, AND WHY IT IS GONE
Until Birdeye was added, recency and liquidity came from different
GeckoTerminal endpoints, so the two had to be joined in memory over time: new
pools were captured with their creation time, held until old enough to be
priced, then released into the candidate list. That worked, and it was ~150
lines of capture/promote/expire state that emptied on every restart, so every
rebuild cost fifteen minutes of dead discovery.

Birdeye returns both facts in one row, which makes the whole thing a filter:
fetch newest-first above the floor, keep the rows whose age is inside the
evaluation window. Stateless, restart-proof, and there is no bookkeeping left
to be wrong. The age hold survives as the window's lower bound and is the one
part that was never optional -- see BIRDEYE_MIN_AGE_MINUTES.

SECURITY NOTE
This feeds attacker-chosen token metadata into the system automatically,
which a manual watchlist did not. That surface is defended in two places:
sanitize_external_text() strips control/zero-width/bidi characters at
ingest, and the dashboard HTML-escapes at every render site. Both matter
here. Do not weaken either while discovery is enabled.

Mint addresses are read from ONE named field per endpoint, never by searching
the payload for something base58-shaped. Birdeye's v3 rows carry
extensions.serum_v3_usdc and extensions.serum_v3_usdt, which are Serum market
addresses and look exactly like mints; a search-based parser would ingest them
and then waste a price lookup on every tick, forever.
"""
import os
import time
import asyncio
import logging
from typing import List, Dict, Any, Optional, Callable, Awaitable

import httpx

logger = logging.getLogger("token_discovery")

RUGCHECK_API_BASE = os.environ.get("RUGCHECK_API_BASE", "https://api.rugcheck.xyz")
DEXSCREENER_BASE = os.environ.get("DEXSCREENER_API_BASE", "https://api.dexscreener.com")
GECKOTERMINAL_BASE = os.environ.get("GECKOTERMINAL_API_BASE", "https://api.geckoterminal.com")
BIRDEYE_BASE = os.environ.get("BIRDEYE_API_BASE", "https://public-api.birdeye.so")
BIRDEYE_API_KEY = os.environ.get("BIRDEYE_API_KEY", "").strip()

# --- Jupiter ---------------------------------------------------------------
#
# The primary discovery source, and the reason Birdeye is no longer load
# bearing. Birdeye's free tier answers every endpoint with
# {"success":false,"message":"Compute units usage limit exceeded"} once the
# monthly budget is spent -- as a 400, which reads as a malformed request --
# and a discovery cycle every three minutes spends that budget reliably. A
# source that stops working on a schedule is not a source.
#
# lite-api is the keyless host. api.jup.ag is the metered one and answers 429
# without a key, so it is not a useful fallback for this.
#
# Jupiter's /tokens/v2 rows carry liquidity, createdAt, holderCount, mint and
# freeze authority under `audit`, and an organicScore -- strictly more than
# Birdeye supplied, and enough to keep the liquidity floor that the
# GeckoTerminal pool walk cannot.
JUPITER_TOKENS_BASE = os.environ.get("JUPITER_TOKENS_API_BASE", "https://lite-api.jup.ag")

# The same floor Birdeye applied server-side. Jupiter has no min_liquidity
# parameter, so it is applied here -- but from a field already in the
# response, so it costs no extra request.
JUPITER_MIN_LIQUIDITY_USD = float(os.environ.get("JUPITER_MIN_LIQUIDITY_USD", "25000"))

# A SEPARATE, lower floor for newly listed tokens.
#
# Birdeye and Jupiter do not mean the same thing by "recent". Birdeye filtered
# by liquidity server-side and THEN sorted by listing time, so a page was "the
# most recent tokens that already have $25k" and reached back hours. Jupiter's
# /tokens/v2/recent returns the newest mints unfiltered, and a token fifteen
# minutes old does not have $25,000 in its pool -- so the same floor applied
# to that list admits nothing, which is exactly what jupiter-recent=0 was.
#
# The two floors are not a convenience. A single floor low enough to catch new
# mints would also drag the breadth sources down into the launchpad band,
# where a "price" is one trade old. Separating them keeps each source sampling
# the population it is meant to.
# $20,000, chosen from the pen's own measurements rather than guessed. The
# 8000 that sat here before was picked before any data existed.
#
# The floor does NOT select the approved cohort -- B_SENTINEL needs $20,000 of
# tradeable depth, which is $40,000 of liquidity, so everything below that is
# rejected downstream whatever discovery admits. What the floor selects is the
# CONTROL arm, and that is why it matters: a rejected cohort made of tokens
# with $3 in the pool is not a counterfactual, it is noise that never traded.
#
# Measured on 239 newly created pools at thirty minutes old: p25 $3, median
# $2,161, p75 $33,835. Half of them are corpses. A floor at $20,000 puts the
# control arm at $20k-$40k -- live tokens refused on depth -- and keeps the
# dead mass out of a sample that only has ~190 evaluation slots an hour.
JUPITER_NEW_LISTING_MIN_LIQUIDITY_USD = float(
    os.environ.get("JUPITER_NEW_LISTING_MIN_LIQUIDITY_USD", "20000"))

# --- The bounds any user-set floor is held to ------------------------------
#
# The floor is a FRAME, not a gate, and both bounds are load bearing.
#
# Above DISCOVERY_FLOOR_MAX_USD the sampler only ever offers tokens that
# already clear B_SENTINEL's depth bar, so approval becomes a tautology, the
# REJECTED arm from that source disappears, and the cohort comparison goes
# with it. That failure is invisible: discovery keeps reporting healthy counts
# while the experiment stops being an experiment. The number is derived rather
# than typed -- B_SENTINEL judges tradeable depth, which is at most half of
# pool liquidity, so its $20,000 depth bar sits at $40,000 of LIQUIDITY.
#
# Below DISCOVERY_FLOOR_MIN_USD the control arm fills with tokens whose
# rejection is a foregone conclusion, and with pools too thin for a quoted
# price to mean anything -- at roughly 190 evaluation slots an hour, those are
# slots spent on noise.
_B_SENTINEL_DEPTH_BAR_USD = 20000.0   # engine.py node_B_SENTINEL, min_depth
_LIQUIDITY_PER_DEPTH = 2.0            # depth <= TVL / 2
DISCOVERY_TAUTOLOGY_FLOOR_USD = _B_SENTINEL_DEPTH_BAR_USD * _LIQUIDITY_PER_DEPTH
DISCOVERY_FLOOR_MAX_USD = DISCOVERY_TAUTOLOGY_FLOOR_USD * 0.95
DISCOVERY_FLOOR_MIN_USD = float(os.environ.get("DISCOVERY_FLOOR_MIN_USD", "5000"))


def clamp_liquidity_floor(value) -> Optional[tuple]:
    """(clamped_floor, note) for a user-supplied floor, or None if unusable.

    Returns the note so the caller can TELL the user their number was moved.
    Silently clamping a setting is how someone ends up believing they are
    sampling a population they are not.
    """
    try:
        floor = float(value)
    except (TypeError, ValueError):
        return None
    if floor != floor or floor in (float("inf"), float("-inf")):
        return None
    if floor < DISCOVERY_FLOOR_MIN_USD:
        return DISCOVERY_FLOOR_MIN_USD, (
            f"raised to ${DISCOVERY_FLOOR_MIN_USD:,.0f}: below that the sample fills "
            f"with pools too thin for a quoted price to mean anything, and the "
            f"evaluation slots are spent on noise")
    if floor > DISCOVERY_FLOOR_MAX_USD:
        return DISCOVERY_FLOOR_MAX_USD, (
            f"lowered to ${DISCOVERY_FLOOR_MAX_USD:,.0f}: at ${DISCOVERY_TAUTOLOGY_FLOOR_USD:,.0f} "
            f"every token offered would already clear the depth gate, so approval "
            f"becomes a tautology and the rejected control arm disappears")
    return floor, None

# Same evaluation window as the Birdeye recency source: old enough that some
# provider has priced it, young enough to still be the population this
# experiment is about.
JUPITER_MIN_AGE_MINUTES = float(os.environ.get("JUPITER_MIN_AGE_MINUTES", "15"))
JUPITER_MAX_AGE_MINUTES = float(os.environ.get("JUPITER_MAX_AGE_MINUTES", "90"))

# Discovery lists move on the order of minutes and every source is rate
# limited. Refreshing once every few minutes and serving many ticks from the
# cache keeps us far under every provider's limit.
DISCOVERY_CACHE_TTL_S = float(os.environ.get("DISCOVERY_CACHE_TTL_S", "180"))

# How long to wait before retrying after EVERY source failed. Shorter than the
# normal TTL so a transient outage recovers quickly, but not zero -- a failed
# refresh still drains the pen, so retrying on every tick destroys the
# experiment's scarcest input while the providers are down.
DISCOVERY_FAILURE_RETRY_S = float(os.environ.get("DISCOVERY_FAILURE_RETRY_S", "30"))

# Ceiling on the merged list. Expected composition is ~220: 100 market rows,
# ~40 in-window recency rows, 20 Birdeye trending, 20 GeckoTerminal trending,
# and ~40 from RugCheck and DexScreener. 300 leaves headroom without the cap
# ever silently discarding a source.
#
# A longer list does not slow the cycle: the 60-minute re-entry cooldown in
# paper_trading limits how often any one token can be recorded, so the effect
# is more DISTINCT tokens per hour rather than more repeats of the same ones.
DISCOVERY_MAX_CANDIDATES = int(os.environ.get("DISCOVERY_MAX_CANDIDATES", "300"))

# --- Birdeye ---------------------------------------------------------------
# Rows per v3/token/list call. 100 is the documented maximum and one page of
# recency-sorted rows was measured reaching back ~10 hours, so paging adds
# nothing: the evaluation window sits comfortably inside a single page.
BIRDEYE_PAGE_LIMIT = int(os.environ.get("BIRDEYE_PAGE_LIMIT", "100"))

# Liquidity floor, applied server-side by min_liquidity.
#
# SAMPLING FRAME, NOT A GATE
# This decides which launches are in the experiment at all -- the same job
# "trending" does for the other sources, and the same job the hand-curated
# watchlist used to do for the whole pipeline. Without a frame the source is
# unbounded: the large majority of new tokens launch with one or two thousand
# dollars of liquidity and are abandoned within the hour, and they would crowd
# out the sample rather than enlarge it. Measured: ~85% of new listings clear
# $1,000, ~27% clear $2,500, ~12% clear $5,000, ~5% clear $25,000.
#
# MIND THE UNITS
# This is pool liquidity -- TVL, both sides. B_SENTINEL judges
# tradeable_depth_usd, which free_market_data.compute_tradeable_depth()
# defines as min(TVL / 2, reported one-sided). Its $20,000 bar therefore sits
# near $40,000 of LIQUIDITY, and possibly higher, since depth is the minimum
# of the two terms.
#
# So $25,000 does not admit tokens that already clear the gate: everything
# here is at most ~$12,500 of one-sided depth at the moment it is seen. What
# decides approval is whether liquidity grows during the age hold, which is
# exactly the uncertainty worth spending an evaluation slot on. Both verdicts
# stay reachable for every token.
#
# Do NOT raise this to $40,000 or beyond. At or above the gate's own level
# approval becomes a tautology -- the sampler would offer only tokens that
# already pass the gate under test, and the REJECTED arm from this source
# disappears along with any ability to compare the two. Both ends of the
# usable range are pinned by tests/test_discovery.py [FRAME].
BIRDEYE_MIN_LIQUIDITY_USD = float(os.environ.get("BIRDEYE_MIN_LIQUIDITY_USD", "25000"))

# Lower bound of the evaluation window: how old a listing must be before it
# may be evaluated.
#
# This is the one piece of the old holding pen that was never optional. A
# token listed seconds ago has no price at any provider yet, so
# record_candidate() drops it at its `price <= 0` guard and the evaluation
# slot produces no observation at all -- and the pipeline manages only ~190
# evaluations an hour, so slots, not candidates, are the scarce resource.
# Fifteen minutes is enough for the price providers to have indexed it and for
# a first candle to exist.
BIRDEYE_MIN_AGE_MINUTES = float(os.environ.get("BIRDEYE_MIN_AGE_MINUTES", "15"))

# Upper bound of the evaluation window.
#
# The sampler draws uniformly at random, so a token needs to sit in the list a
# while to have a fair chance of being drawn at all: at ~190 draws/hour over a
# ~220-token list, a 75-minute window gives each arrival roughly an 83% chance.
# Widening to 165 minutes would reach ~97%, but recovers only ~1.4 tokens/hour
# and triples the spread of age-at-evaluation -- which adds variance to the
# very measurement this is meant to sharpen. The tight window is deliberate.
BIRDEYE_MAX_AGE_MINUTES = float(os.environ.get("BIRDEYE_MAX_AGE_MINUTES", "90"))

# --- GeckoTerminal (secondary) --------------------------------------------
# Pages of the volume-ranked pool list, walked ONLY in fallback. See
# _from_geckoterminal_pools().
DISCOVERY_POOL_PAGES = int(os.environ.get("DISCOVERY_POOL_PAGES", "4"))

# Courtesy spacing between paginated calls within one source. NOT a
# rate-limit control -- the per-provider throttle below is. Raising this was
# tried as a fix for 429s at 1.2s and failed, because the limit is per
# provider and several sources shared one.
DISCOVERY_PAGE_SPACING_S = float(os.environ.get("DISCOVERY_PAGE_SPACING_S", "0.4"))

# --- Per-provider throttling ----------------------------------------------
# Minimum gap between two calls to the SAME provider, enforced across every
# source in this module.
#
# WHY PER PROVIDER AND NOT PER SOURCE
# Rate limits belong to the vendor, not to the function calling it. When three
# sources shared GeckoTerminal and each spaced only its own pages, nine calls
# still arrived in a ~12-second burst; GeckoTerminal answered 429 from the
# sixth onward, and because a failed page ends a walk, whichever source was
# last in line was silently DELETED from that cycle. The candidate list fell
# from ~101 to ~32 and every log line still looked healthy.
#
# GeckoTerminal is now one call per refresh so 2.5s barely binds, but it stays
# as insurance for the fallback walk. Birdeye's 60/minute is enforced per
# second with no burst allowance, hence 1.1s.
GECKOTERMINAL_MIN_INTERVAL_S = float(os.environ.get("GECKOTERMINAL_MIN_INTERVAL_S", "2.5"))
BIRDEYE_MIN_INTERVAL_S = float(os.environ.get("BIRDEYE_MIN_INTERVAL_S", "1.1"))
# lite-api is keyless and correspondingly metered. Three calls per refresh
# at this spacing is nowhere near any published limit, and the throttle is
# per provider so it never delays anything else.
JUPITER_MIN_INTERVAL_S = float(os.environ.get("JUPITER_MIN_INTERVAL_S", "1.1"))

HEADERS = {"User-Agent": "memecoin-trading-agent/1.0", "Accept": "application/json"}

# fetched_at is None until a refresh has actually happened, and NOT 0.0.
# time.monotonic() counts from an arbitrary origin that is near zero at
# process start, so a 0.0 sentinel does not mean "never fetched" -- it means
# "fetched at second zero", which is inside the TTL for the first
# DISCOVERY_CACHE_TTL_S of every process. That made discovery serve an EMPTY
# candidate list for the first three minutes after each restart while
# reporting nothing wrong. It was masked for a long time by a redundant
# `and _cache["candidates"]` term in the TTL guard, which had to be removed
# for the cold-start backoff to work at all -- removing the mask exposed it.
_cache: Dict[str, Any] = {"candidates": [], "fetched_at": None, "floors": None}

# Provider -> monotonic time before which no further call to it may start.
_next_allowed: Dict[str, float] = {}

# Mirrors market_data.is_plausible_solana_address. Duplicated rather than
# imported so this module stays dependency-free -- it is the one place that
# ingests attacker-chosen identifiers, and it should not need the rest of the
# project loaded to do so safely.
_BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _provider_of(url: str) -> Optional[str]:
    """Which rate limit this URL spends. None means unthrottled."""
    if url.startswith(BIRDEYE_BASE):
        return "birdeye"
    if url.startswith(GECKOTERMINAL_BASE):
        return "geckoterminal"
    if url.startswith(JUPITER_TOKENS_BASE):
        return "jupiter"
    return None


def _interval_for(provider: str) -> float:
    return {"birdeye": BIRDEYE_MIN_INTERVAL_S,
            "geckoterminal": GECKOTERMINAL_MIN_INTERVAL_S,
            "jupiter": JUPITER_MIN_INTERVAL_S}.get(provider, 0.0)


async def _throttle(provider: str) -> None:
    """Spaces calls to one provider by its minimum interval.

    The slot is RESERVED before awaiting, and there is no await between
    reading _next_allowed and writing it. On a single-threaded event loop that
    makes the read-modify-write atomic, so concurrent callers queue behind
    each other instead of all measuring the same idle gap and firing together
    -- which is the bug a naive "sleep if the last call was recent" would
    reintroduce the moment anything calls this concurrently.
    """
    interval = _interval_for(provider)
    if interval <= 0:
        return
    now = time.monotonic()
    start = max(now, _next_allowed.get(provider, 0.0))
    _next_allowed[provider] = start + interval
    if start > now:
        await asyncio.sleep(start - now)


def _plausible_mint(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip()
    return 32 <= len(v) <= 44 and all(c in _BASE58 for c in v)


def _as_float(value: Any) -> Optional[float]:
    """Providers return numbers as JSON strings about as often as numbers.
    Neither a string nor a missing key should raise here."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _rows(data: Any) -> List[Any]:
    """Trending endpoints disagree about whether the list is the top-level
    response or nested under a key, and a parser that only accepted a bare
    list silently found nothing when this was first written. Accept both."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "result", "results", "tokens", "items", "pairs"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _birdeye_rows(payload: Any) -> List[Any]:
    """Birdeye nests the list one level deeper than _rows() looks, and not
    consistently: v3/token/list and v2/tokens/new_listing use data.items,
    while defi/token_trending uses data.tokens. Both were confirmed against
    live responses; assuming either one alone finds nothing and reports
    success."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "tokens", "list"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


async def _get(client: httpx.AsyncClient, name: str, url: str,
               params: Optional[Dict[str, Any]] = None,
               headers: Optional[Dict[str, str]] = None) -> Any:
    provider = _provider_of(url)
    if provider:
        await _throttle(provider)
    try:
        resp = await client.get(url, headers=headers or HEADERS,
                                params=params, timeout=20.0)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        # The status line alone is not actionable. A 400 from Birdeye means
        # the request was malformed or the endpoint is not on this plan, and
        # those want opposite fixes -- the body says which, and without it
        # the only way to tell them apart is guessing at parameters.
        body = ""
        try:
            body = (e.response.text or "")[:300].replace("\n", " ")
        except Exception:
            pass
        # Birdeye answers an exhausted compute-unit budget with a 400 and a
        # body saying so. A 400 normally means "you sent something wrong",
        # which sent us looking at parameters and headers for an hour; it is
        # worth naming the real cause where it will be read.
        if "compute unit" in body.lower() or "usage limit" in body.lower():
            logger.warning(
                f"Token discovery source {name} is OUT OF QUOTA, not misconfigured: "
                f"{body}. This resets on the provider's billing window; the keyless "
                f"sources carry discovery until it does.")
        else:
            logger.warning(
                f"Token discovery source {name} failed: {e.response.status_code} "
                f"{e.request.url} -- {body or '(no response body)'}")
        return None
    except Exception as e:
        logger.warning(f"Token discovery source {name} failed: {type(e).__name__}: {e}")
        return None


def _birdeye_headers() -> Dict[str, str]:
    return {**HEADERS, "X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}


def _mints_from(rows: List[Any], field: str = "address") -> List[str]:
    """Reads ONE named field per row. See the security note about
    extensions.serum_v3_usdc looking exactly like a mint."""
    out = []
    for row in rows:
        if isinstance(row, dict):
            value = row.get(field)
            if isinstance(value, str) and value:
                out.append(value)
    return out


async def _birdeye_token_list(client: httpx.AsyncClient, name: str,
                              sort_by: str) -> List[Dict[str, Any]]:
    payload = await _get(
        client, name, f"{BIRDEYE_BASE}/defi/v3/token/list",
        params={"sort_by": sort_by, "sort_type": "desc",
                "min_liquidity": int(BIRDEYE_MIN_LIQUIDITY_USD),
                "offset": 0, "limit": BIRDEYE_PAGE_LIMIT},
        headers=_birdeye_headers())
    return [r for r in _birdeye_rows(payload) if isinstance(r, dict)]


async def _from_birdeye_recent(client: httpx.AsyncClient) -> List[str]:
    """The experiment's population: newly listed tokens that already carry
    real liquidity, filtered to the evaluation window.

    This replaced a ~150-line holding pen. The pen existed only because
    recency and liquidity had to be joined in memory across refreshes; asking
    one endpoint for both turns the same idea into a filter over one response.

    Measured behaviour at a $25,000 floor: ~10 qualifying listings per hour,
    and one page of 100 rows reaches back ~10 hours -- so the window sits
    inside a single call with roughly 8x headroom and paging is pointless.
    That headroom is not guaranteed at a busier hour, which is what the
    truncation warning below is for.
    """
    rows = await _birdeye_token_list(client, "birdeye-recent", "recent_listing_time")
    if not rows:
        return []

    now = time.time()
    out, ages, undated = [], [], 0
    for row in rows:
        listed = _as_float(row.get("recent_listing_time"))
        if listed is None or listed <= 0:
            # No timestamp means the age hold cannot be honoured. Skipping is
            # the conservative direction: admitting it risks evaluating a
            # token before any provider has priced it, which burns a slot for
            # nothing.
            undated += 1
            continue
        age_min = (now - listed) / 60.0
        ages.append(age_min)
        if BIRDEYE_MIN_AGE_MINUTES <= age_min <= BIRDEYE_MAX_AGE_MINUTES:
            address = row.get("address")
            if isinstance(address, str) and address:
                out.append(address)

    # The silent-truncation guard. If listings above the floor ever arrive
    # fast enough that one page no longer reaches past the window, the tail of
    # the window falls off the end of the response and this source quietly
    # returns a short list while every count still looks plausible -- the same
    # failure shape as the 429s that halved the candidate list unnoticed.
    if ages and max(ages) < BIRDEYE_MAX_AGE_MINUTES:
        logger.warning(
            f"Birdeye recency page reaches only {max(ages):.0f} min, short of the "
            f"{BIRDEYE_MAX_AGE_MINUTES:.0f} min window -- listings are arriving faster "
            f"than one page covers, so the oldest part of the window is being missed. "
            f"Raise BIRDEYE_PAGE_LIMIT or page with offset.")
    if undated:
        logger.info(f"Birdeye recency skipped {undated} row(s) with no listing time.")
    return out


async def _from_birdeye_market(client: httpx.AsyncClient) -> List[str]:
    """Breadth: the busiest tokens above the liquidity floor.

    Sorted by 24h volume rather than by liquidity. Sorting by liquidity
    returns wrapped SOL and the stablecoins -- correct, and the opposite of a
    memecoin sample.
    """
    rows = await _birdeye_token_list(client, "birdeye-market", "volume_24h_usd")
    return _mints_from(rows)


async def _from_birdeye_trending(client: httpx.AsyncClient) -> List[str]:
    payload = await _get(
        client, "birdeye-trending", f"{BIRDEYE_BASE}/defi/token_trending",
        params={"sort_by": "rank", "sort_type": "asc", "offset": 0, "limit": 20},
        headers=_birdeye_headers())
    # data.tokens[] here, not data.items[] -- see _birdeye_rows().
    return _mints_from([r for r in _birdeye_rows(payload) if isinstance(r, dict)])


# --- Jupiter sources -------------------------------------------------------

def _jupiter_created_epoch(row: Dict[str, Any]) -> Optional[float]:
    """Listing time as epoch seconds, or None if it cannot be read.

    Two shapes are accepted because Jupiter publishes both across its
    endpoints: an ISO-8601 string ("2026-07-23T19:06:57Z") and a numeric
    epoch. Neither is guessed at -- an unrecognised value returns None and
    the caller SKIPS the row, exactly as the Birdeye source skips an undated
    listing. Admitting a token whose age cannot be established risks
    evaluating it before any provider has priced it, which burns a slot for
    nothing; treating an unparseable date as "new" would do that silently.

    `firstPool.createdAt` is preferred over the token's own `createdAt` when
    present: the experiment's window is about how long the token has been
    TRADEABLE, and a mint can exist well before it has a pool.
    """
    candidates = []
    pool = row.get("firstPool")
    if isinstance(pool, dict):
        candidates.append(pool.get("createdAt"))
    candidates.append(row.get("createdAt"))

    for value in candidates:
        if isinstance(value, (int, float)) and value > 0:
            # Milliseconds if it is far beyond a plausible epoch in seconds.
            return float(value) / 1000.0 if value > 1e11 else float(value)
        if isinstance(value, str) and value.strip():
            text = value.strip().replace("Z", "+00:00")
            try:
                from datetime import datetime
                parsed = datetime.fromisoformat(text)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                from datetime import timezone
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    return None


async def _jupiter_tokens(client: httpx.AsyncClient, name: str,
                          path: str) -> List[Dict[str, Any]]:
    payload = await _get(client, name, f"{JUPITER_TOKENS_BASE}/tokens/v2/{path}")
    rows = payload if isinstance(payload, list) else _rows(payload)
    return [r for r in rows if isinstance(r, dict)]


def _jupiter_liquid(rows: List[Dict[str, Any]], name: str,
                    floor: Optional[float] = None) -> List[Dict[str, Any]]:
    """Applies the liquidity floor Birdeye used to apply server-side.

    A row whose liquidity is absent or unreadable is DROPPED, not admitted.
    The floor exists to keep the sample tradeable, and a token that cannot
    state its own liquidity is precisely the case the floor is for -- letting
    it through would reintroduce, one row at a time, the unfiltered sample
    the GeckoTerminal fallback warns about.
    """
    floor = JUPITER_MIN_LIQUIDITY_USD if floor is None else float(floor)
    kept, unpriced = [], 0
    for row in rows:
        liquidity = _as_float(row.get("liquidity"))
        if liquidity is None:
            unpriced += 1
            continue
        if liquidity >= floor:
            kept.append(row)
    if unpriced:
        logger.info(f"{name} dropped {unpriced} row(s) with no readable liquidity.")
    return kept


async def _from_jupiter_recent(client: httpx.AsyncClient,
                               floor: Optional[float] = None) -> List[str]:
    """Newly listed tokens carrying real liquidity -- the experiment's
    population, and the source Birdeye's quota took away.

    /tokens/v2/recent returns the most recent listings unfiltered, so both
    the liquidity floor and the age window are applied here. The endpoint
    returns ~30 rows, which is a much shallower page than Birdeye's 100, so
    the truncation warning below matters more rather than less: if listings
    arrive faster than 30 rows cover the window, the old end of the window
    silently falls off and every count still looks plausible.
    """
    rows = await _jupiter_tokens(client, "jupiter-recent", "recent")
    if not rows:
        return []

    now = time.time()
    out, ages, undated = [], [], 0
    floor = JUPITER_NEW_LISTING_MIN_LIQUIDITY_USD if floor is None else float(floor)
    for row in _jupiter_liquid(rows, "jupiter-recent", floor):
        created = _jupiter_created_epoch(row)
        if created is None:
            undated += 1
            continue
        age_min = (now - created) / 60.0
        ages.append(age_min)
        if JUPITER_MIN_AGE_MINUTES <= age_min <= JUPITER_MAX_AGE_MINUTES:
            mint = row.get("id")
            if isinstance(mint, str) and mint:
                out.append(mint)

    if ages and max(ages) < JUPITER_MAX_AGE_MINUTES:
        logger.warning(
            f"Jupiter recency page reaches only {max(ages):.0f} min, short of the "
            f"{JUPITER_MAX_AGE_MINUTES:.0f} min window -- listings are arriving faster "
            f"than one page covers, so the oldest part of the window is being missed.")
    if undated:
        logger.info(f"Jupiter recency skipped {undated} row(s) with no readable listing time.")
    return out


async def _from_jupiter_toptraded(client: httpx.AsyncClient,
                                  floor: Optional[float] = None) -> List[str]:
    """Breadth: the busiest tokens, above the liquidity floor.

    The counterpart to birdeye-market. Sorted by traded volume rather than by
    liquidity for the same reason: sorting by liquidity returns wrapped SOL
    and the stablecoins, which is correct and the opposite of a memecoin
    sample.
    """
    rows = await _jupiter_tokens(client, "jupiter-toptraded", "toptraded/24h")
    return _mints_from(_jupiter_liquid(rows, "jupiter-toptraded", floor), field="id")


async def _from_jupiter_organic(client: httpx.AsyncClient,
                                floor: Optional[float] = None) -> List[str]:
    """Trending, by Jupiter's organic-activity score.

    This is the one source in the stack that is ranked by something other
    than raw volume. Jupiter's organicScore is its own estimate of how much
    of a token's activity is real rather than wash traded -- the same
    question market_microstructure.check_turnover() asks from a different
    angle. Ranking by it is not the same as gating on it, and nothing here
    gates on it: it selects which tokens get evaluated, and the gates still
    decide.
    """
    rows = await _jupiter_tokens(client, "jupiter-organic", "toporganicscore/24h")
    return _mints_from(_jupiter_liquid(rows, "jupiter-organic", floor), field="id")


async def _from_rugcheck(client: httpx.AsyncClient) -> List[str]:
    data = await _get(client, "rugcheck-trending", f"{RUGCHECK_API_BASE}/v1/stats/trending")
    out = []
    for row in _rows(data):
        if isinstance(row, dict):
            mint = row.get("mint") or row.get("address") or row.get("tokenAddress")
            if mint:
                out.append(str(mint))
    return out


async def _from_dexscreener_boosts(client: httpx.AsyncClient) -> List[str]:
    """Tokens someone paid to promote. Worth knowing: an investigation of
    3,000+ boosts found boosted tokens averaged a 48% loss for traders, so
    these are a source of live CANDIDATES, not of good ones -- which is
    exactly what an experiment wants, since a sample of only promising
    tokens can't tell you whether the gates discriminate."""
    data = await _get(client, "dexscreener-boosts", f"{DEXSCREENER_BASE}/token-boosts/top/v1")
    out = []
    for row in _rows(data):
        if isinstance(row, dict) and row.get("chainId") == "solana" and row.get("tokenAddress"):
            out.append(str(row["tokenAddress"]))
    return out


async def _from_geckoterminal(client: httpx.AsyncClient) -> List[str]:
    """A second vendor's opinion on "trending". One call.

    Kept deliberately even though Birdeye has its own trending ranking. Two
    reasons: an independent vendor sees a different slice, and a fallback path
    that is never exercised is a fallback that does not work -- if
    GeckoTerminal breaks, this failing every three minutes is how we find out,
    rather than discovering it during a Birdeye outage.
    """
    data = await _get(client, "geckoterminal-trending",
                      f"{GECKOTERMINAL_BASE}/api/v2/networks/solana/trending_pools")
    out = []
    for row in _rows(data):
        try:
            tid = row["relationships"]["base_token"]["data"]["id"]
            if isinstance(tid, str) and tid.startswith("solana_"):
                out.append(tid.split("solana_", 1)[1])
        except Exception:
            continue
    return out


async def _from_geckoterminal_pools(client: httpx.AsyncClient) -> List[str]:
    """Volume-ranked pools across several pages. FALLBACK ONLY.

    This was the primary breadth source before Birdeye. Birdeye's
    v3/token/list does the same job better -- one call instead of four, and
    min_liquidity applied server-side instead of fetching ~90% of rows only to
    reject them here -- so this now runs only when Birdeye contributes
    nothing at all.

    It is not deleted because it is the only breadth source that needs no API
    key. If the Birdeye key expires, hits its quota, or the vendor has an
    outage, this keeps the experiment collecting instead of falling back to
    two trending lists.

    Four pages is four of the five GeckoTerminal calls a fallback cycle
    spends, which is roughly where 429s began when this ran every cycle. That
    is acceptable for a path that should almost never engage, and the
    per-provider throttle spaces them.
    """
    seen: List[str] = []
    known = set()
    base = f"{GECKOTERMINAL_BASE}/api/v2/networks/solana/pools"
    for page in range(1, max(1, DISCOVERY_POOL_PAGES) + 1):
        data = await _get(client, f"geckoterminal-pools p{page}", f"{base}?page={page}")
        if data is None:
            break
        rows = _rows(data)
        if not rows:
            break
        added = 0
        for row in rows:
            try:
                tid = row["relationships"]["base_token"]["data"]["id"]
            except Exception:
                continue
            if not isinstance(tid, str) or not tid.startswith("solana_"):
                continue
            addr = tid.split("solana_", 1)[1]
            if _plausible_mint(addr) and addr not in known:
                known.add(addr)
                seen.append(addr)
                added += 1
        # A page that contributes nothing new means either the end of the
        # results or that `page` is being ignored and we are re-reading page 1.
        # Both mean stop -- continuing would just burn rate limit.
        if added == 0:
            if page > 1:
                logger.info(
                    f"Pool pagination stopped at page {page}: no new tokens "
                    f"(end of results, or the endpoint ignores `page`).")
            break
        if page < DISCOVERY_POOL_PAGES:
            await asyncio.sleep(DISCOVERY_PAGE_SPACING_S)
    return seen


# Order matters: the dedup in discover_candidates() is first-source-wins, so
# whatever comes first survives truncation at DISCOVERY_MAX_CANDIDATES.
#
# Recency leads because it is the SCARCEST source -- ~10 tokens an hour, and
# the only one producing the population the experiment was designed around.
# (This is a reversal: when recency came from the holding pen it went last,
# on the grounds that promotions were plentiful and replaced within minutes.
# Birdeye's floor makes them rare instead, so the ordering follows scarcity.)
# Market breadth goes last because it is the most plentiful and the cheapest
# to lose.
# Jupiter's recency source leads Birdeye's: it is keyless, so it is the one
# that still works when Birdeye's monthly compute budget is spent -- which is
# not an edge case but the steady state of a three-minute refresh on a free
# tier.
_SOURCES: List[Any] = [
    ("jupiter-recent", _from_jupiter_recent),
    ("birdeye-recent", _from_birdeye_recent),
    ("rugcheck", _from_rugcheck),
    ("dexscreener-boosts", _from_dexscreener_boosts),
    ("jupiter-organic", _from_jupiter_organic),
    ("birdeye-trending", _from_birdeye_trending),
    ("geckoterminal-trending", _from_geckoterminal),
    ("jupiter-toptraded", _from_jupiter_toptraded),
    ("birdeye-market", _from_birdeye_market),
]

_BIRDEYE_SOURCES = {"birdeye-recent", "birdeye-trending", "birdeye-market"}

# The sources that apply a liquidity floor. The GeckoTerminal pool walk is a
# fallback for ALL of them failing together, not for Birdeye specifically --
# that distinction is the whole point of adding Jupiter, and wiring the
# fallback to Birdeye alone would have kept the unfiltered pool walk running
# permanently while a perfectly good filtered source sat beside it.
_LIQUIDITY_FILTERED_SOURCES = _BIRDEYE_SOURCES | {
    "jupiter-recent", "jupiter-organic", "jupiter-toptraded"}

# Which floor each source is held to. A source absent from this map takes no
# floor argument at all -- Birdeye applies its own server-side, and the
# keyless supplements have none to apply.
_SOURCE_FLOOR_KIND = {
    "jupiter-recent": "new_listing",
    "jupiter-organic": "general",
    "jupiter-toptraded": "general",
}
_warned_no_key = False


async def discover_candidates(client: httpx.AsyncClient,
                              force_refresh: bool = False,
                              min_liquidity_usd: Optional[float] = None,
                              new_listing_min_liquidity_usd: Optional[float] = None,
                              pen_supplier: Optional[Callable[[], Awaitable[List[str]]]] = None) -> List[str]:
    """Merged, de-duplicated candidate mint addresses. Cached for
    DISCOVERY_CACHE_TTL_S.

    Returns the previous cached list if every source fails, and an empty list
    only if we have never succeeded -- a transient outage shouldn't stall the
    pipeline when we have a perfectly usable list from a few minutes ago.
    """
    global _warned_no_key
    now = time.monotonic()

    floors = (
        JUPITER_MIN_LIQUIDITY_USD if min_liquidity_usd is None else float(min_liquidity_usd),
        (JUPITER_NEW_LISTING_MIN_LIQUIDITY_USD if new_listing_min_liquidity_usd is None
         else float(new_listing_min_liquidity_usd)),
    )
    # A floor change invalidates the cache. Without this, editing the setting
    # appears to do nothing for up to DISCOVERY_CACHE_TTL_S -- which reads as
    # a broken setting, and is the kind of thing someone "fixes" by changing
    # it again.
    if _cache["floors"] is not None and _cache["floors"] != floors:
        logger.info(
            f"Discovery liquidity floors changed from ${_cache['floors'][0]:,.0f}/"
            f"${_cache['floors'][1]:,.0f} to ${floors[0]:,.0f}/${floors[1]:,.0f} "
            f"(general/new-listing) -- refreshing now. The population being sampled "
            f"has changed, so results either side of this point are not one sample.")
        force_refresh = True

    # NOTE the absence of an `and _cache["candidates"]` term. Requiring a
    # non-empty cache to honour the TTL made the backoff below useless in the
    # one case it exists for: a COLD START during a provider outage. With no
    # cached list, the guard was falsy however recently fetched_at had been
    # stamped, so every tick fell through to the destructive refresh -- which
    # drains the holding pen -- three seconds apart, with nothing to fall
    # back on. "Am I allowed to refresh yet" is a question about the clock,
    # not about whether the last answer happened to be non-empty.
    # `is not None`, not a truthiness test: the failure backoff below stamps
    # fetched_at with now - TTL + RETRY, which is legitimately NEGATIVE early
    # in a process's life, and a falsy check would read that as never-fetched
    # and defeat the backoff it exists to enforce.
    if (not force_refresh and _cache["fetched_at"] is not None
            and (now - _cache["fetched_at"]) < DISCOVERY_CACHE_TTL_S):
        return _cache["candidates"]

    have_key = bool(BIRDEYE_API_KEY)
    if not have_key and not _warned_no_key:
        # Warn once, not every three minutes: without a key the Birdeye
        # sources contribute nothing and the GeckoTerminal fallback carries
        # the load, which is a degraded but working configuration.
        logger.info(
            "BIRDEYE_API_KEY is not set -- Birdeye's three sources are skipped. This "
            "is no longer a degraded configuration: Jupiter supplies the same three "
            "jobs (recency, trending, breadth) keylessly and applies the same "
            "liquidity floor. To enable Birdeye anyway, set the key in the "
            "project-root .env AND list it in docker-compose.yml's web environment "
            "block; compose forwards only what it names.")
        _warned_no_key = True

    merged: List[str] = []
    per_source: Dict[str, int] = {}
    filtered_total = 0

    # The holding pen leads, because it is the only source that can supply the
    # evaluation window at all -- every newest-first endpoint spans one or two
    # minutes on Solana. It is INJECTED rather than fetched here so this module
    # stays free of the database: it is the one place that ingests
    # attacker-chosen identifiers, and it should not need the rest of the
    # project loaded to do so safely. See discovery_pen.py.
    # A SUPPLIER, not a list. Draining the pen is destructive -- examining a
    # token marks it, once, forever -- so it must happen only on a refresh
    # that actually uses the result. Passing a pre-drained list meant the
    # caller emptied the pen on every pipeline tick while this function served
    # a three-minute cache, so all but one batch in forty-five was marked
    # examined and then discarded.
    if pen_supplier is not None:
        try:
            supplied = await pen_supplier() or []
        except Exception as e:
            logger.warning(f"Pen supplier raised: {type(e).__name__}: {e}")
            supplied = []
        pen = [m for m in supplied if _plausible_mint(m)]
        per_source["holding-pen"] = len(pen)
        filtered_total += len(pen)
        merged.extend(pen)
    for name, fetch in _SOURCES:
        if name in _BIRDEYE_SOURCES and not have_key:
            continue
        try:
            kind = _SOURCE_FLOOR_KIND.get(name)
            if kind is None:
                found = await fetch(client)
            else:
                found = await fetch(client, floors[1] if kind == "new_listing" else floors[0])
        except Exception as e:
            logger.warning(f"Discovery source {name} raised unexpectedly: {e}")
            found = []
        per_source[name] = len(found)
        if name in _LIQUIDITY_FILTERED_SOURCES:
            filtered_total += len(found)
        merged.extend(found)

    # Fallback, not a routine source. Engaging it means Birdeye gave us
    # nothing at all -- a missing key, an exhausted quota, or a vendor outage
    # -- and it is worth a WARNING rather than a silent substitution, because
    # the population being sampled changes when it happens.
    if filtered_total == 0:
        logger.warning(
            "No liquidity-filtered source produced candidates (pen, Jupiter and Birdeye "
            "both empty); falling back to the GeckoTerminal pool walk. The sample is "
            "no longer liquidity-filtered while this is happening, so the population "
            "being measured has changed -- check the source counts below before "
            "reading any result collected during this period.")
        try:
            fallback = await _from_geckoterminal_pools(client)
        except Exception as e:
            logger.warning(f"Fallback pool walk raised unexpectedly: {e}")
            fallback = []
        per_source["geckoterminal-pools(fallback)"] = len(fallback)
        merged.extend(fallback)

    # Preserve order (first source wins) while de-duplicating, so the list is
    # stable enough to reason about between refreshes.
    seen = set()
    candidates = []
    rejected = 0
    for addr in merged:
        if not _plausible_mint(addr):
            rejected += 1
            continue
        if addr not in seen:
            seen.add(addr)
            candidates.append(addr)
    if rejected:
        # Not fatal, but worth surfacing: these are attacker-chosen strings
        # arriving from third-party feeds, and a malformed one that reached
        # the pipeline would waste a price lookup every tick forever.
        logger.warning(f"Discovery dropped {rejected} candidate(s) that are not "
                       f"plausible Solana mint addresses.")
    candidates = candidates[:DISCOVERY_MAX_CANDIDATES]

    if not candidates:
        # STAMP THE CLOCK EVEN ON FAILURE. Returning early without touching
        # fetched_at left the TTL check permanently satisfied, so the next
        # tick refreshed again -- three seconds later, and every three
        # seconds after that.
        #
        # That matters because refreshing is DESTRUCTIVE: pen_supplier drains
        # the holding pen, and examining a token stamps released_at on it
        # forever. During an upstream outage every source returns empty, this
        # branch is taken, and the drain runs 20x a minute at 120 rows a
        # time. A pen holding a few thousand rows is entirely consumed in
        # under a minute -- every row flagged "examined and failed" without a
        # single liquidity measurement ever being taken -- and the only log
        # line is the reassuring one below.
        _cache["fetched_at"] = now - DISCOVERY_CACHE_TTL_S + DISCOVERY_FAILURE_RETRY_S
        # Record the floors this attempt ran under even though it yielded
        # nothing. The floor-change check above is what lets a settings edit
        # take effect before the TTL expires, and it compares against this
        # value -- leaving it unset after a failed sweep meant a floor lowered
        # during an outage was silently ignored until the backoff lapsed.
        _cache["floors"] = floors
        if _cache["candidates"]:
            logger.warning(
                f"All discovery sources failed; reusing the previous candidate list "
                f"and not retrying for {DISCOVERY_FAILURE_RETRY_S:.0f}s.")
            return _cache["candidates"]
        logger.error(
            f"All discovery sources failed and no cached candidates exist; "
            f"not retrying for {DISCOVERY_FAILURE_RETRY_S:.0f}s.")
        return []

    _cache["candidates"] = candidates
    _cache["fetched_at"] = now
    _cache["floors"] = floors
    # The per-source breakdown matters more than the total. The total is
    # pinned near the sum of the sources' fixed page sizes and barely moves
    # even when every member has changed, so it cannot tell you whether the
    # pool is turning over -- or which source just stopped answering. A whole
    # source silently dropping to zero is the failure this project has hit
    # three times.
    detail = ", ".join(f"{k}={v}" for k, v in per_source.items())
    logger.info(
        f"Token discovery refreshed: {len(candidates)} candidates ({detail}) "
        f"[floors: general ${floors[0]:,.0f}, new-listing ${floors[1]:,.0f}].")
    return candidates
