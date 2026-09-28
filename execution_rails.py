"""Pre-signature rails: the last refusals before real funds move.

WHY THIS IS SEPARATE FROM THE GATES
The gate network answers "is this token worth buying?". These rails answer a
different question -- "is this ACCOUNT in a state where a buy should be signed
right now?" -- and the answer does not depend on the token at all. Wallet
balances, daily order counts and mode are properties of the operator, so they
belong outside the token pipeline and run after it.

DEFENCE IN DEPTH
ABSOLUTE_MAX_POSITION_USD is a constant in source, deliberately above the
configurable ceiling. Config is data: it is edited through a web form, it lives
in a database row, and a single mistyped number there can raise a limit without
review. This constant cannot be raised that way, so it is the backstop that a
bad row cannot lift. It is not a tuning knob and it should be changed only in a
reviewed commit.

ASYMMETRY IS INTENTIONAL
Entries are rail-gated. Exits are not, and must never be: refusing to sell
strands a live position, which is strictly worse than a buy that did not
happen. A refused entry costs nothing. There is deliberately no
`check_exit_rails` in this file.

FAIL CLOSED ON UNMEASURABLE
Every input is Optional and None means "we could not read this". None refuses.
A balance that could not be fetched is not a balance of zero and is certainly
not a balance that is sufficient.
"""
import math
from dataclasses import dataclass
from typing import Optional

# The ceiling no database row can raise. See module docstring.
ABSOLUTE_MAX_POSITION_USD = 250.0

# Fees plus rent for a fresh associated token account. Below this a swap
# cannot land at all, so there is no point signing one.
MIN_SOL_LAMPORTS_FOR_FEES = 7_000_000  # 0.007 SOL


@dataclass
class RailVerdict:
    ok: bool
    reason: Optional[str] = None
    approved_size_usd: Optional[float] = None


def _refuse(reason: str) -> RailVerdict:
    return RailVerdict(ok=False, reason=reason)


def _finite(value) -> Optional[float]:
    """float(value) when it is a finite number, else None.

    NaN is the reason this exists: every comparison against it is False, so
    `nan > ceiling`, `balance < size` and `count >= cap` all PASS. A NaN
    ceiling, balance or count read as the most permissive value possible.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def position_ceiling_usd(live_max_position_usd: Optional[float],
                         max_position_usd: Optional[float]) -> float:
    """The smallest of the configured ceilings and the hard constant.

    Any ceiling that is missing or non-positive contributes 0, which refuses.
    An absent limit is not an absent restriction.
    """
    candidates = []
    for value in (live_max_position_usd, max_position_usd):
        v = _finite(value)
        # min() with a NaN in first position returns NaN, and every later
        # "size > ceiling" test is then False -- an unlimited ceiling.
        if v is None:
            return 0.0
        candidates.append(v)
    candidates.append(ABSOLUTE_MAX_POSITION_USD)
    return max(0.0, min(candidates))


def check_entry_rails(*,
                      mode: Optional[str],
                      run_status: Optional[str],
                      requested_usd: Optional[float],
                      live_max_position_usd: Optional[float],
                      max_position_usd: Optional[float],
                      orders_sent_today: Optional[int],
                      max_orders_per_day: Optional[int],
                      sol_lamports: Optional[int],
                      quote_balance_usd: Optional[float],
                      quote_balance_required: bool = True) -> RailVerdict:
    """Every refusal that can stop an entry, evaluated before anything is signed.

    Pure: it measures nothing itself. The caller fetches balances and counts and
    passes them in, which is what makes every branch here testable without a
    wallet, an RPC endpoint or a database.

    quote_balance_required=False is for the devnet self-transfer ONLY, which
    spends lamports and no quote currency. Every swap path must leave it True.
    """
    if mode is None:
        return _refuse("trading mode unreadable")
    if str(mode).upper() != "LIVE":
        return _refuse(f"agent is not in LIVE mode (mode={mode})")

    if run_status is None:
        return _refuse("run status unreadable")
    if str(run_status).upper() != "RUNNING":
        return _refuse(f"entries blocked: {run_status}")

    requested = _finite(requested_usd)
    if requested is None or not requested > 0:
        return _refuse("requested size is missing, non-finite or not positive")

    ceiling = position_ceiling_usd(live_max_position_usd, max_position_usd)
    if ceiling <= 0:
        return _refuse("position ceiling is zero or unreadable")
    if requested > ceiling:
        return _refuse(f"size ${requested:,.2f} exceeds ceiling ${ceiling:,.2f}")

    # A cap of zero means no orders are permitted. Only an explicitly absent
    # cap is treated as unlimited, and even that is refused rather than
    # assumed -- an unreadable cap is not an infinite one.
    cap = _finite(max_orders_per_day)
    if cap is None:
        return _refuse("daily order cap unreadable")
    if cap <= 0:
        return _refuse("daily order cap is zero -- no live orders permitted")
    sent = _finite(orders_sent_today)
    if sent is None or sent < 0:
        return _refuse("today's order count unreadable")
    if sent >= cap:
        return _refuse(f"daily order cap reached ({int(sent)}/{int(cap)})")

    lamports = _finite(sol_lamports)
    if lamports is None or lamports < 0:
        return _refuse("SOL balance unreadable")
    if lamports < MIN_SOL_LAMPORTS_FOR_FEES:
        return _refuse(f"SOL too low for fees ({lamports / 1e9:.4f} SOL)")

    if quote_balance_required:
        balance = _finite(quote_balance_usd)
        if balance is None:
            return _refuse("quote-currency balance unreadable")
        if balance < requested:
            return _refuse(f"balance ${balance:,.2f} below size ${requested:,.2f}")

    return RailVerdict(ok=True, approved_size_usd=min(requested, ceiling))


def requote_still_acceptable(quoted_impact_percent: Optional[float],
                             tolerance_percent: Optional[float]) -> RailVerdict:
    """Re-check the route immediately before signing.

    The slippage gate measured impact when the token was evaluated, which on
    this asset class may be a minute or more ago and several percent away. This
    is the check against the quote actually being signed, not the one that won
    the token its place in the queue.
    """
    tolerance = _finite(tolerance_percent)
    if tolerance is None:
        return _refuse("slippage tolerance unreadable")
    impact = _finite(quoted_impact_percent)
    if impact is None:
        return _refuse("re-quote returned no usable price impact -- refusing to sign blind")
    if abs(impact) > tolerance:
        return _refuse(f"route degraded to {abs(impact):.2f}% impact, "
                       f"above {tolerance:.2f}% tolerance")
    return RailVerdict(ok=True)
