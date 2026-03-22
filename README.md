# Multi-Tenant User Directory

A reference implementation of a horizontally-scalable, multi-tenant B2B SaaS user directory.
Demonstrates **SQL vs. NoSQL data modeling**, **application-level sharding**, and **primary/replica read routing** in plain Python — no frameworks, no magic.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Project Structure](#project-structure)
- [Quick Start](#quick-start)
- [Design Decisions](#design-decisions)
  - [Data Modeling: SQL + NoSQL](#1-data-modeling-sql--nosql)
  - [Sharding: Consistent Hashing](#2-sharding-consistent-hashing)
  - [Replication: Read Routing](#3-replication-read-routing)
- [Key Code Paths](#key-code-paths)
- [Extending the Project](#extending-the-project)

---

## Architecture Overview

```
                        ┌─────────────────────────────────────┐
                        │          Application Layer           │
                        │         user_directory.py            │
                        └──────────────┬──────────────────────┘
                                       │
               ┌───────────────────────┼───────────────────────┐
               │                       │                       │
       ┌───────▼───────┐       ┌───────▼───────┐      ┌───────▼───────┐
       │  shard_router │       │     db.py      │      │     Redis     │
       │ consistent    │       │  write_conn()  │      │  (sessions)   │
       │ hash ring     │       │  read_conn()   │      └───────────────┘
       └───────┬───────┘       └───────┬────────┘
               │                       │
       ┌───────▼───────────────────────▼──────────────────────┐
       │                  4 PostgreSQL Shards                  │
       │                                                       │
       │  Shard 0            Shard 1            Shard 2/3 ...  │
       │  ┌────────┐         ┌────────┐                        │
       │  │Primary │──rep──► │Replica │  (×4 shards)          │
       │  └────────┘         └────────┘                        │
       └───────────────────────────────────────────────────────┘
```

| Layer | Technology | Purpose |
|---|---|---|
| Relational store | PostgreSQL 16 | Users, tenants, billing — ACID required |
| Session store | Redis 7 | Fast O(1) session lookups with built-in TTL |
| Shard routing | Python (consistent hash) | Partition tenants across 4 DB shards |
| Replication | PostgreSQL streaming | Protect primary from analytics read load |

---

## Project Structure

```
.
├── schema.sql          # PostgreSQL DDL — deployed on every shard
├── config.py           # Shard topology (DSNs) and Redis settings
├── shard_router.py     # Consistent-hash ring; maps tenant_id → shard
├── db.py               # Connection pools, write_conn / read_conn, Redis helpers
├── user_directory.py   # Service layer: tenants, users, billing, analytics, sessions
├── docker-compose.yml  # Local dev cluster (4 primaries + 4 replicas + Redis)
├── requirements.txt    # Python dependencies
├── .env.example        # Template for environment variables
└── .gitignore
```

### Assignment Coverage Map

| Task | Requirement | File(s) | Key symbol |
|---|---|---|---|
| **1. Data Modeling** | Relational schema (ACID) | `schema.sql` | `billing_accounts`, `billing_transactions`, FK constraints |
| **1. Data Modeling** | ACID transaction in code | `user_directory.py` | `charge_tenant()` — `SELECT FOR UPDATE` + atomic debit + ledger insert |
| **1. Data Modeling** | NoSQL key-value store for sessions | `db.py` | `set_session()`, `get_session()`, `delete_session()` via Redis |
| **2. Sharding** | Application-level shard routing | `shard_router.py` | `ShardRouter` — consistent-hash ring with 150 virtual nodes per shard |
| **2. Sharding** | Partition by `tenant_id` | `db.py` | `write_conn(tenant_id)`, `read_conn(tenant_id)` — both call `router.shard_for()` |
| **2. Sharding** | Queries hit only one shard | `shard_router.py` + `db.py` | `shard_for()` → single `ConnectionPool` per request |
| **3. Replication** | Primary/replica infrastructure | `docker-compose.yml` + `config.py` | 4 primaries + 4 replicas; DSNs stored per shard |
| **3. Replication** | Route heavy reads to replica | `user_directory.py` | `get_tenant_analytics()` calls `read_conn(analytics=True)` |
| **3. Replication** | Protect primary performance | `db.py` | `read_conn()` with `analytics=True` → `_replica_pool()`; session set `READ ONLY` |

---

## Quick Start

### Prerequisites

- Python 3.11+
- Docker + Docker Compose

### 1. Clone and install dependencies

```bash
git clone https://github.com/Hisslyn/multi-tenant-user-directory
cd multi-tenant-user-directory

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Start the local cluster

```bash
docker compose up -d
```

This starts 4 PostgreSQL primaries (ports 5432, 5434, 5436, 5438), 4 replicas (5433, 5435, 5437, 5439), and Redis (6379).
The schema is automatically applied to each primary via the init script.

### 3. Configure environment

```bash
cp .env.example .env
# .env.example already points to localhost — no edits needed for local dev
```

### 4. Run a quick smoke test

```python
from user_directory import create_tenant, create_user, add_credit, charge_tenant, login
from decimal import Decimal

# Create a tenant (atomic: inserts tenant + billing account)
acme = create_tenant("Acme Corp", plan="growth")

# Add a user
alice = create_user(acme.tenant_id, "alice@acme.com", "Alice", role="owner")

# Fund and charge the account
add_credit(acme.tenant_id, Decimal("100.00"), "Initial top-up")
charge_tenant(acme.tenant_id, Decimal("29.99"), "March subscription")

# Login creates a Redis session; returns a session token
token = login(acme.tenant_id, "alice@acme.com")
print(f"Session token: {token}")
```

### 5. Tear down

```bash
docker compose down -v   # -v removes volumes (wipes all data)
```

---

## Design Decisions

### 1. Data Modeling: SQL + NoSQL

**PostgreSQL for everything transactional**

Billing data requires [ACID](https://en.wikipedia.org/wiki/ACID) guarantees.
A charge that succeeds but leaves no transaction record is a financial bug.
`charge_tenant()` in `user_directory.py` uses a single database transaction to:

1. Lock the billing row with `SELECT ... FOR UPDATE` (prevents concurrent overdrafts).
2. Decrement `balance_usd`.
3. Insert a row in `billing_transactions`.

If either write fails, the entire transaction rolls back — balance and ledger stay in sync.

**Redis for sessions**

Session data is a pure key-value lookup (`session:<token>` → `{user_id, tenant_id}`).
There is no need for joins, foreign keys, or multi-row atomicity.
Redis gives sub-millisecond reads and handles TTL expiry natively — no cron jobs needed.

---

### 2. Sharding: Consistent Hashing

**Why shard at all?**
A single PostgreSQL instance tops out around a few hundred thousand concurrent connections and tens of terabytes.
Sharding by `tenant_id` keeps each shard's dataset smaller and eliminates cross-tenant lock contention.

**Why consistent hashing instead of modulo (`hash % N`)?**

| Approach | Keys moved when adding a shard |
|---|---|
| `hash(id) % N` | ~(N-1)/N ≈ 75 % (for N=4 → N=5) |
| Consistent hashing | ~1/N ≈ 20 % |

`shard_router.py` builds a virtual-node ring at startup:
- Each physical shard gets 150 virtual nodes spread across the ring.
- A `tenant_id` is SHA-256 hashed to a ring position.
- `bisect_right` finds the next clockwise virtual node — that shard owns the tenant.

All SQL queries for a tenant hit exactly **one shard**; cross-shard joins never occur.

```
Ring (simplified, 4 shards × 3 vnodes each):
─────────────────────────────────────────────
0 ──[S1]────[S3]────[S0]────[S2]────[S1]── 2³²
                      ▲
               tenant_id hashes here → owned by S0
```

---

### 3. Replication: Read Routing

Every shard runs **one primary + one replica** (configurable in `config.py`).
The application routes queries based on consistency requirements:

```
                  write_conn(tenant_id)
                        │
                        ▼
                    PRIMARY  ◄──── all writes, low-latency reads
                        │
              streaming replication
                        │
                        ▼
                    REPLICA  ◄──── analytics / heavy reads
```

| Function | Route | Reason |
|---|---|---|
| `get_user_by_email()` | Primary | Called at login — stale data = security risk |
| `list_users()` | Primary | UI must show current state |
| `charge_tenant()` | Primary | Writes always go to primary |
| `get_tenant_analytics()` | **Replica** | Expensive range scan; slight lag is acceptable |

The replica connection is set `READ ONLY` at the session level as a hard guard against accidental writes.

If no replicas are configured (single-node dev), `db.py` silently falls back to the primary.

---

## Key Code Paths

| Scenario | File | What to look at |
|---|---|---|
| ACID billing charge | `user_directory.py` | `charge_tenant()` — `FOR UPDATE` lock + transaction |
| Shard assignment | `shard_router.py` | `ShardRouter.shard_for()` — binary search on ring |
| Write vs. read routing | `db.py` | `write_conn()`, `read_conn()`, `_replica_pool()` |
| Session lifecycle | `user_directory.py` + `db.py` | `login()` / `logout()` / `resolve_session()` |
| Schema | `schema.sql` | Table definitions, indexes, constraints |

---

## Extending the Project

| Goal | Where to start |
|---|---|
| Add a 5th shard | Add entry to `SHARDS` in `config.py`, migrate ~20 % of tenants |
| Add HTTP API | Wrap `user_directory.py` functions with FastAPI routes |
| Replace Redis with DynamoDB | Swap `set_session` / `get_session` in `db.py` |
| Add cross-shard admin queries | Implement a scatter-gather layer that fans out to all shards |
| Production replication | Replace the Compose replicas with `pg_basebackup` + `recovery.conf` |
