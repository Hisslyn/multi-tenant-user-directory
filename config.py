"""
config.py — Shard topology and connection settings.

In production these values come from environment variables or a config service.
Each SHARD entry declares a primary DSN and zero or more replica DSNs.
Redis is a single logical cluster (or ElastiCache) shared across tenants.
"""

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


SHARDS: List[ShardConfig] = [
    ShardConfig(
        shard_id=0,
        primary_dsn="postgresql://app:secret@shard0-primary:5432/userdb",
        replica_dsns=["postgresql://app:secret@shard0-replica:5432/userdb"],
    ),
    ShardConfig(
        shard_id=1,
        primary_dsn="postgresql://app:secret@shard1-primary:5432/userdb",
        replica_dsns=["postgresql://app:secret@shard1-replica:5432/userdb"],
    ),
    ShardConfig(
        shard_id=2,
        primary_dsn="postgresql://app:secret@shard2-primary:5432/userdb",
        replica_dsns=["postgresql://app:secret@shard2-replica:5432/userdb"],
    ),
    ShardConfig(
        shard_id=3,
        primary_dsn="postgresql://app:secret@shard3-primary:5432/userdb",
        replica_dsns=["postgresql://app:secret@shard3-replica:5432/userdb"],
    ),
]

# ---------------------------------------------------------------------------
# Redis (session store)
# ---------------------------------------------------------------------------
REDIS_URL = "redis://redis-cluster:6379/0"
SESSION_TTL_SECONDS = 3600  # 1 hour
