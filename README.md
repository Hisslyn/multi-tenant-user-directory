# Multi-Tenant User Directory

A reference implementation of a horizontally-scalable, multi-tenant B2B SaaS user directory.
Demonstrates **SQL vs. NoSQL data modeling**, **application-level sharding**, and **primary/replica read routing** in plain Python — no frameworks, no magic.

> **Proven working:** tested end-to-end on Python 3.14 + Docker. See [Quick Start](#quick-start).

---

## Table of Contents

- [What This Is](#what-this-is)
- [Key Concepts Explained Simply](#key-concepts-explained-simply)
- [Architecture Overview](#architecture-overview)
- [Project Structure](#project-structure)
- [How a Request Flows Through the System](#how-a-request-flows-through-the-system)
- [Quick Start](#quick-start)
- [Design Decisions](#design-decisions)
  - [Data Modeling: SQL + NoSQL](#1-data-modeling-sql--nosql)
  - [Sharding: Consistent Hashing](#2-sharding-consistent-hashing)
  - [Replication: Read Routing](#3-replication-read-routing)
- [Assignment Coverage Map](#assignment-coverage-map)
- [Key Code Paths](#key-code-paths)
- [Troubleshooting](#troubleshooting)
- [Extending the Project](#extending-the-project)

---

## What This Is

Imagine you're building software that many different companies (called **tenants**) all use at the same time — like Slack, Notion, or Salesforce. Each company has its own users, its own billing, and its own data. They all share the same codebase, but their data must be completely isolated from each other.

This project builds exactly that: a **user directory** for a B2B SaaS platform where:
- Each company (tenant) can have many users
- Billing is tracked per company and must never get corrupted
- The system must keep working even as hundreds of new companies sign up
- Looking up whether a user is logged in must be near-instant

---

## Key Concepts Explained Simply

### What is Multi-Tenancy?
A single deployment of the software serves multiple companies. "Tenant" = one company/customer. Their data lives in the same databases but is always scoped to their `tenant_id` — they can never see each other's data.

### What is ACID?
When money is involved, operations must be **all-or-nothing**. ACID is a guarantee that a database gives you:
- **A**tomic — either every step of an operation succeeds, or none of them do. A charge that deducts money but fails to record the transaction is a financial bug. ACID prevents that.
- **C**onsistent — the database is never left in an illegal state (e.g. a negative balance caused by two simultaneous charges).
- **I**solated — two operations happening at the same time don't interfere with each other.
- **D**urable — once a transaction is committed, it survives a crash.

PostgreSQL gives you ACID. Redis does not — which is why billing uses Postgres and sessions use Redis.

### What is Sharding?
A single database server has limits: disk space, CPU, number of connections. When you outgrow one server, you split the data across multiple servers — this is called **sharding**.

Instead of one big database holding all companies, we run **4 databases** and assign each company to one of them. Acme Corp's data lives entirely on Shard 2. GlobalTech's data lives entirely on Shard 0. They never mix.

The rule for deciding which shard owns which company is called the **shard routing strategy** — we use consistent hashing (explained below).

### What is Replication?
Replication means keeping an identical copy of a database on a second server. The original is called the **primary** — all writes go here. The copy is called the **replica** — it stays in sync by streaming every change from the primary.

Why bother? Because some queries are expensive (like generating a monthly report that scans millions of rows). Running those on the primary would slow it down for everyone. Instead, we run heavy read queries on the replica, keeping the primary fast and available for real-time operations.

### Why Redis for Sessions?
When a user logs in, we need to remember who they are for the duration of their session. We could store this in Postgres, but that's overkill — we don't need joins, foreign keys, or multi-row transactions. We just need:

```
"session:abc123" → { user_id: "...", tenant_id: "..." }
```

Redis is a key-value store that answers this in under 1ms and automatically deletes expired sessions (TTL). No cron jobs, no cleanup queries.

---

## Architecture Overview

```
                        ┌─────────────────────────────────────┐
                        │          Application Layer          │
                        │         user_directory.py           │
                        └──────────────┬──────────────────────┘
                                       │
               ┌───────────────────────┼───────────────────────┐
               │                       │                       │
       ┌───────▼───────┐       ┌───────▼───────┐      ┌───────▼───────┐
       │  shard_router │       │     db.py     │      │     Redis     │
       │ consistent    │       │  write_conn() │      │  (sessions)   │
       │ hash ring     │       │  read_conn()  │      └───────────────┘
       └───────┬───────┘       └───────┬───────┘
               │                       │
       ┌───────▼───────────────────────▼──────────────────────┐
       │                  4 PostgreSQL Shards                 │
       │                                                      │
       │  Shard 0            Shard 1           Shard 2/3 ...  │
       │  ┌────────┐         ┌────────┐                       │
       │  │Primary │──rep──► │Replica │  (×4 shards)          │
       │  └────────┘         └────────┘                       │
       └──────────────────────────────────────────────────────┘
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
├── schema.sql          # PostgreSQL table definitions — deployed on every shard
├── config.py           # Shard topology (connection strings) and Redis settings
├── shard_router.py     # Consistent-hash ring: maps tenant_id → which shard
├── db.py               # Connection pools, write_conn / read_conn, Redis helpers
├── user_directory.py   # Service layer: tenants, users, billing, analytics, sessions
├── docker-compose.yml  # Local dev cluster (4 primaries + 4 replicas + Redis)
├── requirements.txt    # Python dependencies
├── .env.example        # Template for environment variables — copy to .env
└── .gitignore
```

### What each file does

**`schema.sql`** — The blueprint for the database. Defines every table, column, data type, constraint, and index. This exact file is loaded into all 4 shards when Docker starts them. If you change the schema, you re-run this file on all shards.

**`config.py`** — Knows where all the databases are (connection strings / DSNs). Reads from environment variables (your `.env` file) so you can switch between local dev and production without changing code. Also holds the Redis URL and session TTL.

**`shard_router.py`** — The brain of the sharding system. At startup it builds a consistent-hash ring. When you ask "which shard owns tenant X?", it hashes the `tenant_id` and does a binary search on the ring to find the answer in O(log N) time. This decision is **deterministic** — the same tenant always maps to the same shard.

**`db.py`** — Manages database connections. Maintains a pool of reusable connections per shard (so we're not opening a new connection on every request). Exposes two context managers:
- `write_conn(tenant_id)` — gives you a connection to the **primary** of the correct shard
- `read_conn(tenant_id, analytics=False)` — gives you a connection to the **primary** (for normal reads) or **replica** (for analytics reports)

Also handles all Redis operations: store session, retrieve session, delete session.

**`user_directory.py`** — The service layer. This is the only file most application code would import. Contains all the business logic: creating tenants, managing users, charging billing accounts, running analytics queries, and handling login/logout sessions. All database routing happens automatically via `db.py` — the caller never touches SQL directly.

**`docker-compose.yml`** — Spins up the entire infrastructure locally with one command. Starts 4 primary Postgres instances, 4 replica instances, and 1 Redis instance. The schema is automatically applied to each primary on first boot.

---

## How a Request Flows Through the System

Here's exactly what happens when you call `charge_tenant(tenant_id, Decimal("29.99"), "March subscription")`:

```
1. user_directory.py: charge_tenant() is called with a tenant_id

2. db.py: write_conn(tenant_id) is called
   └── shard_router.py: router.shard_for(tenant_id)
       └── SHA-256 hash the tenant_id → get a ring position
       └── bisect_right on sorted ring → find the owning virtual node
       └── return ShardConfig for e.g. Shard 2 (primary: localhost:5436)

3. db.py: _get_pool("postgresql://...localhost:5436/userdb")
   └── return existing connection pool (or create one if first request)
   └── check out a connection from the pool

4. user_directory.py: begin a database transaction
   └── SELECT billing_id, balance_usd FROM billing_accounts
       WHERE tenant_id = %s FOR UPDATE
       (row-level lock acquired — no other transaction can modify this row)
   └── check balance >= 29.99 (raises ValueError if not)
   └── UPDATE billing_accounts SET balance_usd = balance_usd - 29.99
   └── INSERT INTO billing_transactions (billing_id, tenant_id, amount_usd, ...)
   └── COMMIT  ← both writes land atomically, or neither does

5. connection returned to pool
```

And for `login(tenant_id, email)`:

```
1. user_directory.py: get_user_by_email(tenant_id, email)
   └── read_conn(tenant_id)  → routes to PRIMARY (login must be consistent)
   └── SELECT user_id, ... FROM users WHERE tenant_id=%s AND email=%s

2. if user exists and is active:
   └── generate session_id = uuid4()
   └── db.py: set_session(session_id, user_id, tenant_id)
       └── Redis pipeline:
           HSET session:<session_id> user_id <...> tenant_id <...>
           EXPIRE session:<session_id> 3600
           EXEC  ← both commands sent atomically

3. return session_id token to caller (stored in client cookie)
```

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

This starts:
- 4 PostgreSQL **primaries** on ports `5432`, `5434`, `5436`, `5438`
- 4 PostgreSQL **replicas** on ports `5433`, `5435`, `5437`, `5439`
- 1 **Redis** instance on port `6379`

The schema (`schema.sql`) is automatically applied to each primary on first boot.

Wait ~5 seconds for all containers to become healthy before proceeding.

### 3. Configure environment

```bash
cp .env.example .env
# No edits needed for local dev — .env.example already points to localhost
```

### 4. Run a quick smoke test

```python
from user_directory import create_tenant, create_user, add_credit, charge_tenant, login
from db import close_all_pools
from decimal import Decimal

# Create a tenant — atomically inserts tenant row + billing account
acme = create_tenant("Acme Corp", plan="growth")
print(f"Tenant: {acme.name} ({acme.tenant_id})")

# Add a user to that tenant
alice = create_user(acme.tenant_id, "alice@acme.com", "Alice", role="owner")
print(f"User: {alice.email}")

# Fund the account, then charge it — both are atomic ACID transactions
add_credit(acme.tenant_id, Decimal("100.00"), "Initial top-up")
charge_tenant(acme.tenant_id, Decimal("29.99"), "March subscription")
print("Billing OK")

# Login: looks up user in Postgres, creates session in Redis
token = login(acme.tenant_id, "alice@acme.com")
print(f"Session token: {token}")

# Clean shutdown — closes all Postgres connection pools gracefully
close_all_pools()
```

Expected output:
```
Tenant: Acme Corp (some-uuid-here)
User: alice@acme.com
Billing OK
Session token: some-other-uuid-here
```

### 5. Tear down

```bash
docker compose down        # stop containers, keep data volumes
docker compose down -v     # stop containers AND wipe all data
```

---

## Design Decisions

### 1. Data Modeling: SQL + NoSQL

**Why PostgreSQL for users and billing?**

Billing data has strict correctness requirements. Consider what happens if the server crashes halfway through charging a tenant:

- Without ACID: balance is decremented but no transaction record is created. Money disappears.
- With ACID: the incomplete transaction is rolled back. Balance and ledger stay in sync.

`charge_tenant()` in `user_directory.py` uses a single database transaction to:

1. Lock the billing row with `SELECT ... FOR UPDATE` — prevents two simultaneous requests from both seeing the same balance and both approving a charge that would overdraw the account.
2. Verify the balance is sufficient.
3. Decrement `balance_usd`.
4. Insert a row in `billing_transactions` (the ledger entry).

Steps 3 and 4 are wrapped in `conn.transaction()`. If step 4 fails for any reason, step 3 is automatically undone. The balance and the transaction record are **always** consistent with each other.

**Why Redis for sessions?**

Session data has completely different requirements:
- It's a simple key-value lookup — no joins, no foreign keys, no multi-row atomicity needed.
- It expires automatically after a set time (TTL).
- It needs to be read on **every single request** — sub-millisecond latency matters.

Redis stores `session:<token>` as a hash: `{ user_id: "...", tenant_id: "..." }`. Reads are O(1). TTL expiry is handled natively — no background jobs or cleanup queries. A `hset` + `expire` are sent together in a Redis pipeline so the key is never left without a TTL, even if the process crashes between the two calls.

---

### 2. Sharding: Consistent Hashing

**Why shard at all?**

A single PostgreSQL instance has practical limits:
- Maximum ~a few hundred connections before contention hurts performance
- Storage is bound to one machine's disk
- All tenants compete for the same CPU and I/O

Sharding by `tenant_id` means each shard only holds a subset of tenants. All queries for Acme Corp hit Shard 2 only — they don't compete with GlobalTech's queries running on Shard 0.

**Why consistent hashing instead of modulo (`hash(id) % N`)?**

Modulo is simple but has a catastrophic failure mode: if you add a new shard (change N from 4 to 5), nearly every tenant gets reassigned to a different shard. You'd have to migrate ~80% of your data.

Consistent hashing minimizes this:

| Approach | Tenants remapped when adding 1 shard |
|---|---|
| `hash(id) % N` | ~75% (for N=4 → N=5) |
| Consistent hashing | ~20% (only 1/N of tenants move) |

**How it works:**

`shard_router.py` builds a virtual-node ring at startup. Imagine a circle numbered 0 to 2³²:

```
Ring (simplified, 4 shards × 3 virtual nodes each):
────────────────────────────────────────────────────
0 ──[S1]────[S3]────[S0]────[S2]────[S1]──── 2³²
                      ▲
               tenant_id hashes here → owned by S0
```

- Each physical shard gets **150 virtual nodes** spread evenly around the ring. More virtual nodes = more even distribution of tenants.
- To find the shard for a `tenant_id`: SHA-256 hash the ID → get a position on the ring → find the next virtual node clockwise (`bisect_right`) → that node's shard owns this tenant.
- This mapping is **deterministic**: the same `tenant_id` always resolves to the same shard, every time, with no external state needed.

All SQL queries for a tenant hit **exactly one shard**. Cross-shard joins never occur.

---

### 3. Replication: Read Routing

**Why replicate?**

Analytics queries — like "show me daily active users for the last 30 days" — scan large ranges of data. Running these on the primary database blocks it for everyone during that scan. A tenant's login request might wait seconds for a report query to finish.

Replication solves this by maintaining an identical copy (replica) of each shard. Analytics reads go to the replica; everything else goes to the primary.

**How routing works in the code:**

`db.py` exposes two context managers:

```python
# Always goes to the PRIMARY — for writes and consistency-sensitive reads
with write_conn(tenant_id) as conn:
    ...

# Goes to PRIMARY by default, REPLICA if analytics=True
with read_conn(tenant_id, analytics=True) as conn:
    ...
```

```
                  write_conn(tenant_id)
                        │
                        ▼
                    PRIMARY  ◄──── all writes, latency-sensitive reads
                        │
              streaming replication
                        │
                        ▼
                    REPLICA  ◄──── analytics / heavy reads only
```

**Which queries go where and why:**

| Function | Route | Why |
|---|---|---|
| `create_tenant()` | Primary | Write — must go to primary |
| `create_user()` | Primary | Write — must go to primary |
| `charge_tenant()` | Primary | Write + needs `FOR UPDATE` lock |
| `get_user_by_email()` | Primary | Called at login — stale data = auth bug |
| `list_users()` | Primary | UI must reflect current state |
| `get_tenant_analytics()` | **Replica** | Expensive scan, can tolerate slight lag |

**Safety guard:** the replica connection is set `READ ONLY` at the PostgreSQL session level (`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`). If a bug ever routes a write to `read_conn()`, Postgres will reject it with an error instead of silently corrupting the replica. This runs in a `try/finally` so the connection is always reset before returning to the pool.

**Replica lag:** replicas trail the primary by milliseconds. This is acceptable for analytics (snapshots are written once per day anyway) but unacceptable for login (you must see the user you just created). The routing decisions above reflect this.

**Single-node dev fallback:** if no `SHARD{n}_REPLICA_DSN` is set, `config.py` defaults the replica DSN to the same address as the primary. The app works identically in single-node dev without any code changes.

---

## Assignment Coverage Map

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

## Key Code Paths

| Scenario | File | What to look at |
|---|---|---|
| ACID billing charge | `user_directory.py` | `charge_tenant()` — `FOR UPDATE` lock + transaction |
| Shard assignment | `shard_router.py` | `ShardRouter.shard_for()` — binary search on ring |
| Write vs. read routing | `db.py` | `write_conn()`, `read_conn()`, `_replica_pool()` |
| Session lifecycle | `user_directory.py` + `db.py` | `login()` / `logout()` / `resolve_session()` |
| Schema | `schema.sql` | Table definitions, indexes, constraints |

---

## Troubleshooting

**`failed to resolve host 'shard0-primary'`**
Your `.env` file is missing. The app is using Docker-internal hostnames instead of `localhost`. Fix:
```bash
cp .env.example .env
```

**`PoolTimeout: couldn't get a connection after 30.00 sec`**
Docker containers aren't up yet, or didn't start cleanly. Check:
```bash
docker compose ps          # are all containers healthy?
docker compose logs shard0-primary   # any startup errors?
```

**`PythonFinalizationError: cannot join thread at interpreter shutdown`**
Harmless warning on Python 3.14 when connection pools aren't closed before the interpreter exits. Fix by calling `close_all_pools()` before your script ends (as shown in the smoke test).

**`redis.exceptions.DataError: Invalid input of type: 'UUID'`**
psycopg3 returns UUID columns as Python `uuid.UUID` objects. Always stringify them before passing to Redis. Already handled in `user_directory.py` — if you see this in new code, wrap with `str()`.

**`duplicate key value violates unique constraint "users_tenant_id_email_key"`**
You're trying to create a user with an email that already exists for that tenant. The schema enforces uniqueness of `(tenant_id, email)` — two users at the same company can't share an email.

---

## Extending the Project

| Goal | Where to start |
|---|---|
| Add a 5th shard | Add entry to `SHARDS` in `config.py`; ~20% of tenants will need migrating |
| Add HTTP API | Wrap `user_directory.py` functions with FastAPI routes |
| Replace Redis with DynamoDB | Swap `set_session` / `get_session` / `delete_session` in `db.py` |
| Add cross-shard admin queries | Implement a scatter-gather layer that fans out to all shards and merges results |
| Production replication | Replace Compose replica services with `pg_basebackup` + `primary_conninfo` in `postgresql.conf` |
| Add connection retry/backoff | Wrap `_get_pool()` calls with `tenacity` for transient failures |
