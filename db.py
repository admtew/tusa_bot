"""Работа с базой (SQLite). Для MVP этого достаточно; при росте — PostgreSQL."""
import sqlite3
import time
import uuid

import config

_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init() -> None:
    c = conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            tg_id          INTEGER PRIMARY KEY,
            username       TEXT,
            first_name     TEXT,
            qtickets_token TEXT NOT NULL DEFAULT '',  -- API-токен организатора (один раз)
            created_at     INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id      INTEGER NOT NULL REFERENCES users(tg_id),
            title       TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            starts_at   INTEGER NOT NULL,          -- unix ts начала
            area        TEXT NOT NULL DEFAULT '',  -- район/метро (видно всем)
            address     TEXT NOT NULL DEFAULT '',  -- точный адрес (шлётся за N часов)
            price_text  TEXT NOT NULL DEFAULT '',  -- напр. "от 500 ₽" или "free за рефералку"
            pay_url     TEXT NOT NULL DEFAULT '',  -- ссылка на оплату (qtickets и т.п.)
            capacity    INTEGER NOT NULL DEFAULT 0,-- 0 = без лимита
            refs_needed INTEGER NOT NULL DEFAULT 0,-- сколько друзей привести за free-билет
            channel     TEXT NOT NULL DEFAULT '',  -- @канал для проверки подписки (без @)
            age_limit   TEXT NOT NULL DEFAULT '',  -- напр. "14+", "18+"
            cover       TEXT NOT NULL DEFAULT 'ember', -- ключ обложки-градиента
            city        TEXT NOT NULL DEFAULT 'Москва',
            genre       TEXT NOT NULL DEFAULT '',  -- вайб: "techno · b2b" и т.п.
            qt_event_id INTEGER NOT NULL DEFAULT 0, -- id события в qtickets (0 = не привязано)
            status      TEXT NOT NULL DEFAULT 'active', -- active | cancelled | done
            created_at  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tickets (
            code       TEXT PRIMARY KEY,           -- uuid, он же содержимое QR
            event_id   INTEGER NOT NULL REFERENCES events(id),
            user_id    INTEGER NOT NULL REFERENCES users(tg_id),
            kind       TEXT NOT NULL,              -- free | paid_pending | paid
            status     TEXT NOT NULL DEFAULT 'active', -- active | used | revoked
            qt_order   TEXT NOT NULL DEFAULT '',   -- id оплаченного заказа qtickets (дедуп)
            rem24_sent INTEGER NOT NULL DEFAULT 0,
            rem3_sent  INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            used_at    INTEGER,
            UNIQUE(event_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS referrals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    INTEGER NOT NULL REFERENCES events(id),
            referrer_id INTEGER NOT NULL,
            referred_id INTEGER NOT NULL,
            created_at  INTEGER NOT NULL,
            UNIQUE(event_id, referred_id)          -- одного человека нельзя засчитать дважды
        );
        """
    )
    # мягкие миграции для старых баз
    for ddl in (
        "ALTER TABLE events ADD COLUMN cover TEXT NOT NULL DEFAULT 'ember'",
        "ALTER TABLE events ADD COLUMN city TEXT NOT NULL DEFAULT 'Москва'",
        "ALTER TABLE events ADD COLUMN genre TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE events ADD COLUMN qt_event_id INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN qtickets_token TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE tickets ADD COLUMN qt_order TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE events ADD COLUMN cover_img BLOB",
    ):
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass
    c.commit()


def now() -> int:
    return int(time.time())


# ---------- users ----------

def upsert_user(tg_id: int, username: str | None, first_name: str | None) -> bool:
    """Возвращает True, если пользователь новый."""
    c = conn()
    row = c.execute("SELECT tg_id FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    if row:
        c.execute(
            "UPDATE users SET username=?, first_name=? WHERE tg_id=?",
            (username or "", first_name or "", tg_id),
        )
        c.commit()
        return False
    c.execute(
        "INSERT INTO users(tg_id, username, first_name, created_at) VALUES(?,?,?,?)",
        (tg_id, username or "", first_name or "", now()),
    )
    c.commit()
    return True


# ---------- events ----------

EVENT_FIELDS = (
    "title", "description", "starts_at", "area", "address",
    "price_text", "pay_url", "capacity", "refs_needed", "channel", "age_limit",
)


def create_event(org_id: int, data: dict, status: str = "active",
                  cover_img: bytes | None = None) -> int:
    c = conn()
    cur = c.execute(
        """INSERT INTO events(org_id,title,description,starts_at,area,address,
           price_text,pay_url,capacity,refs_needed,channel,age_limit,cover,city,genre,qt_event_id,
           cover_img,status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            org_id,
            data["title"], data.get("description", ""), int(data["starts_at"]),
            data.get("area", ""), data.get("address", ""),
            data.get("price_text", ""), data.get("pay_url", ""),
            int(data.get("capacity") or 0), int(data.get("refs_needed") or 0),
            data.get("channel", "").lstrip("@"), data.get("age_limit", ""),
            str(data.get("cover") or "ember"),
            str(data.get("city") or "Москва"), data.get("genre", ""),
            int(data.get("qt_event_id") or 0),
            cover_img, status,
            now(),
        ),
    )
    c.commit()
    return cur.lastrowid


def set_event_status(event_id: int, status: str) -> None:
    c = conn()
    c.execute("UPDATE events SET status=? WHERE id=?", (status, event_id))
    c.commit()


def delete_event(event_id: int, org_id: int) -> bool:
    """Мягкое удаление: только владелец."""
    c = conn()
    cur = c.execute("UPDATE events SET status='cancelled' WHERE id=? AND org_id=?",
                    (event_id, org_id))
    c.commit()
    return cur.rowcount > 0


def reschedule_event(event_id: int, org_id: int, new_starts: int, new_status: str) -> bool:
    """Перенос даты владельцем. new_status='pending' если включена модерация."""
    c = conn()
    cur = c.execute("UPDATE events SET starts_at=?, status=? WHERE id=? AND org_id=?",
                    (int(new_starts), new_status, event_id, org_id))
    c.commit()
    return cur.rowcount > 0


def get_cover(event_id: int) -> bytes | None:
    row = conn().execute("SELECT cover_img FROM events WHERE id=?", (event_id,)).fetchone()
    return row[0] if row and row[0] else None


# ---------- qtickets ----------

def set_qtickets_token(user_id: int, token: str) -> None:
    c = conn()
    c.execute("UPDATE users SET qtickets_token=? WHERE tg_id=?", (token.strip(), user_id))
    c.commit()


def events_with_qtickets() -> list[sqlite3.Row]:
    """Активные будущие события, привязанные к qtickets, с токеном организатора."""
    return conn().execute(
        f"""SELECT e.*, u.qtickets_token FROM events e JOIN users u ON u.tg_id = e.org_id
            WHERE e.status='active' AND e.qt_event_id > 0 AND u.qtickets_token != ''
              AND e.starts_at > {now() - 6 * 3600}"""
    ).fetchall()


def pending_paid_tickets(event_id: int) -> list[sqlite3.Row]:
    """Заявки 'я купил' на событии, ждущие подтверждения оплаты."""
    return conn().execute(
        "SELECT * FROM tickets WHERE event_id=? AND kind='paid_pending'", (event_id,)
    ).fetchall()


def mark_paid_by_order(code: str, qt_order: str) -> None:
    c = conn()
    c.execute("UPDATE tickets SET kind='paid', qt_order=? WHERE code=? AND kind='paid_pending'",
              (str(qt_order), code))
    c.commit()


def order_already_used(qt_order: str) -> bool:
    return conn().execute(
        "SELECT 1 FROM tickets WHERE qt_order=?", (str(qt_order),)
    ).fetchone() is not None


def create_paid_ticket_direct(event_id: int, user_id: int, qt_order: str) -> str | None:
    """Создать сразу оплаченный билет (когда заявки не было, а оплата пришла)."""
    if get_user_ticket(event_id, user_id):
        return None
    import uuid as _uuid
    code = _uuid.uuid4().hex
    c = conn()
    c.execute(
        "INSERT INTO tickets(code,event_id,user_id,kind,qt_order,created_at) VALUES(?,?,?,'paid',?,?)",
        (code, event_id, user_id, str(qt_order), now()),
    )
    c.commit()
    return code


def list_events(upcoming_only: bool = True, city: str | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM events WHERE status='active'"
    args: list = []
    if upcoming_only:
        q += f" AND starts_at > {now() - 6 * 3600}"  # показываем ещё 6ч после начала
    if city:
        q += " AND city=?"
        args.append(city)
    q += " ORDER BY starts_at ASC"
    return conn().execute(q, args).fetchall()


def city_counts() -> dict[str, int]:
    rows = conn().execute(
        f"SELECT city, COUNT(*) FROM events WHERE status='active' AND starts_at > {now() - 6 * 3600} GROUP BY city"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def get_user(tg_id: int) -> sqlite3.Row | None:
    return conn().execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


def get_event(event_id: int) -> sqlite3.Row | None:
    return conn().execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()


def org_events(org_id: int) -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM events WHERE org_id=? ORDER BY starts_at DESC", (org_id,)
    ).fetchall()


# ---------- tickets ----------

def tickets_count(event_id: int) -> int:
    return conn().execute(
        "SELECT COUNT(*) FROM tickets WHERE event_id=? AND status!='revoked' AND kind!='paid_pending'",
        (event_id,),
    ).fetchone()[0]


def get_user_ticket(event_id: int, user_id: int) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM tickets WHERE event_id=? AND user_id=?", (event_id, user_id)
    ).fetchone()


def create_ticket(event_id: int, user_id: int, kind: str) -> str:
    code = uuid.uuid4().hex
    c = conn()
    c.execute(
        "INSERT INTO tickets(code,event_id,user_id,kind,created_at) VALUES(?,?,?,?,?)",
        (code, event_id, user_id, kind, now()),
    )
    c.commit()
    return code


def user_tickets(user_id: int) -> list[sqlite3.Row]:
    return conn().execute(
        """SELECT t.*, e.title, e.starts_at, e.area, e.address, e.age_limit, e.cover
           FROM tickets t JOIN events e ON e.id = t.event_id
           WHERE t.user_id=? AND t.status!='revoked' AND e.status='active'
           ORDER BY e.starts_at ASC""",
        (user_id,),
    ).fetchall()


def event_guests(event_id: int) -> list[sqlite3.Row]:
    return conn().execute(
        """SELECT t.*, u.username, u.first_name
           FROM tickets t JOIN users u ON u.tg_id = t.user_id
           WHERE t.event_id=? ORDER BY t.created_at ASC""",
        (event_id,),
    ).fetchall()


def get_ticket(code: str) -> sqlite3.Row | None:
    return conn().execute(
        """SELECT t.*, e.org_id, e.title, e.starts_at, u.username, u.first_name
           FROM tickets t
           JOIN events e ON e.id = t.event_id
           JOIN users u ON u.tg_id = t.user_id
           WHERE t.code=?""",
        (code,),
    ).fetchone()


def set_ticket_status(code: str, status: str) -> None:
    c = conn()
    used_at = now() if status == "used" else None
    c.execute("UPDATE tickets SET status=?, used_at=? WHERE code=?", (status, used_at, code))
    c.commit()


def approve_ticket(code: str) -> None:
    c = conn()
    c.execute("UPDATE tickets SET kind='paid' WHERE code=? AND kind='paid_pending'", (code,))
    c.commit()


def mark_reminded(code: str, field: str) -> None:
    assert field in ("rem24_sent", "rem3_sent")
    c = conn()
    c.execute(f"UPDATE tickets SET {field}=1 WHERE code=?", (code,))
    c.commit()


def tickets_for_reminder(hours_before: int, flag_field: str) -> list[sqlite3.Row]:
    """Билеты ивентов, до которых осталось <= hours_before, напоминание не слалось."""
    deadline = now() + hours_before * 3600
    return conn().execute(
        f"""SELECT t.*, e.title, e.starts_at, e.area, e.address
            FROM tickets t JOIN events e ON e.id = t.event_id
            WHERE e.status='active' AND t.status='active' AND t.kind!='paid_pending'
              AND t.{flag_field}=0 AND e.starts_at <= ? AND e.starts_at > ?""",
        (deadline, now()),
    ).fetchall()


# ---------- referrals ----------

def add_referral(event_id: int, referrer_id: int, referred_id: int) -> bool:
    if referrer_id == referred_id:
        return False
    c = conn()
    try:
        c.execute(
            "INSERT INTO referrals(event_id,referrer_id,referred_id,created_at) VALUES(?,?,?,?)",
            (event_id, referrer_id, referred_id, now()),
        )
        c.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def referrals_of(event_id: int, referrer_id: int) -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM referrals WHERE event_id=? AND referrer_id=?",
        (event_id, referrer_id),
    ).fetchall()
