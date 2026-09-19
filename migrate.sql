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




