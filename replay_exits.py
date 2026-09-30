"""Replay exit policies against the ORDERED price paths we actually observed.

    docker compose exec web python replay_exits.py

WHY THIS EXISTS
---------------
Section 10f found that approved tokens have the same median downside as
rejected ones (-2.07 vs -2.08) and more than twice the upside (+4.17 vs
+1.87), yet every arm loses money. The suspect is the trade GEOMETRY: the
target sits at +15 percent while the median maximum favourable excursion is
+4.17, so the typical trade cannot reach its target and a third of them reach
the stop. Frequent small losses, rare wins.

Testing that needed the ORDERED path. min_price_seen / max_price_seen say how
far a token moved each way but not in which order, so they cannot tell "rose,
then fell through the stop" from "fell through the stop, then recovered".
paper_price_path stores the sequence; this replays any (stop, target) pair
against it.

WHAT IT REUSES, AND WHY THAT MATTERS
------------------------------------
decide_exit, net_pnl_percent and total_cost_percent are IMPORTED, never
reimplemented. A replay that re-derives the exit rules tests a strategy that
does not exist -- it would quietly diverge from production the first time
either side changed, and the divergence would look like a result. The one
thing overridden is the barrier levels, which is the whole point.

WHAT IT CANNOT TELL YOU
-----------------------
1. SAMPLING. Paths are sampled every PATH_SAMPLE_SECONDS (30s), so a barrier
   crossed and recrossed inside one sample is invisible. This makes BOTH
   barriers under-trigger, and it flatters tight stops most -- the tighter the
   stop, the likelier a real crossing fell between samples. Treat tight-stop
   rows as optimistic.
2. SELECTION. Only tokens the gates approved were ever traded. This answers
   "given these tokens, which exit does best", never "which gates to use".
3. OVERFITTING. A grid over a few hundred trades WILL produce a best cell by
   chance. The grid is printed whole, with its sample size, precisely so the
   best cell is read as one draw from a noisy surface rather than a finding.
   A cell that only beats its neighbours is noise; a broad region that beats
   the current policy is worth testing forward.
4. LIMIT ARM. Excluded. Its entry time depends on a fill that itself depends
   on the path, so replaying it under a different stop changes WHICH trades
   exist, not just their outcomes. That is a different experiment.
"""
import os
import sys
from typing import Dict, List, Tuple, Optional

import paper_trading as pt

# psycopg2 is imported INSIDE main(), not here. replay_one is a pure function
# and is the part that can be silently wrong -- a module-level driver import
# would make the one piece worth testing untestable anywhere without a
# database, which is how pure logic ends up with no cover at all.

# Grid. The current policy (7.53 / 15.06) is included so every row is
# comparable against what is actually running.
STOPS = [float(x) for x in os.environ.get(
    "REPLAY_STOPS", "3,5,7.53,10,15,20").split(",")]
TARGETS = [float(x) for x in os.environ.get(
    "REPLAY_TARGETS", "3,5,7.5,10,15.06,25").split(",")]
# 1, not 3. A token with only one or two samples after entry is usually one
# that stopped pricing fast -- i.e. died. Requiring three quietly removed the
# worst outcomes from every cell of the surface.
MIN_SAMPLES = int(os.environ.get("REPLAY_MIN_SAMPLES", "1"))


def load_trades(conn, cohort: str) -> List[dict]:
    """IMMEDIATE trades whose token has an ordered path recorded after entry."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.id, t.token_address, t.pair_address, t.price_at_evaluation,
                   t.evaluated_at, t.assumed_slippage_percent,
                   t.status, t.net_pnl_percent
            FROM paper_trades t
            WHERE t.entry_model = 'IMMEDIATE'
              AND t.cohort = %s
              AND t.pair_address IS NOT NULL
              AND t.price_at_evaluation > 0
              AND EXISTS (SELECT 1 FROM paper_price_path p
                          WHERE p.token_address = t.token_address
                            AND p.pair_address = t.pair_address
                            AND p.observed_at >= t.evaluated_at)
            ORDER BY t.evaluated_at;
        """, (cohort,))
        return [{"id": r[0], "addr": r[1], "pair": r[2], "basis": float(r[3]),
                 "entered": r[4], "slip": r[5], "status": r[6],
                 "prod_net": (None if r[7] is None else float(r[7]))}
                for r in cur.fetchall()]


def load_path(conn, addr: str, pair: str, since) -> List[Tuple[float, float]]:
    """(minutes_since_entry, price), in time order, WITHIN the hold window.

    Bounded above. paper_price_path is keyed by TOKEN, so an unbounded read
    for a token evaluated at T0 and again at T0+24h returned samples from the
    SECOND evaluation as though they were the first trade's path: a trade whose
    pricing stopped at +2h would "reach" its next sample a day later, at
    held=1440, and book a barrier or timeout at a price from another day.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXTRACT(EPOCH FROM (observed_at - %s))/60.0, price
            FROM paper_price_path
            WHERE token_address = %s
              AND pair_address = %s
              AND observed_at >= %s
              AND observed_at <= %s + (%s * INTERVAL '1 minute')
            ORDER BY observed_at;
        """, (since, addr, pair, since, since, pt.MAX_HOLD_MINUTES))
        return [(float(a), float(b)) for a, b in cur.fetchall()]


def replay_one(path: List[Tuple[float, float]], basis: float,
               stop_pct: float, target_pct: float,
               slippage) -> Optional[Tuple[str, float]]:
    """Walk the path in order; return (reason, net_pnl_percent).

    decide_exit is the production rule, unchanged. Only the levels move.
    A path that reaches neither barrier exits at its LAST observed price,
    which is what a timeout does.
    """
    stop = basis * (1.0 - stop_pct / 100.0)
    target = basis * (1.0 + target_pct / 100.0)
    if stop <= 0 or target <= basis:
        return None

    for held, price in path:
        reason, exit_price = pt.decide_exit(price, target, stop, held)
        if reason is not None:
            _, _, net = pt.net_pnl_percent(basis, exit_price, slippage)
            return reason, net

    last = path[-1][1]
    _, _, net = pt.net_pnl_percent(basis, last, slippage)
    return "OPEN_AT_END", net


def evaluate_cell(paths, stop_pct: float, target_pct: float) -> dict:
    """One (stop, target) cell over many trades, TOKEN-weighted.

    Token-weighted because paths are keyed by token: a token evaluated five
    times contributes five overlapping stretches of ONE price series, and a
    trade-weighted mean let a token that lingered in discovery outvote five
    tokens seen once. Every other decision statistic in this project is
    token-weighted; the replay has to be too, or its cells are not comparable
    to anything.

    A trade whose path ENDS before the max hold without touching either barrier
    is UNRESOLVED -- we do not know what it would have done. It is still booked
    at its last observed price in `mean` (dropping it would remove the tokens
    that stopped pricing, i.e. the dead ones, and flatter every cell), but it
    is counted separately so the reader can see how much of a cell rests on it.
    """
    by_token: dict = {}
    unresolved = 0
    trades = 0
    for t, path in paths:
        out = replay_one(path, t["basis"], stop_pct, target_pct, t["slip"])
        if out is None:
            continue
        trades += 1
        if out[0] == "OPEN_AT_END":
            unresolved += 1
        by_token.setdefault(t["addr"], []).append(out[1])
    per_token = [sum(v) / len(v) for v in by_token.values()]
    return {
        "mean": (sum(per_token) / len(per_token)) if per_token else float("nan"),
        "tokens": len(per_token),
        "trades": trades,
        "unresolved": unresolved,
    }


def production_mean(paths) -> Optional[float]:
    """Token-weighted mean of what PRODUCTION actually booked for these trades.

    Printed beside the live-policy cell. If the replay at the live (stop,
    target) does not roughly reproduce this, the gap is replay error --
    sampling resolution, truncated paths, a different fill rule -- and every
    other cell carries the same error. Without this line there was no way to
    tell a real policy difference from a flaw in the instrument.
    """
    by_token: dict = {}
    for t, _ in paths:
        if t.get("status") == "CLOSED" and t.get("prod_net") is not None:
            by_token.setdefault(t["addr"], []).append(t["prod_net"])
    per_token = [sum(v) / len(v) for v in by_token.values()]
    return (sum(per_token) / len(per_token)) if per_token else None


def main() -> int:
    import psycopg2

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set.")
        return 2
    conn = psycopg2.connect(dsn)
    conn.autocommit = True

    print("=" * 78)
    print("EXIT-POLICY REPLAY against observed ordered paths")
    print(f"  sample resolution: {pt.PATH_SAMPLE_SECONDS:.0f}s "
          f"(barriers crossed between samples are invisible)")
    print(f"  current live policy: stop {pt.__dict__.get('STOP_DISTANCE_PERCENT', 7.53)}"
          f" / target 15.06  -- shown in the grid for comparison")
    print("=" * 78)

    for cohort in ("APPROVED", "REJECTED"):
        trades = load_trades(conn, cohort)
        paths = []
        for t in trades:
            p = load_path(conn, t["addr"], t["pair"], t["entered"])
            if len(p) >= MIN_SAMPLES:
                paths.append((t, p))

        print(f"\n### {cohort} -- {len(paths)} trades with a usable path "
              f"(of {len(trades)} with any)")
        if not paths:
            print("  Nothing to replay yet. paper_price_path needs to "
                  "accumulate after a restart.")
            continue
        if len(paths) < 50:
            print(f"  WARNING: {len(paths)} trades is too few to choose a "
                  f"policy from. Read the SHAPE of the surface, not its "
                  f"maximum.")

        span = [len(p) for _, p in paths]
        tokens = len({t["addr"] for t, _ in paths})
        print(f"  {tokens} distinct tokens; samples per trade: min {min(span)}, "
              f"median {sorted(span)[len(span)//2]}, max {max(span)}")

        header = "  stop \\ target " + "".join(f"{t:>9.2f}" for t in TARGETS)
        print(header)
        worst_unresolved = 0.0
        live = None
        for stop_pct in STOPS:
            row = []
            for target_pct in TARGETS:
                c = evaluate_cell(paths, stop_pct, target_pct)
                row.append(c["mean"])
                if c["trades"]:
                    worst_unresolved = max(worst_unresolved,
                                           c["unresolved"] / c["trades"])
                if abs(stop_pct - 7.53) < 0.01 and abs(target_pct - 15.06) < 0.01:
                    live = c
            mark = " *" if abs(stop_pct - 7.53) < 0.01 else "  "
            print(f"  {stop_pct:>6.2f}{mark}     "
                  + "".join(f"{c:>9.2f}" for c in row))
        print(f"  unresolved paths: up to {worst_unresolved:.0%} of trades in a "
              f"cell ended before the max hold without touching a barrier, "
              f"and are booked at their last price.")
        prod = production_mean(paths)
        if live is not None and prod is not None:
            gap = live["mean"] - prod
            print(f"  RECONCILIATION  replay at live policy: {live['mean']:.2f}   "
                  f"production booked: {prod:.2f}   gap: {gap:+.2f}")
            if abs(gap) > 2.0:
                print("  WARNING: the replay does not reproduce production at the "
                      "live policy. Every other cell carries this error -- read "
                      "differences BETWEEN cells, never a cell's absolute level.")
        print("  (* = the stop distance currently running; cells are TOKEN-"
              "weighted mean net P&L, after fees and slippage)")

    conn.close()
    print("\nRead this as a surface, not a leaderboard. A single best cell "
          "over a few hundred\ntrades is a draw from noise; a broad region "
          "beating the current policy is a hypothesis\nworth testing forward "
          "on new data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
