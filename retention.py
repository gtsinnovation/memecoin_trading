"""Bounded storage for the operational tables.

Nothing in this system ever deleted a row. Over a 16-hour collection run that
is merely untidy; over the multi-day runs the experiment actually needs it is
a slow corruption of the thing being measured, because the tick loop shares a
connection pool with queries whose cost grows with table size. The measurement
degrades as a function of how long you measure -- which is the worst possible
failure mode for an experiment whose whole purpose is to run long enough to
reach significance.

WHAT IS NOT PRUNED, AND WHY
---------------------------
`paper_trades` and `paper_horizon_returns` ARE THE EXPERIMENT. They are never
pruned on a timer. A retention policy that quietly ages out the early part of
a run would delete exactly the observations that make a long run worth more
than a short one, and it would do it invisibly -- the report would still print
a number, just a number computed over a window nobody chose. If those tables
need trimming, that is a deliberate decision made once, by hand, with the
horizon written down.

Everything here is operational exhaust: alerts, audit rows, holder samples.
Losing the old ones costs nothing the analysis depends on.
"""
import os
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

RETENTION_ENABLED = os.environ.get("RETENTION_ENABLED", "true").strip().lower() in ("1", "true", "yes")
RETENTION_INTERVAL_S = float(os.environ.get("RETENTION_INTERVAL_S", "900"))
RETENTION_BATCH = int(os.environ.get("RETENTION_BATCH", "5000"))

# Per-table age limits, in hours. None means "never prune".
ALERT_RETENTION_HOURS = float(os.environ.get("ALERT_RETENTION_HOURS", "72"))
# ERROR and CRITICAL are rare and are the rows someone may need after the
# fact -- a worker death, a kill-switch trip. They are kept far longer than
# the routine DEBUG/INFO/WARN traffic that makes up almost all of the volume.
SEVERE_ALERT_RETENTION_HOURS = float(os.environ.get("SEVERE_ALERT_RETENTION_HOURS", "720"))
AUDIT_RETENTION_HOURS = float(os.environ.get("AUDIT_RETENTION_HOURS", "336"))   # 14d
HOLDER_SAMPLE_RETENTION_HOURS = float(os.environ.get("HOLDER_SAMPLE_RETENTION_HOURS", "168"))
# The one genuinely high-volume table. At a 30-second sample and ~100 live
# tokens this is ~290k rows a day. It is NOT the experiment -- it is the
# instrument for replaying exit policies -- but a replay needs paths older
# than the trades it is testing, so this is deliberately longer than the
# longest hold (PAPER_MAX_HOLD_MINUTES, 6h) by a wide margin.
PRICE_PATH_RETENTION_HOURS = float(os.environ.get("PRICE_PATH_RETENTION_HOURS", "336"))

# (table, timestamp column, retention hours, extra predicate)
#
# system_alerts is split by SEVERITY, not by is_dispatched.
#
# The first version pruned only rows with is_dispatched = TRUE, to stop an
# unread CRITICAL notice being aged out. The intent was sound and the effect
# was nothing at all: no code anywhere ever sets is_dispatched to TRUE -- there
# is no dispatcher -- so the predicate matched zero rows, every sweep deleted
# nothing, and the table grew at roughly 100k rows a day while its test passed.
# Severity is what the protection was actually about, and it is a column that
# is always populated.
_POLICIES = (
    ("system_alerts", "created_at", ALERT_RETENTION_HOURS,
     "log_level IN ('DEBUG', 'INFO', 'WARN')"),
    ("system_alerts", "created_at", SEVERE_ALERT_RETENTION_HOURS,
     "log_level NOT IN ('DEBUG', 'INFO', 'WARN')"),
    ("execution_audit_log", "created_at", AUDIT_RETENTION_HOURS, None),
    ("token_holder_samples", "sampled_at", HOLDER_SAMPLE_RETENTION_HOURS, None),
    ("paper_price_path", "observed_at", PRICE_PATH_RETENTION_HOURS, None),
)


async def prune_once(conn) -> dict:
    """Delete aged operational rows. Returns {table: rows_deleted}.

    Deletes in bounded batches rather than one statement per table. An
    unbounded DELETE on a table holding a day of alerts takes a long lock,
    and the tick loop is sharing this database -- the cleanup would stall
    trading in order to tidy up after trading.
    """
    deleted = {}
    for table, ts_col, hours, predicate in _POLICIES:
        if hours is None or hours <= 0:
            continue
        total = 0
        try:
            while True:
                where = f"{ts_col} < NOW() - INTERVAL '{float(hours)} hours'"
                if predicate:
                    where += f" AND {predicate}"
                status = await conn.execute(
                    f"DELETE FROM {table} WHERE ctid IN "
                    f"(SELECT ctid FROM {table} WHERE {where} LIMIT {RETENTION_BATCH});")
                n = int(str(status).rsplit(" ", 1)[-1] or 0)
                total += n
                if n < RETENTION_BATCH:
                    break
                await asyncio.sleep(0)  # yield between batches
        except Exception as exc:
            # Retention is housekeeping. It must never take the loop down.
            logger.error(f"retention: pruning {table} failed: {type(exc).__name__}: {exc}")
            continue
        if total:
            logger.info(f"retention: pruned {total} rows from {table}")
        deleted[table] = deleted.get(table, 0) + total
    return deleted


async def retention_worker(dsn: str, connect=None) -> None:
    """Opens its own connection per sweep, matching discovery_pen_worker.

    `connect` is injectable so the tests can drive a sweep without a database.
    """
    if not RETENTION_ENABLED:
        logger.info("retention: disabled by RETENTION_ENABLED")
        return
    if connect is None:
        import asyncpg
        connect = lambda: asyncpg.connect(dsn=dsn)
    logger.info(
        f"retention: every {RETENTION_INTERVAL_S}s | alerts {ALERT_RETENTION_HOURS}h "
        f"(routine) / {SEVERE_ALERT_RETENTION_HOURS}h (ERROR+) | audit {AUDIT_RETENTION_HOURS}h | "
        f"holder samples {HOLDER_SAMPLE_RETENTION_HOURS}h | "
        f"price path {PRICE_PATH_RETENTION_HOURS}h | "
        f"paper_trades + paper_horizon_returns NEVER")
    await asyncio.sleep(20.0)  # let startup settle before the first sweep
    while True:
        conn = None
        try:
            conn = await connect()
            await prune_once(conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"retention: sweep failed: {type(exc).__name__}: {exc}")
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
        await asyncio.sleep(RETENTION_INTERVAL_S)
