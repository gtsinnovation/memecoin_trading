# paper_trading.py
"""Stage 2: paper trading against real prices.

WHAT THIS IS FOR
One question: do the pipeline's gates have any edge? Everything built so
far -- real market data, rug signals, a signer service, honest depth
figures -- improves the INPUTS to four threshold gates whose constants
were chosen by hand and never validated. No amount of infrastructure
makes a losing rule set profitable, and until this experiment runs there
is no evidence either way.

WHY THE OLD NUMBERS COULDN'T ANSWER IT
evaluate_open_positions() used to walk each position with a bounded
random walk (_simulate_next_price). Every win rate and P&L figure the
dashboard has ever shown was therefore noise. Marking to real prices is
the whole point of this stage.

THE THREE THINGS THAT MAKE IT AN EXPERIMENT RATHER THAN A DEMO

1. A CONTROL GROUP. Every evaluated token is recorded, tagged APPROVED or
   REJECTED. "Approved trades returned X%" is meaningless alone; it only
   means something against what the rejected ones did. If the gates have
   edge, APPROVED should beat REJECTED.

2. TWO ENTRY MODELS. D_PULSE sets entry at a 7% pullback, and the live
   pipeline books the position at that price immediately -- assuming a
   fill that may never happen. So each candidate is recorded twice: once
   IMMEDIATE (entered at the evaluation price) and once LIMIT (fills only
   if price actually reaches the trigger, expiring unfilled otherwise).
   The comparison settles empirically whether the pullback helps.

   Expect it to hurt. The tokens that retrace far enough to fill a limit
   order are disproportionately the ones still falling -- adverse
   selection. If LIMIT underperforms IMMEDIATE, that is the finding, and
   it would be invisible to a backtest that assumed fills.

3. COSTS DEDUCTED. Slippage on both sides plus a DEX fee. Without them,
   results are systematically optimistic in exactly the direction that
   makes a bad strategy look viable.

WHAT IT STILL WON'T TELL YOU
Price impact of our own order (we are a price taker in the model but not
in reality), MEV/sandwich losses, and failed-transaction costs. All three
make live results worse than paper. Treat paper P&L as an optimistic
upper bound, not a forecast.
"""
import os
import logging
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger("paper_trading")

# Round-trip DEX fee, charged per side. Solana AMM fees are commonly
# 0.25-1%; the default is deliberately at the low end so the experiment
# isn't accused of being rigged pessimistic -- raise it to be stricter.
PAPER_FEE_PERCENT_PER_SIDE = float(os.environ.get("PAPER_FEE_PERCENT_PER_SIDE", "0.25"))

# How long a LIMIT candidate waits for its pullback before being written
# off as never-filled. Real limit orders don't sit forever, and letting
# them wait indefinitely would quietly bias the sample toward tokens that
# eventually crashed into the trigger.
LIMIT_FILL_WINDOW_MINUTES = float(os.environ.get("LIMIT_FILL_WINDOW_MINUTES", "60"))

# Positions that neither hit target nor stop are closed at market after
# this long, so capital-equivalent isn't tied up forever and every trade
# eventually produces a result to measure.
MAX_HOLD_MINUTES = float(os.environ.get("PAPER_MAX_HOLD_MINUTES", "360"))

# After a token's paper trade resolves, how long before the same token may be
# recorded again. Without this (and without the live-trade guard below) the
# pipeline records a fresh pair of rows on EVERY tick, so one token evaluated
# for an hour becomes hundreds of overlapping trades sharing one price path.
# They are not independent observations: they inflate the sample size, shrink
# the apparent variance, and let a handful of tokens masquerade as a
# statistically meaningful result. This is the difference between measuring
# the gates and measuring the same coin flip repeatedly.
REENTRY_COOLDOWN_MINUTES = float(os.environ.get("PAPER_REENTRY_COOLDOWN_MINUTES", "60"))

# How long a live trade may go WITHOUT A PRICE before it is abandoned.
#
# This closes a silent, one-directional bias. Both terminal checks in
# mark_to_market() -- the LIMIT expiry and the barrier timeout -- sit
# downstream of `if price is None: continue`, so a trade whose token stops
# pricing could never reach any end state. It stayed OPEN forever.
#
# The tokens that stop pricing are not a random sample. A token whose pair
# is delisted, whose liquidity is pulled, or that simply dies is the WORST
# outcome in the population -- and those were exactly the ones excluded from
# every average, because results_summary() reads only status = 'CLOSED'.
# The reported P&L was therefore biased upward by an unknown amount, and
# invisibly so.
#
# Abandoned trades get their own status rather than a fabricated exit price.
# Closing them at a made-up number would violate the missing-price rule that
# runs through this whole module; leaving them live would keep the bias.
# ABANDONED is excluded from the P&L averages AND counted separately, so the
# dropout is visible instead of silent.
UNPRICEABLE_ABANDON_MINUTES = float(os.environ.get("PAPER_UNPRICEABLE_ABANDON_MINUTES", "180"))

# Elapsed times at which each evaluated token's actual return is recorded,
# independently of the barrier trade -- see paper_horizon_returns in
# schema.sql for why this second measurement exists at all.
HORIZONS_MINUTES = tuple(sorted(
    int(x.strip()) for x in os.environ.get("PAPER_HORIZONS_MINUTES", "30,60,120").split(",")
    if x.strip()
))

# A mark can only be taken on a tick, and a token that stops pricing for a
# while gets its mark late. Marks later than this multiple of the horizon are
# still recorded -- age_minutes_at_mark keeps them honest -- but the summary
# excludes them, because a 90-minute-old price is not a 30-minute return.
HORIZON_TOLERANCE = float(os.environ.get("PAPER_HORIZON_TOLERANCE", "1.5"))


def _max_horizon_window() -> float:
    """How long a token stays interesting for horizon marking."""
    return (max(HORIZONS_MINUTES) * HORIZON_TOLERANCE) if HORIZONS_MINUTES else 0.0


LEVEL_SIGNIFICANT_FIGURES = 10


def _round_sig(value: float, sig: int = LEVEL_SIGNIFICANT_FIGURES) -> float:
    """Round to significant figures, not to a fixed number of decimals.

    WHY THIS IS NOT round(value, 8)
    Fixed decimal places assume a price of order 1. Memecoins are routinely
    quoted at 1e-7 and below, where 8 decimal places leave one significant
    figure or none at all, and the barrier levels stop being levels:

        price 1e-7  -> entry = stop = target = 9e-08   (all three equal)
        price 2e-9  -> entry = stop = target = 0.0     (all three zero)

    Both produce a FABRICATED outcome on the very next mark, because
    mark_to_market() tests `price >= target`. At 1e-7 that is true
    immediately and the trade closes TARGET_HIT at 10% BELOW the evaluation
    price -- a loss recorded as a take-profit. At 2e-9 the target is 0.0, so
    `price >= 0.0` is true for every possible price and the trade closes
    TARGET_HIT at an exit price of zero: a fabricated -100%, labelled a win.

    Significant figures keep the 7%/14% geometry intact at any magnitude a
    float can represent.
    """
    if value == 0.0 or value != value or value in (float("inf"), float("-inf")):
        return 0.0
    import math
    exponent = math.floor(math.log10(abs(value)))
    return round(value, -(exponent) + (sig - 1))


def levels_are_sane(price: float, entry: float, stop: float,
                    target: float) -> bool:
    """A degenerate barrier set must never be recorded as a trade.

    The ordering stop < entry < target is the whole content of the
    experiment's barrier arm: if it collapses, every outcome derived from it
    is an artefact of arithmetic rather than a fact about the token. Checked
    explicitly rather than assumed, because the fixed-decimal rounding above
    silently produced degenerate sets for months and nothing noticed -- they
    close instantly and look like ordinary wins and losses in the table.
    """
    values = (price, entry, stop, target)
    if any(v is None or v != v or v in (float("inf"), float("-inf")) for v in values):
        return False
    if any(v <= 0 for v in values):
        return False
    if not (stop < entry < target):
        return False
    # Ordering alone is not enough. A set can be correctly ordered and still
    # sit entirely below the evaluation price -- target 9.5e-8 against a price
    # of 1e-7 is ordered, and still closes TARGET_HIT on the very next mark
    # because mark_to_market tests `price >= target`. The target has to be
    # somewhere the price has not already been.
    return target > price


def compute_levels(price: float) -> Tuple[float, float, float]:
    """Entry trigger, stop, and target for a candidate.

    Deliberately mirrors node_D_PULSE's formula (7% pullback entry, 14%
    stop, take-profit at 2:1 reward:risk) but is computed here for EVERY
    evaluated token, including ones rejected before D_PULSE ever ran.
    Otherwise the control cohort would have no levels and there would be
    nothing to compare against.

    Rounded to significant figures rather than decimal places -- see
    _round_sig() for what fixed decimals did to sub-1e-7 tokens.
    """
    entry = _round_sig(price * 0.93)
    stop = _round_sig(price * 0.86)
    target = _round_sig(entry + (entry - stop) * 2.0)
    return entry, stop, target


def total_cost_percent(assumed_slippage_percent: Optional[float]) -> float:
    """Round-trip cost as a percentage: fee and slippage on entry and exit.

    Slippage is charged on both sides using the figure measured at
    evaluation. That's an approximation -- exit slippage is really a
    function of depth at exit time, which we don't know -- but charging
    it twice is closer to the truth than charging it once, and erring
    toward higher costs is the right direction for an experiment whose
    failure mode is flattering the strategy.
    """
    slippage = abs(float(assumed_slippage_percent or 0.0))
    return round((PAPER_FEE_PERCENT_PER_SIDE * 2.0) + (slippage * 2.0), 4)


def net_pnl_percent(fill_price: float, exit_price: float,
                      assumed_slippage_percent: Optional[float]) -> Tuple[float, float, float]:
    """Returns (gross_pct, cost_pct, net_pct) for a completed trade."""
    if not fill_price or fill_price <= 0:
        return 0.0, 0.0, 0.0
    gross = ((float(exit_price) / float(fill_price)) - 1.0) * 100.0
    cost = total_cost_percent(assumed_slippage_percent)
    return round(gross, 4), cost, round(gross - cost, 4)


def record_candidate(conn, snapshot: Dict[str, Any], final_state: Dict[str, Any]) -> None:
    """Writes both entry-model rows for one evaluated token.

    Called for EVERY evaluation, approved or rejected -- that is what
    creates the control group. `conn` is a psycopg2 connection (this runs
    in the same worker thread as the rest of the engine's DB work).
    """
    price = float(snapshot.get("current_price") or 0.0)
    if price <= 0:
        return

    termination = final_state.get("termination_reason")
    cohort = "REJECTED" if termination else "APPROVED"
    rejected_by = None
    if termination:
        # Reasons are formatted "<GATE>: Short-circuit. ..." by each node.
        rejected_by = str(termination).split(":", 1)[0].strip()[:40]

    token_address = snapshot.get("token_address")
    if not token_address:
        return

    # One observation per token per opportunity -- see REENTRY_COOLDOWN_MINUTES.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM paper_trades
            WHERE token_address = %s
              AND (status IN ('PENDING_FILL', 'OPEN')
                   OR evaluated_at > CURRENT_TIMESTAMP - (%s * INTERVAL '1 minute'))
            LIMIT 1;
        """, (token_address, REENTRY_COOLDOWN_MINUTES))
        if cur.fetchone():
            return

    entry, stop, target = compute_levels(price)
    if not levels_are_sane(price, entry, stop, target):
        # Refusing beats recording. A degenerate set closes on the next mark
        # with a fabricated outcome, and that outcome is indistinguishable
        # from a real one in every statistic the experiment reports.
        logger.warning(
            f"Degenerate barrier levels for {snapshot.get('token_symbol')} at price {price!r} "
            f"(entry={entry!r}, stop={stop!r}, target={target!r}) -- not recording this "
            f"candidate rather than booking an outcome the levels invented.")
        return
    slippage = snapshot.get("estimated_slippage_percent")

    # Liveness at evaluation time. Recorded, not filtered on: a token that
    # wasn't trading can still print a 7% "move" off one late fill, and those
    # artifacts dilute any real signal rather than creating a false one.
    # Keeping the numbers lets the results be segmented by activity later.
    def _num(key):
        v = snapshot.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # Unmeasured slippage is stored as NULL, never 0. fetch_price_impact_pct()
    # returns None on any Jupiter failure (rate-limits are the common case),
    # and the snapshot coerces that to 0.0 for the gates. Persisting 0 here
    # would charge a zero round-trip cost -- indistinguishable from a token
    # that genuinely trades for free, and biased toward flattering the thin
    # tokens whose real costs are largest.
    if snapshot.get("slippage_data_missing"):
        slippage = None

    volume_h1 = _num("volume_h1_usd")
    depth = _num("tradeable_depth_usd")
    buys, sells = _num("txns_h1_buys"), _num("txns_h1_sells")
    txns = int((buys or 0) + (sells or 0)) if (buys is not None or sells is not None) else None

    # Short-window features. Stored raw rather than pre-combined into a ratio:
    # a stored ratio bakes in one definition of "imbalance" before we know
    # which one predicts anything, and it throws away the counts that say
    # whether the ratio means anything at all (3 buys vs 1 sell is the same
    # ratio as 300 vs 100 and carries none of the confidence).
    volume_m5 = _num("volume_m5_usd")
    m5_buys, m5_sells = _num("txns_m5_buys"), _num("txns_m5_sells")
    chg_m5, chg_h1 = _num("price_change_m5"), _num("price_change_h1")

    rows = [
        # IMMEDIATE is filled on the spot at the evaluation price.
        ("IMMEDIATE", "OPEN", price),
        # LIMIT waits for the pullback and may never fill.
        ("LIMIT", "PENDING_FILL", None),
    ]
    with conn.cursor() as cur:
        for entry_model, status, fill_price in rows:
            # The partial unique index in migrate.sql is the backstop against a
            # race between two ticks. A violation means "already recorded",
            # which is success, not failure -- but an unhandled one would abort
            # the caller's whole transaction, so each insert gets a savepoint.
            cur.execute("SAVEPOINT paper_row;")
            try:
                cur.execute("""
                INSERT INTO paper_trades (
                    token_address, token_symbol, cohort, rejected_by, entry_model, status,
                    price_at_evaluation, entry_trigger_price, target_exit_price,
                    invalidation_level_price, assumed_slippage_percent,
                    fill_price, filled_at, last_price, last_marked_at,
                    volume_h1_usd, txns_h1, txns_h1_buys, txns_h1_sells,
                    tradeable_depth_usd,
                    volume_m5_usd, txns_m5_buys, txns_m5_sells,
                    price_change_m5, price_change_h1
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                          CASE WHEN %s IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END,
                          %s, CURRENT_TIMESTAMP, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s);
                """, (
                    token_address, snapshot.get("token_symbol"),
                    cohort, rejected_by, entry_model, status,
                    price, entry, target, stop, slippage,
                    fill_price, fill_price, price,
                    volume_h1, txns,
                    int(buys) if buys is not None else None,
                    int(sells) if sells is not None else None,
                    depth,
                    volume_m5,
                    int(m5_buys) if m5_buys is not None else None,
                    int(m5_sells) if m5_sells is not None else None,
                    chg_m5, chg_h1,
                ))
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT paper_row;")
                logger.debug(f"Skipped duplicate paper row for {token_address} {entry_model}: {e}")
            else:
                cur.execute("RELEASE SAVEPOINT paper_row;")


def open_token_addresses(conn) -> List[str]:
    """Every token still needing a price mark, for either measurement.

    Two reasons a token qualifies, and the second is easy to miss: a barrier
    trade that is still live, OR an evaluation whose fixed horizons haven't
    all been recorded yet. A token that stopped out after four minutes still
    owes us its 30-, 60- and 120-minute returns, and if this query only
    looked at live trades those horizons would silently never be marked --
    leaving the new measurement empty while appearing to work.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT t.token_address
            FROM paper_trades t
            WHERE t.status IN ('PENDING_FILL', 'OPEN')
               OR (t.entry_model = 'IMMEDIATE'
                   AND t.evaluated_at > CURRENT_TIMESTAMP - (%s * INTERVAL '1 minute')
                   AND (SELECT COUNT(*) FROM paper_horizon_returns h
                        WHERE h.paper_trade_id = t.id) < %s);
        """, (_max_horizon_window(), len(HORIZONS_MINUTES)))
        return [r[0] for r in cur.fetchall()]


def mark_horizons(conn, prices: Dict[str, float]) -> Dict[int, int]:
    """Records each evaluation's return at every horizon that has elapsed.

    Runs alongside mark_to_market() and is deliberately independent of it: a
    barrier close must not stop horizon marking, and a missing price must not
    invent one. Returns a count of new marks per horizon.

    ON CONFLICT DO NOTHING carries the once-only guarantee, so a tick that
    re-examines an already-marked horizon is a no-op rather than a duplicate.
    """
    marks: Dict[int, int] = {}
    if not prices or not HORIZONS_MINUTES:
        return marks

    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.id, t.token_address, t.price_at_evaluation,
                   EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - t.evaluated_at))/60.0 AS age_min
            FROM paper_trades t
            WHERE t.entry_model = 'IMMEDIATE'
              AND t.evaluated_at > CURRENT_TIMESTAMP - (%s * INTERVAL '1 minute')
              AND (SELECT COUNT(*) FROM paper_horizon_returns h
                   WHERE h.paper_trade_id = t.id) < %s;
        """, (_max_horizon_window(), len(HORIZONS_MINUTES)))
        due = cur.fetchall()

        for tid, addr, basis, age_min in due:
            price = prices.get(addr)
            if price is None:
                continue  # unknown != zero, same rule as mark_to_market
            basis = float(basis or 0.0)
            if basis <= 0:
                continue
            age = float(age_min or 0.0)
            ret = ((float(price) / basis) - 1.0) * 100.0
            for horizon in HORIZONS_MINUTES:
                if age < horizon:
                    break  # HORIZONS_MINUTES is sorted; later ones aren't due either
                cur.execute("""
                    INSERT INTO paper_horizon_returns
                        (paper_trade_id, horizon_minutes, price, return_percent,
                         age_minutes_at_mark)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (paper_trade_id, horizon_minutes) DO NOTHING;
                """, (tid, horizon, float(price), round(ret, 4), round(age, 2)))
                # Explicitly > 0: DB-API allows -1 for "unknown", and -1 is
                # truthy, which would count conflict-skipped rows as new marks.
                if cur.rowcount is not None and cur.rowcount > 0:
                    marks[horizon] = marks.get(horizon, 0) + 1
    return marks


def mark_to_market(conn, prices: Dict[str, float]) -> Dict[str, int]:
    """Advances every live paper trade against real prices.

    A token missing from `prices` is left completely untouched. That
    matters: an unknown price is not a price of zero, and marking to zero
    would instantly trip every stop-loss and book fabricated total losses
    that would then pollute the experiment's results.
    """
    stats = {"filled": 0, "expired": 0, "closed_target": 0, "closed_stop": 0,
             "closed_timeout": 0, "abandoned_no_price": 0}
    # NOTE: no early return on an empty `prices`. A tick where NOTHING could be
    # priced is exactly when abandonment matters most -- returning early here
    # is what let unpriceable trades accumulate indefinitely.

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, token_address, entry_model, status, entry_trigger_price,
                   target_exit_price, invalidation_level_price, fill_price,
                   assumed_slippage_percent,
                   EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - evaluated_at))/60.0 AS age_min,
                   EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - COALESCE(filled_at, evaluated_at)))/60.0 AS held_min
            FROM paper_trades
            WHERE status IN ('PENDING_FILL', 'OPEN');
        """)
        live = cur.fetchall()

        for (tid, addr, entry_model, status, trigger, target, stop,
             fill_price, slippage, age_min, held_min) in live:
            price = prices.get(addr)
            if price is None:
                # Unknown is still not zero -- we never invent a price. But a
                # trade that has been unpriceable for this long is not coming
                # back, and leaving it live would quietly drop the worst
                # outcomes out of the sample. Abandon it with no P&L recorded.
                if float(age_min or 0) >= UNPRICEABLE_ABANDON_MINUTES:
                    cur.execute("""
                        UPDATE paper_trades
                        SET status = 'ABANDONED', exit_reason = 'NO_PRICE',
                            closed_at = CURRENT_TIMESTAMP
                        WHERE id = %s;
                    """, (tid,))
                    stats["abandoned_no_price"] += 1
                continue

            price = float(price)
            cur.execute(
                "UPDATE paper_trades SET last_price = %s, last_marked_at = CURRENT_TIMESTAMP WHERE id = %s;",
                (price, tid))

            if status == "PENDING_FILL":
                if price <= float(trigger):
                    cur.execute("""
                        UPDATE paper_trades
                        SET status = 'OPEN', fill_price = %s, filled_at = CURRENT_TIMESTAMP
                        WHERE id = %s;
                    """, (float(trigger), tid))
                    stats["filled"] += 1
                elif float(age_min or 0) >= LIMIT_FILL_WINDOW_MINUTES:
                    cur.execute(
                        "UPDATE paper_trades SET status = 'EXPIRED', closed_at = CURRENT_TIMESTAMP WHERE id = %s;",
                        (tid,))
                    stats["expired"] += 1
                continue

            # status == 'OPEN'
            basis = float(fill_price or 0.0)
            exit_reason = None
            exit_price = price
            if target is not None and price >= float(target):
                exit_reason, exit_price = "TARGET_HIT", float(target)
            elif stop is not None and price <= float(stop):
                exit_reason, exit_price = "STOPPED_OUT", float(stop)
            elif float(held_min or 0) >= MAX_HOLD_MINUTES:
                exit_reason = "TIMEOUT"

            if exit_reason is None:
                continue

            gross, cost, net = net_pnl_percent(basis, exit_price, slippage)
            cur.execute("""
                UPDATE paper_trades
                SET status = 'CLOSED', exit_reason = %s, exit_price = %s,
                    closed_at = CURRENT_TIMESTAMP, gross_pnl_percent = %s,
                    cost_percent = %s, net_pnl_percent = %s
                WHERE id = %s;
            """, (exit_reason, exit_price, gross, cost, net, tid))
            stats[{"TARGET_HIT": "closed_target", "STOPPED_OUT": "closed_stop",
                     "TIMEOUT": "closed_timeout"}[exit_reason]] += 1

    return stats


def results_summary(conn) -> List[Dict[str, Any]]:
    """Per-cohort, per-entry-model results.

    Reports the MEDIAN alongside the mean because memecoin returns are
    fat-tailed: one 40x turns a cohort of losers into a positive average,
    and the mean alone would say the strategy works when most trades lost
    money. Also reports the sample size, without which none of the rest
    should be read at all.

    `tokens` is the distinct-token count and is the number to trust as the
    real sample size -- n counts trades, and correlated trades on the same
    token are not independent evidence.

    GROSS and COST are reported separately from NET because they answer
    different questions. If APPROVED beats REJECTED on net but not on
    gross, the gates are not predicting which tokens go UP -- they are
    selecting tokens that are CHEAP TO TRADE. That is still worth
    something, but it is a much weaker claim than "the strategy has alpha",
    and it fails differently: cost edge evaporates the moment our own order
    is large enough to move the pool.
    """
    # ONE query, with FILTER clauses, over ALL statuses.
    #
    # Two bugs died here. The P&L stats used to come from a query filtered to
    # status='CLOSED' while the fill stats came from a second query grouped by
    # entry_model ALONE -- so the APPROVED/LIMIT row was handed the combined
    # never-filled count of BOTH cohorts. And because the first query only
    # emitted rows for cohort/model pairs that had closed trades, a cohort
    # whose LIMIT orders NEVER filled produced no row at all: the single
    # clearest adverse-selection finding the two-entry-model design exists to
    # surface was the one result it could not display.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT cohort, entry_model,
                   COUNT(*) FILTER (WHERE status = 'CLOSED')::int AS n,
                   COUNT(DISTINCT token_address) FILTER (WHERE status = 'CLOSED')::int AS tokens,
                   COUNT(*) FILTER (WHERE status = 'CLOSED' AND net_pnl_percent > 0)::int AS wins,
                   ROUND(AVG(gross_pnl_percent) FILTER (WHERE status = 'CLOSED'), 2) AS mean_gross,
                   ROUND(AVG(cost_percent)      FILTER (WHERE status = 'CLOSED'), 2) AS mean_cost,
                   ROUND(AVG(net_pnl_percent)   FILTER (WHERE status = 'CLOSED'), 2) AS mean_net,
                   ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY net_pnl_percent)
                         FILTER (WHERE status = 'CLOSED')::numeric, 2) AS median_net,
                   ROUND(MIN(net_pnl_percent) FILTER (WHERE status = 'CLOSED'), 2) AS worst,
                   ROUND(MAX(net_pnl_percent) FILTER (WHERE status = 'CLOSED'), 2) AS best,
                   COUNT(*) FILTER (WHERE status = 'EXPIRED')::int AS never_filled,
                   COUNT(*) FILTER (WHERE status IN ('PENDING_FILL','OPEN'))::int AS still_live,
                   COUNT(*) FILTER (WHERE status = 'ABANDONED')::int AS abandoned_no_price
            FROM paper_trades
            GROUP BY cohort, entry_model
            ORDER BY cohort, entry_model;
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    for row in rows:
        row["win_rate"] = round((row["wins"] / row["n"]) * 100, 1) if row["n"] else None
    return rows


def horizon_summary(conn, min_txns_h1: Optional[int] = None) -> List[Dict[str, Any]]:
    """Per-cohort return distribution at each fixed horizon.

    This is the measurement that can actually settle whether the gates
    predict direction. Read it as a comparison BETWEEN cohorts at the same
    horizon: an approved-vs-rejected difference in mean and median return,
    on samples of independent tokens, is evidence. An approved cohort that is
    merely positive is not -- memecoins drift, and a rising tide lifts the
    rejected cohort too.

    `net_after_costs` subtracts the round-trip cost computed from the slippage
    measured at evaluation -- NOT from paper_trades.cost_percent, which is only
    written when a barrier trade closes and would therefore charge zero costs
    to every still-open trade,
    because a gross edge smaller than the spread is not tradeable. When the
    cohort gap survives in gross but vanishes in net, the gates are selecting
    cheap tokens rather than good ones -- which is precisely what the barrier
    results showed.

    Marks taken more than HORIZON_TOLERANCE past their horizon are excluded:
    a price fetched 50 minutes late does not describe a 30-minute return.

    `min_txns_h1` restricts the result to tokens that were actually trading
    when evaluated. Run it both ways. A token with no recent trades still has
    a priceUsd -- the last print, however old -- so its returns are a mix of
    stale zeros and jumps off single fills that no order could have caught.
    Those observations are noise, and noise dilutes a real signal rather than
    inventing a false one: if the cohort gap only appears once dead tokens
    are excluded, the gates work and DISCOVERY is what needs fixing.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT h.horizon_minutes,
                   t.cohort,
                   COUNT(*)::int AS n,
                   COUNT(DISTINCT t.token_address)::int AS tokens,
                   ROUND(AVG(h.return_percent), 2) AS mean_gross,
                   ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY h.return_percent)::numeric, 2) AS median_gross,
                   ROUND(AVG(h.return_percent - (%s + 2 * ABS(COALESCE(t.assumed_slippage_percent, 0)))), 2) AS net_after_costs,
                   COUNT(*) FILTER (WHERE h.return_percent > 0)::int AS positive,
                   ROUND(STDDEV_SAMP(h.return_percent), 2) AS stdev,
                   ROUND(MIN(h.return_percent), 2) AS worst,
                   ROUND(MAX(h.return_percent), 2) AS best
            FROM paper_horizon_returns h
            JOIN paper_trades t ON t.id = h.paper_trade_id
            WHERE h.age_minutes_at_mark <= h.horizon_minutes * %s
              AND (%s IS NULL OR t.txns_h1 >= %s)
            GROUP BY h.horizon_minutes, t.cohort
            ORDER BY h.horizon_minutes, t.cohort;
        """, (PAPER_FEE_PERCENT_PER_SIDE * 2.0, HORIZON_TOLERANCE,
              min_txns_h1, min_txns_h1))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    for row in rows:
        n = row.get("n") or 0
        row["positive_rate"] = round((row["positive"] / n) * 100, 1) if n else 0.0
        # Standard error of the mean, so a cohort gap can be judged against
        # its own noise instead of being read as a result on sight.
        sd, tokens = row.get("stdev"), row.get("tokens") or 0
        row["stderr"] = round(float(sd) / (tokens ** 0.5), 2) if sd and tokens > 1 else None
    return rows


def staleness_report(conn) -> List[Dict[str, Any]]:
    """How much of the sample is dead tokens rather than measurable ones.

    Two signatures of a token that isn't really trading:

    `zero_return_rate` -- the share of horizon marks that came back at
    EXACTLY 0.0000%. A live token essentially never reprices to the identical
    figure 30 minutes later; a dead one does, because DexScreener is still
    serving the last print. A high rate here means the experiment is
    measuring stale quotes.

    `no_txn_rate` -- the share of evaluations where DexScreener reported zero
    transactions in the preceding hour. That is a token with no counterparty,
    whatever its chart shows.

    Neither is a bug in the pipeline. Both cap how much signal the experiment
    can possibly extract, which is worth knowing before concluding the gates
    have no edge.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.cohort,
                   COUNT(DISTINCT t.token_address)::int AS tokens,
                   COUNT(h.id)::int AS marks,
                   COUNT(h.id) FILTER (WHERE h.return_percent = 0)::int AS zero_returns,
                   COUNT(DISTINCT t.token_address) FILTER (WHERE t.txns_h1 = 0)::int AS tokens_no_txns,
                   COUNT(DISTINCT t.token_address) FILTER (WHERE t.txns_h1 IS NULL)::int AS tokens_txns_unknown,
                   -- Medians come from a SEPARATE aggregate over paper_trades
                   -- alone. Computed across the LEFT JOIN above, each trade is
                   -- counted once per horizon mark -- so a live token with 3
                   -- marks weighs 3x a dead one with 0, in precisely the
                   -- statistic meant to detect dead tokens. The join inflates
                   -- the healthy end of the distribution and hides the problem.
                   (SELECT ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t2.txns_h1)::numeric, 0)
                      FROM paper_trades t2
                     WHERE t2.entry_model = 'IMMEDIATE' AND t2.cohort = t.cohort) AS median_txns_h1,
                   (SELECT ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t2.volume_h1_usd)::numeric, 0)
                      FROM paper_trades t2
                     WHERE t2.entry_model = 'IMMEDIATE' AND t2.cohort = t.cohort) AS median_volume_h1
            FROM paper_trades t
            LEFT JOIN paper_horizon_returns h ON h.paper_trade_id = t.id
            WHERE t.entry_model = 'IMMEDIATE'
            GROUP BY t.cohort
            ORDER BY t.cohort;
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    for row in rows:
        marks = row.get("marks") or 0
        tokens = row.get("tokens") or 0
        row["zero_return_rate"] = round((row["zero_returns"] / marks) * 100, 1) if marks else None
        row["no_txn_rate"] = round((row["tokens_no_txns"] / tokens) * 100, 1) if tokens else None
    return rows


# Candidate predictors, as SQL expressions over paper_trades `t`. Adding one
# here is all it takes to include it in the correlation report.
FEATURE_EXPRESSIONS = {
    # Direction candidates
    "m5_buy_share": "t.txns_m5_buys::numeric / NULLIF(t.txns_m5_buys + t.txns_m5_sells, 0)",
    "h1_buy_share": "t.txns_h1_buys::numeric / NULLIF(t.txns_h1_buys + t.txns_h1_sells, 0)",
    "price_change_m5": "t.price_change_m5",
    "price_change_h1": "t.price_change_h1",
    # Activity / liveness candidates
    "txns_m5_total": "(COALESCE(t.txns_m5_buys,0) + COALESCE(t.txns_m5_sells,0))::numeric",
    "txns_h1": "t.txns_h1::numeric",
    "volume_m5_usd": "t.volume_m5_usd",
    # Capital per participation event -- what E_BREADTH actually gates on
    "capital_per_txn": "t.volume_h1_usd / NULLIF(t.txns_h1, 0)",
    "tradeable_depth_usd": "t.tradeable_depth_usd",
    "slippage_percent": "t.assumed_slippage_percent",
}

# Below this many observations a correlation is not worth reading. Kept
# deliberately blunt: with fewer points than this, |rho| of 0.3 arrives by
# chance often enough to mislead anyone scanning the table.
MIN_CORRELATION_N = 30


def feature_correlations(conn, horizon_minutes: Optional[int] = None) -> List[Dict[str, Any]]:
    """Rank correlation between each recorded feature and the horizon return.

    SPEARMAN, not Pearson, and the reason matters. Memecoin returns are
    violently fat-tailed: one token that ran 40x dominates a Pearson
    coefficient completely, so what looks like "feature X predicts returns"
    is often "feature X happened to be high for the one outlier". Ranking
    both sides first bounds every observation's influence, which is what you
    want when the tail is the whole distribution.

    Read `rho` alongside `n`, and read `n` first. This function exists to
    tell you which candidate predictors are worth gating on -- and, far more
    often, which are not. A feature that fails here has been ruled out
    cheaply, which is the point: E_SIGNAL filtered on a number nobody had
    ever checked against an outcome, and it did that for months.

    Nothing here establishes causation, and a feature that survives should
    still be tested on data collected AFTER it was chosen. Picking the best
    of ten features on one sample and believing its coefficient is how you
    end up trading an artifact of that sample.
    """
    results: List[Dict[str, Any]] = []
    with conn.cursor() as cur:
        for name, expr in FEATURE_EXPRESSIONS.items():
            cur.execute(f"""
                WITH pairs AS (
                    SELECT h.horizon_minutes AS horizon,
                           h.return_percent  AS ret,
                           t.token_address   AS token,
                           ({expr})          AS feat
                    FROM paper_horizon_returns h
                    JOIN paper_trades t ON t.id = h.paper_trade_id
                    WHERE h.age_minutes_at_mark <= h.horizon_minutes * %s
                      AND (%s IS NULL OR h.horizon_minutes = %s)
                      AND ({expr}) IS NOT NULL
                ),
                -- MID-ranks, not RANK(). Spearman's rho with ties requires the
                -- average of the tied positions; RANK() assigns every tie the
                -- MINIMUM position and then skips. That is not Spearman, and
                -- the error is not academic here: staleness_report() exists
                -- because a large share of return_percent values are exactly
                -- 0.0000 (dead tokens re-serving one stale print), and the
                -- same tokens tie at 0 on the activity features. Min-ranking
                -- two large tie blocks manufactures correlation between
                -- variables whose only shared property is being stale.
                ranked AS (
                    SELECT horizon, token,
                           AVG(rk_ret)  OVER (PARTITION BY horizon, ret)  AS r_ret,
                           AVG(rk_feat) OVER (PARTITION BY horizon, feat) AS r_feat
                    FROM (
                        SELECT horizon, token, ret, feat,
                               ROW_NUMBER() OVER (PARTITION BY horizon ORDER BY ret)  AS rk_ret,
                               ROW_NUMBER() OVER (PARTITION BY horizon ORDER BY feat) AS rk_feat
                        FROM pairs
                    ) numbered
                )
                SELECT horizon, COUNT(*)::int AS n,
                       COUNT(DISTINCT token)::int AS tokens,
                       ROUND(CORR(r_ret, r_feat)::numeric, 3) AS rho
                FROM ranked GROUP BY horizon ORDER BY horizon;
            """, (HORIZON_TOLERANCE, horizon_minutes, horizon_minutes))
            for horizon, n, tokens, rho in cur.fetchall():
                results.append({
                    "feature": name,
                    "horizon_minutes": horizon,
                    "n": n,
                    # The sample size that counts. The re-entry cooldown means
                    # one coin contributes a mark every hour, and those marks
                    # are correlated -- treating them as independent inflates
                    # confidence in exactly the features that recur most.
                    "tokens": tokens,
                    "rho": float(rho) if rho is not None else None,
                    # Rough two-sided Spearman threshold: |rho| > ~1.96/sqrt(k-1)
                    # on k INDEPENDENT observations. Not a substitute for a real
                    # test, just a guard against reading noise as a finding.
                    "readable": bool(tokens >= MIN_CORRELATION_N and rho is not None
                                     and abs(float(rho)) > 1.96 / ((tokens - 1) ** 0.5)),
                })
    results.sort(key=lambda r: (r["horizon_minutes"], -abs(r["rho"] or 0.0)))
    return results
