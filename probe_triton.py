# probe_triton.py
"""Read-only probe for Triton One RPC. Changes nothing, writes nothing.

WHAT TRITON IS, AND WHAT IT IS NOT
Triton is Solana RPC infrastructure -- standard JSON-RPC, Dragon's Mouth
(Yellowstone) gRPC streaming, and a historical archive. It has NO trending or
new-listing endpoint, so it cannot be added beside Birdeye as a peer discovery
source. What it offers is the layer underneath every aggregator: the chain
itself.

That matters here for one specific reason. Every number the gates judge --
price, liquidity, holder concentration -- currently arrives as some vendor's
INTERPRETATION of chain state, and two of those vendors are already known to
disagree by 94.6% on at least one live token. Nothing in the system can
currently say which is right.

WHAT THIS PROBE MEASURES
1. That the credential works, and by which auth style (path or x-token header).
2. Round-trip latency and slot freshness against the public RPC.
3. Top-10 holder concentration computed FROM CHAIN, next to what RugCheck
   reports for the same mint. That figure is F_ATLAS's input: the gate refuses
   a token when concentration is too high. If the two sources disagree
   materially, a money gate is deciding on a number that is wrong, and that is
   worth knowing whoever wins the provider bake-off.
4. Mint and freeze authority, which are rug signals no aggregator has to be
   trusted for.

gRPC streaming is deliberately NOT probed here. It needs proto stubs and a
persistent connection, which is a real build; this establishes whether the
cheap half is worth anything first.

Usage, inside the web container:
    docker compose exec web python probe_triton.py
    docker compose exec web python probe_triton.py --mint <address>
"""
import os
import sys
import json
import time
import asyncio

import httpx

# Triton hands out either a URL with the token in the path
# (https://<name>.rpcpool.com/<token>) or a hostname plus an x-token header.
# Both are accepted so the probe does not have to be told which one you have.
TRITON_RPC_URL = os.environ.get("TRITON_RPC_URL", "").strip()
TRITON_X_TOKEN = os.environ.get("TRITON_X_TOKEN", "").strip()
PUBLIC_RPC = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
RUGCHECK_API_BASE = os.environ.get("RUGCHECK_API_BASE", "https://api.rugcheck.xyz")
BIRDEYE_BASE = os.environ.get("BIRDEYE_API_BASE", "https://public-api.birdeye.so")
BIRDEYE_API_KEY = os.environ.get("BIRDEYE_API_KEY", "").strip()

WSOL = "So11111111111111111111111111111111111111112"

# getGenesisHash is the only reliable way to know which chain an endpoint is
# actually on. The hostname is a convention, not a guarantee, and a wrong
# network produces "account not found" on every mainnet mint -- which reads as
# a broken probe rather than the configuration error it is.
GENESIS = {
    "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d": "mainnet-beta",
    "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG": "devnet",
    "4uhcVJyU9pJkvQyS88uRDiswHXSCkY3zQawwpjk2NsNY": "testnet",
}


def _redact(url):
    """Never print the token. Triton path-style URLs embed it."""
    if not url:
        return "(unset)"
    parts = url.split("/")
    if len(parts) > 3 and parts[-1]:
        parts[-1] = f"...{parts[-1][-4:]}" if len(parts[-1]) > 4 else "..."
    return "/".join(parts)


async def rpc(client, url, method, params=None, headers=None, label=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    t0 = time.monotonic()
    try:
        r = await client.post(url, json=body, headers=headers or {}, timeout=25.0)
    except Exception as e:
        return None, 0.0, f"{type(e).__name__}: {e}"
    ms = (time.monotonic() - t0) * 1000.0
    if r.status_code != 200:
        return None, ms, f"HTTP {r.status_code}: {r.text[:200]}"
    try:
        payload = r.json()
    except Exception:
        return None, ms, f"non-JSON: {r.text[:200]}"
    if "error" in payload:
        return None, ms, f"RPC error: {json.dumps(payload['error'])[:200]}"
    return payload.get("result"), ms, None


async def resolve_auth(client):
    """Returns (url, headers, style) for whichever credential form works."""
    attempts = []
    if TRITON_RPC_URL and TRITON_X_TOKEN:
        attempts.append((TRITON_RPC_URL, {"x-token": TRITON_X_TOKEN}, "url + x-token header"))
    if TRITON_RPC_URL:
        attempts.append((TRITON_RPC_URL, {}, "token in URL path"))
    if not attempts:
        return None, None, None
    for url, headers, style in attempts:
        result, ms, err = await rpc(client, url, "getVersion", headers=headers)
        if err is None:
            print(f"  auth OK via {style}  ({ms:.0f}ms)")
            print(f"  solana-core {result.get('solana-core') if isinstance(result, dict) else result}")
            return url, headers, style
        print(f"  {style}: {err[:150]}")
    return None, None, None


async def top10_from_chain(client, url, headers, mint):
    """Top-10 holder percentage, computed from chain rather than reported.

    Deliberately NOT adjusted for burn or LP addresses. RugCheck may or may not
    exclude those, and the point of this probe is to show the raw gap before
    deciding whose definition the gate should use -- an adjustment invented
    here would hide exactly what we are trying to see.
    """
    supply, _, err = await rpc(client, url, "getTokenSupply", [mint], headers)
    if err:
        return None, None, f"getTokenSupply: {err[:120]}"
    try:
        total = float(supply["value"]["amount"])
    except Exception:
        return None, None, f"unexpected supply shape: {json.dumps(supply)[:150]}"
    if total <= 0:
        return None, None, "supply is zero"

    largest, _, err = await rpc(client, url, "getTokenLargestAccounts", [mint], headers)
    if err:
        return None, None, f"getTokenLargestAccounts: {err[:120]}"
    try:
        holders = [float(a["amount"]) for a in largest["value"]][:10]
    except Exception:
        return None, None, f"unexpected largest-accounts shape: {json.dumps(largest)[:150]}"
    return 100.0 * sum(holders) / total, len(holders), None


async def authorities(client, url, headers, mint):
    result, _, err = await rpc(client, url, "getAccountInfo",
                               [mint, {"encoding": "jsonParsed"}], headers)
    if err:
        return None, err[:120]
    try:
        info = result["value"]["data"]["parsed"]["info"]
    except Exception:
        return None, "mint account did not parse as an SPL mint"
    return {"mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            "decimals": info.get("decimals")}, None


async def rugcheck_top10(client, mint):
    try:
        r = await client.get(f"{RUGCHECK_API_BASE}/v1/tokens/{mint}/report",
                             timeout=25.0)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        data = r.json()
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    # Shape varies; try the documented field then fall back to summing holders.
    for key in ("topHolderPercent", "top10HolderPercent", "topHoldersPercent"):
        if isinstance(data.get(key), (int, float)):
            return float(data[key]), None
    holders = data.get("topHolders")
    if isinstance(holders, list) and holders:
        pcts = [h.get("pct") for h in holders[:10] if isinstance(h, dict)]
        pcts = [p for p in pcts if isinstance(p, (int, float))]
        if pcts:
            return float(sum(pcts)), None
    return None, f"no holder field found (keys: {sorted(data)[:12]})"


async def pick_mint(client):
    """A live memecoin beats wrapped SOL for this comparison: concentration on
    a blue chip is uninteresting and tells us nothing about the gate."""
    if not BIRDEYE_API_KEY:
        return None
    try:
        r = await client.get(
            f"{BIRDEYE_BASE}/defi/v3/token/list", timeout=25.0,
            headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"},
            params={"sort_by": "recent_listing_time", "sort_type": "desc",
                    "min_liquidity": 25000, "offset": 0, "limit": 10})
        rows = (r.json().get("data") or {}).get("items") or []
        for row in rows:
            addr = row.get("address")
            if isinstance(addr, str) and addr != WSOL:
                print(f"  using a live candidate: {row.get('symbol')} {addr}")
                return addr
    except Exception as e:
        print(f"  (could not pull a candidate from Birdeye: {type(e).__name__})")
    return None


async def main(mint_arg):
    print("=" * 70)
    print(f"TRITON_RPC_URL = {_redact(TRITON_RPC_URL)}")
    print(f"TRITON_X_TOKEN = {'set (' + str(len(TRITON_X_TOKEN)) + ' chars)' if TRITON_X_TOKEN else '(unset)'}")
    if not TRITON_RPC_URL:
        print("\nNeither credential is visible inside this container.\n\n"
              "Add to the ROOT .env:\n"
              "    TRITON_RPC_URL=https://<your-endpoint>.rpcpool.com/<token>\n"
              "    TRITON_X_TOKEN=          # only if your endpoint uses header auth\n\n"
              "AND to docker-compose.yml under the web service's environment:\n"
              "    - TRITON_RPC_URL=${TRITON_RPC_URL:-}\n"
              "    - TRITON_X_TOKEN=${TRITON_X_TOKEN:-}\n\n"
              "Compose forwards only what it names, so the second step is not\n"
              "optional -- without it this looks exactly like a bad credential.")
        return 1

    async with httpx.AsyncClient() as client:
        print("\n1. AUTHENTICATION")
        url, headers, style = await resolve_auth(client)
        if url is None:
            print("  No credential form worked. Check the endpoint and token.")
            return 1

        print("\n2. WHICH CHAIN IS THIS?")
        genesis, _, g_err = await rpc(client, url, "getGenesisHash", headers=headers)
        network = GENESIS.get(genesis, f"unknown (genesis {genesis})") if not g_err else None
        if g_err:
            print(f"  could not determine: {g_err[:150]}")
        else:
            print(f"  {network}")
        if network != "mainnet-beta":
            print()
            print("  This endpoint is NOT mainnet, so the comparisons below cannot run:")
            print("  every mint Birdeye returns is a mainnet address, and looking one up")
            print("  here returns 'account not found' -- a wrong-network error, not a")
            print("  different answer. Nothing about holder concentration or rug signals")
            print("  can be learned from this endpoint.")
            print()
            print("  It is still useful: signer_service points at a public devnet RPC,")
            print("  which is what rate-limited the Stage 3 airdrops. Pointing it here")
            print("  instead removes that. For the provider comparison, create a mainnet")
            print("  endpoint on the same Triton account and set TRITON_RPC_URL to it.")
            print()
            print("  Continuing with latency only.")

        print("\n3. LATENCY AND FRESHNESS vs the public RPC")
        t_slot, t_ms, t_err = await rpc(client, url, "getSlot", headers=headers)
        p_slot, p_ms, p_err = await rpc(client, PUBLIC_RPC, "getSlot")
        print(f"  triton: slot={t_slot} {t_ms:.0f}ms {t_err or ''}")
        print(f"  public: slot={p_slot} {p_ms:.0f}ms {p_err or ''}")
        if isinstance(t_slot, int) and isinstance(p_slot, int):
            lead = t_slot - p_slot
            print(f"  -> triton is {abs(lead)} slot(s) {'ahead' if lead >= 0 else 'behind'} "
                  f"(~{abs(lead) * 0.4:.1f}s) and {p_ms - t_ms:+.0f}ms on round trip")
            print("     One sample, not a benchmark -- but a persistent multi-slot lag")
            print("     would matter more than any feature difference.")

        if network != "mainnet-beta" and not mint_arg:
            print("\n" + "=" * 70)
            print("STOPPING HERE: a non-mainnet endpoint has nothing to compare.")
            print("Pass --mint <a devnet mint you control> to exercise the reads anyway.")
            return 0

        mint = mint_arg or await pick_mint(client) or WSOL
        if mint == WSOL:
            print("\n  (falling back to wrapped SOL -- pass --mint <address> for a real test)")

        print(f"\n4. HOLDER CONCENTRATION -- F_ATLAS's input, two sources")
        print(f"  mint: {mint}")
        chain_pct, n, err = await top10_from_chain(client, url, headers, mint)
        if err:
            print(f"  from chain: FAILED {err}")
        else:
            print(f"  from chain (top {n}): {chain_pct:.2f}%")
        rug_pct, rug_err = await rugcheck_top10(client, mint)
        if rug_err:
            print(f"  from RugCheck: unavailable ({rug_err})")
        else:
            print(f"  from RugCheck: {rug_pct:.2f}%")
        if chain_pct is not None and rug_pct is not None:
            gap = abs(chain_pct - rug_pct)
            print(f"  -> gap {gap:.2f} percentage points")
            # F_ATLAS refuses above its concentration threshold. A gap that can
            # straddle the threshold means the verdict depends on the vendor,
            # not on the token.
            if gap > 5.0:
                print("     MATERIAL. F_ATLAS approves or refuses on this number, so a gap")
                print("     this size means some verdicts are decided by which vendor was")
                print("     asked. Worth resolving before trusting the cohort comparison.")
            else:
                print("     Close enough that the gate's verdict would rarely change.")
            print("     Note: neither figure is burn/LP-adjusted here on purpose --")
            print("     an adjustment invented in this probe would hide the raw gap.")

        print("\n5. RUG SIGNALS STRAIGHT FROM CHAIN")
        auth, err = await authorities(client, url, headers, mint)
        if err:
            print(f"  unavailable: {err}")
        else:
            mint_auth = auth["mint_authority"]
            freeze = auth["freeze_authority"]
            print(f"  mint authority:   {mint_auth or 'revoked'}"
                  f"{'   <- supply can still be inflated' if mint_auth else ''}")
            print(f"  freeze authority: {freeze or 'revoked'}"
                  f"{'   <- holders can be frozen out' if freeze else ''}")
            print(f"  decimals:         {auth['decimals']}")
            print("  These are facts, not a vendor's score. No aggregator needs to be")
            print("  trusted for them, and neither is currently read by any gate.")

    print("\n" + "=" * 70)
    print("NOT PROBED: Dragon's Mouth gRPC streaming. It needs proto stubs and a")
    print("persistent connection -- a real build. It is also the only thing here")
    print("that would change DISCOVERY rather than verification: sub-second")
    print("new-pool detection instead of waiting for an aggregator to index.")
    return 0


if __name__ == "__main__":
    mint = None
    if "--mint" in sys.argv:
        i = sys.argv.index("--mint")
        if i + 1 < len(sys.argv):
            mint = sys.argv[i + 1]
    raise SystemExit(asyncio.run(main(mint)))
