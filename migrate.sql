-- migrate.sql
--
-- In-place, non-destructive upgrade for an EXISTING database that was
-- created before the capital-limit / run-duration / wallet-labels /
-- balance-visibility / watchdog / kill-switch / auth feature set was added.
--
-- Use this INSTEAD OF "docker compose down -v" when you want to keep the
-- trading_sessions / active_positions / system_alerts rows you already have.
-- Every statement here is safe to run more than once (IF NOT EXISTS /
-- ON CONFLICT DO NOTHING throughout), so re-running it after a partial
-- failure is fine.
--
-- How to run it against the "db" service defined in docker-compose.yml,
-- without shutting anything down:
--
--   docker compose exec -T db psql -U postgres -d memecoin_trading < migrate.sql
--
-- (If your containers use different names/credentials, adjust -U/-d or the
-- service name accordingly.)

-- Extends the original active_positions table with the take-profit /
-- stop-loss / last-simulated-price columns used by evaluate_open_positions().
ALTER TABLE active_positions ADD COLUMN IF NOT EXISTS target_exit_price NUMERIC;
ALTER TABLE active_positions ADD COLUMN IF NOT EXISTS invalidation_level_price NUMERIC;
ALTER TABLE active_positions ADD COLUMN IF NOT EXISTS last_simulated_price NUMERIC;

-- Realized P&L ledger.
CREATE TABLE IF NOT EXISTS closed_positions (
    id SERIAL PRIMARY KEY,
    token_symbol VARCHAR(50) NOT NULL,
    token_address VARCHAR(128) NOT NULL,
    allocated_usd NUMERIC NOT NULL,
    entry_trigger NUMERIC NOT NULL,
    exit_price NUMERIC NOT NULL,
    exit_reason VARCHAR(20) NOT NULL, -- 'TARGET_HIT' or 'STOPPED_OUT'
    realized_pnl_usd NUMERIC NOT NULL,
    realized_pnl_percent NUMERIC NOT NULL,
    opened_at TIMESTAMP WITH TIME ZONE NOT NULL,
    closed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Single-row operator configuration (capital cap, run duration, wallet
-- labels, balance visibility, watchdog behavior, kill-switch thresholds).
CREATE TABLE IF NOT EXISTS app_settings (
    id INTEGER PRIMARY KEY DEFAULT 1,
    max_total_capital_usd NUMERIC,
    run_duration_minutes INTEGER,
    capital_wallet_label VARCHAR(256),
    pnl_wallet_label VARCHAR(256),
    show_realtime_balances BOOLEAN NOT NULL DEFAULT TRUE,
    agent_unresponsive_action VARCHAR(20) NOT NULL DEFAULT 'RESTART_ALL',
    agent_timeout_seconds NUMERIC NOT NULL DEFAULT 15,
    kill_switch_max_drawdown_pct NUMERIC,
    kill_switch_max_loss_usd NUMERIC,
    kill_switch_max_consecutive_losses INTEGER,
    run_status VARCHAR(30) NOT NULL DEFAULT 'RUNNING',
    run_status_reason TEXT,
    run_started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT app_settings_singleton CHECK (id = 1)
);
INSERT INTO app_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- Google sign-in allowlist (feature 8).
CREATE TABLE IF NOT EXISTS authorized_users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(256) NOT NULL UNIQUE,
    added_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_closed_positions_closed_at ON closed_positions(closed_at);

-- Stage 3 (real signing via signer_service -- see STAGE3_SETUP.md).
-- Accountability record for every real-execution attempt, approved or
-- refused, independent of system_alerts. Stays empty until you actually
-- turn ENABLE_STAGE3_EXECUTION on.
CREATE TABLE IF NOT EXISTS execution_audit_log (
    id SERIAL PRIMARY KEY,
    token_address VARCHAR(128) NOT NULL,
    token_symbol VARCHAR(50),
    requested_usd NUMERIC NOT NULL,
    outcome VARCHAR(20) NOT NULL,
    reason TEXT NOT NULL,
    tx_signature VARCHAR(128),
    network VARCHAR(20) NOT NULL DEFAULT 'devnet',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_execution_audit_created_at ON execution_audit_log(created_at);

-- Breadth gate (E_BREADTH): holder-count history, so the gate can compute
-- how fast a token is gaining holders. A single point-in-time count says
-- nothing about momentum; two points an hour apart do.
CREATE TABLE IF NOT EXISTS token_holder_samples (
    id SERIAL PRIMARY KEY,
    token_address VARCHAR(128) NOT NULL,
    total_holders INTEGER NOT NULL,
    sampled_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_holder_samples_token_time
    ON token_holder_samples(token_address, sampled_at DESC);

-- Stage 2 paper trading. One row per (evaluated token x entry model), so
-- every token the pipeline looks at is tracked whether the gates approved
-- it or not. Without the rejected cohort there is no control group, and
-- "approved trades returned X%" is a number with nothing to compare to.
CREATE TABLE IF NOT EXISTS paper_trades (
    id SERIAL PRIMARY KEY,
    token_address VARCHAR(128) NOT NULL,
    token_symbol VARCHAR(50),
    cohort VARCHAR(20) NOT NULL,            -- 'APPROVED' or 'REJECTED'
    rejected_by VARCHAR(40),                -- which gate short-circuited; NULL when approved
    entry_model VARCHAR(20) NOT NULL,       -- 'IMMEDIATE' or 'LIMIT'
    status VARCHAR(20) NOT NULL,            -- 'PENDING_FILL', 'OPEN', 'CLOSED', 'EXPIRED'
    price_at_evaluation NUMERIC NOT NULL,
    entry_trigger_price NUMERIC NOT NULL,
    target_exit_price NUMERIC NOT NULL,
    invalidation_level_price NUMERIC NOT NULL,
    assumed_slippage_percent NUMERIC,       -- measured at evaluation, charged on both sides
    fill_price NUMERIC,
    filled_at TIMESTAMP WITH TIME ZONE,
    last_price NUMERIC,
    last_marked_at TIMESTAMP WITH TIME ZONE,
    exit_price NUMERIC,
    exit_reason VARCHAR(20),                -- 'TARGET_HIT', 'STOPPED_OUT', 'TIMEOUT'
    closed_at TIMESTAMP WITH TIME ZONE,
    gross_pnl_percent NUMERIC,
    cost_percent NUMERIC,
    net_pnl_percent NUMERIC,                -- gross minus costs; THIS is the result
    evaluated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_trades_cohort ON paper_trades(cohort, entry_model);
CREATE INDEX IF NOT EXISTS idx_paper_trades_token_open
    ON paper_trades(token_address) WHERE status IN ('PENDING_FILL', 'OPEN');

-- Backstop for the dedup guard in paper_trading.record_candidate(): a token
-- may hold at most ONE live trade per entry model. Without this, two ticks
-- racing past the application-level check would both insert, and the same
-- price path would be counted twice as if it were two independent trades.
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_trades_live_per_model
    ON paper_trades(token_address, entry_model)
    WHERE status IN ('PENDING_FILL', 'OPEN');

-- Stage 2, second measurement: FIXED-HORIZON RETURNS.
--
-- The barrier exits in paper_trades answer "did +7% arrive before -14%",
-- which is a coin flip whose driftless base rate is 14/(7+14) = 66.7% and
-- whose gross expected value is therefore exactly zero. Detecting a real
-- edge through that channel needs hundreds of independent tokens, because
-- a binary outcome discards the magnitude of every move.
--
-- This table records what the token ACTUALLY did at fixed elapsed times
-- after evaluation, regardless of whether the barrier trade had already
-- closed. A distribution of returns carries far more information per
-- observation than a win/lose bit, and -- crucially -- it does not depend
-- on the 7%/14% levels being correct. Those levels are unvalidated
-- guesses; right now they define the result rather than being tested by it.
--
-- Rows hang off the IMMEDIATE trade only. A horizon return is a property of
-- the token's price path after we looked at it, not of the entry model, so
-- recording it against both models would double-count one observation.
CREATE TABLE IF NOT EXISTS paper_horizon_returns (
    id SERIAL PRIMARY KEY,
    paper_trade_id INTEGER NOT NULL REFERENCES paper_trades(id) ON DELETE CASCADE,
    horizon_minutes INTEGER NOT NULL,
    price NUMERIC NOT NULL,
    return_percent NUMERIC NOT NULL,      -- gross, vs price_at_evaluation
    age_minutes_at_mark NUMERIC NOT NULL, -- true elapsed time; a mark taken
                                          -- 50 min late is not a 30-min return
    marked_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (paper_trade_id, horizon_minutes)
);

CREATE INDEX IF NOT EXISTS idx_horizon_returns_trade
    ON paper_horizon_returns(paper_trade_id);
CREATE INDEX IF NOT EXISTS idx_horizon_returns_horizon
    ON paper_horizon_returns(horizon_minutes);

-- Standalone mirror of the UNIQUE constraint declared inside the CREATE TABLE
-- above. That constraint is applied ONLY when the table is created, so a
-- database where paper_horizon_returns already exists in an earlier shape
-- would migrate "successfully" without it. mark_horizons() ends its insert
-- with ON CONFLICT (paper_trade_id, horizon_minutes) DO NOTHING, which then
-- raises "no unique or exclusion constraint matching the ON CONFLICT
-- specification" -- caught upstream as a warning, so horizon marks silently
-- never record while mark_to_market keeps working and nothing looks wrong.
CREATE UNIQUE INDEX IF NOT EXISTS uq_horizon_returns_trade_horizon
    ON paper_horizon_returns(paper_trade_id, horizon_minutes);

-- Indexes that exist in schema.sql but were never added here, so a migrated
-- database and a fresh-volume one had different index sets.
CREATE INDEX IF NOT EXISTS idx_sessions_status ON trading_sessions(session_status);
CREATE INDEX IF NOT EXISTS idx_alerts_dispatch ON system_alerts(is_dispatched) WHERE is_dispatched = FALSE;

-- Liveness columns: was this token ACTUALLY TRADING when we evaluated it?
--
-- Prompted by a CoinMarketCap DexScan view of $USEFUL -- a token this
-- pipeline had evaluated -- showing 0 unique traders and single-digit-dollar
-- volume per candle, with unevenly spaced timestamps (candles forming only
-- when a trade prints). A token like that still has a priceUsd, and
-- DexScreener will happily serve it: the LAST print, however old. Two marks
-- 30 minutes apart can return the identical number, and a single late trade
-- can move the "price" 7% on eight dollars of volume.
--
-- That matters twice over. A barrier hit on a dead token is an artifact, not
-- an opportunity -- there was no counterparty to fill against at that price.
-- And artifacts are pure noise, which does not create a false signal; it
-- DILUTES a real one. If most evaluated tokens are dead, a genuine edge in
-- the gates would be undetectable no matter how long the experiment ran.
--
-- These are recorded rather than filtered on. Filtering discovery upfront
-- would remove the ability to test the hypothesis at all; recording lets the
-- results be segmented by activity after the fact.
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS volume_h1_usd NUMERIC;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS txns_h1 INTEGER;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS tradeable_depth_usd NUMERIC;

-- Short-window features, recorded for MEASUREMENT ONLY.
--
-- m5 is the finest bucket DexScreener publishes; there is nothing below five
-- minutes, and a shorter window would be worse rather than better -- a token
-- doing 50 trades an hour has no trades at all in most 10-second windows.
--
-- Nothing gates on these. Whether buy/sell imbalance predicts the horizon
-- returns is an empirical question, and paper_trading.feature_correlations()
-- is what answers it. Imbalance is weaker than intuition suggests: every buy
-- has a seller, the DEX only labels trades by direction against the pool, and
-- imbalance in COUNT is not imbalance in SIZE.
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS volume_m5_usd NUMERIC;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS txns_m5_buys INTEGER;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS txns_m5_sells INTEGER;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS price_change_m5 NUMERIC;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS price_change_h1 NUMERIC;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS txns_h1_buys INTEGER;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS txns_h1_sells INTEGER;

-- One open position per token. The dashboard's realized P&L was built from
-- the same coin opened tick after tick -- five identical $STONK rows at
-- +$150.5x, $USEFUL held twice -- which inflated realized P&L, capital
-- deployed and win rate all at once. engine.save_active_position() now checks
-- before inserting; this index is the backstop for two ticks racing.
--
-- NOTE: this index cannot be created while duplicates already exist. If it
-- fails, clear the duplicates first (keeping the oldest row per token):
--   DELETE FROM active_positions a USING active_positions b
--    WHERE a.token_address = b.token_address AND a.id > b.id;
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_positions_one_per_token
    ON active_positions(token_address);





-- ---------------------------------------------------------------------------
-- Why a candidate was refused, in full, on the candidate's own row.
--
-- `rejected_by` records the GATE ('F_ATLAS'), which is not enough to act on.
-- F_ATLAS refuses for two unrelated reasons -- concentration above the
-- ceiling, and concentration that could not be measured at all -- and those
-- want opposite responses: the first is the gate working, the second is a
-- data-coverage problem. They were indistinguishable in paper_trades, so
-- separating them meant joining system_alerts.message by timestamp, which is
-- guesswork once two tokens are evaluated in the same second.
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS reject_reason TEXT;

-- Holder concentration, every way it was measured, recorded for MEASUREMENT
-- ONLY -- no gate reads these.
--
-- The gate compares ONE number against a 30% ceiling. Which number depended
-- on the provider, and the three providers computed three incompatible
-- quantities (see holder_concentration.py). Choosing a single definition
-- requires knowing how far apart they are ON THE TOKENS THIS AGENT ACTUALLY
-- SEES, and that cannot be answered from RugCheck, because the tokens
-- RugCheck has no record of are exactly the ones in question -- they are 67%
-- of everything F_ATLAS rejects.
--
-- holder_pct_provider is what the gate saw. The chain columns are the
-- alternative definitions observed alongside it. A NULL is a failed
-- measurement, never a zero: 0% concentration does not exist.
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS holder_concentration_source VARCHAR(20);
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS holder_pct_provider NUMERIC;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS holder_pct_chain_raw NUMERIC;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS holder_pct_chain_wallet NUMERIC;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS holder_pct_chain_program NUMERIC;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS holder_pct_chain_burn NUMERIC;

-- Segmenting the rejected cohort by reason is the query this was added for,
-- and it runs over the whole table.
CREATE INDEX IF NOT EXISTS ix_paper_trades_rejected_by ON paper_trades(rejected_by);

-- ---------------------------------------------------------------------------
-- Discovery liquidity floors, editable from the settings page.
--
-- TWO floors, not one. Birdeye applied its floor server-side and then sorted
-- by listing time, so a page was "the newest tokens that already have $25k".
-- Jupiter's /tokens/v2/recent returns the newest mints unfiltered, and a
-- fifteen-minute-old token does not have $25,000 in its pool -- the same
-- floor applied to that list admitted nothing at all. A single floor low
-- enough to catch new mints would drag the breadth sources into the launchpad
-- band, where a quoted price is one trade old.
--
-- NULL means "use the configured default", so adding these columns changes no
-- behaviour until someone sets one.
--
-- Both are clamped on write (token_discovery.clamp_liquidity_floor). The
-- upper bound is not arbitrary: B_SENTINEL passes tokens with $20,000 of
-- tradeable depth, which is $40,000 of pool liquidity, so a floor at or above
-- that only ever offers tokens that already clear the gate -- approval
-- becomes a tautology and the REJECTED control arm disappears, with every log
-- line still looking healthy.
ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS discovery_min_liquidity_usd NUMERIC;
ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS discovery_new_listing_min_liquidity_usd NUMERIC;

-- ---------------------------------------------------------------------------
-- The holding pen: newly created pools, held until they age into the
-- evaluation window.
--
-- Solana creates ~30 pools a minute, so every newest-first endpoint spans one
-- or two minutes -- measured: GeckoTerminal new_pools 60 rows over 2 minutes,
-- Jupiter /tokens/v2/recent 30 rows over 1. Fifteen minutes ago is ~450
-- tokens back. Birdeye could reach the window only because it filtered by
-- liquidity server-side BEFORE sorting by listing time; nothing free does
-- that, so the join between "new" and "liquid" has to be held in memory.
--
-- pool_created_at is the CHAIN's timestamp and is what ageing uses.
-- first_seen_at is diagnostic only. If capture stalls, rows captured late
-- must still know their true age -- otherwise a ten-minute outage releases
-- the whole backlog at once, all of it looking fifteen minutes old, and
-- nothing in the data would show it.
CREATE TABLE IF NOT EXISTS discovery_pen (
    token_address VARCHAR(128) PRIMARY KEY,
    pool_created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source VARCHAR(40),
    -- Set when the token was examined at release, whether or not it passed.
    released_at TIMESTAMP WITH TIME ZONE,
    -- Liquidity measured at release. NULL means it was examined and did not
    -- clear the floor, or could not be read -- never that it had none.
    liquidity_at_release NUMERIC
);

-- The release query: unreleased rows inside an age band, oldest first.
CREATE INDEX IF NOT EXISTS ix_discovery_pen_due
    ON discovery_pen(pool_created_at) WHERE released_at IS NULL;

-- Did the token clear the floor when it was examined?
--
-- liquidity_at_release is now recorded for EVERY examined token, passing or
-- not, so this flag is what separates them. Recording it only for the winners
-- threw away exactly the data needed to choose a floor: the distribution that
-- matters includes the failures, and without them the only observable is
-- "N of M passed at whatever floor is configured" -- which cannot distinguish
-- a floor set too high from a market that is genuinely thin.
ALTER TABLE discovery_pen ADD COLUMN IF NOT EXISTS qualified BOOLEAN;

-- Which discovery source offered this token.
--
-- 'holding-pen' means a newly created pool that aged into the evaluation
-- window; 'breadth' means one of the trending/top-traded lists, which are
-- established tokens. These are DIFFERENT POPULATIONS and results cannot be
-- pooled across them. Without this column the mix is invisible: the pen can
-- go quiet for six hours and the cohort silently becomes a study of
-- established tokens while every count still looks healthy.
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS discovery_source VARCHAR(20);

-- Exit confirmation: did the mark that triggered this exit have a
-- counterparty? See the column comments in schema.sql. Existing rows keep
-- NULL, which reads as "unknown" -- correct, because for those rows it is.
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS exit_confirmed BOOLEAN;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS exit_txns_m5 INTEGER;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS exit_txns_h1 INTEGER;

-- Path extremes, for deciding whether the stop or the gates are losing the
-- money. See the column comments in schema.sql. Existing rows keep NULL:
-- their path was never observed and must not be inferred from their endpoints.
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS min_price_seen NUMERIC;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS max_price_seen NUMERIC;

-- Ordered price path, for replaying exit policies against observed sequences.
-- See the table comment in schema.sql. High-volume: retention.py prunes it.
CREATE TABLE IF NOT EXISTS paper_price_path (
    id BIGSERIAL PRIMARY KEY,
    token_address VARCHAR(64) NOT NULL,
    price NUMERIC NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_price_path_token_time
    ON paper_price_path(token_address, observed_at);

-- Index parity with schema.sql. These were added to schema.sql alongside
-- retention.py but never here, so the LIVE database -- which is only ever
-- upgraded by this file -- has been running every retention sweep as a
-- sequential scan over the very tables that grew large enough to need
-- pruning. tests/test_hardening.py now fails if the two files' index sets
-- differ, so this class of drift cannot recur silently.
CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON system_alerts(created_at);
CREATE INDEX IF NOT EXISTS idx_holder_samples_sampled_at ON token_holder_samples(sampled_at);
-- paper_price_path is pruned on observed_at ALONE, and the only index led with
-- token_address, which that predicate cannot use. At ~290k rows a day this
-- was a full scan of the largest table every 15 minutes.
CREATE INDEX IF NOT EXISTS idx_price_path_observed_at ON paper_price_path(observed_at);


-- Live ledger costs. The kill switch sums closed_positions.realized_pnl_usd,
-- which was GROSS of fees and slippage and booked stop-outs AT the stop even
-- when the market gapped far below. From here on realized_pnl_* is NET, with
-- the gross figure and the cost kept alongside. Old rows keep NULL cost and
-- are gross; nothing is back-filled, because their entry slippage was never
-- recorded and must not be invented.
ALTER TABLE active_positions ADD COLUMN IF NOT EXISTS entry_slippage_percent NUMERIC;
ALTER TABLE closed_positions ADD COLUMN IF NOT EXISTS gross_pnl_percent NUMERIC;
ALTER TABLE closed_positions ADD COLUMN IF NOT EXISTS cost_percent NUMERIC;

-- Stage 3 lifecycle and idempotency. See the column comments in schema.sql.
-- Existing rows become 'PAPER' (none of them was ever sent to a signer) and
-- each gets its own random client_order_id from the column default.
ALTER TABLE active_positions ADD COLUMN IF NOT EXISTS execution_status VARCHAR(24) NOT NULL DEFAULT 'PAPER';
ALTER TABLE active_positions ADD COLUMN IF NOT EXISTS client_order_id UUID NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE active_positions ADD COLUMN IF NOT EXISTS tx_signature VARCHAR(128);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_positions_client_order_id
    ON active_positions(client_order_id);

CREATE TABLE IF NOT EXISTS signer_orders (
    client_order_id VARCHAR(64) PRIMARY KEY,
    token_address VARCHAR(128) NOT NULL,
    requested_usd NUMERIC NOT NULL,
    status VARCHAR(20) NOT NULL,
    reason TEXT,
    tx_signature VARCHAR(128),
    network VARCHAR(20),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_signer_orders_created_at ON signer_orders(created_at);

-- Horizon dropout, persisted rather than only logged. See schema.sql.
CREATE TABLE IF NOT EXISTS paper_horizon_dropout (
    hour TIMESTAMP WITH TIME ZONE PRIMARY KEY,
    due INTEGER NOT NULL DEFAULT 0,
    marked INTEGER NOT NULL DEFAULT 0,
    dropped_no_price INTEGER NOT NULL DEFAULT 0,
    dropped_no_basis INTEGER NOT NULL DEFAULT 0
);
