-- schema.sql
-- Production Relational Architecture for Multi-Agent Telemetry and Ledger Accounting

CREATE TABLE IF NOT EXISTS trading_sessions (
    id SERIAL PRIMARY KEY,
    session_start_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    token_symbol VARCHAR(50) NOT NULL,
    token_address VARCHAR(128) NOT NULL,
    current_price NUMERIC NOT NULL,
    pool_liquidity_usd NUMERIC NOT NULL,
    social_volume_score NUMERIC NOT NULL,
    onchain_flow_velocity NUMERIC NOT NULL,
    top_10_holder_percentage NUMERIC NOT NULL,
    max_position_size_usd NUMERIC,
    target_pullback_price NUMERIC,
    invalidation_level_price NUMERIC,
    session_status VARCHAR(50) NOT NULL, -- 'APPROVED', 'REJECTED'
    final_briefing TEXT,
    logged_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS active_positions (
    id SERIAL PRIMARY KEY,
    token_symbol VARCHAR(50) NOT NULL,
    token_address VARCHAR(128) NOT NULL,
    allocated_usd NUMERIC NOT NULL,
    entry_trigger NUMERIC NOT NULL,
    target_exit_price NUMERIC,          -- take-profit level; position closes here on the upside
    invalidation_level_price NUMERIC,   -- stop-loss level; position closes here on the downside
    last_simulated_price NUMERIC,       -- most recent price the exit-monitor walked this position to
    captured_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    -- Slippage measured at entry, charged on both sides at close. NULL means
    -- UNMEASURED and is costed at paper_trading's stand-in, never at zero.
    entry_slippage_percent NUMERIC,
    -- Stage 3 lifecycle. 'PAPER' when execution is off. With it on, a row is
    -- written 'PENDING_EXECUTION' (a capital reservation, never marked or
    -- closed), then becomes 'EXECUTED', is deleted on a definitive refusal,
    -- or becomes 'EXECUTION_UNKNOWN' when the outcome could not be learned.
    execution_status VARCHAR(24) NOT NULL DEFAULT 'PAPER',
    -- Idempotency key the signer dedupes on (signer_orders). A retried or
    -- replayed request with the same key can never be signed twice.
    client_order_id UUID NOT NULL DEFAULT gen_random_uuid(),
    tx_signature VARCHAR(128)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_positions_client_order_id
    ON active_positions(client_order_id);

-- Realized outcomes: a position is moved here (and removed from active_positions)
-- once it hits its take-profit or stop-loss level, so this table is the source
-- of truth for actual P&L, independent of how much capital is currently deployed.
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
    closed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    -- realized_pnl_* are NET of these costs from this column's introduction.
    -- Rows with cost_percent NULL predate it and are GROSS.
    gross_pnl_percent NUMERIC,
    cost_percent NUMERIC
);

CREATE TABLE IF NOT EXISTS system_alerts (
    id SERIAL PRIMARY KEY,
    log_level VARCHAR(20) NOT NULL, -- 'DEBUG', 'INFO', 'WARN', 'ERROR', 'CRITICAL'
    agent_name VARCHAR(50) NOT NULL,
    message TEXT NOT NULL,
    is_dispatched BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Single-row operator configuration: capital limits, run duration, wallet
-- labels (display/bookkeeping only -- these two fields are never read by
-- the signer service; real signing config lives entirely in
-- signer_service's own environment, see STAGE3_SETUP.md), balance
-- visibility, agent watchdog behavior, and kill-switch thresholds. id is
-- pinned to 1 so there is always exactly one settings row.
CREATE TABLE IF NOT EXISTS app_settings (
    id INTEGER PRIMARY KEY DEFAULT 1,
    max_total_capital_usd NUMERIC,                     -- NULL = no cap on total deployed capital
    run_duration_minutes INTEGER,                      -- NULL = run indefinitely
    capital_wallet_label VARCHAR(256),                  -- metadata only, not a connected wallet
    pnl_wallet_label VARCHAR(256),                      -- metadata only, not a connected wallet
    show_realtime_balances BOOLEAN NOT NULL DEFAULT TRUE,
    agent_unresponsive_action VARCHAR(20) NOT NULL DEFAULT 'RESTART_ALL', -- 'RESTART_ALL' or 'SHUTDOWN'
    agent_timeout_seconds NUMERIC NOT NULL DEFAULT 15,
    kill_switch_max_drawdown_pct NUMERIC,               -- NULL = disabled
    kill_switch_max_loss_usd NUMERIC,                   -- NULL = disabled
    kill_switch_max_consecutive_losses INTEGER,         -- NULL = disabled
    run_status VARCHAR(30) NOT NULL DEFAULT 'RUNNING',  -- RUNNING, PAUSED_MANUAL, PAUSED_KILL_SWITCH, PAUSED_DURATION_ELAPSED, SHUTDOWN_WATCHDOG
    run_status_reason TEXT,
    run_started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    -- Discovery liquidity floors. NULL = use the configured default. Two of
    -- them because newly-listed tokens and established ones cannot share a
    -- floor: a 15-minute-old mint has no $25k pool, and a floor low enough to
    -- catch one drags the breadth sources into the launchpad band. Clamped on
    -- write -- see token_discovery.clamp_liquidity_floor and migrate.sql.
    discovery_min_liquidity_usd NUMERIC,
    discovery_new_listing_min_liquidity_usd NUMERIC,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT app_settings_singleton CHECK (id = 1)
);
INSERT INTO app_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- Google accounts allowed to sign in. Seeded/kept in sync from the
-- AUTHORIZED_GOOGLE_EMAIL environment variable at startup; a single row for
-- the single-operator setup this project is configured for today.
CREATE TABLE IF NOT EXISTS authorized_users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(256) NOT NULL UNIQUE,
    added_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Every real-execution attempt the signer service makes, approved or
-- refused, independent of the pipeline's own system_alerts (see
-- signer_service/policy_guard.py and README.md's Stage 3 section). This
-- table is the accountability record for anything that actually touched
-- (or tried to touch) a real signer -- it exists whether or not
-- ENABLE_STAGE3_EXECUTION is ever turned on, and stays empty until it is.
CREATE TABLE IF NOT EXISTS execution_audit_log (
    id SERIAL PRIMARY KEY,
    token_address VARCHAR(128) NOT NULL,
    token_symbol VARCHAR(50),
    requested_usd NUMERIC NOT NULL,
    outcome VARCHAR(20) NOT NULL,        -- 'REFUSED_POLICY', 'REFUSED_TURNKEY', 'REFUSED_DUPLICATE', 'SIGNED_BROADCAST', 'ERROR'
    reason TEXT NOT NULL,                -- policy_guard's reason, Turnkey's failure detail, or the error
    tx_signature VARCHAR(128),           -- Solana transaction signature, only set on SIGNED_BROADCAST
    network VARCHAR(20) NOT NULL DEFAULT 'devnet', -- 'devnet' or 'mainnet' -- never trust this alone; cross-check SOLANA_RPC_URL
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_execution_audit_created_at ON execution_audit_log(created_at);

-- The signer's OWN order ledger (signer_service/main.py). One row per
-- client_order_id, inserted BEFORE anything is signed; the primary key is
-- what makes a replayed or retried request unable to sign twice. The signer's
-- daily order and notional caps are counted from here, not from any table
-- the web app writes. Grant the web role SELECT at most -- see STAGE3_SETUP.md.
CREATE TABLE IF NOT EXISTS signer_orders (
    client_order_id VARCHAR(64) PRIMARY KEY,
    token_address VARCHAR(128) NOT NULL,
    requested_usd NUMERIC NOT NULL,
    status VARCHAR(20) NOT NULL,      -- RESERVED, SIGNED_BROADCAST, REFUSED, FAILED, UNKNOWN
    reason TEXT,
    tx_signature VARCHAR(128),
    network VARCHAR(20),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_signer_orders_created_at ON signer_orders(created_at);

-- Holder-count samples over time, so E_BREADTH can compute how fast a
-- token is gaining holders. A single point-in-time holder count says
-- nothing about momentum; two points an hour apart do. Rows are written
-- by the pipeline each tick and are cheap to prune.
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
    pair_address VARCHAR(128),           -- immutable pool selected at evaluation; NULL means legacy/unverifiable
    price_validation_version VARCHAR(40), -- NULL marks legacy rows recorded before price-method versioning
    token_symbol VARCHAR(50),
    cohort VARCHAR(20) NOT NULL,            -- 'APPROVED' or 'REJECTED'
    rejected_by VARCHAR(40),                -- which gate short-circuited; NULL when approved
    entry_model VARCHAR(20) NOT NULL,       -- 'IMMEDIATE' or 'LIMIT'
    status VARCHAR(20) NOT NULL,            -- PENDING_FILL, OPEN, CLOSED, EXPIRED, ABANDONED, INVALID_DATA
    price_at_evaluation NUMERIC NOT NULL,
    entry_trigger_price NUMERIC NOT NULL,
    target_exit_price NUMERIC NOT NULL,
    invalidation_level_price NUMERIC NOT NULL,
    assumed_slippage_percent NUMERIC,       -- measured at evaluation, charged on both sides
    slippage_probe_usd NUMERIC,             -- exact notional sent to Jupiter for this impact quote
    fill_price NUMERIC,
    filled_at TIMESTAMP WITH TIME ZONE,
    last_price NUMERIC,
    last_marked_at TIMESTAMP WITH TIME ZONE,
    -- The PATH, not just the endpoints.
    --
    -- Section 5 says approved tokens are UP 68 percent of the time at 120
    -- minutes with a median of +2.22 -- and closed trades average -5.41. Both
    -- can be true at once if the token dips through the 7.53 percent stop on
    -- its way to finishing higher: the horizon measurement samples the
    -- ENDPOINT, the barrier trade experiences the PATH, and nothing recorded
    -- the difference. Without these two columns "the gates are wrong" and
    -- "the stop is too tight" produce identical evidence.
    --
    -- Maintained on every mark, from the same prices the rest of the
    -- measurement uses, so they cost no extra request.
    min_price_seen NUMERIC,
    max_price_seen NUMERIC,
    exit_price NUMERIC,
    exit_reason VARCHAR(20),                -- 'TARGET_HIT', 'STOPPED_OUT', 'TIMEOUT'
    -- Was there a COUNTERPARTY at the mark that produced this exit?
    --
    -- A take-profit is a limit sell: it needs somebody on the other side. A
    -- single stale or wicked print on a token that has not traded in five
    -- minutes crossed the target and was booked as a clean win, at the target
    -- price, with no trade behind it. Those hits flatter the APPROVED cohort
    -- specifically, because the gates select thin tokens where a lone print
    -- moves the quote furthest.
    --
    -- TRUE  = the mark showed transactions, so a fill was possible.
    -- FALSE = the mark showed ZERO transactions -- a quote, not a trade.
    -- NULL  = the provider reported no transaction data at all. Unknown, and
    --         deliberately NOT folded into FALSE: analysis filters on
    --         `exit_confirmed IS TRUE`, so unknown and refuted both drop out,
    --         but the two stay distinguishable in the raw data.
    exit_confirmed BOOLEAN,
    exit_txns_m5 INTEGER,                   -- the evidence, so the flag is re-derivable
    exit_txns_h1 INTEGER,
    closed_at TIMESTAMP WITH TIME ZONE,
    gross_pnl_percent NUMERIC,
    cost_percent NUMERIC,
    net_pnl_percent NUMERIC,                -- gross minus costs; THIS is the result
    evaluated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    -- Liveness at evaluation time: was this token actually trading, or is
    -- its "price" just the last print from an hour ago? A barrier hit on a
    -- token with no counterparty is an artifact, and artifacts dilute any
    -- real signal rather than creating a false one. See migrate.sql.
    volume_h1_usd NUMERIC,
    txns_h1 INTEGER,          -- total; the buy/sell split below is kept
    txns_h1_buys INTEGER,     -- separately so buy share is testable at both
    txns_h1_sells INTEGER,    -- the m5 and h1 scales
    tradeable_depth_usd NUMERIC,
    -- Short-window features, recorded for measurement only -- nothing gates
    -- on them. m5 is DexScreener's finest bucket. See migrate.sql.
    volume_m5_usd NUMERIC,
    txns_m5_buys INTEGER,
    txns_m5_sells INTEGER,
    price_change_m5 NUMERIC,
    price_change_h1 NUMERIC,
    -- Full refusal text. `rejected_by` above records only the gate, and
    -- F_ATLAS refuses both for concentration over the ceiling and for
    -- concentration it could not measure -- opposite problems. See
    -- migrate.sql.
    reject_reason TEXT,
    -- Holder concentration, every way it was measured. Measurement only --
    -- no gate reads these. holder_pct_provider is what the gate saw; the
    -- chain columns are the alternative definitions observed alongside it.
    -- NULL means the measurement failed; 0% concentration does not exist.
    -- See holder_concentration.py and migrate.sql.
    holder_concentration_source VARCHAR(20),
    holder_pct_provider NUMERIC,
    holder_pct_chain_raw NUMERIC,
    holder_pct_chain_wallet NUMERIC,
    holder_pct_chain_program NUMERIC,
    holder_pct_chain_burn NUMERIC,
    -- 'holding-pen' (a newly created pool) or 'breadth' (an established
    -- token from a trending list). Different populations; see migrate.sql.
    discovery_source VARCHAR(20)
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
    mark_liquidity_usd NUMERIC,           -- pool liquidity reported with this exact mark
    mark_volume_h1_usd NUMERIC,           -- rolling-hour volume at mark time
    mark_txns_m5 INTEGER,                 -- recent activity evidence at mark time
    mark_txns_h1 INTEGER,
    marked_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (paper_trade_id, horizon_minutes)
);

CREATE INDEX IF NOT EXISTS idx_horizon_returns_trade
    ON paper_horizon_returns(paper_trade_id);
CREATE INDEX IF NOT EXISTS idx_horizon_returns_horizon
    ON paper_horizon_returns(horizon_minutes);

-- Mirrors the table-level UNIQUE above as a named index, so a fresh-volume
-- database and a migrated one converge on identical objects (migrate.sql
-- cannot add a table constraint to a table it did not create).
CREATE UNIQUE INDEX IF NOT EXISTS uq_horizon_returns_trade_horizon
    ON paper_horizon_returns(paper_trade_id, horizon_minutes);

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



-- Establish database tracking indexes for optimal query speeds
CREATE INDEX IF NOT EXISTS idx_sessions_status ON trading_sessions(session_status);
CREATE INDEX IF NOT EXISTS idx_alerts_dispatch ON system_alerts(is_dispatched) WHERE is_dispatched = FALSE;
CREATE INDEX IF NOT EXISTS idx_closed_positions_closed_at ON closed_positions(closed_at);

-- The holding pen for newly created pools. See migrate.sql for why it exists:
-- Solana mints faster than any newest-first endpoint can span, so "new" and
-- "liquid" have to be joined in memory. Ageing uses the CHAIN's timestamp,
-- never our own clock.
CREATE TABLE IF NOT EXISTS discovery_pen (
    token_address VARCHAR(128) PRIMARY KEY,
    pool_created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source VARCHAR(40),
    released_at TIMESTAMP WITH TIME ZONE,
    -- Recorded for EVERY examined token, passing or not: the distribution
    -- that sets the floor includes the failures. See migrate.sql.
    liquidity_at_release NUMERIC,
    qualified BOOLEAN
);
CREATE INDEX IF NOT EXISTS ix_discovery_pen_due
    ON discovery_pen(pool_created_at) WHERE released_at IS NULL;

-- Retention support (see retention.py). The prune predicate is an age scan;
-- without these it degrades into a sequential scan over the very tables that
-- grew large enough to need pruning.
CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON system_alerts(created_at);
CREATE INDEX IF NOT EXISTS idx_holder_samples_sampled_at ON token_holder_samples(sampled_at);

-- ---------------------------------------------------------------------------
-- ORDERED PRICE PATH
--
-- min_price_seen / max_price_seen record HOW FAR a token moved each way but
-- carry no ordering, so they cannot tell "rose, then fell through the stop"
-- from "fell through the stop, then recovered" -- and only the second is the
-- stop firing on noise. That ambiguity is what stopped section 10f being
-- conclusive. Replaying an exit policy needs the sequence, not the extremes.
--
-- Keyed by TOKEN, not by trade: the IMMEDIATE and LIMIT arms of the same
-- token see an identical price series, and storing it twice would double the
-- write volume to record the same facts.
--
-- Sampled, not per-tick. The pipeline marks every ~3 seconds; at roughly a
-- hundred live tokens that is ~3M rows a day to answer a question that
-- 30-second resolution settles. PATH_SAMPLE_SECONDS bounds it, and retention
-- prunes it -- this table is the only one here that is genuinely high-volume.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS paper_price_path (
    id BIGSERIAL PRIMARY KEY,
    token_address VARCHAR(64) NOT NULL,
    pair_address VARCHAR(128),
    price NUMERIC NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- The replay reads one token's series in time order; the sampler asks whether
-- a recent row exists for a token. Both are this index.
CREATE INDEX IF NOT EXISTS idx_price_path_token_time
    ON paper_price_path(token_address, pair_address, observed_at);

-- Index parity with migrate.sql (see the note there). Segmenting the rejected
-- cohort by reason runs over the whole table.
CREATE INDEX IF NOT EXISTS ix_paper_trades_rejected_by ON paper_trades(rejected_by);
-- Retention prunes paper_price_path on observed_at alone.
CREATE INDEX IF NOT EXISTS idx_price_path_observed_at ON paper_price_path(observed_at);


-- Horizon dropout per hour (paper_trading.record_horizon_dropout). Counts are
-- row-ticks; read dropped/due as a rate. Experiment metadata: never pruned.
CREATE TABLE IF NOT EXISTS paper_horizon_dropout (
    hour TIMESTAMP WITH TIME ZONE PRIMARY KEY,
    due INTEGER NOT NULL DEFAULT 0,
    marked INTEGER NOT NULL DEFAULT 0,
    dropped_no_price INTEGER NOT NULL DEFAULT 0,
    dropped_no_basis INTEGER NOT NULL DEFAULT 0
);
