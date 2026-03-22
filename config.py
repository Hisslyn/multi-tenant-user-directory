"""
config.py — Shard topology and connection settings.

In production these values come from environment variables or a config service.
Each SHARD entry declares a primary DSN and zero or more replica DSNs.
Redis is a single logical cluster (or ElastiCache) shared across tenants.
"""

import os
from dataclasses import dataclass, field
from typing import List

# ---------------------------------------------------------------------------
# Shard topology
# ---------------------------------------------------------------------------
# NUM_SHARDS must stay constant once data is written; changing it requires
# a full re-shard migration.
NUM_SHARDS = 4


@dataclass
class ShardConfig:
    shard_id: int
    primary_dsn: str
    replica_dsns: List[str] = field(default_factory=list)


def _shard(n: int) -> ShardConfig:
    """Build a ShardConfig from environment variables, falling back to Docker Compose defaults."""
    primary = os.environ.get(
        f"SHARD{n}_PRIMARY_DSN",
        f"postgresql://app:secret@shard{n}-primary:5432/userdb",
    )
    replica = os.environ.get(
        f"SHARD{n}_REPLICA_DSN",
        f"postgresql://app:secret@shard{n}-replica:5432/userdb",
    )
    return ShardConfig(shard_id=n, primary_dsn=primary, replica_dsns=[replica])


SHARDS: List[ShardConfig] = [_shard(n) for n in range(NUM_SHARDS)]

# ---------------------------------------------------------------------------
# Redis (session store)
# ---------------------------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "3600"))
