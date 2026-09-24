# market_data.py
"""Real Solana market-data adapters for the agent pipeline.

This is Stage 1 of the DEX/wallet integration path described in
README.md: it replaces the random numbers pipeline_executor_worker() used
to invent for price/liquidity/volume/holder-concentration/slippage with
real ones, fetched from public data sources. It does NOT touch a wallet,
sign anything, or submit a transaction -- that's Stage 3+ (see README.md).

Data sources and their honesty caveats:
  - DexScreener (api.dexscreener.com) -- price, liquidity, volume. Free,
    no API key, ~60 req/min. Only covers tokens that have an indexed
    trading pair, so a brand-new pre-graduation pump.fun token may not
    show up yet.
  - Jupiter's quote endpoint (api.jup.ag) -- an estimated price-impact
    (slippage) figure for a hypothetical trade of a given size. Free at
    the "Lite" tier, no API key, capped around 1 request/second.
  - A Solana JSON-RPC endpoint -- getTokenLargestAccounts + getTokenSupply
    for top-10 holder concentration. The default public endpoint
    (api.mainnet.solana.com) is explicitly NOT meant for production use
    (Solana's own docs say so) and will rate-limit/throttle under load --
    set SOLANA_RPC_URL to a real provider (Helius has a usable free tier)
    before relying on this for anything beyond light testing.
  - Social/hype volume -- there is NO good free data source for this, and
    after evaluating the alternatives (Bluesky, Farcaster, Reddit, Google
    Trends, 4chan /biz/) none of them covers a token minted an hour ago.
    fetch_social_volume_score() therefore still returns a clearly-labeled
    placeholder and logs a warning on every call.

    NOTHING READS IT. The gate that used to (E_SIGNAL) was replaced by
    E_BREADTH, which measures participation breadth against committed
    capital from on-chain data we can actually observe. This field is kept
    purely as the hook for a real social feed -- twitterapi.io at roughly
    $0.15/1000 tweets is the only source that covers fresh memecoins -- and
    it stays flagged as unavailable rather than passing a constant off as
    a measurement.
"""
import os
import time
import asyncio
import logging
import unicodedata
from typing import Optional, Dict, Any, List

import httpx

import holder_concentration

logger = logging.getLogger("market_data")


# Longest token symbol we'll accept. Real ones are a handful of characters;
# anything longer is padding meant to break a layout or bury text in a log.
MAX_SYMBOL_LENGTH = 24


def sanitize_external_text(value: Any, max_length: int = MAX_SYMBOL_LENGTH,
                             fallback: str = "?") -> str:
    """Scrubs a string that came from outside our trust boundary.

    Token names and symbols are chosen by whoever minted the token. They
    are not identifiers we assigned, they are attacker-controlled input
    that we read off a public indexer, and they end up in log lines, in
    Postgres, in WebSocket payloads rendered by the dashboard, and in the
    text the agent pipeline reasons over. Treat every one of them as
    hostile.

    This removes every Unicode character in a "C" category -- control
    characters (Cc), format characters (Cf, which covers zero-width
    spaces and the bidi overrides used to make text display in an order
    that differs from its byte order), surrogates (Cs), private-use
    (Co) and unassigned (Cn) code points -- then caps the length.

    Note what this deliberately does NOT do: it does not strip "<", "&"
    or quotes. HTML escaping is the renderer's job and is done at every
    innerHTML site in the dashboard (see esc() there). Doing it here too
    would double-escape and mangle legitimate symbols, and would tempt a
    future reader into thinking the render-side escaping is redundant.
    These are two independent layers guarding two different sinks.

    Returns `fallback` if nothing survives, so a caller never has to
    handle an empty symbol.
    """
    if value is None:
        return fallback
    text = str(value)
    cleaned = "".join(ch for ch in text if not unicodedata.category(ch).startswith("C"))
    cleaned = cleaned.strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned or fallback

DEXSCREENER_BASE = os.environ.get("DEXSCREENER_API_BASE", "https://api.dexscreener.com")
JUPITER_BASE = os.environ.get("JUPITER_API_BASE", "https://api.jup.ag")

# --- Jupiter rate limiting -------------------------------------------------
#
# Jupiter's free tier allows roughly ONE request per second, and the live run
# hit 429s immediately. The cause wasn't tick frequency: fetch_full_snapshot
# fires the price lookup and the slippage quote inside the same
# asyncio.gather, so two Jupiter requests leave at the same instant and the
# second is over the limit no matter how slowly the pipeline ticks.
#
# So every Jupiter call goes through one process-wide throttle that
# serialises them and enforces a minimum gap. Concurrency elsewhere is
# unaffected -- RugCheck and DexScreener still run in parallel with these.
JUPITER_MIN_INTERVAL_S = float(os.environ.get("JUPITER_MIN_INTERVAL_S", "1.2"))
_jupiter_lock = asyncio.Lock()
_jupiter_last_call = 0.0


async def jupiter_throttle() -> None:
    """Blocks until at least JUPITER_MIN_INTERVAL_S has passed since the
    previous Jupiter request, then claims the slot."""
    global _jupiter_last_call
    async with _jupiter_lock:
        wait = JUPITER_MIN_INTERVAL_S - (time.monotonic() - _jupiter_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _jupiter_last_call = time.monotonic()


# Slippage barely moves minute to minute, and the discovery list cycles the
# same ~30 tokens, so caching cuts Jupiter traffic by roughly the ratio of
# tick rate to TTL -- the difference between comfortably under the limit and
# repeatedly banned.
JUPITER_CACHE_TTL_S = float(os.environ.get("JUPITER_CACHE_TTL_S", "120"))
_slippage_cache: Dict[str, Any] = {}
# RPC endpoint and auth are defined in holder_concentration.py -- the module
# that actually talks to the chain -- and re-exported here so every existing
# reference to market_data.SOLANA_RPC_URL still resolves, and so there is
# exactly ONE place the endpoint is configured.
#
# The credential goes in a header, never in the URL: Triton offers a
# path-style URL with the token embedded, and SOLANA_RPC_URL is printed
# verbatim in at least one error message (signer_service/main.py), which
# would land the credential in logs and alert rows.
from holder_concentration import (  # noqa: E402
    SOLANA_RPC_URL,
    SOLANA_RPC_X_TOKEN,
    rpc_headers as _rpc_headers,
)
LUNARCRUSH_API_KEY = os.environ.get("LUNARCRUSH_API_KEY", "")

# Used as the "buy with" side when probing Jupiter for a price-impact
# estimate -- USDC is deep and liquid enough that quoting against it gives
# a meaningful number regardless of which token is being evaluated.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# How large a hypothetical trade to probe Jupiter with when estimating
# slippage, in USD. This is a fixed proxy, not the pipeline's actual
# position size (which G_ANCHOR computes later, per-token, from real
# liquidity) -- it just needs to be roughly the scale of a real position.
SLIPPAGE_PROBE_USD = float(os.environ.get("SLIPPAGE_PROBE_USD", "500"))


def _price_for_side(pair: Dict[str, Any], token_address: str) -> Optional[float]:
    """The USD price OF token_address in this pair -- not the pair's price.

    THE BUG THIS EXISTS TO FIX
    DexScreener's `priceUsd` is always the BASE token's price. A token can
    appear on either side of a pair, and `token-pairs/v1/solana/{addr}`
    returns pairs where it sits on either. Reading `priceUsd` unconditionally
    therefore records the OTHER asset's price whenever our token is the quote
    side -- silently, with no error, and with a plausible-looking number.

    Observed live: SNDK entered at $0.001367 and marked at $1,637.31, a
    1.2-million-fold gap that is two different assets rather than a price
    move. 63 tokens and 390 horizon rows were corrupted this way, and because
    the contaminated values are enormous rather than merely wrong they
    destroyed the mean of the entire REJECTED cohort -- 970,987% at the
    30-minute horizon.

    HOW THE QUOTE SIDE IS PRICED
    priceNative is the base token's price expressed in quote units, so

        price(quote) = priceUsd / priceNative

    Sanity check with SOL/USDC: priceNative 200 (one SOL costs 200 USDC),
    priceUsd 200, so USDC = 200/200 = $1. Correct.

    Returns None when the token is on neither side, or when the arithmetic
    cannot be done. None means "unknown" and callers must treat it as such --
    never as zero, which would mark a position to a total loss.
    """
    base = str(((pair.get("baseToken") or {}).get("address")) or "").lower()
    quote = str(((pair.get("quoteToken") or {}).get("address")) or "").lower()
    wanted = str(token_address or "").lower()

    price_usd = _as_pos_float(pair.get("priceUsd"))
    if wanted and wanted == base:
        return price_usd
    if wanted and wanted == quote:
        if price_usd is None:
            return None
        price_native = _as_pos_float(pair.get("priceNative"))
        if price_native is None:
            # Without priceNative the quote side cannot be derived. Refusing
            # is the only safe answer: returning priceUsd here is precisely
            # the defect.
            return None
        return price_usd / price_native
    # Neither side. DexScreener returned a pair we did not ask about, which
    # happens on the batch endpoint -- it must not be attributed to anything.
    return None


def _opt_count(bucket: Optional[dict], key: str) -> Optional[int]:
    """A transaction count, or None when the provider did not report one.

    `int(bucket.get(key) or 0)` collapsed two different facts into the same
    number: "DexScreener says there were zero trades this hour" (a dead token,
    a real and damning measurement) and "DexScreener returned no txns block at
    all" (we don't know). Downstream both became a hard 0, so the liveness
    filter in stage2_check.sql section 5b silently dropped every unmeasured
    token into the dead bucket, and staleness_report counted them as tokens
    with no counterparty. An absent measurement must arrive at a gate as an
    absence.
    """
    if not isinstance(bucket, dict):
        return None
    raw = bucket.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _as_pos_float(value: Any) -> Optional[float]:
    """Strictly positive finite float, or None. Prices are strings as often
    as numbers, and a zero or NaN price is not a price."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")) or f <= 0:
        return None
    return f


async def fetch_dex_pair_data(client: httpx.AsyncClient, token_address: str) -> Optional[Dict[str, Any]]:
    """Real price/liquidity/volume for a Solana token via DexScreener.

    Returns None if the token has no indexed trading pair yet, or the
    request fails -- callers should treat that as "skip this token this
    tick", not crash the pipeline.
    """
    url = f"{DEXSCREENER_BASE}/token-pairs/v1/solana/{token_address}"
    try:
        resp = await client.get(url, timeout=10.0)
        resp.raise_for_status()
        pairs = resp.json()
    except Exception as e:
        logger.warning(f"DexScreener lookup failed for {token_address}: {e}")
        return None

    if not pairs:
        return None

    # A token can have multiple pools; use whichever has the deepest
    # liquidity as the most representative price.
    best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0))

    base_token = best.get("baseToken") or {}
    quote_token = best.get("quoteToken") or {}
    if str(base_token.get("address", "")).lower() == token_address.lower():
        raw_symbol = base_token.get("symbol") or token_address[:6]
    else:
        raw_symbol = quote_token.get("symbol") or token_address[:6]
    # Attacker-controlled -- whoever minted the token picked this string.
    symbol = sanitize_external_text(raw_symbol, fallback=token_address[:6])
    if symbol != str(raw_symbol).strip():
        logger.warning(
            f"Token {token_address} has a symbol containing control/format characters; "
            f"sanitized to {symbol!r} before use."
        )

    liquidity_usd = float((best.get("liquidity") or {}).get("usd") or 0.0)

    # Which side of this pair we are on decides the price. The symbol logic
    # above already worked that out; for a long time the price ignored it and
    # took priceUsd regardless -- see _price_for_side().
    resolved = _price_for_side(best, token_address)
    if resolved is None:
        # Every other field on this pair (liquidity, volume, txn counts) is
        # side-independent and still valid, but a token with no usable price
        # cannot be evaluated: record_candidate() drops it at `price <= 0`
        # and the gates have nothing to judge. Refusing here is the same
        # fail-closed choice made everywhere else a number is unavailable.
        logger.warning(
            f"No usable price for {token_address} in its deepest pool "
            f"(base={((best.get('baseToken') or {}).get('symbol'))!r}, "
            f"quote={((best.get('quoteToken') or {}).get('symbol'))!r}). "
            f"Skipping rather than recording another pool's price.")
        return None
    price_usd = resolved
    volume = best.get("volume") or {}
    volume_h1 = float(volume.get("h1") or 0.0)
    volume_h24 = float(volume.get("h24") or 0.0)

    # Transaction COUNTS -- the breadth half of the breadth-vs-depth gate
    # (see engine.node_E_BREADTH). Volume alone can't distinguish one
    # whale buying $10k from five thousand bots buying $2 each, and those
    # are very different tokens. Counts are what separate them.
    txns_h1 = (best.get("txns") or {}).get("h1")
    txns_h1_buys = _opt_count(txns_h1, "buys")
    txns_h1_sells = _opt_count(txns_h1, "sells")

    # m5 is the FINEST bucket DexScreener publishes -- there is nothing below
    # five minutes, and chasing shorter windows off-API would be a mistake
    # anyway: a token doing 50 trades an hour has no trades at all in most
    # 10-second windows, so the shorter the window the more of it is noise.
    # A signal is also a commitment to react on its timescale, and between
    # the tick loop, DexScreener's caching, Jupiter quoting and ~400ms
    # blocks, anything sub-minute belongs to bots we would be trading
    # against rather than alongside.
    #
    # These are carried for MEASUREMENT, not for gating. Whether buy/sell
    # imbalance predicts anything is an empirical question the paper-trading
    # experiment can answer -- see paper_trading.feature_correlations(). It
    # is weaker than it looks: every buy has a seller, the DEX merely labels
    # trades by their direction against the pool, and imbalance in COUNT is
    # not imbalance in SIZE (900 one-dollar buys against 50 large sells is
    # distribution, not accumulation). Adding it as a gate on intuition is
    # how E_SIGNAL came to filter on noise for months.
    txns_m5 = (best.get("txns") or {}).get("m5")
    txns_m5_buys = _opt_count(txns_m5, "buys")
    txns_m5_sells = _opt_count(txns_m5, "sells")
    volume_m5 = float(volume.get("m5") or 0.0)
    price_change = best.get("priceChange") or {}

    # priceChange is the percent move of priceUsd -- i.e. of the BASE token.
    # It is the one remaining field on this pair that is NOT side-independent,
    # and it was being copied across regardless, which is the same defect
    # _price_for_side() was written to fix, one field over.
    #
    # Unlike price there is no correct derivation for the quote side: the
    # quote token's own USD move cannot be recovered from the base token's
    # percentage alone. So the only honest answer is None. Recording the base
    # token's move instead would put a +400% "1h momentum" on a token that did
    # not move, and feature_correlations() would then regress that invented
    # feature against the real return -- manufacturing a signal out of a
    # neighbouring asset, in the same shape as the corrupted cohort means.
    ours_is_base = (str(((best.get("baseToken") or {}).get("address")) or "").lower()
                    == str(token_address or "").lower())

    def _pct(key):
        if not ours_is_base:
            return None
        v = price_change.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "token_symbol": symbol,
        "price_usd": price_usd,
        "liquidity_usd": liquidity_usd,
        "volume_h1": volume_h1,
        "volume_h24": volume_h24,
        "volume_m5": volume_m5,
        "txns_h1_buys": txns_h1_buys,
        "txns_h1_sells": txns_h1_sells,
        "txns_m5_buys": txns_m5_buys,
        "txns_m5_sells": txns_m5_sells,
        "price_change_m5": _pct("m5"),
        "price_change_h1": _pct("h1"),
        "pair_address": best.get("pairAddress"),
        "dex_id": best.get("dexId"),
    }


# DexScreener accepts up to 30 comma-separated addresses on its multi-token
# endpoint. Marking open positions one at a time would cost one request per
# position per tick and hit the 60/min limit almost immediately; batching
# makes it one request regardless of how many positions are open.
PRICE_BATCH_SIZE = 30

# Base58 excludes 0, O, I and l precisely so they can't be confused visually.
_BASE58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def is_plausible_solana_address(value: Any) -> bool:
    """Cheap structural check that a string could be a Solana mint address.

    Not a validity guarantee -- it doesn't verify the address exists or that
    the checksum works -- just a filter for values that provably cannot be
    one: Ethereum-style 0x addresses, truncated display strings containing
    an ellipsis, and anything outside base58's alphabet or length range.
    """
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not (32 <= len(v) <= 44):
        return False
    return all(c in _BASE58_ALPHABET for c in v)


def fetch_current_prices_sync(token_addresses: List[str]) -> Dict[str, float]:
    """Current USD prices for many tokens at once.

    Thin wrapper over fetch_current_marks_sync() -- kept because every caller
    that only needs a price should not have to know about the rest of the mark.
    """
    return {a: m["price"] for a, m in fetch_current_marks_sync(token_addresses).items()}


def fetch_current_marks_sync(token_addresses: List[str]) -> Dict[str, Dict[str, Any]]:
    """Current price PLUS the liveness evidence from the same response.

    DexScreener returns the whole pair object on this endpoint, transaction
    counts included, so the evidence costs nothing extra -- no second request,
    no second rate-limit budget. Discarding it was what left mark_to_market
    unable to tell a real fill from a stale print.

    Each value is {"price", "liquidity_usd", "txns_m5", "txns_h1"}, where the
    two counts are None when the provider reported none. None is not zero: see
    _opt_count.

    Synchronous on purpose: this is called from evaluate_open_positions(),
    which the pipeline already runs in a worker thread via run_in_executor,
    so a blocking client is the simpler correct choice there than threading
    an event loop through the engine.

    Returns a dict of address -> price, omitting any token that couldn't be
    priced. A missing entry means "unknown", and callers must leave that
    position untouched rather than treating it as a price of zero -- marking
    a position to zero would instantly trigger its stop-loss and book a
    fabricated total loss.
    """
    if not token_addresses:
        return {}

    marks: Dict[str, Dict[str, Any]] = {}
    unique = list(dict.fromkeys(a for a in token_addresses if is_plausible_solana_address(a)))
    skipped = [a for a in token_addresses if a and not is_plausible_solana_address(a)]
    if skipped:
        # Seen live: a stale row held "0x6927...pump" -- an Ethereum-style
        # prefix with a literal ellipsis, left over from the pre-Stage-1
        # random-data era. It can never price, and including it in every
        # batch wasted a request per tick forever.
        logger.warning(
            f"Skipping {len(skipped)} implausible token address(es) in the price batch: "
            f"{sorted(set(skipped))[:3]}. These will never price -- clean them out of "
            f"active_positions."
        )
    try:
        with httpx.Client(timeout=15.0) as client:
            for i in range(0, len(unique), PRICE_BATCH_SIZE):
                chunk = unique[i:i + PRICE_BATCH_SIZE]
                url = f"{DEXSCREENER_BASE}/tokens/v1/solana/{','.join(chunk)}"
                resp = client.get(url)
                resp.raise_for_status()
                # Attribute every pair to the address WE ASKED FOR, never to
                # whatever came back. This endpoint returns pairs in which a
                # requested token appears on EITHER side, so keying by
                # baseToken.address filed the price under a different token
                # entirely whenever ours was the quote side -- and left ours
                # absent, which is the "No current price for X this tick"
                # warning that ran on every tick for weeks. The token was
                # never missing; it was misfiled.
                wanted = {a.lower(): a for a in chunk}
                for pair in (resp.json() or []):
                    base_addr = str(((pair.get("baseToken") or {}).get("address")) or "").lower()
                    quote_addr = str(((pair.get("quoteToken") or {}).get("address")) or "").lower()
                    for side_addr in (base_addr, quote_addr):
                        addr = wanted.get(side_addr)
                        if not addr:
                            continue
                        value = _price_for_side(pair, addr)
                        if value is None:
                            continue
                        # A token can appear in many pools; keep the deepest
                        # pool's price, matching how fetch_dex_pair_data picks.
                        liq = float((pair.get("liquidity") or {}).get("usd") or 0.0)
                        if addr in marks and liq <= marks[addr]["liquidity_usd"]:
                            continue
                        txns = pair.get("txns") or {}
                        m5, h1 = txns.get("m5"), txns.get("h1")

                        def _total(bucket):
                            b = _opt_count(bucket, "buys")
                            sl = _opt_count(bucket, "sells")
                            if b is None and sl is None:
                                return None
                            return (b or 0) + (sl or 0)

                        marks[addr] = {
                            "price": value,
                            "liquidity_usd": liq,
                            "txns_m5": _total(m5),
                            "txns_h1": _total(h1),
                        }
    except Exception as e:
        logger.warning(f"Batch price lookup failed: {e}")

    return marks


def onchain_flow_velocity_proxy(volume_h1: float, liquidity_usd: float) -> float:
    """Heuristic stand-in for 'on-chain flow velocity': how much of the
    pool's liquidity turned over in the last hour, expressed on roughly
    the 0-100 scale the pipeline's original simulated values used.

    This is a derived judgment call on our part, not a published
    third-party metric -- there's no standard "flow velocity" API. Tune
    the scaling if it doesn't feel representative once you see it against
    real tokens.
    """
    if liquidity_usd <= 0:
        return 0.0
    turnover_ratio = volume_h1 / liquidity_usd
    return round(min(turnover_ratio * 100.0, 100.0), 2)


async def fetch_price_impact_pct(client: httpx.AsyncClient, token_address: str,
                                   trade_size_usd: float = SLIPPAGE_PROBE_USD) -> Optional[float]:
    """Estimated slippage/price-impact (as a percent) for a hypothetical
    USDC -> token_address swap of trade_size_usd, via Jupiter's quote
    endpoint. Returns None if the quote fails (e.g. no route exists)."""
    cached = _slippage_cache.get(token_address)
    if cached and (time.monotonic() - cached["at"]) < JUPITER_CACHE_TTL_S:
        return cached["value"]

    usdc_amount = int(max(trade_size_usd, 1.0) * 1_000_000)  # USDC has 6 decimals
    url = f"{JUPITER_BASE}/swap/v1/quote"
    await jupiter_throttle()
    params = {
        "inputMint": USDC_MINT,
        "outputMint": token_address,
        "amount": usdc_amount,
        "slippageBps": 100,
    }
    try:
        resp = await client.get(url, params=params, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        impact = data.get("priceImpactPct")
        value = round(abs(float(impact)) * 100.0, 3) if impact is not None else None
        # An impact of exactly zero means the route did not COMPUTE one, not
        # that the trade is free. Jupiter returns "0" when it routes through
        # an AMM that reports no impact, and a $500 probe into a real pool is
        # never exactly 0.000%: even an extremely deep pool returns something
        # that survives three decimal places (0.0000123 -> 0.001).
        #
        # Left as a measured 0.0 it set _slippage_data_missing = False, which
        # skipped G_ANCHOR's fail-closed branch entirely -- and stored
        # assumed_slippage_percent = 0.0 instead of NULL, charging a zero
        # round-trip cost to exactly the thin tokens whose real costs are
        # largest. That is the bias paper_trading's own comment block exists
        # to prevent.
        if value is not None and value <= 0.0:
            logger.info(
                f"Jupiter reported zero price impact for {token_address} -- treating as "
                f"unmeasured rather than free. A real quote is never exactly 0.")
            value = None
        _slippage_cache[token_address] = {"value": value, "at": time.monotonic()}
        return value
    except Exception as e:
        logger.warning(f"Jupiter quote failed for {token_address}: {e}")
        return None


async def fetch_top10_holder_pct(client: httpx.AsyncClient, token_address: str) -> Optional[float]:
    """Top-10 holder concentration from chain, RAW -- every one of the ten
    largest token accounts, including the AMM pool and the bonding curve.

    Delegates to holder_concentration so this provider and the "free"
    provider cannot drift into computing different quantities for the same
    30% ceiling. Returns None on any failure; a 0% concentration does not
    exist, and the caller's `_holder_data_missing` flag is what F_ATLAS
    reads to refuse rather than to treat an unmeasured token as clean.

    NOTE the definition: this is the raw number. It is NOT comparable with
    RugCheck's wallet-based figure -- see holder_concentration's docstring.
    """
    chain = await holder_concentration.fetch_chain_concentration(client, token_address)
    if chain.raw_percent is None:
        logger.warning(
            f"Chain holder lookup failed for {token_address}: {chain.error}")
    return chain.raw_percent


async def fetch_social_volume_score(client: httpx.AsyncClient, token_symbol: str) -> float:
    """Social/hype volume score. UNUSED by any gate -- see module docstring.

    THERE IS NO GOOD FREE DATA SOURCE FOR THIS as of when this was written
    -- LunarCrush is the standard provider but no longer offers a free
    tier. Without LUNARCRUSH_API_KEY configured, this returns a fixed,
    deliberately-neutral placeholder (not tuned to pass or fail the gate)
    and logs a warning on every call so it can't be mistaken for real data
    in your logs. The LunarCrush branch below is a starting point based on
    their published API shape, not verified against a live response --
    confirm it against their current docs if you wire in a real key.
    """
    if not LUNARCRUSH_API_KEY:
        # Returned 25.0 -- a number that was then written to the database and
        # broadcast to the dashboard as though it had been measured, while the
        # log line claimed it was a placeholder. A reader of the data had no
        # way to tell. The other provider (free_market_data) already returns
        # 0.0 here and relies on the _social_data_missing flag to mark it
        # unavailable; this now matches, so the two providers agree and the
        # flag is the single place that says "not measured".
        logger.warning(
            f"[SIMULATED] No LUNARCRUSH_API_KEY configured -- social_volume_score for "
            f"${token_symbol} is unavailable and is reported as 0.0 with "
            f"_social_data_missing set. It is NOT a measurement. See README.md."
        )
        return 0.0

    try:
        resp = await client.get(
            "https://lunarcrush.com/api4/public/coins/list/v1",
            headers={"Authorization": f"Bearer {LUNARCRUSH_API_KEY}"},
            params={"symbol": token_symbol},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("data") or []
        if not rows:
            return 0.0
        return float(rows[0].get("social_volume_24h") or rows[0].get("interactions_24h") or 0.0)
    except Exception as e:
        logger.warning(f"LunarCrush lookup failed for {token_symbol}: {e}")
        return 0.0


async def fetch_full_snapshot(client: httpx.AsyncClient, token_address: str) -> Optional[Dict[str, Any]]:
    """Fetches everything the agent pipeline's `inputs` dict needs for one
    token in a single call, or returns None if DexScreener has no pair
    data for it (in which case there's nothing to build gate inputs from,
    so the caller should skip this token for this tick).
    """
    dex_data = await fetch_dex_pair_data(client, token_address)
    if dex_data is None:
        return None

    price_impact, chain, social_score = await asyncio.gather(
        fetch_price_impact_pct(client, token_address),
        holder_concentration.fetch_chain_concentration(client, token_address),
        fetch_social_volume_score(client, dex_data["token_symbol"]),
    )
    # This provider's historic number IS the raw chain figure, so it is what
    # gets offered as the "provider" reading. The wallet-only split rides
    # alongside it unused until the definition is chosen deliberately.
    holder_fields = holder_concentration.snapshot_fields(chain.raw_percent, chain)

    return {
        "token_symbol": dex_data["token_symbol"],
        "token_address": token_address,
        "current_price": dex_data["price_usd"],
        "pool_liquidity_usd": dex_data["liquidity_usd"],
        "social_volume_score": social_score,
        "onchain_flow_velocity": onchain_flow_velocity_proxy(dex_data["volume_h1"], dex_data["liquidity_usd"]),
        "estimated_slippage_percent": price_impact if price_impact is not None else 0.0,
        "onchain_volume_increasing": dex_data["volume_h1"] * 24.0 > dex_data["volume_h24"],
        # DexScreener reports total value locked (both sides of the pool),
        # so the side we actually trade against is about half of it. See
        # free_market_data.compute_tradeable_depth() for why this matters:
        # sizing off TVL takes roughly double the depth the gate intends.
        # This provider has no independent one-sided figure to cross-check
        # against, so it's the plain halving.
        "tradeable_depth_usd": round(dex_data["liquidity_usd"] / 2.0, 2) if dex_data["liquidity_usd"] else 0.0,
        # Breadth inputs for E_BREADTH (see engine.py). volume_h1 is the
        # capital side; the txn counts are the participation side.
        "volume_h1_usd": dex_data["volume_h1"],
        "txns_h1_buys": dex_data["txns_h1_buys"],
        "txns_h1_sells": dex_data["txns_h1_sells"],
        # Short-window features, recorded for measurement only (see above).
        "volume_m5_usd": dex_data.get("volume_m5"),
        "txns_m5_buys": dex_data.get("txns_m5_buys"),
        "txns_m5_sells": dex_data.get("txns_m5_sells"),
        "price_change_m5": dex_data.get("price_change_m5"),
        "price_change_h1": dex_data.get("price_change_h1"),
        "total_holders": None,
        # Not available from this provider -- the free provider supplies them.
        "rug_score": None,
        "rugged": None,
        "mint_authority_renounced": None,
        "freeze_authority_renounced": None,
        "token_age_hours": None,
        "launchpad": None,
        **holder_fields,
        "_slippage_data_missing": price_impact is None,
        "_depth_data_missing": not dex_data["liquidity_usd"],
        "_price_disagreement": False,
        # Without a LunarCrush key the score above is a fixed placeholder,
        # not a measurement -- flag it as missing so the caller logs it the
        # same way it logs any other unavailable input, rather than letting
        # a hardcoded number quietly pass for real data.
        "_social_data_missing": not LUNARCRUSH_API_KEY,
    }


# --- Provider selection ----------------------------------------------------
#
# "free" (recommended) uses free_market_data.py -- DexScreener for price,
# TVL and volume, RugCheck for holder concentration and rug signals, and
# Jupiter for an independent price, token age and slippage. No API key of
# any kind, and it's the only provider that supplies rug/security data.
#
# "dexscreener" is this module's original path: DexScreener plus a Solana
# RPC holder lookup against an endpoint Solana says isn't for production.
# Kept as a fallback and for continuity.
#
# "gmgn" uses gmgn_market_data.py. NOTE: GMGN's API sits behind Cloudflare
# bot management that refuses plain Python clients, and their own docs cap
# the OpenAPI at ~1 request/second with no high-availability guarantee, so
# this provider is effectively unusable for this pipeline. Kept only so the
# integration isn't lost if their access model changes.
#
# All providers return identical dict keys.
MARKET_DATA_PROVIDER = os.environ.get("MARKET_DATA_PROVIDER", "dexscreener").strip().lower()


async def get_snapshot(client: httpx.AsyncClient, token_address: str) -> Optional[Dict[str, Any]]:
    """Dispatches to the configured provider. This is what the pipeline
    calls -- it should never reach into a specific provider directly, so
    that switching providers stays a one-env-var change.

    The gmgn import is deliberately lazy: gmgn_market_data imports helpers
    from this module, so importing it at module scope would be circular.
    """
    if MARKET_DATA_PROVIDER == "free":
        import free_market_data
        return await free_market_data.fetch_full_snapshot(client, token_address)
    if MARKET_DATA_PROVIDER == "gmgn":
        import gmgn_market_data
        return await gmgn_market_data.fetch_full_snapshot(client, token_address)
    if MARKET_DATA_PROVIDER != "dexscreener":
        logger.warning(
            f"Unknown MARKET_DATA_PROVIDER {MARKET_DATA_PROVIDER!r} -- falling back to "
            f"'dexscreener'. Valid values: free, dexscreener, gmgn."
        )
    return await fetch_full_snapshot(client, token_address)
