import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, Generator, List, Optional, Sequence, Tuple

try:
    import pymysql
    import pymysql.cursors
    PYMYSQL_AVAILABLE = True
except ImportError:
    pymysql = None  # type: ignore
    PYMYSQL_AVAILABLE = False

from config import config

DB_PATH = config.db_path

# Standard TikTok Umrechnungswerte:
# 1 Coin = 1 Diamond (im TikTokLive GiftEvent entspricht gift.diamond_count dem Coin-Wert)
# Creator erhält ca. 50% -> 1 Diamond = $0.005 USD Netto-Auszahlung ($5.00 pro 1.000 Coins/Diamonds)
DEFAULT_SETTINGS: Dict[str, str] = {
    "usd_per_diamond": "0.005",        # Netto-Auszahlung an Streamer pro Coin/Diamond in USD
    "gross_usd_per_coin": "0.0106",    # Brutto-Kaufpreis für Zuschauer pro Coin in USD
    "usd_to_eur_rate": "0.92",         # Wechselkurs USD -> EUR
    "currency_primary": "EUR",         # Primäre Anzeige-Währung (EUR oder USD)
    "poll_interval_seconds": "45",     # Intervall für Auto-Live-Check wenn Streamer offline ist
    "api_key": "",                     # Optionaler API-Key für externe Bot-Abfragen
    "telegram_access_mode": config.telegram_access_mode if config.telegram_access_mode in ("whitelist", "public") else "whitelist",
}

_db_init_lock = threading.Lock()
_db_initialized: bool = False


class DBRow(dict):
    """
    Kompatibilitäts-Zeilenobjekt für MySQL/MariaDB, das sich exakt wie `sqlite3.Row` verhält:
    - Unterstützt Dictionary-Zugriff (`row["username"]`, `dict(row)`, `row.get(...)`)
    - Unterstützt numerischen Index-Zugriff (`row[0]` z.B. bei `SELECT COUNT(*) ... fetchone()[0]`)
    """

    def __init__(self, data: Dict[str, Any]) -> None:
        super().__init__(data)
        self._values = list(data.values())

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class MySQLCursorWrapper:
    """Wrapper um den PyMySQL DictCursor mit `sqlite3.Cursor`-kompatibler Schnittstelle."""

    def __init__(self, raw_cursor: Any) -> None:
        self._cur = raw_cursor

    @property
    def lastrowid(self) -> int:
        return int(self._cur.lastrowid or 0)

    @property
    def rowcount(self) -> int:
        return int(self._cur.rowcount or 0)

    def fetchone(self) -> Optional[DBRow]:
        row = self._cur.fetchone()
        return DBRow(row) if row is not None else None

    def fetchall(self) -> List[DBRow]:
        rows = self._cur.fetchall()
        return [DBRow(r) for r in rows] if rows else []


class MySQLConnectionWrapper:
    """
    Übersetzt die SQLite-Queries aus `database.py` automatisch für MySQL / MariaDB:
    - `?` -> `%s`
    - `INSERT OR IGNORE INTO` -> `INSERT IGNORE INTO`
    - `INSERT OR REPLACE INTO` -> `REPLACE INTO`
    - `MAX(a, b)` (2 Argumente) -> `GREATEST(a, b)`
    """

    def __init__(self, raw_conn: Any) -> None:
        self._conn = raw_conn

    @staticmethod
    def _translate_sql(sql: str) -> str:
        out = sql.replace("?", "%s")
        out = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT IGNORE INTO", out, flags=re.IGNORECASE)
        out = re.sub(r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", "REPLACE INTO", out, flags=re.IGNORECASE)
        out = re.sub(r"\bMAX\s*\(\s*last_fan_tickets\s*,\s*%s\s*\)", "GREATEST(last_fan_tickets, %s)", out, flags=re.IGNORECASE)
        return out

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> MySQLCursorWrapper:
        translated = self._translate_sql(sql)
        cur = self._conn.cursor()
        cur.execute(translated, tuple(params) if params is not None else None)
        return MySQLCursorWrapper(cur)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


def _open_mysql_connection(include_db: bool = True) -> Any:
    if not PYMYSQL_AVAILABLE:
        raise RuntimeError(
            "Für DB_ENGINE=mysql wird das Paket 'pymysql' benötigt (pip install pymysql)."
        )
    conn = pymysql.connect(
        host=config.mysql_host,
        port=config.mysql_port,
        user=config.mysql_user,
        password=config.mysql_password,
        database=config.mysql_database if include_db else None,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=10,
    )
    with conn.cursor() as cur:
        cur.execute("SET SESSION sql_mode = (SELECT REPLACE(@@sql_mode, 'ONLY_FULL_GROUP_BY', ''))")
    return conn


@contextmanager
def get_db() -> Generator[Any, None, None]:
    """
    Universeller Datenbank-Context-Manager für `toktracker`:
    - Standard (`DB_ENGINE=sqlite`): Lokale SQLite-Datenbank im schnellen WAL-Modus.
    - Externer SQL-Server (`DB_ENGINE=mysql`): MySQL / MariaDB über PyMySQL.
    """
    if config.is_mysql:
        raw_conn = _open_mysql_connection(include_db=True)
        wrapper = MySQLConnectionWrapper(raw_conn)
        try:
            yield wrapper
            wrapper.commit()
        except Exception:
            wrapper.rollback()
            raise
        finally:
            wrapper.close()
    else:
        conn = sqlite3.connect(DB_PATH, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _ensure_column_sqlite(conn: sqlite3.Connection, table: str, column: str, col_def: str) -> None:
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def _ensure_column_mysql(conn: MySQLConnectionWrapper, table: str, column: str, col_def: str) -> None:
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = ?
        """,
        (config.mysql_database, table, column),
    ).fetchone()
    if not row or int(row[0]) == 0:
        conn.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {col_def}")


def _ensure_index_mysql(conn: MySQLConnectionWrapper, table: str, index_name: str, columns_sql: str, unique: bool = False) -> None:
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND INDEX_NAME = ?
        """,
        (config.mysql_database, table, index_name),
    ).fetchone()
    if not row or int(row[0]) == 0:
        uniq_kw = "UNIQUE " if unique else ""
        conn.execute(f"CREATE {uniq_kw}INDEX {index_name} ON {table}({columns_sql})")


def _init_mysql_schema() -> None:
    # 1. Datenbank anlegen, falls sie noch nicht existiert
    try:
        bootstrap_conn = _open_mysql_connection(include_db=False)
        try:
            with bootstrap_conn.cursor() as cur:
                db_name = re.sub(r"[^a-zA-Z0-9_]", "", config.mysql_database) or "toktracker"
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            bootstrap_conn.commit()
        finally:
            bootstrap_conn.close()
    except Exception:
        # Falls der MySQL-User keine CREATE DATABASE Rechte hat und die DB bereits existiert, normal fortfahren
        pass

    raw_conn = _open_mysql_connection(include_db=True)
    conn = MySQLConnectionWrapper(raw_conn)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                `key` VARCHAR(128) PRIMARY KEY,
                `value` TEXT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS streamers (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(191) NOT NULL UNIQUE,
                display_name VARCHAR(255),
                avatar_url TEXT,
                room_id VARCHAR(128),
                follower_count BIGINT DEFAULT 0,
                is_live TINYINT DEFAULT 0,
                is_tracking TINYINT DEFAULT 0,
                auto_monitor TINYINT DEFAULT 0,
                current_viewers INT DEFAULT 0,
                last_error TEXT,
                created_at VARCHAR(64) NOT NULL,
                last_seen_at VARCHAR(64)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stream_sessions (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                streamer_id BIGINT NOT NULL,
                room_id VARCHAR(128),
                title VARCHAR(512) DEFAULT '',
                started_at VARCHAR(64) NOT NULL,
                ended_at VARCHAR(64),
                peak_viewers INT DEFAULT 0,
                last_fan_tickets BIGINT DEFAULT 0,
                status VARCHAR(32) DEFAULT 'active',
                CONSTRAINT fk_sessions_streamer FOREIGN KEY (streamer_id) REFERENCES streamers(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gift_events (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                streamer_id BIGINT NOT NULL,
                session_id BIGINT NULL,
                event_uid VARCHAR(191) UNIQUE,
                sender_user_id VARCHAR(128) DEFAULT '',
                sender_username VARCHAR(191) NOT NULL,
                sender_nickname VARCHAR(255),
                is_super_fan TINYINT DEFAULT 0,
                gift_id INT DEFAULT 0,
                gift_name VARCHAR(255) NOT NULL,
                gift_icon_url TEXT,
                diamond_count INT NOT NULL,
                repeat_count INT NOT NULL DEFAULT 1,
                combo_count INT DEFAULT 1,
                group_id VARCHAR(128) DEFAULT '',
                room_fan_tickets BIGINT DEFAULT 0,
                total_coins BIGINT NOT NULL,
                usd_net DOUBLE NOT NULL,
                eur_net DOUBLE NOT NULL,
                usd_gross DOUBLE NOT NULL,
                eur_gross DOUBLE NOT NULL,
                source VARCHAR(64) DEFAULT 'tiktoklive',
                created_at VARCHAR(64) NOT NULL,
                CONSTRAINT fk_gifts_streamer FOREIGN KEY (streamer_id) REFERENCES streamers(id) ON DELETE CASCADE,
                CONSTRAINT fk_gifts_session FOREIGN KEY (session_id) REFERENCES stream_sessions(id) ON DELETE SET NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_users (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                telegram_id BIGINT UNIQUE,
                username VARCHAR(191),
                first_name VARCHAR(255) DEFAULT '',
                role VARCHAR(32) DEFAULT 'user',
                is_allowed TINYINT DEFAULT 0,
                note VARCHAR(512) DEFAULT '',
                created_at VARCHAR(64) NOT NULL,
                last_seen_at VARCHAR(64)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tiktok_battles (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                streamer_id BIGINT NOT NULL,
                session_id BIGINT NULL,
                battle_id VARCHAR(128) NOT NULL,
                opponent_user_id VARCHAR(128) DEFAULT '',
                opponent_username VARCHAR(191) DEFAULT '',
                opponent_display_name VARCHAR(255) DEFAULT '',
                opponent_avatar_url TEXT,
                started_at VARCHAR(64) NOT NULL,
                ended_at VARCHAR(64),
                duration_seconds INT DEFAULT 300,
                streamer_score INT DEFAULT 0,
                opponent_score INT DEFAULT 0,
                result VARCHAR(32) DEFAULT 'ONGOING',
                streamer_mvp_name VARCHAR(255) DEFAULT '',
                streamer_mvp_score INT DEFAULT 0,
                streamer_mvp_avatar TEXT,
                opponent_mvp_name VARCHAR(255) DEFAULT '',
                opponent_mvp_score INT DEFAULT 0,
                opponent_mvp_avatar TEXT,
                coins_earned BIGINT DEFAULT 0,
                usd_net DOUBLE DEFAULT 0.0,
                eur_net DOUBLE DEFAULT 0.0,
                usd_gross DOUBLE DEFAULT 0.0,
                eur_gross DOUBLE DEFAULT 0.0,
                status VARCHAR(32) DEFAULT 'active',
                raw_data_json MEDIUMTEXT,
                created_at VARCHAR(64) NOT NULL,
                CONSTRAINT fk_battles_streamer FOREIGN KEY (streamer_id) REFERENCES streamers(id) ON DELETE CASCADE,
                CONSTRAINT fk_battles_session FOREIGN KEY (session_id) REFERENCES stream_sessions(id) ON DELETE SET NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )

        _ensure_index_mysql(conn, "gift_events", "idx_gift_events_streamer_date", "streamer_id, created_at")
        _ensure_index_mysql(conn, "gift_events", "idx_gift_events_session", "session_id")
        _ensure_index_mysql(conn, "stream_sessions", "idx_stream_sessions_streamer", "streamer_id, started_at")
        _ensure_index_mysql(conn, "telegram_users", "idx_telegram_users_username", "username")
        _ensure_index_mysql(conn, "tiktok_battles", "idx_battles_streamer", "streamer_id, created_at")
        _ensure_index_mysql(conn, "tiktok_battles", "idx_battles_battle_id", "battle_id")
        _ensure_index_mysql(conn, "tiktok_battles", "idx_battles_result", "result")
        _ensure_index_mysql(conn, "tiktok_battles", "idx_battles_opponent", "opponent_username")

        _ensure_column_mysql(conn, "tiktok_battles", "battle_mode", "VARCHAR(32) DEFAULT '1v1'")
        _ensure_column_mysql(conn, "tiktok_battles", "teammate_user_id", "VARCHAR(128) DEFAULT ''")
        _ensure_column_mysql(conn, "tiktok_battles", "teammate_username", "VARCHAR(191) DEFAULT ''")
        _ensure_column_mysql(conn, "tiktok_battles", "teammate_display_name", "VARCHAR(255) DEFAULT ''")
        _ensure_column_mysql(conn, "tiktok_battles", "teammate_avatar_url", "TEXT")
        _ensure_column_mysql(conn, "tiktok_battles", "teammate_score", "BIGINT DEFAULT 0")
        _ensure_column_mysql(conn, "tiktok_battles", "opponent2_user_id", "VARCHAR(128) DEFAULT ''")
        _ensure_column_mysql(conn, "tiktok_battles", "opponent2_username", "VARCHAR(191) DEFAULT ''")
        _ensure_column_mysql(conn, "tiktok_battles", "opponent2_display_name", "VARCHAR(255) DEFAULT ''")
        _ensure_column_mysql(conn, "tiktok_battles", "opponent2_avatar_url", "TEXT")
        _ensure_column_mysql(conn, "tiktok_battles", "opponent2_score", "BIGINT DEFAULT 0")
        _ensure_column_mysql(conn, "tiktok_battles", "opponent3_user_id", "VARCHAR(128) DEFAULT ''")
        _ensure_column_mysql(conn, "tiktok_battles", "opponent3_username", "VARCHAR(191) DEFAULT ''")
        _ensure_column_mysql(conn, "tiktok_battles", "opponent3_display_name", "VARCHAR(255) DEFAULT ''")
        _ensure_column_mysql(conn, "tiktok_battles", "opponent3_avatar_url", "TEXT")
        _ensure_column_mysql(conn, "tiktok_battles", "opponent3_score", "BIGINT DEFAULT 0")
        _ensure_column_mysql(conn, "tiktok_battles", "streamer_rank", "INT DEFAULT 0")
        conn.commit()
    finally:
        raw_conn.close()


def _init_sqlite_schema() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                `key` TEXT PRIMARY KEY,
                `value` TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS streamers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT,
                avatar_url TEXT,
                room_id TEXT,
                follower_count INTEGER DEFAULT 0,
                is_live INTEGER DEFAULT 0,
                is_tracking INTEGER DEFAULT 0,
                auto_monitor INTEGER DEFAULT 0,
                current_viewers INTEGER DEFAULT 0,
                last_error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                last_seen_at TEXT
            );

            CREATE TABLE IF NOT EXISTS stream_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                streamer_id INTEGER NOT NULL,
                room_id TEXT,
                title TEXT DEFAULT '',
                started_at TEXT NOT NULL,
                ended_at TEXT,
                peak_viewers INTEGER DEFAULT 0,
                last_fan_tickets INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                FOREIGN KEY (streamer_id) REFERENCES streamers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS gift_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                streamer_id INTEGER NOT NULL,
                session_id INTEGER,
                event_uid TEXT UNIQUE,
                sender_user_id TEXT DEFAULT '',
                sender_username TEXT NOT NULL,
                sender_nickname TEXT,
                is_super_fan INTEGER DEFAULT 0,
                gift_id INTEGER DEFAULT 0,
                gift_name TEXT NOT NULL,
                gift_icon_url TEXT,
                diamond_count INTEGER NOT NULL,
                repeat_count INTEGER NOT NULL DEFAULT 1,
                combo_count INTEGER DEFAULT 1,
                group_id TEXT DEFAULT '',
                room_fan_tickets INTEGER DEFAULT 0,
                total_coins INTEGER NOT NULL,
                usd_net REAL NOT NULL,
                eur_net REAL NOT NULL,
                usd_gross REAL NOT NULL,
                eur_gross REAL NOT NULL,
                source TEXT DEFAULT 'tiktoklive',
                created_at TEXT NOT NULL,
                FOREIGN KEY (streamer_id) REFERENCES streamers(id) ON DELETE CASCADE,
                FOREIGN KEY (session_id) REFERENCES stream_sessions(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS telegram_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE,
                username TEXT COLLATE NOCASE,
                first_name TEXT DEFAULT '',
                role TEXT DEFAULT 'user',
                is_allowed INTEGER DEFAULT 0,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                last_seen_at TEXT
            );

            CREATE TABLE IF NOT EXISTS tiktok_battles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                streamer_id INTEGER NOT NULL,
                session_id INTEGER,
                battle_id TEXT NOT NULL,
                opponent_user_id TEXT DEFAULT '',
                opponent_username TEXT DEFAULT '',
                opponent_display_name TEXT DEFAULT '',
                opponent_avatar_url TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                duration_seconds INTEGER DEFAULT 300,
                streamer_score INTEGER DEFAULT 0,
                opponent_score INTEGER DEFAULT 0,
                result TEXT DEFAULT 'ONGOING',
                streamer_mvp_name TEXT DEFAULT '',
                streamer_mvp_score INTEGER DEFAULT 0,
                streamer_mvp_avatar TEXT,
                opponent_mvp_name TEXT DEFAULT '',
                opponent_mvp_score INTEGER DEFAULT 0,
                opponent_mvp_avatar TEXT,
                coins_earned INTEGER DEFAULT 0,
                usd_net REAL DEFAULT 0.0,
                eur_net REAL DEFAULT 0.0,
                usd_gross REAL DEFAULT 0.0,
                eur_gross REAL DEFAULT 0.0,
                status TEXT DEFAULT 'active',
                raw_data_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (streamer_id) REFERENCES streamers(id) ON DELETE CASCADE,
                FOREIGN KEY (session_id) REFERENCES stream_sessions(id) ON DELETE SET NULL
            );
            """
        )

        _ensure_column_sqlite(conn, "streamers", "auto_monitor", "INTEGER DEFAULT 0")
        _ensure_column_sqlite(conn, "streamers", "current_viewers", "INTEGER DEFAULT 0")
        _ensure_column_sqlite(conn, "streamers", "last_error", "TEXT DEFAULT ''")

        _ensure_column_sqlite(conn, "gift_events", "session_id", "INTEGER")
        _ensure_column_sqlite(conn, "gift_events", "event_uid", "TEXT")
        _ensure_column_sqlite(conn, "gift_events", "sender_user_id", "TEXT DEFAULT ''")
        _ensure_column_sqlite(conn, "gift_events", "is_super_fan", "INTEGER DEFAULT 0")
        _ensure_column_sqlite(conn, "gift_events", "combo_count", "INTEGER DEFAULT 1")
        _ensure_column_sqlite(conn, "gift_events", "group_id", "TEXT DEFAULT ''")
        _ensure_column_sqlite(conn, "gift_events", "room_fan_tickets", "INTEGER DEFAULT 0")

        _ensure_column_sqlite(conn, "tiktok_battles", "battle_mode", "TEXT DEFAULT '1v1'")
        _ensure_column_sqlite(conn, "tiktok_battles", "teammate_user_id", "TEXT DEFAULT ''")
        _ensure_column_sqlite(conn, "tiktok_battles", "teammate_username", "TEXT DEFAULT ''")
        _ensure_column_sqlite(conn, "tiktok_battles", "teammate_display_name", "TEXT DEFAULT ''")
        _ensure_column_sqlite(conn, "tiktok_battles", "teammate_avatar_url", "TEXT DEFAULT ''")
        _ensure_column_sqlite(conn, "tiktok_battles", "teammate_score", "INTEGER DEFAULT 0")
        _ensure_column_sqlite(conn, "tiktok_battles", "opponent2_user_id", "TEXT DEFAULT ''")
        _ensure_column_sqlite(conn, "tiktok_battles", "opponent2_username", "TEXT DEFAULT ''")
        _ensure_column_sqlite(conn, "tiktok_battles", "opponent2_display_name", "TEXT DEFAULT ''")
        _ensure_column_sqlite(conn, "tiktok_battles", "opponent2_avatar_url", "TEXT DEFAULT ''")
        _ensure_column_sqlite(conn, "tiktok_battles", "opponent2_score", "INTEGER DEFAULT 0")
        _ensure_column_sqlite(conn, "tiktok_battles", "opponent3_user_id", "TEXT DEFAULT ''")
        _ensure_column_sqlite(conn, "tiktok_battles", "opponent3_username", "TEXT DEFAULT ''")
        _ensure_column_sqlite(conn, "tiktok_battles", "opponent3_display_name", "TEXT DEFAULT ''")
        _ensure_column_sqlite(conn, "tiktok_battles", "opponent3_avatar_url", "TEXT DEFAULT ''")
        _ensure_column_sqlite(conn, "tiktok_battles", "opponent3_score", "INTEGER DEFAULT 0")
        _ensure_column_sqlite(conn, "tiktok_battles", "streamer_rank", "INTEGER DEFAULT 0")

        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_gift_events_streamer_date
                ON gift_events(streamer_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_gift_events_session
                ON gift_events(session_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gift_events_uid
                ON gift_events(event_uid) WHERE event_uid IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_stream_sessions_streamer
                ON stream_sessions(streamer_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_telegram_users_username
                ON telegram_users(username);
            CREATE INDEX IF NOT EXISTS idx_battles_streamer
                ON tiktok_battles(streamer_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_battles_battle_id
                ON tiktok_battles(battle_id);
            CREATE INDEX IF NOT EXISTS idx_battles_result
                ON tiktok_battles(result);
            CREATE INDEX IF NOT EXISTS idx_battles_opponent
                ON tiktok_battles(opponent_username);
            """
        )


def init_db(force: bool = False) -> None:
    global _db_initialized
    with _db_init_lock:
        if _db_initialized and not force:
            return
        if config.is_mysql:
            _init_mysql_schema()
        else:
            _init_sqlite_schema()

        with get_db() as conn:
            for k, v in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings (`key`, `value`) VALUES (?, ?)", (k, v)
                )

            # Initiale Admins/Whitelist-User aus der .env synchronisieren
            now = datetime.now().isoformat(timespec="seconds")
            for tg_id in config.env_allowed_telegram_ids:
                existing = conn.execute(
                    "SELECT id FROM telegram_users WHERE telegram_id = ?", (tg_id,)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE telegram_users SET is_allowed = 1, role = 'admin' WHERE id = ?",
                        (existing["id"],),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO telegram_users (telegram_id, username, first_name, role, is_allowed, note, created_at, last_seen_at)
                        VALUES (?, '', 'ENV Admin', 'admin', 1, 'Aus .env geladen', ?, ?)
                        """,
                        (tg_id, now, now),
                    )

            for tg_uname in config.env_allowed_telegram_usernames:
                existing = conn.execute(
                    "SELECT id FROM telegram_users WHERE LOWER(username) = LOWER(?)",
                    (tg_uname,),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE telegram_users SET is_allowed = 1, role = 'admin' WHERE id = ?",
                        (existing["id"],),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO telegram_users (telegram_id, username, first_name, role, is_allowed, note, created_at, last_seen_at)
                        VALUES (NULL, ?, 'ENV Admin', 'admin', 1, 'Aus .env geladen', ?, ?)
                        """,
                        (tg_uname.lower(), now, now),
                    )
        _db_initialized = True


def get_settings() -> Dict[str, Any]:
    with get_db() as conn:
        rows = conn.execute("SELECT `key`, `value` FROM settings").fetchall()
        data = {r["key"]: r["value"] for r in rows}
    mode = str(data.get("telegram_access_mode", config.telegram_access_mode or "whitelist")).lower()
    if mode not in ("whitelist", "public"):
        mode = "whitelist"
    return {
        "usd_per_diamond": float(data.get("usd_per_diamond", 0.005)),
        "gross_usd_per_coin": float(data.get("gross_usd_per_coin", 0.0106)),
        "usd_to_eur_rate": float(data.get("usd_to_eur_rate", 0.92)),
        "currency_primary": data.get("currency_primary", "EUR"),
        "poll_interval_seconds": int(float(data.get("poll_interval_seconds", 45))),
        "api_key": str(data.get("api_key", "")),
        "telegram_access_mode": mode,
    }


def update_settings(new_settings: Dict[str, Any]) -> Dict[str, Any]:
    with get_db() as conn:
        for key in DEFAULT_SETTINGS.keys():
            if key in new_settings and new_settings[key] is not None:
                val = str(new_settings[key]).strip()
                if key == "telegram_access_mode":
                    val = val.lower()
                    if val not in ("whitelist", "public"):
                        continue
                conn.execute(
                    "INSERT OR REPLACE INTO settings (`key`, `value`) VALUES (?, ?)",
                    (key, val),
                )
    return get_settings()


# =====================================================================
# TELEGRAM ZUGRIFFSKONTROLLE (WHITELIST / PUBLIC / ROLLEN / GRUPPEN)
# =====================================================================

def get_telegram_access_mode() -> str:
    return get_settings().get("telegram_access_mode", "whitelist")


def set_telegram_access_mode(mode: str) -> str:
    clean_mode = mode.strip().lower()
    if clean_mode not in ("whitelist", "public"):
        raise ValueError("Modus muss 'whitelist' oder 'public' sein.")
    update_settings({"telegram_access_mode": clean_mode})
    return clean_mode


def list_telegram_users() -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM telegram_users
            ORDER BY is_allowed DESC, CASE WHEN role = 'admin' THEN 0 ELSE 1 END ASC, id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_telegram_user_permission(
    identifier: str | int,
    is_allowed: int | bool = 1,
    role: str = "user",
    first_name: Optional[str] = None,
    note: Optional[str] = None,
    telegram_id_override: Optional[int] = None,
    username_override: Optional[str] = None,
) -> Dict[str, Any]:
    raw = str(identifier).strip()
    if not raw:
        raise ValueError("Bitte eine Telegram User-ID oder einen [@]username angeben.")

    clean_role = role.strip().lower() if role else "user"
    if clean_role not in ("admin", "user"):
        clean_role = "user"

    allowed_val = int(is_allowed)
    if allowed_val not in (-1, 0, 1):
        allowed_val = 1 if bool(is_allowed) else 0

    now = datetime.now().isoformat(timespec="seconds")
    clean_ident = raw.lstrip("@").strip()
    is_numeric_id = clean_ident.lstrip("-").isdigit()

    with get_db() as conn:
        existing = None
        tg_id: Optional[int] = telegram_id_override
        uname: Optional[str] = (username_override or "").strip().lstrip("@").lower() or None

        if is_numeric_id:
            tg_id = int(clean_ident)
            existing = conn.execute(
                "SELECT * FROM telegram_users WHERE telegram_id = ? OR id = ?",
                (tg_id, tg_id),
            ).fetchone()
            if not existing and uname:
                existing = conn.execute(
                    "SELECT * FROM telegram_users WHERE LOWER(username) = LOWER(?)",
                    (uname,),
                ).fetchone()
        else:
            uname = clean_ident.lower()
            existing = conn.execute(
                "SELECT * FROM telegram_users WHERE LOWER(username) = LOWER(?)",
                (uname,),
            ).fetchone()
            if not existing and tg_id is not None:
                existing = conn.execute(
                    "SELECT * FROM telegram_users WHERE telegram_id = ?",
                    (tg_id,),
                ).fetchone()

        if existing:
            new_tg_id = tg_id if tg_id is not None else existing["telegram_id"]
            new_uname = uname if uname else existing["username"]
            new_fname = first_name if first_name is not None else existing["first_name"]
            new_note = note if note is not None else existing["note"]
            conn.execute(
                """
                UPDATE telegram_users
                SET telegram_id = ?, username = ?, is_allowed = ?, role = ?, first_name = ?, note = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (new_tg_id, new_uname, allowed_val, clean_role, new_fname, new_note, now, existing["id"]),
            )
            row_id = existing["id"]
        else:
            cur = conn.execute(
                """
                INSERT INTO telegram_users (
                    telegram_id, username, first_name, role, is_allowed, note, created_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tg_id,
                    uname or "",
                    first_name or "",
                    clean_role,
                    allowed_val,
                    note or "Manuell angelegt",
                    now,
                    now,
                ),
            )
            row_id = cur.lastrowid

        row = conn.execute(
            "SELECT * FROM telegram_users WHERE id = ?", (row_id,)
        ).fetchone()
        return dict(row)


def delete_telegram_user(record_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM telegram_users WHERE id = ?", (record_id,))
        return cur.rowcount > 0


def check_telegram_user_access(
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    chat_id: Optional[int] = None,
    chat_title: Optional[str] = None,
) -> Dict[str, Any]:
    clean_uname = (username or "").strip().lstrip("@").lower()
    clean_fname = (first_name or "").strip()
    now = datetime.now().isoformat(timespec="seconds")
    mode = get_telegram_access_mode()

    in_env_admin = (
        telegram_id in config.env_allowed_telegram_ids
        or (clean_uname and clean_uname in config.env_allowed_telegram_usernames)
    )

    is_group = bool(chat_id is not None and chat_id != telegram_id)
    source_note = (
        f"Gruppe: {chat_title or chat_id}" if is_group else "Privatchat"
    )

    with get_db() as conn:
        row_by_id = conn.execute(
            "SELECT * FROM telegram_users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()

        row_by_uname = None
        if clean_uname:
            row_by_uname = conn.execute(
                "SELECT * FROM telegram_users WHERE LOWER(username) = LOWER(?)",
                (clean_uname,),
            ).fetchone()

        if row_by_id and row_by_uname and row_by_id["id"] != row_by_uname["id"]:
            merged_allowed = max(int(row_by_id["is_allowed"]), int(row_by_uname["is_allowed"]))
            if int(row_by_id["is_allowed"]) < 0 or int(row_by_uname["is_allowed"]) < 0:
                if max(int(row_by_id["is_allowed"]), int(row_by_uname["is_allowed"])) < 1:
                    merged_allowed = -1
            merged_role = (
                "admin"
                if (row_by_id["role"] == "admin" or row_by_uname["role"] == "admin")
                else "user"
            )
            conn.execute("DELETE FROM telegram_users WHERE id = ?", (row_by_uname["id"],))
            conn.execute(
                "UPDATE telegram_users SET is_allowed = ?, role = ? WHERE id = ?",
                (merged_allowed, merged_role, row_by_id["id"]),
            )
            row = conn.execute(
                "SELECT * FROM telegram_users WHERE id = ?", (row_by_id["id"],)
            ).fetchone()
        else:
            row = row_by_id or row_by_uname

        if row:
            new_role = "admin" if in_env_admin else row["role"]
            new_allowed = 1 if in_env_admin else int(row["is_allowed"])
            conn.execute(
                """
                UPDATE telegram_users
                SET telegram_id = ?,
                    username = COALESCE(NULLIF(?, ''), username),
                    first_name = COALESCE(NULLIF(?, ''), first_name),
                    role = ?,
                    is_allowed = ?,
                    last_seen_at = ?
                WHERE id = ?
                """,
                (
                    telegram_id,
                    clean_uname,
                    clean_fname,
                    new_role,
                    new_allowed,
                    now,
                    row["id"],
                ),
            )
            row_id = row["id"]
        else:
            initial_role = "admin" if in_env_admin else "user"
            initial_allowed = 1 if in_env_admin else 0
            note = "Aus .env autorisiert" if in_env_admin else f"Anfrage ({source_note})"
            cur = conn.execute(
                """
                INSERT INTO telegram_users (
                    telegram_id, username, first_name, role, is_allowed, note, created_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_id,
                    clean_uname,
                    clean_fname,
                    initial_role,
                    initial_allowed,
                    note,
                    now,
                    now,
                ),
            )
            row_id = cur.lastrowid

        user_row = dict(
            conn.execute("SELECT * FROM telegram_users WHERE id = ?", (row_id,)).fetchone()
        )

        group_whitelisted = False
        if is_group and chat_id is not None:
            grp_row = conn.execute(
                "SELECT is_allowed FROM telegram_users WHERE telegram_id = ?",
                (int(chat_id),),
            ).fetchone()
            if grp_row and int(grp_row["is_allowed"]) == 1:
                group_whitelisted = True

    is_allowed_flag = int(user_row.get("is_allowed", 0))
    role_val = user_row.get("role", "user")

    if is_allowed_flag < 0:
        allowed = False
        is_admin = False
    elif mode == "public":
        allowed = True
        is_admin = bool(is_allowed_flag == 1 and role_val == "admin")
    else:
        allowed = bool(is_allowed_flag == 1 or group_whitelisted)
        is_admin = bool(is_allowed_flag == 1 and role_val == "admin")

    return {
        "allowed": allowed,
        "is_admin": is_admin,
        "mode": mode,
        "is_group": is_group,
        "user": user_row,
    }


def get_streamer_by_id_or_username(identifier: str | int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        if isinstance(identifier, int) or str(identifier).isdigit():
            row = conn.execute(
                "SELECT * FROM streamers WHERE id = ?", (int(identifier),)
            ).fetchone()
            if row:
                return dict(row)
        clean = str(identifier).strip().lstrip("@").lower()
        row = conn.execute(
            "SELECT * FROM streamers WHERE LOWER(username) = LOWER(?)", (clean,)
        ).fetchone()
        return dict(row) if row else None


def get_auto_monitor_streamers() -> List[Dict[str, Any]]:
    """
    Liefert alle Streamer zurück, die in der Datenbank für die automatische Aufzeichnung
    markiert sind (`auto_monitor = 1` oder `is_tracking = 1`).
    Synchronisiert dabei `auto_monitor = 1`, damit der Status konsistent bleibt.
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE streamers SET auto_monitor = 1 WHERE is_tracking = 1 AND auto_monitor = 0"
        )
        rows = conn.execute(
            "SELECT * FROM streamers WHERE auto_monitor = 1 OR is_tracking = 1 ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_streamer(
    username: str,
    display_name: Optional[str] = None,
    avatar_url: Optional[str] = None,
    room_id: Optional[str] = None,
    follower_count: Optional[int] = None,
    is_live: Optional[bool] = None,
    auto_monitor: Optional[bool] = None,
) -> Dict[str, Any]:
    clean_username = username.strip().lstrip("@").lower()
    if not clean_username:
        raise ValueError("Username darf nicht leer sein.")

    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM streamers WHERE LOWER(username) = LOWER(?)", (clean_username,)
        ).fetchone()

        if existing:
            new_display = display_name if display_name else existing["display_name"]
            new_avatar = avatar_url if avatar_url else existing["avatar_url"]
            new_room = room_id if room_id is not None else existing["room_id"]
            new_followers = (
                follower_count if follower_count is not None else existing["follower_count"]
            )
            new_live = int(is_live) if is_live is not None else existing["is_live"]
            new_auto = (
                int(auto_monitor) if auto_monitor is not None else existing["auto_monitor"]
            )
            conn.execute(
                """
                UPDATE streamers
                SET display_name = ?, avatar_url = ?, room_id = ?,
                    follower_count = ?, is_live = ?, auto_monitor = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (
                    new_display,
                    new_avatar,
                    new_room,
                    new_followers,
                    new_live,
                    new_auto,
                    now,
                    existing["id"],
                ),
            )
            streamer_id = existing["id"]
        else:
            cursor = conn.execute(
                """
                INSERT INTO streamers (
                    username, display_name, avatar_url, room_id,
                    follower_count, is_live, auto_monitor, created_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_username,
                    display_name or clean_username,
                    avatar_url or "",
                    room_id or "",
                    follower_count or 0,
                    int(bool(is_live)),
                    int(bool(auto_monitor)),
                    now,
                    now,
                ),
            )
            streamer_id = cursor.lastrowid

        row = conn.execute(
            "SELECT * FROM streamers WHERE id = ?", (streamer_id,)
        ).fetchone()
        return dict(row)


def update_streamer_runtime_state(
    username: str,
    is_live: Optional[bool] = None,
    is_tracking: Optional[bool] = None,
    auto_monitor: Optional[bool] = None,
    room_id: Optional[str] = None,
    display_name: Optional[str] = None,
    avatar_url: Optional[str] = None,
    follower_count: Optional[int] = None,
    current_viewers: Optional[int] = None,
    last_error: Optional[str] = None,
) -> None:
    clean_username = username.strip().lstrip("@").lower()
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM streamers WHERE LOWER(username) = LOWER(?)", (clean_username,)
        ).fetchone()
        if not row:
            return
        conn.execute(
            """
            UPDATE streamers
            SET is_live = COALESCE(?, is_live),
                is_tracking = COALESCE(?, is_tracking),
                auto_monitor = COALESCE(?, auto_monitor),
                room_id = COALESCE(?, room_id),
                display_name = COALESCE(?, display_name),
                avatar_url = COALESCE(?, avatar_url),
                follower_count = COALESCE(?, follower_count),
                current_viewers = COALESCE(?, current_viewers),
                last_error = COALESCE(?, last_error),
                last_seen_at = ?
            WHERE id = ?
            """,
            (
                int(is_live) if is_live is not None else None,
                int(is_tracking) if is_tracking is not None else None,
                int(auto_monitor) if auto_monitor is not None else None,
                room_id,
                display_name,
                avatar_url,
                follower_count,
                current_viewers,
                last_error,
                now,
                row["id"],
            ),
        )


def start_stream_session(
    streamer_id: int, room_id: str = "", title: str = ""
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        active = conn.execute(
            """
            SELECT id, room_id FROM stream_sessions
            WHERE streamer_id = ? AND status = 'active'
            ORDER BY id DESC LIMIT 1
            """,
            (streamer_id,),
        ).fetchone()

        if active and (not room_id or active["room_id"] == room_id):
            if title:
                conn.execute(
                    "UPDATE stream_sessions SET title = ? WHERE id = ?",
                    (title, active["id"]),
                )
            return int(active["id"])

        conn.execute(
            """
            UPDATE stream_sessions
            SET status = 'ended', ended_at = ?
            WHERE streamer_id = ? AND status = 'active'
            """,
            (now, streamer_id),
        )

        cur = conn.execute(
            """
            INSERT INTO stream_sessions (
                streamer_id, room_id, title, started_at, status
            )
            VALUES (?, ?, ?, ?, 'active')
            """,
            (streamer_id, room_id or "", title or "", now),
        )
        return int(cur.lastrowid)


def update_stream_session_metrics(
    session_id: int,
    viewer_count: Optional[int] = None,
    room_fan_tickets: Optional[int] = None,
) -> None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT peak_viewers, last_fan_tickets FROM stream_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return
        new_peak = max(int(row["peak_viewers"] or 0), int(viewer_count or 0))
        new_tickets = max(int(row["last_fan_tickets"] or 0), int(room_fan_tickets or 0))
        conn.execute(
            """
            UPDATE stream_sessions
            SET peak_viewers = ?, last_fan_tickets = ?
            WHERE id = ?
            """,
            (new_peak, new_tickets, session_id),
        )


def end_stream_session(streamer_id: int, session_id: Optional[int] = None) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        if session_id:
            conn.execute(
                "UPDATE stream_sessions SET status = 'ended', ended_at = ? WHERE id = ?",
                (now, session_id),
            )
        else:
            conn.execute(
                """
                UPDATE stream_sessions
                SET status = 'ended', ended_at = ?
                WHERE streamer_id = ? AND status = 'active'
                """,
                (now, streamer_id),
            )


def record_gift_event(
    streamer_id: int,
    sender_username: str,
    gift_name: str,
    diamond_count: int,
    repeat_count: int = 1,
    sender_nickname: Optional[str] = None,
    sender_user_id: str = "",
    is_super_fan: bool = False,
    gift_id: int = 0,
    gift_icon_url: str = "",
    combo_count: int = 1,
    group_id: str = "",
    room_fan_tickets: int = 0,
    event_uid: Optional[str] = None,
    session_id: Optional[int] = None,
    source: str = "tiktoklive",
    created_at: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    cfg = get_settings()
    diamond_count = max(0, int(diamond_count))
    repeat_count = max(1, int(repeat_count))
    total_coins = diamond_count * repeat_count

    usd_net = round(total_coins * cfg["usd_per_diamond"], 4)
    eur_net = round(usd_net * cfg["usd_to_eur_rate"], 4)
    usd_gross = round(total_coins * cfg["gross_usd_per_coin"], 4)
    eur_gross = round(usd_gross * cfg["usd_to_eur_rate"], 4)

    ts = created_at or datetime.now().isoformat(timespec="seconds")

    with get_db() as conn:
        if event_uid:
            existing = conn.execute(
                "SELECT id FROM gift_events WHERE event_uid = ?", (event_uid,)
            ).fetchone()
            if existing:
                return None, False

        cur = conn.execute(
            """
            INSERT INTO gift_events (
                streamer_id, session_id, event_uid,
                sender_user_id, sender_username, sender_nickname, is_super_fan,
                gift_id, gift_name, gift_icon_url,
                diamond_count, repeat_count, combo_count, group_id, room_fan_tickets,
                total_coins, usd_net, eur_net, usd_gross, eur_gross,
                source, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                streamer_id,
                session_id,
                event_uid,
                str(sender_user_id or ""),
                sender_username,
                sender_nickname or sender_username,
                int(bool(is_super_fan)),
                gift_id,
                gift_name,
                gift_icon_url,
                diamond_count,
                repeat_count,
                combo_count,
                str(group_id or ""),
                int(room_fan_tickets or 0),
                total_coins,
                usd_net,
                eur_net,
                usd_gross,
                eur_gross,
                source,
                ts,
            ),
        )
        conn.execute(
            "UPDATE streamers SET last_seen_at = ? WHERE id = ?",
            (ts, streamer_id),
        )
        if session_id and room_fan_tickets > 0:
            conn.execute(
                """
                UPDATE stream_sessions
                SET last_fan_tickets = MAX(last_fan_tickets, ?)
                WHERE id = ?
                """,
                (int(room_fan_tickets), session_id),
            )

        row = conn.execute(
            """
            SELECT g.*, s.username AS streamer_username, s.display_name AS streamer_display_name
            FROM gift_events g
            JOIN streamers s ON s.id = g.streamer_id
            WHERE g.id = ?
            """,
            (cur.lastrowid,),
        ).fetchone()
        return (dict(row) if row else None), True


def delete_streamer(streamer_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM streamers WHERE id = ?", (streamer_id,))
        return cur.rowcount > 0


def _get_period_cutoff(period: str) -> Optional[str]:
    now = datetime.now()
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    if period == "24h":
        return (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    if period == "7d":
        return (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    if period == "30d":
        return (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    return None


def get_dashboard_overview(period: str = "all") -> Dict[str, Any]:
    cutoff = _get_period_cutoff(period)
    cfg = get_settings()

    with get_db() as conn:
        if cutoff:
            streamer_rows = conn.execute(
                """
                SELECT
                    s.*,
                    COALESCE(SUM(g.total_coins), 0) AS total_coins,
                    COUNT(g.id) AS gift_events_count,
                    COALESCE(SUM(g.repeat_count), 0) AS total_gifts_sent,
                    COUNT(DISTINCT g.sender_username) AS unique_gifters
                FROM streamers s
                LEFT JOIN gift_events g
                    ON g.streamer_id = s.id AND g.created_at >= ?
                GROUP BY s.id
                ORDER BY total_coins DESC, s.username ASC
                """,
                (cutoff,),
            ).fetchall()
        else:
            streamer_rows = conn.execute(
                """
                SELECT
                    s.*,
                    COALESCE(SUM(g.total_coins), 0) AS total_coins,
                    COUNT(g.id) AS gift_events_count,
                    COALESCE(SUM(g.repeat_count), 0) AS total_gifts_sent,
                    COUNT(DISTINCT g.sender_username) AS unique_gifters
                FROM streamers s
                LEFT JOIN gift_events g
                    ON g.streamer_id = s.id
                GROUP BY s.id
                ORDER BY total_coins DESC, s.username ASC
                """
            ).fetchall()

        streamers: List[Dict[str, Any]] = []
        totals = {
            "streamer_count": len(streamer_rows),
            "live_count": 0,
            "tracking_count": 0,
            "total_coins": 0,
            "usd_net": 0.0,
            "eur_net": 0.0,
            "usd_gross": 0.0,
            "eur_gross": 0.0,
            "total_gifts_sent": 0,
            "gift_events_count": 0,
        }

        for r in streamer_rows:
            d = dict(r)
            coins = int(d["total_coins"] or 0)
            d["usd_net"] = round(coins * cfg["usd_per_diamond"], 2)
            d["eur_net"] = round(d["usd_net"] * cfg["usd_to_eur_rate"], 2)
            d["usd_gross"] = round(coins * cfg["gross_usd_per_coin"], 2)
            d["eur_gross"] = round(d["usd_gross"] * cfg["usd_to_eur_rate"], 2)

            top_gift = conn.execute(
                """
                SELECT gift_name, SUM(total_coins) AS coins, SUM(repeat_count) AS count
                FROM gift_events
                WHERE streamer_id = ? AND (? IS NULL OR created_at >= ?)
                GROUP BY gift_name
                ORDER BY coins DESC
                LIMIT 1
                """,
                (d["id"], cutoff, cutoff),
            ).fetchone()
            d["top_gift"] = dict(top_gift) if top_gift else None

            top_gifter = conn.execute(
                """
                SELECT sender_username, MAX(sender_nickname) AS sender_nickname, SUM(total_coins) AS coins
                FROM gift_events
                WHERE streamer_id = ? AND (? IS NULL OR created_at >= ?)
                GROUP BY sender_username
                ORDER BY coins DESC
                LIMIT 1
                """,
                (d["id"], cutoff, cutoff),
            ).fetchone()
            d["top_gifter"] = dict(top_gifter) if top_gifter else None

            active_sess = conn.execute(
                """
                SELECT id, room_id, started_at, peak_viewers, last_fan_tickets
                FROM stream_sessions
                WHERE streamer_id = ? AND status = 'active'
                ORDER BY id DESC LIMIT 1
                """,
                (d["id"],),
            ).fetchone()
            d["active_session"] = dict(active_sess) if active_sess else None

            streamers.append(d)

            totals["live_count"] += int(bool(d["is_live"]))
            totals["tracking_count"] += int(bool(d["is_tracking"]))
            totals["total_coins"] += coins
            totals["usd_net"] = round(totals["usd_net"] + d["usd_net"], 2)
            totals["eur_net"] = round(totals["eur_net"] + d["eur_net"], 2)
            totals["usd_gross"] = round(totals["usd_gross"] + d["usd_gross"], 2)
            totals["eur_gross"] = round(totals["eur_gross"] + d["eur_gross"], 2)
            totals["total_gifts_sent"] += int(d["total_gifts_sent"] or 0)
            totals["gift_events_count"] += int(d["gift_events_count"] or 0)

        if cutoff:
            recent_rows = conn.execute(
                """
                SELECT g.*, s.username AS streamer_username, s.display_name AS streamer_display_name
                FROM gift_events g
                JOIN streamers s ON s.id = g.streamer_id
                WHERE g.created_at >= ?
                ORDER BY g.created_at DESC, g.id DESC
                LIMIT 50
                """,
                (cutoff,),
            ).fetchall()
        else:
            recent_rows = conn.execute(
                """
                SELECT g.*, s.username AS streamer_username, s.display_name AS streamer_display_name
                FROM gift_events g
                JOIN streamers s ON s.id = g.streamer_id
                ORDER BY g.created_at DESC, g.id DESC
                LIMIT 50
                """
            ).fetchall()

        recent_gifts = []
        for r in recent_rows:
            gd = dict(r)
            coins = int(gd["total_coins"] or 0)
            gd["usd_net"] = round(coins * cfg["usd_per_diamond"], 2)
            gd["eur_net"] = round(gd["usd_net"] * cfg["usd_to_eur_rate"], 2)
            recent_gifts.append(gd)

    return {
        "settings": cfg,
        "period": period,
        "totals": totals,
        "streamers": streamers,
        "recent_gifts": recent_gifts,
        "telegram_users": list_telegram_users(),
    }


def get_streamer_detail(streamer_id: int, period: str = "all") -> Optional[Dict[str, Any]]:
    cutoff = _get_period_cutoff(period)
    cfg = get_settings()

    with get_db() as conn:
        streamer = conn.execute(
            "SELECT * FROM streamers WHERE id = ?", (streamer_id,)
        ).fetchone()
        if not streamer:
            return None

        s_dict = dict(streamer)

        gift_breakdown = conn.execute(
            """
            SELECT
                MAX(gift_id) AS gift_id,
                gift_name,
                diamond_count,
                SUM(repeat_count) AS total_quantity,
                SUM(total_coins) AS total_coins,
                COUNT(id) AS events_count
            FROM gift_events
            WHERE streamer_id = ? AND (? IS NULL OR created_at >= ?)
            GROUP BY gift_name, diamond_count
            ORDER BY total_coins DESC
            """,
            (streamer_id, cutoff, cutoff),
        ).fetchall()

        gifts_list = []
        total_coins = 0
        for g in gift_breakdown:
            gd = dict(g)
            c = int(gd["total_coins"] or 0)
            total_coins += c
            gd["usd_net"] = round(c * cfg["usd_per_diamond"], 2)
            gd["eur_net"] = round(gd["usd_net"] * cfg["usd_to_eur_rate"], 2)
            gifts_list.append(gd)

        top_gifters_rows = conn.execute(
            """
            SELECT
                sender_username,
                MAX(sender_nickname) AS sender_nickname,
                MAX(is_super_fan) AS is_super_fan,
                SUM(total_coins) AS total_coins,
                SUM(repeat_count) AS gifts_sent,
                COUNT(id) AS transactions
            FROM gift_events
            WHERE streamer_id = ? AND (? IS NULL OR created_at >= ?)
            GROUP BY sender_username
            ORDER BY total_coins DESC
            LIMIT 15
            """,
            (streamer_id, cutoff, cutoff),
        ).fetchall()

        top_gifters = []
        for tg in top_gifters_rows:
            tgd = dict(tg)
            c = int(tgd["total_coins"] or 0)
            tgd["usd_net"] = round(c * cfg["usd_per_diamond"], 2)
            tgd["eur_net"] = round(tgd["usd_net"] * cfg["usd_to_eur_rate"], 2)
            top_gifters.append(tgd)

        sessions_rows = conn.execute(
            """
            SELECT
                ss.*,
                COALESCE(SUM(g.total_coins), 0) AS session_coins,
                COUNT(g.id) AS gift_events
            FROM stream_sessions ss
            LEFT JOIN gift_events g ON g.session_id = ss.id
            WHERE ss.streamer_id = ?
            GROUP BY ss.id
            ORDER BY ss.id DESC
            LIMIT 20
            """,
            (streamer_id,),
        ).fetchall()

        sessions = []
        for sr in sessions_rows:
            sd = dict(sr)
            c = int(sd["session_coins"] or 0)
            sd["usd_net"] = round(c * cfg["usd_per_diamond"], 2)
            sd["eur_net"] = round(sd["usd_net"] * cfg["usd_to_eur_rate"], 2)
            sessions.append(sd)

        recent_rows = conn.execute(
            """
            SELECT *
            FROM gift_events
            WHERE streamer_id = ? AND (? IS NULL OR created_at >= ?)
            ORDER BY created_at DESC, id DESC
            LIMIT 50
            """,
            (streamer_id, cutoff, cutoff),
        ).fetchall()

        recent_events = []
        for r in recent_rows:
            rd = dict(r)
            c = int(rd["total_coins"] or 0)
            rd["usd_net"] = round(c * cfg["usd_per_diamond"], 2)
            rd["eur_net"] = round(rd["usd_net"] * cfg["usd_to_eur_rate"], 2)
            recent_events.append(rd)

        usd_net = round(total_coins * cfg["usd_per_diamond"], 2)
        eur_net = round(usd_net * cfg["usd_to_eur_rate"], 2)
        usd_gross = round(total_coins * cfg["gross_usd_per_coin"], 2)
        eur_gross = round(usd_gross * cfg["usd_to_eur_rate"], 2)

        return {
            "streamer": s_dict,
            "summary": {
                "total_coins": total_coins,
                "usd_net": usd_net,
                "eur_net": eur_net,
                "usd_gross": usd_gross,
                "eur_gross": eur_gross,
            },
            "gift_breakdown": gifts_list,
            "top_gifters": top_gifters,
            "sessions": sessions,
            "recent_events": recent_events,
        }


def get_analytics_data(streamer_id: Optional[int] = None, period: str = "all") -> Dict[str, Any]:
    """
    Liefert detaillierte Analytics und aggregierte Kennzahlen für Charts:
    - Gesamt-KPIs (Netto/Brutto, Coins, Gifts, Unique Gifters, Averages)
    - Whale-Konzentrationsmetriken (Top 1, Top 3, Top 5 Spenderanteil)
    - Chronologischer Einnahmen-Verlauf (stündlich bei 24h/today, täglich bei 7d/30d/all)
    - Multi-Streamer-Timeline-Vergleich für Top-Streamer (wenn kein Einzelstreamer gewählt)
    - Tageszeit-Aktivitätsmuster (00:00 - 23:00 Uhr)
    - Top Geschenke Verteilung (Doughnut/Pie & Tabelle)
    - Top Spender & Supporter Ranking (Bar-Chart & Tabelle)
    - Streamer-Vergleich (Ranking & Einnahmenverteilung)
    - Stream-Sessions Historie mit berechneter Dauer und Hourly Run-Rate (Coins/h)
    """
    cutoff = _get_period_cutoff(period)
    cfg = get_settings()

    with get_db() as conn:
        # 1. Streamer-Details, falls ein spezifischer Streamer gewählt wurde
        selected_streamer = None
        if streamer_id:
            s_row = conn.execute(
                "SELECT * FROM streamers WHERE id = ?", (streamer_id,)
            ).fetchone()
            if s_row:
                selected_streamer = dict(s_row)
            else:
                streamer_id = None

        # 2. Aggregierte Gesamt-Metriken im gewählten Zeitraum & Scope
        agg_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(total_coins), 0) AS total_coins,
                COALESCE(SUM(repeat_count), 0) AS total_gifts_sent,
                COUNT(id) AS gift_events_count,
                COUNT(DISTINCT sender_username) AS unique_gifters
            FROM gift_events
            WHERE (? IS NULL OR streamer_id = ?)
              AND (? IS NULL OR created_at >= ?)
            """,
            (streamer_id, streamer_id, cutoff, cutoff),
        ).fetchone()

        total_coins = int(agg_row["total_coins"] or 0)
        total_gifts = int(agg_row["total_gifts_sent"] or 0)
        gift_events_cnt = int(agg_row["gift_events_count"] or 0)
        unique_gifters = int(agg_row["unique_gifters"] or 0)

        usd_net = round(total_coins * cfg["usd_per_diamond"], 2)
        eur_net = round(usd_net * cfg["usd_to_eur_rate"], 2)
        usd_gross = round(total_coins * cfg["gross_usd_per_coin"], 2)
        eur_gross = round(usd_gross * cfg["usd_to_eur_rate"], 2)

        avg_coins_per_gift = round(total_coins / total_gifts, 1) if total_gifts > 0 else 0.0
        avg_coins_per_gifter = round(total_coins / unique_gifters, 1) if unique_gifters > 0 else 0.0
        avg_eur_per_gifter = round(eur_net / unique_gifters, 2) if unique_gifters > 0 else 0.0

        # 3. Top Spender & Supporter-Ranking sowie Whale-Konzentration
        top_gifters_rows = conn.execute(
            """
            SELECT
                sender_username,
                MAX(sender_nickname) AS sender_nickname,
                MAX(is_super_fan) AS is_super_fan,
                SUM(total_coins) AS total_coins,
                SUM(repeat_count) AS gifts_sent,
                COUNT(id) AS transactions
            FROM gift_events
            WHERE (? IS NULL OR streamer_id = ?)
              AND (? IS NULL OR created_at >= ?)
            GROUP BY sender_username
            ORDER BY total_coins DESC
            LIMIT 25
            """,
            (streamer_id, streamer_id, cutoff, cutoff),
        ).fetchall()

        top_gifters = []
        top1_coins = 0
        top3_coins = 0
        top5_coins = 0
        for idx, tg in enumerate(top_gifters_rows):
            c = int(tg["total_coins"] or 0)
            u_net = round(c * cfg["usd_per_diamond"], 2)
            e_net = round(u_net * cfg["usd_to_eur_rate"], 2)
            pct = round((c / total_coins * 100), 1) if total_coins > 0 else 0.0
            if idx == 0:
                top1_coins += c
            if idx < 3:
                top3_coins += c
            if idx < 5:
                top5_coins += c
            top_gifters.append({
                "rank": idx + 1,
                "sender_username": tg["sender_username"],
                "sender_nickname": tg["sender_nickname"] or tg["sender_username"],
                "is_super_fan": bool(tg["is_super_fan"]),
                "total_coins": c,
                "usd_net": u_net,
                "eur_net": e_net,
                "gifts_sent": int(tg["gifts_sent"] or 0),
                "transactions": int(tg["transactions"] or 0),
                "pct_of_total": pct,
            })

        whale_share_top1 = round((top1_coins / total_coins * 100), 1) if total_coins > 0 else 0.0
        whale_share_top3 = round((top3_coins / total_coins * 100), 1) if total_coins > 0 else 0.0
        whale_share_top5 = round((top5_coins / total_coins * 100), 1) if total_coins > 0 else 0.0

        # 4. Top Geschenke-Aufschlüsselung
        top_gifts_rows = conn.execute(
            """
            SELECT
                MAX(gift_id) AS gift_id,
                gift_name,
                diamond_count,
                SUM(repeat_count) AS total_quantity,
                SUM(total_coins) AS total_coins,
                COUNT(id) AS events_count
            FROM gift_events
            WHERE (? IS NULL OR streamer_id = ?)
              AND (? IS NULL OR created_at >= ?)
            GROUP BY gift_name, diamond_count
            ORDER BY total_coins DESC
            LIMIT 20
            """,
            (streamer_id, streamer_id, cutoff, cutoff),
        ).fetchall()

        top_gifts = []
        for g in top_gifts_rows:
            c = int(g["total_coins"] or 0)
            u_net = round(c * cfg["usd_per_diamond"], 2)
            e_net = round(u_net * cfg["usd_to_eur_rate"], 2)
            pct = round((c / total_coins * 100), 1) if total_coins > 0 else 0.0
            top_gifts.append({
                "gift_id": g["gift_id"],
                "gift_name": g["gift_name"],
                "diamond_count": int(g["diamond_count"] or 0),
                "total_quantity": int(g["total_quantity"] or 0),
                "total_coins": c,
                "usd_net": u_net,
                "eur_net": e_net,
                "events_count": int(g["events_count"] or 0),
                "pct_of_total": pct,
            })

        # 5. Tageszeit-Aktivitätsmuster (00:00 - 23:00 Uhr)
        tod_rows = conn.execute(
            """
            SELECT
                CAST(SUBSTR(created_at, 12, 2) AS INTEGER) AS hour_of_day,
                SUM(total_coins) AS coins,
                COUNT(id) AS events_count
            FROM gift_events
            WHERE (? IS NULL OR streamer_id = ?)
              AND (? IS NULL OR created_at >= ?)
            GROUP BY CAST(SUBSTR(created_at, 12, 2) AS INTEGER)
            ORDER BY hour_of_day ASC
            """,
            (streamer_id, streamer_id, cutoff, cutoff),
        ).fetchall()

        tod_map = {int(r["hour_of_day"]): (int(r["coins"] or 0), int(r["events_count"] or 0)) for r in tod_rows}
        time_of_day = []
        for h in range(24):
            c, ev = tod_map.get(h, (0, 0))
            u_net = round(c * cfg["usd_per_diamond"], 2)
            e_net = round(u_net * cfg["usd_to_eur_rate"], 2)
            time_of_day.append({
                "hour": h,
                "label": f"{h:02d}:00",
                "coins": c,
                "eur_net": e_net,
                "events_count": ev,
            })

        # 6. Chronologischer Einnahmen-Verlauf (Timeline)
        is_hourly = period in ("24h", "today")
        substr_len = 13 if is_hourly else 10

        timeline_rows = conn.execute(
            f"""
            SELECT
                SUBSTR(created_at, 1, {substr_len}) AS bucket,
                COALESCE(SUM(total_coins), 0) AS coins,
                COUNT(id) AS events_count,
                COUNT(DISTINCT sender_username) AS unique_gifters
            FROM gift_events
            WHERE (? IS NULL OR streamer_id = ?)
              AND (? IS NULL OR created_at >= ?)
            GROUP BY SUBSTR(created_at, 1, {substr_len})
            ORDER BY bucket ASC
            """,
            (streamer_id, streamer_id, cutoff, cutoff),
        ).fetchall()

        timeline = []
        for tr in timeline_rows:
            b = tr["bucket"]
            c = int(tr["coins"] or 0)
            u_net = round(c * cfg["usd_per_diamond"], 2)
            e_net = round(u_net * cfg["usd_to_eur_rate"], 2)
            if is_hourly:
                parts = b.replace("T", " ").split(" ")
                time_part = f"{parts[1]}:00" if len(parts) > 1 else b
                label = time_part if period == "today" else f"{parts[0][5:]} {time_part}"
            else:
                label = f"{b[8:10]}.{b[5:7]}." if len(b) >= 10 else b

            timeline.append({
                "bucket": b,
                "label": label,
                "coins": c,
                "eur_net": e_net,
                "events_count": int(tr["events_count"] or 0),
                "unique_gifters": int(tr["unique_gifters"] or 0),
            })

        # 7. Streamer Ranking & Vergleich
        streamer_ranking_rows = conn.execute(
            """
            SELECT
                s.id,
                s.username,
                s.display_name,
                s.avatar_url,
                s.follower_count,
                s.current_viewers,
                s.is_live,
                s.is_tracking,
                COALESCE(SUM(g.total_coins), 0) AS total_coins,
                COUNT(g.id) AS gift_events_count,
                COALESCE(SUM(g.repeat_count), 0) AS total_gifts_sent,
                COUNT(DISTINCT g.sender_username) AS unique_gifters
            FROM streamers s
            LEFT JOIN gift_events g
                ON g.streamer_id = s.id AND (? IS NULL OR g.created_at >= ?)
            GROUP BY s.id
            ORDER BY total_coins DESC, s.username ASC
            """,
            (cutoff, cutoff),
        ).fetchall()

        streamers_ranking = []
        top_streamer_ids = []
        for sr in streamer_ranking_rows:
            c = int(sr["total_coins"] or 0)
            u_net = round(c * cfg["usd_per_diamond"], 2)
            e_net = round(u_net * cfg["usd_to_eur_rate"], 2)
            pct = round((c / total_coins * 100), 1) if (total_coins > 0 and not streamer_id) else (100.0 if c > 0 else 0.0)
            streamers_ranking.append({
                "id": sr["id"],
                "username": sr["username"],
                "display_name": sr["display_name"] or sr["username"],
                "avatar_url": sr["avatar_url"] or "",
                "follower_count": int(sr["follower_count"] or 0),
                "current_viewers": int(sr["current_viewers"] or 0),
                "is_live": bool(sr["is_live"]),
                "is_tracking": bool(sr["is_tracking"]),
                "total_coins": c,
                "eur_net": e_net,
                "usd_net": u_net,
                "gift_events_count": int(sr["gift_events_count"] or 0),
                "total_gifts_sent": int(sr["total_gifts_sent"] or 0),
                "unique_gifters": int(sr["unique_gifters"] or 0),
                "pct_of_total": pct,
            })
            if c > 0 and len(top_streamer_ids) < 5:
                top_streamer_ids.append(sr["id"])

        # 8. Multi-Streamer-Timeline-Vergleich für die Top 5 Streamer (wenn Gesamtansicht)
        multi_streamer_datasets = []
        if not streamer_id and top_streamer_ids and timeline:
            buckets = [t["bucket"] for t in timeline]
            for sid in top_streamer_ids:
                s_meta = next((s for s in streamers_ranking if s["id"] == sid), None)
                if not s_meta:
                    continue
                s_rows = conn.execute(
                    f"""
                    SELECT
                        SUBSTR(created_at, 1, {substr_len}) AS bucket,
                        COALESCE(SUM(total_coins), 0) AS coins
                    FROM gift_events
                    WHERE streamer_id = ? AND (? IS NULL OR created_at >= ?)
                    GROUP BY SUBSTR(created_at, 1, {substr_len})
                    """,
                    (sid, cutoff, cutoff),
                ).fetchall()
                s_map = {r["bucket"]: int(r["coins"] or 0) for r in s_rows}
                series_data = [s_map.get(b, 0) for b in buckets]
                multi_streamer_datasets.append({
                    "streamer_id": sid,
                    "username": s_meta["username"],
                    "display_name": s_meta["display_name"],
                    "data": series_data,
                })

        # 9. Aufgezeichnete Sessions & Hourly Run-Rates
        sessions_rows = conn.execute(
            """
            SELECT
                ss.*,
                s.username AS streamer_username,
                s.display_name AS streamer_display_name,
                COALESCE(SUM(g.total_coins), 0) AS session_coins,
                COUNT(g.id) AS session_gifts
            FROM stream_sessions ss
            JOIN streamers s ON s.id = ss.streamer_id
            LEFT JOIN gift_events g ON g.session_id = ss.id
            WHERE (? IS NULL OR ss.streamer_id = ?)
              AND (? IS NULL OR ss.started_at >= ?)
            GROUP BY ss.id
            ORDER BY ss.started_at DESC, ss.id DESC
            LIMIT 25
            """,
            (streamer_id, streamer_id, cutoff, cutoff),
        ).fetchall()

        now_dt = datetime.now()
        sessions = []
        total_session_minutes = 0
        for sr in sessions_rows:
            c = int(sr["session_coins"] or 0)
            u_net = round(c * cfg["usd_per_diamond"], 2)
            e_net = round(u_net * cfg["usd_to_eur_rate"], 2)

            start_str = sr["started_at"]
            end_str = sr["ended_at"]
            duration_mins = 0
            try:
                s_dt = datetime.fromisoformat(start_str)
                e_dt = datetime.fromisoformat(end_str) if end_str else now_dt
                duration_mins = max(1, int(round((e_dt - s_dt).total_seconds() / 60)))
            except Exception:
                duration_mins = 0

            total_session_minutes += duration_mins
            coins_per_hour = round(c / (duration_mins / 60)) if duration_mins > 0 else c

            sessions.append({
                "id": sr["id"],
                "streamer_id": sr["streamer_id"],
                "streamer_username": sr["streamer_username"],
                "streamer_display_name": sr["streamer_display_name"] or sr["streamer_username"],
                "room_id": sr["room_id"],
                "title": sr["title"],
                "started_at": sr["started_at"],
                "ended_at": sr["ended_at"],
                "duration_minutes": duration_mins,
                "peak_viewers": int(sr["peak_viewers"] or 0),
                "status": sr["status"],
                "session_coins": c,
                "eur_net": e_net,
                "usd_net": u_net,
                "session_gifts": int(sr["session_gifts"] or 0),
                "coins_per_hour": coins_per_hour,
            })

    return {
        "period": period,
        "selected_streamer_id": streamer_id,
        "selected_streamer": selected_streamer,
        "totals": {
            "total_coins": total_coins,
            "eur_net": eur_net,
            "usd_net": usd_net,
            "eur_gross": eur_gross,
            "usd_gross": usd_gross,
            "total_gifts_sent": total_gifts,
            "gift_events_count": gift_events_cnt,
            "unique_gifters": unique_gifters,
            "avg_coins_per_gift": avg_coins_per_gift,
            "avg_coins_per_gifter": avg_coins_per_gifter,
            "avg_eur_per_gifter": avg_eur_per_gifter,
            "whale_share_top1_pct": whale_share_top1,
            "whale_share_top3_pct": whale_share_top3,
            "whale_share_top5_pct": whale_share_top5,
            "sessions_count": len(sessions),
            "total_stream_minutes": total_session_minutes,
        },
        "timeline": timeline,
        "multi_streamer_datasets": multi_streamer_datasets,
        "time_of_day": time_of_day,
        "top_gifts": top_gifts,
        "top_gifters": top_gifters,
        "streamers_ranking": streamers_ranking,
        "sessions": sessions,
    }


def build_streamer_bot_report(
    streamer_row: Dict[str, Any],
    hours: int = 24,
    is_tracking_active: bool = False,
    worker_state: str = "stopped",
) -> Dict[str, Any]:
    cfg = get_settings()
    cutoff = (datetime.now() - timedelta(hours=max(1, hours))).isoformat(timespec="seconds")
    sid = int(streamer_row["id"])

    with get_db() as conn:
        period_agg = conn.execute(
            """
            SELECT
                COALESCE(SUM(total_coins), 0) AS coins,
                COALESCE(SUM(repeat_count), 0) AS gifts_sent,
                COUNT(id) AS gift_events,
                COUNT(DISTINCT sender_username) AS unique_gifters
            FROM gift_events
            WHERE streamer_id = ? AND created_at >= ?
            """,
            (sid, cutoff),
        ).fetchone()

        alltime_agg = conn.execute(
            """
            SELECT COALESCE(SUM(total_coins), 0) AS coins
            FROM gift_events
            WHERE streamer_id = ?
            """,
            (sid,),
        ).fetchone()

        sess_agg = conn.execute(
            """
            SELECT
                COUNT(id) AS sessions_count,
                COALESCE(MAX(peak_viewers), 0) AS max_peak_viewers
            FROM stream_sessions
            WHERE streamer_id = ? AND (started_at >= ? OR status = 'active')
            """,
            (sid, cutoff),
        ).fetchone()

        top_gifts_rows = conn.execute(
            """
            SELECT
                gift_name,
                diamond_count,
                SUM(repeat_count) AS total_quantity,
                SUM(total_coins) AS total_coins,
                COUNT(id) AS events_count
            FROM gift_events
            WHERE streamer_id = ? AND created_at >= ?
            GROUP BY gift_name, diamond_count
            ORDER BY total_coins DESC
            LIMIT 5
            """,
            (sid, cutoff),
        ).fetchall()

        top_gifters_rows = conn.execute(
            """
            SELECT
                sender_username,
                MAX(sender_nickname) AS sender_nickname,
                MAX(is_super_fan) AS is_super_fan,
                SUM(total_coins) AS total_coins,
                SUM(repeat_count) AS gifts_sent,
                COUNT(id) AS transactions
            FROM gift_events
            WHERE streamer_id = ? AND created_at >= ?
            GROUP BY sender_username
            ORDER BY total_coins DESC
            LIMIT 5
            """,
            (sid, cutoff),
        ).fetchall()

    p_coins = int(period_agg["coins"] or 0)
    p_usd_net = round(p_coins * cfg["usd_per_diamond"], 2)
    p_eur_net = round(p_usd_net * cfg["usd_to_eur_rate"], 2)
    p_usd_gross = round(p_coins * cfg["gross_usd_per_coin"], 2)
    p_eur_gross = round(p_usd_gross * cfg["usd_to_eur_rate"], 2)

    all_coins = int(alltime_agg["coins"] or 0)
    all_usd_net = round(all_coins * cfg["usd_per_diamond"], 2)
    all_eur_net = round(all_usd_net * cfg["usd_to_eur_rate"], 2)

    top_gifts = []
    for g in top_gifts_rows:
        c = int(g["total_coins"] or 0)
        u_net = round(c * cfg["usd_per_diamond"], 2)
        e_net = round(u_net * cfg["usd_to_eur_rate"], 2)
        top_gifts.append(
            {
                "gift_name": g["gift_name"],
                "diamond_count": int(g["diamond_count"] or 0),
                "total_quantity": int(g["total_quantity"] or 0),
                "total_coins": c,
                "events_count": int(g["events_count"] or 0),
                "usd_net": u_net,
                "eur_net": e_net,
            }
        )

    top_gifters = []
    for tg in top_gifters_rows:
        c = int(tg["total_coins"] or 0)
        u_net = round(c * cfg["usd_per_diamond"], 2)
        e_net = round(u_net * cfg["usd_to_eur_rate"], 2)
        top_gifters.append(
            {
                "sender_username": tg["sender_username"],
                "sender_nickname": tg["sender_nickname"] or tg["sender_username"],
                "is_super_fan": bool(tg["is_super_fan"]),
                "total_coins": c,
                "gifts_sent": int(tg["gifts_sent"] or 0),
                "transactions": int(tg["transactions"] or 0),
                "usd_net": u_net,
                "eur_net": e_net,
            }
        )

    uname = streamer_row["username"]
    dname = streamer_row["display_name"] or uname
    live_icon = "🟢 LIVE" if streamer_row.get("is_live") else "⚪ Offline"
    top_gift_str = (
        f"{top_gifts[0]['gift_name']} ({top_gifts[0]['total_coins']:,} Coins)"
        if top_gifts
        else "Keine"
    )
    top_gifter_str = (
        f"@{top_gifters[0]['sender_username']} ({top_gifters[0]['total_coins']:,} Coins)"
        if top_gifters
        else "Keine"
    )

    telegram_text = (
        f"📊 *{dname}* (@{uname}) [{live_icon}]\n"
        f"• Zeitraum ({hours}h): *{p_eur_net:,.2f} €* (${p_usd_net:,.2f}) | *{p_coins:,} Coins*\n"
        f"• Brutto-Kaufwert ({hours}h): {p_eur_gross:,.2f} € | Gifts: {int(period_agg['gifts_sent'] or 0)} ({int(period_agg['unique_gifters'] or 0)} Gifter)\n"
        f"• Top Gift: {top_gift_str}\n"
        f"• Top Spender: {top_gifter_str}\n"
        f"• Gesamt (All-Time): {all_eur_net:,.2f} € ({all_coins:,} Coins)"
    )

    return {
        "streamer_id": sid,
        "username": uname,
        "display_name": dname,
        "is_live": bool(streamer_row.get("is_live")),
        "is_tracking_active": is_tracking_active,
        "worker_state": worker_state,
        "current_viewers": int(streamer_row.get("current_viewers") or 0),
        "follower_count": int(streamer_row.get("follower_count") or 0),
        "period_hours": hours,
        "period_coins": p_coins,
        "period_gifts_sent": int(period_agg["gifts_sent"] or 0),
        "period_gift_events": int(period_agg["gift_events"] or 0),
        "period_unique_gifters": int(period_agg["unique_gifters"] or 0),
        "period_usd_net": p_usd_net,
        "period_eur_net": p_eur_net,
        "period_usd_gross": p_usd_gross,
        "period_eur_gross": p_eur_gross,
        "period_sessions_count": int(sess_agg["sessions_count"] or 0),
        "period_peak_viewers": max(
            int(sess_agg["max_peak_viewers"] or 0),
            int(streamer_row.get("current_viewers") or 0),
        ),
        "alltime_coins": all_coins,
        "alltime_usd_net": all_usd_net,
        "alltime_eur_net": all_eur_net,
        "top_gifts": top_gifts,
        "top_gifters": top_gifters,
        "telegram_text": telegram_text,
    }


def record_battle_start(
    streamer_id: int,
    battle_id: str,
    opponent_username: str = "",
    opponent_display_name: str = "",
    opponent_avatar_url: str = "",
    opponent_user_id: str = "",
    session_id: Optional[int] = None,
    duration_seconds: int = 300,
    started_at: Optional[str] = None,
    battle_mode: str = "1v1",
    teammate_user_id: str = "",
    teammate_username: str = "",
    teammate_display_name: str = "",
    teammate_avatar_url: str = "",
    opponent2_user_id: str = "",
    opponent2_username: str = "",
    opponent2_display_name: str = "",
    opponent2_avatar_url: str = "",
    opponent3_user_id: str = "",
    opponent3_username: str = "",
    opponent3_display_name: str = "",
    opponent3_avatar_url: str = "",
    streamer_rank: int = 0,
) -> int:
    """
    Registriert den Start eines TikTok Battles (1v1, 2v2, 1v1v1 Dreier oder 1v1v1v1 4er-Einzel) in der Datenbank.
    Gibt die lokale Datenbank-ID (id) des Battle-Eintrags zurück.
    """
    started_at = started_at or datetime.now().isoformat(timespec="seconds")
    created_at = started_at

    with get_db() as conn:
        existing = conn.execute(
            """
            SELECT id, battle_mode, opponent_username, teammate_username, opponent2_username, opponent3_username FROM tiktok_battles
            WHERE streamer_id = ? AND battle_id = ? AND status = 'active'
            ORDER BY id DESC LIMIT 1
            """,
            (streamer_id, str(battle_id)),
        ).fetchone()
        if existing:
            # Nachträgliche Aktualisierung falls Partner/Gegner erst jetzt per Event bekannt wurden
            conn.execute(
                """
                UPDATE tiktok_battles
                SET battle_mode = CASE WHEN (battle_mode IS NULL OR battle_mode = '1v1') AND ? != '1v1' THEN ? ELSE battle_mode END,
                    opponent_username = CASE WHEN (opponent_username IS NULL OR opponent_username = '') THEN ? ELSE opponent_username END,
                    opponent_display_name = CASE WHEN (opponent_display_name IS NULL OR opponent_display_name = '') THEN ? ELSE opponent_display_name END,
                    opponent_avatar_url = CASE WHEN (opponent_avatar_url IS NULL OR opponent_avatar_url = '') THEN ? ELSE opponent_avatar_url END,
                    opponent_user_id = CASE WHEN (opponent_user_id IS NULL OR opponent_user_id = '') THEN ? ELSE opponent_user_id END,
                    teammate_username = CASE WHEN (teammate_username IS NULL OR teammate_username = '') THEN ? ELSE teammate_username END,
                    teammate_display_name = CASE WHEN (teammate_display_name IS NULL OR teammate_display_name = '') THEN ? ELSE teammate_display_name END,
                    teammate_avatar_url = CASE WHEN (teammate_avatar_url IS NULL OR teammate_avatar_url = '') THEN ? ELSE teammate_avatar_url END,
                    teammate_user_id = CASE WHEN (teammate_user_id IS NULL OR teammate_user_id = '') THEN ? ELSE teammate_user_id END,
                    opponent2_username = CASE WHEN (opponent2_username IS NULL OR opponent2_username = '') THEN ? ELSE opponent2_username END,
                    opponent2_display_name = CASE WHEN (opponent2_display_name IS NULL OR opponent2_display_name = '') THEN ? ELSE opponent2_display_name END,
                    opponent2_avatar_url = CASE WHEN (opponent2_avatar_url IS NULL OR opponent2_avatar_url = '') THEN ? ELSE opponent2_avatar_url END,
                    opponent2_user_id = CASE WHEN (opponent2_user_id IS NULL OR opponent2_user_id = '') THEN ? ELSE opponent2_user_id END,
                    opponent3_username = CASE WHEN (opponent3_username IS NULL OR opponent3_username = '') THEN ? ELSE opponent3_username END,
                    opponent3_display_name = CASE WHEN (opponent3_display_name IS NULL OR opponent3_display_name = '') THEN ? ELSE opponent3_display_name END,
                    opponent3_avatar_url = CASE WHEN (opponent3_avatar_url IS NULL OR opponent3_avatar_url = '') THEN ? ELSE opponent3_avatar_url END,
                    opponent3_user_id = CASE WHEN (opponent3_user_id IS NULL OR opponent3_user_id = '') THEN ? ELSE opponent3_user_id END,
                    streamer_rank = CASE WHEN (streamer_rank IS NULL OR streamer_rank = 0) AND ? > 0 THEN ? ELSE streamer_rank END
                WHERE id = ?
                """,
                (
                    battle_mode, battle_mode,
                    str(opponent_username or ""), str(opponent_display_name or opponent_username or ""),
                    str(opponent_avatar_url or ""), str(opponent_user_id or ""),
                    str(teammate_username or ""), str(teammate_display_name or teammate_username or ""),
                    str(teammate_avatar_url or ""), str(teammate_user_id or ""),
                    str(opponent2_username or ""), str(opponent2_display_name or opponent2_username or ""),
                    str(opponent2_avatar_url or ""), str(opponent2_user_id or ""),
                    str(opponent3_username or ""), str(opponent3_display_name or opponent3_username or ""),
                    str(opponent3_avatar_url or ""), str(opponent3_user_id or ""),
                    int(streamer_rank or 0), int(streamer_rank or 0),
                    int(existing["id"]),
                ),
            )
            return int(existing["id"])

        cursor = conn.execute(
            """
            INSERT INTO tiktok_battles (
                streamer_id, session_id, battle_id, opponent_user_id,
                opponent_username, opponent_display_name, opponent_avatar_url,
                started_at, duration_seconds, streamer_score, opponent_score,
                result, status, created_at,
                battle_mode, teammate_user_id, teammate_username, teammate_display_name,
                teammate_avatar_url, teammate_score, opponent2_user_id, opponent2_username,
                opponent2_display_name, opponent2_avatar_url, opponent2_score,
                opponent3_user_id, opponent3_username, opponent3_display_name,
                opponent3_avatar_url, opponent3_score, streamer_rank
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, 0, 0,
                'ONGOING', 'active', ?,
                ?, ?, ?, ?,
                ?, 0, ?, ?,
                ?, ?, 0,
                ?, ?, ?,
                ?, 0, ?
            )
            """,
            (
                streamer_id,
                session_id,
                str(battle_id),
                str(opponent_user_id or ""),
                str(opponent_username or ""),
                str(opponent_display_name or opponent_username or ""),
                str(opponent_avatar_url or ""),
                started_at,
                int(duration_seconds or 300),
                created_at,
                str(battle_mode or "1v1"),
                str(teammate_user_id or ""),
                str(teammate_username or ""),
                str(teammate_display_name or teammate_username or ""),
                str(teammate_avatar_url or ""),
                str(opponent2_user_id or ""),
                str(opponent2_username or ""),
                str(opponent2_display_name or opponent2_username or ""),
                str(opponent2_avatar_url or ""),
                str(opponent3_user_id or ""),
                str(opponent3_username or ""),
                str(opponent3_display_name or opponent3_username or ""),
                str(opponent3_avatar_url or ""),
                int(streamer_rank or 0),
            ),
        )
        return int(cursor.lastrowid)


def update_battle_scores(
    battle_db_id: int,
    streamer_score: int,
    opponent_score: int,
    streamer_mvp_name: Optional[str] = None,
    streamer_mvp_score: Optional[int] = None,
    streamer_mvp_avatar: Optional[str] = None,
    opponent_mvp_name: Optional[str] = None,
    opponent_mvp_score: Optional[int] = None,
    opponent_mvp_avatar: Optional[str] = None,
    teammate_score: Optional[int] = None,
    opponent2_score: Optional[int] = None,
    opponent3_score: Optional[int] = None,
    streamer_rank: Optional[int] = None,
    battle_mode: Optional[str] = None,
    raw_data_json: Optional[str] = None,
) -> None:
    """
    Aktualisiert fortlaufende Zwischenstände und MVP-Spender während eines aktiven Battles (1v1, 2v2, 1v1v1 oder 1v1v1v1).
    """
    with get_db() as conn:
        conn.execute(
            """
            UPDATE tiktok_battles
            SET streamer_score = ?,
                opponent_score = ?,
                teammate_score = COALESCE(?, teammate_score),
                opponent2_score = COALESCE(?, opponent2_score),
                opponent3_score = COALESCE(?, opponent3_score),
                streamer_rank = COALESCE(?, streamer_rank),
                battle_mode = COALESCE(?, battle_mode),
                streamer_mvp_name = COALESCE(?, streamer_mvp_name),
                streamer_mvp_score = COALESCE(?, streamer_mvp_score),
                streamer_mvp_avatar = COALESCE(?, streamer_mvp_avatar),
                opponent_mvp_name = COALESCE(?, opponent_mvp_name),
                opponent_mvp_score = COALESCE(?, opponent_mvp_score),
                opponent_mvp_avatar = COALESCE(?, opponent_mvp_avatar),
                raw_data_json = COALESCE(?, raw_data_json)
            WHERE id = ?
            """,
            (
                int(streamer_score),
                int(opponent_score),
                teammate_score,
                opponent2_score,
                opponent3_score,
                streamer_rank,
                battle_mode,
                streamer_mvp_name,
                streamer_mvp_score,
                streamer_mvp_avatar,
                opponent_mvp_name,
                opponent_mvp_score,
                opponent_mvp_avatar,
                raw_data_json,
                battle_db_id,
            ),
        )


def record_battle_finish(
    battle_db_id: int,
    streamer_score: int,
    opponent_score: int,
    result: Optional[str] = None,
    streamer_mvp_name: str = "",
    streamer_mvp_score: int = 0,
    streamer_mvp_avatar: str = "",
    opponent_mvp_name: str = "",
    opponent_mvp_score: int = 0,
    opponent_mvp_avatar: str = "",
    teammate_score: Optional[int] = None,
    opponent2_score: Optional[int] = None,
    opponent3_score: Optional[int] = None,
    streamer_rank: Optional[int] = None,
    battle_mode: Optional[str] = None,
    raw_data_json: Optional[str] = None,
    ended_at: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Schließt ein Battle ab (1v1, 2v2, 1v1v1 oder 1v1v1v1), ermittelt Sieger/Verlierer/Platzierung,
    aggregiert während des Duells generierte Geschenke/Coins/EUR und speichert den Endstand.
    """
    ended_at = ended_at or datetime.now().isoformat(timespec="seconds")
    cfg = get_settings()

    # Platzierungs- und Ergebnisermittlung für Free-for-all Modi (1v1v1 und 1v1v1v1)
    if battle_mode in ("1v1v1", "1v1v1v1"):
        scores = [("streamer", streamer_score), ("opp1", opponent_score), ("opp2", opponent2_score or 0)]
        if battle_mode == "1v1v1v1":
            scores.append(("opp3", opponent3_score or 0))
        scores_sorted = sorted(scores, key=lambda x: x[1], reverse=True)
        computed_rank = 1
        for idx, (who, _) in enumerate(scores_sorted):
            if who == "streamer":
                computed_rank = idx + 1
                break
        streamer_rank = streamer_rank or computed_rank

        if not result or result == "ONGOING":
            if streamer_rank == 1:
                # Prüfen auf Gleichstand mit Platz 2
                if len(scores_sorted) > 1 and scores_sorted[0][1] == scores_sorted[1][1] and scores_sorted[0][1] > 0:
                    result = "DRAW"
                else:
                    result = "WIN"
            else:
                result = "LOSE"
    elif not result or result == "ONGOING":
        if streamer_score > opponent_score:
            result = "WIN"
        elif streamer_score < opponent_score:
            result = "LOSE"
        else:
            result = "DRAW"

    with get_db() as conn:
        b_row = conn.execute(
            "SELECT * FROM tiktok_battles WHERE id = ?", (battle_db_id,)
        ).fetchone()
        if not b_row:
            return {}

        streamer_id = b_row["streamer_id"]
        started_at = b_row["started_at"]

        # Gifts während des Duell-Fensters aufsummieren
        gift_agg = conn.execute(
            """
            SELECT COALESCE(SUM(total_coins), 0) AS coins
            FROM gift_events
            WHERE streamer_id = ? AND created_at >= ? AND created_at <= ?
            """,
            (streamer_id, started_at, ended_at),
        ).fetchone()

        coins_earned = int(gift_agg["coins"] or 0) if gift_agg else 0
        usd_net = round(coins_earned * cfg["usd_per_diamond"], 2)
        eur_net = round(usd_net * cfg["usd_to_eur_rate"], 2)
        usd_gross = round(coins_earned * cfg["gross_usd_per_coin"], 2)
        eur_gross = round(usd_gross * cfg["usd_to_eur_rate"], 2)

        duration_sec = b_row["duration_seconds"] or 300
        try:
            s_dt = datetime.fromisoformat(started_at)
            e_dt = datetime.fromisoformat(ended_at)
            duration_sec = max(1, int(round((e_dt - s_dt).total_seconds())))
        except Exception:
            pass

        conn.execute(
            """
            UPDATE tiktok_battles
            SET streamer_score = ?,
                opponent_score = ?,
                teammate_score = COALESCE(?, teammate_score),
                opponent2_score = COALESCE(?, opponent2_score),
                opponent3_score = COALESCE(?, opponent3_score),
                streamer_rank = COALESCE(?, streamer_rank),
                battle_mode = COALESCE(?, battle_mode),
                result = ?,
                streamer_mvp_name = ?,
                streamer_mvp_score = ?,
                streamer_mvp_avatar = ?,
                opponent_mvp_name = ?,
                opponent_mvp_score = ?,
                opponent_mvp_avatar = ?,
                coins_earned = ?,
                usd_net = ?,
                eur_net = ?,
                usd_gross = ?,
                eur_gross = ?,
                duration_seconds = ?,
                ended_at = ?,
                status = 'finished',
                raw_data_json = COALESCE(?, raw_data_json)
            WHERE id = ?
            """,
            (
                int(streamer_score),
                int(opponent_score),
                teammate_score,
                opponent2_score,
                opponent3_score,
                streamer_rank,
                battle_mode,
                result,
                streamer_mvp_name or b_row["streamer_mvp_name"] or "",
                int(streamer_mvp_score or b_row["streamer_mvp_score"] or 0),
                streamer_mvp_avatar or b_row["streamer_mvp_avatar"] or "",
                opponent_mvp_name or b_row["opponent_mvp_name"] or "",
                int(opponent_mvp_score or b_row["opponent_mvp_score"] or 0),
                opponent_mvp_avatar or b_row["opponent_mvp_avatar"] or "",
                coins_earned,
                usd_net,
                eur_net,
                usd_gross,
                eur_gross,
                duration_sec,
                ended_at,
                raw_data_json,
                battle_db_id,
            ),
        )

        updated_row = conn.execute(
            "SELECT * FROM tiktok_battles WHERE id = ?", (battle_db_id,)
        ).fetchone()
        return dict(updated_row) if updated_row else {}


def get_battles_list(
    streamer_id: Optional[int] = None,
    period: str = "all",
    result_filter: Optional[str] = None,
    mode_filter: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Liefert paginierte Liste aller erfassten Battles mit Streamer- und Gegnerdetails.
    Unterstützt 1v1, 2v2, 1v1v1 und 1v1v1v1 Matches und gezielte Filterung nach Modus.
    """
    cutoff = _get_period_cutoff(period)
    where_clauses = []
    params = []

    if streamer_id:
        where_clauses.append("b.streamer_id = ?")
        params.append(streamer_id)
    if cutoff:
        where_clauses.append("b.created_at >= ?")
        params.append(cutoff)
    if result_filter and result_filter.upper() != "ALL":
        where_clauses.append("b.result = ?")
        params.append(result_filter.upper())
    if mode_filter and mode_filter.upper() != "ALL":
        where_clauses.append("b.battle_mode = ?")
        params.append(mode_filter.lower())
    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        where_clauses.append(
            "(LOWER(b.opponent_username) LIKE ? OR LOWER(b.opponent_display_name) LIKE ? "
            "OR LOWER(b.teammate_username) LIKE ? OR LOWER(b.teammate_display_name) LIKE ? "
            "OR LOWER(b.opponent2_username) LIKE ? OR LOWER(b.opponent2_display_name) LIKE ? "
            "OR LOWER(b.opponent3_username) LIKE ? OR LOWER(b.opponent3_display_name) LIKE ?)"
        )
        params.extend([term, term, term, term, term, term, term, term])

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    with get_db() as conn:
        count_row = conn.execute(
            f"SELECT COUNT(b.id) AS cnt FROM tiktok_battles b {where_sql}",
            tuple(params),
        ).fetchone()
        total_count = int(count_row["cnt"] or 0) if count_row else 0

        query_sql = f"""
            SELECT
                b.*,
                s.username AS streamer_username,
                s.display_name AS streamer_display_name,
                s.avatar_url AS streamer_avatar_url
            FROM tiktok_battles b
            JOIN streamers s ON s.id = b.streamer_id
            {where_sql}
            ORDER BY b.started_at DESC, b.id DESC
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(query_sql, tuple(params + [limit, offset])).fetchall()
        battles = [dict(r) for r in rows]

    return {
        "total": total_count,
        "limit": limit,
        "offset": offset,
        "battles": battles,
    }


def get_battle_stats(
    streamer_id: Optional[int] = None,
    period: str = "all",
) -> Dict[str, Any]:
    """
    Liefert aggregierte Duell-Kennzahlen (Winrate, Duelle gesamt, Einnahmen, Top-Rivale).
    """
    cutoff = _get_period_cutoff(period)

    where_clauses = []
    params = []
    if streamer_id:
        where_clauses.append("streamer_id = ?")
        params.append(streamer_id)
    if cutoff:
        where_clauses.append("created_at >= ?")
        params.append(cutoff)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    with get_db() as conn:
        agg = conn.execute(
            f"""
            SELECT
                COUNT(id) AS total_battles,
                COALESCE(SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN result = 'LOSE' THEN 1 ELSE 0 END), 0) AS losses,
                COALESCE(SUM(CASE WHEN result = 'DRAW' THEN 1 ELSE 0 END), 0) AS draws,
                COALESCE(SUM(CASE WHEN status = 'active' OR result = 'ONGOING' THEN 1 ELSE 0 END), 0) AS ongoing,
                COALESCE(SUM(coins_earned), 0) AS total_coins,
                COALESCE(SUM(eur_net), 0.0) AS total_eur_net,
                COALESCE(MAX(streamer_score), 0) AS max_streamer_score
            FROM tiktok_battles
            {where_sql}
            """,
            tuple(params),
        ).fetchone()

        total = int(agg["total_battles"] or 0)
        wins = int(agg["wins"] or 0)
        losses = int(agg["losses"] or 0)
        draws = int(agg["draws"] or 0)
        ongoing = int(agg["ongoing"] or 0)
        finished = wins + losses + draws
        winrate = round((wins / finished * 100), 1) if finished > 0 else 0.0

        # Top Rivals (Häufige Gegner & Head-to-Head)
        rival_where = ("WHERE " + " AND ".join(where_clauses + ["opponent_username != ''"])) if where_clauses else "WHERE opponent_username != ''"
        rival_rows = conn.execute(
            f"""
            SELECT
                opponent_username,
                MAX(opponent_display_name) AS opponent_display_name,
                MAX(opponent_avatar_url) AS opponent_avatar_url,
                COUNT(id) AS matches_count,
                COALESCE(SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN result = 'LOSE' THEN 1 ELSE 0 END), 0) AS losses,
                COALESCE(SUM(CASE WHEN result = 'DRAW' THEN 1 ELSE 0 END), 0) AS draws,
                COALESCE(SUM(coins_earned), 0) AS total_coins,
                COALESCE(SUM(eur_net), 0.0) AS total_eur_net,
                AVG(streamer_score) AS avg_streamer_score,
                AVG(opponent_score) AS avg_opponent_score
            FROM tiktok_battles
            {rival_where}
            GROUP BY opponent_username
            ORDER BY matches_count DESC, total_coins DESC
            LIMIT 10
            """,
            tuple(params),
        ).fetchall()

        rivals = []
        for r in rival_rows:
            m_cnt = int(r["matches_count"] or 0)
            w_cnt = int(r["wins"] or 0)
            r_fin = w_cnt + int(r["losses"] or 0) + int(r["draws"] or 0)
            r_winrate = round((w_cnt / r_fin * 100), 1) if r_fin > 0 else 0.0
            rivals.append({
                "opponent_username": r["opponent_username"],
                "opponent_display_name": r["opponent_display_name"] or r["opponent_username"],
                "opponent_avatar_url": r["opponent_avatar_url"] or "",
                "matches_count": m_cnt,
                "wins": w_cnt,
                "losses": int(r["losses"] or 0),
                "draws": int(r["draws"] or 0),
                "winrate_pct": r_winrate,
                "total_coins": int(r["total_coins"] or 0),
                "total_eur_net": round(float(r["total_eur_net"] or 0.0), 2),
                "avg_streamer_score": int(round(r["avg_streamer_score"] or 0)),
                "avg_opponent_score": int(round(r["avg_opponent_score"] or 0)),
            })

    return {
        "total_battles": total,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "ongoing": ongoing,
        "winrate_pct": winrate,
        "total_coins": int(agg["total_coins"] or 0),
        "total_eur_net": round(float(agg["total_eur_net"] or 0.0), 2),
        "max_streamer_score": int(agg["max_streamer_score"] or 0),
        "top_opponent": rivals[0] if rivals else None,
        "rivals": rivals,
    }


def get_battle_details(battle_id: int) -> Optional[Dict[str, Any]]:
    """
    Liefert vollständige Details eines Battles inkl. aller Geschenke, die während des Matches empfangen wurden.
    """
    with get_db() as conn:
        b_row = conn.execute(
            """
            SELECT
                b.*,
                s.username AS streamer_username,
                s.display_name AS streamer_display_name,
                s.avatar_url AS streamer_avatar_url
            FROM tiktok_battles b
            JOIN streamers s ON s.id = b.streamer_id
            WHERE b.id = ?
            """,
            (battle_id,),
        ).fetchone()
        if not b_row:
            return None

        battle = dict(b_row)

        gifts = []
        if battle.get("started_at") and battle.get("ended_at"):
            g_rows = conn.execute(
                """
                SELECT * FROM gift_events
                WHERE streamer_id = ? AND created_at >= ? AND created_at <= ?
                ORDER BY total_coins DESC, id DESC
                LIMIT 50
                """,
                (battle["streamer_id"], battle["started_at"], battle["ended_at"]),
            ).fetchall()
            gifts = [dict(g) for g in g_rows]

        battle["gifts"] = gifts
        return battle


def seed_demo_data() -> None:
    demo_streamers = [
        {"username": "montanablack", "display_name": "MontanaBlack", "follower_count": 2950000},
        {"username": "eliasn97", "display_name": "EliasN97", "follower_count": 1850000},
        {"username": "papaplatte", "display_name": "Papaplatte", "follower_count": 1420000},
    ]
    now = datetime.now()
    streamer_ids = {}
    for s in demo_streamers:
        row = upsert_streamer(
            username=s["username"],
            display_name=s["display_name"],
            follower_count=s["follower_count"],
        )
        streamer_ids[s["username"]] = row["id"]

    sample_gifts = [
        ("montanablack", "king_donator99", "KingDonator", "TikTok Universe", 44999, 1, 1),
        ("montanablack", "lukas_hh", "Lukas HH", "Lion", 29999, 1, 2),
        ("montanablack", "max_power", "MaxPower", "Galaxy", 1000, 12, 6),
        ("eliasn97", "fokus_fan1", "FOKUS Ultra", "Lion", 29999, 1, 2),
        ("eliasn97", "berlin_boy", "BerlinBoy", "Sports Car", 7000, 3, 5),
        ("papaplatte", "domo_enjoyer", "DomoEnjoyer", "Falcon", 10999, 2, 12),
    ]
    for idx, (uname, sender, nick, gname, diamonds, repeat, hours_ago) in enumerate(sample_gifts):
        ts = (now - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        record_gift_event(
            streamer_id=streamer_ids[uname],
            sender_username=sender,
            sender_nickname=nick,
            gift_name=gname,
            diamond_count=diamonds,
            repeat_count=repeat,
            event_uid=f"demo-{uname}-{idx}",
            source="demo",
            created_at=ts,
        )


def clear_demo_data() -> int:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM gift_events WHERE source = 'demo'")
        return cur.rowcount


# =====================================================================
# SQLITE DATENBANK IMPORTER & INSPECTOR (WEBINTERFACE)
# =====================================================================

def inspect_sqlite_database(sqlite_file_path: str) -> Dict[str, Any]:
    """
    Untersucht eine SQLite-Datei und liefert Informationen über Tabellen,
    Zeilenzahlen und Datengröße zurück.
    """
    if not os.path.exists(sqlite_file_path):
        raise FileNotFoundError(f"SQLite-Datei nicht gefunden: {sqlite_file_path}")

    file_size_bytes = os.path.getsize(sqlite_file_path)
    file_size_mb = round(file_size_bytes / (1024 * 1024), 2)

    src = sqlite3.connect(sqlite_file_path)
    src.row_factory = sqlite3.Row
    try:
        cur = src.cursor()
        existing_tables = [
            r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]

        table_counts: Dict[str, int] = {}
        for tbl in ["settings", "streamers", "stream_sessions", "gift_events", "telegram_users", "tiktok_battles"]:
            if tbl in existing_tables:
                try:
                    c = cur.execute(f"SELECT COUNT(*) FROM `{tbl}`").fetchone()[0]
                    table_counts[tbl] = int(c)
                except Exception:
                    table_counts[tbl] = 0
            else:
                table_counts[tbl] = 0

        # Ermittle gefundene Streamer
        streamer_names: List[str] = []
        if "streamers" in existing_tables:
            try:
                s_rows = cur.execute("SELECT username FROM streamers ORDER BY username ASC LIMIT 30").fetchall()
                streamer_names = [r["username"] for r in s_rows]
            except Exception:
                pass

        return {
            "file_path": sqlite_file_path,
            "file_size_bytes": file_size_bytes,
            "file_size_mb": file_size_mb,
            "tables": existing_tables,
            "table_counts": table_counts,
            "streamer_samples": streamer_names,
            "total_records": sum(table_counts.values()),
        }
    finally:
        src.close()


def import_from_sqlite_database(
    sqlite_file_path: str,
    import_tables: Optional[List[str]] = None,
    mode: str = "merge",  # "merge" (REPLACE/INSERT IGNORE) oder "clean" (vorher leeren)
) -> Dict[str, Any]:
    """
    Liest Daten aus einer SQLite-Datei aus und schreibt sie in die aktuell aktive
    Ziel-Datenbank (egal ob SQLite oder MySQL / MariaDB).
    """
    if not os.path.exists(sqlite_file_path):
        raise FileNotFoundError(f"SQLite-Datei nicht gefunden: {sqlite_file_path}")

    all_supported = ["settings", "streamers", "stream_sessions", "gift_events", "telegram_users", "tiktok_battles"]
    target_tables = [t for t in (import_tables or all_supported) if t in all_supported]

    src = sqlite3.connect(sqlite_file_path)
    src.row_factory = sqlite3.Row

    imported_stats: Dict[str, int] = {t: 0 for t in target_tables}
    errors: List[str] = []

    try:
        cur_src = src.cursor()

        with get_db() as dst:
            # 1. Falls "clean", bereinige die gewählten Tabellen (in umgekehrter Fremdschlüssel-Reihenfolge)
            if mode == "clean":
                clean_order = ["gift_events", "tiktok_battles", "stream_sessions", "streamers", "telegram_users", "settings"]
                for tbl in clean_order:
                    if tbl in target_tables:
                        try:
                            dst.execute(f"DELETE FROM `{tbl}`")
                        except Exception as e:
                            errors.append(f"Fehler beim Leeren von `{tbl}`: {e}")

            # 2. SETTINGS
            if "settings" in target_tables:
                try:
                    s_rows = cur_src.execute("SELECT `key`, `value` FROM settings").fetchall()
                    for r in s_rows:
                        dst.execute(
                            "INSERT OR REPLACE INTO settings (`key`, `value`) VALUES (?, ?)",
                            (r["key"], r["value"]),
                        )
                    imported_stats["settings"] = len(s_rows)
                except Exception as e:
                    errors.append(f"Settings Import: {e}")

            # 3. STREAMERS
            if "streamers" in target_tables:
                try:
                    streamer_rows = cur_src.execute("SELECT * FROM streamers ORDER BY id ASC").fetchall()
                    for r in streamer_rows:
                        d = dict(r)
                        dst.execute(
                            """
                            INSERT OR REPLACE INTO streamers (
                                id, username, display_name, avatar_url, room_id,
                                follower_count, is_live, is_tracking, auto_monitor,
                                current_viewers, last_error, created_at, last_seen_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                d["id"],
                                d["username"],
                                d.get("display_name") or d["username"],
                                d.get("avatar_url") or "",
                                d.get("room_id") or "",
                                int(d.get("follower_count") or 0),
                                int(d.get("is_live") or 0),
                                int(d.get("is_tracking") or 0),
                                int(d.get("auto_monitor") or 0),
                                int(d.get("current_viewers") or 0),
                                d.get("last_error") or "",
                                d["created_at"],
                                d.get("last_seen_at") or d["created_at"],
                            ),
                        )
                    imported_stats["streamers"] = len(streamer_rows)
                except Exception as e:
                    errors.append(f"Streamers Import: {e}")

            # 4. STREAM SESSIONS
            if "stream_sessions" in target_tables:
                try:
                    session_rows = cur_src.execute("SELECT * FROM stream_sessions ORDER BY id ASC").fetchall()
                    for r in session_rows:
                        d = dict(r)
                        dst.execute(
                            """
                            INSERT OR REPLACE INTO stream_sessions (
                                id, streamer_id, room_id, title, started_at,
                                ended_at, peak_viewers, last_fan_tickets, status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                d["id"],
                                d["streamer_id"],
                                d.get("room_id") or "",
                                d.get("title") or "",
                                d["started_at"],
                                d.get("ended_at"),
                                int(d.get("peak_viewers") or 0),
                                int(d.get("last_fan_tickets") or 0),
                                d.get("status") or "ended",
                            ),
                        )
                    imported_stats["stream_sessions"] = len(session_rows)
                except Exception as e:
                    errors.append(f"Sessions Import: {e}")

            # 5. GIFT EVENTS
            if "gift_events" in target_tables:
                try:
                    gift_rows = cur_src.execute("SELECT * FROM gift_events ORDER BY id ASC").fetchall()
                    for r in gift_rows:
                        d = dict(r)
                        dst.execute(
                            """
                            INSERT OR IGNORE INTO gift_events (
                                id, streamer_id, session_id, event_uid,
                                sender_user_id, sender_username, sender_nickname, is_super_fan,
                                gift_id, gift_name, gift_icon_url,
                                diamond_count, repeat_count, combo_count, group_id, room_fan_tickets,
                                total_coins, usd_net, eur_net, usd_gross, eur_gross,
                                source, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                d["id"],
                                d["streamer_id"],
                                d.get("session_id"),
                                d.get("event_uid"),
                                d.get("sender_user_id") or "",
                                d["sender_username"],
                                d.get("sender_nickname") or d["sender_username"],
                                int(d.get("is_super_fan") or 0),
                                int(d.get("gift_id") or 0),
                                d["gift_name"],
                                d.get("gift_icon_url") or "",
                                int(d["diamond_count"]),
                                int(d["repeat_count"]),
                                int(d.get("combo_count") or 1),
                                d.get("group_id") or "",
                                int(d.get("room_fan_tickets") or 0),
                                int(d["total_coins"]),
                                float(d["usd_net"]),
                                float(d["eur_net"]),
                                float(d["usd_gross"]),
                                float(d["eur_gross"]),
                                d.get("source") or "tiktoklive",
                                d["created_at"],
                            ),
                        )
                    imported_stats["gift_events"] = len(gift_rows)
                except Exception as e:
                    errors.append(f"Gifts Import: {e}")

            # 6. TELEGRAM USERS
            if "telegram_users" in target_tables:
                try:
                    tg_rows = cur_src.execute("SELECT * FROM telegram_users ORDER BY id ASC").fetchall()
                    for r in tg_rows:
                        d = dict(r)
                        dst.execute(
                            """
                            INSERT OR REPLACE INTO telegram_users (
                                id, telegram_id, username, first_name,
                                role, is_allowed, note, created_at, last_seen_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                d["id"],
                                d.get("telegram_id"),
                                d.get("username") or "",
                                d.get("first_name") or "",
                                d.get("role") or "user",
                                int(d.get("is_allowed") or 0),
                                d.get("note") or "",
                                d["created_at"],
                                d.get("last_seen_at") or d["created_at"],
                            ),
                        )
                    imported_stats["telegram_users"] = len(tg_rows)
                except Exception as e:
                    errors.append(f"Telegram Users Import: {e}")

            # 7. TIKTOK BATTLES
            if "tiktok_battles" in target_tables:
                try:
                    has_battles_table = cur_src.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tiktok_battles'"
                    ).fetchone()[0] > 0
                    if has_battles_table:
                        battle_rows = cur_src.execute("SELECT * FROM tiktok_battles ORDER BY id ASC").fetchall()
                        for r in battle_rows:
                            d = dict(r)
                            dst.execute(
                                """
                                INSERT OR REPLACE INTO tiktok_battles (
                                    id, streamer_id, session_id, battle_id,
                                    opponent_user_id, opponent_username, opponent_display_name, opponent_avatar_url,
                                    started_at, ended_at, duration_seconds,
                                    streamer_score, opponent_score, result,
                                    streamer_mvp_name, streamer_mvp_score, streamer_mvp_avatar,
                                    opponent_mvp_name, opponent_mvp_score, opponent_mvp_avatar,
                                    coins_earned, usd_net, eur_net, usd_gross, eur_gross,
                                    status, raw_data_json, created_at,
                                    battle_mode,
                                    teammate_user_id, teammate_username, teammate_display_name, teammate_avatar_url, teammate_score,
                                    opponent2_user_id, opponent2_username, opponent2_display_name, opponent2_avatar_url, opponent2_score,
                                    opponent3_user_id, opponent3_username, opponent3_display_name, opponent3_avatar_url, opponent3_score,
                                    streamer_rank
                                ) VALUES (
                                    ?, ?, ?, ?,
                                    ?, ?, ?, ?,
                                    ?, ?, ?,
                                    ?, ?, ?,
                                    ?, ?, ?,
                                    ?, ?, ?,
                                    ?, ?, ?, ?, ?,
                                    ?, ?, ?,
                                    ?,
                                    ?, ?, ?, ?, ?,
                                    ?, ?, ?, ?, ?,
                                    ?, ?, ?, ?, ?,
                                    ?
                                )
                                """,
                                (
                                    d["id"],
                                    d["streamer_id"],
                                    d.get("session_id"),
                                    d.get("battle_id") or "",
                                    d.get("opponent_user_id") or "",
                                    d.get("opponent_username") or "",
                                    d.get("opponent_display_name") or "",
                                    d.get("opponent_avatar_url") or "",
                                    d["started_at"],
                                    d.get("ended_at"),
                                    int(d.get("duration_seconds") or 300),
                                    int(d.get("streamer_score") or 0),
                                    int(d.get("opponent_score") or 0),
                                    d.get("result") or "ONGOING",
                                    d.get("streamer_mvp_name") or "",
                                    int(d.get("streamer_mvp_score") or 0),
                                    d.get("streamer_mvp_avatar") or "",
                                    d.get("opponent_mvp_name") or "",
                                    int(d.get("opponent_mvp_score") or 0),
                                    d.get("opponent_mvp_avatar") or "",
                                    int(d.get("coins_earned") or 0),
                                    float(d.get("usd_net") or 0.0),
                                    float(d.get("eur_net") or 0.0),
                                    float(d.get("usd_gross") or 0.0),
                                    float(d.get("eur_gross") or 0.0),
                                    d.get("status") or "active",
                                    d.get("raw_data_json") or "",
                                    d["created_at"],
                                    d.get("battle_mode") or "1v1",
                                    d.get("teammate_user_id") or "",
                                    d.get("teammate_username") or "",
                                    d.get("teammate_display_name") or "",
                                    d.get("teammate_avatar_url") or "",
                                    int(d.get("teammate_score") or 0),
                                    d.get("opponent2_user_id") or "",
                                    d.get("opponent2_username") or "",
                                    d.get("opponent2_display_name") or "",
                                    d.get("opponent2_avatar_url") or "",
                                    int(d.get("opponent2_score") or 0),
                                    d.get("opponent3_user_id") or "",
                                    d.get("opponent3_username") or "",
                                    d.get("opponent3_display_name") or "",
                                    d.get("opponent3_avatar_url") or "",
                                    int(d.get("opponent3_score") or 0),
                                    int(d.get("streamer_rank") or 0),
                                ),
                            )
                        imported_stats["tiktok_battles"] = len(battle_rows)
                except Exception as e:
                    errors.append(f"Battles Import: {e}")

        return {
            "ok": len(errors) == 0,
            "mode": mode,
            "source_file": sqlite_file_path,
            "imported_counts": imported_stats,
            "total_imported": sum(imported_stats.values()),
            "errors": errors,
        }
    finally:
        src.close()
