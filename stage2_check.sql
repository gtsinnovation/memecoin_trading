-- Stage 2 health + results, in one pass.
--
--   Get-Content stage2_check.sql | docker compose exec -T db psql -U postgres -d memecoin_trading
--
-- Sections 1-2 answer "is it recording?" and are meaningful within minutes.
-- Sections 3-6 answer "what does it say?" and are NOT meaningful until the
-- token counts are in the dozens. Read n before reading any number beside it.

-- Tunables. Each is guarded SEPARATELY, because one \if around all of them
-- meant that passing any single override (psql -v fee=1.5) skipped the
-- defaults for the others and the report failed on an unset variable.
--
-- Round-trip fee = PAPER_FEE_PERCENT_PER_SIDE * 2. Override with:
--   psql -v fee=1.5 -f stage2_check.sql
\if :{?fee}
\else
\set fee 0.5
\endif

-- Charged when price impact could not be measured at all. MUST match
-- paper_trading.PAPER_UNMEASURED_SLIPPAGE_PERCENT, or this report and the
-- Python one answer the same question differently.
\if :{?unmeasured_slip}
\else
\set unmeasured_slip 3.0
\endif

-- MUST match holder_concentration.TOP10_CONCENTRATION_CEILING_PERCENT.
\if :{?ceiling}
\else
\set ceiling 30.0
\endif

-- MUST match paper_trading.HORIZON_TOLERANCE (PAPER_HORIZON_TOLERANCE): a
-- mark taken later than horizon x this is not that horizon's return. It was
-- a hard-coded 1.5 in nine places, so changing the env knob silently made
-- this report and the dashboard measure different windows.
\if :{?horizon_tolerance}
\else
\set horizon_tolerance 1.5
\endif

-- MUST match paper_trading.REENTRY_COOLDOWN_MINUTES (PAPER_REENTRY_COOLDOWN_MINUTES).
\if :{?cooldown_min}
\else
\set cooldown_min 60
\endif

-- THE UNIT OF EVIDENCE: each token's FIRST evaluation, in the cohort that
-- evaluation assigned it. Every decision section below reads marks through
-- this view.
--
-- A token can be approved at one evaluation and rejected at the next. Counted
-- in both arms, the two samples shared tokens and were not independent; and a
-- token is only RE-evaluated if it survived in the discovery list, so later
-- evaluations are selected on outcome. The first evaluation is neither. It
-- also makes every "per-token" statistic well defined -- one observation per
-- token per horizon -- so "up" means the same thing in 5, 10b, 10c and the
-- dashboard's horizon_summary(): the first evaluation's return was above zero.
-- A TEMP view: it lives for this psql session only and changes nothing stored.
CREATE TEMP VIEW first_eval AS
SELECT DISTINCT ON (token_address) id, token_address, cohort
FROM paper_trades WHERE entry_model = 'IMMEDIATE'
ORDER BY token_address, evaluated_at, id;

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

-- THE DEDUP ALARM, stated so it can actually fire. rows_per_token_per_hour
-- above is averaged over every token and the whole run, so a dedup failure
-- on a handful of tokens -- or for one bad hour -- disappears into it and the
-- "materially above 2" signature is unreachable in practice. This counts the
-- thing itself: IMMEDIATE evaluations of the same token closer together than
-- the re-entry cooldown. It must be 0.
SELECT COUNT(*) FILTER (WHERE gap_min < :cooldown_min) AS evaluations_inside_cooldown,
       COUNT(DISTINCT token_address) FILTER (WHERE gap_min < :cooldown_min) AS tokens_affected,
       CASE WHEN COUNT(*) FILTER (WHERE gap_min < :cooldown_min) = 0 THEN 'ok'
            ELSE 'DEDUP FAILING' END AS verdict
FROM (SELECT token_address,
             EXTRACT(EPOCH FROM (evaluated_at - LAG(evaluated_at)
                 OVER (PARTITION BY token_address ORDER BY evaluated_at)))/60.0 AS gap_min
      FROM paper_trades WHERE entry_model = 'IMMEDIATE') g;

-- Tokens evaluated into BOTH cohorts, and what the first-evaluation rule
-- (first_eval, above) sets aside. Every decision section counts each token
-- once, in its first cohort; this is how much that excludes.
SELECT COUNT(*) AS tokens,
       COUNT(*) FILTER (WHERE n_cohorts > 1) AS tokens_in_both_cohorts,
       SUM(evaluations) AS evaluations,
       SUM(evaluations) - COUNT(*) AS later_evaluations_set_aside
FROM (SELECT token_address, COUNT(DISTINCT cohort) AS n_cohorts, COUNT(*) AS evaluations
      FROM paper_trades WHERE entry_model = 'IMMEDIATE' GROUP BY token_address) c;

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
\echo '=== 2b. PRICE-METHOD BOUNDARY: legacy versus side-aware observations ==='
-- NULL versions predate explicit method tagging and must not be pooled with
-- the side-aware sample. Invalid rows remain visible in coverage counts.
SELECT COALESCE(price_validation_version, '(legacy / unversioned)') AS price_method,
       cohort, entry_model,
       COUNT(DISTINCT t.id)::int AS rows,
       COUNT(DISTINCT token_address)::int AS tokens,
       COUNT(DISTINCT t.id) FILTER (WHERE t.status = 'INVALID_DATA')::int AS invalid_rows,
       COUNT(h.id)::int AS horizon_marks,
       MIN(evaluated_at) AS first_evaluation,
       MAX(evaluated_at) AS last_evaluation
FROM paper_trades t
LEFT JOIN paper_horizon_returns h ON h.paper_trade_id = t.id
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

\echo ''
\echo '=== 2c. CLEAN-SAMPLE HORIZON RETURNS: versioned rows only ==='
-- One first IMMEDIATE evaluation per token in the current resolver version.
-- This is an observational paper sample, not evidence of executable fills.
WITH first_eval AS (
    SELECT DISTINCT ON (token_address) id, token_address, cohort,
           assumed_slippage_percent
    FROM paper_trades
    WHERE entry_model = 'IMMEDIATE'
      AND price_validation_version = 'pair-side-aware-v1'
      AND pair_address IS NOT NULL
      AND status <> 'INVALID_DATA'
    ORDER BY token_address, evaluated_at, id
), marks AS (
    SELECT f.cohort, f.token_address, h.horizon_minutes, h.return_percent,
           h.return_percent - (:fee + 2 * ABS(COALESCE(f.assumed_slippage_percent, :unmeasured_slip))) AS net_return_percent
    FROM first_eval f
    JOIN paper_horizon_returns h ON h.paper_trade_id = f.id
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
)
SELECT horizon_minutes, cohort,
       COUNT(DISTINCT token_address)::int AS tokens,
       ROUND(AVG(return_percent), 2) AS mean_gross_pct,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY return_percent)::numeric, 2) AS median_gross_pct,
       ROUND(AVG(net_return_percent), 2) AS mean_net_pct,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY net_return_percent)::numeric, 2) AS median_net_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE net_return_percent > 0) / NULLIF(COUNT(*), 0), 1) AS positive_net_pct
FROM marks
GROUP BY horizon_minutes, cohort
ORDER BY horizon_minutes, cohort;
\echo ''
\echo '=== 2d. CLEAN-SAMPLE DECISION GAP: current resolver, pinned and valid ==='
-- One first IMMEDIATE evaluation per token in the current price resolver
-- version, with an immutable entry-pool identity and no INVALID_DATA flag.
-- This deliberately complements (rather than silently replaces) the
-- historical all-version sections below. It is still observational: gates
-- do not randomize cohorts, and quote marks do not prove executable fills.
WITH first_clean_eval AS (
    SELECT DISTINCT ON (token_address)
           id, token_address, cohort, assumed_slippage_percent
    FROM paper_trades
    WHERE entry_model = 'IMMEDIATE'
      AND price_validation_version = 'pair-side-aware-v1'
      AND pair_address IS NOT NULL
      AND status <> 'INVALID_DATA'
    ORDER BY token_address, evaluated_at, id
), per_token AS (
    SELECT f.token_address AS token,
           f.cohort,
           h.horizon_minutes AS mins,
           AVG(h.return_percent) AS gross_ret,
           AVG(h.return_percent
               - (:fee + 2 * ABS(COALESCE(f.assumed_slippage_percent, :unmeasured_slip)))) AS net_ret
    FROM first_clean_eval f
    JOIN paper_horizon_returns h ON h.paper_trade_id = f.id
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
    GROUP BY f.token_address, f.cohort, h.horizon_minutes
), per_cohort AS (
    SELECT mins, cohort, COUNT(*) AS tokens,
           AVG(gross_ret) AS gross_mean,
           AVG(net_ret) AS net_mean,
           COALESCE(VAR_SAMP(net_ret), 0) AS net_var
    FROM per_token
    GROUP BY mins, cohort
), gap AS (
    SELECT a.mins,
           a.tokens AS approved_tokens,
           r.tokens AS rejected_tokens,
           a.gross_mean AS approved_gross_mean,
           a.net_mean AS approved_net_mean,
           r.net_mean AS rejected_net_mean,
           a.net_mean - r.net_mean AS net_gap,
           SQRT(a.net_var / NULLIF(a.tokens, 0)
              + r.net_var / NULLIF(r.tokens, 0)) AS net_gap_se
    FROM per_cohort a
    JOIN per_cohort r ON r.mins = a.mins AND r.cohort = 'REJECTED'
    WHERE a.cohort = 'APPROVED'
)
SELECT mins,
       approved_tokens,
       rejected_tokens,
       ROUND(approved_gross_mean::numeric, 2) AS approved_gross_mean_pct,
       ROUND(approved_net_mean::numeric, 2) AS approved_net_mean_pct,
       ROUND(rejected_net_mean::numeric, 2) AS rejected_net_mean_pct,
       ROUND(net_gap::numeric, 2) AS approved_minus_rejected_net_pp,
       ROUND(net_gap_se::numeric, 2) AS se_of_net_gap,
       ROUND((net_gap / NULLIF(net_gap_se, 0))::numeric, 2) AS net_gap_t_stat,
       CASE
         WHEN LEAST(approved_tokens, rejected_tokens) < 30 THEN 'too few tokens'
         WHEN net_gap > 2 * net_gap_se THEN 'observational gap > 2 SE'
         ELSE 'no clear positive gap'
       END AS verdict
FROM gap
ORDER BY mins;

\echo ''
\echo '=== 2e. CURRENT-VERSION PRICE-ANOMALY SENSITIVITY ==='
-- Apply the same frozen-mark and 10x-hourly-volume flags as 10d/10e, but
-- only to the first eligible evaluation in the current resolver version.
-- This shows whether the large all-sample means also occur in the clean
-- resolver vintage. The screen is a sensitivity, not ground truth: an
-- unflagged quote still is not proof of an executable fill.
WITH first_clean_eval AS (
    SELECT DISTINCT ON (token_address)
           id, token_address, cohort, price_at_evaluation,
           tradeable_depth_usd, volume_h1_usd, assumed_slippage_percent
    FROM paper_trades
    WHERE entry_model = 'IMMEDIATE'
      AND price_validation_version = 'pair-side-aware-v1'
      AND pair_address IS NOT NULL
      AND status <> 'INVALID_DATA'
    ORDER BY token_address, evaluated_at, id
), mark_shape AS (
    SELECT h.paper_trade_id,
           COUNT(DISTINCT h.price) FILTER (
               WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
           ) AS distinct_marks,
           COUNT(*) FILTER (
               WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
           ) AS n_marks
    FROM first_clean_eval f
    JOIN paper_horizon_returns h ON h.paper_trade_id = f.id
    GROUP BY h.paper_trade_id
), scored AS (
    SELECT f.token_address AS token,
           f.cohort,
           h.horizon_minutes AS mins,
           h.return_percent AS gross_ret,
           h.return_percent
             - (:fee + 2 * ABS(COALESCE(f.assumed_slippage_percent, :unmeasured_slip))) AS net_ret,
           (COALESCE(f.tradeable_depth_usd, 0) / 2.0)
             * (SQRT(h.price / NULLIF(f.price_at_evaluation, 0)) - 1) AS buying_needed,
           COALESCE(f.volume_h1_usd, 0) AS vol_h1,
           s.distinct_marks,
           s.n_marks
    FROM first_clean_eval f
    JOIN paper_horizon_returns h ON h.paper_trade_id = f.id
    JOIN mark_shape s ON s.paper_trade_id = f.id
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
      AND h.price > 0
      AND f.price_at_evaluation > 0
), flagged AS (
    SELECT scored.*,
           (gross_ret > 1000 AND
             ((distinct_marks = 1 AND n_marks > 1)
              OR buying_needed > 10 * vol_h1)) AS suspect
    FROM scored
)
SELECT mins,
       cohort,
       COUNT(*) AS tokens,
       COUNT(*) FILTER (WHERE suspect) AS suspect_tokens,
       ROUND(AVG(gross_ret), 2) AS mean_gross_pct,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY gross_ret)::numeric, 2)
           AS median_gross_pct,
       ROUND(AVG(net_ret), 2) AS mean_net_pct,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY net_ret)::numeric, 2)
           AS median_net_pct,
       ROUND(AVG(net_ret) FILTER (WHERE NOT suspect), 2) AS mean_net_after_screen_pct,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY net_ret)
           FILTER (WHERE NOT suspect)::numeric, 2) AS median_net_after_screen_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE net_ret > 0)
           / NULLIF(COUNT(*), 0), 1) AS positive_net_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE net_ret > 0 AND NOT suspect)
           / NULLIF(COUNT(*) FILTER (WHERE NOT suspect), 0), 1)
           AS positive_net_after_screen_pct
FROM flagged
GROUP BY mins, cohort
ORDER BY mins, cohort;

\echo ''
\echo '=== 2f. OUTLIER MARK-TIME EVIDENCE ==='
-- Existing horizon rows remain NULL; this section is informative after a fresh
-- run records new marks with the attached pool context. The current-first flag
-- lets this all-evaluation scan be cross-checked against Section 2e's cohort.
WITH first_current AS (
    SELECT DISTINCT ON (token_address) id, token_address
    FROM paper_trades
    WHERE entry_model = 'IMMEDIATE'
      AND price_validation_version = 'pair-side-aware-v1'
      AND pair_address IS NOT NULL
      AND status <> 'INVALID_DATA'
    ORDER BY token_address, evaluated_at, id
)
SELECT t.id AS paper_trade_id,
       COALESCE(t.price_validation_version, '(legacy / unversioned)') AS price_method,
       CASE WHEN f.id = t.id THEN 'yes' ELSE 'no' END AS first_current_eval,
       t.cohort,
       t.entry_model,
       LEFT(t.token_address, 8) AS token,
       h.horizon_minutes AS mins,
       ROUND(h.return_percent::numeric, 2) AS gross_return_pct,
       ROUND((h.price / NULLIF(t.price_at_evaluation, 0))::numeric, 2) AS price_multiple,
       ROUND(h.mark_liquidity_usd::numeric, 0) AS mark_liquidity_usd,
       ROUND(h.mark_volume_h1_usd::numeric, 0) AS mark_volume_h1_usd,
       h.mark_txns_m5,
       h.mark_txns_h1,
       h.marked_at,
       CASE WHEN h.mark_liquidity_usd IS NULL
                  OR h.mark_volume_h1_usd IS NULL
                  OR h.mark_txns_m5 IS NULL
                  OR h.mark_txns_h1 IS NULL
            THEN 'mark context missing' ELSE 'mark context captured' END AS context_status
FROM paper_horizon_returns h
JOIN paper_trades t ON t.id = h.paper_trade_id
LEFT JOIN first_current f ON f.id = t.id
WHERE h.return_percent > 1000
  AND h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
  AND h.price > 0 AND t.price_at_evaluation > 0
  AND t.pair_address IS NOT NULL
ORDER BY h.return_percent DESC
LIMIT 25;

\echo ''
\echo '=== 2g. SIZE-AWARE SLIPPAGE QUOTE COVERAGE ==='
SELECT COALESCE(price_validation_version, '(legacy / unversioned)') AS price_method,
       cohort,
       entry_model,
       COUNT(*) AS rows,
       COUNT(slippage_probe_usd) AS rows_with_probe_size,
       COUNT(*) FILTER (WHERE slippage_probe_usd IS NOT NULL
                         AND assumed_slippage_percent IS NOT NULL) AS rows_with_sized_impact,
       ROUND(100.0 * COUNT(slippage_probe_usd) / NULLIF(COUNT(*), 0), 1) AS pct_with_probe_size,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY slippage_probe_usd)::numeric, 2)
           AS median_probe_size_usd,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY assumed_slippage_percent)::numeric, 3)
           AS median_quoted_impact_pct
FROM paper_trades
GROUP BY price_validation_version, cohort, entry_model
ORDER BY price_method, cohort, entry_model;

\echo ''
\echo '=== 2h. CURRENT-VERSION QUOTE COVERAGE BY EVALUATION HOUR ==='
-- Distinguish a recent instrumentation rollout from a continuing capture gap.
-- A measured impact without its exact quote notional is not size-aware evidence.
SELECT date_trunc('hour', evaluated_at) AS evaluation_hour,
       cohort,
       COUNT(*) AS rows,
       COUNT(slippage_probe_usd) AS rows_with_probe_size,
       COUNT(assumed_slippage_percent) AS rows_with_impact,
       COUNT(*) FILTER (WHERE slippage_probe_usd IS NOT NULL
                         AND assumed_slippage_percent IS NOT NULL) AS rows_with_both,
       ROUND(100.0 * COUNT(*) FILTER (WHERE slippage_probe_usd IS NOT NULL
                                       AND assumed_slippage_percent IS NOT NULL)
             / NULLIF(COUNT(*), 0), 1) AS pct_with_both
FROM paper_trades
WHERE entry_model = 'IMMEDIATE'
  AND price_validation_version = 'pair-side-aware-v1'
GROUP BY 1, 2
ORDER BY 1, 2;

\echo ''
\echo '=== 2i. CURRENT-VERSION MARK CONTEXT BY MARK HOUR ==='
-- Mark context can be absent in older rows or when the provider omits fields.
-- The hourly shape shows whether complete context is appearing in new marks.
SELECT date_trunc('hour', h.marked_at) AS mark_hour,
       COUNT(*) AS marks,
       COUNT(*) FILTER (WHERE h.mark_liquidity_usd IS NOT NULL
                         AND h.mark_volume_h1_usd IS NOT NULL
                         AND h.mark_txns_m5 IS NOT NULL
                         AND h.mark_txns_h1 IS NOT NULL) AS marks_with_full_context,
       COUNT(*) FILTER (WHERE h.mark_liquidity_usd IS NULL) AS missing_liquidity,
       COUNT(*) FILTER (WHERE h.mark_volume_h1_usd IS NULL) AS missing_volume,
       COUNT(*) FILTER (WHERE h.mark_txns_m5 IS NULL) AS missing_txns_m5,
       COUNT(*) FILTER (WHERE h.mark_txns_h1 IS NULL) AS missing_txns_h1
FROM paper_horizon_returns h
JOIN paper_trades t ON t.id = h.paper_trade_id
WHERE t.entry_model = 'IMMEDIATE'
  AND t.price_validation_version = 'pair-side-aware-v1'
GROUP BY 1
ORDER BY 1;

\echo ''
\echo '=== 3. CANDIDATE QUALITY: are these tokens actually trading? ==='
-- The $USEFUL problem. A high no_txn_pct means discovery is surfacing dead
-- tokens whose "price" is a stale last print -- pure noise, which dilutes
-- any real signal rather than creating a false one.
-- Counts are per DISTINCT TOKEN in its FIRST assigned cohort, not per row
-- or per every cohort it ever visited. With a 60-minute re-entry
-- cooldown a token that stays in the discovery list all day is re-evaluated
-- hourly, so one persistently-listed dead token could contribute 24 rows
-- while 20 live tokens contribute 20 -- turning a true 5% dead rate into a
-- reported 55%. This is the same row-vs-token error section 1 warns about.
-- NULL txns_h1 (rows written before the column existed) is reported
-- separately rather than counted as a dead token.
SELECT t.cohort,
       COUNT(DISTINCT t.token_address) AS tokens,
       COUNT(DISTINCT t.token_address) FILTER (WHERE t.txns_h1 = 0) AS no_txns,
       COUNT(DISTINCT t.token_address) FILTER (WHERE t.txns_h1 IS NULL) AS txns_unknown,
       ROUND(100.0 * COUNT(DISTINCT t.token_address) FILTER (WHERE t.txns_h1 = 0)
             / NULLIF(COUNT(DISTINCT t.token_address),0)) AS no_txn_pct,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.txns_h1)::numeric, 0) AS median_txns_h1,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.volume_h1_usd)::numeric, 0) AS median_vol_h1,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.tradeable_depth_usd)::numeric, 0) AS median_depth
FROM paper_trades t
JOIN first_eval fe ON fe.id = t.id
WHERE t.entry_model = 'IMMEDIATE'
GROUP BY t.cohort ORDER BY t.cohort;

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
\echo '=== 3c. HORIZON DROPOUT per day (persisted by the pipeline) ==='
-- The share of due horizon marks that could not be taken, because the token
-- stopped pricing or had no basis. Every horizon statistic describes the
-- survivors only; a rising rate here means they are a smaller, more selected
-- group. Row-ticks, so read the percentage, not the counts.
SELECT to_char(date_trunc('day', hour), 'YYYY-MM-DD') AS day,
       SUM(due) AS due, SUM(dropped_no_price) AS no_price, SUM(dropped_no_basis) AS no_basis,
       ROUND(100.0 * SUM(dropped_no_price + dropped_no_basis) / NULLIF(SUM(due), 0), 1) AS dropout_pct
FROM paper_horizon_dropout GROUP BY 1 ORDER BY 1 DESC LIMIT 14;

\echo ''
\echo '--- 3d. PRICE INTEGRITY: candidates rejected before entry ---'
-- An INVALID_DATA paper row is kept for the candidate funnel, but is never
-- marked as a fill or included in return/path analysis.
SELECT t.cohort,
       COUNT(DISTINCT t.token_address) AS first_eval_tokens,
       COUNT(DISTINCT t.token_address) FILTER (WHERE t.status = 'INVALID_DATA')
           AS price_integrity_rejections
FROM paper_trades t
JOIN first_eval fe ON fe.id = t.id
WHERE t.entry_model = 'IMMEDIATE'
GROUP BY t.cohort ORDER BY t.cohort;

\echo ''
\echo '=== 3e. HOURLY HORIZON DROPOUT ==='
-- The daily section above can hide a short outage or deployment boundary.
-- These are still repeated due-row ticks, not unique trades permanently lost.
SELECT hour, due, marked, dropped_no_price, dropped_no_basis,
       ROUND(100.0 * (dropped_no_price + dropped_no_basis)
             / NULLIF(due, 0), 1) AS dropout_pct
FROM paper_horizon_dropout
WHERE hour >= CURRENT_TIMESTAMP - INTERVAL '7 days'
ORDER BY hour DESC;

\echo ''
\echo '=== 4. STALENESS: how much of the sample is a repeated stale quote? ==='
-- A live token essentially never reprices to the IDENTICAL figure 30 minutes
-- later. A dead one does, every time.
--
-- Marks taken INSIDE their horizon window only -- the same filter
-- staleness_report() applies. Unfiltered, a mark taken hours late on a token
-- long dead comes back at exactly zero and counts as "stale", which is how
-- the dashboard said 9 percent while this section said 62.
SELECT t.cohort,
       COUNT(h.id) AS marks,
       COUNT(h.id) FILTER (WHERE h.return_percent = 0) AS zero_returns,
       ROUND(100.0 * COUNT(h.id) FILTER (WHERE h.return_percent = 0) / NULLIF(COUNT(h.id),0)) AS zero_pct
FROM paper_trades t JOIN paper_horizon_returns h ON h.paper_trade_id = t.id
WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
GROUP BY t.cohort ORDER BY t.cohort;

\echo ''
\echo '=== 5. HORIZON RETURNS by cohort -- the measurement that matters ==='
-- TOKEN-WEIGHTED: one observation per token -- its FIRST evaluation (see
-- first_eval at the top). Every token counts once regardless of how many
-- times it was re-recorded; the per-token AVG below is now over one mark.
--
-- Averaging raw marks was wrong in a way that flattered the result. A
-- 60-minute re-entry cooldown means a token that stays in the discovery
-- list all day contributes ~24 rows while a token seen once contributes 1,
-- so a row-weighted mean is dominated by whichever coins happened to linger
-- -- and those are not a random sample. Worse, repeated marks on one token
-- are correlated, so treating rows as independent understates the standard
-- error by roughly sqrt(rows/tokens): at 154 rows over 19 tokens that is a
-- factor of 2.8, which turns noise into an apparent edge.
--
-- `se` is the standard error of the token-level mean and is the only number
-- here that says whether a gap is real. A cohort gap smaller than about
-- twice the larger `se` is not evidence of anything. Section 5c does that
-- subtraction explicitly.
--
-- `net` charges the round-trip fee (:fee) plus twice the slippage, with
-- unmeasured slippage charged at :unmeasured_slip rather than skipped --
-- letting it propagate to NULL made `mean` and `net` describe different
-- populations (AVG ignores NULL rows), the more liquid subset being the one
-- that survived. pct_assumed shows how much of `net` rests on that
-- assumption.
WITH marks AS (
    SELECT h.horizon_minutes AS mins,
           t.cohort,
           t.token_address AS token,
           h.return_percent AS ret,
           h.return_percent
             - (:fee + 2 * ABS(COALESCE(t.assumed_slippage_percent, :unmeasured_slip)))
             AS net_ret,
           (t.assumed_slippage_percent IS NULL)::int AS assumed
    FROM paper_horizon_returns h
    JOIN paper_trades t ON t.id = h.paper_trade_id
    JOIN first_eval fe ON fe.id = t.id
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
),
per_token AS (
    SELECT mins, cohort, token,
           AVG(ret) AS ret,
           AVG(net_ret) AS net_ret,
           AVG(assumed::numeric) AS assumed_share,
           AVG((ret > 0)::int::numeric) AS up_share,
           COUNT(*) AS marks
    FROM marks GROUP BY 1, 2, 3
)
SELECT mins, cohort,
       COUNT(*) AS tokens,
       SUM(marks) AS marks,
       ROUND(AVG(ret), 2) AS mean,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ret)::numeric, 2) AS median,
       ROUND(STDDEV_SAMP(ret), 2) AS stdev,
       ROUND((STDDEV_SAMP(ret) / NULLIF(SQRT(COUNT(*)), 0))::numeric, 2) AS se,
       ROUND(AVG(net_ret), 2) AS net,
       ROUND(100.0 * AVG(assumed_share)) AS pct_assumed,
       ROUND(100.0 * AVG(up_share)) AS pct_up
FROM per_token GROUP BY 1, 2 ORDER BY 1, 2;

\echo ''
\echo '=== 5c. THE DECISION NUMBER: approved minus rejected, against its error ==='
-- One row per horizon: the gap, and how big the gap would have to be to
-- mean anything. `verdict` is deliberately blunt -- 'signal' requires the
-- gap to clear two standard errors AND both arms to have enough tokens for
-- the standard error itself to be trustworthy.
--
-- Power, so the wait is not a surprise: detecting a 10-percentage-point
-- effect at memecoin variance needs roughly 144 tokens per arm. Reading
-- this table at 19 tokens tells you nothing either way -- and 'noise' at a
-- small n is not evidence the gates are worthless, only that the question
-- has not been asked yet.
WITH marks AS (
    SELECT h.horizon_minutes AS mins, t.cohort, t.token_address AS token,
           h.return_percent AS ret
    FROM paper_horizon_returns h
    JOIN paper_trades t ON t.id = h.paper_trade_id
    JOIN first_eval fe ON fe.id = t.id
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
),
per_token AS (
    SELECT mins, cohort, token, AVG(ret) AS ret FROM marks GROUP BY 1, 2, 3
),
per_cohort AS (
    SELECT mins, cohort, COUNT(*) AS tokens, AVG(ret) AS mean,
           COALESCE(VAR_SAMP(ret), 0) AS var
    FROM per_token GROUP BY 1, 2
),
gap AS (
    SELECT a.mins, a.tokens AS approved_tokens, r.tokens AS rejected_tokens,
           a.mean - r.mean AS diff,
           SQRT(a.var / NULLIF(a.tokens, 0) + r.var / NULLIF(r.tokens, 0)) AS se_diff
    FROM per_cohort a JOIN per_cohort r ON r.mins = a.mins AND r.cohort = 'REJECTED'
    WHERE a.cohort = 'APPROVED'
)
SELECT mins, approved_tokens, rejected_tokens,
       ROUND(diff::numeric, 2) AS approved_minus_rejected,
       ROUND(se_diff::numeric, 2) AS se_of_gap,
       ROUND((diff / NULLIF(se_diff, 0))::numeric, 2) AS t_stat,
       CASE
         WHEN LEAST(approved_tokens, rejected_tokens) < 30 THEN 'too few tokens'
         WHEN ABS(diff) > 2 * se_diff THEN 'signal'
         ELSE 'noise'
       END AS verdict
FROM gap ORDER BY mins;

\echo ''
\echo '=== 5b. Same as 5, but LIVE tokens only (>= 50 txns in the hour) ==='
-- If the cohort gap appears ONLY here, the gates work and DISCOVERY is what
-- needs fixing. That is a completely different repair from the one the
-- barrier results pointed at. Token-weighted for the same reason as 5.
WITH marks AS (
    SELECT h.horizon_minutes AS mins, t.cohort, t.token_address AS token,
           h.return_percent AS ret
    FROM paper_horizon_returns h
    JOIN paper_trades t ON t.id = h.paper_trade_id
    JOIN first_eval fe ON fe.id = t.id
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
      AND t.txns_h1 >= 50
),
per_token AS (
    SELECT mins, cohort, token, AVG(ret) AS ret, COUNT(*) AS marks
    FROM marks GROUP BY 1, 2, 3
)
SELECT mins, cohort,
       COUNT(*) AS tokens, SUM(marks) AS marks,
       ROUND(AVG(ret), 2) AS mean,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ret)::numeric, 2) AS median,
       ROUND(STDDEV_SAMP(ret), 2) AS stdev,
       ROUND((STDDEV_SAMP(ret) / NULLIF(SQRT(COUNT(*)), 0))::numeric, 2) AS se
FROM per_token GROUP BY 1, 2 ORDER BY 1, 2;

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
    JOIN first_eval fe ON fe.id = t.id
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
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

\echo ''
\echo '=== 7. WHY CANDIDATES ARE REFUSED -- per gate, and within F_ATLAS ==='
-- The funnel, by distinct token rather than by row. F_ATLAS is split by
-- reason because its two refusals are opposite problems: concentration over
-- the ceiling is the gate working, concentration that could not be measured
-- is a data-coverage failure. `reject_reason` is what makes that separable -- before it
-- existed both recorded rejected_by='F_ATLAS'.
SELECT COALESCE(t.rejected_by, '(approved)') AS gate,
       CASE
         WHEN t.reject_reason ILIKE '%unavailable%' OR t.reject_reason ILIKE '%could not be measured%'
           THEN 'unmeasurable'
         WHEN t.reject_reason IS NULL THEN ''
         ELSE 'failed the rule'
       END AS kind,
       COUNT(DISTINCT t.token_address) AS tokens,
       ROUND(100.0 * COUNT(DISTINCT t.token_address)
             / NULLIF(SUM(COUNT(DISTINCT t.token_address)) OVER (), 0), 1) AS pct
FROM paper_trades t
JOIN first_eval fe ON fe.id = t.id
WHERE t.entry_model = 'IMMEDIATE'
GROUP BY 1, 2 ORDER BY tokens DESC;

\echo ''
\echo '=== 8. HOLDER CONCENTRATION: how far apart are the three definitions? ==='
-- The calibration that decides which definition the 30% ceiling should be
-- applied to. Measurement only -- no gate reads the chain columns yet.
--
-- ONE ROW PER TOKEN (its most recent evaluation). Counting rows would weight
-- a token that lingered in the discovery list all day five times against one
-- seen once -- the same row-vs-token error section 5 exists to correct, and
-- it would bias this calibration toward whatever the persistent tokens look
-- like.
--
-- chain_covered_pct against provider_covered_pct is the coverage argument:
-- if chain measures tokens the provider cannot, moving the gate onto a chain
-- definition recovers candidates currently refused for no reason other than
-- absence. median_raw_minus_wallet is the size of the LP-pool distortion; a
-- large gap means raw chain concentration cannot reuse the 30% ceiling. A
-- small median_wallet_minus_provider means the wallet definition can.
WITH latest AS (
    SELECT DISTINCT ON (token_address)
           token_address, holder_pct_provider, holder_pct_chain_raw,
           holder_pct_chain_wallet, holder_pct_chain_program
    FROM paper_trades
    WHERE entry_model = 'IMMEDIATE'
    ORDER BY token_address, evaluated_at DESC
)
SELECT COUNT(*) AS tokens,
       ROUND(100.0 * COUNT(holder_pct_provider) / NULLIF(COUNT(*), 0)) AS provider_covered_pct,
       ROUND(100.0 * COUNT(holder_pct_chain_wallet) / NULLIF(COUNT(*), 0)) AS chain_covered_pct,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY holder_pct_provider)::numeric, 1) AS median_provider,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY holder_pct_chain_wallet)::numeric, 1) AS median_wallet,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY holder_pct_chain_raw)::numeric, 1) AS median_raw,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
             ORDER BY holder_pct_chain_raw - holder_pct_chain_wallet)::numeric, 1)
             AS median_raw_minus_wallet,
       -- Computed only where BOTH exist, which is the only place the
       -- comparison means anything -- and note that those tokens are the
       -- ones the provider already covers, so this gap says nothing about
       -- the tokens it does not.
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
             ORDER BY holder_pct_chain_wallet - holder_pct_provider)::numeric, 1)
             AS median_wallet_minus_provider
FROM latest;

\echo ''
\echo '=== 8b. How many tokens would each definition reject at the ceiling? ==='
-- The funnel consequence of the choice, per token. A definition that rejects
-- almost everything is not a stricter gate, it is a mismeasured one -- and
-- that is the expected shape for chain_raw, which counts the liquidity pool
-- as a holder.
WITH latest AS (
    SELECT DISTINCT ON (token_address)
           token_address, holder_pct_provider, holder_pct_chain_raw,
           holder_pct_chain_wallet
    FROM paper_trades
    WHERE entry_model = 'IMMEDIATE'
    ORDER BY token_address, evaluated_at DESC
),
defs AS (
    SELECT 'provider' AS definition, holder_pct_provider AS pct FROM latest
    UNION ALL SELECT 'chain_wallet', holder_pct_chain_wallet FROM latest
    UNION ALL SELECT 'chain_raw', holder_pct_chain_raw FROM latest
)
SELECT definition,
       COUNT(pct) AS measured,
       COUNT(*) FILTER (WHERE pct IS NULL) AS unmeasurable,
       COUNT(*) FILTER (WHERE pct > :ceiling) AS over_ceiling,
       ROUND(100.0 * COUNT(*) FILTER (WHERE pct IS NULL OR pct > :ceiling)
             / NULLIF(COUNT(*), 0)) AS would_refuse_pct
FROM defs GROUP BY definition
ORDER BY would_refuse_pct;

\echo ''
\echo '=== 9. THE TWO POPULATIONS -- never pool these ==='
-- 'holding-pen' is a newly created pool that aged into the 15-90 minute
-- window. 'breadth' is an established token off a trending or top-traded
-- list. They are different populations and a gap measured across both is not
-- one result -- it is two, averaged.
--
-- This is also the health check for the recency arm. If holding-pen tokens
-- stop appearing, the run quietly became a study of established tokens, and
-- that fact needs to be visible here rather than remembered.
SELECT COALESCE(t.discovery_source, '(pre-instrumentation)') AS population,
       t.cohort,
       COUNT(DISTINCT t.token_address) AS tokens,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.tradeable_depth_usd)::numeric, 0) AS median_depth,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.txns_h1)::numeric, 0) AS median_txns_h1
FROM paper_trades t
JOIN first_eval fe ON fe.id = t.id
WHERE t.entry_model = 'IMMEDIATE'
GROUP BY 1, 2 ORDER BY 1, 2;

\echo ''
\echo '=== 9b. THE DECISION NUMBER, PER POPULATION ==='
-- Section 5c, split. Token-weighted, with the standard error of the gap.
-- Read the verdict column, and read `tokens` before the verdict: below 30 per
-- arm nothing here means anything, and a 'noise' verdict at small n is not
-- evidence the gates are worthless -- only that the question is unanswered.
WITH marks AS (
    SELECT COALESCE(t.discovery_source, '(pre-instrumentation)') AS population,
           h.horizon_minutes AS mins, t.cohort, t.token_address AS token,
           h.return_percent AS ret
    FROM paper_horizon_returns h
    JOIN paper_trades t ON t.id = h.paper_trade_id
    JOIN first_eval fe ON fe.id = t.id
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
),
per_token AS (
    SELECT population, mins, cohort, token, AVG(ret) AS ret
    FROM marks GROUP BY 1, 2, 3, 4
),
per_cohort AS (
    SELECT population, mins, cohort, COUNT(*) AS tokens, AVG(ret) AS mean,
           COALESCE(VAR_SAMP(ret), 0) AS var
    FROM per_token GROUP BY 1, 2, 3
),
gap AS (
    SELECT a.population, a.mins,
           a.tokens AS approved_tokens, r.tokens AS rejected_tokens,
           a.mean - r.mean AS diff,
           SQRT(a.var / NULLIF(a.tokens, 0) + r.var / NULLIF(r.tokens, 0)) AS se_diff
    FROM per_cohort a
    JOIN per_cohort r ON r.mins = a.mins AND r.population = a.population
                     AND r.cohort = 'REJECTED'
    WHERE a.cohort = 'APPROVED'
)
SELECT population, mins, approved_tokens, rejected_tokens,
       ROUND(diff::numeric, 2) AS approved_minus_rejected,
       ROUND(se_diff::numeric, 2) AS se_of_gap,
       CASE
         WHEN LEAST(approved_tokens, rejected_tokens) < 30 THEN 'too few tokens'
         WHEN ABS(diff) > 2 * se_diff THEN 'signal'
         ELSE 'noise'
       END AS verdict
FROM gap ORDER BY population, mins;

\echo ''
\echo '=== 10. THE ROBUST DECISION NUMBER -- rank-based, outlier-proof ==='
-- Sections 5c and 9b compare MEANS, and on this data that is the wrong tool.
-- One mark printed +13,054,508% on a token with twenty transactions in an
-- hour; another showed an identical +6,711% at 30, 60 AND 120 minutes, which
-- is a price that moved once and froze. Those are not observations the mean
-- should be allowed to weigh, and they inflate the standard error until every
-- verdict reads 'noise' regardless of what the sample says.
--
-- Section 6 already reasoned this out for the correlations -- ranked, not
-- raw, because "one 40x runner dictates a Pearson coefficient entirely" --
-- and then the section that actually decides things used means anyway.
--
-- This is Mann-Whitney U on TOKEN-LEVEL MEDIANS. Each token contributes the
-- median of its own marks (robust to one bad print within a token), those are
-- ranked across tokens, and the test asks whether approved tokens sit higher
-- in the ranking than chance allows. The magnitude of a runner is irrelevant
-- to a rank; only its position matters. No trimming, no winsorising, no
-- judgement call about which outliers are "real".
WITH marks AS (
    SELECT COALESCE(t.discovery_source, '(pre)') AS population,
           h.horizon_minutes AS mins, t.cohort, t.token_address AS token,
           h.return_percent AS ret
    FROM paper_horizon_returns h
    JOIN paper_trades t ON t.id = h.paper_trade_id
    JOIN first_eval fe ON fe.id = t.id
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
),
per_token AS (
    SELECT population, mins, cohort, token,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ret) AS ret
    FROM marks GROUP BY 1, 2, 3, 4
),
-- MID-ranks: ties get the average of the positions they span. A large block
-- of tokens tied at exactly 0.00 return -- dead ones -- would otherwise be
-- min-ranked, which manufactures separation between cohorts whose only
-- shared property is being stale.
ranked AS (
    SELECT population, mins, cohort, token,
           AVG(rk) OVER (PARTITION BY population, mins, ret) AS r
    FROM (SELECT population, mins, cohort, token, ret,
                 ROW_NUMBER() OVER (PARTITION BY population, mins ORDER BY ret) AS rk
          FROM per_token) n
),
agg AS (
    SELECT population, mins,
           COUNT(*) FILTER (WHERE cohort = 'APPROVED') AS n1,
           COUNT(*) FILTER (WHERE cohort = 'REJECTED') AS n2,
           SUM(r) FILTER (WHERE cohort = 'APPROVED') AS r1
    FROM ranked GROUP BY 1, 2
),
u AS (
    SELECT population, mins, n1, n2,
           r1 - (n1 * (n1 + 1) / 2.0) AS u1,
           n1 * n2 / 2.0 AS mu,
           SQRT(n1 * n2 * (n1 + n2 + 1) / 12.0) AS sigma
    FROM agg WHERE n1 > 0 AND n2 > 0
)
SELECT population, mins, n1 AS approved, n2 AS rejected,
       -- Probability a randomly chosen approved token outranks a randomly
       -- chosen rejected one. 0.50 is no edge. This is the effect size, and
       -- unlike a mean it cannot be moved by how big the biggest winner was.
       ROUND((u1 / (n1 * n2))::numeric, 3) AS p_outrank,
       ROUND(((u1 - mu) / NULLIF(sigma, 0))::numeric, 2) AS z,
       CASE
         WHEN LEAST(n1, n2) < 30 THEN 'too few tokens'
         WHEN ABS((u1 - mu) / NULLIF(sigma, 0)) > 1.96 THEN 'SIGNAL'
         ELSE 'noise'
       END AS verdict
FROM u ORDER BY population, mins;

\echo ''
\echo '=== 10b. UP-RATE: the other robust statistic, and the larger gap ==='
-- "How often was this token up at the horizon" cannot be distorted by a
-- 13,000,000% print -- it counts as one success, the same as +0.01%. On the
-- 16-hour sample this showed 71% for approved against 51% for rejected,
-- which is a bigger separation than anything in the return columns.
--
-- Two-proportion z-test on TOKENS, not marks: repeated marks on one token
-- are correlated observations and counting them as independent is how a
-- 60-minute re-entry cooldown turns 19 coins into "154 trades".
WITH marks AS (
    SELECT COALESCE(t.discovery_source, '(pre)') AS population,
           h.horizon_minutes AS mins, t.cohort, t.token_address AS token,
           h.return_percent AS ret
    FROM paper_horizon_returns h
    JOIN paper_trades t ON t.id = h.paper_trade_id
    JOIN first_eval fe ON fe.id = t.id
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
),
per_token AS (
    SELECT population, mins, cohort, token,
           (PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ret) > 0)::int AS up
    FROM marks GROUP BY 1, 2, 3, 4
),
agg AS (
    SELECT population, mins,
           COUNT(*) FILTER (WHERE cohort = 'APPROVED') AS n1,
           SUM(up) FILTER (WHERE cohort = 'APPROVED') AS k1,
           COUNT(*) FILTER (WHERE cohort = 'REJECTED') AS n2,
           SUM(up) FILTER (WHERE cohort = 'REJECTED') AS k2
    FROM per_token GROUP BY 1, 2
),
z AS (
    SELECT *, k1::numeric / NULLIF(n1, 0) AS p1, k2::numeric / NULLIF(n2, 0) AS p2,
           (k1 + k2)::numeric / NULLIF(n1 + n2, 0) AS p
    FROM agg WHERE n1 > 0 AND n2 > 0
)
SELECT population, mins, n1 AS approved, n2 AS rejected,
       ROUND(100 * p1, 0) AS pct_up_approved,
       ROUND(100 * p2, 0) AS pct_up_rejected,
       ROUND((100 * (p1 - p2))::numeric, 1) AS gap_pp,
       ROUND(((p1 - p2) / NULLIF(SQRT(p * (1 - p) * (1.0/n1 + 1.0/n2)), 0))::numeric, 2) AS z,
       CASE
         WHEN LEAST(n1, n2) < 30 THEN 'too few tokens'
         WHEN ABS((p1 - p2) / NULLIF(SQRT(p * (1 - p) * (1.0/n1 + 1.0/n2)), 0)) > 1.96
           THEN 'SIGNAL'
         ELSE 'noise'
       END AS verdict
FROM z ORDER BY population, mins;

\echo ''
\echo '=== 10c. THE LIVENESS CONFOUND -- is the edge just "not dead"? ==='
-- Section 4 shows the rejected cohort carrying EIGHT TIMES the stale-quote
-- rate (8 percent of marks at exactly 0.0000 against 1 percent), and section
-- 3b shows it losing four times as many tokens to abandonment. A gate that
-- mostly selects tokens still trading will look like alpha and is not.
--
-- TWO CORRECTIONS over the first version of this section, both of which
-- changed what it was capable of showing:
--
-- 1. SPLIT BY POPULATION. Breadth and holding-pen are different experiments
--    with different cohort mixes (section 9). Pooling them meant the bands
--    below mixed two samples, so a band could move because its population
--    mix moved rather than because anything about liveness did.
--
-- 2. TEST UP-RATE, NOT JUST RANK. The signal claimed in 10b is an UP-RATE
--    gap of 22 to 32 points. The first version tested p_outrank instead --
--    a different statistic -- so it could not confirm or refute the thing
--    it was written to interrogate. Both are reported here, because their
--    DISAGREEMENT is itself informative: a large up-rate gap beside a
--    p_outrank near 0.5 means approved tokens rise more OFTEN but not by
--    MORE, i.e. reliable small drift and no tail.
--
-- Read it this way: if the up-rate gap survives inside every activity band,
-- the gates are picking something beyond liveness. If it collapses to zero
-- within bands and only exists pooled, they are a liveness filter and no
-- threshold tuning changes that.
WITH marks AS (
    SELECT h.horizon_minutes AS mins, t.cohort, t.token_address AS token,
           COALESCE(t.discovery_source, 'unknown') AS population,
           h.return_percent AS ret,
           CASE WHEN t.txns_h1 IS NULL THEN 'z. unknown'
                WHEN t.txns_h1 < 50 THEN 'a. <50 txns (near dead)'
                WHEN t.txns_h1 < 500 THEN 'b. 50-500 txns'
                ELSE 'c. 500+ txns (busy)' END AS activity
    FROM paper_horizon_returns h
    JOIN paper_trades t ON t.id = h.paper_trade_id
    JOIN first_eval fe ON fe.id = t.id
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
),
per_token AS (
    SELECT population, activity, mins, cohort, token,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ret) AS ret
    FROM marks GROUP BY 1, 2, 3, 4, 5
),
ranked AS (
    SELECT population, activity, mins, cohort, ret,
           AVG(rk) OVER (PARTITION BY population, activity, mins, ret) AS r
    FROM (SELECT population, activity, mins, cohort, ret,
                 ROW_NUMBER() OVER (PARTITION BY population, activity, mins
                                    ORDER BY ret) AS rk
          FROM per_token) n
),
agg AS (
    SELECT population, activity, mins,
           COUNT(*) FILTER (WHERE cohort = 'APPROVED') AS n1,
           COUNT(*) FILTER (WHERE cohort = 'REJECTED') AS n2,
           SUM(r)   FILTER (WHERE cohort = 'APPROVED') AS r1,
           COUNT(*) FILTER (WHERE cohort = 'APPROVED' AND ret > 0) AS up1,
           COUNT(*) FILTER (WHERE cohort = 'REJECTED' AND ret > 0) AS up2,
           -- Tokens that MOVED AT ALL. A frozen quote returns exactly zero
           -- and so reads as "not up", which hands the up-rate gap to the
           -- cohort with fewer dead tokens for free. Excluding them is the
           -- sharpest single test of the confound.
           COUNT(*) FILTER (WHERE cohort = 'APPROVED' AND ret <> 0) AS mv1,
           COUNT(*) FILTER (WHERE cohort = 'REJECTED' AND ret <> 0) AS mv2,
           COUNT(*) FILTER (WHERE cohort = 'APPROVED' AND ret > 0) AS mvup1,
           COUNT(*) FILTER (WHERE cohort = 'REJECTED' AND ret > 0) AS mvup2
    FROM ranked GROUP BY 1, 2, 3
)
SELECT population, activity, mins,
       n1 AS appr, n2 AS rej,
       ROUND(((r1 - (n1 * (n1 + 1) / 2.0)) / NULLIF(n1 * n2, 0))::numeric, 3)
           AS p_outrank,
       ROUND(100.0 * up1 / NULLIF(n1, 0), 0) AS up_appr,
       ROUND(100.0 * up2 / NULLIF(n2, 0), 0) AS up_rej,
       ROUND(100.0 * up1 / NULLIF(n1, 0) - 100.0 * up2 / NULLIF(n2, 0), 1)
           AS up_gap_pp,
       -- The same gap among tokens that actually moved.
       ROUND(100.0 * mvup1 / NULLIF(mv1, 0) - 100.0 * mvup2 / NULLIF(mv2, 0), 1)
           AS up_gap_moved_pp,
       CASE WHEN LEAST(n1, n2) < 20 THEN 'too few' ELSE '' END AS note
FROM agg
WHERE n1 > 0 AND n2 > 0 AND mins = 60
ORDER BY population, activity;

\echo ''
\echo '--- 10d. PRICE SANITY: could these moves physically have happened? ---'
-- This operational scan covers every evaluation (unlike the first-evaluation
-- cohort statistics in Section 5). Group marks by trade/evaluation so one
-- token's later re-entry cannot hide or manufacture a frozen-price finding.
-- A token recorded at 0.0000117 and then at 1.53 is a 130,000x move; the
-- likelier explanation is the wrong side or wrong pool than a token that
-- actually did that in thirty minutes.
--
-- This version applies two tests that CAN fail.
--
-- TEST 1 -- PHYSICS. On a constant-product pool, moving the price by a factor
-- r requires the quote reserve to grow by sqrt(r), so the buying needed is
-- about (TVL / 2) * (sqrt(r) - 1). That capital has to come from somewhere,
-- and we recorded the hour's actual volume at evaluation. If the move needed
-- vastly more buying than the token saw, the move did not happen: the price
-- is wrong. This is deliberately generous -- it compares against the WHOLE
-- hour's volume, and uses depth at evaluation, which understates a pool that
-- genuinely grew. A row still failing this margin is not a borderline case.
--
-- TEST 2 -- FROZEN MARKS. The same token reporting the IDENTICAL price at 30,
-- 60 and 120 minutes did not moon three times; its quote stopped updating. A
-- frozen quote at an absurd level is the signature of a bad read, not a rally.
WITH x AS (
    SELECT t.id AS trade_id, t.cohort, t.discovery_source,
           LEFT(t.token_address, 8) AS token,
           h.horizon_minutes AS mins,
           t.price_at_evaluation AS basis,
           h.price AS mark,
           h.return_percent AS ret,
           h.mark_liquidity_usd,
           h.mark_volume_h1_usd,
           h.mark_txns_m5,
           h.mark_txns_h1,
           t.tradeable_depth_usd AS depth,
           t.volume_h1_usd AS vol_h1,
           h.price / NULLIF(t.price_at_evaluation, 0) AS ratio,
           f.distinct_marks, f.n_marks
    FROM paper_horizon_returns h
    JOIN paper_trades t ON t.id = h.paper_trade_id
    -- Per-trade mark spread, as its own aggregate. Postgres has no
    -- COUNT(DISTINCT ...) OVER (...), so this cannot be a window function.
    JOIN (SELECT t2.id AS trade_id,
                 COUNT(DISTINCT h2.price) AS distinct_marks,
                 COUNT(*)                 AS n_marks
          FROM paper_horizon_returns h2
          JOIN paper_trades t2 ON t2.id = h2.paper_trade_id
          GROUP BY t2.id) f ON f.trade_id = t.id
    WHERE h.return_percent > 1000          -- only the tail can distort a mean
),
y AS (
    SELECT x.*,
           CASE WHEN ratio > 1 THEN
               (COALESCE(depth, 0) / 2.0) * (SQRT(ratio) - 1)
           END AS buying_needed_usd
    FROM x
)
SELECT cohort, token, mins,
       basis, mark,
       ROUND(ratio, 0)                              AS price_x,
       ROUND(depth, 0)                              AS depth_usd,
       ROUND(vol_h1, 0)                             AS volume_h1,
       ROUND(mark_liquidity_usd, 0)                 AS mark_liquidity_usd,
       ROUND(mark_volume_h1_usd, 0)                 AS mark_volume_h1_usd,
       mark_txns_m5,
       mark_txns_h1,
       ROUND(buying_needed_usd, 0)                  AS buying_needed,
       -- How many times the entire hour's observed volume would have had to
       -- be spent, on this one token, to produce the recorded price.
       ROUND(buying_needed_usd / NULLIF(vol_h1, 0), 0) AS x_of_hourly_volume,
       CASE WHEN distinct_marks = 1 AND n_marks > 1
                 THEN 'FROZEN -- one price repeated across every horizon'
            WHEN depth IS NOT NULL AND depth > 0
                 AND vol_h1 IS NOT NULL
                 AND buying_needed_usd > 10 * vol_h1
                 THEN 'IMPOSSIBLE -- needs far more buying than the token saw'
            WHEN depth IS NULL OR depth <= 0 OR vol_h1 IS NULL
                 OR mark_liquidity_usd IS NULL OR mark_volume_h1_usd IS NULL
                 OR mark_txns_m5 IS NULL OR mark_txns_h1 IS NULL
                 THEN 'UNVERIFIED -- entry or mark context missing'
            ELSE 'plausible -- no test refutes it' END AS verdict
FROM y
ORDER BY ret DESC
LIMIT 15;

\echo ''
\echo '--- 10e. WHAT THE MEANS BECOME WITHOUT THE REFUTED ROWS ---'
-- This is the first-evaluation cohort sensitivity, matching Section 5.
-- Section 10d is a separate operational scan across every evaluation.
-- Here, frozen quotes and >1,000% returns are screened per paper trade;
-- the 1x/10x/100x columns show sensitivity to the volume plausibility bar.
-- These diagnostics flag suspect prices; they do not prove that every
-- unflagged quote was executable.
--
-- The median column is there to make the point: it does not move at all,
-- whatever is dropped. That is the argument for reading sections 10 and 10b
-- rather than any mean.
WITH scored AS (
    SELECT t.cohort, h.horizon_minutes AS mins, t.token_address AS token,
           h.return_percent AS ret,
           (COALESCE(t.tradeable_depth_usd, 0) / 2.0)
             * (SQRT(h.price / NULLIF(t.price_at_evaluation, 0)) - 1)
             AS buying_needed,
           COALESCE(t.volume_h1_usd, 0) AS vol_h1,
           f.distinct_marks, f.n_marks
    FROM paper_horizon_returns h
    JOIN paper_trades t ON t.id = h.paper_trade_id
    JOIN (SELECT t2.id AS trade_id,
                 COUNT(DISTINCT h2.price) AS distinct_marks,
                 COUNT(*)                 AS n_marks
          FROM paper_horizon_returns h2
          JOIN paper_trades t2 ON t2.id = h2.paper_trade_id
          JOIN first_eval fe2 ON fe2.id = t2.id
          GROUP BY t2.id) f ON f.trade_id = t.id
    JOIN first_eval fe ON fe.id = t.id
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * :horizon_tolerance
      AND h.price > 0 AND t.price_at_evaluation > 0
),
flagged AS (
    SELECT cohort, mins, token, ret,
           -- The same two tests 10d reports, applied identically here.
           (ret > 1000 AND distinct_marks = 1 AND n_marks > 1)  AS frozen,
           (ret > 1000 AND buying_needed >   1 * vol_h1)        AS imposs_1x,
           (ret > 1000 AND buying_needed >  10 * vol_h1)        AS imposs_10x,
           (ret > 1000 AND buying_needed > 100 * vol_h1)        AS imposs_100x
    FROM scored
),
per_token AS (
    SELECT cohort, mins, token,
           AVG(ret)                                               AS ret_all,
           AVG(ret) FILTER (WHERE NOT (frozen OR imposs_1x))      AS keep_1x,
           AVG(ret) FILTER (WHERE NOT (frozen OR imposs_10x))     AS keep_10x,
           AVG(ret) FILTER (WHERE NOT (frozen OR imposs_100x))    AS keep_100x,
           COUNT(*) FILTER (WHERE frozen)                         AS n_frozen,
           COUNT(*) FILTER (WHERE imposs_1x)                      AS n_imposs
    FROM flagged GROUP BY 1, 2, 3
)
SELECT mins, cohort,
       COUNT(*)                                     AS tokens,
       SUM(n_frozen)                                AS frozen_rows,
       SUM(n_imposs)                                AS impossible_rows,
       ROUND(AVG(ret_all),   2)                     AS mean_as_reported,
       ROUND(AVG(keep_100x), 2)                     AS mean_drop_100x,
       ROUND(AVG(keep_10x),  2)                     AS mean_drop_10x,
       ROUND(AVG(keep_1x),   2)                     AS mean_drop_1x,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ret_all)::numeric, 2)
                                                    AS median_unmoved
FROM per_token
GROUP BY mins, cohort
ORDER BY mins, cohort;

\echo ''
\echo '=== 10f. IS IT THE GATES OR THE STOP? -- from the ORDERED path ==='
-- Section 5 says approved tokens finish UP most of the time; section 11 says
-- approved trades LOSE money. Both hold if tokens dip through the 7.53 stop on
-- the way to finishing higher: the horizon measurement samples the endpoint,
-- the barrier trade experiences the path.
--
-- REBUILT on paper_price_path. The first version read min_price_seen /
-- max_price_seen, and was wrong four ways:
--   1. extremes carry NO ORDER, so "rose, then stopped" counted as "stopped
--      on noise, then recovered" -- the one distinction this section exists for;
--   2. LIMIT rows used price_at_evaluation as the basis, but a LIMIT enters on
--      a ~7% pullback, so its real stop sits ~14% below evaluation price and
--      would_stop came out near 100% BY CONSTRUCTION;
--   3. the extremes were updated while PENDING_FILL, before any entry existed;
--   4. every trade covered a different window depending on when production
--      stopped pricing it.
-- Now: basis is what the trade actually paid (fill_price for LIMIT, entry
-- only once filled), the window is [entry, entry + 360 min], and recovery is
-- tested strictly AFTER the first stop crossing.
--
-- Only trades with a recorded path appear. Paths began when paper_price_path
-- was created, so older trades are simply absent -- they are not zero.
WITH trades AS (
    SELECT t.id, t.token_address, t.pair_address, t.cohort, t.entry_model,
           CASE WHEN t.entry_model = 'LIMIT' THEN t.filled_at
                ELSE t.evaluated_at END AS entered,
           CASE WHEN t.entry_model = 'LIMIT' THEN t.fill_price
                ELSE t.price_at_evaluation END AS basis
    FROM paper_trades t
    WHERE t.status <> 'INVALID_DATA'
      AND t.pair_address IS NOT NULL
      AND ((t.entry_model = 'IMMEDIATE' AND t.price_at_evaluation > 0)
       OR (t.entry_model = 'LIMIT' AND t.filled_at IS NOT NULL AND t.fill_price > 0))
),
path AS (
    SELECT tr.id, tr.token_address, tr.cohort, tr.entry_model, tr.basis,
           p.observed_at, p.price,
           100.0 * (p.price - tr.basis) / tr.basis AS ret
    FROM trades tr
    JOIN paper_price_path p
      ON p.token_address = tr.token_address
     AND p.pair_address = tr.pair_address
     AND p.observed_at >= tr.entered
     AND p.observed_at <= tr.entered + INTERVAL '360 minutes'
),
per_trade AS (
    SELECT id, token_address, cohort, entry_model,
           MIN(ret) AS mae, MAX(ret) AS mfe,
           MIN(observed_at) FILTER (WHERE ret <= -7.53) AS first_stop
    FROM path GROUP BY 1, 2, 3, 4
),
judged AS (
    SELECT pt.*,
           -- Recovery STRICTLY AFTER the first stop crossing. This is the cell
           -- the first version could not compute: min/max had no order.
           EXISTS (SELECT 1 FROM path q
                   WHERE q.id = pt.id AND pt.first_stop IS NOT NULL
                     AND q.observed_at > pt.first_stop AND q.ret > 0)
               AS recovered_after_stop
    FROM per_trade pt
)
SELECT cohort, entry_model,
       COUNT(*)                                                     AS trades,
       COUNT(DISTINCT token_address)                                AS tokens,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mae)::numeric, 2) AS median_mae,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mfe)::numeric, 2) AS median_mfe,
       COUNT(*) FILTER (WHERE first_stop IS NOT NULL)               AS hit_stop,
       ROUND(100.0 * COUNT(*) FILTER (WHERE first_stop IS NOT NULL)
             / NULLIF(COUNT(*), 0), 0)                              AS hit_stop_pct,
       -- THE cell: stopped, and then traded back above entry afterwards.
       COUNT(*) FILTER (WHERE recovered_after_stop)                 AS stopped_then_recovered,
       ROUND(100.0 * COUNT(*) FILTER (WHERE recovered_after_stop)
             / NULLIF(COUNT(*) FILTER (WHERE first_stop IS NOT NULL), 0), 0)
                                                                    AS recovered_pct_of_stops
FROM judged
GROUP BY cohort, entry_model
ORDER BY cohort, entry_model;

\echo ''
\echo '--- 10g. WHAT STOP DISTANCE WOULD THE PATHS HAVE SURVIVED? ---'
-- A description of the observed paths, NOT a recommendation. A wider stop
-- keeps more trades alive AND makes each real loss bigger; this shows only the
-- first half, so on its own it cannot justify a change. Use replay_exits.py for
-- the trade-off, which applies the actual exit and cost rules.
-- Same ordered path and basis as 10f; IMMEDIATE only, so the entry is unambiguous.
WITH trades AS (
    SELECT t.id, t.token_address, t.pair_address, t.cohort, t.evaluated_at AS entered,
           t.price_at_evaluation AS basis
    FROM paper_trades t
    WHERE t.entry_model = 'IMMEDIATE' AND t.status <> 'INVALID_DATA'
      AND t.pair_address IS NOT NULL
      AND t.price_at_evaluation > 0
),
mae AS (
    SELECT tr.id, tr.cohort,
           MIN(100.0 * (p.price - tr.basis) / tr.basis) AS mae
    FROM trades tr
    JOIN paper_price_path p
      ON p.token_address = tr.token_address
     AND p.pair_address = tr.pair_address
     AND p.observed_at >= tr.entered
     AND p.observed_at <= tr.entered + INTERVAL '360 minutes'
    GROUP BY 1, 2
)
SELECT cohort, COUNT(*) AS trades,
       ROUND(100.0 * COUNT(*) FILTER (WHERE mae > -5)    / NULLIF(COUNT(*),0), 0) AS survive_5pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE mae > -7.53) / NULLIF(COUNT(*),0), 0) AS survive_current,
       ROUND(100.0 * COUNT(*) FILTER (WHERE mae > -12)   / NULLIF(COUNT(*),0), 0) AS survive_12pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE mae > -20)   / NULLIF(COUNT(*),0), 0) AS survive_20pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE mae > -35)   / NULLIF(COUNT(*),0), 0) AS survive_35pct
FROM mae GROUP BY cohort ORDER BY cohort;

\echo ''
\echo '--- 10h. POOL IDENTITY COVERAGE ---'
-- Historical rows without a stored pool cannot be reconciled to a single
-- market series. Keep them visible here, but exclude them from pool-pinned
-- return summaries and replay rather than guessing which pool produced them.
SELECT entry_model, status,
       COUNT(*) AS rows,
       COUNT(*) FILTER (WHERE pair_address IS NOT NULL) AS pool_pinned,
       COUNT(*) FILTER (WHERE pair_address IS NULL) AS pool_unverified,
       COUNT(DISTINCT pair_address) AS distinct_pools
FROM paper_trades
GROUP BY entry_model, status
ORDER BY entry_model, status;

\echo ''
\echo '=== 11. EXIT CONFIRMATION -- were the wins traded, or just quoted? ==='
-- A take-profit is a LIMIT SELL: it needs somebody on the other side. On a
-- token that has not traded in five minutes the quote is the last print, not
-- a price anyone will pay -- so a lone stale or wicked figure crossing the
-- target booked a clean win, AT the target, with no trade behind it.
--
-- The error is not symmetric across cohorts, which is why it matters here
-- rather than as a general nuisance: the gates select THIN tokens, and thin
-- is exactly where a single print moves the quote furthest. Fabricated wins
-- therefore land disproportionately in APPROVED -- in the same direction as
-- the effect this whole experiment is trying to detect.
--
-- Read the last two columns as a pair. If `net_confirmed` holds up against
-- `net_all`, the result survives. If the gap is large, the headline was
-- resting on prints nobody traded against, and the honest number is the
-- confirmed one -- computed on a smaller sample, with correspondingly wider
-- uncertainty.
SELECT cohort,
       entry_model,
       COUNT(*) FILTER (WHERE status = 'CLOSED')::int                     AS closed,
       COUNT(*) FILTER (WHERE status='CLOSED' AND exit_confirmed IS TRUE)::int
                                                                          AS confirmed,
       COUNT(*) FILTER (WHERE status='CLOSED' AND exit_confirmed IS FALSE)::int
                                                                          AS refuted,
       COUNT(*) FILTER (WHERE status='CLOSED' AND exit_confirmed IS NULL)::int
                                                                          AS unknown,
       ROUND(100.0 * COUNT(*) FILTER (WHERE status='CLOSED' AND exit_confirmed IS TRUE)
             / NULLIF(COUNT(*) FILTER (WHERE status = 'CLOSED'), 0), 1)   AS confirmed_pct,
       -- Target hits specifically: the exit the defect flatters.
       COUNT(*) FILTER (WHERE exit_reason='TARGET_HIT')::int              AS target_hits,
       COUNT(*) FILTER (WHERE exit_reason='TARGET_HIT'
                          AND exit_confirmed IS NOT TRUE)::int            AS target_hits_unconfirmed,
       ROUND(AVG(net_pnl_percent) FILTER (WHERE status='CLOSED'), 2)      AS net_all,
       ROUND(AVG(net_pnl_percent) FILTER (WHERE status='CLOSED'
                                            AND exit_confirmed IS TRUE), 2)
                                                                          AS net_confirmed
FROM paper_trades
GROUP BY cohort, entry_model
ORDER BY cohort, entry_model;

\echo ''
\echo '--- 11b. Is the CONFIRMED share itself different between cohorts? ---'
-- A cohort whose exits are confirmed far less often is not merely noisier:
-- its trades are being marked against quotes rather than trades, so every
-- other statistic about it is built on a weaker measurement. A large split
-- here is a finding in its own right, independent of the returns.
WITH per_cohort AS (
    SELECT cohort,
           COUNT(*) FILTER (WHERE status='CLOSED')                     AS closed,
           COUNT(*) FILTER (WHERE status='CLOSED' AND exit_confirmed IS TRUE) AS conf
    FROM paper_trades
    WHERE entry_model = 'IMMEDIATE'
    GROUP BY cohort
)
SELECT cohort, closed, conf,
       ROUND(100.0 * conf / NULLIF(closed, 0), 1) AS confirmed_pct,
       CASE WHEN closed < 30 THEN 'too few closed trades to compare'
            ELSE 'compare the two rows directly' END AS note
FROM per_cohort
ORDER BY cohort;
