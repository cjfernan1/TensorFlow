"""Almacenamiento local en SQLite."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from stores import ProductSnapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    query        TEXT NOT NULL,          -- texto tal como está en la hoja
    store        TEXT NOT NULL,          -- exito | jumbo
    status       TEXT NOT NULL,          -- auto | confirmado | pendiente | no_existe
    sku          TEXT,
    ean          TEXT,
    name         TEXT,
    brand        TEXT,
    url          TEXT,
    unit_qty     REAL,
    unit         TEXT,
    method       TEXT,
    score        REAL,
    base_price   REAL,                   -- precio de la primera consulta
    base_ppu     REAL,
    base_date    TEXT,
    updated_at   TEXT,
    PRIMARY KEY (query, store)
);
CREATE TABLE IF NOT EXISTS price_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    query          TEXT NOT NULL,
    store          TEXT NOT NULL,
    sku            TEXT,
    ean            TEXT,
    name           TEXT,
    brand          TEXT,
    url            TEXT,
    price          REAL,
    list_price     REAL,
    available      INTEGER,
    unit_qty       REAL,
    unit           TEXT,
    price_per_unit REAL,
    ts             TEXT NOT NULL,
    regular_price  REAL,
    price_label    TEXT
);
CREATE INDEX IF NOT EXISTS ix_hist ON price_history(query, store, ts);
CREATE TABLE IF NOT EXISTS alerts_sent (
    key   TEXT PRIMARY KEY,
    ts    TEXT NOT NULL,
    text  TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str = "precios.db"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(price_history)")}
        for col, typ in (("regular_price", "REAL"), ("price_label", "TEXT")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE price_history ADD COLUMN {col} {typ}")
        self.db.commit()

    def close(self):
        self.db.close()

    # ---- matches -----------------------------------------------------------
    def get_matches(self, query: str) -> dict[str, sqlite3.Row]:
        rows = self.db.execute("SELECT * FROM matches WHERE query=?", (query,)).fetchall()
        return {r["store"]: r for r in rows}

    def all_matches(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM matches ORDER BY query, store").fetchall()

    def save_match(self, query: str, store: str, status: str,
                   snap: Optional[ProductSnapshot] = None, method: str = "",
                   score: float | None = None):
        prev = self.get_matches(query).get(store)
        same_sku = prev is not None and snap is not None and prev["sku"] == snap.sku
        base = (prev["base_price"], prev["base_ppu"], prev["base_date"]) if same_sku else (None, None, None)
        self.db.execute(
            """INSERT OR REPLACE INTO matches
               (query, store, status, sku, ean, name, brand, url, unit_qty, unit, method, score,
                base_price, base_ppu, base_date, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (query, store, status,
             snap.sku if snap else None, snap.ean if snap else None,
             snap.name if snap else None, snap.brand if snap else None,
             snap.url if snap else None, snap.unit_qty if snap else None,
             snap.unit if snap else None, method, score, *base, _now()))
        self.db.commit()

    def set_base_price(self, query: str, store: str, snap: ProductSnapshot) -> bool:
        """Fija el precio base si no existe. Devuelve True si se fijó ahora."""
        cur = self.db.execute(
            """UPDATE matches SET base_price=?, base_ppu=?, base_date=?
               WHERE query=? AND store=? AND base_price IS NULL""",
            (snap.price, snap.price_per_unit, snap.timestamp, query, store))
        self.db.commit()
        return cur.rowcount > 0

    def delete_matches(self, query: str):
        self.db.execute("DELETE FROM matches WHERE query=?", (query,))
        self.db.commit()

    # ---- historial ---------------------------------------------------------
    def add_price(self, query: str, snap: ProductSnapshot):
        self.db.execute(
            """INSERT INTO price_history (query, store, sku, ean, name, brand, url, price,
               list_price, available, unit_qty, unit, price_per_unit, ts, regular_price, price_label)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (query, snap.store, snap.sku, snap.ean, snap.name, snap.brand, snap.url,
             snap.price, snap.list_price, int(snap.available), snap.unit_qty, snap.unit,
             snap.price_per_unit, snap.timestamp, snap.regular_price, snap.price_label))
        self.db.commit()

    def last_price(self, query: str, store: str, sku: str) -> Optional[sqlite3.Row]:
        return self.db.execute(
            """SELECT * FROM price_history WHERE query=? AND store=? AND sku=?
               ORDER BY ts DESC, id DESC LIMIT 1""", (query, store, sku)).fetchone()

    def stats(self, query: str, store: str, sku: str, days: int = 30) -> dict:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        row = self.db.execute(
            """SELECT AVG(price) avg, MIN(price) min, COUNT(*) n FROM price_history
               WHERE query=? AND store=? AND sku=? AND ts>=? AND price IS NOT NULL""",
            (query, store, sku, since)).fetchone()
        hist_min = self.db.execute(
            """SELECT MIN(price) FROM price_history WHERE query=? AND store=? AND sku=?
               AND price IS NOT NULL""", (query, store, sku)).fetchone()[0]
        return {"avg": row["avg"], "min30": row["min"], "n": row["n"], "min_all": hist_min}

    # ---- alertas -----------------------------------------------------------
    def alert_recent(self, key: str, cooldown_hours: float) -> bool:
        row = self.db.execute("SELECT ts FROM alerts_sent WHERE key=?", (key,)).fetchone()
        if not row:
            return False
        return datetime.fromisoformat(row["ts"]) > datetime.now() - timedelta(hours=cooldown_hours)

    def mark_alert(self, key: str, text: str):
        self.db.execute("INSERT OR REPLACE INTO alerts_sent (key, ts, text) VALUES (?,?,?)",
                        (key, _now(), text))
        self.db.commit()
