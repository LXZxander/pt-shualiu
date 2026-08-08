"""SQLite 状态：种子登记、去重历史、限速状态、每日统计。"""
import sqlite3
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS torrents (
    hash TEXT PRIMARY KEY,
    site TEXT NOT NULL,
    category TEXT,
    name TEXT,
    added_at REAL,
    completion_on INTEGER DEFAULT 0,
    last_uploaded INTEGER DEFAULT 0,
    last_upload_change REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS history (
    site TEXT, site_id INTEGER, PRIMARY KEY (site, site_id)
);
CREATE TABLE IF NOT EXISTS ratelimit (
    direction TEXT PRIMARY KEY,
    mode TEXT DEFAULT 'high',
    limit_bytes INTEGER,
    cap_minutes INTEGER DEFAULT 0,
    low_remaining_min INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS daily_stats (
    d TEXT, site TEXT, action TEXT, n INTEGER DEFAULT 0,
    PRIMARY KEY (d, site, action)
);
"""

class State:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---- torrents ----
    def upsert_torrent(self, hash_, site, category, name, added_at):
        self._conn.execute(
            "INSERT OR REPLACE INTO torrents(hash,site,category,name,added_at) VALUES(?,?,?,?,?)",
            (hash_, site, category, name, added_at))
        self._conn.commit()

    def update_torrent(self, hash_, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(f"UPDATE torrents SET {cols} WHERE hash=?", (*fields.values(), hash_))
        self._conn.commit()

    def get_torrent(self, hash_):
        r = self._conn.execute("SELECT * FROM torrents WHERE hash=?", (hash_,)).fetchone()
        if not r:
            return None
        cols = [d[0] for d in self._conn.execute("SELECT * FROM torrents").description]
        return dict(zip(cols, r))

    def all_torrents(self):
        cols = [d[0] for d in self._conn.execute("SELECT * FROM torrents").description]
        return [dict(zip(cols, r)) for r in self._conn.execute("SELECT * FROM torrents").fetchall()]

    def remove_torrent(self, hash_):
        self._conn.execute("DELETE FROM torrents WHERE hash=?", (hash_,))
        self._conn.commit()

    # ---- history（站点种子 id 去重） ----
    def add_history(self, site, site_id):
        self._conn.execute("INSERT OR IGNORE INTO history(site,site_id) VALUES(?,?)", (site, site_id))
        self._conn.commit()

    def is_history(self, site, site_id):
        return self._conn.execute(
            "SELECT 1 FROM history WHERE site=? AND site_id=?", (site, site_id)).fetchone() is not None

    def history_ids(self, site):
        return {r[0] for r in self._conn.execute(
            "SELECT site_id FROM history WHERE site=?", (site,)).fetchall()}

    # ---- ratelimit ----
    def get_rate_state(self, direction):
        r = self._conn.execute("SELECT * FROM ratelimit WHERE direction=?", (direction,)).fetchone()
        if not r:
            return {"mode": "high", "limit_bytes": None, "cap_minutes": 0, "low_remaining_min": 0}
        cols = [d[0] for d in self._conn.execute("SELECT * FROM ratelimit").description]
        return dict(zip(cols, r))

    def set_rate_state(self, direction, mode, limit_bytes, cap_minutes, low_remaining_min):
        self._conn.execute(
            "INSERT OR REPLACE INTO ratelimit(direction,mode,limit_bytes,cap_minutes,low_remaining_min)"
            " VALUES(?,?,?,?,?)",
            (direction, mode, limit_bytes, cap_minutes, low_remaining_min))
        self._conn.commit()

    # ---- daily_stats（计数累计） ----
    def incr_stat(self, site, action, n=1):
        d = date.today().isoformat()
        self._conn.execute(
            "INSERT INTO daily_stats(d,site,action,n) VALUES(?,?,?,?) "
            "ON CONFLICT(d,site,action) DO UPDATE SET n=n+?",
            (d, site, action, n, n))
        self._conn.commit()

    def stat(self, site, action):
        d = date.today().isoformat()
        r = self._conn.execute("SELECT n FROM daily_stats WHERE d=? AND site=? AND action=?",
                               (d, site, action)).fetchone()
        return r[0] if r else 0

    def close(self):
        self._conn.close()
