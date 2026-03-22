-- =============================================================================
-- Multi-Tenant User Directory: PostgreSQL Schema
-- Handles billing and user data requiring ACID guarantees.
-- This schema is deployed identically on every shard.
-- =============================================================================

-- gen_random_uuid() is built-in on PG 13+; pgcrypto provides it on older versions.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Tenants
-- ---------------------------------------------------------------------------
CREATE TABLE tenants (
    tenant_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT        NOT NULL,
    plan        TEXT        NOT NULL CHECK (plan IN ('starter', 'growth', 'enterprise')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Users
-- ---------------------------------------------------------------------------
CREATE TABLE users (
    user_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    email       TEXT        NOT NULL,
    display_name TEXT,
    role        TEXT        NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'admin', 'member')),
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Enforce uniqueness of email within a tenant (not globally)
    UNIQUE (tenant_id, email)
);

CREATE INDEX idx_users_tenant ON users(tenant_id);

-- ---------------------------------------------------------------------------
-- Billing Accounts  (one per tenant)
-- ---------------------------------------------------------------------------
CREATE TABLE billing_accounts (
    billing_id  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL UNIQUE REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    balance_usd NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Billing Transactions
-- ACID-critical: debits / credits must be atomic with balance updates.
-- ---------------------------------------------------------------------------
CREATE TABLE billing_transactions (
    txn_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    billing_id      UUID        NOT NULL REFERENCES billing_accounts(billing_id),
    tenant_id       UUID        NOT NULL,          -- denormalized for shard-local queries
    amount_usd      NUMERIC(12,2) NOT NULL,        -- positive = credit, negative = debit
    description     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_billing_txn_tenant ON billing_transactions(tenant_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Daily Analytics Snapshot  (populated by background jobs, read from replica)
-- ---------------------------------------------------------------------------
CREATE TABLE daily_analytics (
    snapshot_id     BIGSERIAL   PRIMARY KEY,
    tenant_id       UUID        NOT NULL,
    snapshot_date   DATE        NOT NULL,
    active_users    INT         NOT NULL DEFAULT 0,
    new_users       INT         NOT NULL DEFAULT 0,
    total_spend_usd NUMERIC(12,2) NOT NULL DEFAULT 0.00,

    UNIQUE (tenant_id, snapshot_date)
);

CREATE INDEX idx_analytics_tenant_date ON daily_analytics(tenant_id, snapshot_date DESC);
