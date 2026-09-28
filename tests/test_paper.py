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
        """Age the trade AND its last mark -- i.e. it has been silent that long.

        Abandonment keys on SILENCE, not on total age. Pushing evaluated_at
        back while leaving last_marked_at at now describes a trade that is old
        but still pricing happily, which must NOT be abandoned; see the
        [SILENCE] block below, which asserts exactly that.
        """
        with conn.cursor() as cur:
            cur.execute("UPDATE paper_trades SET evaluated_at = CURRENT_TIMESTAMP - "
                        "(%s * INTERVAL '1 minute'), last_marked_at = CURRENT_TIMESTAMP - "
                        "(%s * INTERVAL '1 minute') WHERE token_address = %s;",
                        (minutes, minutes, addr))

    def age_entry_only(addr, minutes):
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
            # EACH ARM GETS ITS OWN CORRECT TERMINAL STATE. This used to
            # assert a single pair, because both arms went down the ABANDONED
            # path -- the LIMIT order was recorded as "we lost the price feed"
            # when what actually happened is that its trigger was never touched
            # inside the fill window. Those are different facts, and conflating
            # them took the unfilled order out of the fill-rate denominator.
            #
            # The original guarantee is unchanged and now stated per arm: the
            # token terminates, nothing is fabricated, and it stops being
            # priced. 200 minutes is past BOTH the 60-minute fill window and
            # the 180-minute abandonment window, so both fire.
            cur.execute("SELECT entry_model, status, exit_reason FROM paper_trades "
                        "WHERE token_address=%s ORDER BY entry_model;", (dead,))
            s.check("IMMEDIATE abandoned / LIMIT expired, each with its own reason",
                    cur.fetchall(),
                    [("IMMEDIATE", "ABANDONED", "NO_PRICE"),
                     ("LIMIT", "EXPIRED", None)])
            # The rule that must survive: we never invent a price we did not see.
            cur.execute("SELECT count(*) FROM paper_trades WHERE token_address=%s "
                        "AND (net_pnl_percent IS NOT NULL OR exit_price IS NOT NULL);", (dead,))
            s.check("no P&L or exit price fabricated", int(cur.fetchone()[0]), 0)
        # Pricing is bounded by the PATH window, not by abandonment. Within it
        # the token is still asked for (so the ordered path does not depend on
        # the production outcome -- see open_token_addresses); past it, it
        # stops. "Forever" is what this block guards against, and still does.
        s.check("still priced inside the path window",
                dead in paper_trading.open_token_addresses(conn),
                paper_trading.UNPRICEABLE_ABANDON_MINUTES + 20 < paper_trading.PATH_WINDOW_MINUTES)
        age(dead, paper_trading.PATH_WINDOW_MINUTES + 20)
        s.check("no longer priced once past the path window",
                dead in paper_trading.open_token_addresses(conn), False)

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

        print("\n[COSTS] the unmeasured stand-in must not undercut the gate it mirrors")
        # The old comment asserted equality with G_ANCHOR's ceiling and the two
        # numbers were 3.0 and 2.5. The real invariant is >=: a token whose impact
        # could not be quoted is thinner than anything the gate would approve, so
        # charging it LESS than the worst approvable case understates the cost of
        # exactly the rows that end up in the control arm.
        import os as _os, re as _re
        engine_src = open(_os.path.join(_os.path.dirname(_os.path.dirname(
            _os.path.abspath(__file__))), "engine.py"), encoding="utf-8").read()
        m = _re.search(r"max_slippage\s*=\s*([0-9.]+)", engine_src)
        s.check_true("G_ANCHOR's impact ceiling was found in engine.py", m is not None)
        ceiling = float(m.group(1)) if m else None
        s.check_true(f"the stand-in ({paper_trading.PAPER_UNMEASURED_SLIPPAGE_PERCENT}) is at "
                     f"least the gate ceiling ({ceiling})",
                     ceiling is not None
                     and paper_trading.PAPER_UNMEASURED_SLIPPAGE_PERCENT >= ceiling)
        s.check_true("an unmeasured trade costs strictly more than a measured one at the ceiling",
                     paper_trading.total_cost_percent(None)
                     > paper_trading.total_cost_percent(ceiling * 0.999))

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
        print("\n[SILENCE] one rate-limited tick must not wipe the open book")
        # Keyed on total age, a single 429 abandoned EVERY open trade older
        # than the threshold at once -- positions that had priced happily
        # thirty seconds earlier. The trades it wiped were the long-lived
        # ones, which is to say the winners: the sample lost its right tail
        # and the loss looked like ordinary censoring.
        with conn.cursor() as cur:
            cur.execute("TRUNCATE paper_trades CASCADE;")
        veteran = make_address(41)
        rec(_snap(veteran))
        paper_trading.mark_to_market(conn, {veteran: 1.0})     # sets last_marked_at
        age_entry_only(veteran, paper_trading.UNPRICEABLE_ABANDON_MINUTES + 120)
        stats = paper_trading.mark_to_market(conn, {})          # one tick prices nothing
        s.check("an old but freshly-priced trade survives a missed tick",
                stats["abandoned_no_price"], 0)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM paper_trades WHERE token_address=%s "
                        "AND status IN ('OPEN','PENDING_FILL');", (veteran,))
            s.check_true("still live", cur.fetchone()[0] >= 1)
        # ...and genuine silence still abandons it.
        age(veteran, paper_trading.UNPRICEABLE_ABANDON_MINUTES + 120)
        stats = paper_trading.mark_to_market(conn, {})
        s.check_true("sustained silence still abandons", stats["abandoned_no_price"] >= 1)

        print("\n[GAP FILL] a LIMIT that fills on a gap through its stop stops out on that mark")
        # Fill and stop on the same mark. The stop-out used to wait for the
        # next tick; a token that then went silent was ABANDONED with no P&L,
        # so the worst LIMIT outcomes left the sample.
        gapper = make_address(43)
        rec(_snap(gapper))
        with conn.cursor() as cur:
            cur.execute("SELECT entry_trigger_price, invalidation_level_price FROM paper_trades "
                        "WHERE token_address=%s AND entry_model='LIMIT';", (gapper,))
            trig, stop = (float(x) for x in cur.fetchone())
        gap_price = stop * 0.5
        paper_trading.mark_to_market(conn, {gapper: gap_price})
        with conn.cursor() as cur:
            cur.execute("SELECT status, exit_reason, fill_price, exit_price, net_pnl_percent "
                        "FROM paper_trades WHERE token_address=%s AND entry_model='LIMIT';", (gapper,))
            st, why, fp, xp, net = cur.fetchone()
        s.check("the gap fills AND stops on the same mark", (st, why), ("CLOSED", "STOPPED_OUT"))
        s.check_true("filled at the trigger (the conservative basis)", abs(float(fp) - trig) < 1e-12)
        s.check_true("exited at the gap price, not the stop", abs(float(xp) - gap_price) < 1e-12)
        s.check_true("the loss is the real one (~ -50% or worse vs the trigger)",
                     float(net) < (gap_price / trig - 1) * 100 + 0.01)
        cleaner = make_address(44)
        rec(_snap(cleaner))
        with conn.cursor() as cur:
            cur.execute("SELECT entry_trigger_price, invalidation_level_price FROM paper_trades "
                        "WHERE token_address=%s AND entry_model='LIMIT';", (cleaner,))
            trig2, stop2 = (float(x) for x in cur.fetchone())
        paper_trading.mark_to_market(conn, {cleaner: (trig2 + stop2) / 2})
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM paper_trades WHERE token_address=%s "
                        "AND entry_model='LIMIT';", (cleaner,))
            s.check("a fill between trigger and stop stays OPEN", cur.fetchone()[0], "OPEN")

        print("\n[FIRST EVAL] a token counts once, in the cohort of its first evaluation")
        with conn.cursor() as cur:
            cur.execute("TRUNCATE paper_trades CASCADE;")
        flip = make_address(61)
        rec(_snap(flip))                                          # APPROVED first
        with conn.cursor() as cur:
            cur.execute("UPDATE paper_trades SET evaluated_at = evaluated_at - INTERVAL '3 hours', "
                        "status = 'CLOSED' WHERE token_address = %s;", (flip,))
        rec(_snap(flip), {"termination_reason": "B_SENTINEL: thin"})  # then REJECTED
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(DISTINCT cohort) FROM paper_trades WHERE token_address=%s;", (flip,))
            both = cur.fetchone()[0]
            cur.execute("INSERT INTO paper_horizon_returns (paper_trade_id, horizon_minutes, price, "
                        "return_percent, age_minutes_at_mark) SELECT id, 30, 1.1, 10, 30 FROM paper_trades "
                        "WHERE token_address=%s AND entry_model='IMMEDIATE';", (flip,))
        s.check("fixture: the token really is in both cohorts", both, 2)
        hz = {(r["horizon_minutes"], r["cohort"]): r for r in paper_trading.horizon_summary(conn)}
        s.check_true("it is counted in its FIRST cohort", (30, "APPROVED") in hz and hz[(30, "APPROVED")]["tokens"] == 1)
        s.check_true("and not again in the later one", (30, "REJECTED") not in hz)

        print("\n[DROPOUT] horizon dropout is persisted, not only logged")
        with conn.cursor() as cur:
            cur.execute("TRUNCATE paper_horizon_dropout;")
        pt = paper_trading
        tick = {pt.HORIZON_DUE_TOTAL: 10, pt.HORIZON_DROPPED_NO_PRICE: 3,
                pt.HORIZON_DROPPED_NO_BASIS: 1, 30: 6}
        pt.record_horizon_dropout(conn, tick)
        pt.record_horizon_dropout(conn, tick)
        pt.record_horizon_dropout(conn, {pt.HORIZON_DUE_TOTAL: 0})
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), SUM(due), SUM(marked), SUM(dropped_no_price), "
                        "SUM(dropped_no_basis) FROM paper_horizon_dropout;")
            s.check("two ticks in one hour accumulate into one row",
                    tuple(int(x) for x in cur.fetchone()), (1, 20, 12, 6, 2))

        print("\n[FILL RATE] an unfilled LIMIT expires without needing a price")
        # "The trigger was never touched in 60 minutes" is a determinate fact
        # about the past, knowable with no price at all. Gating it on price
        # availability sent every dead token's unfilled order down the
        # ABANDONED path, which removed it from the fill-rate DENOMINATOR --
        # reporting 62.5% filled against a true 50%.
        with conn.cursor() as cur:
            cur.execute("TRUNCATE paper_trades CASCADE;")
        ghost = make_address(42)
        rec(_snap(ghost))
        age(ghost, paper_trading.LIMIT_FILL_WINDOW_MINUTES + 5)
        stats = paper_trading.mark_to_market(conn, {})          # never prices again
        s.check_true("expired, not abandoned", stats["expired"] >= 1)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM paper_trades WHERE token_address=%s "
                        "AND entry_model='LIMIT' AND status='EXPIRED';", (ghost,))
            s.check_true("the LIMIT arm is EXPIRED", cur.fetchone()[0] == 1)
            cur.execute("SELECT COUNT(*) FROM paper_trades WHERE token_address=%s "
                        "AND entry_model='LIMIT' AND status='ABANDONED';", (ghost,))
            s.check("it did not leave the fill-rate denominator", cur.fetchone()[0], 0)

        print("\n[CONFIRMATION] a target hit on a dead token is not a win")
        # The mark that crossed the target had ZERO transactions behind it.
        # The trade still closes -- refusing the exit would change the fill
        # model -- but it is flagged, so the analysis can exclude it instead
        # of counting a stale print as alpha.
        with conn.cursor() as cur:
            cur.execute("TRUNCATE paper_trades CASCADE;")
        ghost = make_address(43)
        rec(_snap(ghost, price=1.0))
        with conn.cursor() as cur:
            cur.execute("SELECT target_exit_price FROM paper_trades WHERE "
                        "token_address=%s AND entry_model='IMMEDIATE';", (ghost,))
            tgt = float(cur.fetchone()[0])
        paper_trading.mark_to_market(
            conn, {ghost: {"price": tgt * 1.05, "txns_m5": 0, "txns_h1": 0}})
        with conn.cursor() as cur:
            cur.execute("SELECT exit_reason, exit_confirmed, exit_txns_m5 FROM paper_trades "
                        "WHERE token_address=%s AND status='CLOSED' "
                        "AND entry_model='IMMEDIATE';", (ghost,))
            row = cur.fetchone()
        s.check("it still books as a target hit", row[0], "TARGET_HIT")
        s.check("but it is flagged unconfirmed", row[1], False)
        s.check("and the evidence is stored so the flag is re-derivable", row[2], 0)

        print("\n[CONFIRMATION] a target hit with real trades behind it stands")
        with conn.cursor() as cur:
            cur.execute("TRUNCATE paper_trades CASCADE;")
        live = make_address(44)
        rec(_snap(live, price=1.0))
        with conn.cursor() as cur:
            cur.execute("SELECT target_exit_price FROM paper_trades WHERE "
                        "token_address=%s AND entry_model='IMMEDIATE';", (live,))
            tgt = float(cur.fetchone()[0])
        paper_trading.mark_to_market(
            conn, {live: {"price": tgt * 1.05, "txns_m5": 12, "txns_h1": 300}})
        with conn.cursor() as cur:
            cur.execute("SELECT exit_confirmed FROM paper_trades WHERE token_address=%s "
                        "AND status='CLOSED' AND entry_model='IMMEDIATE';", (live,))
            s.check("confirmed", cur.fetchone()[0], True)
        # results_summary must separate the two and never silently substitute
        # the filtered figure for the headline one.
        rows = paper_trading.results_summary(conn)
        s.check_true("results_summary reports a confirmed-exit count",
                     any(r.get("exits_confirmed") for r in rows))
        s.check_true("and a confirmed share",
                     any(r.get("exit_confirmed_rate") is not None for r in rows))

        print("\n[CONFIRMATION] a legacy bare price records unknown, never confirmed")
        # Every existing caller passes a plain float. That must not be read as
        # evidence -- doing so would reinstate the defect wherever the richer
        # mark has not been threaded through yet.
        with conn.cursor() as cur:
            cur.execute("TRUNCATE paper_trades CASCADE;")
        legacy = make_address(45)
        rec(_snap(legacy, price=1.0))
        with conn.cursor() as cur:
            cur.execute("SELECT target_exit_price FROM paper_trades WHERE "
                        "token_address=%s AND entry_model='IMMEDIATE';", (legacy,))
            tgt = float(cur.fetchone()[0])
        paper_trading.mark_to_market(conn, {legacy: tgt * 1.05})
        with conn.cursor() as cur:
            cur.execute("SELECT exit_reason, exit_confirmed FROM paper_trades "
                        "WHERE token_address=%s AND status='CLOSED' "
                        "AND entry_model='IMMEDIATE';", (legacy,))
            row = cur.fetchone()
        s.check("the exit still happens", row[0], "TARGET_HIT")
        s.check("but carries no confirmation", row[1], None)

        print("\n[PATH] the extremes accumulate; they are not overwritten")
        # 10f and 10g are built entirely on these two columns, so a silent
        # defect here produces confident, plausible, wrong answers about
        # whether the stop or the gates are losing the money. LEAST/GREATEST
        # over the STORED value is what makes them accumulate; take that away
        # and min_price_seen becomes "the most recent price", which still looks
        # like a number and is never obviously wrong.
        #
        # EVERY PRICE HERE SITS INSIDE BOTH BARRIERS, and that is not
        # incidental. The first version of this test walked 1.00 / 0.88 / 1.19
        # / 1.05 on a basis of 1.0 -- but the stop sits at 0.9247, so 0.88
        # CLOSED the trade on the second mark, and mark_to_market only selects
        # PENDING_FILL and OPEN. The last two prices were never applied and the
        # test failed against correct code. Accumulation and barrier behaviour
        # are separate claims and have to be exercised separately.
        with conn.cursor() as cur:
            cur.execute("TRUNCATE paper_trades CASCADE;")
        path = make_address(46)
        rec(_snap(path, price=1.0))
        with conn.cursor() as cur:
            cur.execute("SELECT invalidation_level_price, target_exit_price "
                        "FROM paper_trades WHERE token_address=%s "
                        "AND entry_model='IMMEDIATE';", (path,))
            stop_px, target_px = [float(x) for x in cur.fetchone()]
        # Down, then up past the start, then back to the middle -- all inside.
        walk = (1.00, 0.95, 1.12, 1.05)
        for px in walk:
            assert stop_px < px < target_px, (
                f"fixture price {px} is outside the barriers "
                f"({stop_px:.4f} .. {target_px:.4f}) and would close the trade")
            paper_trading.mark_to_market(conn, {path: px})
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT status FROM paper_trades "
                        "WHERE token_address=%s AND entry_model='IMMEDIATE';",
                        (path,))
            s.check("the trade stayed open for every mark",
                    cur.fetchone()[0], "OPEN")
            cur.execute("SELECT min_price_seen, max_price_seen, last_price "
                        "FROM paper_trades WHERE token_address=%s "
                        "AND entry_model='IMMEDIATE';", (path,))
            lo, hi, last = [float(x) for x in cur.fetchone()]
        s.check("the LOW is the lowest price seen, not the latest", lo, 0.95)
        s.check("the HIGH is the highest price seen, not the latest", hi, 1.12)
        s.check("last_price is still the latest, and is neither extreme", last, 1.05)
        s.check_true("the low is strictly below the last mark", lo < last)
        s.check_true("the high is strictly above the last mark", hi > last)

        print("\n[PATH] a CLOSED trade keeps accruing its path via mark_horizons")
        # This is the behaviour 10f's stopped_but_rose depends on. Once a stop
        # fires, mark_to_market drops the row from its query forever -- so if
        # nothing else updated the extremes, the recorded high would always be
        # the pre-stop high and "the token recovered afterwards" would be
        # unobservable by construction. mark_horizons keeps pricing a token for
        # the whole horizon window regardless of barrier state, which is why
        # the path update lives there too.
        with conn.cursor() as cur:
            cur.execute("TRUNCATE paper_trades CASCADE;")
        recov = make_address(48)
        rec(_snap(recov, price=1.0))
        paper_trading.mark_to_market(conn, {recov: 0.80})   # through the stop
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT status FROM paper_trades "
                        "WHERE token_address=%s AND entry_model='IMMEDIATE';",
                        (recov,))
            s.check("the stop closed it", cur.fetchone()[0], "CLOSED")
            cur.execute("SELECT max_price_seen FROM paper_trades "
                        "WHERE token_address=%s AND entry_model='IMMEDIATE';",
                        (recov,))
            hi_before = float(cur.fetchone()[0])
        # Age it so a horizon is due, then price it far ABOVE entry.
        age(recov, paper_trading.HORIZONS_MINUTES[0] + 1)
        paper_trading.mark_horizons(conn, {recov: 1.40})
        with conn.cursor() as cur:
            cur.execute("SELECT max_price_seen FROM paper_trades "
                        "WHERE token_address=%s AND entry_model='IMMEDIATE';",
                        (recov,))
            hi_after = float(cur.fetchone()[0])
        s.check_true("the post-close recovery raised the recorded high",
                     hi_after > hi_before)
        s.check("and it is the recovered price", hi_after, 1.40)

        print("\n[PATH] the first mark seeds both extremes")
        # Guards the seeding case regardless of HOW it is spelled. Postgres
        # LEAST/GREATEST ignore nulls rather than propagating them, so this
        # would pass with or without the COALESCE in the statement -- which is
        # worth asserting precisely because that is an exception to the usual
        # null rules and easy to "tidy up" wrongly later.
        with conn.cursor() as cur:
            cur.execute("TRUNCATE paper_trades CASCADE;")
        seed = make_address(47)
        rec(_snap(seed, price=1.0))
        paper_trading.mark_to_market(conn, {seed: 0.5})
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT min_price_seen, max_price_seen FROM paper_trades "
                        "WHERE token_address=%s AND entry_model='IMMEDIATE';", (seed,))
            lo, hi = cur.fetchone()
        s.check("one mark sets the low", float(lo), 0.5)
        s.check("and the high", float(hi), 0.5)

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
