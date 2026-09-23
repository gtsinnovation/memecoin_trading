"""One definition of top-10 holder concentration, shared by every provider.

WHY THIS MODULE EXISTS

Before it, three providers computed "top 10 holder concentration" three
incompatible ways and handed all three to the SAME 30% ceiling in F_ATLAS:

  free         RugCheck's report -- roughly real-wallet concentration
  dexscreener  market_data.fetch_top10_holder_pct -- RAW chain, which counts
               the AMM pool, the bonding curve and burn addresses as
               "holders"
  gmgn         a third vendor's number (provider is dead in practice)

Those are not the same quantity. A freshly-launched token with 60% of supply
sitting in its own liquidity pool reads as 60%+ concentrated under the raw
definition and as near-zero under the wallet definition. Which one the gate
saw depended on an environment variable -- and the two disagreed about the
default: market_data.py defaulted to "dexscreener" while docker-compose.yml
overrode it to "free". Flipping one variable silently changed what the gate
had been screening for, with no signal anywhere that the number had changed
meaning.

So concentration is computed here, once, with the definition named
explicitly, and the ceiling lives here as a single constant rather than as a
literal inside the gate.

THE WALLET DISCRIMINATOR

getTokenLargestAccounts returns the largest TOKEN ACCOUNTS. A token account's
OWNER is a real wallet when that owner address is itself owned by the System
Program; an AMM vault's owner is a program-derived address owned by the AMM
program. That test needs no registry of pool addresses and keeps working for
launchpads that did not exist last week, which a hardcoded list does not.

FAIL-CLOSED

Every failure path returns None, never 0.0. A concentration of 0% does not
exist, and the whole point of F_ATLAS's missing-data branch is that "nobody
could measure this" must not read as "perfectly distributed". The resolver
below also refuses to SUBSTITUTE one definition for another when the
configured one is unavailable: a ceiling calibrated against wallet
concentration means nothing applied to a raw number, so an unavailable
measurement is an absence, not an excuse to use the other one.
"""
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

SYSTEM_PROGRAM = "11111111111111111111111111111111"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"

# THE ceiling. Imported by engine.node_F_ATLAS -- it used to be a literal
# inside the gate, which meant the threshold and the definition of the thing
# being thresholded lived in different files.
TOP10_CONCENTRATION_CEILING_PERCENT = float(
    os.environ.get("TOP10_CONCENTRATION_CEILING_PERCENT", "30.0"))

# Which measurement the GATE sees. Deliberately defaults to "provider", i.e.
# exactly the behaviour that has been running -- this module is wired in
# without changing any verdict until the definition is chosen deliberately
# against the calibration data (probe_holder_calibration.py).
#
#   provider      whatever the configured market-data provider reports
#   chain_wallet  chain, counting only accounts owned by real wallets
#   chain_raw     chain, counting every top-10 account including the pool
CONCENTRATION_SOURCE = os.environ.get(
    "HOLDER_CONCENTRATION_SOURCE", "provider").strip().lower()

# Measure from chain even when the gate is not using it, so the calibration
# accumulates on the real token population rather than on a 60-token probe.
# Costs ~4 RPC calls per evaluated token; turn off on a rate-limited endpoint.
_OBSERVE_DEFAULT = "true"
OBSERVE_CHAIN = os.environ.get(
    "HOLDER_CONCENTRATION_OBSERVE", _OBSERVE_DEFAULT).strip().lower() in ("1", "true", "yes")

# --- RPC configuration -----------------------------------------------------
#
# Lives here rather than in market_data.py because this module is the one
# that talks to the chain; market_data imports these names back so there is
# still exactly one place the endpoint is configured.
#
# The token goes in a HEADER, never in the URL. Triton and most paid
# providers accept a path-style URL with the credential embedded, and that
# form is deliberately NOT used: SOLANA_RPC_URL is printed verbatim into at
# least one error message that reaches logs and alert rows, so a credential
# in the URL is a credential in the database.
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet.solana.com")
SOLANA_RPC_X_TOKEN = (os.environ.get("SOLANA_RPC_X_TOKEN")
                      or os.environ.get("TRITON_X_TOKEN") or "").strip()


def rpc_headers() -> Dict[str, str]:
    return {"x-token": SOLANA_RPC_X_TOKEN} if SOLANA_RPC_X_TOKEN else {}


# --- Is this address a wallet, structurally? -------------------------------
#
# A program-derived address is BY CONSTRUCTION not a point on the ed25519
# curve -- that is the property `findProgramAddress` searches for, and it is
# what makes a PDA unsignable. A keypair wallet address IS a compressed curve
# point. So the question "could anyone hold the private key to this?" is
# answerable offline, from the 32 bytes alone, with no RPC call.
#
# This matters because the account-state test alone gets it wrong in a
# specific and common case: a PDA that is only ever used as a signing
# authority has no account on chain at all, and an address with no account
# looks exactly like an ordinary never-written-to wallet. Raydium's pool
# authority is such a PDA. Classifying it as a wallet moved the entire
# liquidity pool into the "wallet concentration" bucket and reported a
# freshly-graduated token as 99% held by ten wallets.
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}


def b58decode(address: str) -> Optional[bytes]:
    """Base58 -> bytes, or None if the string is not valid base58."""
    n = 0
    for ch in address:
        idx = _B58_INDEX.get(ch)
        if idx is None:
            return None
        n = n * 58 + idx
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(address) - len(address.lstrip("1"))) + raw


def is_on_curve(address: str) -> bool:
    """True when the address is a valid ed25519 point, i.e. a keypair wallet.

    False for a program-derived address, and false for anything that is not a
    well-formed 32-byte base58 key -- an address we cannot parse is not an
    address we should call a wallet.
    """
    raw = b58decode(address or "")
    if raw is None or len(raw) != 32:
        return False
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    uv3 = u * v % _P * v % _P * v % _P
    uv7 = uv3 * v % _P * v % _P * v % _P * v % _P
    x = uv3 * pow(uv7, (_P - 5) // 8, _P) % _P
    vx2 = v * x % _P * x % _P
    # The second branch is the root that needs multiplying by sqrt(-1); the
    # point is on the curve either way, which is all this function reports.
    return vx2 == u % _P or vx2 == (-u) % _P


@dataclass
class ChainConcentration:
    """What chain says, broken out by holder kind. Percentages of total supply.

    Any field may be None -- that is the fail-closed absence, not a zero.
    `raw_percent` can be present while `wallet_percent` is None: the largest
    accounts resolved but their owners could not be classified.
    """
    raw_percent: Optional[float] = None
    wallet_percent: Optional[float] = None
    program_percent: Optional[float] = None
    burn_percent: Optional[float] = None
    n_accounts: Optional[int] = None
    error: Optional[str] = None

    def percent_for(self, definition: str) -> Optional[float]:
        if definition == "chain_wallet":
            return self.wallet_percent
        if definition == "chain_raw":
            return self.raw_percent
        return None


async def _rpc(client: httpx.AsyncClient, method: str, params: List[Any]) -> tuple:
    """(result, error). Never raises."""
    try:
        resp = await client.post(
            SOLANA_RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers=rpc_headers(), timeout=15.0)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return None, f"{method}: {type(e).__name__}: {e}"
    if isinstance(payload, dict) and "error" in payload:
        return None, f"{method}: {str(payload['error'])[:160]}"
    return (payload or {}).get("result"), None


async def _classify_owners(client: httpx.AsyncClient,
                           owners: List[Optional[str]]) -> Optional[Dict[str, bool]]:
    """{owner_address: is_real_wallet}, or None if classification failed.

    None is returned rather than a partial map: a partial classification
    would silently move program-held supply into the wallet bucket, which
    biases the number DOWN -- the one direction that turns a concentrated
    token into a passing one.
    """
    uniq = sorted({o for o in owners if o and o != INCINERATOR})
    if not uniq:
        return {}
    out: Dict[str, bool] = {}
    for i in range(0, len(uniq), 100):
        chunk = uniq[i:i + 100]
        res, err = await _rpc(client, "getMultipleAccounts", [chunk, {"encoding": "base64"}])
        if err:
            logger.warning(f"Owner classification failed: {err}")
            return None
        values = (res or {}).get("value")
        if not isinstance(values, list) or len(values) != len(chunk):
            logger.warning("Owner classification returned an unexpected shape.")
            return None
        for addr, acc in zip(chunk, values):
            # BOTH tests must pass. The curve test alone would call a
            # token-program-owned account a wallet; the account test alone
            # calls a signer-only PDA a wallet, because a PDA with no account
            # is indistinguishable from a never-written-to keypair by account
            # state. An address off the curve cannot be signed for by anyone,
            # so it is not somebody's holding however its account looks.
            state_ok = (acc is None) or (acc.get("owner") == SYSTEM_PROGRAM)
            out[addr] = state_ok and is_on_curve(addr)
    return out


def bucket_amounts(amounts: List[float], owners: List[Optional[str]],
                   is_wallet: Dict[str, bool]) -> Optional[tuple]:
    """(wallet_sum, program_sum, burn_sum), or None if any owner is unclassified.

    Extracted from fetch_chain_concentration so this arithmetic is reachable
    by a test without an RPC fixture. It is the step where a mistake is
    invisible: every wrong answer here is still a plausible-looking
    percentage, and the one that matters biases DOWNWARD -- counting pool
    supply as wallet supply turns a concentrated token into a passing one.

    An owner missing from the classification map is refused rather than
    defaulted. `_classify_owners` returns None rather than a partial map, so
    that should not happen; "should not happen" is exactly the condition
    worth failing closed on, and a default of False here would have been
    silently flippable to True by a later edit with nothing to catch it.

    These two guards are deliberately redundant. Mutation testing confirms
    it: making `_classify_owners` hand back a partial map instead of None
    now produces the same outcome (raw kept, wallet absent) because this
    function refuses the unclassified owner. That mutant is equivalent, not
    escaped -- which is the point of having the second guard.
    """
    wallet_sum = program_sum = burn_sum = 0.0
    for amount, owner in zip(amounts, owners):
        if owner is None or owner == INCINERATOR:
            burn_sum += amount
        elif owner not in is_wallet:
            return None
        elif is_wallet[owner]:
            wallet_sum += amount
        else:
            program_sum += amount
    return wallet_sum, program_sum, burn_sum


async def fetch_chain_concentration(client: httpx.AsyncClient,
                                    token_address: str) -> ChainConcentration:
    """Top-10 concentration from chain, split into wallet / program / burn."""
    supply, err = await _rpc(client, "getTokenSupply", [token_address])
    if err:
        return ChainConcentration(error=err)
    try:
        # Raw base units, not uiAmount: uiAmount is a float the RPC has
        # already divided by 10**decimals, and for a 9-decimal token with a
        # large supply that division is lossy before we ever see it.
        total = float(((supply or {}).get("value") or {}).get("amount") or 0.0)
    except (TypeError, ValueError):
        return ChainConcentration(error="unexpected getTokenSupply shape")
    if total <= 0:
        return ChainConcentration(error="total supply is zero")

    largest, err = await _rpc(client, "getTokenLargestAccounts", [token_address])
    if err:
        return ChainConcentration(error=err)
    rows = ((largest or {}).get("value") or [])[:10]
    if not rows:
        return ChainConcentration(error="no token accounts")
    try:
        amounts = [float(r["amount"]) for r in rows]
        accounts = [r["address"] for r in rows]
    except (KeyError, TypeError, ValueError):
        return ChainConcentration(error="unexpected getTokenLargestAccounts shape")

    raw = 100.0 * sum(amounts) / total
    result = ChainConcentration(raw_percent=round(raw, 4), n_accounts=len(amounts))

    info, err = await _rpc(client, "getMultipleAccounts", [accounts, {"encoding": "jsonParsed"}])
    if err:
        result.error = f"owners unavailable: {err}"
        return result
    owners: List[Optional[str]] = []
    for acc in ((info or {}).get("value") or []):
        try:
            owners.append(acc["data"]["parsed"]["info"]["owner"])
        except (KeyError, TypeError):
            owners.append(None)
    if len(owners) != len(amounts):
        result.error = "owner list length mismatch"
        return result

    is_wallet = await _classify_owners(client, owners)
    if is_wallet is None:
        result.error = "owner classification failed"
        return result

    buckets = bucket_amounts(amounts, owners, is_wallet)
    if buckets is None:
        result.error = "an owner was not classified"
        return result
    wallet_sum, program_sum, burn_sum = buckets

    result.wallet_percent = round(100.0 * wallet_sum / total, 4)
    result.program_percent = round(100.0 * program_sum / total, 4)
    result.burn_percent = round(100.0 * burn_sum / total, 4)
    return result


@dataclass
class Resolution:
    percent: Optional[float]
    source: str
    missing: bool


def resolve_concentration(provider_percent: Optional[float],
                          chain: Optional[ChainConcentration] = None,
                          source: Optional[str] = None) -> Resolution:
    """Pick the number the GATE sees, and say where it came from.

    Does NOT fall back across definitions. If the configured source has no
    measurement the answer is an absence, because the ceiling is calibrated
    against one definition and silently applying it to another is the exact
    divergence this module exists to end.
    """
    definition = (source or CONCENTRATION_SOURCE or "provider").strip().lower()
    if definition not in ("provider", "chain_wallet", "chain_raw"):
        logger.warning(
            f"Unknown HOLDER_CONCENTRATION_SOURCE {definition!r} -- falling back to "
            f"'provider'. Valid values: provider, chain_wallet, chain_raw.")
        definition = "provider"

    if definition == "provider":
        value = provider_percent
    else:
        value = chain.percent_for(definition) if chain is not None else None

    return Resolution(percent=value, source=definition, missing=value is None)


def snapshot_fields(provider_percent: Optional[float],
                    chain: Optional[ChainConcentration] = None,
                    source: Optional[str] = None) -> Dict[str, Any]:
    """The concentration-related keys every provider's snapshot merges in.

    `top_10_holder_percentage` keeps its historic 0.0-when-absent shape --
    the gates read it only after checking `_holder_data_missing`, and
    changing the shape here would be a change to audited behaviour for no
    gain. The observed-but-unused measurements ride alongside so the
    definition can be chosen from the real population later.
    """
    resolved = resolve_concentration(provider_percent, chain, source)
    fields: Dict[str, Any] = {
        "top_10_holder_percentage": resolved.percent if resolved.percent is not None else 0.0,
        "_holder_data_missing": resolved.missing,
        "holder_concentration_source": resolved.source,
        "holder_concentration_provider_pct": provider_percent,
        "holder_concentration_raw_pct": chain.raw_percent if chain else None,
        "holder_concentration_wallet_pct": chain.wallet_percent if chain else None,
        "holder_concentration_program_pct": chain.program_percent if chain else None,
        "holder_concentration_burn_pct": chain.burn_percent if chain else None,
    }
    return fields


async def observe_chain(client: httpx.AsyncClient,
                        token_address: str) -> Optional[ChainConcentration]:
    """Chain measurement when it is needed or being observed, else None.

    Needed = the gate is configured to use it. Observed = OBSERVE_CHAIN. A
    failure is never fatal: the caller gets a ChainConcentration carrying
    `error`, and resolve_concentration turns that into an absence only if
    the gate was actually depending on it.
    """
    if not (OBSERVE_CHAIN or CONCENTRATION_SOURCE.startswith("chain")):
        return None
    try:
        return await fetch_chain_concentration(client, token_address)
    except Exception as e:  # defensive: an observation must never break a tick
        logger.warning(f"Chain concentration observation failed for {token_address}: {e}")
        return ChainConcentration(error=f"{type(e).__name__}: {e}")
