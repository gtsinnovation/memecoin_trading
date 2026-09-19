# probe_discovery.py
"""READ-ONLY probe: can discovery be widened enough to unstick Stage 2?

Changes nothing. Writes nothing. Makes only GET requests.

    docker compose exec web python probe_discovery.py

THE QUESTION THIS ANSWERS
After ~18 hours the experiment plateaued at 50 distinct tokens and 17
approved. Discovery returns 34 candidates per refresh, and all three of its
sources (RugCheck trending, DexScreener top boosts, GeckoTerminal trending
pools) are POPULARITY rankings -- which are stable over hours by design.
DISCOVERY_MAX_CANDIDATES is 60 and isn't binding, so the cap is not the
constraint; source breadth is.

The bottleneck is APPROVED tokens, and approval needs >= $20k tradeable
depth. So the intuitive fix -- add a "new pools" feed -- may well make things
worse: young pools are thin, they would fail B_SENTINEL, and the REJECTED
cohort would grow while APPROVED stayed stuck. This probe tests that claim
rather than assuming it, and measures whether a volume-ranked, paginated pool
list would do better.

WHAT IT REPORTS
  1. Does the pools endpoint paginate, and does sorting work?
  2. How many unique tokens do N pages yield?
  3. How much do they overlap what discovery already finds?
  4. How many clear a $20k tradeable-depth bar -- the number that decides
     whether the APPROVED cohort can actually grow?
  5. The same for new_pools, as a control.
"""
import asyncio
import os
import sys

import httpx

GECKOTERMINAL_BASE = os.environ.get("GECKOTERMINAL_API_BASE", "https://api.geckoterminal.com")
HEADERS = {"User-Agent": "memecoin-trading-agent/1.0", "Accept": "application/json"}

# Free tier is 30 calls/minute. 2.5s between calls keeps us at ~24/min.
CALL_SPACING_S = 2.5
PAGES = int(os.environ.get("PROBE_PAGES", "8"))

# B_SENTINEL's threshold, in one-sided terms (engine.node_B_SENTINEL).
MIN_DEPTH_USD = 20000.0


def tradeable_depth(reserve_usd: float) -> float:
    """Conservative one-sided depth from a total pool reserve.

    GeckoTerminal's reserve_in_usd is the whole pool, both sides summed --
    the same convention that caused the Stage 1 sizing bug. Halving it is the
    same conservative treatment free_market_data.compute_tradeable_depth()
    applies, so this probe's bar means the same thing B_SENTINEL's does.
    """
    return reserve_usd / 2.0


def _pools_from(payload):
    """(token_address, pool_name, reserve_usd) for each pool in a response."""
    out = []
    for row in (payload or {}).get("data", []) or []:
        try:
            attrs = row.get("attributes") or {}
            rels = row.get("relationships") or {}
            tid = rels["base_token"]["data"]["id"]
            if not isinstance(tid, str) or not tid.startswith("solana_"):
                continue
            addr = tid.split("solana_", 1)[1]
            reserve = float(attrs.get("reserve_in_usd") or 0.0)
            out.append((addr, attrs.get("name") or "?", reserve))
        except Exception:
            continue
    return out


async def _get(client, label, url):
    try:
        r = await client.get(url, headers=HEADERS, timeout=20.0)
        if r.status_code != 200:
            print(f"    {label}: HTTP {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        print(f"    {label}: {type(e).__name__}: {e}")
        return None


async def main() -> int:
    print("=" * 72)
    print("DISCOVERY WIDENING PROBE -- read-only, changes nothing")
    print("=" * 72)

    async with httpx.AsyncClient() as client:
        # --- baseline: what discovery finds today
        print("\n[1] Current discovery set")
        try:
            import token_discovery
            baseline = set(await token_discovery.discover_candidates(client, force_refresh=True))
            print(f"    {len(baseline)} unique candidates from the three existing sources")
        except Exception as e:
            print(f"    could not load token_discovery ({e}); continuing without a baseline")
            baseline = set()

        # --- does the pools endpoint paginate and sort?
        print(f"\n[2] GeckoTerminal /networks/solana/pools -- pagination and sorting")
        base_url = f"{GECKOTERMINAL_BASE}/api/v2/networks/solana/pools"
        p1 = await _get(client, "page=1", f"{base_url}?page=1")
        await asyncio.sleep(CALL_SPACING_S)
        p2 = await _get(client, "page=2", f"{base_url}?page=2")
        await asyncio.sleep(CALL_SPACING_S)
        if p1 is None:
            print("    endpoint unavailable -- stopping. Nothing was changed.")
            return 1
        a1 = {a for a, _, _ in _pools_from(p1)}
        a2 = {a for a, _, _ in _pools_from(p2)} if p2 else set()
        print(f"    page 1: {len(a1)} pools")
        print(f"    page 2: {len(a2)} pools")
        if not a2:
            print("    PAGINATION: page 2 empty -- the endpoint may not paginate this way")
        elif a1 & a2 == a1:
            print("    PAGINATION: page 2 identical to page 1 -- `page` is being IGNORED")
        else:
            print(f"    PAGINATION: WORKS -- {len(a2 - a1)} of {len(a2)} page-2 pools are new")

        sorted_resp = await _get(client, "sort=h24_volume_usd_desc",
                                 f"{base_url}?page=1&sort=h24_volume_usd_desc")
        await asyncio.sleep(CALL_SPACING_S)
        if sorted_resp is not None:
            s1 = [a for a, _, _ in _pools_from(sorted_resp)]
            same = s1 == [a for a, _, _ in _pools_from(p1)]
            print(f"    SORTING: {'accepted but ordering unchanged (may be the default)' if same else 'changes the ordering'}")

        # --- how much does pagination actually buy?
        print(f"\n[3] Walking {PAGES} pages")
        pools = {}
        for page in range(1, PAGES + 1):
            data = await _get(client, f"page={page}", f"{base_url}?page={page}")
            if data is None:
                print(f"    page {page}: failed -- stopping the walk here")
                break
            rows = _pools_from(data)
            if not rows:
                print(f"    page {page}: no pools returned -- end of results")
                break
            for addr, name, reserve in rows:
                # Keep the deepest pool per token, matching how the pipeline
                # picks a pair.
                if addr not in pools or reserve > pools[addr][1]:
                    pools[addr] = (name, reserve)
            print(f"    page {page}: {len(rows):>3} pools, {len(pools):>4} unique tokens so far")
            await asyncio.sleep(CALL_SPACING_S)

        if not pools:
            print("\n    No pools collected. Nothing was changed.")
            return 1

        # --- the number that decides whether APPROVED can grow
        print(f"\n[4] How many clear B_SENTINEL's ${MIN_DEPTH_USD:,.0f} tradeable-depth bar?")
        deep = {a: v for a, v in pools.items() if tradeable_depth(v[1]) >= MIN_DEPTH_USD}
        print(f"    tokens found          : {len(pools)}")
        print(f"    clearing the depth bar: {len(deep)}  ({100*len(deep)/len(pools):.0f}%)")
        new_to_us = set(pools) - baseline
        deep_new = set(deep) - baseline
        print(f"    NOT already in discovery      : {len(new_to_us)}")
        print(f"    NOT in discovery AND deep enough: {len(deep_new)}   <-- the number that matters")
        if baseline:
            overlap = len(set(pools) & baseline)
            print(f"    overlap with current discovery : {overlap} of {len(baseline)} baseline tokens")

        # --- control: does a recency feed help the APPROVED bottleneck?
        print(f"\n[5] CONTROL -- /networks/solana/new_pools")
        print("    Testing the claim that a recency feed would grow REJECTED, not APPROVED.")
        np_data = await _get(client, "new_pools", f"{GECKOTERMINAL_BASE}/api/v2/networks/solana/new_pools")
        if np_data is not None:
            np_rows = _pools_from(np_data)
            np_deep = [r for r in np_rows if tradeable_depth(r[2]) >= MIN_DEPTH_USD]
            print(f"    new pools returned    : {len(np_rows)}")
            if np_rows:
                print(f"    clearing the depth bar: {len(np_deep)}  ({100*len(np_deep)/len(np_rows):.0f}%)")
                med = sorted(tradeable_depth(r[2]) for r in np_rows)[len(np_rows)//2]
                print(f"    median tradeable depth: ${med:,.0f}")

        # --- verdict
        print("\n" + "=" * 72)
        print("READ THIS")
        print("=" * 72)
        print(f"  Approved tokens are stuck at 16-17. Each new token that clears the depth")
        print(f"  bar is a candidate to grow that cohort.")
        print(f"\n  Paginated volume-ranked pools would add {len(deep_new)} such tokens today.")
        if len(deep_new) >= 40:
            print("  -> Enough to roughly triple the approved cohort. Worth doing.")
        elif len(deep_new) >= 15:
            print("  -> A meaningful increase, though it may still fall short of 30+ approved.")
        else:
            print("  -> Too few to fix the bottleneck. The plateau is the Solana memecoin")
            print("     market itself, not this pipeline's sources -- and the honest answer")
            print("     is that the experiment cannot reach a verdict on liquid tokens alone.")
        print("\n  Nothing was changed by this probe.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
