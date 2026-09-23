"""Which endpoint can actually supply "15-90 minutes old, with liquidity"?

    docker compose exec web python probe_recency_sources.py

WHY

Jupiter's /tokens/v2/recent cannot. Measured: 30 rows spanning 0.1 to 0.7
minutes of age -- the page reaches back ONE minute. The evaluation window
starts at 15 minutes, for the good reason that a token seconds old has no
price at any provider, so 0/30 rows qualify and always will. limit and offset
are ignored; the page is 30 rows whatever you ask for. Lowering the liquidity
floor cannot fix this: the binding constraint is age.

Birdeye did the join for us -- filter by liquidity server-side, sort by
listing time, and one page reached back ten hours. Something else has to do
that now. The candidates:

  GeckoTerminal /new_pools   pools sorted by creation, with reserve_in_usd.
                             Same semantics as Birdeye, already a configured
                             and throttled provider here.
  Jupiter v2 short periods   toptraded/1h and toporganicscore/1h surface
                             tokens that are young AND already active, which
                             is a different selection but may hit the same
                             population.
  datapi pool endpoints      what jup.ag's own UI is backed by.

For each, this reports what the rows ACTUALLY contain and how many land
inside the window with real liquidity. It prints the attribute keys rather
than assuming them, because the field names are the part worth not guessing.

Changes nothing.
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
FLOOR = float(os.environ.get("JUPITER_NEW_LISTING_MIN_LIQUIDITY_USD", "8000"))


def get(url):
    try:
        with httpx.Client(timeout=25.0, follow_redirects=True) as c:
            r = c.get(url, headers={"Accept": "application/json"})
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:120]}"
        return r.json(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def minutes_since(value):
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


def summarise(label, pairs, note=""):
    """pairs = [(age_minutes|None, liquidity|None)]"""
    if not pairs:
        print(f"  {label:<34} no rows  {note}")
        return
    ages = sorted(a for a, _ in pairs if a is not None)
    liqs = sorted(l for _, l in pairs if l is not None)
    in_window = sum(1 for a, _ in pairs if a is not None and MIN_AGE <= a <= MAX_AGE)
    qualified = sum(1 for a, l in pairs
                    if a is not None and l is not None
                    and MIN_AGE <= a <= MAX_AGE and l >= FLOOR)
    print(f"  {label:<34} {len(pairs):>4} rows | "
          f"age {(f'{ages[0]:.0f}-{ages[-1]:.0f}m' if ages else 'n/a'):>12} | "
          f"median liq {(f'${statistics.median(liqs):,.0f}' if liqs else 'n/a'):>12} | "
          f"in window {in_window:>3} | QUALIFY {qualified:>3}  {note}")


def keys_of(row, path=""):
    if isinstance(row, dict):
        return sorted(row)
    return type(row).__name__


def main():
    print("=" * 118)
    print(f"RECENCY SOURCES -- need age {MIN_AGE:.0f}-{MAX_AGE:.0f} min AND liquidity >= ${FLOOR:,.0f}")
    print("QUALIFY is the only column that matters: rows this source could actually hand to the pipeline.")
    print("=" * 118)

    print("\nGeckoTerminal new_pools -- pools by creation time, with reserves")
    seen_keys = False
    for page in (1, 2, 3):
        data, err = get(f"{GT}/api/v2/networks/solana/new_pools?page={page}")
        if err:
            print(f"  page {page:<29} {err}")
            continue
        rows = (data or {}).get("data") or []
        if rows and not seen_keys:
            attrs = (rows[0].get("attributes") or {})
            print(f"  attribute keys: {sorted(attrs)}")
            seen_keys = True
        pairs = []
        for r in rows:
            a = r.get("attributes") or {}
            age = None
            for field in ("pool_created_at", "created_at"):
                age = minutes_since(a.get(field))
                if age is not None:
                    break
            liq = a.get("reserve_in_usd")
            try:
                liq = float(liq) if liq is not None else None
            except (TypeError, ValueError):
                liq = None
            pairs.append((age, liq))
        summarise(f"new_pools page {page}", pairs)

    print("\nJupiter /tokens/v2 -- shorter periods than 24h")
    for path in ("toptraded/1h", "toptraded/6h", "toporganicscore/5m",
                 "toporganicscore/1h", "toporganicscore/6h"):
        data, err = get(f"{JUP}/tokens/v2/{path}")
        if err:
            print(f"  {path:<34} {err}")
            continue
        rows = data if isinstance(data, list) else []
        pairs = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            age = minutes_since((r.get("firstPool") or {}).get("createdAt")) \
                or minutes_since(r.get("createdAt"))
            liq = r.get("liquidity")
            liq = float(liq) if isinstance(liq, (int, float)) else None
            pairs.append((age, liq))
        summarise(path, pairs)

    print("\ndatapi -- what jup.ag's own UI is backed by")
    for path in ("v1/pools/recent", "v1/pools/new", "v1/pools/toptrending/5m",
                 "v1/pools/toptrending/1h", "v1/assets/recent"):
        data, err = get(f"{DATAPI}/{path}")
        if err:
            print(f"  {path:<34} {err}")
            continue
        rows = data if isinstance(data, list) else (
            (data or {}).get("pools") or (data or {}).get("data") or [])
        pairs = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            age = minutes_since(r.get("createdAt"))
            liq = r.get("liquidity")
            liq = float(liq) if isinstance(liq, (int, float)) else None
            pairs.append((age, liq))
        note = f"keys={keys_of(rows[0])}" if rows else ""
        summarise(path, pairs, note)

    print("\n" + "=" * 118)
    print("Read the QUALIFY column. A source with zero cannot supply the recency")
    print("population no matter how the floors are tuned -- that was the mistake")
    print("/tokens/v2/recent hid behind a plausible-looking 200 OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
