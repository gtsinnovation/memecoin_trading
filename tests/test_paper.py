# tests/test_paper.py
"""Paper-trading MEASUREMENT correctness. Guards audit fixes 7-11.

These are the subtlest regressions in the project, because every one of them
produces a number that looks entirely reasonable. A censored sample still
prints a mean. A Spearman coefficient computed from min-ranks is still a
number between -1 and 1. Nothing errors; the answer is just wrong, in a
direction that flatters the strategy.
"""
from .harness import Suite, make_address


def _snap(addr, price=1.0, slip=0.4, slip_missing=False, txns=(300, 200)):
    return {"token_address": addr, "token_symbol": "T", "current_price": price,
            "estimated_slippage_percent": slip, "slippage_data_missing": slip_missing,
            "volume_h1_usd": 50000.0, "txns_h1_buys": txns[0], "txns_h1_sells": txns[1],
            "tradeable_depth_usd": 30000.0, "volume_m5_usd": 900.0,
            "txns_m5_buys": 40, "txns_m5_sells": 35,
            "price_change_m5": 1.0, "price_change_h1": 3.0}


def run(psycopg2, paper_trading, dsn) -> Suite:
    s = Suite("paper-trading measurement")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True

    def rec(snap, state=None):
        # Mirrors main._record_paper_candidate exactly: record_candidate uses
        # SAVEPOINT for its per-row inserts, and SAVEPOINT is only legal inside
        # a transaction block. Calling it on an autocommit connection fails
        # with "SAVEPOINT can only be used in transaction blocks" -- so the
        # test has to reproduce production's transaction, not just its call.
        conn.autocommit = False
        try:
            with conn:
                paper_trading.record_candidate(conn, snap, state or {})
        finally:
            conn.autocommit = True

    def age(addr, minutes):
        with conn.cursor() as cur:
            cur.execute("UPDATE paper_trades SET evaluated_at = CURRENT_TIMESTAMP - "
                        "(%s * INTERVAL '1 minute') WHERE token_address = %s;", (minutes, addr))

    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE paper_trades CASCADE;")

        print("\n[CENSORING] a token that stops pricing must not stay OPEN forever")
        dead = make_address(10)
        rec(_snap(dead))
        paper_trading.mark_to_market(conn, {dead: 1.0})
        age(dead, paper_trading.UNPRICEABLE_ABANDON_MINUTES + 20)
        stats = paper_trading.mark_to_market(conn, {})       # nothing prices at all
        s.check_true("abandoned once past the window", stats["abandoned_no_price"] >= 1)
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT status, exit_reason FROM paper_trades "
                        "WHERE token_address=%s;", (dead,))
            s.check("status ABANDONED / reason NO_PRICE", sorted(cur.fetchall()),
                    [("ABANDONED", "NO_PRICE")])
            # The rule that must survive: we never invent a price we did not see.
            cur.execute("SELECT count(*) FROM paper_trades WHERE token_address=%s "
                        "AND (net_pnl_percent IS NOT NULL OR exit_price IS NOT NULL);", (dead,))
            s.check("no P&L or exit price fabricated", int(cur.fetchone()[0]), 0)
        s.check("no longer priced every tick", dead in paper_trading.open_token_addresses(conn), False)

        young = make_address(11)
        rec(_snap(young)); age(young, 5)
        paper_trading.mark_to_market(conn, {})
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM paper_trades WHERE token_address=%s "
                        "AND status='ABANDONED';", (young,))
            s.check("a briefly-unpriceable trade is left alone", int(cur.fetchone()[0]), 0)

        print("\n[COSTS] unmeasured slippage is NULL, never 0")
        miss = make_address(12)
        rec(_snap(miss, slip_missing=True))
        with conn.cursor() as cur:
            cur.execute("SELECT assumed_slippage_percent FROM paper_trades "
                        "WHERE token_address=%s LIMIT 1;", (miss,))
            s.check("stored NULL, not 0", cur.fetchone()[0], None)
        meas = make_address(13)
        rec(_snap(meas, slip=1.25))
        with conn.cursor() as cur:
            cur.execute("SELECT assumed_slippage_percent FROM paper_trades "
                        "WHERE token_address=%s LIMIT 1;", (meas,))
            s.check("a measured slippage is still stored", float(cur.fetchone()[0]), 1.25)

        print("\n[COHORTS] fill statistics belong to their own cohort")
        with conn.cursor() as cur:
            cur.execute("TRUNCATE paper_trades CASCADE;")
        for i in range(3):
            a = make_address(100 + i); rec(_snap(a))
            with conn.cursor() as cur:
                cur.execute("UPDATE paper_trades SET status='EXPIRED' WHERE token_address=%s "
                            "AND entry_model='LIMIT';", (a,))
                cur.execute("UPDATE paper_trades SET status='CLOSED', net_pnl_percent=5 "
                            "WHERE token_address=%s AND entry_model='IMMEDIATE';", (a,))
        for i in range(7):
            a = make_address(200 + i); rec(_snap(a), {"termination_reason": "B_SENTINEL: thin"})
            with conn.cursor() as cur:
                cur.execute("UPDATE paper_trades SET status='EXPIRED' WHERE token_address=%s "
                            "AND entry_model='LIMIT';", (a,))
                cur.execute("UPDATE paper_trades SET status='CLOSED', net_pnl_percent=-5 "
                            "WHERE token_address=%s AND entry_model='IMMEDIATE';", (a,))
        rows = {(r["cohort"], r["entry_model"]): r for r in paper_trading.results_summary(conn)}
        # A cohort whose LIMIT orders NEVER filled must still produce a row --
        # that absence IS the adverse-selection finding.
        s.check("APPROVED/LIMIT row exists with zero closes", ("APPROVED", "LIMIT") in rows, True)
        s.check("REJECTED/LIMIT row exists with zero closes", ("REJECTED", "LIMIT") in rows, True)
        s.check("APPROVED never_filled is its own 3", rows[("APPROVED", "LIMIT")]["never_filled"], 3)
        s.check("REJECTED never_filled is its own 7", rows[("REJECTED", "LIMIT")]["never_filled"], 7)
        s.check("not the combined 10", rows[("APPROVED", "LIMIT")]["never_filled"] != 10, True)
        s.check("IMMEDIATE never_filled is 0, not borrowed",
                rows[("APPROVED", "IMMEDIATE")]["never_filled"], 0)
        s.check("closed P&L still computed", float(rows[("APPROVED", "IMMEDIATE")]["mean_net"]), 5.0)
        s.check("win_rate None (not 0.0) with no closes", rows[("APPROVED", "LIMIT")]["win_rate"], None)

        print("\n[SPEARMAN] mid-ranks, not min-ranks")
        # A FIXED fixture in which ties are the ONLY structure. On this data:
        #   correct Spearman (mid-ranks) = -0.074   -- essentially no relationship
        #   RANK() min-ranks             = +0.173  -- a sign flip and 0.25 of rho
        # conjured from nothing but how the tie blocks were numbered.
        #
        # 23 of 40 returns are exactly 0.0000 -- which is not contrived: it is
        # what staleness_report() exists to measure, dead tokens re-serving one
        # stale print. The feature ties alongside them. This is the shape the
        # real data actually has, which is why min-ranking mattered here rather
        # than being a textbook footnote.
        RET = [0, 0, 0, 0, 0, -21.2337, -5.7004, -11.2194, 25.9929, 0, 35.5902, -2.9353, 0, 0, -1.762, 19.9704, 34.7691, 0, 0, 0, 0, 0, -5.6429, 0, 0, -35.3554, -3.6626, -16.9586, 0, 0.3418, 0, 0, 0, 0, 34.998, 0, 0, 0, 27.1465, 30.9532]
        FEAT = [2, 0, 1, 0, 0, 2, 1, 1, 1, 0, 1, 2, 0, 0, 2, 0, 2, 2, 0, 0, 0, 1, 0, 0, 0, 1, 1, 0, 0, 2, 0, 0, 0, 0, 2, 0, 0, 0, 0, 0]
        MIDRANK_RHO, MINRANK_RHO = -0.074, 0.173
        with conn.cursor() as cur:
            cur.execute("TRUNCATE paper_trades CASCADE;")
        for i, (r_pct, f_val) in enumerate(zip(RET, FEAT)):
            a = make_address(300 + i)
            rec(_snap(a))
            with conn.cursor() as cur:
                cur.execute("UPDATE paper_trades SET evaluated_at = CURRENT_TIMESTAMP - "
                            "INTERVAL '35 minutes', txns_m5_buys=%s, txns_m5_sells=0 "
                            "WHERE token_address=%s;", (f_val, a))
            paper_trading.mark_horizons(conn, {a: 1.0 + (r_pct / 100.0)})
        corr = {r["feature"]: r for r in paper_trading.feature_correlations(conn, horizon_minutes=30)}
        rho = corr["txns_m5_total"]["rho"]
        s.check_true("rho matches mid-rank Spearman", abs(rho - MIDRANK_RHO) < 0.03)
        s.check_true("rho is NOT the min-rank value", abs(rho - MINRANK_RHO) > 0.10)
        s.check_true("sign is correct (negative, not flipped positive)", rho < 0)
        s.check_true("distinct tokens counted, not marks",
                     corr["txns_m5_total"]["tokens"] <= corr["txns_m5_total"]["n"])
        print(f"        rho={rho}  (mid-rank {MIDRANK_RHO}, min-rank would be {MINRANK_RHO})")

        print("\n[STALENESS] medians must not be fanned out by the horizon join")
        # Two tokens, identical txns_h1. One collects 3 horizon marks, one
        # collects none. Across the LEFT JOIN the marked token would weigh 3x,
        # skewing the median toward the live end -- in the very report whose
        # job is to reveal dead tokens.
        with conn.cursor() as cur:
            cur.execute("TRUNCATE paper_trades CASCADE;")
        live_tok, quiet_tok = make_address(400), make_address(401)
        rec(_snap(live_tok, txns=(900, 900)))
        rec(_snap(quiet_tok, txns=(50, 50)))
        with conn.cursor() as cur:
            cur.execute("UPDATE paper_trades SET evaluated_at=CURRENT_TIMESTAMP - "
                        "INTERVAL '200 minutes' WHERE token_address=%s;", (live_tok,))
        paper_trading.mark_horizons(conn, {live_tok: 1.05})   # collects 30/60/120
        rep = {r["cohort"]: r for r in paper_trading.staleness_report(conn)}
        med = float(rep["APPROVED"]["median_txns_h1"])
        # True median of {1800, 100} is 950. Fanned out it would be pulled to 1800.
        s.check("median is over trades, not over marks", med, 950.0)
        s.check_true("not dragged to the marked token's value", med != 1800.0)

        print("\n[REJECT REASON] the gate name alone cannot say WHY")
        # F_ATLAS refuses for two unrelated reasons and both record
        # rejected_by='F_ATLAS'. One is the gate working; the other is a
        # data-coverage problem. Separating them used to require joining
        # system_alerts.message by timestamp, which is guesswork the moment two
        # tokens are evaluated in the same second.
        conc = make_address(510)
        unmeasured = make_address(511)
        over = ("F_ATLAS: Short-circuit. Top 10 wallets hold 61.4%, "
                "violating the 30.0% ceiling.")
        absent = ("F_ATLAS: Short-circuit. Holder-concentration data unavailable -- "
                  "refusing rather than treating an unmeasured token as well distributed.")
        rec(_snap(conc), {"termination_reason": over})
        rec(_snap(unmeasured), {"termination_reason": absent})
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT rejected_by FROM paper_trades "
                        "WHERE token_address IN (%s, %s);", (conc, unmeasured))
            s.check("both refusals are the same gate", sorted(r[0] for r in cur.fetchall()),
                    ["F_ATLAS"])
            cur.execute("SELECT reject_reason FROM paper_trades WHERE token_address=%s "
                        "LIMIT 1;", (conc,))
            s.check("the concentration refusal keeps its full text", cur.fetchone()[0], over)
            cur.execute("SELECT count(*) FROM paper_trades "
                        "WHERE token_address IN (%s, %s) AND rejected_by = 'F_ATLAS' "
                        "AND reject_reason LIKE %s;",
                        (conc, unmeasured, "%unavailable%"))
            # Two rows: record_candidate writes one per entry model, and only the
            # unmeasured token's reason matches.
            s.check("the two F_ATLAS refusals are now separable by reason",
                    int(cur.fetchone()[0]), 2)

        ok = make_address(512)
        rec(_snap(ok))
        with conn.cursor() as cur:
            cur.execute("SELECT cohort, reject_reason FROM paper_trades "
                        "WHERE token_address=%s LIMIT 1;", (ok,))
            row = cur.fetchone()
            s.check("an approved candidate is APPROVED", row[0], "APPROVED")
            s.check("an approved candidate carries no reason", row[1], None)

        print("\n[CONCENTRATION] every measurement is recorded, absences as NULL")
        obs = make_address(520)
        snap = _snap(obs)
        snap.update({"holder_concentration_source": "provider",
                     "holder_concentration_provider_pct": 12.5,
                     "holder_concentration_raw_pct": 70.0,
                     "holder_concentration_wallet_pct": 20.0,
                     "holder_concentration_program_pct": 40.0,
                     "holder_concentration_burn_pct": 10.0})
        rec(snap)
        with conn.cursor() as cur:
            cur.execute("SELECT holder_concentration_source, holder_pct_provider, "
                        "holder_pct_chain_raw, holder_pct_chain_wallet, "
                        "holder_pct_chain_program, holder_pct_chain_burn "
                        "FROM paper_trades WHERE token_address=%s LIMIT 1;", (obs,))
            row = cur.fetchone()
            s.check("the source the gate used is recorded", row[0], "provider")
            s.check("what the gate saw is recorded", float(row[1]), 12.5)
            s.check("the raw chain alternative is recorded", float(row[2]), 70.0)
            s.check("the wallet-only alternative is recorded", float(row[3]), 20.0)
            s.check_true("the gap the definition decision rests on is computable",
                         abs(float(row[2]) - float(row[3]) - 50.0) < 1e-9)

        # A failed chain measurement must land as NULL. Stored as 0 it would read
        # as a perfectly distributed token in every query that ever averages
        # these columns -- the same fabrication the gate's missing-data branch
        # exists to prevent, one layer down.
        nochain = make_address(521)
        snap = _snap(nochain)
        snap.update({"holder_concentration_source": "provider",
                     "holder_concentration_provider_pct": 9.0,
                     "holder_concentration_raw_pct": None,
                     "holder_concentration_wallet_pct": None})
        rec(snap)
        with conn.cursor() as cur:
            cur.execute("SELECT holder_pct_chain_raw, holder_pct_chain_wallet "
                        "FROM paper_trades WHERE token_address=%s LIMIT 1;", (nochain,))
            s.check("an unmeasured chain figure is NULL, not 0", list(cur.fetchone()),
                    [None, None])
    finally:
        conn.close()
    
    print("\n[LEVELS] barrier geometry must hold at memecoin price scales")
    # round(price, 8) assumes a price of order 1. Memecoins are routinely
    # quoted below 1e-7, where 8 decimal places leave one significant figure
    # or none, and the levels stop being levels:
    #   1e-7 -> entry = stop = target = 9e-08   (all equal)
    #   2e-9 -> entry = stop = target = 0.0     (all zero)
    # Both FABRICATE an outcome on the next mark, because mark_to_market
    # tests `price >= target`. At 1e-7 that is true immediately and books
    # TARGET_HIT 10% BELOW the evaluation price -- a loss recorded as a
    # take-profit. At 2e-9 the target is 0.0, so `price >= 0.0` is true for
    # every price and the trade books TARGET_HIT at an exit of zero: a
    # fabricated -100%, labelled a win. Neither is distinguishable from a
    # real outcome in any statistic the experiment reports.
    for price in (1637.0, 1.0, 1e-4, 1e-6, 5e-7, 1e-7, 2e-8, 2e-9, 1e-15):
        entry, stop, target = paper_trading.compute_levels(price)
        s.check_true(f"levels ordered at price {price:g}",
                     paper_trading.levels_are_sane(price, entry, stop, target))
        # The geometry itself must survive, not just the ordering: a set that
        # is ordered but squashed to 0.1% wide is as useless as a collapsed one.
        s.check_true(f"stop is ~-14% at price {price:g}",
                     abs((stop / price - 1) * 100 + 14.0) < 0.5)
        s.check_true(f"target is ~+7% at price {price:g}",
                     abs((target / price - 1) * 100 - 7.0) < 0.5)
        s.check_true(f"target is above the evaluation price at {price:g}", target > price)

    print("\n[LEVELS] a degenerate set is refused rather than recorded")
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        entry, stop, target = paper_trading.compute_levels(bad)
        s.check_true(f"price {bad!r} judged not sane",
                     not paper_trading.levels_are_sane(bad, entry, stop, target))
    s.check_true("an ordered-but-collapsed set is refused",
                 not paper_trading.levels_are_sane(1.0, 0.93, 0.93, 0.93))
    # Correctly ordered and still degenerate: the whole set sits below the
    # evaluation price, so `price >= target` is true on the next mark and the
    # trade books a take-profit it never reached. This is the 1e-7 failure in
    # a subtler shape, and an ordering-only guard lets it straight through.
    s.check_true("an ordered set sitting entirely below the price is refused",
                 not paper_trading.levels_are_sane(1e-7, 9.3e-8, 8.6e-8, 9.5e-8))
    s.check_true("a normal set is still accepted",
                 paper_trading.levels_are_sane(1.0, 0.93, 0.86, 1.07))

    return s
