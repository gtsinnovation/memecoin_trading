"""Retention prunes exhaust, never the experiment.

The defect this guards is not a crash. It is a retention policy that quietly
ages out the early part of a collection run: the report still prints a number,
just a number computed over a window nobody chose.
"""
import asyncio
from tests.harness import Suite

import retention


class FakeConn:
    """Counts DELETEs and reports rows affected, the way asyncpg does."""

    def __init__(self, counts=(), raises=False):
        self.counts = list(counts)
        self.sql = []
        self.raises = raises

    async def execute(self, sql):
        self.sql.append(sql)
        if self.raises:
            raise RuntimeError("relation does not exist")
        return f"DELETE {self.counts.pop(0) if self.counts else 0}"


def run() -> Suite:
    s = Suite("retention")

    tables = {t for (t, _, _, _) in retention._POLICIES}
    s.check_true("paper_trades is never on a retention timer",
                 "paper_trades" not in tables)
    s.check_true("paper_horizon_returns is never on a retention timer",
                 "paper_horizon_returns" not in tables)

    # An alert nobody has read yet is still owed to somebody. Pruning on age
    # alone is how a CRITICAL worker-death notice disappears unseen.
    alerts = [p for p in retention._POLICIES if p[0] == "system_alerts"][0]
    s.check("undispatched alerts survive their age", alerts[3], "is_dispatched = TRUE")

    # Batching is not a micro-optimisation: the tick loop shares this
    # database, and an unbounded DELETE over a day of alerts holds a lock
    # long enough to stall trading in order to tidy up after trading.
    conn = FakeConn(counts=[retention.RETENTION_BATCH, 3])
    out = asyncio.run(retention.prune_once(conn))
    s.check("a full batch is followed by another pass",
            out.get("system_alerts"), retention.RETENTION_BATCH + 3)
    s.check_true("every DELETE is bounded by a LIMIT",
                 conn.sql and all("LIMIT" in q for q in conn.sql))

    # Housekeeping must never take the loop down.
    out = asyncio.run(retention.prune_once(FakeConn(raises=True)))
    s.check_true("a failing sweep returns rather than raising",
                 all(v == 0 for v in out.values()))

    return s
