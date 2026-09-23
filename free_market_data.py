# free_market_data.py
"""Composite market-data provider built entirely from free, keyless APIs.

Selected with MARKET_DATA_PROVIDER=free. No API key, no signup, no
custody, no vendor account of any kind.

WHY THIS IS THE DEFAULT-WORTHY OPTION
Field discovery against a live pump.fun token confirmed the free sources
between them cover every input the pipeline needs except hype:

  DexScreener  symbol, price, pool TVL, 1h/24h volume
  RugCheck     top-10 holder concentration, rug score, mint/freeze
               authority status, holder count, insider networks
  Jupiter      independent price, one-sided liquidity, token age,
               launchpad, and the slippage/price-impact quote

RugCheck in particular replaces the Solana RPC holder lookup the
DexScreener provider used -- an endpoint Solana's own docs say is not for
production -- and adds rug signals this project never had.

THE LIQUIDITY DEFINITION BUG THIS FIXES
Probing one token across four providers produced two clusters:
DexScreener $79,276 and RugCheck $78,217 against Jupiter $38,521 and
GeckoTerminal $42,446. That 2:1 split is definitional, not noise --
DexScreener and RugCheck report total value locked (BOTH sides of the
pool summed), while Jupiter reports roughly the tradeable side.

That matters because position sizing and the liquidity floor were both
reading TVL as if it were depth, so a position sized at "1% of liquidity"
was really taking about 2% of the side it actually trades against, and
would eat correspondingly more slippage than the gate believed. This
module therefore reports BOTH numbers and keeps them straight:

  pool_liquidity_usd    TVL. Unchanged meaning, kept for display and for
                        continuity with historical rows.
  tradeable_depth_usd   A deliberately conservative one-sided estimate --
                        the minimum of (TVL / 2) and any directly-reported
                        one-sided figure. This is what sizing should use.

Erring low here is intentional: undersizing costs opportunity, oversizing
costs real money to slippage.
"""
import os
import time
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import httpx

import holder_concentration

from market_data import (
    sanitize_external_text,
    onchain_flow_velocity_proxy,
    fetch_price_impact_pct,
    fetch_dex_pair_data,
    jupiter_throttle,
    JUPITER_CACHE_TTL_S,
)

logger = logging.getLogger("free_market_data")

RUGCHECK_API_BASE = os.environ.get("RUGCHECK_API_BASE", "https://api.rugcheck.xyz")
JUPITER_PRICE_BASE = os.environ.get("JUPITER_PRICE_BASE", "https://api.jup.ag")

# Warn when two independent price sources disagree by more than this. Thin
# memecoin pools genuinely diverge, so this is a staleness/oddity signal to
# log, not a reason to reject a token.
PRICE_DISAGREEMENT_WARN_PCT = float(os.environ.get("PRICE_DISAGREEMENT_WARN_PCT", "10"))

# An honest User-Agent. None of these providers has bot-blocked a plain
# Python client (verified with test_provider_access.py), unlike GMGN --
# but identifying ourselves is correct practice regardless.
HEADERS = {"User-Agent": "memecoin-trading-agent/1.0", "Accept": "application/json"}

# Jupiter's free tier is ~1 req/sec and the live run hit 429s. Token age,
# launchpad and the cross-check price change slowly, so cache per token and
# share market_data's process-wide Jupiter throttle -- both calls to Jupiter
# (this one and the slippage quote) queue through the same gate.
_jupiter_cache: Dict[str, Any] = {}


def _as_float(value: Any) -> Optional[float]:
    """Parse defensively. These APIs return numbers as strings, sometimes
    with absurd precision -- GeckoTerminal once returned a 90-digit
    liquidity value -- and use "" / null for "not measured", which is NOT
    the same as zero and must never be coerced into it."""
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # Guard against inf/nan sneaking into a gate comparison.
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


async def fetch_rugcheck_report(client: httpx.AsyncClient, token_address: str) -> Optional[Dict[str, Any]]:
    """Holder concentration and rug/security signals. Free, no key.

    Returns None when RugCheck has no meaningful record of the token --
    which it signals not with an error but with a fully-shaped report full
    of zeros (price 0, no holders, no liquidity). Treating that stub as
    real data would report 0% holder concentration for an unknown token,
    which the F_ATLAS gate would read as "perfectly distributed, safe".
    """
    url = f"{RUGCHECK_API_BASE}/v1/tokens/{token_address}/report"
    try:
        resp = await client.get(url, headers=HEADERS, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"RugCheck lookup failed for {token_address}: {e}")
        return None

    if not isinstance(data, dict):
        return None

    holders: List[Dict[str, Any]] = data.get("topHolders") or []
    total_holders = _as_float(data.get("totalHolders")) or 0.0
    price = _as_float(data.get("price")) or 0.0

    if not holders and total_holders <= 0 and price <= 0:
        logger.warning(f"RugCheck has no meaningful record for {token_address} -- ignoring its report.")
        return None

    # Every one of the top-10 entries must carry a usable `pct`, or this is
    # UNMEASURED -- not a smaller number.
    #
    # The previous form was `sum(_as_float(h.get("pct")) or 0.0 for h in ...)`,
    # which turned a missing percentage into a zero contribution. A report
    # whose topHolders array is populated but whose `pct` fields are absent
    # therefore produced top10_pct = 0.0 rather than None, so
    # _holder_data_missing was False, so F_ATLAS took its MEASURED branch and
    # evaluated `0.0 > 30.0` -- passing a token where one wallet may hold 82%
    # of supply as perfectly distributed. That is verbatim the outcome
    # node_F_ATLAS's own comment says must never happen; the fail-closed guard
    # was added one layer downstream and this line kept feeding it a number.
    #
    # A PARTIAL sum is refused for the same reason: three of ten holders
    # reporting 14% between them is not "14% concentration", it is an unknown
    # concentration of at least 14%, and it understates in the one direction
    # that matters.
    top10_pct = None
    if holders:
        parsed = [_as_float(h.get("pct")) for h in holders[:10]]
        if parsed and all(v is not None for v in parsed):
            top10_pct = round(sum(parsed), 2)
        else:
            logger.warning(
                f"RugCheck returned {len(parsed)} top holders for {token_address} but only "
                f"{sum(1 for v in parsed if v is not None)} carry a usable percentage -- "
                f"reporting concentration as unmeasured so F_ATLAS refuses rather than "
                f"treating the token as well distributed.")

    # These two are the most important safety fields on a Solana token, and
    # null is MEANINGFUL: it means the authority was renounced, which is the
    # safe state. Absence of the key means RugCheck didn't report it, which
    # is unknown. Those must not collapse into the same value.
    def authority_renounced(key: str) -> Optional[bool]:
        if key not in data:
            return None
        return data.get(key) in (None, "")

    return {
        "top_10_holder_percentage": top10_pct,
        "total_holders": int(total_holders) if total_holders else None,
        "rug_score": _as_float(data.get("score_normalised")),
        "rugged": data.get("rugged") if isinstance(data.get("rugged"), bool) else None,
        "mint_authority_renounced": authority_renounced("mintAuthority"),
        "freeze_authority_renounced": authority_renounced("freezeAuthority"),
        "lp_locked_pct": _as_float(data.get("lpLockedPct")),
        "rugcheck_liquidity_usd": _as_float(data.get("totalMarketLiquidity")),
        "risks": [r.get("name") for r in (data.get("risks") or []) if isinstance(r, dict)],
    }


async def fetch_jupiter_token_data(client: httpx.AsyncClient, token_address: str) -> Optional[Dict[str, Any]]:
    """Independent price, one-sided liquidity, token age and launchpad.
    Free, no key, roughly 1 request/second."""
    cached = _jupiter_cache.get(token_address)
    if cached and (time.time() - cached["at"]) < JUPITER_CACHE_TTL_S:
        return cached["value"]

    url = f"{JUPITER_PRICE_BASE}/price/v3"
    await jupiter_throttle()
    try:
        resp = await client.get(url, params={"ids": token_address}, headers=HEADERS, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Jupiter price lookup failed for {token_address}: {e}")
        return None

    row = (data or {}).get(token_address)
    if not isinstance(row, dict):
        return None

    age_hours = None
    created = row.get("createdAt")
    if created:
        try:
            created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            age_hours = round((datetime.now(timezone.utc) - created_dt).total_seconds() / 3600.0, 2)
        except Exception:
            logger.warning(f"Could not parse Jupiter createdAt {created!r} for {token_address}.")

    result = {
        "price_usd": _as_float(row.get("usdPrice")),
        "one_sided_liquidity_usd": _as_float(row.get("liquidity")),
        "token_age_hours": age_hours,
        "launchpad": sanitize_external_text(row.get("launchpad"), fallback="") or None,
    }
    _jupiter_cache[token_address] = {"value": result, "at": time.time()}
    return result


def compute_tradeable_depth(tvl_usd: Optional[float],
                              one_sided_usd: Optional[float]) -> Optional[float]:
    """The conservative one-sided depth a trade actually executes against.

    A balanced AMM pool's total value locked is roughly twice the value of
    either side, so TVL/2 approximates the side we trade into. When a
    provider reports a one-sided figure directly we take the lower of the
    two, because being wrong in the low direction only costs opportunity
    while being wrong high costs slippage on real money.
    """
    candidates = []
    if tvl_usd is not None and tvl_usd > 0:
        candidates.append(tvl_usd / 2.0)
    if one_sided_usd is not None and one_sided_usd > 0:
        candidates.append(one_sided_usd)
    if not candidates:
        return None
    return round(min(candidates), 2)


async def fetch_full_snapshot(client: httpx.AsyncClient, token_address: str) -> Optional[Dict[str, Any]]:
    """Same contract as market_data.fetch_full_snapshot(), plus the extra
    fields the free stack makes available. Returns None if DexScreener has
    no indexed pair, since without a tradeable pool there is nothing to
    evaluate -- the other sources can enrich a token but can't substitute
    for it having a market.
    """
    dex_data = await fetch_dex_pair_data(client, token_address)
    if dex_data is None:
        return None

    rug, jup, price_impact, chain = await asyncio.gather(
        fetch_rugcheck_report(client, token_address),
        fetch_jupiter_token_data(client, token_address),
        fetch_price_impact_pct(client, token_address),
        holder_concentration.observe_chain(client, token_address),
    )
    rug = rug or {}
    jup = jup or {}

    tvl_usd = dex_data["liquidity_usd"]
    tradeable_depth = compute_tradeable_depth(tvl_usd, jup.get("one_sided_liquidity_usd"))

    # Cross-check the two independent prices. Disagreement doesn't reject
    # the token -- thin pools legitimately diverge -- but it's worth seeing
    # in the log, because it usually means one source is stale or quoting a
    # different pool than the one we'd actually trade.
    dex_price = dex_data["price_usd"]
    jup_price = jup.get("price_usd")
    price_disagreement = False
    if dex_price and jup_price and dex_price > 0:
        delta_pct = abs(jup_price - dex_price) / dex_price * 100.0
        if delta_pct > PRICE_DISAGREEMENT_WARN_PCT:
            price_disagreement = True
            logger.warning(
                f"Price disagreement on {dex_data['token_symbol']}: DexScreener ${dex_price} "
                f"vs Jupiter ${jup_price} ({delta_pct:.1f}% apart). Using DexScreener's, since "
                f"that's the pool the slippage estimate is quoted against."
            )

    # RugCheck's figure is this provider's reading. The chain measurements
    # ride alongside it, unused by any gate, so the wallet-vs-RugCheck gap
    # accumulates on the real token population -- that comparison is what
    # decides whether the 30% ceiling can be reused under a chain
    # definition, and it cannot be answered from RugCheck alone because the
    # tokens RugCheck has no record of are precisely the ones in question.
    holder_pct = rug.get("top_10_holder_percentage")
    holder_fields = holder_concentration.snapshot_fields(holder_pct, chain)

    return {
        # --- the original contract, unchanged ---
        "token_symbol": dex_data["token_symbol"],
        "token_address": token_address,
        "current_price": dex_price,
        "pool_liquidity_usd": tvl_usd,
        "social_volume_score": 0.0,   # still unsolved by any free source
        "onchain_flow_velocity": onchain_flow_velocity_proxy(dex_data["volume_h1"], tvl_usd),
        "estimated_slippage_percent": price_impact if price_impact is not None else 0.0,
        "onchain_volume_increasing": dex_data["volume_h1"] * 24.0 > dex_data["volume_h24"],

        # --- new: honest depth, used for sizing (see module docstring) ---
        "tradeable_depth_usd": tradeable_depth if tradeable_depth is not None else 0.0,

        # --- breadth inputs for E_BREADTH (see engine.py) ---
        # volume_h1_usd is the capital side; the txn counts are the
        # participation side. Keeping both raw lets the gate compute
        # capital-per-participant rather than guessing from volume alone.
        "volume_h1_usd": dex_data["volume_h1"],
        "txns_h1_buys": dex_data["txns_h1_buys"],
        "txns_h1_sells": dex_data["txns_h1_sells"],

        # --- short-window features: RECORDED, NOT GATED ON ---
        # m5 is DexScreener's finest bucket. These ride along in the same
        # response the price came from, so they cost nothing extra, and
        # they exist so the experiment can test whether short-window
        # activity predicts the horizon returns -- not so a gate can filter
        # on them today. See paper_trading.feature_correlations().
        "volume_m5_usd": dex_data.get("volume_m5"),
        "txns_m5_buys": dex_data.get("txns_m5_buys"),
        "txns_m5_sells": dex_data.get("txns_m5_sells"),
        "price_change_m5": dex_data.get("price_change_m5"),
        "price_change_h1": dex_data.get("price_change_h1"),

        "total_holders": rug.get("total_holders"),

        # --- new: rug/security signals, free, never available before ---
        "rug_score": rug.get("rug_score"),
        "rugged": rug.get("rugged"),
        "mint_authority_renounced": rug.get("mint_authority_renounced"),
        "freeze_authority_renounced": rug.get("freeze_authority_renounced"),
        "token_age_hours": jup.get("token_age_hours"),
        "launchpad": jup.get("launchpad"),

        # --- flags: main.py pops these before the agent network sees them ---
        **holder_fields,
        "_slippage_data_missing": price_impact is None,
        "_social_data_missing": True,   # no free source; see README
        "_depth_data_missing": tradeable_depth is None,
        "_price_disagreement": price_disagreement,
    }
