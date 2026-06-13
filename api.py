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


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path.startswith("/api/"):
        user = validate_init_data(request.headers.get("X-Init-Data", ""))
        if not user:
            return web.json_response({"error": "unauthorized"}, status=401)
        db.upsert_user(user["id"], user.get("username"), user.get("first_name"))
        request["user"] = user
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
        "sold_out": bool(e["capacity"] and taken >= e["capacity"]),
        "is_mine": me_id == e["org_id"],
        "status": e["status"],
        "ends_at": e["ends_at"] if "ends_at" in e.keys() else 0,
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
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [open_btn],
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"mod_ok_{event_id}"),
         InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"mod_no_{event_id}")],
    ])
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
    return web.json_response({"id": event_id, "status": status})


async def h_delete_event(request: web.Request):
    me = request["user"]["id"]
    eid = int(request.match_info["id"])
    e = db.get_event(eid)
    if not e or e["org_id"] != me:
        return web.json_response({"error": "Не получилось — это не твоё событие"}, status=403)
    users = db.event_ticket_users(eid)
    db.delete_event(eid, me)
    # этап 3: уведомить всех, кто шёл
    if users:
        await notify.broadcast(request.app["bot"], users,
                               f"❌ Событие «{e['title']}» отменено организатором.")
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
        await notify.broadcast(request.app["bot"], users,
                               f"🗓 Событие «{e['title']}» перенесено на {when}.")
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
    return web.json_response({"connected": bool(u and u["qtickets_token"])})


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
    return web.json_response({"connected": True})


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
    types = await asyncio.get_event_loop().run_in_executor(
        None, qtickets.get_ticket_types, u["qtickets_token"], eid)
    return web.json_response({"qt_event_id": eid, "types": types})


async def h_claim_free(request: web.Request):
    """Забрать free-билет: проверяем подписку, рефералов, лимит мест."""
    me = request["user"]["id"]
    event_id = int(request.match_info["id"])
    e = db.get_event(event_id)
    if not e or e["status"] != "active":
        return web.json_response({"error": "Туса не найдена"}, status=404)
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


async def h_health(request: web.Request):
    """Проверка живости + где лежит база и сколько в ней данных (для контроля Volume)."""
    try:
        c = db.conn()
        events = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        tickets = c.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        persistent = config.DB_PATH.startswith("/data")
        return web.json_response({
            "ok": True, "db_path": config.DB_PATH, "persistent_volume": persistent,
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
        web.post("/api/events/{id}/delete", h_delete_event),
        web.post("/api/events/{id}/reschedule", h_reschedule_event),
        web.post("/api/events/{id}/broadcast", h_broadcast),
        web.post("/api/events/{id}/report", h_report_event),
        web.get("/api/cities", h_cities),
        web.get("/api/meta", h_meta),
        web.post("/api/events", h_create_event),
        web.get("/api/events/{id}", h_event),
        web.post("/api/events/{id}/claim_free", h_claim_free),
        web.post("/api/events/{id}/claim_paid", h_claim_paid),
        web.get("/api/events/{id}/guests", h_guests),
        web.get("/api/me/tickets", h_my_tickets),
        web.get("/api/me/events", h_my_events),
        web.get("/api/me/history", h_my_history),
        web.get("/api/me/log", h_org_log),
        web.post("/api/approve", h_approve),
        web.post("/api/scan", h_scan),
        web.get("/api/qtickets/status", h_qtickets_status),
        web.post("/api/qtickets/connect", h_qtickets_connect),
        web.post("/api/qtickets/preview", h_qtickets_preview),
    ])
    return app
