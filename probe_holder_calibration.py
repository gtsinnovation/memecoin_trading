"""Calibrate chain-computed top-10 holder concentration against RugCheck's.

    docker compose exec web python probe_holder_calibration.py

WHY
F_ATLAS refuses a token when holder concentration cannot be measured, and 67%
of its rejections are that refusal rather than a genuine concentration
failure. Computing concentration from chain would close that gap, because
every SPL mint has largest-accounts data whereas RugCheck's coverage is
partial.

The catch is that the two numbers do not mean the same thing.
getTokenLargestAccounts returns the biggest TOKEN ACCOUNTS, which on a live
token includes the liquidity pool, the bonding curve and burn addresses.
RugCheck reports something closer to real-wallet concentration. Swapping one
for the other under a 30% ceiling would convert "we could not measure it"
into "this looks concentrated" and the funnel would not move.

So this measures the gap before anything is changed:

  raw_chain      every one of the top 10 accounts, unadjusted
  wallet_chain   only accounts held by real wallets -- program-owned
                 accounts (AMM vaults, bonding curves) and the incinerator
                 removed
  rugcheck       what RugCheck reports, where it reports anything

A token account's OWNER is a wallet when that owner address is itself owned
by the System Program. An AMM vault's owner is a program-derived address
owned by the AMM program. That distinction needs no registry of pool
addresses and works for launchpads that did not exist last week.

Prints per-token rows and the summary needed to choose a definition and a
ceiling. Changes nothing.
"""
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

# The SAME classifier the pipeline uses. Importing it rather than
# reimplementing it is the point: a probe that measures something the
# gate does not is worse than no probe.
from holder_concentration import is_on_curve

SYSTEM_PROGRAM = "11111111111111111111111111111111"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
RUGCHECK = os.environ.get("RUGCHECK_API_BASE", "https://api.rugcheck.xyz")
RPC_URL = os.environ.get("SOLANA_RPC_URL") or "https://solana-rpc.publicnode.com"
RPC_X_TOKEN = os.environ.get("SOLANA_RPC_X_TOKEN") or os.environ.get("TRITON_X_TOKEN") or ""
CEILING = 30.0
SAMPLE = int(os.environ.get("CALIB_SAMPLE", "25"))


# Public Solana RPC throttles hard and `getTokenLargestAccounts` is one of the
# expensive calls. The first version fired ~4 requests per token 0.25s apart
# and was 429'd on essentially every token. These defaults assume the free
# endpoint; with a paid RPC set CALIB_RPC_INTERVAL=0.1 and it finishes in
# seconds instead of minutes.
RPC_MIN_INTERVAL = float(os.environ.get("CALIB_RPC_INTERVAL", "1.2"))
RPC_MAX_TRIES = int(os.environ.get("CALIB_RPC_TRIES", "5"))
_last_call = [0.0]


def _throttle():
    wait = RPC_MIN_INTERVAL - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()


def _rpc(method, params, tries=None):
    """One RPC call, throttled, with real backoff on 429.

    A 429 is not a failure to report -- it means "you asked too fast", and
    treating it as a dead endpoint is what made the first run look like the
    chain had no data for any token.
    """
    tries = tries or RPC_MAX_TRIES
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    headers = {"Content-Type": "application/json"}
    if RPC_X_TOKEN:
        headers["x-token"] = RPC_X_TOKEN
    last = None
    backoff = 2.0
    for attempt in range(tries):
        _throttle()
        try:
            req = urllib.request.Request(RPC_URL, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read())
            if "error" in out:
                msg = str(out["error"])
                # Some endpoints return 429 inside a JSON-RPC error body.
                if "429" in msg or "rate" in msg.lower():
                    last = msg[:120]
                    time.sleep(backoff); backoff *= 2
                    continue
                return None, msg[:120]
            return out.get("result"), None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (429, 503):
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    pause = float(retry_after) if retry_after else backoff
                except ValueError:
                    pause = backoff
                time.sleep(min(pause, 30.0)); backoff = min(backoff * 2, 30.0)
                continue
            return None, last
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(backoff); backoff = min(backoff * 2, 30.0)
    return None, f"{last} (gave up after {tries} tries)"


def _get(url):
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "holder-calibration/1.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read()), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def rugcheck_top10(mint):
    data, err = _get(f"{RUGCHECK}/v1/tokens/{mint}/report")
    if err:
        return None, err
    for key in ("topHolderPercent", "top10HolderPercent", "topHoldersPercent"):
        if isinstance(data.get(key), (int, float)):
            return float(data[key]), None
    holders = data.get("topHolders")
    if isinstance(holders, list) and holders:
        pcts = [h.get("pct") for h in holders[:10] if isinstance(h, dict)]
        pcts = [p for p in pcts if isinstance(p, (int, float))]
        # Partial reporting is refused, exactly as free_market_data does.
        if len(pcts) >= 10:
            return float(sum(pcts)), None
        return None, f"only {len(pcts)}/10 holders carry a percentage"
    return None, "no usable holder data"


def chain_top10(mint):
    """(raw_pct, wallet_pct, detail) computed from chain, or (None, None, err)."""
    supply, err = _rpc("getTokenSupply", [mint])
    if err:
        return None, None, f"getTokenSupply: {err}"
    try:
        total = float(supply["value"]["amount"])
    except Exception:
        return None, None, "unexpected supply shape"
    if total <= 0:
        return None, None, "supply is zero"

    largest, err = _rpc("getTokenLargestAccounts", [mint])
    if err:
        return None, None, f"getTokenLargestAccounts: {err}"
    try:
        rows = largest["value"][:10]
        amounts = [float(a["amount"]) for a in rows]
        accounts = [a["address"] for a in rows]
    except Exception:
        return None, None, "unexpected largest-accounts shape"
    if not amounts:
        return None, None, "no token accounts"

    raw = 100.0 * sum(amounts) / total

    # Who owns each token account?
    info, err = _rpc("getMultipleAccounts", [accounts, {"encoding": "jsonParsed"}])
    if err:
        return raw, None, f"owners unavailable: {err}"
    owners = []
    for acc in (info or {}).get("value") or []:
        try:
            owners.append(acc["data"]["parsed"]["info"]["owner"])
        except Exception:
            owners.append(None)
    if len(owners) != len(amounts):
        return raw, None, "owner list length mismatch"

    # An owner is a real wallet when its own account is System-Program owned.
    uniq = [o for o in set(o for o in owners if o) if o != INCINERATOR]
    is_wallet = {}
    was_wallet_before = {}
    for i in range(0, len(uniq), 100):
        chunk = uniq[i:i + 100]
        res, err = _rpc("getMultipleAccounts", [chunk, {"encoding": "base64"}])
        if err:
            return raw, None, f"owner classification failed: {err}"
        for addr, acc in zip(chunk, (res or {}).get("value") or []):
            # Account state alone is not enough: a signer-only PDA has no
            # account, which looks exactly like a never-written-to keypair.
            # The curve test separates them -- a PDA is by construction off
            # the ed25519 curve, which is what makes it unsignable.
            state_ok = (acc is None) or (acc.get("owner") == SYSTEM_PROGRAM)
            is_wallet[addr] = state_ok and is_on_curve(addr)
            # What the account-state-only rule WOULD have said, so the size of
            # the misclassification is measured rather than inferred.
            was_wallet_before[addr] = state_ok

    wallet_sum, program_sum, burn_sum, offcurve_sum = 0.0, 0.0, 0.0, 0.0
    for amt, owner in zip(amounts, owners):
        if owner == INCINERATOR or owner is None:
            burn_sum += amt
        elif is_wallet.get(owner, False):
            wallet_sum += amt
        else:
            program_sum += amt
            # Supply the old account-state-only rule counted as a wallet and
            # the curve test correctly reassigns to a program.
            if was_wallet_before.get(owner, False):
                offcurve_sum += amt
    detail = {"raw": raw,
              "wallet": 100.0 * wallet_sum / total,
              "program": 100.0 * program_sum / total,
              "burn": 100.0 * burn_sum / total,
              "offcurve": 100.0 * offcurve_sum / total,
              "n_accounts": len(amounts)}
    return raw, detail["wallet"], detail


def load_mints():
    """Tokens the agent has actually evaluated, newest first."""
    try:
        import psycopg2
        dsn = os.environ.get("DATABASE_URL",
                             "postgresql://postgres:secret@db:5432/memecoin_trading")
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT token_address, MAX(token_symbol), MAX(rejected_by)
                FROM paper_trades
                WHERE evaluated_at > NOW() - INTERVAL '3 days'
                GROUP BY token_address
                ORDER BY MAX(evaluated_at) DESC
                LIMIT %s;
            """, (SAMPLE,))
            rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"Could not read tokens from the database ({e}).")
        return []


def main():
    print("=" * 96)
    print("HOLDER CONCENTRATION CALIBRATION -- chain vs RugCheck")
    print(f"RPC: {RPC_URL}{'  (x-token set)' if RPC_X_TOKEN else ''}")
    print("=" * 96)

    mints = load_mints()
    if not mints:
        print("No tokens to calibrate on.")
        return 2
    print(f"{len(mints)} tokens evaluated in the last 3 days\n")

    print(f"{'symbol':<12} {'rugcheck':>9} {'raw chain':>10} {'wallets':>9} "
          f"{'programs':>9} {'burn':>7} {'pda*':>7}  verdict under a 30% ceiling")
    print("-" * 104)
    print("  pda* = supply held by signer-only PDAs, which the account-state test alone")
    print("         called wallets. A large column here is the bug this run re-measures.")

    est = len(mints) * 4 * RPC_MIN_INTERVAL
    print(f"~4 RPC calls per token at {RPC_MIN_INTERVAL:.1f}s spacing "
          f"-- roughly {est/60:.1f} min. Set CALIB_RPC_INTERVAL lower on a paid RPC.\n")
    rows = []
    for idx, (mint, symbol, rejected_by) in enumerate(mints, 1):
        print(f"  [{idx}/{len(mints)}]", end="\r", flush=True)
        rc, rc_err = rugcheck_top10(mint)
        raw, wallet, detail = chain_top10(mint)
        if raw is None:
            print(f"{(symbol or '?')[:12]:<12} {'-':>9} {'chain failed':>10}  {detail}")
            continue
        prog = detail["program"] if isinstance(detail, dict) else None
        burn = detail["burn"] if isinstance(detail, dict) else None
        offc = detail["offcurve"] if isinstance(detail, dict) else None

        def verdict(v):
            return "REFUSE" if v is None else ("reject" if v > CEILING else "pass")

        note = f"rugcheck={verdict(rc):<6} raw={verdict(raw):<6} wallet={verdict(wallet)}"
        print(f"{(symbol or '?')[:12]:<12} "
              f"{(f'{rc:.1f}%' if rc is not None else 'none'):>9} "
              f"{raw:>9.1f}% "
              f"{(f'{wallet:.1f}%' if wallet is not None else '?'):>9} "
              f"{(f'{prog:.1f}%' if prog is not None else '?'):>9} "
              f"{(f'{burn:.1f}%' if burn is not None else '?'):>7} "
              f"{(f'{offc:.1f}%' if offc is not None else '?'):>7}  {note}")
        rows.append({"symbol": symbol, "rugcheck": rc, "raw": raw,
                     "wallet": wallet, "program": prog, "burn": burn,
                     "offcurve": offc, "rejected_by": rejected_by})

    if not rows:
        print("\nNo token produced a chain measurement.")
        return 1

    covered = [r for r in rows if r["rugcheck"] is not None]
    print("\n" + "=" * 96)
    print(f"RugCheck covered {len(covered)}/{len(rows)} tokens "
          f"({100.0 * len(covered) / len(rows):.0f}%) -- the rest are the ones F_ATLAS refuses today")

    def summarise(label, values):
        vals = [v for v in values if v is not None]
        if not vals:
            print(f"  {label:<26} no data"); return
        vals.sort()
        med = statistics.median(vals)
        over = sum(1 for v in vals if v > CEILING)
        print(f"  {label:<26} median {med:>6.1f}%   "
              f"p25 {vals[len(vals)//4]:>6.1f}%   p75 {vals[3*len(vals)//4]:>6.1f}%   "
              f"over {CEILING:.0f}%: {over}/{len(vals)} ({100.0*over/len(vals):.0f}%)")

    print("\nAcross every token measured from chain:")
    summarise("raw chain top-10", [r["raw"] for r in rows])
    summarise("wallets only", [r["wallet"] for r in rows])
    summarise("program-owned share", [r["program"] for r in rows])
    summarise("of which signer-only PDAs", [r["offcurve"] for r in rows])
    moved = [r for r in rows if (r["offcurve"] or 0.0) > 1.0]
    print(f"\n  {len(moved)}/{len(rows)} tokens had supply that the account-state test alone "
          f"counted as\n  wallet concentration and the curve test moves to programs.")

    if covered:
        print("\nOn the tokens RugCheck DOES cover -- this is the calibration:")
        summarise("rugcheck", [r["rugcheck"] for r in covered])
        summarise("raw chain", [r["raw"] for r in covered])
        summarise("wallets only", [r["wallet"] for r in covered])
        gaps_raw = [r["raw"] - r["rugcheck"] for r in covered]
        gaps_wal = [r["wallet"] - r["rugcheck"] for r in covered
                    if r["wallet"] is not None]
        print(f"\n  raw    minus rugcheck: median {statistics.median(gaps_raw):+.1f} pp")
        if gaps_wal:
            print(f"  wallet minus rugcheck: median {statistics.median(gaps_wal):+.1f} pp")
        print("\n  A small wallet-minus-rugcheck gap means the wallet-only definition can")
        print("  reuse the 30% ceiling directly. A large one means the ceiling needs")
        print("  recalibrating to the new measurement before it is trusted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
