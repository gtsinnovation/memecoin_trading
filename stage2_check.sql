-- Stage 2 health + results, in one pass.
--
--   Get-Content stage2_check.sql | docker compose exec -T db psql -U postgres -d memecoin_trading
--
-- Sections 1-2 answer "is it recording?" and are meaningful within minutes.
-- Sections 3-6 answer "what does it say?" and are NOT meaningful until the
-- token counts are in the dozens. Read n before reading any number beside it.

-- Round-trip fee = PAPER_FEE_PERCENT_PER_SIDE * 2. Override with:
--   psql -v fee=1.5 -f stage2_check.sql
\if :{?fee}
\else
\set fee 0.5
\endif

\echo ''
\echo '=== 1. PLUMBING: is anything being recorded, and is dedup holding? ==='
-- Read rows_per_token_per_HOUR, not rows_per_token.
--
-- Each evaluation writes 2 rows (IMMEDIATE + LIMIT), and
-- PAPER_REENTRY_COOLDOWN_MINUTES lets a token be re-recorded once an hour --
-- so over a long run rows_per_token climbs by design. After 17 hours, 11.6
-- rows/token is normal; it does NOT mean dedup has failed.
--
-- The dedup-failure signature is rows_per_token_per_hour materially above 2:
-- that is a token being re-recorded every TICK rather than every cooldown,
-- which is the bug that made the first run's 157 "trades" actually 16 coins.
SELECT COUNT(*) AS rows,
       COUNT(DISTINCT token_address) AS tokens,
       ROUND(COUNT(*)::numeric / NULLIF(COUNT(DISTINCT token_address), 0), 2) AS rows_per_token,
       ROUND(COUNT(*)::numeric
             / NULLIF(COUNT(DISTINCT token_address), 0)
             / GREATEST(EXTRACT(EPOCH FROM (MAX(evaluated_at) - MIN(evaluated_at)))/3600.0, 1), 2)
             AS rows_per_token_per_hour,
       -- to_char keeps the zone label. Casting timestamptz -> timestamp
       -- converts to the session zone and then DISCARDS the label, leaving a
       -- bare wall-clock time whose meaning depends on the client's TimeZone
       -- -- the exact ambiguity app_time.py exists to remove.
       to_char(MIN(evaluated_at), 'YYYY-MM-DD HH24:MI:SS TZ') AS first_seen,
       to_char(MAX(evaluated_at), 'YYYY-MM-DD HH24:MI:SS TZ') AS last_seen
FROM paper_trades;

\echo ''
\echo '=== 2. PLUMBING: are the new columns actually populating? ==='
-- Every pct_* should be at or near 100. A column stuck at 0 means the
-- provider is not supplying that field and the feature is untestable --
-- worth knowing NOW rather than after a week of accumulation.
SELECT COUNT(*) AS immediate_rows,
       ROUND(100.0 * COUNT(volume_h1_usd)   / NULLIF(COUNT(*),0)) AS pct_vol_h1,
       ROUND(100.0 * COUNT(txns_h1)         / NULLIF(COUNT(*),0)) AS pct_txns_h1,
       ROUND(100.0 * COUNT(txns_m5_buys)    / NULLIF(COUNT(*),0)) AS pct_txns_m5,
       ROUND(100.0 * COUNT(volume_m5_usd)   / NULLIF(COUNT(*),0)) AS pct_vol_m5,
       ROUND(100.0 * COUNT(price_change_m5) / NULLIF(COUNT(*),0)) AS pct_chg_m5,
       ROUND(100.0 * COUNT(tradeable_depth_usd) / NULLIF(COUNT(*),0)) AS pct_depth
FROM paper_trades WHERE entry_model = 'IMMEDIATE';

\echo ''
\echo '=== 3. CANDIDATE QUALITY: are these tokens actually trading? ==='
-- The $USEFUL problem. A high no_txn_pct means discovery is surfacing dead
-- tokens whose "price" is a stale last print -- pure noise, which dilutes
-- any real signal rather than creating a false one.
-- Counts are per DISTINCT TOKEN, not per row. With a 60-minute re-entry
-- cooldown a token that stays in the discovery list all day is re-evaluated
-- hourly, so one persistently-listed dead token could contribute 24 rows
-- while 20 live tokens contribute 20 -- turning a true 5% dead rate into a
-- reported 55%. This is the same row-vs-token error section 1 warns about.
-- NULL txns_h1 (rows written before the column existed) is reported
-- separately rather than counted as a dead token.
SELECT cohort,
       COUNT(DISTINCT token_address) AS tokens,
       COUNT(DISTINCT token_address) FILTER (WHERE txns_h1 = 0) AS no_txns,
       COUNT(DISTINCT token_address) FILTER (WHERE txns_h1 IS NULL) AS txns_unknown,
       ROUND(100.0 * COUNT(DISTINCT token_address) FILTER (WHERE txns_h1 = 0)
             / NULLIF(COUNT(DISTINCT token_address),0)) AS no_txn_pct,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY txns_h1)::numeric, 0) AS median_txns_h1,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY volume_h1_usd)::numeric, 0) AS median_vol_h1,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY tradeable_depth_usd)::numeric, 0) AS median_depth
FROM paper_trades WHERE entry_model = 'IMMEDIATE' GROUP BY cohort ORDER BY cohort;

\echo ''
\echo '=== 3b. DROPOUT: trades abandoned because the token stopped pricing ==='
-- These are excluded from every P&L average by design (no exit price was
-- ever observed, and inventing one would be worse). But they are NOT a
-- random sample -- a token that stops pricing has usually died -- so a
-- large count here means the reported returns are optimistic. This number
-- existing at all is the point: the alternative was those trades sitting
-- OPEN forever and silently never entering the averages.
SELECT cohort, entry_model, COUNT(*) AS abandoned,
       COUNT(DISTINCT token_address) AS tokens
FROM paper_trades WHERE status = 'ABANDONED'
GROUP BY 1,2 ORDER BY 1,2;

\echo ''
\echo '=== 4. STALENESS: how much of the sample is a repeated stale quote? ==='
-- A live token essentially never reprices to the IDENTICAL figure 30 minutes
-- later. A dead one does, every time.
SELECT t.cohort,
       COUNT(h.id) AS marks,
       COUNT(h.id) FILTER (WHERE h.return_percent = 0) AS zero_returns,
       ROUND(100.0 * COUNT(h.id) FILTER (WHERE h.return_percent = 0) / NULLIF(COUNT(h.id),0)) AS zero_pct
FROM paper_trades t JOIN paper_horizon_returns h ON h.paper_trade_id = t.id
GROUP BY t.cohort ORDER BY t.cohort;

\echo ''
\echo '=== 5. HORIZON RETURNS by cohort -- the measurement that matters ==='
-- Compare APPROVED vs REJECTED at the SAME horizon, and judge any gap
-- against stdev. An approved cohort that is merely positive proves nothing:
-- memecoins drift, and a rising tide lifts the rejected cohort too.
-- A gap present in `mean` but absent in `net` is a liquidity filter, not
-- alpha -- and a cost edge does not survive real order sizes.
SELECT h.horizon_minutes AS mins, t.cohort,
       COUNT(DISTINCT t.token_address) AS tokens,
       ROUND(AVG(h.return_percent), 2) AS mean,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY h.return_percent)::numeric, 2) AS median,
       ROUND(STDDEV_SAMP(h.return_percent), 2) AS stdev,
       -- :fee is PAPER_FEE_PERCENT_PER_SIDE * 2, defaulted below. Hardcoding
       -- 0.5 meant an operator who raised the fee to be stricter saw no
       -- change in the only report that actually ships.
       -- NULL slippage propagates to a NULL net rather than being charged as
       -- zero cost -- an unmeasured cost is not a free trade.
       ROUND(AVG(h.return_percent - (:fee + 2*ABS(t.assumed_slippage_percent))), 2) AS net,
       ROUND(100.0 * COUNT(*) FILTER (WHERE h.return_percent > 0) / NULLIF(COUNT(*),0)) AS pct_up
FROM paper_horizon_returns h JOIN paper_trades t ON t.id = h.paper_trade_id
WHERE h.age_minutes_at_mark <= h.horizon_minutes * 1.5
GROUP BY 1, 2 ORDER BY 1, 2;

\echo ''
\echo '=== 5b. Same, but LIVE tokens only (>= 50 txns in the hour) ==='
-- If the cohort gap appears ONLY here, the gates work and DISCOVERY is what
-- needs fixing. That is a completely different repair from the one the
-- barrier results pointed at.
SELECT h.horizon_minutes AS mins, t.cohort,
       COUNT(DISTINCT t.token_address) AS tokens,
       ROUND(AVG(h.return_percent), 2) AS mean,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY h.return_percent)::numeric, 2) AS median,
       ROUND(STDDEV_SAMP(h.return_percent), 2) AS stdev
FROM paper_horizon_returns h JOIN paper_trades t ON t.id = h.paper_trade_id
WHERE h.age_minutes_at_mark <= h.horizon_minutes * 1.5
  AND t.txns_h1 >= 50
GROUP BY 1, 2 ORDER BY 1, 2;

\echo ''
\echo '=== 6. FEATURE CORRELATIONS (Spearman) -- hypotheses, not findings ==='
-- Ranked, not raw: memecoin returns are fat-tailed enough that one 40x
-- runner dictates a Pearson coefficient entirely.
--
-- READ n FIRST. Below ~30 tokens nothing here means anything. And with ten
-- features across three horizons, roughly 1-2 of these WILL look significant
-- by chance -- so treat anything that surfaces as a hypothesis to confirm on
-- data collected AFTER you picked it, never as a result.
WITH pairs AS (
    SELECT h.horizon_minutes AS horizon, h.return_percent AS ret,
           t.token_address AS token, f.name, f.val
    FROM paper_horizon_returns h
    JOIN paper_trades t ON t.id = h.paper_trade_id
    CROSS JOIN LATERAL (VALUES
        ('m5_buy_share',   t.txns_m5_buys::numeric / NULLIF(t.txns_m5_buys + t.txns_m5_sells, 0)),
        ('h1_buy_share',   t.txns_h1_buys::numeric / NULLIF(t.txns_h1_buys + t.txns_h1_sells, 0)),
        ('price_chg_m5',   t.price_change_m5),
        ('price_chg_h1',   t.price_change_h1),
        ('txns_m5_total',  (COALESCE(t.txns_m5_buys,0) + COALESCE(t.txns_m5_sells,0))::numeric),
        ('txns_h1',        t.txns_h1::numeric),
        ('volume_m5_usd',  t.volume_m5_usd),
        ('capital_per_txn', t.volume_h1_usd / NULLIF(t.txns_h1, 0)),
        ('depth_usd',      t.tradeable_depth_usd),
        ('slippage_pct',   t.assumed_slippage_percent)
    ) AS f(name, val)
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * 1.5
      AND f.val IS NOT NULL
),
-- MID-ranks. RANK() assigns tied values the MINIMUM position and skips,
-- which is not Spearman's rho once ties exist -- and section 4 exists
-- precisely because a large share of returns are exactly 0.0000 and the
-- same tokens tie at 0 on the activity features. Min-ranking two big tie
-- blocks manufactures correlation between variables whose only shared
-- property is being stale.
ranked AS (
    SELECT horizon, name, token,
           AVG(rk_ret) OVER (PARTITION BY horizon, name, ret) AS r_ret,
           AVG(rk_val) OVER (PARTITION BY horizon, name, val) AS r_val
    FROM (
        SELECT horizon, name, token, ret, val,
               ROW_NUMBER() OVER (PARTITION BY horizon, name ORDER BY ret) AS rk_ret,
               ROW_NUMBER() OVER (PARTITION BY horizon, name ORDER BY val) AS rk_val
        FROM pairs
    ) numbered
)
-- `tokens` is the number that decides significance, not `n`. With a 60-minute
-- re-entry cooldown the same coin is re-recorded hourly, and repeated marks on
-- one token are correlated observations -- counting them as independent makes
-- the threshold far too easy to clear.
SELECT horizon AS mins, name AS feature,
       COUNT(*) AS n, COUNT(DISTINCT token) AS tokens,
       ROUND(CORR(r_ret, r_val)::numeric, 3) AS rho,
       CASE WHEN COUNT(DISTINCT token) >= 30
             AND ABS(CORR(r_ret, r_val)) > 1.96 / SQRT(COUNT(DISTINCT token) - 1)
            THEN 'maybe' ELSE 'noise' END AS verdict
FROM ranked
GROUP BY horizon, name
ORDER BY horizon, ABS(CORR(r_ret, r_val)) DESC NULLS LAST;
