"""Fill reconstruction and barrier geometry.

The defect these guard against is not a crash. It is a plausible number: a
fill price derived from a defaulted decimal count, or a stop that rounds onto
the entry. Both produce values that look entirely ordinary downstream, which
is exactly why they need pinning here.
"""
from tests.harness import Suite
import fill_accounting as fa

OWNER = "OwnerWa11et"
TOKEN = "TokenMint111"
QUOTE = "QuoteMint111"


def meta(pre_tok, post_tok, pre_q, post_q, *, tok_dec=9, q_dec=6,
         owner=OWNER, fee=5000, drop_pre=False, drop_post=False,
         tok_dec_missing=False):
    def row(mint, amount, decimals):
        ui = {"amount": str(amount)}
        if decimals is not None:
            ui["decimals"] = decimals
        return {"owner": owner, "mint": mint, "uiTokenAmount": ui}
    pre = None if drop_pre else [row(TOKEN, pre_tok, None if tok_dec_missing else tok_dec),
                                 row(QUOTE, pre_q, q_dec)]
    post = None if drop_post else [row(TOKEN, post_tok, None if tok_dec_missing else tok_dec),
                                   row(QUOTE, post_q, q_dec)]
    return {"preTokenBalances": pre, "postTokenBalances": post, "fee": fee}


def run() -> Suite:
    s = Suite("fill accounting")

    # --- a clean buy: 50 USDC out, 1000 tokens in ---
    f = fa.reconstruct_fill(meta(0, 1000 * 10**9, 50 * 10**6, 0),
                            owner=OWNER, token_mint=TOKEN, quote_mint=QUOTE)
    s.check_true("a clean buy reconstructs", f is not None)
    s.check_true("the buy direction is read from the sign of the token delta", f.is_buy)
    s.check("tokens received are scaled by the token's own decimals", f.tokens, 1000.0)
    s.check("quote spent is scaled by the quote's own decimals", f.quote_amount, 50.0)
    s.check("fill price is quote spent over tokens received", round(f.fill_price, 10), 0.05)
    s.check("the network fee is carried through", f.fee_lamports, 5000)

    # Decimals must come from the transaction. A 6-decimal token priced with a
    # defaulted 9 is wrong by 1000x and looks completely normal.
    f6 = fa.reconstruct_fill(meta(0, 1000 * 10**6, 50 * 10**6, 0, tok_dec=6),
                             owner=OWNER, token_mint=TOKEN, quote_mint=QUOTE)
    s.check("a 6-decimal token prices identically to a 9-decimal one",
            round(f6.fill_price, 10), 0.05)
    s.check_true("unreadable decimals must refuse rather than default",
                 fa.reconstruct_fill(meta(0, 1000, 50 * 10**6, 0, tok_dec_missing=True),
                                     owner=OWNER, token_mint=TOKEN, quote_mint=QUOTE) is None)

    # --- a clean sell ---
    f = fa.reconstruct_fill(meta(1000 * 10**9, 0, 0, 60 * 10**6),
                            owner=OWNER, token_mint=TOKEN, quote_mint=QUOTE)
    s.check_true("a sell reconstructs", f is not None)
    s.check_true("a sell is not flagged as a buy", not f.is_buy)
    s.check("a sell prices from quote received over tokens sold", round(f.fill_price, 10), 0.06)

    # --- everything that must refuse ---
    for kwargs, label in [
        (dict(tx_meta=None), "no transaction metadata"),
        (dict(owner=None), "no owner"),
        (dict(token_mint=None), "no token mint"),
        (dict(quote_mint=None), "no quote mint"),
    ]:
        args = dict(tx_meta=meta(0, 1000 * 10**9, 50 * 10**6, 0),
                    owner=OWNER, token_mint=TOKEN, quote_mint=QUOTE)
        args.update(kwargs)
        s.check_true(f"{label} must refuse", fa.reconstruct_fill(**args) is None)

    s.check_true("missing pre-balances must refuse",
                 fa.reconstruct_fill(meta(0, 1000, 50, 0, drop_pre=True),
                                     owner=OWNER, token_mint=TOKEN, quote_mint=QUOTE) is None)
    s.check_true("a token balance that did not move must refuse",
                 fa.reconstruct_fill(meta(0, 0, 50 * 10**6, 0),
                                     owner=OWNER, token_mint=TOKEN, quote_mint=QUOTE) is None)
    s.check_true("a quote balance that did not move must refuse",
                 fa.reconstruct_fill(meta(0, 1000 * 10**9, 50 * 10**6, 50 * 10**6),
                                     owner=OWNER, token_mint=TOKEN, quote_mint=QUOTE) is None)
    s.check_true("both sides moving the same way is not a swap",
                 fa.reconstruct_fill(meta(0, 1000 * 10**9, 0, 50 * 10**6),
                                     owner=OWNER, token_mint=TOKEN, quote_mint=QUOTE) is None)
    s.check_true("another wallet's balances must not be read as ours",
                 fa.reconstruct_fill(meta(0, 1000 * 10**9, 50 * 10**6, 0, owner="SomeoneElse"),
                                     owner=OWNER, token_mint=TOKEN, quote_mint=QUOTE) is None)

    # --- realised P&L from two measured fills ---
    entry = fa.reconstruct_fill(meta(0, 1000 * 10**9, 50 * 10**6, 0),
                                owner=OWNER, token_mint=TOKEN, quote_mint=QUOTE)
    exit_ = fa.reconstruct_fill(meta(1000 * 10**9, 0, 0, 60 * 10**6),
                                owner=OWNER, token_mint=TOKEN, quote_mint=QUOTE)
    s.check("P&L is what came back minus what went out", fa.realized_pnl_usd(entry, exit_), 10.0)
    s.check_true("an unknown exit fill yields no P&L, not a zero",
                 fa.realized_pnl_usd(entry, None) is None)

    # --- barrier geometry at every magnitude ---
    # This is the defect that fixed-decimal rounding produces: at five decimals
    # anything under ~1e-4 collapses, at eight anything under ~1e-7 does.
    for price in [1e-2, 1e-4, 5e-6, 1e-6, 1e-7, 2e-9, 5e-11]:
        levels = fa.barrier_levels(price, stop_loss_percent=14.0, take_profit_percent=7.0)
        s.check_true(f"levels stay strictly ordered at price {price:.0e}",
                     levels is not None
                     and 0 < levels["stop_loss_price"] < levels["entry_price"] < levels["take_profit_price"])

    levels = fa.barrier_levels(1e-7, stop_loss_percent=14.0, take_profit_percent=7.0)
    s.check_true("the target sits the right distance above entry at 1e-7",
                 abs(levels["take_profit_price"] / levels["entry_price"] - 1.07) < 1e-6)
    s.check_true("the stop sits the right distance below entry at 1e-7",
                 abs(1 - levels["stop_loss_price"] / levels["entry_price"] - 0.14) < 1e-6)

    s.check_true("a zero entry price yields no levels", fa.barrier_levels(0.0, stop_loss_percent=14, take_profit_percent=7) is None)
    s.check_true("a missing entry price yields no levels", fa.barrier_levels(None, stop_loss_percent=14, take_profit_percent=7) is None)
    s.check_true("a negative entry price yields no levels", fa.barrier_levels(-1.0, stop_loss_percent=14, take_profit_percent=7) is None)
    s.check_true("a zero-width band is refused rather than recorded",
                 fa.barrier_levels(1e-7, stop_loss_percent=0.0, take_profit_percent=0.0) is None)
    s.check_true("a 100% stop would put the floor at zero and is refused",
                 fa.barrier_levels(1e-7, stop_loss_percent=100.0, take_profit_percent=7.0) is None)

    return s
