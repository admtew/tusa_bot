"""Клиент qtickets REST API: типы билетов и отслеживание оплат.

Все запросы требуют токен организатора (Authorization: Bearer ...),
который берётся в кабинете qtickets: Настройки → Основное.
"""
from __future__ import annotations

import logging
import re

import requests

log = logging.getLogger("tusa.qtickets")
BASE = "https://qtickets.ru/api/rest/v1"
TIMEOUT = 12


def parse_event_id(url_or_id: str) -> int:
    """Из ссылки вида https://qtickets.ru/event/230015 или 'd12.company/tusa/...' достаём число.
    Принимает и просто число. Возвращает 0, если не нашли."""
    s = (url_or_id or "").strip()
    if s.isdigit():
        return int(s)
    # /event/230015  или  ?event=230015  или  /230015 в конце
    m = re.search(r"event[/=](\d+)", s) or re.search(r"/(\d{3,})(?:\D|$)", s)
    return int(m.group(1)) if m else 0


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def check_token(token: str) -> bool:
    """Лёгкая проверка валидности токена."""
    try:
        r = requests.get(f"{BASE}/events", headers=_headers(token),
                         json={"page": 1}, timeout=TIMEOUT)
        return r.status_code == 200
    except Exception as e:
        log.warning("check_token failed: %s", e)
        return False


def get_ticket_types(token: str, event_id: int) -> list[dict]:
    """Список тарифов события: [{'name': 'VIP', 'price': 1000}, ...].
    Парсит shows[].prices и сопоставляет с зонами схемы (price_id '#N' -> prices[N])."""
    try:
        r = requests.get(f"{BASE}/events/{event_id}", headers=_headers(token), timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        data = (r.json() or {}).get("data") or {}
    except Exception as e:
        log.warning("get_ticket_types failed: %s", e)
        return []

    out: list[dict] = []
    seen = set()
    for show in (data.get("shows") or []):
        prices = show.get("prices") or []
        zones = ((show.get("scheme_properties") or {}).get("zones")) or {}
        # имя зоны -> индекс цены по price_id "#N"
        zone_by_idx: dict[int, str] = {}
        for zname, zinfo in zones.items():
            pid = str((zinfo or {}).get("price_id", ""))
            m = re.match(r"#(\d+)", pid)
            if m:
                zone_by_idx[int(m.group(1))] = zname
        for idx, p in enumerate(prices):
            price = p.get("default_price")
            if price is None:
                continue
            name = zone_by_idx.get(idx, f"Тариф {idx + 1}")
            key = (name, price)
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": name, "price": price})
    return out


def extract_tg_id(order: dict) -> int | None:
    """Достаём telegram id покупателя из заказа: либо из client.details.telegram_user,
    либо из utm_content (если ссылку на оплату пометили tg_id-ом)."""
    try:
        tu = (((order.get("client") or {}).get("details") or {}).get("telegram_user")) or {}
        if tu.get("id"):
            return int(tu["id"])
    except Exception:
        pass
    utm = order.get("utm")
    try:
        if isinstance(utm, dict):
            val = utm.get("utm_content") or utm.get("content")
            if val and str(val).isdigit():
                return int(val)
        elif isinstance(utm, list):
            for item in utm:
                if isinstance(item, dict):
                    v = item.get("utm_content") or item.get("content")
                    if v and str(v).isdigit():
                        return int(v)
    except Exception:
        pass
    return None


def list_my_events(token: str) -> list[dict]:
    """Все активные события организатора в qtickets (для автоимпорта в бота)."""
    try:
        body = {"where": [{"column": "deleted_at", "operator": "null"}],
                "orderBy": {"id": "desc"}, "page": 1}
        r = requests.get(f"{BASE}/events", headers=_headers(token), json=body, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        data = r.json() or {}
        rows = data.get("data") or []
        return rows if isinstance(rows, list) else []
    except Exception as e:
        log.warning("list_my_events failed: %s", e)
        return []


def event_times(ev: dict) -> tuple[int, int]:
    """Из события qtickets достаём (начало, конец) ближайшего показа как unix ts."""
    import datetime
    starts, ends = 0, 0
    shows = ev.get("shows") or []
    best = None
    for sh in shows:
        sd = sh.get("start_date") or sh.get("open_date")
        if not sd:
            continue
        try:
            ts = int(datetime.datetime.fromisoformat(sd).timestamp())
        except Exception:
            continue
        if best is None or ts < best:
            best = ts
            starts = ts
            fd = sh.get("finish_date")
            if fd:
                try:
                    ends = int(datetime.datetime.fromisoformat(fd).timestamp())
                except Exception:
                    ends = 0
    return starts, ends


def list_paid_orders(token: str, event_id: int) -> list[dict]:
    """Оплаченные заказы события."""
    try:
        body = {"where": [{"column": "payed", "value": 1},
                          {"column": "event_id", "value": event_id}],
                "orderBy": {"id": "desc"}, "page": 1}
        r = requests.get(f"{BASE}/orders", headers=_headers(token), json=body, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        data = r.json() or {}
        rows = data.get("data") or data.get("orders") or []
        return rows if isinstance(rows, list) else []
    except Exception as e:
        log.warning("list_paid_orders failed: %s", e)
        return []
