# gmgn_market_data.py
"""Alternative market-data provider backed by GMGN's OpenAPI.

Selected by setting MARKET_DATA_PROVIDER=gmgn (default is "dexscreener",
which uses market_data.py's original DexScreener + Solana-RPC path). Both
providers implement the same fetch_full_snapshot() contract and return
dicts with identical keys, so the pipeline doesn't know or care which one
is in use -- see market_data.get_snapshot() for the dispatch.

WHY THIS EXISTS
GMGN indexes the same Solana tokens we were reading from DexScreener, but
returns price, liquidity, volume AND top-10 holder concentration from a
single request, where our DexScreener path needed a separate Solana RPC
call for holder data against an endpoint Solana's own docs say is not for
production. It also exposes a real attention metric (search heat), which
was the closest thing to a hype signal we found -- though the gate that
would have consumed it (E_SIGNAL) has since been replaced by E_BREADTH,
which uses on-chain participation data instead.

READ-ONLY, BY DELIBERATE CHOICE
This module calls ONLY GMGN's unsigned read endpoints, which need nothing
but an API key: no request-signing key, no wallet binding, no deposit, no
custody relationship of any kind. GMGN's trading endpoints are custodial
-- GMGN generates and holds the wallet key, it cannot be exported, and
there are no spending caps or token allowlists at their API layer -- which
is why execution stays on our own Turnkey-based signer (see
STAGE3_SETUP.md) and why nothing in this file touches /v1/trade/*. Keep it
that way: adding one trading call here would quietly convert a read-only
integration into a custodial one.

We also keep using Jupiter for the slippage estimate rather than GMGN's
/v1/trade/quote, even though that endpoint is unsigned -- staying entirely
out of the /v1/trade/ namespace makes the read-only boundary something you
can verify by grepping this file rather than by reading docs.

VERIFICATION STATUS
Endpoint paths, auth mechanics, response envelope, field names and units
below were taken from GMGN's own client source (github.com/GMGNAI/
gmgn-skills, src/client/OpenApiClient.ts and the skills/*/SKILL.md field
tables), not from guesswork. But this sandbox has no network access to
GMGN, so none of it has been executed against the live API -- it is
mock-tested only, exactly like market_data.py was at Stage 1. Run the
smoke test in README.md's "Switching to the GMGN provider" section before
trusting it, and expect field-level surprises: their own docs contradict
each other in several places, so this module sticks to the fields their
measured/dated documentation confirms and treats everything else as
unavailable rather than guessing.
"""
import os
import time
import uuid
import asyncio
import logging
from typing import Optional, Dict, Any, List

import httpx

import holder_concentration

from market_data import (
    sanitize_external_text,
    onchain_flow_velocity_proxy,
    fetch_price_impact_pct,
)

logger = logging.getLogger("gmgn_market_data")

GMGN_API_BASE = os.environ.get("GMGN_API_BASE", "https://openapi.gmgn.ai")
GMGN_API_KEY = os.environ.get("GMGN_API_KEY", "")
GMGN_CHAIN = "sol"

# GMGN's API sits behind Cloudflare, which rejects requests carrying a
# default HTTP-library User-Agent ("python-httpx/x.y.z") with a 403 and an
# HTML block page -- before the request ever reaches GMGN, so you get no
# JSON error code to diagnose from. Their own CLI sends "gmgn-cli/<version>".
# We send an honest identifier for this application instead of pretending
# to be their tool; override it if their edge rules ever reject this one.
GMGN_USER_AGENT = os.environ.get("GMGN_USER_AGENT", "memecoin-trading-agent/1.0")

# How long a fetched hot-searches ranking stays usable. That endpoint
# returns a global top-N list rather than a per-token lookup, so we fetch
# it once and answer many token evaluations out of it. 300s keeps us far
# under the rate limit while staying fresh enough for a metric whose
# shortest published interval is 1 minute.
HOT_SEARCH_CACHE_TTL_S = float(os.environ.get("GMGN_HOT_SEARCH_CACHE_TTL_S", "300"))
HOT_SEARCH_INTERVAL = os.environ.get("GMGN_HOT_SEARCH_INTERVAL", "1h")
HOT_SEARCH_LIMIT = int(os.environ.get("GMGN_HOT_SEARCH_LIMIT", "500"))

_hot_search_cache: Dict[str, Any] = {"fetched_at": 0.0, "ranking": None}


class GmgnConfigError(Exception):
    """Raised when MARKET_DATA_PROVIDER=gmgn but no API key is set."""


def _auth_query() -> Dict[str, Any]:
    """Every GMGN request carries a fresh timestamp and client_id as query
    params, alongside the X-APIKEY header. Their server rejects a
    timestamp more than ~5s from its own clock (AUTH_TIMESTAMP_EXPIRED)
    and rejects a client_id replayed within ~7s (AUTH_CLIENT_ID_REPLAYED),
    so the UUID must be regenerated per request -- never cached."""
    return {"timestamp": int(time.time()), "client_id": str(uuid.uuid4())}


def _headers() -> Dict[str, str]:
    if not GMGN_API_KEY:
        raise GmgnConfigError(
            "MARKET_DATA_PROVIDER=gmgn but GMGN_API_KEY is not set. Get a read-only "
            "key from https://gmgn.ai/ai -- see README.md. (Do NOT configure a "
            "request-signing key or bind a wallet; this integration is read-only.)"
        )
    return {
        "X-APIKEY": GMGN_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Required in practice, not by GMGN's API but by the Cloudflare edge
        # in front of it -- see GMGN_USER_AGENT above. Without it you get a
        # 403 HTML block page instead of any JSON error you could act on.
        "User-Agent": GMGN_USER_AGENT,
    }


def _unwrap(payload: Any, context: str) -> Optional[Any]:
    """GMGN wraps every response, success or failure, in
    {code, data, message, error}. Success is code == 0 -- and crucially
    the HTTP status is NOT the signal: a non-zero code can arrive with a
    200. Returns the inner `data`, or None (logged) on any error code."""
    if not isinstance(payload, dict):
        logger.warning(f"GMGN {context}: unexpected non-object response: {type(payload)}")
        return None
    code = payload.get("code")
    if code != 0:
        err = payload.get("error") or ""
        msg = payload.get("message") or ""
        if err.startswith("RATE_LIMIT"):
            # reset_at is documented in the body but their own client reads
            # the X-RateLimit-Reset header instead, so callers should check
            # both. We don't auto-retry here -- GMGN's docs warn that
            # retrying at or before the reset instant extends the ban.
            logger.warning(f"GMGN {context}: rate limited ({err}). Backing off; do not retry immediately.")
        else:
            logger.warning(f"GMGN {context}: API error code={code} error={err!r} message={msg!r}")
        return None
    return payload.get("data")


def _as_float(value: Any) -> Optional[float]:
    """GMGN returns most numerics as strings, and uses "" / None to mean
    "not measured" -- which is NOT the same as zero and must never be
    coerced to it (a 0% top-10 holder concentration does not exist; an
    empty string means they didn't measure it). Returns None for anything
    unparseable so callers can distinguish missing from genuinely zero."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def fetch_token_info(client: httpx.AsyncClient, token_address: str) -> Optional[Dict[str, Any]]:
    """GET /v1/token/info -- price, liquidity, volumes, holder count, and
    the `stat` block carrying holder-concentration and insider metrics.
    Rate-limit weight 1.

    Returns None if GMGN has no record of this token, which their API
    signals by returning a fully-shaped object with an EMPTY `symbol`
    rather than by returning an error -- so the emptiness check below is
    load-bearing, not defensive padding.
    """
    url = f"{GMGN_API_BASE}/v1/token/info"
    params = {"chain": GMGN_CHAIN, "address": token_address, **_auth_query()}
    try:
        resp = await client.get(url, params=params, headers=_headers(), timeout=10.0)
        resp.raise_for_status()
        data = _unwrap(resp.json(), "token/info")
    except GmgnConfigError:
        raise
    except Exception as e:
        logger.warning(f"GMGN token/info request failed for {token_address}: {e}")
        return None

    if not isinstance(data, dict):
        return None

    raw_symbol = data.get("symbol") or ""
    if not str(raw_symbol).strip():
        logger.warning(f"GMGN has no record for {token_address} (empty symbol) -- skipping.")
        return None

    symbol = sanitize_external_text(raw_symbol, fallback=token_address[:6])
    if symbol != str(raw_symbol).strip():
        logger.warning(
            f"Token {token_address} has a symbol containing control/format characters; "
            f"sanitized to {symbol!r} before use."
        )

    price_block = data.get("price") or {}
    stat_block = data.get("stat") or {}

    # info.liquidity can read "0" while pool.liquidity carries the real
    # figure (GMGN's own field docs flag this) -- take whichever is
    # non-zero rather than trusting either alone.
    pool_block = data.get("pool") or {}
    liquidity = _as_float(data.get("liquidity")) or 0.0
    pool_liquidity = _as_float(pool_block.get("liquidity")) or 0.0
    liquidity_usd = liquidity if liquidity > 0 else pool_liquidity

    # Same pattern for holder concentration: security.top_10_holder_rate
    # can be "0" while stat.top_10_holder_rate has the real value. These
    # are 0-1 FRACTIONS despite the "rate" naming, so scale to a percent
    # to match the contract the pipeline's F_ATLAS gate expects.
    top10_rate = _as_float(stat_block.get("top_10_holder_rate"))
    top10_pct = round(top10_rate * 100.0, 2) if top10_rate and top10_rate > 0 else None

    return {
        "token_symbol": symbol,
        "price_usd": _as_float(price_block.get("price")) or 0.0,
        "liquidity_usd": liquidity_usd,
        "volume_h1": _as_float(price_block.get("volume_1h")) or 0.0,
        "volume_h24": _as_float(price_block.get("volume_24h")) or 0.0,
        "top_10_holder_percentage": top10_pct,
        "holder_count": data.get("holder_count"),
    }


async def _fetch_hot_search_ranking(client: httpx.AsyncClient) -> Optional[Dict[str, int]]:
    """POST /v1/market/hot_searches -- a global ranking of tokens by
    search/visit heat. Rate-limit weight 3.

    There is NO per-token heat lookup in GMGN's API, so we pull the whole
    ranked list and index it by address. Result is cached for
    HOT_SEARCH_CACHE_TTL_S and shared across every token evaluated in that
    window, which is what keeps this affordable.

    We pass an explicit empty `filters` list on purpose: omitting it is
    NOT "no filter" -- GMGN applies per-chain defaults, and the Solana
    defaults silently drop tokens whose mint/freeze authority isn't
    renounced. Those are exactly the risky tokens whose hype we'd most
    want to see, so we opt out of the implicit filtering.

    Returns {address: rank}, or None if the request failed.
    """
    url = f"{GMGN_API_BASE}/v1/market/hot_searches"
    body = {"params": [{
        "label": "hot-search",
        "chain": GMGN_CHAIN,
        "interval": HOT_SEARCH_INTERVAL,
        "limit": HOT_SEARCH_LIMIT,
        "filters": [],
    }]}
    try:
        resp = await client.post(url, params=_auth_query(), json=body,
                                   headers=_headers(), timeout=15.0)
        resp.raise_for_status()
        data = _unwrap(resp.json(), "market/hot_searches")
    except GmgnConfigError:
        raise
    except Exception as e:
        logger.warning(f"GMGN hot_searches request failed: {e}")
        return None

    blocks: List[Any] = data if isinstance(data, list) else []
    ranking: Dict[str, int] = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for entry in (block.get("tokens") or []):
            if not isinstance(entry, dict):
                continue
            address = entry.get("address")
            rank = entry.get("rank")
            if address and isinstance(rank, int):
                ranking[address] = rank
    return ranking or None


async def fetch_social_volume_score(client: httpx.AsyncClient, token_address: str) -> Optional[float]:
    """Attention score on the same 0-100 scale the pipeline's other
    signals use, derived from where this token sits in GMGN's search-heat
    ranking.

    We score by RANK rather than by raw visiting_count deliberately: the
    raw count has no fixed ceiling, so it can't be normalized to 0-100
    without inventing a scaling constant that would silently go stale as
    GMGN's traffic changes. Rank position is self-normalizing -- #1 of 500
    scores 100, the bottom of the list scores near 0.

    Returns None when the token isn't in the ranking at all. That is
    genuinely "unknown", NOT zero heat: the list is capped at the top
    HOT_SEARCH_LIMIT tokens chain-wide, so almost every token is absent
    almost all the time. Callers must not read absence as a real
    measurement of low interest.

    IMPORTANT HONESTY NOTE: this measures searches on GMGN's own platform.
    It is a real signal from real users and it is a large improvement on
    the fixed placeholder it replaces, but it is not off-platform social
    volume -- there's no Twitter/X post count or sentiment in here. Read
    it accordingly -- though no gate consumes it today.
    """
    now = time.monotonic()
    cached = _hot_search_cache.get("ranking")
    if cached is None or (now - _hot_search_cache["fetched_at"]) > HOT_SEARCH_CACHE_TTL_S:
        ranking = await _fetch_hot_search_ranking(client)
        if ranking is not None:
            _hot_search_cache["ranking"] = ranking
            _hot_search_cache["fetched_at"] = now
            cached = ranking
        elif cached is None:
            return None  # never successfully fetched; nothing to score against

    rank = cached.get(token_address)
    if rank is None:
        return None

    # Normalize against the size of the RANKED UNIVERSE, not the number of
    # entries we happened to parse. `rank` is a 1-based position within
    # GMGN's ranking (up to HOT_SEARCH_LIMIT), and the two are not the same
    # number -- a response can carry ranks up to 500 while we parse fewer
    # usable rows, and dividing by the row count would push almost every
    # token to a clamped 0.0 and make real hype look like no hype.
    universe = max(max(cached.values()), len(cached), 1)
    score = (1.0 - ((rank - 1) / universe)) * 100.0
    return round(max(0.0, min(score, 100.0)), 2)


async def fetch_full_snapshot(client: httpx.AsyncClient, token_address: str) -> Optional[Dict[str, Any]]:
    """Same contract as market_data.fetch_full_snapshot(): identical keys,
    same "return None means skip this token rather than invent numbers"
    rule. See that function for what each key feeds.

    Slippage still comes from Jupiter (see module docstring on why we stay
    out of GMGN's /v1/trade/ namespace entirely).
    """
    info = await fetch_token_info(client, token_address)
    if info is None:
        return None

    social_score, price_impact, chain = await asyncio.gather(
        fetch_social_volume_score(client, token_address),
        fetch_price_impact_pct(client, token_address),
        holder_concentration.observe_chain(client, token_address),
    )
    holder_fields = holder_concentration.snapshot_fields(
        info["top_10_holder_percentage"], chain)

    return {
        "token_symbol": info["token_symbol"],
        "token_address": token_address,
        "current_price": info["price_usd"],
        "pool_liquidity_usd": info["liquidity_usd"],
        "social_volume_score": social_score if social_score is not None else 0.0,
        "onchain_flow_velocity": onchain_flow_velocity_proxy(info["volume_h1"], info["liquidity_usd"]),
        "estimated_slippage_percent": price_impact if price_impact is not None else 0.0,
        "onchain_volume_increasing": info["volume_h1"] * 24.0 > info["volume_h24"],
        # GMGN reports pool TVL like DexScreener, so the tradeable side is
        # about half -- see free_market_data.compute_tradeable_depth().
        "tradeable_depth_usd": round(info["liquidity_usd"] / 2.0, 2) if info["liquidity_usd"] else 0.0,
        # GMGN's token/info has no transaction-count field we confirmed,
        # so the breadth inputs are unavailable from this provider.
        "volume_h1_usd": info["volume_h1"],
        "txns_h1_buys": None,
        "txns_h1_sells": None,
        "total_holders": info.get("holder_count"),
        "rug_score": None,
        "rugged": None,
        "mint_authority_renounced": None,
        "freeze_authority_renounced": None,
        "token_age_hours": None,
        "launchpad": None,
        **holder_fields,
        "_slippage_data_missing": price_impact is None,
        "_social_data_missing": social_score is None,
        "_depth_data_missing": not info["liquidity_usd"],
        "_price_disagreement": False,
    }
