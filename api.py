"""HTTP API для Mini App + раздача статики (webapp/index.html)."""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

import config
import db
import notify

log = logging.getLogger("tusa.api")
WEBAPP_DIR = Path(__file__).parent / "webapp"


# ---------- rate limiter (защита от спама) ----------

class RateLimiter:
    """Простой in-memory rate limiter: user_id -> [timestamps]."""
    def __init__(self, max_requests: int = 30, window: int = 60):
        self._max = max_requests
        self._window = window
        self._hits: dict[int, list[float]] = {}

    def check(self, user_id: int) -> bool:
        """True = разрешено, False = лимит превышен."""
        now = time.time()
        hits = self._hits.get(user_id, [])
        # отсекаем старые
        hits = [t for t in hits if now - t < self._window]
        if len(hits) >= self._max:
            self._hits[user_id] = hits
            return False
        hits.append(now)
        self._hits[user_id] = hits
        return True

    def cleanup(self):
        """Периодическая очистка памяти от старых записей."""
        now = time.time()
        stale = [uid for uid, hits in self._hits.items()
                 if not hits or now - hits[-1] > self._window * 2]
        for uid in stale:
            del self._hits[uid]


_rate = RateLimiter(max_requests=120, window=60)  # 120 запросов/мин на пользователя
_rate_write = RateLimiter(max_requests=30, window=60)  # 30 записей/мин (create, claim, etc.)


# ---------- проверка подлинности initData (подпись Telegram) ----------

def validate_init_data(init_data: str) -> dict | None:
    """Проверяет HMAC-подпись initData. Возвращает dict с user или None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, received_hash):
            return None
        # подпись валидна не дольше суток
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        user = json.loads(pairs.get("user", "{}"))
        return user if user.get("id") else None
    except Exception:
        return None


# кэш статуса подписки на обязательный канал (чтобы не дёргать Telegram на каждый запрос)
_sub_cache: dict[int, tuple[bool, float]] = {}
SUB_TTL = 300  # сек

# кэш известных пользователей (чтобы не дёргать БД на каждый запрос)
_user_cache: dict[int, float] = {}
_USER_TTL = 120  # сек


async def user_subscribed(bot, user_id: int) -> bool:
    if not config.REQUIRED_CHANNEL:
        return True
    hit = _sub_cache.get(user_id)
    if hit and time.time() - hit[1] < SUB_TTL and hit[0]:
        return True
    from bot import check_subscribed
    ok = await check_subscribed(bot, config.REQUIRED_CHANNEL, user_id)
    _sub_cache[user_id] = (ok, time.time())
    return ok


# пути, доступные без подписки (сама проверка и статика)
_GATE_FREE = ("/api/gate", "/api/meta")


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path.startswith("/api/"):
        user = validate_init_data(request.headers.get("X-Init-Data", ""))
        if not user:
            return web.json_response({"error": "unauthorized"}, status=401)
        uid = user["id"]
        # rate limiting
        if not _rate.check(uid):
            return web.json_response({"error": "Слишком много запросов, подожди"}, status=429)
        # write-rate для мутирующих запросов
        if request.method == "POST" and not _rate_write.check(uid):
            return web.json_response({"error": "Слишком частые действия, подожди минуту"}, status=429)
        # кэш: не писать в БД если юзер уже известен
        now_t = time.time()
        if uid not in _user_cache or now_t - _user_cache[uid] > _USER_TTL:
            db.upsert_user(uid, user.get("username"), user.get("first_name"))
            _user_cache[uid] = now_t
        request["user"] = user
        # шлагбаум: обязательная подписка на канал
        if config.REQUIRED_CHANNEL and request.path not in _GATE_FREE:
            if not await user_subscribed(request.app["bot"], uid):
                return web.json_response(
                    {"error": "Подпишись на канал, чтобы продолжить",
                     "need": "subscribe", "channel": config.REQUIRED_CHANNEL}, status=403)
    return await handler(request)


@web.middleware
async def security_middleware(request: web.Request, handler):
    """Заголовки безопасности + запрет кеширования приватных данных."""
    try:
        resp = await handler(request)
    except web.HTTPException:
        raise
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # Mini App открывается ВНУТРИ Telegram (iframe). Разрешаем встраивание только
    # доменам Telegram, остальным — запрещаем (защита от клонов/кликджекинга).
    resp.headers["Content-Security-Policy"] = (
        "frame-ancestors https://web.telegram.org https://*.telegram.org "
        "https://telegram.org tg://"
    )
    if request.path.startswith("/api/"):
        # приватные ответы (билеты, гости, токены) не кешируем нигде
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp


def event_json(e, me_id: int | None = None) -> dict:
    taken = db.tickets_count(e["id"])
    d = {
        "id": e["id"], "title": e["title"], "description": e["description"],
        "starts_at": e["starts_at"], "area": e["area"],
        "price_text": e["price_text"], "pay_url": e["pay_url"],
        "capacity": e["capacity"], "refs_needed": e["refs_needed"],
        "channel": e["channel"], "age_limit": e["age_limit"],
        "cover": e["cover"], "city": e["city"], "genre": e["genre"],
        "taken": taken,
        "sold_out": bool((e["capacity"] and taken >= e["capacity"])
                         or ("soldout" in e.keys() and e["soldout"])),
        "is_mine": me_id == e["org_id"],
        "is_admin": me_id in config.ADMIN_IDS,
        "org_id": e["org_id"],
        "status": e["status"],
        "ends_at": e["ends_at"] if "ends_at" in e.keys() else 0,
        "featured": bool(e["featured_until"] and e["featured_until"] > int(time.time())) if "featured_until" in e.keys() else False,
        "soldout_flag": bool(e["soldout"]) if "soldout" in e.keys() else False,
        "has_cover": bool(e["cover_img"]) if "cover_img" in e.keys() else False,
        "cover_ver": e["created_at"],   # версия для обхода кэша картинок
        "photos": db.event_photo_count(e["id"]),
        "promo_code": e["promo_code"] if "promo_code" in e.keys() else "",
    }
    org = db.get_user(e["org_id"])
    d["host"] = (org["username"] or org["first_name"] or "host") if org else "host"
    d["host_verified"] = bool(org and org["is_verified"]) if org else False
    return d


# ---------- handlers ----------

async def h_events(request: web.Request):
    me = request["user"]["id"]
    city = request.query.get("city") or None
    events = db.list_events(city=city)
    if not events:
        return web.json_response([])
    # batch: 3 запроса вместо 3*N
    eids = [e["id"] for e in events]
    org_ids = list({e["org_id"] for e in events})
    taken_map = db.tickets_counts_batch(eids)
    photo_map = db.photo_counts_batch(eids)
    orgs_map = db.get_users_batch(org_ids)
    now_ts = int(time.time())
    result = []
    for e in events:
        taken = taken_map.get(e["id"], 0)
        org = orgs_map.get(e["org_id"])
        result.append({
            "id": e["id"], "title": e["title"], "description": e["description"],
            "starts_at": e["starts_at"], "area": e["area"],
            "price_text": e["price_text"], "pay_url": e["pay_url"],
            "capacity": e["capacity"], "refs_needed": e["refs_needed"],
            "channel": e["channel"], "age_limit": e["age_limit"],
            "cover": e["cover"], "city": e["city"], "genre": e["genre"],
            "taken": taken,
            "sold_out": bool((e["capacity"] and taken >= e["capacity"])
                             or ("soldout" in e.keys() and e["soldout"])),
            "is_mine": me == e["org_id"], "org_id": e["org_id"], "status": e["status"],
            "ends_at": e["ends_at"] if "ends_at" in e.keys() else 0,
            "featured": bool(e["featured_until"] and e["featured_until"] > now_ts) if "featured_until" in e.keys() else False,
            "soldout_flag": bool(e["soldout"]) if "soldout" in e.keys() else False,
            "has_cover": bool(e["cover_img"]) if "cover_img" in e.keys() else False,
            "cover_ver": e["created_at"],
            "photos": photo_map.get(e["id"], 0),
            "promo_code": e["promo_code"] if "promo_code" in e.keys() else "",
            "host": (org["username"] or org["first_name"] or "host") if org else "host",
            "host_verified": bool(org and org["is_verified"]) if org else False,
        })
    return web.json_response(result)


async def h_cities(request: web.Request):
    return web.json_response(db.city_counts())


async def h_event(request: web.Request):
    e = db.get_event(int(request.match_info["id"]))
    if not e:
        return web.json_response({"error": "not_found"}, status=404)
    me = request["user"]["id"]
    data = event_json(e, me)

    # иду ли я на событие
    t = db.get_user_ticket(e["id"], me)
    data["my_ticket"] = (
        {"code": t["code"], "kind": t["kind"], "status": t["status"]} if t else None
    )
    data["address"] = e["address"]
    data["is_admin"] = me in config.ADMIN_IDS
    bot = request.app["bot"]
    app = request.app
    if "bot_username" not in app:
        app["bot_username"] = (await bot.get_me()).username
    me_username = app["bot_username"]
    data["share_link"] = f"https://t.me/{me_username}?start=evt_{e['id']}"
    return web.json_response(data)


def _decode_cover(data_url: str) -> bytes | None:
    """base64 data-URL картинки -> bytes (ограничение размера 1.5 МБ)."""
    if not data_url or "," not in data_url:
        return None
    try:
        import base64
        raw = base64.b64decode(data_url.split(",", 1)[1])
        return raw if 0 < len(raw) <= 1_500_000 else None
    except Exception:
        return None


def _moderation_on() -> bool:
    return bool(config.ADMIN_IDS)


async def _notify_admins_new(bot, event_id: int, title: str, org: str, warn: str | None = None):
    import datetime
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    e = db.get_event(event_id)
    open_btn = InlineKeyboardButton(
        text="👁 Открыть карточку",
        web_app=WebAppInfo(url=f"{config.WEBAPP_URL}#event/{event_id}"),
    )
    org_btn = InlineKeyboardButton(
        text="👤 Профиль организатора",
        web_app=WebAppInfo(url=f"{config.WEBAPP_URL}#org/{e['org_id']}")) if e else None
    rows = [[open_btn]]
    if org_btn:
        rows.append([org_btn])
    rows.append([InlineKeyboardButton(text="✅ Одобрить", callback_data=f"mod_ok_{event_id}"),
                 InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"mod_no_{event_id}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if e:
        when = datetime.datetime.fromtimestamp(e["starts_at"]).strftime("%d.%m.%Y %H:%M")
        price = e["price_text"] or ("по ссылке" if e["pay_url"] else "free")
        lines = [
            "🆕 <b>На модерацию</b>",
            f"<b>{e['title']}</b>",
            f"🗓 {when}",
            f"🏙 {e['city']}" + (f" · {e['area']}" if e["area"] else ""),
        ]
        if e["address"]:
            lines.append(f"📍 Адрес: {e['address']}")
        lines.append(f"💸 {price}" + (f" · {e['age_limit']}" if e["age_limit"] else ""))
        if e["capacity"]:
            lines.append(f"👥 Лимит: {e['capacity']}")
        if e["genre"]:
            lines.append(f"🎵 {e['genre']}")
        if e["description"]:
            desc = e["description"][:600]
            lines.append(f"\n{desc}")
        lines.append(f"\nОрганизатор: {org} · ID {event_id}")
        if warn:
            lines.insert(1, f"<b>{warn}</b>")
        text = "\n".join(lines)
    else:
        text = f"🆕 <b>На модерацию</b>\n«{title}»\nОрганизатор: {org}\nID: {event_id}"
    for admin in config.ADMIN_IDS:
        try:
            await bot.send_message(admin, text, reply_markup=kb)
        except Exception:
            pass


async def h_create_event(request: web.Request):
    me = request["user"]["id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некорректный JSON"}, status=400)
    required = ("title", "starts_at")
    if any(not body.get(k) for k in required):
        return web.json_response({"error": "title и дата обязательны"}, status=400)
    title = str(body.get("title", "")).strip()
    if len(title) > 80 or len(title) < 2:
        return web.json_response({"error": "Название: 2-80 символов"}, status=400)
    body["title"] = title
    # валидация числовых полей
    try:
        body["starts_at"] = int(body["starts_at"])
    except (ValueError, TypeError):
        return web.json_response({"error": "Некорректная дата"}, status=400)
    if body["starts_at"] <= time.time():
        return web.json_response({"error": "Дата должна быть в будущем"}, status=400)
    # ограничение длины текстовых полей
    if len(str(body.get("description", ""))) > 3000:
        return web.json_response({"error": "Описание слишком длинное (макс. 3000)"}, status=400)
    if len(str(body.get("area", ""))) > 200:
        return web.json_response({"error": "Район слишком длинный"}, status=400)
    if len(str(body.get("address", ""))) > 300:
        return web.json_response({"error": "Адрес слишком длинный"}, status=400)
    if len(str(body.get("promo_code", ""))) > 40:
        return web.json_response({"error": "Промокод слишком длинный"}, status=400)
    # защита от спама событиями: лимит активных на пользователя
    active_count = len([e for e in db.org_events(me) if e["status"] in ("active", "pending")])
    if active_count >= 20:
        return web.json_response({"error": "Лимит: не более 20 активных событий"}, status=400)
    cover_img = _decode_cover(body.get("cover_data", ""))
    # антифрод (этап 5): дубль чужого события
    dup = db.find_duplicate_event(body["title"], int(body["starts_at"]),
                                  body.get("area", ""), exclude_org=me)
    # модерация (этап 1/5): включена если есть админы И организатор не верифицирован
    moderate = _moderation_on() and not db.is_verified(me)
    status = "pending" if (moderate or dup) else "active"
    event_id = db.create_event(me, body, status=status, cover_img=cover_img)
    # доп. фото карусели (этап 6) — массив data-url
    for i, ph in enumerate((body.get("photos_data") or [])[:5]):
        img = _decode_cover(ph)
        if img:
            db.add_event_photo(event_id, img, i)
    if status == "pending":
        org = request["user"].get("username") or request["user"].get("first_name") or str(me)
        note = "⚠️ ВОЗМОЖНЫЙ ДУБЛЬ чужого события!" if dup else None
        await _notify_admins_new(request.app["bot"], event_id, body["title"], org, warn=note)
    else:
        # сразу опубликовано — уведомляем подписчиков в фоне (не тормозим ответ)
        asyncio.create_task(notify_followers(request.app["bot"], db.get_event(event_id)))
    return web.json_response({"id": event_id, "status": status})


async def h_edit_event(request: web.Request):
    """Редактирование события владельцем. После правок снова идёт на модерацию (если включена)."""
    me = request["user"]["id"]
    eid = int(request.match_info["id"])
    e = db.get_event(eid)
    is_admin = me in config.ADMIN_IDS
    if not e or (e["org_id"] != me and not is_admin):
        return web.json_response({"error": "Это не твоё событие"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некорректный JSON"}, status=400)
    if "title" in body and not str(body["title"]).strip():
        return web.json_response({"error": "Название не может быть пустым"}, status=400)
    if "title" in body and len(body["title"]) > 80:
        return web.json_response({"error": "слишком длинное название"}, status=400)
    if body.get("starts_at") and int(body["starts_at"]) <= time.time():
        return web.json_response({"error": "Дата должна быть в будущем"}, status=400)
    # обложка: меняем только если прислали новую (set_cover) или явно сбросили
    set_cover = "cover_data" in body
    cover_img = _decode_cover(body.get("cover_data", "")) if body.get("cover_data") else None
    # модератор правит без повторной модерации; владельца — по обычным правилам
    moderate = _moderation_on() and not is_admin and not db.is_verified(me)
    status = "pending" if moderate else "active"
    owner_id = e["org_id"] if is_admin else me
    ok = db.update_event(eid, owner_id, body, status, cover_img=cover_img,
                         set_cover=set_cover, force=is_admin)
    if not ok:
        return web.json_response({"error": "Не получилось обновить"}, status=400)
    if status == "pending":
        org = request["user"].get("username") or request["user"].get("first_name") or str(me)
        await _notify_admins_new(request.app["bot"], eid, e["title"], org,
                                 warn="✏️ Изменённое событие — перепроверь")
    return web.json_response({"ok": True, "status": status})


async def h_soldout(request: web.Request):
    """Отметить событие распроданным / снова в продаже (владелец или админ)."""
    me = request["user"]["id"]
    eid = int(request.match_info["id"])
    body = await request.json()
    val = bool(body.get("soldout"))
    force = me in config.ADMIN_IDS
    if not db.set_soldout(eid, me, val, force=force):
        return web.json_response({"error": "Это не твоё событие"}, status=403)
    return web.json_response({"ok": True, "soldout": val})


async def h_delete_event(request: web.Request):
    me = request["user"]["id"]
    eid = int(request.match_info["id"])
    e = db.get_event(eid)
    is_admin = me in config.ADMIN_IDS
    if not e or (e["org_id"] != me and not is_admin):
        return web.json_response({"error": "Не получилось — это не твоё событие"}, status=403)
    users = db.event_ticket_users(eid)
    db.delete_event(eid, me, force=is_admin)
    # этап 3: уведомить всех, кто шёл (в фоне)
    if users:
        asyncio.create_task(notify.broadcast(request.app["bot"], users,
                            f"❌ Событие «{e['title']}» отменено организатором."))
    return web.json_response({"ok": True})


async def h_reschedule_event(request: web.Request):
    me = request["user"]["id"]
    eid = int(request.match_info["id"])
    body = await request.json()
    new_starts = int(body.get("starts_at") or 0)
    new_ends = int(body.get("ends_at") or 0)
    if new_starts <= time.time():
        return web.json_response({"error": "Дата должна быть в будущем"}, status=400)
    e = db.get_event(eid)
    is_admin = me in config.ADMIN_IDS
    if not e or (e["org_id"] != me and not is_admin):
        return web.json_response({"error": "Это не твоё событие"}, status=403)
    users = db.event_ticket_users(eid)
    moderate = _moderation_on() and not is_admin and not db.is_verified(me)
    status = "pending" if moderate else "active"
    db.reschedule_event(eid, me, new_starts, status, new_ends, force=is_admin)
    import datetime
    when = datetime.datetime.fromtimestamp(new_starts).strftime("%d.%m %H:%M")
    if users:
        asyncio.create_task(notify.broadcast(request.app["bot"], users,
                            f"🗓 Событие «{e['title']}» перенесено на {when}."))
    if status == "pending":
        org = request["user"].get("username") or request["user"].get("first_name") or str(me)
        await _notify_admins_new(request.app["bot"], eid, e["title"] + " (перенос)", org)
    return web.json_response({"ok": True, "status": status})


async def h_broadcast(request: web.Request):
    """Этап 3: организатор шлёт сообщение всем участникам своего события."""
    me = request["user"]["id"]
    eid = int(request.match_info["id"])
    body = await request.json()
    text = (body.get("text") or "").strip()
    reason = (body.get("reason") or "").strip()
    e = db.get_event(eid)
    if not e or (e["org_id"] != me and me not in config.ADMIN_IDS):
        return web.json_response({"error": "Это не твоё событие"}, status=403)
    if not text:
        return web.json_response({"error": "Пустое сообщение"}, status=400)
    users = db.event_ticket_users(eid)
    msg = f"📣 <b>{e['title']}</b>\n{text}"
    if reason:
        msg += f"\n\n<i>Причина: {reason}</i>"
    sent = await notify.broadcast(request.app["bot"], users, msg)
    db.log_action(eid, me, "broadcast", text[:80])
    return web.json_response({"ok": True, "sent": sent, "total": len(users)})


async def h_report_event(request: web.Request):
    """Этап 5: пожаловаться на событие."""
    me = request["user"]["id"]
    eid = int(request.match_info["id"])
    body = await request.json()
    e = db.get_event(eid)
    if not e:
        return web.json_response({"error": "Событие не найдено"}, status=404)
    if not db.add_report(eid, me, (body.get("reason") or "")[:200]):
        return web.json_response({"ok": True, "already": True})
    cnt = db.report_count(eid)
    for admin in config.ADMIN_IDS:
        try:
            await request.app["bot"].send_message(
                admin, f"🚩 Жалоба на «{e['title']}» (ID {eid}). Всего жалоб: {cnt}.")
        except Exception:
            pass
    return web.json_response({"ok": True})


async def h_org_profile(request: web.Request):
    """Профиль организатора. Модератор видит всё (любые статусы, жалобы); гость — только активные."""
    me = request["user"]["id"]
    org_id = int(request.match_info["id"])
    org = db.get_user(org_id)
    if not org:
        return web.json_response({"error": "not_found"}, status=404)
    is_admin = me in config.ADMIN_IDS
    rows = db.org_events(org_id)
    if not is_admin:
        rows = [e for e in rows if e["status"] in ("active", "past")]
    events, total_guests, total_reports = [], 0, 0
    for e in rows:
        taken = db.tickets_count(e["id"])
        reports = db.report_count(e["id"])
        total_guests += taken
        total_reports += reports
        item = {
            "id": e["id"], "title": e["title"], "starts_at": e["starts_at"],
            "city": e["city"], "cover": e["cover"], "has_cover": bool(e["cover_img"]),
            "cover_ver": e["created_at"], "status": e["status"], "taken": taken,
            "price_text": e["price_text"], "pay_url": e["pay_url"],
        }
        if is_admin:
            item["reports"] = reports
            item["area"] = e["area"]
        events.append(item)
    data = {
        "id": org_id,
        "name": org["first_name"] or "Организатор",
        "username": org["username"],
        "verified": bool(org["is_verified"]),
        "since": org["created_at"],
        "events_count": len(events),
        "total_guests": total_guests,
        "followers": db.follower_count(org_id),
        "following": db.is_following(org_id, me),
        "is_self": me == org_id,
        "is_admin_view": is_admin,
    }
    if is_admin:
        data["total_reports"] = total_reports
    data["events"] = events
    return web.json_response(data)


async def h_org_follow(request: web.Request):
    """Подписка/отписка на организатора (уведомления о новых событиях)."""
    me = request["user"]["id"]
    org_id = int(request.match_info["id"])
    body = await request.json()
    if body.get("follow"):
        db.follow(org_id, me)
    else:
        db.unfollow(org_id, me)
    return web.json_response({"ok": True, "following": db.is_following(org_id, me)})


async def h_org_set_verify(request: web.Request):
    """Модератор выдаёт/снимает галочку доверия."""
    me = request["user"]["id"]
    if me not in config.ADMIN_IDS:
        return web.json_response({"error": "forbidden"}, status=403)
    org_id = int(request.match_info["id"])
    body = await request.json()
    want = bool(body.get("set"))
    db.set_verified(org_id, want)
    try:
        if want:
            await request.app["bot"].send_message(
                org_id, "✅ Тебе выдали галочку проверенного организатора! События публикуются сразу.")
        else:
            await request.app["bot"].send_message(
                org_id, "Галочка проверенного организатора снята. События снова проходят модерацию.")
    except Exception:
        pass
    return web.json_response({"ok": True, "verified": want})


async def notify_followers(bot, event) -> int:
    """Уведомить подписчиков организатора о новом активном событии."""
    if not event or event["status"] != "active":
        return 0
    ids = db.follower_ids(event["org_id"])
    if not ids:
        return 0
    import datetime
    when = datetime.datetime.fromtimestamp(event["starts_at"]).strftime("%d.%m в %H:%M")
    org = db.get_user(event["org_id"])
    name = (org["username"] or org["first_name"]) if org else "организатор"
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="Открыть событие 🎉",
        web_app=WebAppInfo(url=f"{config.WEBAPP_URL}#event/{event['id']}"))]])
    txt = f"🔔 <b>{name}</b> публикует новое событие!\n<b>{event['title']}</b>\n{when} · {event['city']}"
    return await notify.broadcast(bot, ids, txt, reply_markup=kb)


async def h_org_log(request: web.Request):
    """Этап 4: история действий организатора."""
    me = request["user"]["id"]
    rows = db.org_log(me)
    return web.json_response([
        {"action": r["action"], "detail": r["detail"], "title": r["title"],
         "at": r["created_at"]} for r in rows
    ])


async def h_my_history(request: web.Request):
    """Этап 4: прошедшие/посещённые события гостя."""
    me = request["user"]["id"]
    rows = db.user_past_events(me)
    return web.json_response([
        {"id": r["id"], "title": r["title"], "starts_at": r["starts_at"], "city": r["city"],
         "cover": r["cover"], "ticket_status": r["ticket_status"], "kind": r["kind"]}
        for r in rows
    ])


async def h_photo(request: web.Request):
    """Доп. фото карусели (публично)."""
    eid = int(request.match_info["id"])
    idx = int(request.match_info["idx"])
    img = db.get_event_photo(eid, idx)
    if not img:
        return web.Response(status=404)
    return web.Response(body=img, content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


async def h_cover(request: web.Request):
    """Картинка-обложка события (публично, без авторизации — это просто постер)."""
    eid = int(request.match_info["id"])
    img = db.get_cover(eid)
    if not img:
        return web.Response(status=404)
    return web.Response(body=img, content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


# ---------- статус организатора ----------

async def h_me_status(request: web.Request):
    me = request["user"]["id"]
    u = db.get_user(me)
    return web.json_response({"verified": bool(u and u["is_verified"])})


async def h_request_verify(request: web.Request):
    """Заявка организатора на галочку доверия → модераторам."""
    me = request["user"]["id"]
    u = db.get_user(me)
    if u and u["is_verified"]:
        return web.json_response({"ok": True, "already": True})
    if not config.ADMIN_IDS:
        return web.json_response({"error": "Модерация не настроена"}, status=400)
    name = request["user"].get("first_name", "Организатор")
    uname = request["user"].get("username")
    n_events = len(db.org_events(me))
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 Профиль организатора",
                              web_app=WebAppInfo(url=f"{config.WEBAPP_URL}#org/{me}"))],
        [InlineKeyboardButton(text="✅ Выдать галочку", callback_data=f"vrf_ok_{me}"),
         InlineKeyboardButton(text="🚫 Отказать", callback_data=f"vrf_no_{me}")],
    ])
    txt = (f"🛡 <b>Заявка на проверенного организатора</b>\n"
           f"{name}" + (f" @{uname}" if uname else "") + f" (id {me})\n"
           f"Событий создано: {n_events}")
    for admin in config.ADMIN_IDS:
        try:
            await request.app["bot"].send_message(admin, txt, reply_markup=kb)
        except Exception:
            pass
    return web.json_response({"ok": True})


async def h_attend(request: web.Request):
    """«Я иду»: добавляем пользователя в список участников. Без QR/пруфов —
    просто запись + напоминание за 2 часа до начала."""
    me = request["user"]["id"]
    event_id = int(request.match_info["id"])
    e = db.get_event(event_id)
    if not e or e["status"] != "active":
        return web.json_response({"error": "Событие не найдено"}, status=404)
    if e["org_id"] == me:
        return web.json_response({"error": "Это твоё событие"}, status=400)
    if db.get_user_ticket(event_id, me):
        return web.json_response({"ok": True, "already": True})
    status, _code = db.create_ticket_capped(event_id, me, "going", e["capacity"] or 0)
    if status == db.TICKET_SOLD_OUT:
        return web.json_response({"error": "Мест больше нет 😢"}, status=400)
    if status == db.TICKET_DUP:
        return web.json_response({"ok": True, "already": True})
    return web.json_response({"ok": True})


async def h_unattend(request: web.Request):
    """Отмена участия («Я иду» → отменить)."""
    me = request["user"]["id"]
    event_id = int(request.match_info["id"])
    db.remove_user_ticket(event_id, me)
    return web.json_response({"ok": True})


async def h_claim_free(request: web.Request):
    """Забрать free-билет: проверяем подписку, рефералов, лимит мест."""
    me = request["user"]["id"]
    event_id = int(request.match_info["id"])
    e = db.get_event(event_id)
    if not e or e["status"] != "active":
        return web.json_response({"error": "Туса не найдена"}, status=404)
    if e["org_id"] == me:
        return web.json_response({"error": "Это твоё событие"}, status=400)
    if db.get_user_ticket(event_id, me):
        return web.json_response({"error": "У тебя уже есть билет 😉"}, status=400)
    if e["capacity"] and db.tickets_count(event_id) >= e["capacity"]:
        return web.json_response({"error": "Мест больше нет 😢"}, status=400)

    bot = request.app["bot"]
    from bot import check_subscribed

    if not await check_subscribed(bot, e["channel"], me):
        return web.json_response(
            {"error": f"Сначала подпишись на @{e['channel']}", "need": "subscribe"}, status=400
        )

    # пересчёт валидных рефералов: приглашённый должен быть подписан на канал прямо сейчас
    refs = db.referrals_of(event_id, me)
    valid = 0
    if e["channel"]:
        for r in refs:
            if await check_subscribed(bot, e["channel"], r["referred_id"]):
                valid += 1
    else:
        # если канала нет — все рефералы считаются валидными
        valid = len(refs)
    valid += db.get_referral_bonus(event_id, me)  # модераторский бонус
    if valid < e["refs_needed"]:
        need_more = e["refs_needed"] - valid
        return web.json_response(
            {"error": f"Приведи ещё {need_more} друз.", "need": "refs",
             "have": valid, "needed": e["refs_needed"]},
            status=400,
        )

    # атомарная выдача с учётом лимита — без перепродажи даже при наплыве
    status, code = db.create_ticket_capped(event_id, me, "free", e["capacity"] or 0)
    if status == db.TICKET_SOLD_OUT:
        return web.json_response({"error": "Мест больше нет 😢"}, status=400)
    if status == db.TICKET_DUP:
        return web.json_response({"error": "У тебя уже есть билет 😉"}, status=400)
    return web.json_response({"code": code})


async def h_claim_paid(request: web.Request):
    """«Я купил билет» (этап 2): заявка + скрин/PDF → орг одобряет/отклоняет в боте."""
    me = request["user"]["id"]
    event_id = int(request.match_info["id"])
    e = db.get_event(event_id)
    if not e or e["status"] != "active":
        return web.json_response({"error": "Событие не найдено"}, status=404)
    if e["org_id"] == me:
        return web.json_response({"error": "Это твоё событие"}, status=400)
    if db.get_user_ticket(event_id, me):  # антидубль: один билет на человека
        return web.json_response({"error": "У тебя уже есть заявка/билет на это событие"}, status=400)
    body = await request.json()
    proof = _decode_cover(body.get("proof_data", ""))
    mime = "application/pdf" if (body.get("proof_data", "").startswith("data:application/pdf")) else "image/jpeg"
    code = db.create_pending_with_proof(event_id, me, proof, mime)
    if not code:
        return web.json_response({"error": "Заявка уже есть"}, status=400)
    # уведомляем организатора: новый гость + файл + кнопки
    if proof:
        await _notify_org_new_guest(request.app["bot"], e, request["user"], code)
    return web.json_response({"code": code, "pending": True})


async def _notify_org_new_guest(bot, e, user, code: str):
    from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                               BufferedInputFile)
    name = user.get("first_name", "Гость")
    uname = f" @{user['username']}" if user.get("username") else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"tk_ok_{code}"),
        InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"tk_no_{code}"),
    ]])
    caption = (f"🎫 <b>Новый гость</b> на «{e['title']}»\n"
               f"{name}{uname} (id {user['id']})\nПроверь билет и реши:")
    proof = db.get_proof(code)
    try:
        if proof and proof[1] == "application/pdf":
            await bot.send_document(e["org_id"], BufferedInputFile(proof[0], "ticket.pdf"),
                                    caption=caption, reply_markup=kb)
        elif proof:
            await bot.send_photo(e["org_id"], BufferedInputFile(proof[0], "ticket.jpg"),
                                 caption=caption, reply_markup=kb)
        else:
            await bot.send_message(e["org_id"], caption, reply_markup=kb)
    except Exception as ex:
        log.warning("notify org new guest failed: %s", ex)


async def h_my_tickets(request: web.Request):
    me = request["user"]["id"]
    out = []
    for t in db.user_tickets(me):
        out.append({
            "code": t["code"], "kind": t["kind"], "status": t["status"],
            "event_id": t["event_id"], "event_status": t["event_status"],
            "title": t["title"], "starts_at": t["starts_at"], "ends_at": t["ends_at"],
            "area": t["area"], "age_limit": t["age_limit"], "cover": t["cover"],
            "address": t["address"],
            "has_cover": bool(t["has_cover"]) if "has_cover" in t.keys() else False,
            "cover_ver": t["cover_ver"] if "cover_ver" in t.keys() else 0,
        })
    return web.json_response(out)


async def h_my_events(request: web.Request):
    me = request["user"]["id"]
    events = db.org_events(me)
    if not events:
        return web.json_response([])
    eids = [e["id"] for e in events]
    taken_map = db.tickets_counts_batch(eids)
    photo_map = db.photo_counts_batch(eids)
    org = db.get_user(me)
    now_ts = int(time.time())
    result = []
    for e in events:
        taken = taken_map.get(e["id"], 0)
        result.append({
            "id": e["id"], "title": e["title"], "description": e["description"],
            "starts_at": e["starts_at"], "area": e["area"],
            "price_text": e["price_text"], "pay_url": e["pay_url"],
            "capacity": e["capacity"], "refs_needed": e["refs_needed"],
            "channel": e["channel"], "age_limit": e["age_limit"],
            "cover": e["cover"], "city": e["city"], "genre": e["genre"],
            "taken": taken,
            "sold_out": bool((e["capacity"] and taken >= e["capacity"])
                             or ("soldout" in e.keys() and e["soldout"])),
            "is_mine": True, "org_id": e["org_id"], "status": e["status"],
            "ends_at": e["ends_at"] if "ends_at" in e.keys() else 0,
            "featured": bool(e["featured_until"] and e["featured_until"] > now_ts) if "featured_until" in e.keys() else False,
            "soldout_flag": bool(e["soldout"]) if "soldout" in e.keys() else False,
            "has_cover": bool(e["cover_img"]) if "cover_img" in e.keys() else False,
            "cover_ver": e["created_at"],
            "photos": photo_map.get(e["id"], 0),
            "promo_code": e["promo_code"] if "promo_code" in e.keys() else "",
            "host": (org["username"] or org["first_name"] or "host") if org else "host",
            "host_verified": bool(org and org["is_verified"]) if org else False,
        })
    return web.json_response(result)


async def h_guests(request: web.Request):
    me = request["user"]["id"]
    event_id = int(request.match_info["id"])
    e = db.get_event(event_id)
    is_admin = me in config.ADMIN_IDS
    if not e or (e["org_id"] != me and not is_admin):
        return web.json_response({"error": "forbidden"}, status=403)
    guests_fn = db.admin_all_tickets if is_admin else db.event_guests
    out = []
    for g in guests_fn(event_id):
        out.append({
            "code": g["code"], "kind": g["kind"], "status": g["status"],
            "name": g["first_name"], "username": g["username"],
            "user_id": g["user_id"],
        })
    return web.json_response(out)


async def h_approve(request: web.Request):
    """Организатор или админ подтверждает оплату заявки."""
    me = request["user"]["id"]
    body = await request.json()
    t = db.get_ticket(body.get("code", ""))
    is_admin = me in config.ADMIN_IDS
    if not t or (t["org_id"] != me and not is_admin):
        return web.json_response({"error": "forbidden"}, status=403)
    db.approve_ticket(t["code"])
    bot = request.app["bot"]
    try:
        await bot.send_message(
            t["user_id"],
            f"Оплата подтверждена — билет на «{t['title']}» у тебя! 🎟 Смотри вкладку «Билеты».",
        )
    except Exception:
        pass
    return web.json_response({"ok": True})


# ---------- admin god-mode: управление билетами ----------

def _require_admin(request) -> int:
    me = request["user"]["id"]
    if me not in config.ADMIN_IDS:
        raise web.HTTPForbidden(text='{"error":"admin only"}', content_type="application/json")
    return me


async def h_admin_create_ticket(request: web.Request):
    """Админ вручную выдаёт билет пользователю."""
    _require_admin(request)
    body = await request.json()
    event_id = int(body.get("event_id", 0))
    user_id = int(body.get("user_id", 0))
    kind = body.get("kind", "paid")
    if kind not in ("free", "paid"):
        return web.json_response({"error": "kind must be free or paid"}, status=400)
    e = db.get_event(event_id)
    if not e:
        return web.json_response({"error": "Событие не найдено"}, status=404)
    u = db.get_user(user_id)
    if not u:
        return web.json_response({"error": "Пользователь не найден"}, status=404)
    code = db.admin_create_ticket(event_id, user_id, kind)
    if not code:
        return web.json_response({"error": "У этого пользователя уже есть билет"}, status=400)
    # уведомляем пользователя
    bot = request.app["bot"]
    try:
        await bot.send_message(
            user_id,
            f"🎟 Модератор выдал тебе билет на «{e['title']}»! Смотри вкладку «Билеты».",
        )
    except Exception:
        pass
    return web.json_response({"ok": True, "code": code})


async def h_admin_set_refs(request: web.Request):
    """Модератор вручную задаёт счётчик приглашённых пользователю на событие.
    Значение — итоговое число, которое увидит пользователь (бонус = value - реальные)."""
    _require_admin(request)
    body = await request.json()
    event_id = int(body.get("event_id", 0))
    user_id = int(body.get("user_id", 0))
    value = int(body.get("value", 0))
    e = db.get_event(event_id)
    if not e:
        return web.json_response({"error": "Событие не найдено"}, status=404)
    if not db.get_user(user_id):
        return web.json_response({"error": "Пользователь не найден"}, status=404)
    real = len(db.referrals_of(event_id, user_id))
    bonus = max(0, value - real)
    db.set_referral_bonus(event_id, user_id, bonus)
    db.log_action(event_id, user_id, "refs_set", str(value))
    return web.json_response({"ok": True, "value": real + bonus, "real": real, "bonus": bonus})


async def h_admin_delete_ticket(request: web.Request):
    """Админ удаляет билет полностью."""
    _require_admin(request)
    body = await request.json()
    code = body.get("code", "")
    t = db.get_ticket(code)
    if not t:
        return web.json_response({"error": "Билет не найден"}, status=404)
    db.admin_delete_ticket(code)
    return web.json_response({"ok": True})


async def h_admin_update_ticket(request: web.Request):
    """Админ меняет статус и/или тип билета."""
    _require_admin(request)
    body = await request.json()
    code = body.get("code", "")
    t = db.get_ticket(code)
    if not t:
        return web.json_response({"error": "Билет не найден"}, status=404)
    new_status = body.get("status")
    new_kind = body.get("kind")
    if new_status:
        if new_status not in ("active", "used", "revoked"):
            return web.json_response({"error": "status: active/used/revoked"}, status=400)
        db.admin_set_ticket_status(code, new_status)
    if new_kind:
        if new_kind not in ("free", "paid", "paid_pending"):
            return web.json_response({"error": "kind: free/paid/paid_pending"}, status=400)
        db.admin_set_ticket_kind(code, new_kind)
    return web.json_response({"ok": True})


async def h_meta(request: web.Request):
    """Имя бота для построения t.me-ссылок на клиенте (кэшируется)."""
    app = request.app
    if "bot_username" not in app:
        app["bot_username"] = (await app["bot"].get_me()).username
    return web.json_response({"bot": app["bot_username"]})


async def h_gate(request: web.Request):
    """Проверка обязательной подписки на канал (свежая, без кэша)."""
    me = request["user"]["id"]
    if not config.REQUIRED_CHANNEL:
        return web.json_response({"subscribed": True})
    from bot import check_subscribed
    ok = await check_subscribed(request.app["bot"], config.REQUIRED_CHANNEL, me)
    _sub_cache[me] = (ok, time.time())
    return web.json_response({"subscribed": ok, "channel": config.REQUIRED_CHANNEL})


async def h_channel_check(request: web.Request):
    """Проверка: добавлен ли бот админом в канал (для входа за подписку/друзей)."""
    me = request["user"]["id"]  # noqa: F841 (нужна авторизация)
    body = await request.json()
    channel = (body.get("channel") or "").strip().lstrip("@")
    if not channel:
        return web.json_response({"ok": False, "error": "Пусто"}, status=400)
    from bot import bot_is_admin
    ok = await bot_is_admin(request.app["bot"], channel)
    bu = request.app.get("bot_username") or (await request.app["bot"].get_me()).username
    request.app["bot_username"] = bu
    return web.json_response({"bot_admin": ok, "channel": channel, "bot": bu})


async def h_health(request: web.Request):
    """Проверка живости + где лежит база и сколько в ней данных (для контроля Volume)."""
    try:
        c = db.conn()
        events = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        tickets = c.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        import os
        data_dir_exists = os.path.isdir("/data")
        persistent = config.DB_PATH.startswith("/data") and data_dir_exists
        return web.json_response({
            "ok": True,
            "persistent_volume": persistent,            # пишем ли на постоянный диск
            "events": events, "tickets": tickets, "users": users,
        })
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def h_index(request: web.Request):
    return web.FileResponse(WEBAPP_DIR / "index.html")


def make_web_app(bot) -> web.Application:
    app = web.Application(middlewares=[security_middleware, auth_middleware],
                          client_max_size=5 * 1024 * 1024)  # 5MB max body
    app["bot"] = bot
    app.add_routes([
        web.get("/", h_index),
        web.get("/health", h_health),
        web.get("/cover/{id}", h_cover),
        web.get("/photo/{id}/{idx}", h_photo),
        web.get("/api/events", h_events),
        web.post("/api/events/{id}/edit", h_edit_event),
        web.post("/api/events/{id}/soldout", h_soldout),
        web.post("/api/events/{id}/delete", h_delete_event),
        web.post("/api/events/{id}/reschedule", h_reschedule_event),
        web.post("/api/events/{id}/broadcast", h_broadcast),
        web.post("/api/events/{id}/report", h_report_event),
        web.get("/api/cities", h_cities),
        web.get("/api/meta", h_meta),
        web.get("/api/gate", h_gate),
        web.post("/api/channel/check", h_channel_check),
        web.post("/api/events", h_create_event),
        web.get("/api/events/{id}", h_event),
        web.post("/api/events/{id}/attend", h_attend),
        web.post("/api/events/{id}/unattend", h_unattend),
        web.post("/api/events/{id}/claim_free", h_claim_free),
        web.post("/api/events/{id}/claim_paid", h_claim_paid),
        web.get("/api/events/{id}/guests", h_guests),
        web.get("/api/me/tickets", h_my_tickets),
        web.get("/api/me/events", h_my_events),
        web.get("/api/me/history", h_my_history),
        web.get("/api/me/log", h_org_log),
        web.get("/api/org/{id}", h_org_profile),
        web.post("/api/org/{id}/follow", h_org_follow),
        web.post("/api/org/{id}/verify", h_org_set_verify),
        web.post("/api/approve", h_approve),
        web.post("/api/admin/ticket/create", h_admin_create_ticket),
        web.post("/api/admin/refs", h_admin_set_refs),
        web.post("/api/admin/ticket/delete", h_admin_delete_ticket),
        web.post("/api/admin/ticket/update", h_admin_update_ticket),
        web.get("/api/me/status", h_me_status),
        web.post("/api/request_verify", h_request_verify),
    ])
    return app
