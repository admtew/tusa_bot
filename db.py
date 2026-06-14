"""Работа с базой (SQLite). Для MVP этого достаточно; при росте — PostgreSQL."""
import sqlite3
import time
import uuid

import config

_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.execute("PRAGMA busy_timeout=30000")  # ждать блокировку, а не падать
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA cache_size=-8000")     # 8MB кэш вместо 2MB
        _conn.execute("PRAGMA temp_store=MEMORY")    # temp таблицы в RAM
        _conn.execute("PRAGMA mmap_size=67108864")   # 64MB memory-mapped I/O
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

        -- история действий организаторов (этап 4)
        CREATE TABLE IF NOT EXISTS event_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id   INTEGER NOT NULL,
            org_id     INTEGER NOT NULL,
            action     TEXT NOT NULL,             -- create|edit|cancel|reschedule|time|broadcast
            detail     TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        );

        -- доп. фото событий, карусель (этап 6)
        CREATE TABLE IF NOT EXISTS event_photos (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            idx      INTEGER NOT NULL DEFAULT 0,
            img      BLOB NOT NULL
        );

        -- жалобы на события (этап 5)
        CREATE TABLE IF NOT EXISTS reports (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            reason     TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            UNIQUE(event_id, user_id)
        );

        -- подписки на организаторов (уведомления о новых событиях)
        CREATE TABLE IF NOT EXISTS follows (
            org_id     INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (org_id, user_id)
        );

        -- ручная правка счётчика рефералов модератором (бонус-дельта к подсчёту)
        CREATE TABLE IF NOT EXISTS referral_bonus (
            event_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            bonus      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (event_id, user_id)
        );
        """
    )
    # мягкие миграции для старых баз (идемпотентны)
    for ddl in (
        "ALTER TABLE events ADD COLUMN cover TEXT NOT NULL DEFAULT 'ember'",
        "ALTER TABLE events ADD COLUMN city TEXT NOT NULL DEFAULT 'Москва'",
        "ALTER TABLE events ADD COLUMN genre TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE events ADD COLUMN qt_event_id INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN qtickets_token TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE tickets ADD COLUMN qt_order TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE events ADD COLUMN cover_img BLOB",
        # этап 1
        "ALTER TABLE events ADD COLUMN ends_at INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN is_verified INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN verified_at INTEGER",
        # этап 2: пруф оплаты (скрин/PDF) для ручного флоу
        "ALTER TABLE tickets ADD COLUMN proof_img BLOB",
        "ALTER TABLE tickets ADD COLUMN proof_mime TEXT NOT NULL DEFAULT ''",
        # платное промо: до какого времени событие в топе афиши (0 = не продвигается)
        "ALTER TABLE events ADD COLUMN featured_until INTEGER NOT NULL DEFAULT 0",
        # ручная отметка «распродано» (работает для любого события, в т.ч. внешних)
        "ALTER TABLE events ADD COLUMN soldout INTEGER NOT NULL DEFAULT 0",
        # промокод (скидочный код, заданный организатором)
        "ALTER TABLE events ADD COLUMN promo_code TEXT NOT NULL DEFAULT ''",
    ):
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass
    # индексы под нагрузку
    for idx in (
        "CREATE INDEX IF NOT EXISTS idx_events_status_start ON events(status, starts_at)",
        "CREATE INDEX IF NOT EXISTS idx_tickets_event ON tickets(event_id)",
        "CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_log_event ON event_log(event_id)",
        "CREATE INDEX IF NOT EXISTS idx_photos_event ON event_photos(event_id, idx)",
        "CREATE INDEX IF NOT EXISTS idx_follows_org ON follows(org_id)",
    ):
        try:
            c.execute(idx)
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
        """INSERT INTO events(org_id,title,description,starts_at,ends_at,area,address,
           price_text,pay_url,capacity,refs_needed,channel,age_limit,cover,city,genre,qt_event_id,
           cover_img,status,promo_code,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            org_id,
            data["title"], data.get("description", ""), int(data["starts_at"]),
            int(data.get("ends_at") or 0),
            data.get("area", ""), data.get("address", ""),
            data.get("price_text", ""), data.get("pay_url", ""),
            int(data.get("capacity") or 0), int(data.get("refs_needed") or 0),
            data.get("channel", "").lstrip("@"), data.get("age_limit", ""),
            str(data.get("cover") or "ember"),
            str(data.get("city") or "Москва"), data.get("genre", ""),
            int(data.get("qt_event_id") or 0),
            cover_img, status,
            str(data.get("promo_code") or ""),
            now(),
        ),
    )
    c.commit()
    eid = cur.lastrowid
    log_action(eid, org_id, "create", data.get("title", ""))
    return eid


# ---------- статусы / авто-past (этап 1) ----------

def _event_end(e) -> int:
    """Время фактического окончания: ends_at или старт + 6ч по умолчанию."""
    return e["ends_at"] if e["ends_at"] else e["starts_at"] + 6 * 3600


def mark_past_events() -> int:
    """Активные события, у которых вышло время, переводим в past (не удаляем)."""
    c = conn()
    cur = c.execute(
        """UPDATE events SET status='past'
           WHERE status='active'
             AND (CASE WHEN ends_at>0 THEN ends_at ELSE starts_at + 21600 END) < ?""",
        (now(),),
    )
    c.commit()
    return cur.rowcount


# ---------- верификация организатора (этап 1/5) ----------

def set_verified(user_id: int, verified: bool) -> None:
    c = conn()
    c.execute("UPDATE users SET is_verified=?, verified_at=? WHERE tg_id=?",
              (1 if verified else 0, now() if verified else None, user_id))
    c.commit()


def is_verified(user_id: int) -> bool:
    r = conn().execute("SELECT is_verified FROM users WHERE tg_id=?", (user_id,)).fetchone()
    return bool(r and r["is_verified"])


# ---------- история действий (этап 4) ----------

def log_action(event_id: int, org_id: int, action: str, detail: str = "") -> None:
    c = conn()
    c.execute(
        "INSERT INTO event_log(event_id,org_id,action,detail,created_at) VALUES(?,?,?,?,?)",
        (event_id, org_id, action, detail or "", now()),
    )
    c.commit()


def org_log(org_id: int, limit: int = 50) -> list[sqlite3.Row]:
    return conn().execute(
        """SELECT l.*, e.title FROM event_log l LEFT JOIN events e ON e.id=l.event_id
           WHERE l.org_id=? ORDER BY l.id DESC LIMIT ?""",
        (org_id, limit),
    ).fetchall()


def user_past_events(user_id: int) -> list[sqlite3.Row]:
    """Посещённые/прошедшие события гостя (для истории профиля)."""
    return conn().execute(
        """SELECT e.id, e.title, e.starts_at, e.city, e.cover, t.status AS ticket_status, t.kind
           FROM tickets t JOIN events e ON e.id=t.event_id
           WHERE t.user_id=? AND t.status!='revoked'
             AND (e.status='past' OR e.status='cancelled' OR t.status='used')
           ORDER BY e.starts_at DESC""",
        (user_id,),
    ).fetchall()


# ---------- участники события для рассылок (этап 3) ----------

def event_ticket_users(event_id: int) -> list[int]:
    """tg_id всех, у кого активный билет на событие (идут)."""
    rows = conn().execute(
        "SELECT DISTINCT user_id FROM tickets WHERE event_id=? AND status='active' AND kind!='paid_pending'",
        (event_id,),
    ).fetchall()
    return [r[0] for r in rows]


# ---------- доп. фото (этап 6) ----------

def add_event_photo(event_id: int, img: bytes, idx: int) -> None:
    c = conn()
    c.execute("INSERT INTO event_photos(event_id, idx, img) VALUES(?,?,?)", (event_id, idx, img))
    c.commit()


def event_photo_count(event_id: int) -> int:
    return conn().execute(
        "SELECT COUNT(*) FROM event_photos WHERE event_id=?", (event_id,)
    ).fetchone()[0]


def get_event_photo(event_id: int, idx: int) -> bytes | None:
    r = conn().execute(
        "SELECT img FROM event_photos WHERE event_id=? AND idx=? LIMIT 1", (event_id, idx)
    ).fetchone()
    return r[0] if r else None


# ---------- жалобы и антидубль (этап 5) ----------

def add_report(event_id: int, user_id: int, reason: str) -> bool:
    c = conn()
    try:
        c.execute("INSERT INTO reports(event_id,user_id,reason,created_at) VALUES(?,?,?,?)",
                  (event_id, user_id, reason or "", now()))
        c.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def report_count(event_id: int) -> int:
    return conn().execute(
        "SELECT COUNT(*) FROM reports WHERE event_id=?", (event_id,)
    ).fetchone()[0]


# ---------- подписки на организаторов ----------

def follow(org_id: int, user_id: int) -> bool:
    if org_id == user_id:
        return False
    c = conn()
    try:
        c.execute("INSERT INTO follows(org_id,user_id,created_at) VALUES(?,?,?)",
                  (org_id, user_id, now()))
        c.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def unfollow(org_id: int, user_id: int) -> None:
    c = conn()
    c.execute("DELETE FROM follows WHERE org_id=? AND user_id=?", (org_id, user_id))
    c.commit()


def is_following(org_id: int, user_id: int) -> bool:
    return conn().execute(
        "SELECT 1 FROM follows WHERE org_id=? AND user_id=?", (org_id, user_id)
    ).fetchone() is not None


def follower_ids(org_id: int) -> list[int]:
    rows = conn().execute("SELECT user_id FROM follows WHERE org_id=?", (org_id,)).fetchall()
    return [r[0] for r in rows]


def user_follows(user_id: int) -> list[sqlite3.Row]:
    """Организаторы, на которых подписан пользователь."""
    return conn().execute(
        """SELECT f.org_id, u.username, u.first_name
           FROM follows f JOIN users u ON u.tg_id = f.org_id
           WHERE f.user_id=? ORDER BY f.created_at DESC""",
        (user_id,),
    ).fetchall()


def follower_count(org_id: int) -> int:
    return conn().execute(
        "SELECT COUNT(*) FROM follows WHERE org_id=?", (org_id,)
    ).fetchone()[0]


def find_duplicate_event(title: str, starts_at: int, area: str, exclude_org: int) -> sqlite3.Row | None:
    """Похожее событие другого организатора (совпадение названия, ~времени и места)."""
    return conn().execute(
        """SELECT * FROM events
           WHERE status IN ('active','pending') AND org_id != ?
             AND lower(title)=lower(?) AND lower(area)=lower(?)
             AND ABS(starts_at-?) < 7200
           LIMIT 1""",
        (exclude_org, title, area, int(starts_at)),
    ).fetchone()


def set_event_status(event_id: int, status: str) -> None:
    c = conn()
    c.execute("UPDATE events SET status=? WHERE id=?", (status, event_id))
    c.commit()


# редактируемые поля (всё, кроме служебных)
EDITABLE = ("title", "description", "area", "address", "price_text", "pay_url",
            "capacity", "refs_needed", "channel", "age_limit", "cover", "city",
            "genre", "starts_at", "ends_at", "promo_code")


def update_event(event_id: int, org_id: int, data: dict, status: str,
                 cover_img: bytes | None = None, set_cover: bool = False,
                 force: bool = False) -> bool:
    """Полное редактирование события. force=True — модератор правит чужое событие."""
    if force:
        e = conn().execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    else:
        e = conn().execute("SELECT * FROM events WHERE id=? AND org_id=?",
                           (event_id, org_id)).fetchone()
    if not e:
        return False
    sets, args = [], []
    for f in EDITABLE:
        if f in data:
            val = data[f]
            if f in ("capacity", "refs_needed", "starts_at", "ends_at"):
                val = int(val or 0)
            elif f == "channel":
                val = str(val or "").lstrip("@")
            sets.append(f"{f}=?")
            args.append(val)
    sets.append("status=?"); args.append(status)
    if set_cover:
        sets.append("cover_img=?"); args.append(cover_img)
    if force:
        args.append(event_id)
        where = "WHERE id=?"
    else:
        args += [event_id, org_id]
        where = "WHERE id=? AND org_id=?"
    c = conn()
    cur = c.execute(f"UPDATE events SET {', '.join(sets)} {where}", args)
    c.commit()
    if cur.rowcount > 0:
        log_action(event_id, org_id, "edit", data.get("title", e["title"]))
    return cur.rowcount > 0


def delete_event(event_id: int, org_id: int, force: bool = False) -> bool:
    """Мягкое удаление (отмена). force=True — модератор отменяет чужое событие."""
    c = conn()
    if force:
        cur = c.execute("UPDATE events SET status='cancelled' WHERE id=?", (event_id,))
    else:
        cur = c.execute("UPDATE events SET status='cancelled' WHERE id=? AND org_id=?",
                        (event_id, org_id))
    c.commit()
    if cur.rowcount > 0:
        log_action(event_id, org_id, "cancel")
    return cur.rowcount > 0


def reschedule_event(event_id: int, org_id: int, new_starts: int, new_status: str,
                     new_ends: int = 0, force: bool = False) -> bool:
    """Перенос даты/времени. force=True — модератор переносит чужое событие."""
    c = conn()
    if force:
        cur = c.execute("UPDATE events SET starts_at=?, ends_at=?, status=? WHERE id=?",
                        (int(new_starts), int(new_ends or 0), new_status, event_id))
    else:
        cur = c.execute("UPDATE events SET starts_at=?, ends_at=?, status=? WHERE id=? AND org_id=?",
                        (int(new_starts), int(new_ends or 0), new_status, event_id, org_id))
    c.commit()
    if cur.rowcount > 0:
        log_action(event_id, org_id, "reschedule",
                   time.strftime("%d.%m %H:%M", time.localtime(new_starts)))
    return cur.rowcount > 0


# ---------- ручной флоу с пруфом (этап 2) ----------

def create_pending_with_proof(event_id: int, user_id: int, img: bytes | None,
                              mime: str) -> str | None:
    if get_user_ticket(event_id, user_id):
        return None
    code = uuid.uuid4().hex
    c = conn()
    c.execute(
        """INSERT INTO tickets(code,event_id,user_id,kind,proof_img,proof_mime,created_at)
           VALUES(?,?,?,'paid_pending',?,?,?)""",
        (code, event_id, user_id, img, mime or "", now()),
    )
    c.commit()
    return code


def get_proof(code: str) -> tuple[bytes, str] | None:
    r = conn().execute("SELECT proof_img, proof_mime FROM tickets WHERE code=?", (code,)).fetchone()
    if r and r["proof_img"]:
        return r["proof_img"], (r["proof_mime"] or "image/jpeg")
    return None


def reject_ticket(code: str) -> None:
    """Отклонение заявки: билет revoked (для гостя — тишина). Скрин сразу стираем."""
    c = conn()
    c.execute("UPDATE tickets SET status='revoked', proof_img=NULL, proof_mime='' "
              "WHERE code=? AND kind='paid_pending'", (code,))
    c.commit()


def purge_old_proofs() -> int:
    """Удаляем скрины/PDF билетов: после конца мероприятия +24ч (приватность).
    Сам билет остаётся, стирается только файл-пруф."""
    c = conn()
    cutoff = now() - 24 * 3600
    cur = c.execute(
        """UPDATE tickets SET proof_img=NULL, proof_mime='' WHERE proof_img IS NOT NULL
           AND event_id IN (
             SELECT id FROM events
             WHERE (CASE WHEN ends_at>0 THEN ends_at ELSE starts_at + 21600 END) < ?
           )""",
        (cutoff,),
    )
    c.commit()
    return cur.rowcount


def get_cover(event_id: int) -> bytes | None:
    row = conn().execute("SELECT cover_img FROM events WHERE id=?", (event_id,)).fetchone()
    return row[0] if row and row[0] else None


# ---------- qtickets ----------

def set_qtickets_token(user_id: int, token: str) -> None:
    c = conn()
    c.execute("UPDATE users SET qtickets_token=? WHERE tg_id=?", (token.strip(), user_id))
    c.commit()


def event_by_qt(org_id: int, qt_event_id: int) -> sqlite3.Row | None:
    """Уже импортированное из qtickets событие этого организатора (дедуп импорта)."""
    return conn().execute(
        "SELECT * FROM events WHERE org_id=? AND qt_event_id=? LIMIT 1",
        (org_id, qt_event_id),
    ).fetchone()


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
    # продвигаемые (featured_until > now) идут первыми, затем по дате
    q += f" ORDER BY (CASE WHEN featured_until > {now()} THEN 0 ELSE 1 END), starts_at ASC"
    return conn().execute(q, args).fetchall()


def set_featured(event_id: int, until: int) -> bool:
    c = conn()
    cur = c.execute("UPDATE events SET featured_until=? WHERE id=?", (int(until), event_id))
    c.commit()
    return cur.rowcount > 0


def set_soldout(event_id: int, org_id: int, val: bool, force: bool = False) -> bool:
    """Отметить событие распроданным/снова в продаже. force=True для админа."""
    c = conn()
    if force:
        cur = c.execute("UPDATE events SET soldout=? WHERE id=?", (1 if val else 0, event_id))
    else:
        cur = c.execute("UPDATE events SET soldout=? WHERE id=? AND org_id=?",
                        (1 if val else 0, event_id, org_id))
    c.commit()
    return cur.rowcount > 0


def all_user_ids() -> list[int]:
    """Все пользователи бота (для промо-рассылки)."""
    rows = conn().execute("SELECT tg_id FROM users").fetchall()
    return [r[0] for r in rows]


def city_counts() -> dict[str, int]:
    rows = conn().execute(
        f"SELECT city, COUNT(*) FROM events WHERE status='active' AND starts_at > {now() - 6 * 3600} GROUP BY city"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def get_user(tg_id: int) -> sqlite3.Row | None:
    return conn().execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


def get_users_batch(tg_ids: list[int]) -> dict[int, sqlite3.Row]:
    """Пакетное получение пользователей."""
    if not tg_ids:
        return {}
    c = conn()
    placeholders = ",".join("?" * len(tg_ids))
    rows = c.execute(
        f"SELECT * FROM users WHERE tg_id IN ({placeholders})", tg_ids
    ).fetchall()
    return {r["tg_id"]: r for r in rows}


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


def tickets_counts_batch(event_ids: list[int]) -> dict[int, int]:
    """Пакетный подсчёт билетов для списка событий (без N+1)."""
    if not event_ids:
        return {}
    c = conn()
    placeholders = ",".join("?" * len(event_ids))
    rows = c.execute(
        f"SELECT event_id, COUNT(*) FROM tickets WHERE event_id IN ({placeholders}) "
        f"AND status!='revoked' AND kind!='paid_pending' GROUP BY event_id",
        event_ids,
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def photo_counts_batch(event_ids: list[int]) -> dict[int, int]:
    """Пакетный подсчёт фото для списка событий."""
    if not event_ids:
        return {}
    c = conn()
    placeholders = ",".join("?" * len(event_ids))
    rows = c.execute(
        f"SELECT event_id, COUNT(*) FROM event_photos WHERE event_id IN ({placeholders}) GROUP BY event_id",
        event_ids,
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def get_user_ticket(event_id: int, user_id: int) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM tickets WHERE event_id=? AND user_id=?", (event_id, user_id)
    ).fetchone()


def remove_user_ticket(event_id: int, user_id: int) -> bool:
    """Пользователь отменил участие («Я иду» → отмена). Удаляем его запись."""
    c = conn()
    cur = c.execute("DELETE FROM tickets WHERE event_id=? AND user_id=?",
                    (event_id, user_id))
    c.commit()
    return cur.rowcount > 0


def create_ticket(event_id: int, user_id: int, kind: str) -> str:
    code = uuid.uuid4().hex
    c = conn()
    c.execute(
        "INSERT INTO tickets(code,event_id,user_id,kind,created_at) VALUES(?,?,?,?,?)",
        (code, event_id, user_id, kind, now()),
    )
    c.commit()
    return code


# результаты атомарной выдачи билета
TICKET_OK = "ok"
TICKET_DUP = "dup"          # у пользователя уже есть билет
TICKET_SOLD_OUT = "sold"    # мест больше нет


def create_ticket_capped(event_id: int, user_id: int, kind: str,
                         capacity: int = 0) -> tuple[str, str | None]:
    """Атомарная выдача билета с учётом лимита мест — защита от перепродажи
    при наплыве людей. Один SQL-стейтмент: вставка только если COUNT < capacity.
    capacity=0 — без лимита. Возвращает (статус, код|None)."""
    code = uuid.uuid4().hex
    c = conn()
    try:
        if capacity and capacity > 0:
            cur = c.execute(
                """INSERT INTO tickets(code,event_id,user_id,kind,created_at)
                   SELECT ?,?,?,?,?
                   WHERE (SELECT COUNT(*) FROM tickets
                          WHERE event_id=? AND status!='revoked') < ?""",
                (code, event_id, user_id, kind, now(), event_id, capacity),
            )
        else:
            cur = c.execute(
                "INSERT INTO tickets(code,event_id,user_id,kind,created_at) VALUES(?,?,?,?,?)",
                (code, event_id, user_id, kind, now()),
            )
        c.commit()
    except sqlite3.IntegrityError:
        # сработал UNIQUE(event_id,user_id) — билет уже есть
        return TICKET_DUP, None
    if cur.rowcount == 0:
        return TICKET_SOLD_OUT, None
    return TICKET_OK, code


def user_tickets(user_id: int) -> list[sqlite3.Row]:
    return conn().execute(
        """SELECT t.*, e.title, e.starts_at, e.ends_at, e.area, e.address,
                  e.age_limit, e.cover, e.qt_event_id, e.pay_url, e.status AS event_status,
                  (e.cover_img IS NOT NULL AND length(e.cover_img)>0) AS has_cover,
                  e.created_at AS cover_ver
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
    """Подтверждение оплаты. Скрин больше не нужен — стираем сразу (приватность)."""
    c = conn()
    c.execute("UPDATE tickets SET kind='paid', proof_img=NULL, proof_mime='' "
              "WHERE code=? AND kind='paid_pending'", (code,))
    c.commit()


def admin_create_ticket(event_id: int, user_id: int, kind: str = "paid") -> str | None:
    """Админ вручную создаёт билет для пользователя (обходит все проверки)."""
    if get_user_ticket(event_id, user_id):
        return None
    code = uuid.uuid4().hex
    c = conn()
    c.execute(
        "INSERT INTO tickets(code,event_id,user_id,kind,created_at) VALUES(?,?,?,?,?)",
        (code, event_id, user_id, kind, now()),
    )
    c.commit()
    return code


def admin_delete_ticket(code: str) -> bool:
    """Админ полностью удаляет билет (hard delete)."""
    c = conn()
    cur = c.execute("DELETE FROM tickets WHERE code=?", (code,))
    c.commit()
    return cur.rowcount > 0


def admin_set_ticket_status(code: str, status: str) -> bool:
    """Админ меняет статус билета (active/used/revoked)."""
    c = conn()
    used_at = now() if status == "used" else None
    cur = c.execute("UPDATE tickets SET status=?, used_at=? WHERE code=?",
                    (status, used_at, code))
    c.commit()
    return cur.rowcount > 0


def admin_set_ticket_kind(code: str, kind: str) -> bool:
    """Админ меняет тип билета (free/paid/paid_pending)."""
    c = conn()
    cur = c.execute("UPDATE tickets SET kind=?, proof_img=NULL, proof_mime='' WHERE code=?",
                    (kind, code))
    c.commit()
    return cur.rowcount > 0


def admin_all_tickets(event_id: int) -> list[sqlite3.Row]:
    """Все билеты события (включая revoked) для админ-панели."""
    return conn().execute(
        """SELECT t.*, u.username, u.first_name
           FROM tickets t JOIN users u ON u.tg_id = t.user_id
           WHERE t.event_id=? ORDER BY t.created_at ASC""",
        (event_id,),
    ).fetchall()


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


def get_referral_bonus(event_id: int, user_id: int) -> int:
    r = conn().execute(
        "SELECT bonus FROM referral_bonus WHERE event_id=? AND user_id=?",
        (event_id, user_id),
    ).fetchone()
    return int(r["bonus"]) if r else 0


def set_referral_bonus(event_id: int, user_id: int, bonus: int) -> None:
    """Модераторский бонус к счётчику рефералов (дельта, прибавляется к реальным)."""
    bonus = max(0, int(bonus))
    c = conn()
    c.execute(
        "INSERT INTO referral_bonus(event_id,user_id,bonus) VALUES(?,?,?) "
        "ON CONFLICT(event_id,user_id) DO UPDATE SET bonus=excluded.bonus",
        (event_id, user_id, bonus),
    )
    c.commit()
