"""HTTP API для Mini App + раздача статики (webapp/index.html)."""
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


def event_json(e, me_id: int | None = None) -> dict:
    taken = db.tickets_count(e["id"])
    d = {
        "id": e["id"], "title": e["title"], "description": e["description"],
        "starts_at": e["starts_at"], "area": e["area"],
        "price_text": e["price_text"], "pay_url": e["pay_url"],
        "capacity": e["capacity"], "refs_needed": e["refs_needed"],
        "channel": e["channel"], "age_limit": e["age_limit"],
        "taken": taken,
        "sold_out": bool(e["capacity"] and taken >= e["capacity"]),
        "is_mine": me_id == e["org_id"],
    }
    return d


# ---------- handlers ----------

async def h_events(request: web.Request):
    me = request["user"]["id"]
    return web.json_response([event_json(e, me) for e in db.list_events()])


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
    me_username = (await bot.get_me()).username
    data["ref_link"] = f"https://t.me/{me_username}?start=ref_{e['id']}_{me}"
    data["share_link"] = f"https://t.me/{me_username}?start=evt_{e['id']}"
    return web.json_response(data)


async def h_create_event(request: web.Request):
    me = request["user"]["id"]
    body = await request.json()
    required = ("title", "starts_at")
    if any(not body.get(k) for k in required):
        return web.json_response({"error": "title и дата обязательны"}, status=400)
    if len(body.get("title", "")) > 80:
        return web.json_response({"error": "слишком длинное название"}, status=400)
    event_id = db.create_event(me, body)
    return web.json_response({"id": event_id})


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
    """«Я купил билет» — заявка, организатор подтверждает в панели."""
    me = request["user"]["id"]
    event_id = int(request.match_info["id"])
    e = db.get_event(event_id)
    if not e or e["status"] != "active":
        return web.json_response({"error": "Туса не найдена"}, status=404)
    if db.get_user_ticket(event_id, me):
        return web.json_response({"error": "Заявка уже есть"}, status=400)
    code = db.create_ticket(event_id, me, "paid_pending")
    return web.json_response({"code": code, "pending": True})


async def h_my_tickets(request: web.Request):
    me = request["user"]["id"]
    out = []
    for t in db.user_tickets(me):
        out.append({
            "code": t["code"], "kind": t["kind"], "status": t["status"],
            "title": t["title"], "starts_at": t["starts_at"], "area": t["area"],
            "age_limit": t["age_limit"],
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


async def h_index(request: web.Request):
    return web.FileResponse(WEBAPP_DIR / "index.html")


def make_web_app(bot) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["bot"] = bot
    app.add_routes([
        web.get("/", h_index),
        web.get("/api/events", h_events),
        web.post("/api/events", h_create_event),
        web.get("/api/events/{id}", h_event),
        web.post("/api/events/{id}/claim_free", h_claim_free),
        web.post("/api/events/{id}/claim_paid", h_claim_paid),
        web.get("/api/events/{id}/guests", h_guests),
        web.get("/api/me/tickets", h_my_tickets),
        web.get("/api/me/events", h_my_events),
        web.post("/api/approve", h_approve),
        web.post("/api/scan", h_scan),
    ])
    return app
