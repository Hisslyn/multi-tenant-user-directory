"""
user_directory.py — Service layer for the multi-tenant user directory.

Demonstrates:
  • ACID transactions for billing (debit must be atomic with transaction record).
  • Sharded writes routed through write_conn(tenant_id).
  • Analytics reads routed to replicas through read_conn(..., analytics=True).
  • Session management delegated to Redis.
"""

from __future__ import annotations

import uuid
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from db import write_conn, read_conn, set_session, get_session, delete_session

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes (lightweight domain objects — no ORM to keep the example clear)
# ---------------------------------------------------------------------------

@dataclass
class Tenant:
    tenant_id: str
    name: str
    plan: str


@dataclass
class User:
    user_id: str
    tenant_id: str
    email: str
    display_name: Optional[str]
    role: str
    is_active: bool


@dataclass
class AnalyticsSnapshot:
    snapshot_date: str
    active_users: int
    new_users: int
    total_spend_usd: Decimal


# ---------------------------------------------------------------------------
# Tenant management
# ---------------------------------------------------------------------------

def create_tenant(name: str, plan: str = "starter") -> Tenant:
    """
    Create a new tenant and its billing account in a single ACID transaction.
    Both rows are inserted atomically; if either fails, nothing is committed.
    """
    tenant_id = str(uuid.uuid4())

    with write_conn(tenant_id) as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO tenants (tenant_id, name, plan)
                VALUES (%s, %s, %s)
                """,
                (tenant_id, name, plan),
            )
            conn.execute(
                """
                INSERT INTO billing_accounts (tenant_id, balance_usd)
                VALUES (%s, 0.00)
                """,
                (tenant_id,),
            )

    log.info("Tenant created: %s (%s) on shard for %s", name, tenant_id[:8], tenant_id)
    return Tenant(tenant_id=tenant_id, name=name, plan=plan)


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

def create_user(tenant_id: str, email: str, display_name: str = "", role: str = "member") -> User:
    user_id = str(uuid.uuid4())

    with write_conn(tenant_id) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, tenant_id, email, display_name, role)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (user_id, tenant_id, email, display_name or None, role),
        )

    return User(user_id=user_id, tenant_id=tenant_id, email=email,
                display_name=display_name, role=role, is_active=True)


def get_user_by_email(tenant_id: str, email: str) -> Optional[User]:
    """
    Latency-sensitive lookup → routed to PRIMARY (no replica lag risk).
    Called during login; stale data here would be a security issue.
    """
    with read_conn(tenant_id) as conn:
        row = conn.execute(
            """
            SELECT user_id, tenant_id, email, display_name, role, is_active
            FROM users
            WHERE tenant_id = %s AND email = %s
            """,
            (tenant_id, email),
        ).fetchone()

    if row is None:
        return None
    return User(str(row[0]), str(row[1]), row[2], row[3], row[4], row[5])


def list_users(tenant_id: str) -> list[User]:
    """All active users for a tenant. Also primary-routed for consistency."""
    with read_conn(tenant_id) as conn:
        rows = conn.execute(
            """
            SELECT user_id, tenant_id, email, display_name, role, is_active
            FROM users
            WHERE tenant_id = %s AND is_active = TRUE
            ORDER BY email
            """,
            (tenant_id,),
        ).fetchall()

    return [User(str(r[0]), str(r[1]), r[2], r[3], r[4], r[5]) for r in rows]


def deactivate_user(tenant_id: str, user_id: str) -> None:
    with write_conn(tenant_id) as conn:
        conn.execute(
            "UPDATE users SET is_active = FALSE WHERE tenant_id = %s AND user_id = %s",
            (tenant_id, user_id),
        )


# ---------------------------------------------------------------------------
# Billing — ACID-critical path
# ---------------------------------------------------------------------------

def charge_tenant(tenant_id: str, amount_usd: Decimal, description: str) -> None:
    """
    Debit a tenant's balance and record the transaction atomically.

    Both the balance UPDATE and the transaction INSERT happen inside a
    single database transaction.  If the balance check fails (insufficient
    funds) the entire block is rolled back — no partial state is written.

    This is the core ACID guarantee: the balance and the ledger are always
    consistent with each other.
    """
    if amount_usd <= 0:
        raise ValueError("Charge amount must be positive")

    with write_conn(tenant_id) as conn:
        with conn.transaction():
            row = conn.execute(
                """
                SELECT billing_id, balance_usd
                FROM billing_accounts
                WHERE tenant_id = %s
                FOR UPDATE          -- row-level lock prevents concurrent overdrafts
                """,
                (tenant_id,),
            ).fetchone()

            if row is None:
                raise LookupError(f"No billing account for tenant {tenant_id}")

            billing_id, balance = row
            if balance < amount_usd:
                raise ValueError(
                    f"Insufficient balance: have ${balance}, need ${amount_usd}"
                )

            conn.execute(
                """
                UPDATE billing_accounts
                SET balance_usd = balance_usd - %s,
                    updated_at  = now()
                WHERE billing_id = %s
                """,
                (amount_usd, billing_id),
            )
            conn.execute(
                """
                INSERT INTO billing_transactions
                    (billing_id, tenant_id, amount_usd, description)
                VALUES (%s, %s, %s, %s)
                """,
                (billing_id, tenant_id, -amount_usd, description),
            )

    log.info("Charged tenant %s $%.2f — %s", tenant_id[:8], amount_usd, description)


def add_credit(tenant_id: str, amount_usd: Decimal, description: str) -> None:
    """Credit a tenant's balance (e.g. subscription payment received)."""
    if amount_usd <= 0:
        raise ValueError("Credit amount must be positive")

    with write_conn(tenant_id) as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT billing_id FROM billing_accounts WHERE tenant_id = %s FOR UPDATE",
                (tenant_id,),
            ).fetchone()

            if row is None:
                raise LookupError(f"No billing account for tenant {tenant_id}")

            billing_id = row[0]
            conn.execute(
                """
                UPDATE billing_accounts
                SET balance_usd = balance_usd + %s,
                    updated_at  = now()
                WHERE billing_id = %s
                """,
                (amount_usd, billing_id),
            )
            conn.execute(
                """
                INSERT INTO billing_transactions
                    (billing_id, tenant_id, amount_usd, description)
                VALUES (%s, %s, %s, %s)
                """,
                (billing_id, tenant_id, amount_usd, description),
            )


# ---------------------------------------------------------------------------
# Analytics — replica-routed reads
# ---------------------------------------------------------------------------

def get_tenant_analytics(tenant_id: str, days: int = 30) -> list[AnalyticsSnapshot]:
    """
    Pull the last `days` daily snapshots for a tenant.

    analytics=True routes this query to a READ REPLICA.  These reports are
    expensive (full-range scans) and can tolerate a small amount of replication
    lag (snapshots are written once per day by a background job).

    Routing them to replicas keeps the primary free for low-latency OLTP work.
    """
    with read_conn(tenant_id, analytics=True) as conn:
        rows = conn.execute(
            """
            SELECT snapshot_date, active_users, new_users, total_spend_usd
            FROM daily_analytics
            WHERE tenant_id = %s
              AND snapshot_date >= CURRENT_DATE - (INTERVAL '1 day' * %s)
            ORDER BY snapshot_date DESC
            """,
            (tenant_id, days),
        ).fetchall()

    return [
        AnalyticsSnapshot(
            snapshot_date=str(r[0]),
            active_users=r[1],
            new_users=r[2],
            total_spend_usd=r[3],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Session management (Redis)
# ---------------------------------------------------------------------------

def login(tenant_id: str, email: str) -> Optional[str]:
    """
    Simulate a login: look up the user (primary DB), then create a Redis session.
    Returns the session_id token to be stored in the client cookie, or None.
    """
    user = get_user_by_email(tenant_id, email)
    if user is None or not user.is_active:
        return None

    session_id = str(uuid.uuid4())
    set_session(session_id, user.user_id, tenant_id)
    log.info("Session created for %s / %s", tenant_id[:8], email)
    return session_id


def logout(session_id: str) -> None:
    delete_session(session_id)


def resolve_session(session_id: str) -> Optional[dict]:
    """Return {'user_id': ..., 'tenant_id': ...} or None if expired."""
    return get_session(session_id)
