"""
Database module for ET Results Checker Bot.
Provides persistent storage using PostgreSQL (Neon Serverless Postgres),
with seamless fallback to local JSON files when DATABASE_URL is not configured.
"""

import json
import logging
import os
import time
from contextlib import contextmanager

try:
    import psycopg2
    import psycopg2.pool
    from psycopg2.extras import Json, execute_values
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

from config import (
    DATABASE_URL,
    DATA_DIR,
    SUBSCRIBERS_FILE,
    SUBSCRIBER_PREFS_FILE,
    LAST_RESULTS_FILE,
)

logger = logging.getLogger(__name__)

# ─── Persistent Connection Pool ──────────────────────────────────────────────
# Re-uses connections across calls instead of opening a new TCP connection on
# every database operation. minconn=1 keeps one warm connection alive; maxconn=5
# allows burst concurrency without exhausting the Supabase session pooler limit.
_pool: "psycopg2.pool.SimpleConnectionPool | None" = None


def _get_pool() -> "psycopg2.pool.SimpleConnectionPool":
    """Return (or lazily create) the global connection pool."""
    global _pool
    if _pool is None:
        if not (DATABASE_URL and PSYCOPG2_AVAILABLE):
            raise RuntimeError("DATABASE_URL is not configured or psycopg2 is not installed.")
        _pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=5,
            dsn=DATABASE_URL,
            connect_timeout=10,
        )
        logger.info("✅ Database connection pool created.")
    return _pool


def is_db_configured() -> bool:
    """Check if DATABASE_URL is set and psycopg2 is available."""
    return bool(DATABASE_URL and PSYCOPG2_AVAILABLE)


@contextmanager
def get_db_connection(max_retries: int = 3):
    """
    Context manager providing a pooled database connection.
    Automatically commits on success and rolls back on error.
    Retries on transient failures (e.g. serverless wake-up).
    """
    if not is_db_configured():
        raise RuntimeError("DATABASE_URL is not configured or psycopg2 is not installed.")

    pool = _get_pool()
    conn = None
    last_err = None

    for attempt in range(1, max_retries + 1):
        try:
            conn = pool.getconn()
            # Ensure the connection is alive (ping it)
            with conn.cursor() as ping_cur:
                ping_cur.execute("SELECT 1")
            break
        except Exception as e:
            last_err = e
            # If the connection is broken, close and remove it from pool
            if conn is not None:
                try:
                    pool.putconn(conn, close=True)
                except Exception:
                    pass
                conn = None
            logger.warning(f"DB connection attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                time.sleep(1.5 * attempt)

    if conn is None:
        raise last_err or RuntimeError("Failed to acquire database connection.")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if conn is not None:
            try:
                if conn.closed:
                    pool.putconn(conn, close=True)
                else:
                    pool.putconn(conn)
            except Exception:
                pass


def init_db():
    """Initialize database tables if they do not exist."""
    if not is_db_configured():
        logger.info("ℹ️ DATABASE_URL not set — running in local file storage mode.")
        return

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # 1. Subscribers table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS subscribers (
                        chat_id BIGINT PRIMARY KEY,
                        role_filter VARCHAR(32) DEFAULT 'all',
                        subscribed_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    );
                    ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS role_filter VARCHAR(32) DEFAULT 'all';
                """)

                # 2. Tracked results table (to prevent spam on restart)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS tracked_results (
                        id VARCHAR(64) PRIMARY KEY,
                        position TEXT,
                        location TEXT,
                        announcement TEXT,
                        description TEXT,
                        date_time TEXT,
                        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 3. Full announcement details (for Web App candidate viewer)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS announcements (
                        id VARCHAR(64) PRIMARY KEY,
                        data JSONB NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    );
                """)
        logger.info("✅ Database tables verified/initialized in Neon PostgreSQL.")
    except Exception as e:
        logger.error(f"❌ Failed to initialize database tables: {e}", exc_info=True)


def migrate_local_data():
    """
    Migrate existing local JSON files (subscribers, last results, announcements)
    into Neon Postgres if not already present.
    """
    if not is_db_configured():
        return

    logger.info("🔄 Checking for local data to migrate to Neon PostgreSQL...")

    # 1. Migrate subscribers
    if os.path.exists(SUBSCRIBERS_FILE):
        try:
            with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
                subs = json.load(f)
                if isinstance(subs, list) and subs:
                    with get_db_connection() as conn:
                        with conn.cursor() as cur:
                            execute_values(
                                cur,
                                "INSERT INTO subscribers (chat_id) VALUES %s ON CONFLICT (chat_id) DO NOTHING",
                                [(int(cid),) for cid in subs],
                            )
                    logger.info(f"✅ Migrated {len(subs)} subscriber(s) to Neon.")
        except Exception as e:
            logger.warning(f"Failed to migrate local subscribers: {e}")

    # 2. Migrate last results
    if os.path.exists(LAST_RESULTS_FILE):
        try:
            with open(LAST_RESULTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                results = list(data.values()) if isinstance(data, dict) else data
                if isinstance(results, list) and results:
                    with get_db_connection() as conn:
                        with conn.cursor() as cur:
                            tuples = [
                                (
                                    r.get("id", ""),
                                    r.get("position", ""),
                                    r.get("location", ""),
                                    r.get("announcement", ""),
                                    r.get("description", ""),
                                    r.get("date_time", ""),
                                )
                                for r in results if r.get("id")
                            ]
                            execute_values(
                                cur,
                                """
                                INSERT INTO tracked_results (id, position, location, announcement, description, date_time)
                                VALUES %s
                                ON CONFLICT (id) DO UPDATE SET
                                    position = EXCLUDED.position,
                                    location = EXCLUDED.location,
                                    announcement = EXCLUDED.announcement,
                                    description = EXCLUDED.description,
                                    date_time = EXCLUDED.date_time,
                                    updated_at = CURRENT_TIMESTAMP
                                """,
                                tuples,
                            )
                    logger.info(f"✅ Migrated {len(tuples)} tracked result(s) to Neon.")
        except Exception as e:
            logger.warning(f"Failed to migrate last_results.json: {e}")

    # 3. Migrate announcements (batch insert in a single query)
    announcements_dir = os.path.join(DATA_DIR, "announcements")
    if os.path.exists(announcements_dir):
        try:
            announcement_items = []
            for fname in os.listdir(announcements_dir):
                if fname.endswith(".json"):
                    aid = fname[:-5]
                    fpath = os.path.join(announcements_dir, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            adata = json.load(f)
                        announcement_items.append((aid, Json(adata)))
                    except Exception as e:
                        logger.warning(f"Could not load {fname} for migration: {e}")

            if announcement_items:
                with get_db_connection() as conn:
                    with conn.cursor() as cur:
                        execute_values(
                            cur,
                            """
                            INSERT INTO announcements (id, data)
                            VALUES %s
                            ON CONFLICT (id) DO UPDATE SET
                                data = EXCLUDED.data,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            announcement_items,
                        )
                logger.info(f"✅ Migrated {len(announcement_items)} announcement detail file(s) to Neon in 1 batch.")
        except Exception as e:
            logger.warning(f"Failed to migrate announcements folder: {e}")


# ─── Subscriber API ─────────────────────────────────────────────────────────

def _load_local_prefs() -> dict[str, str]:
    """Load local subscriber role preferences map {chat_id: role_filter}."""
    if os.path.exists(SUBSCRIBER_PREFS_FILE):
        try:
            with open(SUBSCRIBER_PREFS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.warning(f"Failed to read subscriber prefs: {e}")
    return {}


def _save_local_prefs(prefs: dict[str, str]):
    """Save local subscriber role preferences map to file."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SUBSCRIBER_PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write subscriber prefs: {e}")


def get_subscribers() -> set[int]:
    """Load all subscriber chat IDs from Neon (or fallback to local file)."""
    if is_db_configured():
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT chat_id FROM subscribers;")
                    rows = cur.fetchall()
                    return {row[0] for row in rows}
        except Exception as e:
            logger.error(f"Error reading subscribers from database: {e}. Falling back to file.")

    # Fallback to local file
    if os.path.exists(SUBSCRIBERS_FILE):
        try:
            with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def get_subscribers_with_filters() -> dict[int, str]:
    """
    Load all subscribers mapped to their role filter preference
    ('all', 'pilot', 'cabin', 'crew').
    """
    if is_db_configured():
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT chat_id, COALESCE(role_filter, 'all') FROM subscribers;")
                    rows = cur.fetchall()
                    return {row[0]: (row[1] or "all").lower() for row in rows}
        except Exception as e:
            logger.error(f"Error reading subscribers with filters from DB: {e}. Falling back to file.")

    # Fallback to local files
    subs = get_subscribers()
    prefs = _load_local_prefs()
    return {cid: prefs.get(str(cid), "all").lower() for cid in subs}


def get_subscriber_filter(chat_id: int) -> str:
    """Get the notification role filter for a specific chat ID."""
    if is_db_configured():
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COALESCE(role_filter, 'all') FROM subscribers WHERE chat_id = %s;", (chat_id,))
                    row = cur.fetchone()
                    if row and row[0]:
                        return row[0].lower()
        except Exception as e:
            logger.error(f"Error reading subscriber filter for {chat_id}: {e}")

    prefs = _load_local_prefs()
    return prefs.get(str(chat_id), "all").lower()


def set_subscriber_filter(chat_id: int, role_filter: str) -> bool:
    """Set notification role filter for a subscriber ('all', 'pilot', 'cabin', 'crew')."""
    role_filter = (role_filter or "all").lower()
    success = False

    if is_db_configured():
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO subscribers (chat_id, role_filter)
                        VALUES (%s, %s)
                        ON CONFLICT (chat_id) DO UPDATE SET role_filter = EXCLUDED.role_filter;
                        """,
                        (chat_id, role_filter),
                    )
            success = True
        except Exception as e:
            logger.error(f"Error setting subscriber role filter for {chat_id}: {e}")

    # Dual-write to local storage (skip add_subscriber to avoid a redundant
    # second DB INSERT — the DB write above already handles it)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        current = set()
        if os.path.exists(SUBSCRIBERS_FILE):
            with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
                current = set(json.load(f))
        current.add(chat_id)
        with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(current), f)

        prefs = _load_local_prefs()
        prefs[str(chat_id)] = role_filter
        _save_local_prefs(prefs)
        success = True
    except Exception as e:
        logger.warning(f"Could not save subscriber filter locally: {e}")

    return success


def add_subscriber(chat_id: int, role_filter: str = "all") -> bool:
    """Add a new subscriber chat ID to database and sync to local file."""
    role_filter = (role_filter or "all").lower()
    success = False
    if is_db_configured():
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO subscribers (chat_id, role_filter)
                        VALUES (%s, %s)
                        ON CONFLICT (chat_id) DO NOTHING;
                        """,
                        (chat_id, role_filter),
                    )
            success = True
        except Exception as e:
            logger.error(f"Error adding subscriber {chat_id} to database: {e}")

    # Dual-write to local file as well
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        current = set()
        if os.path.exists(SUBSCRIBERS_FILE):
            with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
                current = set(json.load(f))
        current.add(chat_id)
        with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(current), f)

        # Set default pref if not already present
        prefs = _load_local_prefs()
        if str(chat_id) not in prefs:
            prefs[str(chat_id)] = role_filter
            _save_local_prefs(prefs)

        success = True
    except Exception as e:
        logger.warning(f"Could not write subscriber to local file: {e}")

    return success


def remove_subscriber(chat_id: int) -> bool:
    """Remove a subscriber chat ID from database and local file."""
    success = False
    if is_db_configured():
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM subscribers WHERE chat_id = %s;", (chat_id,))
            success = True
        except Exception as e:
            logger.error(f"Error removing subscriber {chat_id} from database: {e}")

    # Dual-write to local file as well
    try:
        if os.path.exists(SUBSCRIBERS_FILE):
            with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
                current = set(json.load(f))
            current.discard(chat_id)
            with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
                json.dump(list(current), f)

        prefs = _load_local_prefs()
        if str(chat_id) in prefs:
            del prefs[str(chat_id)]
            _save_local_prefs(prefs)

        success = True
    except Exception as e:
        logger.warning(f"Could not update local subscribers file: {e}")

    return success


# ─── Tracked Results API ───────────────────────────────────────────────────

def get_last_results() -> dict[str, dict]:
    """Load tracked results from Neon (or fallback to local file)."""
    if is_db_configured():
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id, position, location, announcement, description, date_time FROM tracked_results;")
                    rows = cur.fetchall()
                    return {
                        row[0]: {
                            "id": row[0],
                            "position": row[1] or "",
                            "location": row[2] or "",
                            "announcement": row[3] or "",
                            "description": row[4] or "",
                            "date_time": row[5] or "",
                        }
                        for row in rows
                    }
        except Exception as e:
            logger.error(f"Error reading tracked results from database: {e}. Falling back to file.")

    # Fallback to local file
    if os.path.exists(LAST_RESULTS_FILE):
        try:
            with open(LAST_RESULTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {r["id"]: r for r in data if "id" in r}
                return data
        except Exception:
            return {}
    return {}


def save_last_results(results: dict[str, dict]):
    """Save tracked results to Neon and dual-write to local file."""
    if is_db_configured() and results:
        try:
            tuples = [
                (
                    r.get("id", ""),
                    r.get("position", ""),
                    r.get("location", ""),
                    r.get("announcement", ""),
                    r.get("description", ""),
                    r.get("date_time", ""),
                )
                for r in results.values() if r.get("id")
            ]
            if tuples:
                with get_db_connection() as conn:
                    with conn.cursor() as cur:
                        execute_values(
                            cur,
                            """
                            INSERT INTO tracked_results (id, position, location, announcement, description, date_time)
                            VALUES %s
                            ON CONFLICT (id) DO UPDATE SET
                                position = EXCLUDED.position,
                                location = EXCLUDED.location,
                                announcement = EXCLUDED.announcement,
                                description = EXCLUDED.description,
                                date_time = EXCLUDED.date_time,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            tuples,
                        )
        except Exception as e:
            logger.error(f"Error saving tracked results to database: {e}")

    # Dual-write to local file
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LAST_RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Could not save last results to local file: {e}")


# ─── Announcements API (Web Viewer) ─────────────────────────────────────────

def save_announcement(result: dict):
    """Save full announcement details (with candidate list) to Neon and local file."""
    result_id = result.get("id")
    if not result_id:
        return

    if is_db_configured():
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO announcements (id, data)
                        VALUES (%s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            data = EXCLUDED.data,
                            updated_at = CURRENT_TIMESTAMP;
                        """,
                        (result_id, Json(result)),
                    )
        except Exception as e:
            logger.error(f"Error saving announcement {result_id} to database: {e}")

    # Dual-write to local disk
    try:
        announcements_dir = os.path.join(DATA_DIR, "announcements")
        os.makedirs(announcements_dir, exist_ok=True)
        filename = os.path.join(announcements_dir, f"{result_id}.json")
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Could not save announcement to local file: {e}")


def save_announcements_batch(results: list[dict]):
    """Save multiple announcements (with candidate lists) to Neon in a single batch query."""
    if not results:
        return

    # Deduplicate by id to avoid Postgres "ON CONFLICT DO UPDATE command cannot affect row a second time" error
    deduped: dict[str, dict] = {}
    for r in results:
        if isinstance(r, dict) and r.get("id"):
            deduped[r["id"]] = r

    valid_results = list(deduped.values())
    if not valid_results:
        return

    if is_db_configured():
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    execute_values(
                        cur,
                        """
                        INSERT INTO announcements (id, data)
                        VALUES %s
                        ON CONFLICT (id) DO UPDATE SET
                            data = EXCLUDED.data,
                            updated_at = CURRENT_TIMESTAMP;
                        """,
                        [(r["id"], Json(r)) for r in valid_results],
                    )
        except Exception as e:
            logger.error(f"Error batch saving announcements to database: {e}")

    # Dual-write to local disk
    try:
        announcements_dir = os.path.join(DATA_DIR, "announcements")
        os.makedirs(announcements_dir, exist_ok=True)
        for r in valid_results:
            filename = os.path.join(announcements_dir, f"{r['id']}.json")
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Could not batch save announcements to local files: {e}")


def get_announcement(result_id: str) -> dict | None:
    """Retrieve full announcement details by ID from Neon or local disk."""
    if is_db_configured():
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT data FROM announcements WHERE id = %s;", (result_id,))
                    row = cur.fetchone()
                    if row and row[0]:
                        return row[0]

                    # Fallback: check if metadata exists in tracked_results table
                    cur.execute(
                        "SELECT position, location, announcement, description, date_time FROM tracked_results WHERE id = %s;",
                        (result_id,),
                    )
                    t_row = cur.fetchone()
                    if t_row:
                        return {
                            "id": result_id,
                            "position": t_row[0] or "",
                            "location": t_row[1] or "",
                            "announcement": t_row[2] or "",
                            "description": t_row[3] or "",
                            "date_time": t_row[4] or "",
                            "candidates": [],
                            "archived": True,
                        }
        except Exception as e:
            logger.error(f"Error fetching announcement {result_id} from database: {e}")

    # Fallback to local disk
    data_path = os.path.join(DATA_DIR, "announcements", f"{result_id}.json")
    if os.path.exists(data_path):
        try:
            with open(data_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    return None
