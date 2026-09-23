"""Pick the discovery source and the liquidity floors from measurement.

    docker compose exec web python calibrate_discovery.py

WHAT THIS DECIDES, AND ON WHAT BASIS

Two questions have been answered by guessing today, badly. This answers both
from live data.

  1. WHICH SOURCE can supply "15-90 minutes old, with real liquidity"?
     Jupiter's /tokens/v2/recent cannot: its 30 rows span forty seconds, so
     nothing in it has ever been old enough to evaluate. Birdeye did the join
     (filter by liquidity, then sort by listing time) and its quota is spent.
     Something has to do that join.

  2. WHAT FLOOR? Not a matter of taste. The floor sets the RATE at which
     candidates enter the experiment, and the rate is what decides whether
     ~144 approved tokens per arm arrives in days or weeks. So the floor is
     chosen as: the highest floor that still yields the target rate. Highest,
     because every dollar of floor buys sample quality -- a deeper pool means
     the quoted price is real and a position could actually fill -- and we
     want the most quality the rate budget affords, not the least.

  Both floors stay inside the frame bounds, which are not negotiable:
  above ~$40,000 every token offered already clears B_SENTINEL, approval
  becomes a tautology and the rejected control arm vanishes; below ~$5,000
  the "price" is one trade old and the slot is spent on noise.

Changes nothing. Prints a recommendation and the exact line to apply it.
"""
import os
import statistics
import sys
from datetime import datetime, timezone

import httpx

GT = os.environ.get("GECKOTERMINAL_API_BASE", "https://api.geckoterminal.com")
JUP = os.environ.get("JUPITER_TOKENS_API_BASE", "https://lite-api.jup.ag")
DATAPI = "https://datapi.jup.ag"

MIN_AGE = float(os.environ.get("JUPITER_MIN_AGE_MINUTES", "15"))
MAX_AGE = float(os.environ.get("JUPITER_MAX_AGE_MINUTES", "90"))
FLOOR_MIN = float(os.environ.get("DISCOVERY_FLOOR_MIN_USD", "5000"))
FLOOR_MAX = 38000.0

# Birdeye's recency source measured ~10 qualifying listings an hour and that
# was the experiment's whole intake. Aim a little above it: at ~20/hour the
# evaluation slots stay busy without the re-entry cooldown throttling us.
TARGET_PER_HOUR = float(os.environ.get("CALIB_TARGET_PER_HOUR", "20"))


def get(url):
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as c:
            r = c.get(url, headers={"Accept": "application/json"})
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:100]}"
        return r.json(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def age_min(value):
    if isinstance(value, (int, float)) and value > 0:
        epoch = float(value) / 1000.0 if value > 1e11 else float(value)
        return (datetime.now(timezone.utc).timestamp() - epoch) / 60.0
    if isinstance(value, str) and value.strip():
        try:
            p = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if p.tzinfo is None:
            p = p.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - p).total_seconds() / 60.0
    return None


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


# --- sources: each returns [(mint, age_minutes, liquidity_usd)] -------------

def src_gt_new_pools(pages=3):
    out = []
    for page in range(1, pages + 1):
        data, err = get(f"{GT}/api/v2/networks/solana/new_pools?page={page}")
        if err:
            if page == 1:
                return out, err
            break
        for row in (data or {}).get("data") or []:
            a = row.get("attributes") or {}
            rel = ((row.get("relationships") or {}).get("base_token") or {}).get("data") or {}
            mint = str(rel.get("id") or "").replace("solana_", "")
            age = age_min(a.get("pool_created_at")) or age_min(a.get("created_at"))
            out.append((mint, age, num(a.get("reserve_in_usd"))))
    return out, None


def src_jup(path):
    data, err = get(f"{JUP}/tokens/v2/{path}")
    if err:
        return [], err
    out = []
    for r in data if isinstance(data, list) else []:
        if not isinstance(r, dict):
            continue
        age = age_min((r.get("firstPool") or {}).get("createdAt"))
        if age is None:
            age = age_min(r.get("createdAt"))
        out.append((r.get("id"), age, num(r.get("liquidity"))))
    return out, None


def src_datapi(path):
    data, err = get(f"{DATAPI}/{path}")
    if err:
        return [], err
    rows = data if isinstance(data, list) else (
        (data or {}).get("pools") or (data or {}).get("data") or [])
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        base = r.get("baseAsset") if isinstance(r.get("baseAsset"), dict) else {}
        out.append((base.get("id") or r.get("id"), age_min(r.get("createdAt")),
                    num(r.get("liquidity"))))
    return out, None


SOURCES = [
    ("geckoterminal new_pools", lambda: src_gt_new_pools(3)),
    ("jupiter toptraded/1h", lambda: src_jup("toptraded/1h")),
    ("jupiter toptraded/6h", lambda: src_jup("toptraded/6h")),
    ("jupiter toporganic/5m", lambda: src_jup("toporganicscore/5m")),
    ("jupiter toporganic/1h", lambda: src_jup("toporganicscore/1h")),
    ("jupiter recent", lambda: src_jup("recent")),
    ("datapi pools/recent", lambda: src_datapi("v1/pools/recent")),
    ("datapi pools/toptrending/1h", lambda: src_datapi("v1/pools/toptrending/1h")),
]


def main():
    print("=" * 112)
    print(f"DISCOVERY CALIBRATION -- window {MIN_AGE:.0f}-{MAX_AGE:.0f} min, "
          f"floor bounds ${FLOOR_MIN:,.0f}-${FLOOR_MAX:,.0f}, target {TARGET_PER_HOUR:.0f} qualifying/hour")
    print("=" * 112)
    print(f"{'source':<30} {'rows':>5} {'dated':>6} {'age range':>16} "
          f"{'in window':>10} {'median liq':>12}  note")
    print("-" * 112)

    best = None
    for label, fetch in SOURCES:
        try:
            rows, err = fetch()
        except Exception as e:
            rows, err = [], f"{type(e).__name__}: {e}"
        if err and not rows:
            print(f"{label:<30} {'-':>5} {'-':>6} {'-':>16} {'-':>10} {'-':>12}  {err}")
            continue
        dated = [r for r in rows if r[1] is not None]
        ages = sorted(r[1] for r in dated)
        window = [r for r in dated if MIN_AGE <= r[1] <= MAX_AGE and r[2] is not None]
        liqs = sorted(r[2] for r in window)
        print(f"{label:<30} {len(rows):>5} {len(dated):>6} "
              f"{(f'{ages[0]:.0f}-{ages[-1]:.0f}m' if ages else 'n/a'):>16} "
              f"{len(window):>10} "
              f"{(f'${statistics.median(liqs):,.0f}' if liqs else 'n/a'):>12}  "
              f"{'<-- covers the window' if len(window) >= 5 else ''}")
        if len(window) >= 5 and (best is None or len(window) > len(best[1])):
            best = (label, window, ages)

    if best is None:
        print("\nNo source put five or more tokens inside the window.")
        print("Without one, the recency arm cannot be supplied at any floor, and the")
        print("experiment runs on breadth sources only -- which is a different and")
        print("older population than the one it was designed around. Say so in the")
        print("results rather than reporting it as a memecoin study.")
        return 1

    label, window, ages = best
    liqs = sorted(r[2] for r in window)
    span_min = (max(ages) - min(ages)) or 1.0
    # Rows the source shows per hour, from the age span it actually covers.
    per_hour_total = len(window) * (60.0 / min(span_min, MAX_AGE - MIN_AGE or 1.0))

    print("\n" + "=" * 112)
    print(f"BEST SOURCE: {label}")
    print(f"  {len(window)} tokens inside the window, spanning {span_min:.0f} minutes "
          f"-- roughly {per_hour_total:.0f} qualifying tokens/hour before any floor")
    print("=" * 112)

    print(f"\n{'floor':>10} {'survive':>9} {'per hour':>10}   what that buys")
    print("-" * 112)
    recommended = None
    for floor in (5000, 7500, 10000, 12500, 15000, 20000, 25000, 30000, 38000):
        if floor < FLOOR_MIN or floor > FLOOR_MAX:
            continue
        kept = [v for v in liqs if v >= floor]
        rate = per_hour_total * (len(kept) / len(liqs)) if liqs else 0.0
        mark = ""
        if rate >= TARGET_PER_HOUR:
            recommended = floor          # highest floor still meeting the target
            mark = "meets target"
        print(f"${floor:>9,} {len(kept):>9} {rate:>10.1f}   {mark}")

    print("\n" + "=" * 112)
    if recommended is None:
        print(f"NO floor in range reaches {TARGET_PER_HOUR:.0f}/hour. Take the minimum")
        print(f"(${FLOOR_MIN:,.0f}) and accept a slower fill, or widen the age window --")
        print("do NOT drop below the minimum: the sample stops being tradeable and")
        print("the slots are spent on tokens whose price is one trade old.")
        recommended = FLOOR_MIN
    print(f"RECOMMENDED new-listing floor: ${recommended:,}")
    print(f"  Chosen as the HIGHEST floor still meeting {TARGET_PER_HOUR:.0f}/hour. Every")
    print(f"  dollar of floor buys sample quality; this spends the rate budget and")
    print(f"  nothing more.")
    print(f"\nRECOMMENDED general floor: $25,000  (unchanged -- it is what Birdeye ran,")
    print(f"  it sits comfortably under the ${FLOOR_MAX + 2000:,.0f} tautology line, and the breadth")
    print(f"  sources are already producing ~100 candidates a refresh at it)")
    print(f"\nApply with:")
    print(f"  docker compose exec -T db psql -U postgres -d memecoin_trading -c \\")
    print(f"    \"UPDATE app_settings SET discovery_min_liquidity_usd = 25000, \"\\")
    print(f"    \"discovery_new_listing_min_liquidity_usd = {recommended} WHERE id = 1;\"")
    print("\nThe running container picks this up on its next refresh -- the floor change")
    print("invalidates the discovery cache, so there is nothing to restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
