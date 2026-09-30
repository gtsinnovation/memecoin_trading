# tests/harness.py
"""Shared plumbing for the regression suite.

WHY THIS EXISTS
Fourteen defects were found and fixed in a single audit. Most were the kind
that produce a plausible-looking wrong number rather than a crash: a gate that
passes when it should refuse, a cohort statistic borrowed from the wrong
cohort, a Spearman coefficient that is not Spearman. Nothing about the running
system looks broken when any of them regresses.

That is what these tests are for. They are not here to prove the code works --
they are here so that a change made six months from now, in a file that seems
unrelated, cannot quietly undo one of them.

SAFETY: THESE TESTS NEVER TOUCH YOUR LIVE DATA
Every suite TRUNCATEs tables. Run against the live database, that would erase
the paper-trading experiment. So the harness creates a SEPARATE database, runs
there, and drops it afterwards -- and refuses outright to run against a
database whose name doesn't end in `_test`. The guard is deliberately
unconditional: there is no flag to override it, because the one time someone
would reach for that flag is the one time it matters.
"""
import os
import sys
from urllib.parse import urlparse, urlunparse

TEST_DB_SUFFIX = "_test"
DEFAULT_TEST_DB = "memecoin_trading_test"

def _with_dbname(dsn: str, dbname: str) -> str:
    # Split DSN into prefix and path manually to preserve credentials
    if "/" not in dsn:
        raise ValueError(f"Invalid DSN: {dsn}")

    # Find last slash (start of db name)
    prefix, _ = dsn.rsplit("/", 1)
    return f"{prefix}/{dbname}"

def test_dsn() -> str:
    """The DSN the suites connect to -- always a dedicated test database."""
    live = os.environ.get("DATABASE_URL", "")
    if not live:
        raise RuntimeError("DATABASE_URL is not set. Run this inside the container.")
    return _with_dbname(live, DEFAULT_TEST_DB)

def admin_dsn() -> str:
    """A DSN pointing at the `postgres` maintenance database, for CREATE/DROP."""
    return _with_dbname(
        os.environ.get(
            "DATABASE_URL",
            "postgresql://postgres:supersecretpassword123@db:5432/postgres"
        ),
        "postgres"
    )

def _dbname_of(dsn: str) -> str:
    """Return the database name from a PostgreSQL URL DSN."""
    from urllib.parse import urlparse
    return urlparse(dsn).path.lstrip("/")

def assert_is_test_db(dsn: str) -> None:
    """Hard stop if this is not a throwaway database.

    Unconditional on purpose. Every suite truncates; pointed at the live
    database that erases the experiment, and the experiment is the only thing
    in this project that cannot be rebuilt from source.
    """
    dbname = _dbname_of(dsn)
    if not dbname.endswith(TEST_DB_SUFFIX):
        raise RuntimeError(
            f"REFUSING TO RUN: target database is {dbname!r}, which does not end in "
            f"{TEST_DB_SUFFIX!r}. These suites TRUNCATE tables. Against the live "
            f"database that would erase the paper-trading experiment."
        )


def build_test_database(psycopg2, schema_path: str) -> str:
    """(Re)create the test database and apply schema.sql to it. Returns its DSN."""
    dsn = test_dsn()
    assert_is_test_db(dsn)
    dbname = _dbname_of(dsn)

    admin = psycopg2.connect(admin_dsn())
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{dbname}";')
            cur.execute(f'CREATE DATABASE "{dbname}";')
    finally:
        admin.close()

    with open(schema_path, encoding="utf-8") as fh:
        schema_sql = fh.read()
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
    finally:
        conn.close()
    return dsn


def drop_test_database(psycopg2) -> None:
    dsn = test_dsn()
    assert_is_test_db(dsn)
    dbname = _dbname_of(dsn)
    admin = psycopg2.connect(admin_dsn())
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{dbname}";')
    finally:
        admin.close()


class Suite:
    """Minimal assertion collector -- no pytest dependency in the images."""

    def __init__(self, name: str):
        self.name = name
        self.failures = []
        self.passes = 0

    def check(self, label, got, want):
        if got == want:
            self.passes += 1
            print(f"  PASS  {label}")
        else:
            self.failures.append(f"{label}: got {got!r}, want {want!r}")
            print(f"  FAIL  {label}: got {got!r}, want {want!r}")

    def check_true(self, label, got):
        self.check(label, bool(got), True)

    def ok(self) -> bool:
        return not self.failures


B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def make_address(seed: int) -> str:
    """A deterministic, structurally valid Solana address for fixtures.

    Generated rather than hardcoded: shipping real mint addresses in the repo
    is the thing this project deliberately avoids everywhere else.
    """
    import random
    rnd = random.Random(seed)
    return "".join(rnd.choice(B58) for _ in range(43))
