"""Why is jupiter-recent contributing zero?

    docker compose exec web python probe_jupiter_recent.py

THE PROBLEM

Live discovery reports jupiter-recent=0 while jupiter-organic=49 and
jupiter-toptraded=50. Recency is the source the experiment is actually built
around -- newly listed tokens are the population -- and it is the only one
returning nothing.

THE LIKELY CAUSE, WHICH THIS CHECKS RATHER THAN ASSUMES

Birdeye and Jupiter mean different things by "recent". Birdeye's
/defi/v3/token/list applied min_liquidity=25000 SERVER-side and then sorted by
listing time, so one page of 100 rows was "the 100 most recent tokens that
already have $25k of liquidity" -- which measured back about ten hours.

Jupiter's /tokens/v2/recent returns the 30 most recently created tokens,
unfiltered. Applying a $25,000 floor to the 30 newest mints on Solana may
well match nothing at all, because a token minutes old normally has a few
thousand dollars in its pool. If so, the floor and the window are not wrong
individually -- they are being applied to a list that was never filtered to
begin with, and the two sources are not interchangeable the way the code now
assumes.

This prints the actual age and liquidity of every row so the answer is read
off data instead of reasoned about. It also tries whether the endpoint honours
a limit parameter, since a deeper page is the cheapest fix if it does.

Changes nothing.
"""
import json
import os
import sys
from datetime import datetime, timezone

# httpx, NOT urllib. The pipeline reaches this exact URL successfully with
# httpx every three minutes; the first version of this probe used urllib and
# got a 403, which looked like the endpoint being gone. Guessing at which
# header placates the edge is the wrong game -- using the client that is
# already known to work removes the variable entirely.
import httpx

BASE = os.environ.get("JUPITER_TOKENS_API_BASE", "https://lite-api.jup.ag")
FLOOR = float(os.environ.get("JUPITER_MIN_LIQUIDITY_USD", "25000"))
MIN_AGE = float(os.environ.get("JUPITER_MIN_AGE_MINUTES", "15"))
MAX_AGE = float(os.environ.get("JUPITER_MAX_AGE_MINUTES", "90"))


def get(url):
    try:
        with httpx.Client(timeout=25.0, follow_redirects=True) as client:
            resp = client.get(url, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}: {resp.text[:160]}"
        return resp.json(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def age_minutes(row):
    for value in ((row.get("firstPool") or {}).get("createdAt"), row.get("createdAt")):
        if isinstance(value, (int, float)) and value > 0:
            epoch = float(value) / 1000.0 if value > 1e11 else float(value)
            return (datetime.now(timezone.utc).timestamp() - epoch) / 60.0
        if isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - parsed).total_seconds() / 60.0
    return None


def main():
    print("=" * 92)
    print(f"JUPITER /tokens/v2/recent -- floor ${FLOOR:,.0f}, window {MIN_AGE:.0f}-{MAX_AGE:.0f} min")
    print("=" * 92)

    rows, err = get(f"{BASE}/tokens/v2/recent")
    if err or not isinstance(rows, list):
        print(f"could not read the endpoint: {err}")
        return 1
    print(f"{len(rows)} rows returned\n")
    print(f"{'symbol':<14} {'age(min)':>9} {'liquidity':>14} {'mcap':>14} "
          f"{'holders':>8} {'launchpad':<12} verdict")
    print("-" * 92)

    liquid = in_window = both = undated = 0
    ages, liqs = [], []
    for row in rows:
        age = age_minutes(row)
        liq = row.get("liquidity")
        liq = float(liq) if isinstance(liq, (int, float)) else None
        if age is None:
            undated += 1
        else:
            ages.append(age)
        if liq is not None:
            liqs.append(liq)
        ok_liq = liq is not None and liq >= FLOOR
        ok_age = age is not None and MIN_AGE <= age <= MAX_AGE
        liquid += ok_liq
        in_window += ok_age
        both += (ok_liq and ok_age)
        why = ("ADMITTED" if (ok_liq and ok_age) else
               ", ".join(filter(None, [
                   None if ok_liq else ("no liquidity figure" if liq is None
                                        else f"below floor"),
                   None if ok_age else ("no date" if age is None
                                        else ("too new" if age < MIN_AGE else "too old"))])))
        print(f"{str(row.get('symbol'))[:14]:<14} "
              f"{(f'{age:.1f}' if age is not None else '?'):>9} "
              f"{(f'${liq:,.0f}' if liq is not None else '?'):>14} "
              f"{(f'${row.get(chr(109)+chr(99)+chr(97)+chr(112)):,.0f}' if isinstance(row.get('mcap'), (int, float)) else '?'):>14} "
              f"{str(row.get('holderCount') or '?'):>8} "
              f"{str(row.get('launchpad') or '-')[:12]:<12} {why}")

    print("\n" + "=" * 92)
    print(f"  pass the ${FLOOR:,.0f} liquidity floor : {liquid}/{len(rows)}")
    print(f"  inside the {MIN_AGE:.0f}-{MAX_AGE:.0f} min window : {in_window}/{len(rows)}")
    print(f"  pass BOTH (what discovery admits) : {both}/{len(rows)}")
    if undated:
        print(f"  undated rows                      : {undated}")
    if ages:
        ages.sort()
        print(f"\n  age spread: newest {ages[0]:.1f} min, median {ages[len(ages)//2]:.1f} min, "
              f"oldest {ages[-1]:.1f} min")
        print(f"  -- the page reaches back {ages[-1]:.0f} minutes. The window needs "
              f"{MAX_AGE:.0f}.")
    if liqs:
        liqs.sort()
        print(f"  liquidity spread: min ${liqs[0]:,.0f}, median ${liqs[len(liqs)//2]:,.0f}, "
              f"max ${liqs[-1]:,.0f}")
        over = sum(1 for v in liqs if v >= FLOOR)
        print(f"  -- {over}/{len(liqs)} of these newly listed tokens have ${FLOOR:,.0f}.")

    print("\n" + "=" * 92)
    print("Does the endpoint take a limit? A deeper page is the cheapest fix if so.")
    print("=" * 92)
    for suffix in ("?limit=100", "?limit=250", "?offset=30", "?limit=100&offset=0"):
        data, err = get(f"{BASE}/tokens/v2/recent{suffix}")
        n = len(data) if isinstance(data, list) else "not a list"
        print(f"  /tokens/v2/recent{suffix:<22} -> {err or n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
