"""A holding pen for newly created pools, so the recency arm can exist at all.

WHY THIS IS BACK

A pen like this was deleted from this codebase, and deleting it was correct at
the time: Birdeye applied a liquidity filter SERVER-side and then sorted by
listing time, so one page of 100 rows reached back about ten hours and the
join between "new" and "liquid" needed no memory.

That is gone. Birdeye's free tier answers every endpoint with "Compute units
usage limit exceeded" under a three-minute refresh, and no free provider
offers the same filter-then-sort. Measured on the replacements:

    geckoterminal new_pools     60 rows spanning 2 minutes
    jupiter /tokens/v2/recent   30 rows spanning 1 minute

Solana creates roughly thirty pools a minute. Fifteen minutes ago is about
450 tokens back, so NO newest-first endpoint can reach the evaluation window.
The join has to happen in memory again, which is what this is.

THE TWO DECISIONS THAT MAKE IT CORRECT

1. AGE COMES FROM THE PROVIDER, NEVER OUR CLOCK. `pool_created_at` is stored
   and `first_seen_at` is only diagnostic. If the capture loop stalls for ten
   minutes -- a restart, a rate limit, a dead container -- everything captured
   afterwards would be dated from when we happened to notice it, and the whole
   backlog would release at once looking fifteen minutes old. Ageing from the
   chain's timestamp makes an outage cost coverage, which is visible, instead
   of correctness, which is not.

2. CAPTURE BROADLY, FILTER AT RELEASE. A pool one minute old has almost no
   liquidity -- the measured median was $3,551 at twenty-four seconds. The
   tokens worth evaluating are the ones that GREW into the floor by the time
   they are fifteen minutes old. Applying the floor at capture would discard
   precisely the population this exists to find, and would look like it was
   working.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("discovery_pen")

GECKOTERMINAL_BASE = os.environ.get("GECKOTERMINAL_API_BASE", "https://api.geckoterminal.com")
DEXSCREENER_BASE = os.environ.get("DEXSCREENER_API_BASE", "https://api.dexscreener.com")

# Pages of new_pools to sweep per capture.
#
# MEASURED, not assumed: GeckoTerminal returns 20 rows a page, and Solana
# creates roughly 30 pools a minute. So each page covers about 40 seconds of
# launches. At a 60-second cadence two pages is only ~1.3x redundancy -- one
# slow cycle or a burst of launches and tokens fall through the gap
# permanently, because there is no second chance to see a pool being born.
# Four pages is ~2.7x, and costs four throttled calls a minute against a
# provider already in the rotation.
PEN_CAPTURE_PAGES = int(os.environ.get("PEN_CAPTURE_PAGES", "4"))
PEN_CAPTURE_INTERVAL_S = float(os.environ.get("PEN_CAPTURE_INTERVAL_S", "60"))

# Rows per DexScreener batch call. The pipeline already uses this endpoint at
# this width elsewhere.
PEN_BATCH_SIZE = int(os.environ.get("PEN_BATCH_SIZE", "30"))

# WHEN in the window a token is examined.
#
# This is not the same as the window's lower bound, and conflating them was a
# real defect: releasing from 15 minutes meant every token was measured at the
# YOUNGEST moment it was eligible, which is the worst possible time to ask
# whether it cleared a liquidity floor. The whole premise of the pen is that
# these tokens GROW into the floor -- measured median liquidity was $3,551 at
# twenty-four seconds old. Checking at 15 minutes and concluding "the floor is
# too high" would have been a conclusion about the measurement, not the market.
#
# 30 minutes leaves an hour of the 15-90 window for the agent to evaluate and
# mark the token, while giving it twice as long to build a pool.
PEN_EXAMINE_AGE_MINUTES = float(os.environ.get("PEN_EXAMINE_AGE_MINUTES", "30"))

# Anything older than the window plus this margin is dead weight; pruning
# keeps the table bounded at roughly (pools/minute * window) rows.
PEN_RETENTION_MINUTES = float(os.environ.get("PEN_RETENTION_MINUTES", "360"))

_BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def parse_created(value: Any) -> Optional[datetime]:
    """Provider timestamp -> aware datetime, or None if it cannot be read.

    asyncpg binds parameters by TYPE. A timestamptz parameter wants a
    datetime; handing it the provider's ISO STRING raises rather than
    coercing, which is how the first version of this stored zero of every
    forty rows it fetched while reporting a clean sweep.

    Both shapes are accepted because providers ship both, and an unreadable
    value returns None so the row is skipped -- a token that cannot be aged
    must never enter the pen, because it would either never release or
    release at the wrong age, and both are silent.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and value > 0:
        epoch = float(value) / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _plausible_mint(value: Any) -> bool:
    return (isinstance(value, str) and 32 <= len(value) <= 44
            and all(ch in _BASE58 for ch in value))


def _as_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


async def capture(conn, client: httpx.AsyncClient) -> Dict[str, int]:
    """Sweep new pools into the pen. Returns {seen, stored}.

    Never raises: a capture failure costs coverage for one cycle, and the
    caller is a background loop that must keep running.
    """
    seen = stored = undated = 0
    last_error: Optional[str] = None
    for page in range(1, PEN_CAPTURE_PAGES + 1):
        try:
            resp = await client.get(
                f"{GECKOTERMINAL_BASE}/api/v2/networks/solana/new_pools",
                params={"page": page}, timeout=20.0)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            logger.warning(f"Pen capture page {page} failed: {type(e).__name__}: {e}")
            break

        for row in (payload or {}).get("data") or []:
            attrs = row.get("attributes") or {}
            rel = ((row.get("relationships") or {}).get("base_token") or {}).get("data") or {}
            mint = str(rel.get("id") or "").replace("solana_", "")
            created = attrs.get("pool_created_at") or attrs.get("created_at")
            seen += 1
            # A pool with no creation timestamp cannot be aged, and a token
            # that cannot be aged must not enter the pen -- it would either
            # never release or release at the wrong time, and both are silent.
            born = parse_created(created)
            if not _plausible_mint(mint) or born is None:
                undated += 1
                continue
            try:
                await conn.execute(
                    """
                    INSERT INTO discovery_pen (token_address, pool_created_at, source)
                    VALUES ($1, $2, 'geckoterminal-new-pools')
                    ON CONFLICT (token_address) DO NOTHING;
                    """, mint, born)
                stored += 1
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"

    # Storing NOTHING from a sweep that fetched rows is a failure, not a quiet
    # market, and it is invisible downstream: the pen simply stays empty and
    # discovery reports healthy counts from its other sources. The first
    # version of this logged the cause at DEBUG and ran for an hour storing
    # zero of every forty.
    if seen and not stored:
        logger.warning(
            f"Pen capture stored NOTHING from {seen} fetched rows "
            f"({undated} unusable). This is a defect, not a quiet market -- "
            f"last error: {last_error or 'none; every row was unusable'}")
    return {"seen": seen, "stored": stored, "undated": undated}


async def _liquidity_for(client: httpx.AsyncClient,
                         mints: List[str]) -> Dict[str, float]:
    """{mint: deepest pool liquidity in USD} for as many as can be read.

    A mint missing from the result has NO reading, which the caller treats as
    "does not clear the floor" rather than as zero. The distinction matters:
    these are tokens we are choosing to spend an evaluation slot on, and an
    unreadable one is not evidence of anything.
    """
    out: Dict[str, float] = {}
    for i in range(0, len(mints), PEN_BATCH_SIZE):
        chunk = mints[i:i + PEN_BATCH_SIZE]
        try:
            resp = await client.get(
                f"{DEXSCREENER_BASE}/tokens/v1/solana/{','.join(chunk)}", timeout=20.0)
            resp.raise_for_status()
            pairs = resp.json()
        except Exception as e:
            logger.warning(f"Pen liquidity batch failed: {type(e).__name__}: {e}")
            continue
        for pair in pairs if isinstance(pairs, list) else []:
            if not isinstance(pair, dict):
                continue
            liq = _as_float(((pair.get("liquidity") or {}).get("usd")))
            if liq is None:
                continue
            for side in ("baseToken", "quoteToken"):
                addr = (pair.get(side) or {}).get("address")
                if addr in chunk:
                    # Deepest pool wins, matching how the rest of the
                    # pipeline reads liquidity for a token.
                    out[addr] = max(out.get(addr, 0.0), liq)
    return out


async def release_due(conn, client: httpx.AsyncClient, *,
                      min_age_minutes: float, max_age_minutes: float,
                      floor_usd: float, limit: int = 120) -> List[str]:
    """Mints that have aged into the window AND now clear the floor.

    Marks what it returns as released so the same token is not offered on
    every refresh for the rest of its window.
    """
    try:
        rows = await conn.fetch(
            """
            SELECT token_address
            FROM discovery_pen
            WHERE released_at IS NULL
              AND pool_created_at <= NOW() - ($1::double precision * INTERVAL '1 minute')
              AND pool_created_at >= NOW() - ($2::double precision * INTERVAL '1 minute')
            ORDER BY pool_created_at
            LIMIT $3::int;
            """, float(min_age_minutes), float(max_age_minutes), int(limit))
    except Exception as e:
        logger.warning(f"Pen release query failed: {type(e).__name__}: {e}")
        return []

    candidates = [r["token_address"] for r in rows]
    if not candidates:
        return []

    liquidity = await _liquidity_for(client, candidates)
    qualified = [m for m in candidates if liquidity.get(m, 0.0) >= floor_usd]

    # The measured liquidity is recorded for EVERY examined token, passing or
    # not. Storing it only for the winners would have thrown away exactly the
    # data needed to set the floor: the distribution that matters includes the
    # failures, and without them the only observable is "N of M passed at
    # whatever floor happens to be configured" -- which cannot tell a floor
    # that is too high from a market that is thin.
    #
    # Everything examined is marked, including the failures. A token too thin
    # at thirty minutes is not re-checked: re-offering it every refresh would
    # spend the same evaluation slot repeatedly on the candidate least likely
    # to deserve it, and starve the ones arriving behind it.
    try:
        await conn.executemany(
            """
            UPDATE discovery_pen
            SET released_at = CURRENT_TIMESTAMP,
                liquidity_at_release = $2::numeric,
                qualified = $3::boolean
            WHERE token_address = $1;
            """,
            [(m, liquidity.get(m), m in set(qualified)) for m in candidates])
    except Exception as e:
        logger.warning(f"Pen release marking failed: {type(e).__name__}: {e}")

    if candidates:
        logger.info(
            f"Pen released {len(qualified)}/{len(candidates)} aged tokens "
            f"above ${floor_usd:,.0f} (window {min_age_minutes:.0f}-{max_age_minutes:.0f} min).")
    return qualified


async def prune(conn) -> int:
    try:
        result = await conn.execute(
            "DELETE FROM discovery_pen WHERE pool_created_at < NOW() - ($1::double precision * INTERVAL '1 minute');",
            float(PEN_RETENTION_MINUTES))
        return int(str(result).rsplit(" ", 1)[-1] or 0)
    except Exception as e:
        logger.warning(f"Pen prune failed: {type(e).__name__}: {e}")
        return 0


async def stats(conn) -> Dict[str, Any]:
    """Pen health, for the log line. An empty pen is the failure that would
    otherwise look identical to a quiet market."""
    try:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE released_at IS NULL) AS waiting,
                   COUNT(*) FILTER (WHERE qualified) AS qualified,
                   EXTRACT(EPOCH FROM (NOW() - MIN(pool_created_at)))/60.0 AS oldest_minutes
            FROM discovery_pen;
            """)
        return dict(row) if row else {}
    except Exception as e:
        logger.warning(f"Pen stats failed: {type(e).__name__}: {e}")
        return {}
