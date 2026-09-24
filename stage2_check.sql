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
SELECT COALESCE(discovery_source, '(pre-instrumentation)') AS population,
       cohort,
       COUNT(DISTINCT token_address) AS tokens,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY tradeable_depth_usd)::numeric, 0) AS median_depth,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY txns_h1)::numeric, 0) AS median_txns_h1
FROM paper_trades
WHERE entry_model = 'IMMEDIATE'
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
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * 1.5
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
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * 1.5
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
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * 1.5
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
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * 1.5
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
-- Section 5 reports a REJECTED mean of 29,438 percent. The first version of
-- this section recomputed each extreme return from its own two recorded
-- prices and reported "consistent" -- which proved only that the ARITHMETIC
-- was right. It used the same two numbers that produced the return, so it
-- could never have found a wrong PRICE. That is the failure mode here: a
-- token recorded at 0.0000117 and then at 1.53 is a 130,000x move, and the
-- likelier explanation is the wrong side or the wrong pool than a token that
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
    SELECT t.cohort, t.discovery_source,
           LEFT(t.token_address, 8) AS token,
           h.horizon_minutes AS mins,
           t.price_at_evaluation AS basis,
           h.price AS mark,
           h.return_percent AS ret,
           t.tradeable_depth_usd AS depth,
           t.volume_h1_usd AS vol_h1,
           h.price / NULLIF(t.price_at_evaluation, 0) AS ratio,
           f.distinct_marks, f.n_marks
    FROM paper_horizon_returns h
    JOIN paper_trades t ON t.id = h.paper_trade_id
    -- Per-token mark spread, as its own aggregate. Postgres has no
    -- COUNT(DISTINCT ...) OVER (...), so this cannot be a window function.
    JOIN (SELECT t2.token_address,
                 COUNT(DISTINCT h2.price) AS distinct_marks,
                 COUNT(*)                 AS n_marks
          FROM paper_horizon_returns h2
          JOIN paper_trades t2 ON t2.id = h2.paper_trade_id
          GROUP BY t2.token_address) f ON f.token_address = t.token_address
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
       ROUND(buying_needed_usd, 0)                  AS buying_needed,
       -- How many times the entire hour's observed volume would have had to
       -- be spent, on this one token, to produce the recorded price.
       ROUND(buying_needed_usd / NULLIF(vol_h1, 0), 0) AS x_of_hourly_volume,
       CASE WHEN distinct_marks = 1 AND n_marks > 1
                 THEN 'FROZEN -- one price repeated across every horizon'
            WHEN buying_needed_usd > 10 * COALESCE(vol_h1, 0)
                 THEN 'IMPOSSIBLE -- needs far more buying than the token saw'
            ELSE 'plausible -- no test refutes it' END AS verdict
FROM y
ORDER BY ret DESC
LIMIT 15;

\echo ''
\echo '--- 10e. WHAT THE MEANS BECOME WITHOUT THE REFUTED ROWS ---'
-- TWO FIXES over the first version, both of which made it disagree with 10d.
--
-- 1. It applied only the PHYSICS test and ignored the FROZEN test that 10d
--    applies. So 10d could report a row as frozen while 10e silently kept it
--    in the mean -- which is exactly what happened at 60 minutes, where 10d
--    flagged JE3MdNMM and 10e refuted nothing at all. Two sections of the
--    same report contradicting each other is the defect this whole audit has
--    been about; it does not get an exemption for being mine.
--
-- 2. It used a single arbitrary threshold (10x hourly volume). A conclusion
--    that moves with a number nobody can justify is not a conclusion, so the
--    sensitivity is now shown across three of them. The 1x column is the
--    honest bar: needing MORE than the token's entire hourly volume, counting
--    sells as if they were buys, concentrated into a window a third as long,
--    is already impossible. 10x and 100x are shown so the reader can see the
--    answer does not depend on where the line is drawn.
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
    JOIN (SELECT t2.token_address,
                 COUNT(DISTINCT h2.price) AS distinct_marks,
                 COUNT(*)                 AS n_marks
          FROM paper_horizon_returns h2
          JOIN paper_trades t2 ON t2.id = h2.paper_trade_id
          GROUP BY t2.token_address) f ON f.token_address = t.token_address
    WHERE h.age_minutes_at_mark <= h.horizon_minutes * 1.5
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
\echo '=== 10f. IS IT THE GATES OR THE STOP? -- path vs endpoint ==='
-- The contradiction this section exists to resolve:
--
--   Section 5   approved tokens are UP 68 percent of the time at 120 minutes,
--               median +2.22
--   Section 11  approved IMMEDIATE trades average -5.41
--
-- Both can be true. The horizon measurement samples the ENDPOINT; the barrier
-- trade experiences the PATH. A token that dips 9 percent and finishes +2 is
-- a win to section 5 and a stopped-out loss to section 11. If that is what is
-- happening, the gates are doing their job and the 7.53 percent stop is
-- giving the money back -- a completely different problem with a completely
-- different fix, and until now the evidence for it looked identical to "the
-- gates do not work".
--
-- MAE is maximum adverse excursion: the worst drawdown from entry before the
-- horizon. Read the last column. If the median MAE is deeper than the stop
-- distance, the stop was never survivable on this asset class and the trade
-- outcomes say nothing about the gates at all.
--
-- NULL for every row recorded before min_price_seen existed. Their paths were
-- never observed and must not be inferred from their endpoints.
WITH p AS (
    SELECT t.cohort, t.entry_model, t.discovery_source,
           t.price_at_evaluation AS basis,
           t.min_price_seen AS lo, t.max_price_seen AS hi,
           t.status, t.exit_reason, t.net_pnl_percent,
           100.0 * (t.min_price_seen - t.price_at_evaluation)
                 / NULLIF(t.price_at_evaluation, 0) AS mae_pct,
           100.0 * (t.max_price_seen - t.price_at_evaluation)
                 / NULLIF(t.price_at_evaluation, 0) AS mfe_pct
    FROM paper_trades t
    WHERE t.min_price_seen IS NOT NULL
      AND t.price_at_evaluation > 0
)
SELECT cohort, entry_model,
       COUNT(*)                                                   AS trades,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mae_pct)::numeric, 2)
                                                                  AS median_mae,
       ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY mae_pct)::numeric, 2)
                                                                  AS p25_mae,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mfe_pct)::numeric, 2)
                                                                  AS median_mfe,
       -- How many would have been stopped out by the CURRENT distance, purely
       -- from the path, whatever they finished at.
       COUNT(*) FILTER (WHERE mae_pct <= -7.53)                    AS would_stop,
       ROUND(100.0 * COUNT(*) FILTER (WHERE mae_pct <= -7.53)
             / NULLIF(COUNT(*), 0), 0)                             AS would_stop_pct,
       -- The damning cell: stopped out, and the token was ABOVE entry at its
       -- best. The stop fired on noise the position then recovered from.
       COUNT(*) FILTER (WHERE mae_pct <= -7.53 AND mfe_pct > 0)     AS stopped_but_rose
FROM p
GROUP BY cohort, entry_model
ORDER BY cohort, entry_model;

\echo ''
\echo '--- 10g. WHAT STOP DISTANCE WOULD THE PATHS HAVE SURVIVED? ---'
-- Not a recommendation -- a description of what the observed paths did. A
-- wider stop keeps more trades alive AND makes each loss bigger; this shows
-- only the first half, so it cannot on its own justify a change. It says
-- which distances are even in the running.
WITH p AS (
    SELECT t.cohort,
           100.0 * (t.min_price_seen - t.price_at_evaluation)
                 / NULLIF(t.price_at_evaluation, 0) AS mae_pct
    FROM paper_trades t
    WHERE t.min_price_seen IS NOT NULL AND t.price_at_evaluation > 0
      AND t.entry_model = 'IMMEDIATE'
)
SELECT cohort, COUNT(*) AS trades,
       ROUND(100.0 * COUNT(*) FILTER (WHERE mae_pct > -5)  / NULLIF(COUNT(*),0), 0) AS survive_5pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE mae_pct > -7.53)/ NULLIF(COUNT(*),0), 0) AS survive_current,
       ROUND(100.0 * COUNT(*) FILTER (WHERE mae_pct > -12) / NULLIF(COUNT(*),0), 0) AS survive_12pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE mae_pct > -20) / NULLIF(COUNT(*),0), 0) AS survive_20pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE mae_pct > -35) / NULLIF(COUNT(*),0), 0) AS survive_35pct
FROM p GROUP BY cohort ORDER BY cohort;

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
