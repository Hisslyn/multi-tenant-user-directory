"""
shard_router.py — Application-level sharding by tenant_id.

Strategy: consistent hashing over a virtual-node ring.
  - Each physical shard owns (VNODES_PER_SHARD) virtual nodes.
  - A tenant's shard is determined by hashing its tenant_id (UUID) and
    walking the sorted ring to find the first virtual node >= hash.
  - Consistent hashing minimises key movement when shards are added/removed.

Why not plain modulo?
  hash(tenant_id) % N  is simpler but reshuffles ~all keys when N changes.
  Consistent hashing reshuffles only ~1/N of keys.
"""

import hashlib
import bisect
from typing import Dict, Tuple

from config import SHARDS, NUM_SHARDS, ShardConfig

VNODES_PER_SHARD = 150   # more vnodes → better key distribution


class ShardRouter:
    """
    Builds a consistent-hash ring at startup and exposes a single
    public method: `shard_for(tenant_id) -> ShardConfig`.
    """

    def __init__(self):
        # ring maps a sorted list of virtual-node hashes → shard_id
        self._ring: Dict[int, int] = {}   # vnode_hash → shard_id
        self._sorted_keys: list[int] = []
        self._shards: Dict[int, ShardConfig] = {s.shard_id: s for s in SHARDS}

        self._build_ring()

    # ------------------------------------------------------------------
    # Ring construction
    # ------------------------------------------------------------------

    def _build_ring(self) -> None:
        for shard in SHARDS:
            for vnode in range(VNODES_PER_SHARD):
                vkey = f"shard-{shard.shard_id}-vnode-{vnode}"
                h = self._hash(vkey)
                self._ring[h] = shard.shard_id

        self._sorted_keys = sorted(self._ring.keys())

    @staticmethod
    def _hash(key: str) -> int:
        """Return a 32-bit unsigned integer from a SHA-256 digest."""
        digest = hashlib.sha256(key.encode()).hexdigest()
        return int(digest[:8], 16)  # first 4 bytes → deterministic, fast

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def shard_for(self, tenant_id: str) -> ShardConfig:
        """
        Return the ShardConfig responsible for the given tenant_id.

        The tenant_id (a UUID string) is hashed, then we do a binary search
        on the sorted ring to find the next virtual node clockwise.
        """
        h = self._hash(tenant_id)
        idx = bisect.bisect_right(self._sorted_keys, h) % len(self._sorted_keys)
        shard_id = self._ring[self._sorted_keys[idx]]
        return self._shards[shard_id]

    def debug_distribution(self, tenant_ids: list[str]) -> Dict[int, list[str]]:
        """Show which shard each tenant lands on — useful for load analysis."""
        distribution: Dict[int, list[str]] = {s.shard_id: [] for s in SHARDS}
        for tid in tenant_ids:
            shard = self.shard_for(tid)
            distribution[shard.shard_id].append(tid)
        return distribution


# Module-level singleton — import and use directly
router = ShardRouter()
