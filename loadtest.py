"""Нагрузочный тест AFTERS: проверяем, выдержит ли сервер наплыв людей.

Поднимает РЕАЛЬНОЕ aiohttp-приложение (api.make_web_app) с фейковым ботом,
обходит Telegram-аутентификацию через заголовок X-Test-Uid и гоняет тысячи
конкурентных запросов: лента, открытие события, гонка за билетами.

Запуск:  python3 loadtest.py
"""
from __future__ import annotations
import os, sys, time, asyncio, tempfile, statistics

os.environ.setdefault("BOT_TOKEN", "test")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["REQUIRED_CHANNEL"] = ""        # отключаем шлагбаум подписки для чистоты замера
os.environ["ADMIN_IDS"] = "1"

import config
import db
import api
from aiohttp.test_utils import TestServer, TestClient


# ── фейковый бот: считаем сетевые вызовы Telegram ──
class FakeBot:
    def __init__(self):
        self.get_chat_member_calls = 0
        self.send_message_calls = 0
    async def get_chat_member(self, chat_id, user_id):
        self.get_chat_member_calls += 1
        await asyncio.sleep(0.03)   # реальная задержка сети Telegram ~30мс
        class M:  # noqa
            status = "member"
        return M()
    async def send_message(self, *a, **k):
        self.send_message_calls += 1
        return None
    async def get_me(self):
        class U:  # noqa
            username = "afters_bot"; id = 999
        return U()


# обходим проверку initData: пользователь берётся из заголовка X-Test-Uid
def _patched_validate(init_data: str):
    return None
def _install_auth_bypass():
    import api as _api
    orig = _api.auth_middleware
    from aiohttp import web
    @web.middleware
    async def bypass(request, handler):
        if request.path.startswith("/api/"):
            uid = int(request.headers.get("X-Test-Uid", "0") or 0)
            if not uid:
                return web.json_response({"error": "no uid"}, status=401)
            if not _api._rate.check(uid):
                return web.json_response({"error": "rate"}, status=429)
            if request.method == "POST" and not _api._rate_write.check(uid):
                return web.json_response({"error": "wrate"}, status=429)
            db.upsert_user(uid, f"u{uid}", f"U{uid}")  # как настоящий auth_middleware
            request["user"] = {"id": uid, "username": f"u{uid}", "first_name": f"U{uid}"}
        return await handler(request)
    return bypass


def pct(vals, p):
    if not vals: return 0
    k = int(len(vals) * p / 100)
    return sorted(vals)[min(k, len(vals)-1)]


async def fire(client, method, path, uid, body=None):
    h = {"X-Test-Uid": str(uid)}
    t0 = time.perf_counter()
    try:
        if method == "GET":
            async with client.get(path, headers=h) as r:
                await r.read(); st = r.status
        else:
            async with client.post(path, headers=h, json=(body or {})) as r:
                await r.read(); st = r.status
    except Exception as e:
        return (time.perf_counter()-t0)*1000, -1
    return (time.perf_counter()-t0)*1000, st


async def main():
    db.init()
    bot = FakeBot()

    # сидируем данные: 1 организатор + 30 событий, часть с лимитом мест
    db.upsert_user(1, "org", "Org")
    eids = []
    now = int(time.time())
    for i in range(30):
        cap = 10 if i == 0 else (50 if i < 5 else 0)
        eid = db.create_event(1, {
            "title": f"Party {i}", "starts_at": now + 86400*(i+1),
            "area": "м. Курская", "address": "addr", "city": "Москва",
            "capacity": cap, "refs_needed": 0, "channel": "",
            "age_limit": "18+", "price_text": "", "pay_url": "",
            "genre": "", "cover": "ember", "description": "d"*200,
        }, "active")
        eids.append(eid)

    # сетап сервера
    app = api.make_web_app(bot)
    app.middlewares.clear()
    app.middlewares.append(api.security_middleware)
    app.middlewares.append(_install_auth_bypass())
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    results = {}

    async def scenario(name, tasks):
        t0 = time.perf_counter()
        out = await asyncio.gather(*tasks)
        dur = time.perf_counter() - t0
        lat = [x[0] for x in out]
        codes = {}
        for _, st in out:
            codes[st] = codes.get(st, 0) + 1
        results[name] = (len(out), dur, lat, codes)
        rps = len(out)/dur if dur else 0
        print(f"\n[{name}]  {len(out)} запросов за {dur:.2f}s = {rps:,.0f} rps")
        print(f"   latency ms: p50={pct(lat,50):.0f}  p95={pct(lat,95):.0f}  p99={pct(lat,99):.0f}  max={max(lat):.0f}")
        print(f"   ответы: {codes}")

    # 1) ЛЕНТА — 2000 одновременных читателей открывают афишу
    await scenario("Лента x2000", [
        fire(client, "GET", "/api/events?city=Москва", 1000+i) for i in range(2000)
    ])

    # 2) ОТКРЫТИЕ СОБЫТИЯ — 2000 заходов в карточку (без канала → без Telegram-вызовов)
    await scenario("Деталь события x2000", [
        fire(client, "GET", f"/api/events/{eids[i % len(eids)]}", 1000+i) for i in range(2000)
    ])

    # 3) ГОНКА ЗА БИЛЕТАМИ — 500 РАЗНЫХ людей ломятся за билетом на событие с лимитом 10
    gid = eids[0]
    await scenario("Гонка за билет (cap=10) x500", [
        fire(client, "POST", f"/api/events/{gid}/claim_free", 5000+i) for i in range(500)
    ])
    taken = db.tickets_count(gid)
    cap = db.get_event(gid)["capacity"]
    oversold = taken > cap
    print(f"   -> выдано билетов: {taken} при лимите {cap}  =>  "
          f"{'❌ ПЕРЕПРОДАЖА!' if oversold else '✅ лимит соблюдён'}")

    # 3b) ГОНКА С КАНАЛОМ — здесь await на проверке подписки реально уступает циклу
    #     событие с лимитом 20 и каналом; 600 разных людей штурмуют одновременно
    chid = db.create_event(1, {
        "title": "Channel Party", "starts_at": now + 999999,
        "area": "a", "address": "x", "city": "Москва", "capacity": 20,
        "refs_needed": 0, "channel": "afters_test", "age_limit": "18+",
        "price_text": "", "pay_url": "", "genre": "", "cover": "ember", "description": "d",
    }, "active")
    await scenario("Гонка с каналом (cap=20) x600", [
        fire(client, "POST", f"/api/events/{chid}/claim_free", 6000+i) for i in range(600)
    ])
    taken2 = db.tickets_count(chid)
    oversold2 = taken2 > 20
    print(f"   -> выдано билетов: {taken2} при лимите 20  =>  "
          f"{'❌ ПЕРЕПРОДАЖА!' if oversold2 else '✅ лимит соблюдён'}")

    # 4) СМЕШАННАЯ НАГРУЗКА — 3000 запросов вперемешку
    mixed = []
    for i in range(3000):
        u = 1000 + i
        if i % 3 == 0:
            mixed.append(fire(client, "GET", "/api/events", u))
        elif i % 3 == 1:
            mixed.append(fire(client, "GET", f"/api/events/{eids[i%len(eids)]}", u))
        else:
            mixed.append(fire(client, "GET", "/api/me/tickets", u))
    await scenario("Смешанная x3000", mixed)

    print(f"\nTelegram get_chat_member вызовов всего: {bot.get_chat_member_calls}")

    await client.close()

    # вердикт
    print("\n" + "="*52)
    bad = False
    for name, (n, dur, lat, codes) in results.items():
        errs = sum(v for k, v in codes.items() if k not in (200,))
        # 429 — это защита, не падение; считаем отдельно
        hard = sum(v for k, v in codes.items() if k in (-1, 500, 502, 503))
        if hard:
            bad = True
            print(f"❌ {name}: {hard} жёстких ошибок")
        if pct(lat,95) > 1500:
            print(f"⚠️  {name}: p95={pct(lat,95):.0f}ms — медленно")
    print("ИТОГ:", "❌ ЕСТЬ ПРОБЛЕМЫ" if bad else "✅ СЕРВЕР ДЕРЖИТ НАГРУЗКУ")


if __name__ == "__main__":
    asyncio.run(main())
