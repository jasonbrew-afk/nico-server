"""Postgres-backed DCA state store for Nico (source of truth, behind nico-server).

nico-server is the ONLY holder of DATABASE_URL and the only writer of state, so
the one-shot `run_live` cron and the always-on `nico-bot` never race on the budget
counters and never get DB credentials — they talk to this service over HTTP with a
scoped token (see the /state, /pending, /placed endpoints in main.py).

Tables
------
dca_state  — singleton row: running spend, triggers used, and the dates of the
             last emission (intent sent to Imago) and last placement (fill).
orders     — ledger, one row per request_id: intent -> approval -> placement.

Money rules enforced here (not in the cron/bot):
  * record_placed is idempotent on request_id (a retried fill never double-spends).
  * spend + trigger count advance exactly once, on confirmed placement.

If DATABASE_URL is unset the whole subsystem is disabled (enabled() -> False) and
the endpoints return 503 — nico-server's webhook-relay behaviour is unchanged.
"""

from __future__ import annotations

import os
from typing import Any, Optional

try:
    import asyncpg
except Exception:  # asyncpg not installed — subsystem stays disabled
    asyncpg = None  # type: ignore

_pool: Optional["asyncpg.Pool"] = None


def enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL")) and asyncpg is not None


SCHEMA = """
CREATE TABLE IF NOT EXISTS dca_state (
    id            INT PRIMARY KEY DEFAULT 1,
    spent         DOUBLE PRECISION NOT NULL DEFAULT 0,
    triggers_used INT NOT NULL DEFAULT 0,
    last_run      DATE,
    last_emitted  DATE,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dca_state_singleton CHECK (id = 1)
);
INSERT INTO dca_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS orders (
    request_id     TEXT PRIMARY KEY,
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL,
    qty            DOUBLE PRECISION NOT NULL,
    notional_usd   DOUBLE PRECISION NOT NULL DEFAULT 0,
    price          DOUBLE PRECISION,
    rationale      TEXT,
    trigger_number INT,
    status         TEXT NOT NULL DEFAULT 'pending',   -- pending | placed | rejected
    order_id       TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    placed_at      TIMESTAMPTZ
);
"""


async def init_pool() -> None:
    """Create the connection pool and ensure the schema. No-op if disabled."""
    global _pool
    if not enabled() or _pool is not None:
        return
    _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=5)
    async with _pool.acquire() as con:
        await con.execute(SCHEMA)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def get_state() -> dict[str, Any]:
    assert _pool is not None
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT spent, triggers_used, last_run, last_emitted, updated_at "
            "FROM dca_state WHERE id = 1"
        )
        n_pending = await con.fetchval("SELECT count(*) FROM orders WHERE status = 'pending'")
    return {
        "spent": float(row["spent"]),
        "triggers_used": int(row["triggers_used"]),
        "last_run": row["last_run"].isoformat() if row["last_run"] else None,
        "last_emitted": row["last_emitted"].isoformat() if row["last_emitted"] else None,
        "pending_count": int(n_pending),
        "updated_at": row["updated_at"].isoformat(),
    }


async def record_pending(order: dict[str, Any]) -> dict[str, Any]:
    """Persist an intent (status=pending) and stamp last_emitted=today.

    Idempotent on request_id (re-emitting the same intent is a no-op insert).
    """
    assert _pool is not None
    async with _pool.acquire() as con, con.transaction():
        await con.execute(
            """
            INSERT INTO orders
                (request_id, symbol, side, qty, notional_usd, price, rationale, trigger_number, status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'pending')
            ON CONFLICT (request_id) DO NOTHING
            """,
            order["request_id"], order["symbol"], order["side"], float(order["qty"]),
            float(order.get("notional_usd") or 0.0),
            order.get("price"), order.get("rationale"), order.get("trigger_number"),
        )
        await con.execute(
            "UPDATE dca_state SET last_emitted = CURRENT_DATE, updated_at = now() WHERE id = 1"
        )
    return {"status": "pending", "request_id": order["request_id"]}


async def record_placed(request_id: str, order_id: str, order_status: str) -> dict[str, Any]:
    """Mark an order placed and advance spend/triggers exactly once.

    Idempotent: a second call for an already-placed request_id does not double-spend.
    """
    assert _pool is not None
    async with _pool.acquire() as con, con.transaction():
        row = await con.fetchrow(
            "SELECT status, notional_usd FROM orders WHERE request_id = $1 FOR UPDATE",
            request_id,
        )
        if row is None:
            return {"status": "unknown_request", "request_id": request_id}
        if row["status"] == "placed":
            return {"status": "duplicate", "request_id": request_id}

        await con.execute(
            "UPDATE orders SET status = 'placed', order_id = $2, placed_at = now() "
            "WHERE request_id = $1",
            request_id, order_id,
        )
        await con.execute(
            "UPDATE dca_state "
            "SET spent = spent + $1, triggers_used = triggers_used + 1, "
            "    last_run = CURRENT_DATE, updated_at = now() "
            "WHERE id = 1",
            float(row["notional_usd"]),
        )
    return {
        "status": "placed",
        "request_id": request_id,
        "order_id": order_id,
        "order_status": order_status,
    }


async def list_pending() -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as con:
        rows = await con.fetch(
            "SELECT request_id, symbol, side, qty, notional_usd, price, rationale, "
            "trigger_number, created_at FROM orders WHERE status = 'pending' ORDER BY created_at"
        )
    return [dict(r) | {"created_at": r["created_at"].isoformat()} for r in rows]
