#!/usr/bin/env python3
"""
Provider accessibility test -- run this BEFORE integrating or paying for
any market-data provider.

WHY THIS EXISTS
We integrated GMGN on the strength of its data quality without ever
checking whether a plain server-side Python client could reach it. It
cannot: Cloudflare bot management refuses httpx with a 403 challenge page
before the request reaches their API, and no combination of headers fixes
a TLS-fingerprint rejection. That cost several rounds of debugging that a
sixty-second test would have prevented.

So: this script asks one question of each candidate provider -- "does a
plain httpx request from THIS machine, on THIS IP, get a usable response?"
-- and answers it before any signup, subscription or integration work.

Most of the providers below need no API key at all, so you can run this
right now and learn which ones are viable from your network. The two that
do need keys (Solana Tracker, Birdeye) are still probed for reachability,
and will report a clean auth error rather than a Cloudflare block if the
host is usable -- an auth error is GOOD NEWS here: it means you got
through to the API and only need a key.

    python test_provider_access.py
    python test_provider_access.py --token <mint address>

Optional keys, picked up from .env or the environment if present:
    SOLANATRACKER_API_KEY, BIRDEYE_API_KEY
"""
import os
import re
import sys
import json
import argparse

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(2)


def load_dotenv():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v:
                os.environ.setdefault(k, v)


load_dotenv()

# Wrapped SOL. Used ONLY as a probe target to prove connectivity -- it is
# not a trading candidate and nothing here evaluates it as one. Override
# with --token if you'd rather probe with something else.
PROBE_TOKEN = "So11111111111111111111111111111111111111112"

UA = "memecoin-trading-agent/1.0"

REACHABLE, BLOCKED, NEEDS_KEY, ERROR = "REACHABLE", "BLOCKED", "NEEDS KEY", "ERROR"
results = []


def classify(resp) -> tuple:
    """Distinguish 'the API answered' from 'a bot wall answered'. This is
    the whole point of the script: a 403 of HTML is a completely different
    problem from a 401 of JSON."""
    ctype = resp.headers.get("content-type", "").lower()
    body_head = resp.text[:300].lstrip().lower()
    is_html = "html" in ctype or body_head.startswith("<!doctype html") or body_head.startswith("<html")
    server = resp.headers.get("server", "").lower()

    if is_html and resp.status_code in (403, 503, 429):
        marker = ""
        m = re.search(r'cf-error-code[^>]*>\s*(\d{4})|[Ee]rror\s*(\d{4})', resp.text)
        if m:
            marker = f" (Cloudflare error {m.group(1) or m.group(2)})"
        elif "attention required" in resp.text.lower() or "just a moment" in resp.text.lower():
            marker = " (Cloudflare bot challenge)"
        elif "cloudflare" in server:
            marker = " (Cloudflare)"
        return BLOCKED, f"HTTP {resp.status_code}, HTML block page{marker}"

    if resp.status_code in (401, 403):
        return NEEDS_KEY, f"HTTP {resp.status_code} -- API answered, credentials needed/invalid"

    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            return ERROR, f"HTTP 200 but body was not JSON: {resp.text[:120]}"
        preview = json.dumps(data)[:160]
        return REACHABLE, f"HTTP 200, JSON: {preview}"

    return ERROR, f"HTTP {resp.status_code}: {resp.text[:160]}"


def probe(name, method, url, needs_key=None, **kwargs):
    print(f"\n--- {name} ---")
    if needs_key and not needs_key[1]:
        print(f"    (no {needs_key[0]} set -- probing anyway to test host reachability)")
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    headers.setdefault("Accept", "application/json")
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as c:
            resp = c.request(method, url, headers=headers, **kwargs)
    except Exception as e:
        results.append((name, ERROR, str(e)))
        print(f"    {ERROR}: {e}")
        return
    status, detail = classify(resp)
    results.append((name, status, detail))
    print(f"    {status}: {detail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=PROBE_TOKEN, help="Solana mint address to probe with")
    args = ap.parse_args()
    tok = args.token

    print("PROVIDER ACCESSIBILITY TEST")
    print("Answers one question per provider: can plain Python reach it from this machine?")
    print(f"Probe token: {tok}")

    st_key = os.environ.get("SOLANATRACKER_API_KEY", "")
    be_key = os.environ.get("BIRDEYE_API_KEY", "")

    print("\n" + "=" * 70)
    print("NO API KEY REQUIRED -- these should all work right now")
    print("=" * 70)

    probe("DexScreener (current provider, baseline)", "GET",
          f"https://api.dexscreener.com/token-pairs/v1/solana/{tok}")

    probe("RugCheck (free rug/security analysis)", "GET",
          f"https://api.rugcheck.xyz/v1/tokens/{tok}/report/summary")

    probe("Jupiter (price)", "GET",
          f"https://api.jup.ag/price/v3?ids={tok}")

    probe("GeckoTerminal (DEX market data)", "GET",
          f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{tok}")

    print("\n" + "=" * 70)
    print("API KEY REQUIRED -- 'NEEDS KEY' here is a PASS (host is reachable)")
    print("=" * 70)

    probe("Solana Tracker (recommended primary)", "GET",
          f"https://data.solanatracker.io/tokens/{tok}",
          needs_key=("SOLANATRACKER_API_KEY", st_key),
          headers={"x-api-key": st_key} if st_key else {})

    probe("Birdeye (mature alternative)", "GET",
          f"https://public-api.birdeye.so/defi/price?address={tok}",
          needs_key=("BIRDEYE_API_KEY", be_key),
          headers={"X-API-KEY": be_key, "x-chain": "solana"} if be_key
                  else {"x-chain": "solana"})

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    blocked = [r for r in results if r[1] == BLOCKED]
    reachable = [r for r in results if r[1] in (REACHABLE, NEEDS_KEY)]
    errored = [r for r in results if r[1] == ERROR]

    for name, status, _ in results:
        print(f"  {status:<10} {name}")

    print(f"\n  {len(reachable)} usable, {len(blocked)} bot-blocked, {len(errored)} errored")

    if blocked:
        print("\n  BOT-BLOCKED (do not build on these from a server):")
        for name, _, detail in blocked:
            print(f"    - {name}: {detail}")

    if any(n.startswith("Solana Tracker") and s in (REACHABLE, NEEDS_KEY) for n, s, _ in results):
        print("\n  Solana Tracker's host is reachable from this machine. That's the")
        print("  one that matters most -- it returns price, liquidity, volume,")
        print("  top-10 holder concentration AND insider/bundler/sniper analysis")
        print("  in a single call. Get a free key at solanatracker.io and re-run.")

    return 0 if not blocked else 1


if __name__ == "__main__":
    sys.exit(main())
