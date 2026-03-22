"""
db.py — Connection pool management and read/write routing.

Key rules
---------
1. Writes (INSERT, UPDATE, DELETE, transactions) → primary only.
2. Heavy reads (analytics reports) → a randomly chosen replica.
3. Latency-sensitive reads (user lookups) → primary to avoid replica lag.
4. Each shard has its own independent pool; pools are created lazily.
5. Redis is used for session data; connections are pooled via redis-py.

Dependencies (install with pip):
    psycopg[pool]   >= 3.1
    redis           >= 5.0
"""

import random
import logging
from contextlib import contextmanager
from typing import Generator

import psycopg
from psycopg_pool import ConnectionPool
import redis as redis_lib

from config import SHARDS, ShardConfig, REDIS_URL, SESSION_TTL_SECONDS
from shard_router import router

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PostgreSQL connection pools  (keyed by DSN string)
# ---------------------------------------------------------------------------
_pools: dict[str, ConnectionPool] = {}


def _get_pool(dsn: str) -> ConnectionPool:
    if dsn not in _pools:
        _pools[dsn] = ConnectionPool(
            dsn,
            min_size=2,
            max_size=10,
            kwargs={"autocommit": False},
        )
        log.info("Pool created for %s", dsn)
    return _pools[dsn]


def close_all_pools() -> None:
    """Call at application shutdown to release all connections gracefully."""
    for pool in _pools.values():
        pool.close()
    _pools.clear()


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------

def _primary_pool(shard: ShardConfig) -> ConnectionPool:
    return _get_pool(shard.primary_dsn)


def _replica_pool(shard: ShardConfig) -> ConnectionPool:
    """
    Pick a replica at random (simple load spreading).
    Falls back to primary if no replicas are configured, ensuring the
    application keeps working in a single-node dev environment.
    """
    if shard.replica_dsns:
        dsn = random.choice(shard.replica_dsns)
    else:
        log.warning("Shard %d has no replicas; routing read to primary", shard.shard_id)
        dsn = shard.primary_dsn
    return _get_pool(dsn)


# ---------------------------------------------------------------------------
# Public context managers
# ---------------------------------------------------------------------------

@contextmanager
def write_conn(tenant_id: str) -> Generator[psycopg.Connection, None, None]:
    """
    Yields a connection to the PRIMARY of the shard that owns `tenant_id`.
    Use for all INSERT / UPDATE / DELETE and for multi-statement transactions.

    Example:
        with write_conn(tenant_id) as conn:
            with conn.transaction():
                conn.execute("UPDATE billing_accounts SET ...")
                conn.execute("INSERT INTO billing_transactions ...")
    """
    shard = router.shard_for(tenant_id)
    with _primary_pool(shard).connection() as conn:
        yield conn


@contextmanager
def read_conn(tenant_id: str, *, analytics: bool = False) -> Generator[psycopg.Connection, None, None]:
    """
    Yields a read-only connection for `tenant_id`.

    Parameters
    ----------
    analytics : bool
        If True, route to a REPLICA (tolerates slight replication lag;
        avoids saturating the primary with expensive report queries).
        If False (default), route to PRIMARY (consistent, no lag).

    Example:
        # Fast user lookup — primary for consistency
        with read_conn(tenant_id) as conn:
            row = conn.execute("SELECT * FROM users WHERE ...").fetchone()

        # Nightly report — replica to protect primary
        with read_conn(tenant_id, analytics=True) as conn:
            rows = conn.execute("SELECT * FROM daily_analytics WHERE ...").fetchall()
    """
    shard = router.shard_for(tenant_id)
    pool = _replica_pool(shard) if analytics else _primary_pool(shard)
    with pool.connection() as conn:
        # Enforce read-only at the session level so misrouted writes are caught.
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        yield conn
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE")


# ---------------------------------------------------------------------------
# Redis session store
# ---------------------------------------------------------------------------

_redis_client: redis_lib.Redis | None = None


def get_redis() -> redis_lib.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis_lib.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


def set_session(session_id: str, user_id: str, tenant_id: str) -> None:
    """Store a session with a TTL. O(1), no SQL involved."""
    r = get_redis()
    r.hset(f"session:{session_id}", mapping={"user_id": user_id, "tenant_id": tenant_id})
    r.expire(f"session:{session_id}", SESSION_TTL_SECONDS)


def get_session(session_id: str) -> dict | None:
    """Retrieve session data. Returns None if expired or missing."""
    r = get_redis()
    data = r.hgetall(f"session:{session_id}")
    return data if data else None


def delete_session(session_id: str) -> None:
    get_redis().delete(f"session:{session_id}")
