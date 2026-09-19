#!/usr/bin/env python3
"""
Field discovery for the free, no-key providers.

WHY THIS EXISTS
The accessibility test proved DexScreener, RugCheck, Jupiter and
GeckoTerminal are all reachable from your machine without an API key. The
open question is how much of the pipeline's data contract they can
actually fill between them -- because if the answer is "all of it", a paid
provider is an optimisation rather than a prerequisite.

Rather than read four sets of docs and guess (which is exactly how the
GMGN integration went wrong), this fetches real responses for a real
token and reports, field by field, what is genuinely present.

It probes a CURRENTLY TRENDING memecoin rather than a blue chip, because
rug and insider fields are empty and uninformative on something like
wrapped SOL -- the token is discovered from RugCheck's own free trending
endpoint, so nothing is hardcoded.

    python explore_free_providers.py
    python explore_free_providers.py --token <mint address>
    python explore_free_providers.py --save    # write raw JSON for inspection

Nothing here needs a key, writes anything, or touches a wallet.
"""
import os
import sys
import json
import argparse

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(2)

UA = "memecoin-trading-agent/1.0"
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

# The contract every provider adapter must fill -- see
# market_data.fetch_full_snapshot(). This is what we're shopping for.
CONTRACT = [
    ("current_price", "Price in USD"),
    ("pool_liquidity_usd", "Pool liquidity in USD"),
    ("volume_h1 / volume_h24", "Volume, 1h and 24h"),
    ("top_10_holder_percentage", "Top-10 holder concentration"),
    ("estimated_slippage_percent", "Slippage estimate"),
    ("social_volume_score", "Attention / hype"),
    ("rug/security signals", "Mint+freeze authority, LP burn, insiders (NEW - we never had these)"),
]

found = {}


def get(client, name, url, **kw):
    try:
        r = client.get(url, headers=HEADERS, timeout=25.0, **kw)
        if r.status_code != 200:
            print(f"    ! {name}: HTTP {r.status_code} {r.text[:120]}")
            return None
        return r.json()
    except Exception as e:
        print(f"    ! {name}: {e}")
        return None


def show(label, value, note=""):
    """Print a field only when it's genuinely present. An absent field and
    a zero field are different things and we care about the difference."""
    if value is None or value == "" or value == []:
        print(f"      {label:<34} -- (absent)")
        return False
    suffix = f"   {note}" if note else ""
    text = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
    print(f"      {label:<34} {text[:90]}{suffix}")
    return True


def show_authority(label, report, key):
    """Mint/freeze authority: null means renounced (safe). Absence of the
    KEY means RugCheck didn't report it. Those are different and must not
    render the same way."""
    if key not in report:
        print(f"      {label:<34} -- (field not returned)")
        return False
    value = report.get(key)
    if value in (None, ""):
        print(f"      {label:<34} null  -> RENOUNCED (safe)")
    else:
        print(f"      {label:<34} {str(value)[:60]}  -> STILL HELD (risk)")
    return True


def _rows(data):
    """Trending endpoints disagree about whether the list is the top-level
    response or nested under a key. Accept either rather than silently
    finding nothing (which is what happened on the first run)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "result", "results", "tokens", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def pick_token(client, debug=False):
    """Find a real, currently-traded memecoin to probe with.

    Blue chips are useless here: RugCheck returned an essentially empty
    report for wrapped SOL (price 0, no holders, insiders endpoint
    rejecting the mint outright), which tells us nothing about what it
    holds for the tokens we actually care about. Three independent
    sources are tried so one bad response shape can't defeat discovery.
    """
    print("\nDiscovering a live memecoin to probe...")

    # 1. RugCheck's own trending list.
    data = get(client, "rugcheck trending", "https://api.rugcheck.xyz/v1/stats/trending")
    if debug and data is not None:
        print(f"    [debug] rugcheck trending: {json.dumps(data)[:300]}")
    for row in _rows(data):
        if isinstance(row, dict):
            mint = row.get("mint") or row.get("address") or row.get("tokenAddress")
            if mint:
                print(f"  Using {mint}  (source: RugCheck trending)")
                return mint

    # 2. DexScreener boosts -- tokens someone paid to promote, so they are
    #    real, live and usually recent.
    data = get(client, "dexscreener boosts", "https://api.dexscreener.com/token-boosts/top/v1")
    if debug and data is not None:
        print(f"    [debug] dexscreener boosts: {json.dumps(data)[:300]}")
    for row in _rows(data):
        if isinstance(row, dict) and row.get("chainId") == "solana" and row.get("tokenAddress"):
            print(f"  Using {row['tokenAddress']}  (source: DexScreener boosts)")
            return row["tokenAddress"]

    # 3. GeckoTerminal trending pools -- JSON:API shaped, so the mint is
    #    embedded in a relationship id like "solana_<mint>".
    data = get(client, "geckoterminal trending",
               "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools")
    for row in _rows(data):
        try:
            tid = row["relationships"]["base_token"]["data"]["id"]
            if tid.startswith("solana_"):
                mint = tid.split("solana_", 1)[1]
                print(f"  Using {mint}  (source: GeckoTerminal trending pools)")
                return mint
        except Exception:
            continue

    print("  All three discovery sources failed. Re-run with --token <mint address>")
    print("  using any memecoin from dexscreener.com/solana.")
    return None


def probe_rugcheck(client, token, save):
    print("\n" + "=" * 72)
    print("RUGCHECK  (free, no key)  -- rug/security, the gap we've never filled")
    print("=" * 72)
    rep = get(client, "report", f"https://api.rugcheck.xyz/v1/tokens/{token}/report")
    if not rep:
        return
    if save:
        open("raw_rugcheck.json", "w").write(json.dumps(rep, indent=2))
        print("    (raw saved to raw_rugcheck.json)")

    print("\n    Security / rug:")
    found["rug"] = any([
        show("score_normalised", rep.get("score_normalised"), "0-100, higher = riskier"),
        show("rugged", rep.get("rugged")),
        show("risks[]", [r.get("name") for r in (rep.get("risks") or [])]),
        # These two are the exception to the absent/present rule above:
        # null is a MEANINGFUL value here (the authority was renounced,
        # which is the safe state), not missing data. Rendering it as
        # "(absent)" would invert the meaning of the most important
        # safety field on the token.
        show_authority("mintAuthority", rep, "mintAuthority"),
        show_authority("freezeAuthority", rep, "freezeAuthority"),
        show("lpLockedPct", rep.get("lpLockedPct")),
    ])

    print("\n    Holder concentration:")
    holders = rep.get("topHolders") or []
    if holders:
        top10 = sum(float(h.get("pct") or 0) for h in holders[:10])
        show("topHolders[] count", len(holders))
        show("computed top-10 %", round(top10, 2), "<-- replaces our Solana RPC call")
        found["holders"] = True
    else:
        show("topHolders[]", None)

    print("\n    Market data (bonus if present):")
    show("price", rep.get("price"))
    show("totalMarketLiquidity", rep.get("totalMarketLiquidity"))
    show("totalHolders", rep.get("totalHolders"))

    # Is this report real, or a stub for a token RugCheck doesn't track?
    # A report with no holders, no price and no liquidity is the latter,
    # and drawing "RugCheck can't do holder concentration" from it would
    # be a false conclusion about the provider.
    indexed = bool(holders) or bool(rep.get("totalHolders")) or bool(rep.get("price"))
    found["rugcheck_indexed"] = indexed
    if not indexed:
        print("\n    !! This token is NOT meaningfully indexed by RugCheck -- empty")
        print("       report (no holders, no price, no liquidity). Nothing about")
        print("       RugCheck's real coverage can be concluded from this run.")
        print("       Re-run against a live memecoin.")

    print("\n    Insider analysis:")
    ins = get(client, "insiders", f"https://api.rugcheck.xyz/v1/tokens/{token}/insiders/graph")
    if isinstance(ins, list):
        show("insider networks", len(ins), "networks of linked wallets")
        found["insiders"] = bool(ins)
    else:
        show("insiders/graph", None)


def probe_jupiter(client, token, save):
    print("\n" + "=" * 72)
    print("JUPITER  (free, no key, ~1 req/sec)")
    print("=" * 72)
    data = get(client, "price", f"https://api.jup.ag/price/v3?ids={token}")
    if not data:
        return
    row = data.get(token) or {}
    if save:
        open("raw_jupiter.json", "w").write(json.dumps(data, indent=2))
    show("usdPrice", row.get("usdPrice"))
    show("liquidity", row.get("liquidity"))
    show("createdAt", row.get("createdAt"), "<-- token age, a gate input we don't have yet")
    show("priceChange24h", row.get("priceChange24h") or row.get("usdPrice24hChange"))
    for k in row:
        if k not in ("usdPrice", "liquidity", "createdAt", "priceChange24h", "usdPrice24hChange"):
            show(f"(also) {k}", row[k])


def probe_geckoterminal(client, token, save):
    print("\n" + "=" * 72)
    print("GECKOTERMINAL  (free, no key)")
    print("=" * 72)
    data = get(client, "token", f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{token}")
    if not data:
        return
    attrs = ((data.get("data") or {}).get("attributes")) or {}
    if save:
        open("raw_geckoterminal.json", "w").write(json.dumps(data, indent=2))
    show("price_usd", attrs.get("price_usd"))
    show("total_reserve_in_usd", attrs.get("total_reserve_in_usd"), "liquidity proxy")
    show("volume_usd", attrs.get("volume_usd"))
    show("market_cap_usd", attrs.get("market_cap_usd"))
    show("fdv_usd", attrs.get("fdv_usd"))


def probe_dexscreener(client, token, save):
    print("\n" + "=" * 72)
    print("DEXSCREENER  (free, no key -- what we already use)")
    print("=" * 72)
    data = get(client, "pairs", f"https://api.dexscreener.com/token-pairs/v1/solana/{token}")
    if not data:
        return
    if not isinstance(data, list) or not data:
        print("      (no indexed pair)")
        return
    best = max(data, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    if save:
        open("raw_dexscreener.json", "w").write(json.dumps(data, indent=2))
    show("priceUsd", best.get("priceUsd"))
    show("liquidity.usd", (best.get("liquidity") or {}).get("usd"))
    show("volume.h1 / h24", {k: (best.get("volume") or {}).get(k) for k in ("h1", "h24")})
    show("baseToken.symbol", (best.get("baseToken") or {}).get("symbol"))


def summarize():
    print("\n" + "=" * 72)
    print("WHAT THE FREE SOURCES COVER")
    print("=" * 72)

    if found.get("rugcheck_indexed") is False:
        print("\n  RUN INVALID for the RugCheck questions. The probed token isn't")
        print("  indexed there, so its empty response says nothing about whether")
        print("  RugCheck provides holder concentration or insider data. Re-run")
        print("  against a live memecoin before treating any verdict below as real.\n")
    verdict = {
        "current_price": "DexScreener + Jupiter + GeckoTerminal (three independent sources)",
        "pool_liquidity_usd": "DexScreener + Jupiter + GeckoTerminal",
        "volume_h1 / volume_h24": "DexScreener",
        "estimated_slippage_percent": "Jupiter quote endpoint (already integrated)",
        "top_10_holder_percentage": "RugCheck topHolders[] -- IF present above" if found.get("holders")
                                      else "NOT covered free -- still needs Solana RPC or a paid provider",
        "rug/security signals": "RugCheck -- NEW capability, we never had this" if found.get("rug")
                                  else "check output above",
        "social_volume_score": "NOT covered by any free source -- still the real gap",
    }
    for field, who in verdict.items():
        print(f"  {field:<30} {who}")
    print("\n  Bottom line: if holder concentration came back populated, the free")
    print("  stack covers everything except hype -- and adds rug/insider signals")
    print("  we never had. A paid provider then buys convenience and one metric,")
    print("  not viability. Decide after seeing the output above.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token")
    ap.add_argument("--save", action="store_true", help="write raw JSON responses to files")
    ap.add_argument("--debug", action="store_true", help="print raw trending responses if discovery fails")
    args = ap.parse_args()

    print("FREE PROVIDER FIELD DISCOVERY")
    with httpx.Client(follow_redirects=True) as client:
        token = args.token or pick_token(client, debug=args.debug)
        if not token:
            return 1
        probe_rugcheck(client, token, args.save)
        probe_jupiter(client, token, args.save)
        probe_dexscreener(client, token, args.save)
        probe_geckoterminal(client, token, args.save)
    summarize()
    return 0


if __name__ == "__main__":
    sys.exit(main())
