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
import qtickets

log = logging.getLogger("tusa.api")
WEBAPP_DIR = Path(__file__).parent / "webapp"


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
        db.upsert_user(user["id"], user.get("username"), user.get("first_name"))
        request["user"] = user
        # шлагбаум: обязательная подписка на канал
        if config.REQUIRED_CHANNEL and request.path not in _GATE_FREE:
            if not await user_subscribed(request.app["bot"], user["id"]):
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
    resp.headers["X-Frame-Options"] = "ALLOW-FROM https://web.telegram.org"
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
        "org_id": e["org_id"],
        "status": e["status"],
        "ends_at": e["ends_at"] if "ends_at" in e.keys() else 0,
        "featured": bool(e["featured_until"] and e["featured_until"] > int(time.time())) if "featured_until" in e.keys() else False,
        "soldout_flag": bool(e["soldout"]) if "soldout" in e.keys() else False,
        "has_cover": bool(e["cover_img"]) if "cover_img" in e.keys() else False,
        "cover_ver": e["created_at"],   # версия для обхода кэша картинок
        "photos": db.event_photo_count(e["id"]),
    }
    org = db.get_user(e["org_id"])
    d["host"] = (org["username"] or org["first_name"] or "host") if org else "host"
    d["host_verified"] = bool(org and org["is_verified"]) if org else False
    return d


# ---------- handlers ----------

async def h_events(request: web.Request):
    me = request["user"]["id"]
    city = request.query.get("city") or None
    return web.json_response([event_json(e, me) for e in db.list_events(city=city)])


async def h_cities(request: web.Request):
    return web.json_response(db.city_counts())


async def h_event(request: web.Request):
    e = db.get_event(int(request.match_info["id"]))
    if not e:
        return web.json_response({"error": "not_found"}, status=404)
    me = request["user"]["id"]
    data = event_json(e, me)

    # мой билет и прогресс рефералки
    t = db.get_user_ticket(e["id"], me)
    data["my_ticket"] = (
        {"code": t["code"], "kind": t["kind"], "status": t["status"]} if t else None
    )
    refs = db.referrals_of(e["id"], me)
    data["my_refs"] = len(refs)

    bot = request.app["bot"]
    from bot import check_subscribed  # локальный импорт, чтобы не плодить циклы

    data["subscribed"] = await check_subscribed(bot, e["channel"], me)
    # точный адрес видят владелец и модераторы (для проверки), гостям — скрыт
    if me == e["org_id"] or me in config.ADMIN_IDS:
        data["address"] = e["address"]
        data["is_admin_view"] = me in config.ADMIN_IDS and me != e["org_id"]
    me_username = (await bot.get_me()).username
    data["ref_link"] = f"https://t.me/{me_username}?start=ref_{e['id']}_{me}"
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
        price = e["price_text"] or ("ссылка qtickets" if e["pay_url"] else "free")
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
    body = await request.json()
    required = ("title", "starts_at")
    if any(not body.get(k) for k in required):
        return web.json_response({"error": "title и дата обязательны"}, status=400)
    if len(body.get("title", "")) > 80:
        return web.json_response({"error": "слишком длинное название"}, status=400)
    # привязка к qtickets: достаём id события из ссылки оплаты
    if body.get("pay_url"):
        body["qt_event_id"] = qtickets.parse_event_id(body["pay_url"])
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
    if not e or e["org_id"] != me:
        return web.json_response({"error": "Это не твоё событие"}, status=403)
    body = await request.json()
    if "title" in body and not str(body["title"]).strip():
        return web.json_response({"error": "Название не может быть пустым"}, status=400)
    if "title" in body and len(body["title"]) > 80:
        return web.json_response({"error": "слишком длинное название"}, status=400)
    if body.get("starts_at") and int(body["starts_at"]) <= time.time():
        return web.json_response({"error": "Дата должна быть в будущем"}, status=400)
    # привязка qtickets, если поменяли ссылку
    if "pay_url" in body:
        body["qt_event_id"] = qtickets.parse_event_id(body.get("pay_url", ""))
    # обложка: меняем только если прислали новую (set_cover) или явно сбросили
    set_cover = "cover_data" in body
    cover_img = _decode_cover(body.get("cover_data", "")) if body.get("cover_data") else None
    moderate = _moderation_on() and not db.is_verified(me)
    status = "pending" if moderate else "active"
    ok = db.update_event(eid, me, body, status, cover_img=cover_img, set_cover=set_cover)
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
    if not e or e["org_id"] != me:
        return web.json_response({"error": "Не получилось — это не твоё событие"}, status=403)
    users = db.event_ticket_users(eid)
    db.delete_event(eid, me)
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
    if not e or e["org_id"] != me:
        return web.json_response({"error": "Это не твоё событие"}, status=403)
    users = db.event_ticket_users(eid)
    moderate = _moderation_on() and not db.is_verified(me)
    status = "pending" if moderate else "active"
    db.reschedule_event(eid, me, new_starts, status, new_ends)
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
    if not e or e["org_id"] != me:
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
        data["qtickets"] = bool(org["qtickets_token"])
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


# ---------- qtickets ----------

async def h_qtickets_status(request: web.Request):
    me = request["user"]["id"]
    u = db.get_user(me)
    return web.json_response({
        "connected": bool(u and u["qtickets_token"]),
        "verified": bool(u and u["is_verified"]),
    })


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


async def h_qtickets_connect(request: web.Request):
    me = request["user"]["id"]
    body = await request.json()
    token = (body.get("token") or "").strip()
    if not token:
        # пустой токен = отключить
        db.set_qtickets_token(me, "")
        return web.json_response({"connected": False})
    ok = await asyncio.get_event_loop().run_in_executor(None, qtickets.check_token, token)
    if not ok:
        return web.json_response({"error": "Токен не подошёл. Проверь, что скопировал целиком из «Настройки → Основное» в qtickets."}, status=400)
    db.set_qtickets_token(me, token)
    # сразу импортируем все события организатора из qtickets
    imported = await _import_qtickets_events(me, token)
    return web.json_response({"connected": True, "imported": imported})


async def _import_qtickets_events(org_id: int, token: str) -> int:
    """Тянет события организатора из qtickets и заводит недостающие в боте."""
    loop = asyncio.get_event_loop()
    events = await loop.run_in_executor(None, qtickets.list_my_events, token)
    created = 0
    verified = db.is_verified(org_id)
    for ev in events:
        qt_id = ev.get("id")
        if not qt_id or db.event_by_qt(org_id, int(qt_id)):
            continue
        starts, ends = qtickets.event_times(ev)
        if not starts or starts < time.time() - 6 * 3600:
            continue  # пропускаем прошедшие/без даты
        data = {
            "title": (ev.get("name") or "Событие")[:80],
            "description": (ev.get("description") or "")[:1500],
            "starts_at": starts, "ends_at": ends,
            "area": ev.get("place_name") or "",
            "address": ev.get("place_address") or "",
            "city": "Москва",
            "pay_url": ev.get("site_url") or f"https://qtickets.ru/event/{qt_id}",
            "qt_event_id": int(qt_id),
            "cover": "ultramarine",
        }
        # импортированные с реального qtickets-аккаунта считаем достоверными
        status = "active" if verified else "pending"
        eid = db.create_event(org_id, data, status=status)
        created += 1
    return created


async def h_qtickets_import(request: web.Request):
    """Повторный импорт/синхронизация событий организатора из qtickets."""
    me = request["user"]["id"]
    u = db.get_user(me)
    if not u or not u["qtickets_token"]:
        return web.json_response({"error": "Сначала подключи qtickets", "need": "connect"}, status=400)
    imported = await _import_qtickets_events(me, u["qtickets_token"])
    return web.json_response({"ok": True, "imported": imported})


async def h_qtickets_preview(request: web.Request):
    """Показать типы билетов по ссылке qtickets (для формы создания)."""
    me = request["user"]["id"]
    u = db.get_user(me)
    if not u or not u["qtickets_token"]:
        return web.json_response({"error": "Сначала подключи qtickets в профиле", "need": "connect"}, status=400)
    body = await request.json()
    eid = qtickets.parse_event_id(body.get("url", ""))
    if not eid:
        return web.json_response({"error": "Не похоже на ссылку qtickets на событие"}, status=400)
    loop = asyncio.get_event_loop()
    types = await loop.run_in_executor(None, qtickets.get_ticket_types, u["qtickets_token"], eid)
    fields = await loop.run_in_executor(None, qtickets.event_fields, u["qtickets_token"], eid)
    return web.json_response({"qt_event_id": eid, "types": types, "fields": fields})


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
    for r in refs:
        if await check_subscribed(bot, e["channel"], r["referred_id"]):
            valid += 1
    if valid < e["refs_needed"]:
        return web.json_response(
            {"error": f"Приведи ещё {e['refs_needed'] - valid} друз.", "need": "refs",
             "have": valid, "needed": e["refs_needed"]},
            status=400,
        )

    code = db.create_ticket(event_id, me, "free")
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
        # точный адрес отдаём только незадолго до начала
        reveal = t["starts_at"] - time.time() <= config.ADDRESS_REVEAL_HOURS * 3600
        out.append({
            "code": t["code"], "kind": t["kind"], "status": t["status"],
            "event_id": t["event_id"], "event_status": t["event_status"],
            "title": t["title"], "starts_at": t["starts_at"], "ends_at": t["ends_at"],
            "area": t["area"], "age_limit": t["age_limit"], "cover": t["cover"],
            "address": t["address"] if reveal else None,
            # билет через qtickets: настоящий билет выдаёт qtickets, наш QR не нужен
            "qtickets": bool(t["qt_event_id"]),
        })
    return web.json_response(out)


async def h_my_events(request: web.Request):
    me = request["user"]["id"]
    return web.json_response([event_json(e, me) for e in db.org_events(me)])


async def h_guests(request: web.Request):
    me = request["user"]["id"]
    event_id = int(request.match_info["id"])
    e = db.get_event(event_id)
    if not e or e["org_id"] != me:
        return web.json_response({"error": "forbidden"}, status=403)
    out = []
    for g in db.event_guests(event_id):
        out.append({
            "code": g["code"], "kind": g["kind"], "status": g["status"],
            "name": g["first_name"], "username": g["username"],
        })
    return web.json_response(out)


async def h_approve(request: web.Request):
    """Организатор подтверждает оплату заявки."""
    me = request["user"]["id"]
    body = await request.json()
    t = db.get_ticket(body.get("code", ""))
    if not t or t["org_id"] != me:
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


async def h_scan(request: web.Request):
    """Сканер на входе: погасить билет по коду QR."""
    me = request["user"]["id"]
    body = await request.json()
    t = db.get_ticket(body.get("code", ""))
    if not t:
        return web.json_response({"ok": False, "msg": "Билет не найден ❌"})
    if t["org_id"] != me:
        return web.json_response({"ok": False, "msg": "Это билет не на твою тусу"})
    name = t["first_name"] + (f" (@{t['username']})" if t["username"] else "")
    if t["kind"] == "paid_pending":
        return web.json_response({"ok": False, "msg": f"{name}: оплата НЕ подтверждена ⚠️"})
    if t["status"] == "used":
        return web.json_response({"ok": False, "msg": f"{name}: билет УЖЕ использован ⚠️"})
    if t["status"] != "active":
        return web.json_response({"ok": False, "msg": "Билет недействителен ❌"})
    db.set_ticket_status(t["code"], "used")
    return web.json_response({"ok": True, "msg": f"✅ {name} — проходит!"})


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
            "ok": True, "db_path": config.DB_PATH,
            "data_dir_exists": data_dir_exists,         # виден ли смонтированный Volume
            "persistent_volume": persistent,
            "events": events, "tickets": tickets, "users": users,
        })
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def h_index(request: web.Request):
    return web.FileResponse(WEBAPP_DIR / "index.html")


# ---------- авто-отслеживание оплат qtickets ----------

async def poll_qtickets_payments(bot) -> None:
    """Опрашивает qtickets по привязанным событиям и автоматически выдаёт билеты
    тем, кто оплатил (матчинг по telegram_user / utm_content)."""
    loop = asyncio.get_event_loop()
    for e in db.events_with_qtickets():
        token, qt_eid, event_id = e["qtickets_token"], e["qt_event_id"], e["id"]
        # авто sold-out: спрашиваем остаток мест у qtickets
        try:
            sold = await loop.run_in_executor(None, qtickets.is_sold_out, token, qt_eid)
            if sold is not None and bool(e["soldout"]) != sold:
                db.set_soldout(event_id, e["org_id"], sold, force=True)
        except Exception as ex:
            log.warning("auto soldout event=%s failed: %s", event_id, ex)
        try:
            orders = await loop.run_in_executor(None, qtickets.list_paid_orders, token, qt_eid)
        except Exception as ex:
            log.warning("poll qtickets event=%s failed: %s", event_id, ex)
            continue
        for order in orders:
            oid = str(order.get("id") or order.get("uniqid") or "")
            if not oid or db.order_already_used(oid):
                continue
            tg_id = qtickets.extract_tg_id(order)
            if not tg_id:
                continue  # не смогли сопоставить — оставим на ручное подтверждение
            db.upsert_user(tg_id, None, None)
            existing = db.get_user_ticket(event_id, tg_id)
            if existing and existing["kind"] == "paid_pending":
                db.mark_paid_by_order(existing["code"], oid)
                code = existing["code"]
            elif existing:
                continue  # уже есть билет — пропускаем
            else:
                code = db.create_paid_ticket_direct(event_id, tg_id, oid)
                if not code:
                    continue
            try:
                await bot.send_message(
                    tg_id,
                    f"Оплата получена — билет на «{e['title']}» у тебя! 🎟\nСмотри вкладку «Билеты».",
                )
            except Exception:
                pass
            log.info("qtickets auto-issued ticket event=%s tg=%s order=%s", event_id, tg_id, oid)


def make_web_app(bot) -> web.Application:
    app = web.Application(middlewares=[security_middleware, auth_middleware])
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
        web.post("/api/scan", h_scan),
        web.get("/api/qtickets/status", h_qtickets_status),
        web.post("/api/qtickets/connect", h_qtickets_connect),
        web.post("/api/qtickets/preview", h_qtickets_preview),
        web.post("/api/qtickets/import", h_qtickets_import),
        web.post("/api/request_verify", h_request_verify),
    ])
    return app
