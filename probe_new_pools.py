# probe_new_pools.py
"""Read-only probe for the new-pool feed. Changes nothing, writes nothing.

Answers two questions that were guessed at when the holding pen's defaults
were chosen, and guessed wrong:

  1. Does /new_pools paginate, and how deep? One page covers about seven
     seconds of launches, so the page count is what decides how much of the
     firehose the pen actually samples. If page 8 repeats page 1, raising
     DISCOVERY_NEW_POOL_PAGES buys nothing and the knob is a lie.

  2. What does the reserve distribution look like? The $5,000 floor was set
     on an assumed ~25% pass rate. Production reports 5%. This prints the
     real curve so the next choice is made from data.

Usage, inside the web container:
    docker compose exec web python probe_new_pools.py
    docker compose exec web python probe_new_pools.py 16     # deeper walk
"""
import sys
import time
import asyncio
from datetime import datetime, timezone

import httpx

import token_discovery as td

FLOORS = (0, 500, 1_000, 2_500, 5_000, 10_000, 25_000, 100_000)


async def main(pages: int) -> int:
    base = f"{td.GECKOTERMINAL_BASE}/api/v2/networks/solana/new_pools"
    now = datetime.now(timezone.utc)
    seen = {}            # mint -> (reserve, age_seconds)
    per_page = []
    first_page_mints = set()
    repeated_at = None

    async with httpx.AsyncClient() as client:
        for page in range(1, pages + 1):
            t0 = time.monotonic()
            try:
                r = await client.get(f"{base}?page={page}", headers=td.HEADERS, timeout=20.0)
                status = r.status_code
                rows = td._rows(r.json()) if status == 200 else []
            except Exception as e:
                print(f"  page {page:>2}: FAILED {type(e).__name__}: {e}")
                break

            page_mints, added = set(), 0
            oldest = newest = None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    tid = row["relationships"]["base_token"]["data"]["id"]
                except Exception:
                    continue
                if not isinstance(tid, str) or not tid.startswith("solana_"):
                    continue
                mint = tid.split("solana_", 1)[1]
                if not td._plausible_mint(mint):
                    continue
                attributes = row.get("attributes") or {}
                reserve = td._as_float(attributes.get("reserve_in_usd")) or 0.0
                age = td._pool_age_seconds(attributes, now)
                page_mints.add(mint)
                oldest = age if oldest is None else max(oldest, age)
                newest = age if newest is None else min(newest, age)
                if mint not in seen:
                    seen[mint] = (reserve, age)
                    added += 1

            if page == 1:
                first_page_mints = set(page_mints)
            elif page_mints and page_mints == first_page_mints and repeated_at is None:
                repeated_at = page

            per_page.append((page, status, len(rows), added, newest, oldest,
                             (time.monotonic() - t0) * 1000.0))
            print(f"  page {page:>2}: {status} rows={len(rows):>3} new={added:>3} "
                  f"age {newest or 0:>6.0f}-{oldest or 0:>6.0f}s  ({(time.monotonic()-t0)*1000:.0f}ms)")
            if not rows:
                print(f"  -> page {page} returned nothing; pagination ends here.")
                break
            if added == 0:
                print(f"  -> page {page} added no new mints; deeper pages are wasted calls.")
                break
            await asyncio.sleep(td.DISCOVERY_PAGE_SPACING_S)

    if not seen:
        print("\nNo pools parsed. The feed shape may have changed -- the pen would be "
              "silently filing nothing, and the refresh log would still look healthy.")
        return 1

    ages = sorted(a for _, a in seen.values())
    span = ages[-1] - ages[0] if len(ages) > 1 else 0.0
    productive_pages = sum(1 for entry in per_page if entry[2] > 0)
    print(f"\n{len(seen)} unique mints across {productive_pages} productive page(s), "
          f"spanning {span:.0f}s of launches ({span/60.0:.1f} min).")
    if repeated_at:
        print(f"WARNING: page {repeated_at} repeated page 1 exactly -- `page` is being "
              f"ignored. DISCOVERY_NEW_POOL_PAGES above 1 does nothing.")

    # A refresh happens every DISCOVERY_CACHE_TTL_S. Pools per second lets us
    # say what fraction of launches a given page count actually samples.
    rate = (len(seen) / span) if span > 0 else 0.0
    born = rate * td.DISCOVERY_CACHE_TTL_S
    print(f"Launch rate ~{rate:.2f} pools/s, so ~{born:.0f} are born between refreshes; "
          f"this walk saw {len(seen)} of them ({100.0*len(seen)/born if born else 0:.1f}%).")

    print(f"\n{'floor':>10} {'pass':>6} {'rate':>7}   {'filed/cycle':>11} {'filed/hour':>10}")
    print(f"{'-'*10} {'-'*6} {'-'*7}   {'-'*11} {'-'*10}")
    cycles_per_hour = 3600.0 / max(1.0, td.DISCOVERY_CACHE_TTL_S)
    # Only pages that actually returned rows count toward the per-page yield.
    # The empty page that ends the walk would otherwise divide the yield by a
    # page that contributed nothing, understating every projection below.
    productive = max(1, sum(1 for entry in per_page if entry[2] > 0))
    for floor in FLOORS:
        passing = sum(1 for reserve, _ in seen.values() if reserve >= floor)
        share = passing / len(seen)
        # Scale to the configured page count, not this probe's depth.
        per_cycle = share * (len(seen) / productive) * td.NEW_POOL_PAGES
        mark = "  <-- configured" if floor == int(td.NEW_POOL_MIN_RESERVE_USD) else ""
        print(f"${floor:>9,} {passing:>6} {100*share:>6.1f}%   {per_cycle:>11.1f} "
              f"{per_cycle*cycles_per_hour:>10.0f}{mark}")

    reserves = sorted(reserve for reserve, _ in seen.values())
    def pct(p):
        return reserves[min(len(reserves) - 1, int(p * len(reserves)))]
    print(f"\nreserve percentiles: p50 ${pct(0.50):,.0f}  p75 ${pct(0.75):,.0f}  "
          f"p90 ${pct(0.90):,.0f}  p99 ${pct(0.99):,.0f}  max ${reserves[-1]:,.0f}")
    print("\nfiled/hour is an upper bound: a promoted mint still has to be drawn "
          "at random from the whole candidate list before its retention window "
          "expires, and the pipeline manages only ~190 evaluations an hour.")
    return 0


if __name__ == "__main__":
    depth = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    print(f"Walking up to {depth} pages of {td.GECKOTERMINAL_BASE}"
          f"/api/v2/networks/solana/new_pools\n")
    raise SystemExit(asyncio.run(main(depth)))
