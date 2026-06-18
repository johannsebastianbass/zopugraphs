"""Camada de persistência (SQLite) do painel multi-empresa ZOPU.

Guarda:
- tenants  : ambientes/clientes (webhook do Bitrix de cada empresa);
- app_users: usuários de login (master = ZOPU vê tudo; client = uma empresa);
- deals/leads: cache local sincronizado do Bitrix (por tenant);
- tenant_meta: metadados (estágios, fontes, nomes de usuários) por tenant;
- sync_log : controle da última sincronização;
- quotas   : metas por vendedor/mês.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

DB_PATH = os.environ.get("ZOPU_DB", str(Path(__file__).parent / "zopu.db"))

DEAL_COLS = [
    "ID", "TITLE", "STAGE_ID", "CATEGORY_ID", "OPPORTUNITY", "CURRENCY_ID",
    "ASSIGNED_BY_ID", "SOURCE_ID", "DATE_CREATE", "DATE_MODIFY", "CLOSEDATE", "CLOSED",
    "SEGMENTO", "MOTIVO",
]
LEAD_COLS = [
    "ID", "TITLE", "STATUS_ID", "STATUS_SEMANTIC_ID", "OPPORTUNITY",
    "ASSIGNED_BY_ID", "SOURCE_ID", "DATE_CREATE", "DATE_MODIFY",
    "SEGMENTO", "CARGO", "MOTIVO",
]
MEETING_COLS = [
    "ID", "TITLE", "STAGE_ID", "CATEGORY_ID", "ASSIGNED_BY_ID", "SOURCE_ID",
    "CREATED_TIME", "BEGINDATE",
]
# Itens genéricos de SPA (Smart Process), ex.: reuniões, processamento de pedido,
# pós-vendas, diárias. Cada tenant define quais SPAs acompanha no FIELD_MAP.
SPA_COLS = [
    "ID", "TITLE", "STAGE_ID", "CATEGORY_ID", "ASSIGNED_BY_ID", "SOURCE_ID",
    "CREATED_TIME", "BEGINDATE", "OPPORTUNITY",
]
# Linhas de produto dos negócios (crm.deal.productrows)
PRODUCT_COLS = ["ID", "DEAL_ID", "PRODUCT_ID", "PRODUCT_NAME", "PRICE", "QUANTITY", "TOTAL"]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with get_conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                NAME TEXT UNIQUE NOT NULL,
                WEBHOOK TEXT NOT NULL,
                SALES_CATEGORY_ID TEXT NOT NULL DEFAULT '16',
                FIELD_MAP TEXT,
                ACTIVE INTEGER NOT NULL DEFAULT 1,
                CREATED_AT TEXT
            );

            CREATE TABLE IF NOT EXISTS app_users (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                USERNAME TEXT UNIQUE NOT NULL,
                PASSWORD_HASH TEXT NOT NULL,
                SALT TEXT NOT NULL,
                ROLE TEXT NOT NULL DEFAULT 'client',   -- 'master' | 'client'
                TENANT_ID INTEGER,
                NAME TEXT,
                ACTIVE INTEGER NOT NULL DEFAULT 1,
                CREATED_AT TEXT
            );

            CREATE TABLE IF NOT EXISTS deals (
                TENANT_ID INTEGER NOT NULL,
                ID TEXT NOT NULL,
                TITLE TEXT, STAGE_ID TEXT, CATEGORY_ID TEXT,
                OPPORTUNITY REAL, CURRENCY_ID TEXT, ASSIGNED_BY_ID TEXT,
                SOURCE_ID TEXT, DATE_CREATE TEXT, DATE_MODIFY TEXT,
                CLOSEDATE TEXT, CLOSED TEXT,
                PRIMARY KEY (TENANT_ID, ID)
            );

            CREATE TABLE IF NOT EXISTS leads (
                TENANT_ID INTEGER NOT NULL,
                ID TEXT NOT NULL,
                TITLE TEXT, STATUS_ID TEXT, STATUS_SEMANTIC_ID TEXT,
                OPPORTUNITY REAL, ASSIGNED_BY_ID TEXT, SOURCE_ID TEXT,
                DATE_CREATE TEXT, DATE_MODIFY TEXT,
                SEGMENTO TEXT, CARGO TEXT, MOTIVO TEXT,
                PRIMARY KEY (TENANT_ID, ID)
            );

            CREATE TABLE IF NOT EXISTS meetings (
                TENANT_ID INTEGER NOT NULL,
                ID TEXT NOT NULL,
                TITLE TEXT, STAGE_ID TEXT, CATEGORY_ID TEXT,
                ASSIGNED_BY_ID TEXT, SOURCE_ID TEXT,
                CREATED_TIME TEXT, BEGINDATE TEXT,
                PRIMARY KEY (TENANT_ID, ID)
            );

            CREATE TABLE IF NOT EXISTS spa_items (
                TENANT_ID INTEGER NOT NULL,
                ENTITY_TYPE_ID INTEGER NOT NULL,
                ID TEXT NOT NULL,
                TITLE TEXT, STAGE_ID TEXT, CATEGORY_ID TEXT,
                ASSIGNED_BY_ID TEXT, SOURCE_ID TEXT,
                CREATED_TIME TEXT, BEGINDATE TEXT, OPPORTUNITY REAL,
                PRIMARY KEY (TENANT_ID, ENTITY_TYPE_ID, ID)
            );

            CREATE TABLE IF NOT EXISTS products (
                TENANT_ID INTEGER NOT NULL,
                ID TEXT NOT NULL,
                DEAL_ID TEXT, PRODUCT_ID TEXT, PRODUCT_NAME TEXT,
                PRICE REAL, QUANTITY REAL, TOTAL REAL,
                PRIMARY KEY (TENANT_ID, ID)
            );
            CREATE INDEX IF NOT EXISTS idx_products_deal ON products (TENANT_ID, DEAL_ID);

            CREATE TABLE IF NOT EXISTS tenant_meta (
                TENANT_ID INTEGER PRIMARY KEY,
                STATUS_MAP TEXT, USER_MAP TEXT, CATEGORIES TEXT, UPDATED_AT TEXT
            );

            CREATE TABLE IF NOT EXISTS sync_log (
                TENANT_ID INTEGER PRIMARY KEY,
                LAST_RUN TEXT, DEALS_COUNT INTEGER, LEADS_COUNT INTEGER, NOTE TEXT
            );

            CREATE TABLE IF NOT EXISTS quotas (
                TENANT_ID INTEGER NOT NULL,
                ASSIGNED_BY_ID TEXT NOT NULL,
                YEAR INTEGER NOT NULL,
                MONTH INTEGER NOT NULL,
                TARGET_VALUE REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (TENANT_ID, ASSIGNED_BY_ID, YEAR, MONTH)
            );
            """
        )
        # migrações idempotentes para bancos já existentes
        _ensure_col(c, "deals", "SEGMENTO", "TEXT")
        _ensure_col(c, "deals", "MOTIVO", "TEXT")
        _ensure_col(c, "leads", "SEGMENTO", "TEXT")
        _ensure_col(c, "leads", "CARGO", "TEXT")
        _ensure_col(c, "leads", "MOTIVO", "TEXT")
        _ensure_col(c, "tenants", "FIELD_MAP", "TEXT")


def _ensure_col(conn, table: str, col: str, decl: str) -> None:
    existing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if col not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


# ---------------------------------------------------------------- tenants
def add_tenant(name: str, webhook: str, category_id: str = "16") -> int:
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO tenants (NAME, WEBHOOK, SALES_CATEGORY_ID, ACTIVE, CREATED_AT)"
            " VALUES (?,?,?,1,?)",
            (name, webhook, category_id, datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def update_tenant(tenant_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with get_conn() as c:
        c.execute(f"UPDATE tenants SET {cols} WHERE ID=?", (*fields.values(), tenant_id))


def list_tenants(active_only: bool = True) -> List[Dict[str, Any]]:
    q = "SELECT * FROM tenants"
    if active_only:
        q += " WHERE ACTIVE=1"
    q += " ORDER BY NAME"
    with get_conn() as c:
        return [dict(r) for r in c.execute(q)]


def get_tenant(tenant_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as c:
        r = c.execute("SELECT * FROM tenants WHERE ID=?", (tenant_id,)).fetchone()
        return dict(r) if r else None


def get_field_map(tenant_id: int) -> Dict[str, Any]:
    t = get_tenant(tenant_id)
    if t and t.get("FIELD_MAP"):
        try:
            return json.loads(t["FIELD_MAP"])
        except (ValueError, TypeError):
            return {}
    return {}


def set_field_map(tenant_id: int, field_map: Dict[str, Any]) -> None:
    with get_conn() as c:
        c.execute("UPDATE tenants SET FIELD_MAP=? WHERE ID=?",
                  (json.dumps(field_map, ensure_ascii=False), tenant_id))


# ---------------------------------------------------------------- users
def add_user(username: str, password_hash: str, salt: str, role: str,
             tenant_id: Optional[int], name: str = "") -> int:
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO app_users (USERNAME, PASSWORD_HASH, SALT, ROLE, TENANT_ID, NAME,"
            " ACTIVE, CREATED_AT) VALUES (?,?,?,?,?,?,1,?)",
            (username, password_hash, salt, role, tenant_id, name,
             datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def get_user(username: str) -> Optional[Dict[str, Any]]:
    with get_conn() as c:
        r = c.execute("SELECT * FROM app_users WHERE USERNAME=?", (username,)).fetchone()
        return dict(r) if r else None


def list_users() -> List[Dict[str, Any]]:
    with get_conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM app_users ORDER BY USERNAME")]


def set_user_password(username: str, password_hash: str, salt: str) -> None:
    with get_conn() as c:
        c.execute("UPDATE app_users SET PASSWORD_HASH=?, SALT=? WHERE USERNAME=?",
                  (password_hash, salt, username))


def set_user_active(username: str, active: bool) -> None:
    with get_conn() as c:
        c.execute("UPDATE app_users SET ACTIVE=? WHERE USERNAME=?", (1 if active else 0, username))


# ---------------------------------------------------------------- meta
def save_meta(tenant_id: int, status_map: dict, user_map: dict, categories: dict) -> None:
    with get_conn() as c:
        c.execute(
            "INSERT INTO tenant_meta (TENANT_ID, STATUS_MAP, USER_MAP, CATEGORIES, UPDATED_AT)"
            " VALUES (?,?,?,?,?) ON CONFLICT(TENANT_ID) DO UPDATE SET"
            " STATUS_MAP=excluded.STATUS_MAP, USER_MAP=excluded.USER_MAP,"
            " CATEGORIES=excluded.CATEGORIES, UPDATED_AT=excluded.UPDATED_AT",
            (tenant_id, json.dumps(status_map), json.dumps(user_map),
             json.dumps(categories), datetime.now().isoformat(timespec="seconds")),
        )


def get_meta(tenant_id: int) -> Dict[str, Any]:
    with get_conn() as c:
        r = c.execute("SELECT * FROM tenant_meta WHERE TENANT_ID=?", (tenant_id,)).fetchone()
    if not r:
        return {"status_map": {}, "user_map": {}, "categories": {}, "updated_at": None}
    return {
        "status_map": json.loads(r["STATUS_MAP"] or "{}"),
        "user_map": json.loads(r["USER_MAP"] or "{}"),
        "categories": json.loads(r["CATEGORIES"] or "{}"),
        "updated_at": r["UPDATED_AT"],
    }


# ---------------------------------------------------------------- upserts
def _upsert(table: str, cols: List[str], tenant_id: int, rows: List[dict]) -> int:
    if not rows:
        return 0
    placeholders = ",".join(["?"] * (len(cols) + 1))
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "ID")
    sql = (
        f"INSERT INTO {table} (TENANT_ID,{','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(TENANT_ID,ID) DO UPDATE SET {updates}"
    )
    data = []
    for r in rows:
        vals = [tenant_id]
        for col in cols:
            v = r.get(col)
            if col == "OPPORTUNITY":
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    v = 0.0
            vals.append(v)
        data.append(vals)
    with get_conn() as c:
        c.executemany(sql, data)
    return len(data)


def upsert_deals(tenant_id: int, rows: List[dict]) -> int:
    return _upsert("deals", DEAL_COLS, tenant_id, rows)


def upsert_leads(tenant_id: int, rows: List[dict]) -> int:
    return _upsert("leads", LEAD_COLS, tenant_id, rows)


def upsert_meetings(tenant_id: int, rows: List[dict]) -> int:
    return _upsert("meetings", MEETING_COLS, tenant_id, rows)


def upsert_spa_items(tenant_id: int, entity_type_id: int, rows: List[dict]) -> int:
    if not rows:
        return 0
    cols = SPA_COLS
    placeholders = ",".join(["?"] * (len(cols) + 2))  # + TENANT_ID + ENTITY_TYPE_ID
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "ID")
    sql = (
        f"INSERT INTO spa_items (TENANT_ID,ENTITY_TYPE_ID,{','.join(cols)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(TENANT_ID,ENTITY_TYPE_ID,ID) DO UPDATE SET {updates}"
    )
    data = []
    for r in rows:
        vals = [tenant_id, entity_type_id]
        for col in cols:
            v = r.get(col)
            if col == "OPPORTUNITY":
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    v = 0.0
            vals.append(v)
        data.append(vals)
    with get_conn() as c:
        c.executemany(sql, data)
    return len(data)


def spa_items_df(tenant_id: int, entity_type_id: int) -> pd.DataFrame:
    with get_conn() as c:
        return pd.read_sql_query(
            f"SELECT {','.join(SPA_COLS)} FROM spa_items WHERE TENANT_ID=? AND ENTITY_TYPE_ID=?",
            c, params=(tenant_id, entity_type_id),
        )


def clear_tenant_data(tenant_id: int) -> None:
    """Apaga os dados sincronizados de um tenant (deals/leads/SPAs/produtos),
    para uma recarga limpa quando registros foram removidos no Bitrix."""
    with get_conn() as c:
        for table in ("deals", "leads", "spa_items", "products"):
            c.execute(f"DELETE FROM {table} WHERE TENANT_ID=?", (tenant_id,))


def deal_ids(tenant_id: int) -> List[str]:
    with get_conn() as c:
        return [r[0] for r in c.execute("SELECT ID FROM deals WHERE TENANT_ID=?", (tenant_id,))]


def replace_products(tenant_id: int, deal_ids_list: List[str], rows: List[dict]) -> int:
    """Substitui as linhas de produto dos negócios informados (apaga as antigas
    desses negócios e insere as novas), mantendo consistência por negócio."""
    with get_conn() as c:
        if deal_ids_list:
            c.executemany("DELETE FROM products WHERE TENANT_ID=? AND DEAL_ID=?",
                          [(tenant_id, str(d)) for d in deal_ids_list])
        if rows:
            cols = PRODUCT_COLS
            ph = ",".join(["?"] * (len(cols) + 1))
            c.executemany(
                f"INSERT OR REPLACE INTO products (TENANT_ID,{','.join(cols)}) VALUES ({ph})",
                [[tenant_id] + [r.get(col) for col in cols] for r in rows],
            )
    return len(rows)


def products_df(tenant_id: int) -> pd.DataFrame:
    with get_conn() as c:
        return pd.read_sql_query(
            f"SELECT {','.join(PRODUCT_COLS)} FROM products WHERE TENANT_ID=?", c, params=(tenant_id,)
        )


def count_records(tenant_id: int) -> Dict[str, int]:
    with get_conn() as c:
        d = c.execute("SELECT COUNT(*) FROM deals WHERE TENANT_ID=?", (tenant_id,)).fetchone()[0]
        l = c.execute("SELECT COUNT(*) FROM leads WHERE TENANT_ID=?", (tenant_id,)).fetchone()[0]
        s = c.execute("SELECT COUNT(*) FROM spa_items WHERE TENANT_ID=?", (tenant_id,)).fetchone()[0]
    return {"deals": d, "leads": l, "spa": s}


def deals_df(tenant_id: int) -> pd.DataFrame:
    with get_conn() as c:
        return pd.read_sql_query(
            f"SELECT {','.join(DEAL_COLS)} FROM deals WHERE TENANT_ID=?", c, params=(tenant_id,)
        )


def leads_df(tenant_id: int) -> pd.DataFrame:
    with get_conn() as c:
        return pd.read_sql_query(
            f"SELECT {','.join(LEAD_COLS)} FROM leads WHERE TENANT_ID=?", c, params=(tenant_id,)
        )


def meetings_df(tenant_id: int) -> pd.DataFrame:
    with get_conn() as c:
        return pd.read_sql_query(
            f"SELECT {','.join(MEETING_COLS)} FROM meetings WHERE TENANT_ID=?", c, params=(tenant_id,)
        )


# ---------------------------------------------------------------- sync log
def set_sync(tenant_id: int, deals_count: int, leads_count: int, note: str = "") -> None:
    with get_conn() as c:
        c.execute(
            "INSERT INTO sync_log (TENANT_ID, LAST_RUN, DEALS_COUNT, LEADS_COUNT, NOTE)"
            " VALUES (?,?,?,?,?) ON CONFLICT(TENANT_ID) DO UPDATE SET"
            " LAST_RUN=excluded.LAST_RUN, DEALS_COUNT=excluded.DEALS_COUNT,"
            " LEADS_COUNT=excluded.LEADS_COUNT, NOTE=excluded.NOTE",
            (tenant_id, datetime.now().isoformat(timespec="seconds"),
             deals_count, leads_count, note),
        )


def get_sync(tenant_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as c:
        r = c.execute("SELECT * FROM sync_log WHERE TENANT_ID=?", (tenant_id,)).fetchone()
        return dict(r) if r else None


# ---------------------------------------------------------------- quotas
def set_quota(tenant_id: int, assigned_by_id: str, year: int, month: int, value: float) -> None:
    with get_conn() as c:
        c.execute(
            "INSERT INTO quotas (TENANT_ID, ASSIGNED_BY_ID, YEAR, MONTH, TARGET_VALUE)"
            " VALUES (?,?,?,?,?) ON CONFLICT(TENANT_ID,ASSIGNED_BY_ID,YEAR,MONTH)"
            " DO UPDATE SET TARGET_VALUE=excluded.TARGET_VALUE",
            (tenant_id, str(assigned_by_id), int(year), int(month), float(value)),
        )


def quotas_df(tenant_id: int, year: Optional[int] = None) -> pd.DataFrame:
    q = "SELECT ASSIGNED_BY_ID, YEAR, MONTH, TARGET_VALUE FROM quotas WHERE TENANT_ID=?"
    params: list = [tenant_id]
    if year is not None:
        q += " AND YEAR=?"
        params.append(year)
    with get_conn() as c:
        return pd.read_sql_query(q, c, params=tuple(params))
