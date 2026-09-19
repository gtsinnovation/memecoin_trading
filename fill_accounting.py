"""What a trade actually did, read from the committed transaction.

WHY THIS EXISTS
There are two ways to record what a trade cost. One is to take the price the
strategy intended and write that down. The other is to read the wallet's token
balances before and after the confirmed transaction and derive the price from
what actually moved.

Only the second is a measurement. The first is a restatement of the plan, and
if the plan and the fill differ -- which on this asset class they routinely do,
by several percent -- every downstream number inherits that gap: entry price,
stop distance, target distance, realised P&L, and the cohort statistics built
on top of them. An intended price that is 7% better than the real one makes
every position look 7% better than it was, forever, in a way no later analysis
can detect or undo.

So this module derives the fill from balance deltas and nothing else.

FAIL CLOSED, PARTICULARLY ON DECIMALS
Token decimals are read from the transaction. They are NOT defaulted. A
plausible-looking default of 9 applied to a 6-decimal token misprices the fill
by a factor of a thousand, and the resulting number looks entirely ordinary --
it is a price, it is positive, it sorts correctly. Nothing downstream would
catch it. When decimals cannot be read, this module returns None and the caller
must refuse to record a fill rather than record a confident wrong one.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


DEFAULT_SIGNIFICANT_FIGURES = 10


def round_significant(value: Optional[float],
                      significant_figures: int = DEFAULT_SIGNIFICANT_FIGURES) -> Optional[float]:
    """Round to significant figures rather than decimal places.

    Decimal-place rounding is the wrong tool for prices that span many orders
    of magnitude. round(x, 5) on a token at 1e-4 returns the same value for
    both a 7% and a 14% offset from it; below about 5e-6 it returns 0.0 for
    everything. Significant figures keep the same relative precision at every
    magnitude, which is what price levels actually need.
    """
    if value is None:
        return None
    if value == 0:
        return 0.0
    from math import floor, log10
    digits = significant_figures - int(floor(log10(abs(value)))) - 1
    return round(value, digits)


@dataclass
class Fill:
    """A completed trade, entirely derived from on-chain balance deltas."""
    token_delta_raw: int       # signed: positive on a buy, negative on a sell
    quote_delta_raw: int       # signed: negative on a buy, positive on a sell
    token_decimals: int
    quote_decimals: int
    fee_lamports: int

    @property
    def tokens(self) -> float:
        return abs(self.token_delta_raw) / (10 ** self.token_decimals)

    @property
    def quote_amount(self) -> float:
        return abs(self.quote_delta_raw) / (10 ** self.quote_decimals)

    @property
    def fill_price(self) -> Optional[float]:
        """Quote currency per token. None when the division is meaningless."""
        tokens = self.tokens
        if tokens <= 0:
            return None
        return self.quote_amount / tokens

    @property
    def is_buy(self) -> bool:
        return self.token_delta_raw > 0


def _balance_raw(rows: Optional[List[Dict[str, Any]]],
                 owner: str, mint: str) -> Optional[int]:
    """Our raw balance of one mint. None when the transaction does not say.

    Absent is not zero. A wallet with no token account for a mint and a wallet
    whose balance the RPC did not report look identical here, and only one of
    them is safe to treat as zero -- so neither is.
    """
    if rows is None:
        return None
    total = None
    for row in rows:
        if row.get("owner") != owner or row.get("mint") != mint:
            continue
        raw = (row.get("uiTokenAmount") or {}).get("amount")
        if raw is None:
            continue
        try:
            total = (total or 0) + int(raw)
        except (TypeError, ValueError):
            return None
    return total


def _decimals_for(rows_sets: List[Optional[List[Dict[str, Any]]]], mint: str) -> Optional[int]:
    for rows in rows_sets:
        for row in rows or []:
            if row.get("mint") != mint:
                continue
            value = (row.get("uiTokenAmount") or {}).get("decimals")
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def reconstruct_fill(tx_meta: Optional[Dict[str, Any]], *,
                     owner: Optional[str],
                     token_mint: Optional[str],
                     quote_mint: Optional[str]) -> Optional[Fill]:
    """Derive the fill from a confirmed transaction's metadata.

    Returns None whenever any input needed for an honest number is missing.
    The caller must treat None as "this trade's fill is unknown" -- which for a
    live position means it needs reconciling by hand, not filling in with the
    quoted price.
    """
    if not tx_meta or not owner or not token_mint or not quote_mint:
        return None

    pre = tx_meta.get("preTokenBalances")
    post = tx_meta.get("postTokenBalances")
    # Defence in depth, and known to be so: mutation testing shows that
    # removing this guard does not change any observable result, because a
    # transaction with no pre-balance rows computes both deltas from an assumed
    # zero and is then refused by the opposite-sign check below. It stays
    # because relying on that coincidence would make a later change to the
    # sign check silently load-bearing for something unrelated.
    if pre is None or post is None:
        return None

    token_decimals = _decimals_for([post, pre], token_mint)
    quote_decimals = _decimals_for([post, pre], quote_mint)
    if token_decimals is None or quote_decimals is None:
        return None

    # A mint absent from `pre` genuinely means a zero starting balance only
    # when the other side of the pair was reported, which establishes that the
    # RPC did return balance rows for this wallet.
    pre_token = _balance_raw(pre, owner, token_mint)
    post_token = _balance_raw(post, owner, token_mint)
    pre_quote = _balance_raw(pre, owner, quote_mint)
    post_quote = _balance_raw(post, owner, quote_mint)

    if post_token is None or post_quote is None:
        return None
    if pre_token is None:
        pre_token = 0
    if pre_quote is None:
        pre_quote = 0

    token_delta = post_token - pre_token
    quote_delta = post_quote - pre_quote
    if token_delta == 0 or quote_delta == 0:
        # A swap that moved one side but not the other did not fill.
        return None
    if (token_delta > 0) == (quote_delta > 0):
        # Both sides moving the same way is not a swap.
        return None

    try:
        fee = int(tx_meta.get("fee") or 0)
    except (TypeError, ValueError):
        fee = 0

    return Fill(token_delta_raw=token_delta, quote_delta_raw=quote_delta,
                token_decimals=token_decimals, quote_decimals=quote_decimals,
                fee_lamports=fee)


def realized_pnl_usd(entry_fill: Optional[Fill], exit_fill: Optional[Fill]) -> Optional[float]:
    """Profit in quote currency: what came back minus what went out.

    Derived from two measured fills, so it needs no assumed cost model, no
    slippage estimate and no fee constant -- those are already inside the
    numbers. None when either fill is unknown.
    """
    if entry_fill is None or exit_fill is None:
        return None
    return exit_fill.quote_amount - entry_fill.quote_amount


def barrier_levels(entry_price: Optional[float], *,
                   stop_loss_percent: float,
                   take_profit_percent: float,
                   significant_figures: int = 10) -> Optional[Dict[str, float]]:
    """Stop and target from a measured entry price, at full precision.

    Rounded to SIGNIFICANT FIGURES rather than decimal places. Fixed-decimal
    rounding collapses at small prices: at eight decimals a token priced at
    1e-7 has its stop, entry and target round to the same number, and at five
    decimals anything under about 1e-4 does. A collapsed set means the exit
    test fires on the first mark, booking an instant close that never happened.

    Returns None when the resulting levels are not strictly ordered around the
    entry -- a degenerate set must be refused, not recorded.
    """
    if entry_price is None or not (entry_price > 0):
        return None

    def sig(value: float) -> float:
        return round_significant(value, significant_figures)

    stop = sig(entry_price * (1 - stop_loss_percent / 100.0))
    target = sig(entry_price * (1 + take_profit_percent / 100.0))
    entry = sig(entry_price)

    if not (0 < stop < entry < target):
        return None
    return {"entry_price": entry, "stop_loss_price": stop, "take_profit_price": target}
