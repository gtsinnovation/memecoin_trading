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
-- TOKEN-WEIGHTED, in two stages: average the marks within each token, then
-- average across tokens. Every token counts once regardless of how many
-- times it was re-recorded.
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
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * 1.5
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
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * 1.5
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
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * 1.5
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

\echo ''
\echo '=== 7. WHY CANDIDATES ARE REFUSED -- per gate, and within F_ATLAS ==='
-- The funnel, by distinct token rather than by row. F_ATLAS is split by
-- reason because its two refusals are opposite problems: concentration over
-- the ceiling is the gate working, concentration that could not be measured
-- is a data-coverage failure, and roughly two thirds of its rejections have
-- been the latter. `reject_reason` is what makes that separable -- before it
-- existed both recorded rejected_by='F_ATLAS'.
SELECT COALESCE(rejected_by, '(approved)') AS gate,
       CASE
         WHEN reject_reason ILIKE '%unavailable%' OR reject_reason ILIKE '%could not be measured%'
           THEN 'unmeasurable'
         WHEN reject_reason IS NULL THEN ''
         ELSE 'failed the rule'
       END AS kind,
       COUNT(DISTINCT token_address) AS tokens,
       ROUND(100.0 * COUNT(DISTINCT token_address)
             / NULLIF(SUM(COUNT(DISTINCT token_address)) OVER (), 0), 1) AS pct
FROM paper_trades
WHERE entry_model = 'IMMEDIATE'
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
