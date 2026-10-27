"""Telegram-пульт для Playerok + полуавтоматическая закупка на Plati.

Что умеет:
  • Покупка на Playerok -> ссылка на оплату товара на Plati -> вы платите ->
    бот ловит письмо Plati и сам отдаёт ключ покупателю.
  • Уведомления: покупка, подтверждение, возврат, жалоба, новый отзыв.
  • Сообщения покупателей приходят в Telegram; ответить можно reply или кнопкой быстрого ответа.
  • /lots — лоты: цена, название, описание, снять / вернуть в продажу.
  • /stats — статистика за день; итог дня приходит сам в 23:59.
"""
import asyncio
import email
import email.policy
import imaplib
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from PlayerokAPI import Account, EventType

log = logging.getLogger("bot")
BASE = Path(__file__).parent
STATE_FILE = BASE / "state.json"
LOTS_FILE = BASE / "lots.json"
REPLIES_FILE = BASE / "replies.json"


def load_env() -> None:
    # панели хостинга часто не дают создать .env, поэтому читаем и env.txt
    env = next((BASE / n for n in (".env", "env.txt", ".env.txt", "env") if (BASE / n).is_file()), None)
    if env is None:
        raise SystemExit(f"Не найден файл настроек: положите env.txt рядом с bot.py ({BASE})")
    print(f"Настройки из {env.name}", flush=True)
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.split(" #", 1)[0].strip())


load_env()
PLAYEROK_TOKEN = os.environ["PLAYEROK_TOKEN"]
TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_OWNER = int(os.environ["TG_OWNER_ID"])
MAIL_USER = os.environ.get("MAIL_USER", "")  # пусто = без почты, ссылку на покупку присылаете боту сами
MAIL_PASSWORD = os.environ.get("MAIL_APP_PASSWORD", "")
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
MAIL_POLL = int(os.environ.get("MAIL_POLL_SECONDS", "15"))
TZ = timezone(timedelta(hours=int(os.environ.get("TZ_OFFSET", "3"))))

DELIVERY_TEXT = (
    "Спасибо за покупку! Ваш товар:\n\n{key}\n\n"
    "Если возникнут проблемы с активацией, напишите мне в этот чат."
)
PAY_URL = "https://oplata.info/asp2/pay.asp?id_d={id}&email={email}&lang=ru-RU"
PURCHASE_URL = "https://shop.digiseller.com/xml/purchase.asp"
BUY_LINK_RE = re.compile(r"id_i=(\d+)(?:&amp;|&)uid=([0-9A-Za-z]+)")
REMIND_AFTER = 20 * 60
STATUS_NAME = {"APPROVED": "в продаже", "DISCONTINUED": "снят", "EXPIRED": "истёк", "SOLD": "продан",
               "DECLINED": "отклонён", "BLOCKED": "заблокирован", "REMOVED": "удалён",
               "PENDING_MODERATION": "на модерации", "PENDING_APPROVAL": "на проверке", "DRAFT": "черновик"}


def price_text(it) -> str:
    """price — цена со скидкой, rawPrice — без скидки."""
    price, raw = float(it.price or 0), float(getattr(it, "raw_price", 0) or 0)
    return f"{price:g} ₽ (без скидки {raw:g} ₽)" if raw and raw != price else f"{price:g} ₽"


STATUS_ICON = {"APPROVED": "🟢", "DISCONTINUED": "⚪", "EXPIRED": "⚪", "SOLD": "✔️",
               "DECLINED": "🔴", "BLOCKED": "🔴", "REMOVED": "🔴"}

state = {
    "seen": [], "pending": {}, "delivered": [], "awaiting": {}, "used_purchases": [],
    "deal_status": {}, "chat_links": {}, "input": None, "reviews_seen": [],
    "stats": {}, "summary_sent": "", "mail_seen": [],
}
if STATE_FILE.exists():
    state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
inflight: set[str] = set()
ME: dict = {}


def save_state() -> None:
    # не даём файлу расти бесконечно
    for key, keep in (("seen", 2000), ("delivered", 2000), ("used_purchases", 2000), ("reviews_seen", 500),
                      ("mail_seen", 2000)):
        state[key] = state[key][-keep:]
    for key, keep in (("pending", 500), ("chat_links", 500), ("deal_status", 2000)):
        if len(state[key]) > keep:
            state[key] = dict(list(state[key].items())[-keep:])
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def sv(x) -> str:
    return str(getattr(x, "value", x) or "")


def today() -> str:
    return datetime.now(TZ).date().isoformat()


def stat(key: str, amount: float = 1) -> None:
    if state["stats"].get("date") != today():
        state["stats"] = {"date": today(), "sales": 0, "revenue": 0.0, "confirmed": 0,
                          "confirmed_sum": 0.0, "refunds": 0, "reviews": 0, "rating_sum": 0}
    state["stats"][key] = state["stats"].get(key, 0) + amount
    save_state()


def deal_price(deal) -> float:
    for v in (getattr(getattr(deal, "transaction", None), "value", None),
              getattr(getattr(deal, "item", None), "price", None)):
        try:
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            pass
    return 0.0


def short(text: str, n: int) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def load_replies() -> list[str]:
    if REPLIES_FILE.exists():
        return json.loads(REPLIES_FILE.read_text(encoding="utf-8"))[:6]
    return []


def find_lot(item_name: str) -> dict | None:
    if not LOTS_FILE.exists():
        return None
    lots = json.loads(LOTS_FILE.read_text(encoding="utf-8"))
    if item_name in lots:  # точная привязка (через кнопку 🔗 Plati) важнее
        return lots[item_name]
    for keyword, cfg in lots.items():
        if keyword.lower() in item_name.lower():
            return cfg
    return None


def order_email(tag: str) -> str:
    local, domain = MAIL_USER.split("@", 1)
    return f"{local}+{tag}@{domain}"


def make_tag(deal_id: str) -> str:
    return "o" + re.sub(r"[^0-9a-z]", "", deal_id.lower())[-10:]


# ================= Telegram =================

HARMLESS_TG_ERRORS = ("message is not modified", "query is too old", "query id is invalid")


async def tg(http: httpx.AsyncClient, method: str, **params):
    for attempt in range(3):
        r = await http.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=params, timeout=70)
        data = r.json()
        if data.get("ok"):
            return data["result"]
        desc = data.get("description", "")
        if any(e in desc for e in HARMLESS_TG_ERRORS):
            return None  # повторное нажатие кнопки / старая кнопка — не ошибка
        retry = (data.get("parameters") or {}).get("retry_after")
        if retry and attempt < 2:  # Telegram просит подождать (слишком много сообщений)
            await asyncio.sleep(min(int(retry), 30))
            continue
        log.error("Telegram %s: %s | %s", method, desc, json.dumps(params, ensure_ascii=False)[:500])
        raise RuntimeError(f"Telegram {method}: {desc}")


async def say(http, text: str, buttons=None, **kw):
    text = (text or "…")[:4000]  # лимит Telegram — 4096 символов
    params = {"chat_id": TG_OWNER, "text": text, "disable_web_page_preview": True, **kw}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    return await tg(http, "sendMessage", **params)


async def tg_manual(http, deal_id: str, chat_id: str, text: str) -> None:
    msg = await say(http, text + "\n\nЧтобы выдать вручную — ответьте (reply) на это сообщение ключом.")
    state["pending"][str(msg["message_id"])] = {"deal": deal_id, "chat": chat_id}
    save_state()


# ================= сделки =================

async def deliver(acc: Account, deal_id: str, chat_id: str, key: str) -> None:
    if deal_id in state["delivered"]:
        return
    await acc.chats.send(chat_id, DELIVERY_TEXT.format(key=key))
    state["delivered"].append(deal_id)
    state["awaiting"] = {t: o for t, o in state["awaiting"].items() if o["deal"] != deal_id}
    save_state()


async def on_paid(acc: Account, http, deal) -> None:
    """Новая покупка: шлём ссылку на оплату товара на Plati."""
    if deal.id in state["seen"]:
        return
    state["seen"].append(deal.id)
    save_state()
    name, price = deal.item.name, deal_price(deal)
    buyer = getattr(getattr(deal, "user", None), "username", "") or "?"
    stat("sales")
    stat("revenue", price)
    head = f"🛒 Покупка: {name}\nЦена: {price:g} ₽ · покупатель {buyer}"

    lot = find_lot(name)
    if not lot:
        await tg_manual(http, deal.id, deal.chat_id, f"{head}\n{deal.url}\n\n⚠️ Лот не указан в lots.json.")
        return
    tag = make_tag(deal.id)
    if MAIL_USER:
        addr = order_email(tag)
        pay = PAY_URL.format(id=int(lot["plati_id"]), email=quote(addr))
        how = f"Почта в форме оплаты: {addr}\nПосле оплаты ключ уйдёт покупателю сам."
    else:
        pay = PAY_URL.format(id=int(lot["plati_id"]), email="")
        how = ("После оплаты скопируйте адрес страницы покупки (oplata.info/...id_i=...&uid=...) "
               "и ответьте им на это сообщение — бот сам заберёт ключ и выдаст.")
    state["awaiting"][tag] = {"deal": deal.id, "chat": deal.chat_id, "name": name,
                              "since": time.time(), "reminded": False}
    save_state()
    await tg_manual(http, deal.id, deal.chat_id, f"{head}\n\n💳 Оплатите на Plati:\n{pay}\n\n{how}")


async def handle_deal(acc: Account, http, deal_id: str) -> None:
    if deal_id in inflight:
        return
    inflight.add(deal_id)
    try:
        deal = await acc.deals.get(deal_id)
        if deal.is_purchase:
            return  # это мы что-то купили — не уведомляем
        status, problem = sv(deal.status), bool(deal.has_problem)
        prev = state["deal_status"].get(deal_id, {})
        if prev.get("s") == status and prev.get("p") == problem:
            return
        state["deal_status"][deal_id] = {"s": status, "p": problem}
        save_state()
        name, price = deal.item.name, deal_price(deal)

        if status == "PAID" and prev.get("s") != "PAID":
            await on_paid(acc, http, deal)
        if status == "CONFIRMED" and prev.get("s") != "CONFIRMED":
            stat("confirmed")
            stat("confirmed_sum", price)
            await say(http, f"✅ Подтверждено: {name} · {price:g} ₽")
        if status == "ROLLED_BACK" and prev.get("s") != "ROLLED_BACK":
            stat("refunds")
            await say(http, f"↩️ Возврат: {name} · {price:g} ₽\n{deal.url}")
        if problem and not prev.get("p"):
            await say(http, f"🚨 Жалоба по сделке: {name}\n{deal.url}")
    except Exception:
        log.exception("Ошибка обработки сделки %s", deal_id)
    finally:
        inflight.discard(deal_id)


# ================= чаты =================

async def on_message(acc: Account, http, message) -> None:
    if message.is_system or message.author_id == ME.get("id") or getattr(message, "is_auto_response", False):
        return
    who = getattr(getattr(message, "user", None), "username", "") or "покупатель"
    text = message.text or ("🖼 картинка" if getattr(message, "image_links", None) else "…")
    buttons = [[{"text": short(r, 30), "callback_data": f"r:{i}:{message.chat_id}"}]
               for i, r in enumerate(load_replies())]
    msg = await say(http, f"💬 {who}:\n{short(text, 3500)}\n\n↩️ Ответьте reply, чтобы написать в чат.",
                    buttons or None)
    state["chat_links"][str(msg["message_id"])] = message.chat_id
    save_state()


# ================= лоты =================

PAGE_SIZE = 5  # Playerok не отдаёт большие страницы — грузим понемногу


async def my_items(acc: Account) -> tuple[list, list[str]]:
    seen, out, errors = set(), [], []
    for status in (None, "DISCONTINUED", "EXPIRED", "SOLD"):
        after = None
        for _ in range(20):  # до 100 лотов на статус
            try:
                page = await acc.items.search(user_id=ME["id"], status=status, first=PAGE_SIZE, after=after)
            except Exception as e:
                log.exception("Не удалось получить лоты (%s)", status)
                if status is None:  # без статуса — основной запрос, его ошибку показываем
                    errors.append(short(str(e), 200))
                break
            for it in page:
                if it.id not in seen:
                    seen.add(it.id)
                    out.append(it)
            if not getattr(page, "has_next_page", False) or not getattr(page, "end_cursor", None):
                break
            after = page.end_cursor
    return out, errors


# Категории лотов в порядке показа: сначала открытые, потом закрытые и т.д.
LOT_GROUPS = [
    ("open", "🟢 В продаже", {"APPROVED"}),
    ("closed", "⚪ Закрытые", {"DISCONTINUED", "EXPIRED"}),
    ("sold", "✔️ Проданные", {"SOLD"}),
    ("moder", "🟡 На модерации", {"PENDING_MODERATION", "PENDING_APPROVAL", "PENDING_STATUS_PAYMENT", "DRAFT"}),
    ("bad", "🔴 Отклонённые", {"DECLINED", "BLOCKED", "REMOVED"}),
]
_items_cache: dict = {"at": 0.0, "items": [], "errors": []}


def lot_group(status: str) -> str:
    return next((key for key, _, statuses in LOT_GROUPS if status in statuses), "moder")


async def cached_items(acc: Account, fresh: bool = False) -> tuple[list, list[str]]:
    if fresh or time.time() - _items_cache["at"] > 30:
        items, errors = await my_items(acc)
        _items_cache.update(at=time.time(), items=items, errors=errors)
    return _items_cache["items"], _items_cache["errors"]


async def edit_or_send(http, edit_msg: int | None, text: str, buttons) -> None:
    if edit_msg:
        await tg(http, "editMessageText", chat_id=TG_OWNER, message_id=edit_msg, text=(text or "…")[:4000],
                 disable_web_page_preview=True, reply_markup={"inline_keyboard": buttons or []})
    else:
        await say(http, text, buttons)


async def show_lots(acc: Account, http, edit_msg: int | None = None) -> None:
    """Экран категорий."""
    items, errors = await cached_items(acc, fresh=edit_msg is None)
    if not items:
        text = "Лотов не найдено." + (f"\nОшибка Playerok: {errors[0]}" if errors else "")
        await edit_or_send(http, edit_msg, text, None)
        return
    counts = {key: 0 for key, _, _ in LOT_GROUPS}
    for it in items:
        counts[lot_group(sv(it.status))] += 1
    buttons = [[{"text": f"{title} ({counts[key]})", "callback_data": f"cat:{key}"}]
               for key, title, _ in LOT_GROUPS if counts[key]]
    buttons.append([{"text": "🔄 Обновить", "callback_data": "lots_fresh"}])
    await edit_or_send(http, edit_msg, f"📦 Лоты ({len(items)}). Выберите категорию:", buttons)


async def show_category(acc: Account, http, key: str, edit_msg: int) -> None:
    items, _ = await cached_items(acc)
    title = next((t for k, t, _ in LOT_GROUPS if k == key), key)
    in_group = [it for it in items if lot_group(sv(it.status)) == key]
    buttons = [[{"text": f"{short(it.name, 30)} — {float(it.price or 0):g} ₽", "callback_data": f"l:{it.id}"}]
               for it in in_group[:40]]
    buttons.append([{"text": "« Категории", "callback_data": "lots"}])
    text = f"{title} ({len(in_group)})" if in_group else f"{title}: пусто"
    await edit_or_send(http, edit_msg, text, buttons)


async def show_lot(acc: Account, http, item_id: str, msg_id: int) -> None:
    it = await acc.items.get(item_id=item_id)
    status = sv(it.status)
    text = (f"{STATUS_ICON.get(status, '🟡')} {it.name}\n"
            f"Цена: {price_text(it)} · {STATUS_NAME.get(status, status)}\n"
            f"Просмотров: {it.views_counter or 0} · продаж: {it.deals_counter or 0}\n\n"
            f"{short(it.description or '', 700)}")
    lot = find_lot(it.name or "")
    text += (f"\n\n🔗 Plati: plati.market/itm/{lot['plati_id']}" if lot
             else "\n\n🔗 Plati: не привязан — при покупке ссылки на оплату не будет")
    toggle = ([{"text": "⛔ Снять с продажи", "callback_data": f"off:{item_id}"}] if status == "APPROVED"
              else [{"text": "✅ Вернуть в продажу", "callback_data": f"on:{item_id}"}])
    toggle.append({"text": "🔗 Plati", "callback_data": f"e:plati:{item_id}"})
    buttons = [
        [{"text": "💰 Цена", "callback_data": f"e:price:{item_id}"},
         {"text": "✏️ Название", "callback_data": f"e:name:{item_id}"},
         {"text": "📝 Описание", "callback_data": f"e:description:{item_id}"}],
        toggle,
        [{"text": "« Назад", "callback_data": f"cat:{lot_group(status)}"}],
    ]
    await edit_or_send(http, msg_id, text, buttons)


def link_plati(item_name: str, plati_id: int) -> None:
    """Записывает в lots.json: точное название лота -> id товара на Plati."""
    lots = json.loads(LOTS_FILE.read_text(encoding="utf-8")) if LOTS_FILE.exists() else {}
    lots = {k: v for k, v in lots.items() if not k.startswith("часть названия")}  # убираем пример
    lots[item_name] = {"plati_id": plati_id}
    LOTS_FILE.write_text(json.dumps(lots, ensure_ascii=False, indent=2), encoding="utf-8")


async def apply_edit(acc: Account, http, field: str, item_id: str, value: str) -> None:
    if field == "plati":
        nums = re.findall(r"\d{4,}", value)  # из ссылки берём последний длинный номер
        if not nums:
            await say(http, "Пришлите ссылку на товар Plati или его номер, например 2142729. /cancel — отмена.")
            return
        state["input"] = None
        save_state()
        it = await acc.items.get(item_id=item_id)
        link_plati(it.name, int(nums[-1]))
        await say(http, f"🔗 «{short(it.name, 60)}» → plati.market/itm/{nums[-1]}\n"
                        "Теперь при покупке этого лота придёт ссылка на оплату.")
        return
    if field == "price":
        try:
            new = float(value.replace(",", ".").replace("₽", "").strip())
        except ValueError:
            await say(http, "Нужна цена числом, например 199. Попробуйте ещё раз или /cancel.")
            return
    else:
        new = value
    state["input"] = None
    save_state()
    try:
        old_name = (await acc.items.get(item_id=item_id)).name if field == "name" else None
        await acc.items.update(item_id, {field: new})
        _items_cache["at"] = 0
        if old_name and LOTS_FILE.exists():  # переносим привязку к Plati на новое название
            lots = json.loads(LOTS_FILE.read_text(encoding="utf-8"))
            if old_name in lots:
                lots[new] = lots.pop(old_name)
                LOTS_FILE.write_text(json.dumps(lots, ensure_ascii=False, indent=2), encoding="utf-8")
        await say(http, "✅ Сохранено. Изменения могут уйти на модерацию Playerok.")
    except Exception as e:
        log.exception("Не удалось изменить лот")
        await say(http, f"❌ Playerok не принял изменение: {short(str(e), 300)}")


# ================= статистика =================

async def balance_text(acc: Account) -> str:
    """Playerok отдаёт в балансе только те поля, что не равны нулю (часто лишь value)."""
    try:
        raw = (await acc.get_balance()).raw or {}
    except Exception:
        log.exception("Не удалось получить баланс")
        return "\n💼 Баланс: не удалось получить"
    parts = [f"💼 Баланс: {float(raw.get('value') or 0):g} ₽"]
    for key, label in (("available", "доступно"), ("withdrawable", "к выводу"),
                       ("frozen", "заморожено"), ("pendingIncome", "ожидается")):
        if raw.get(key) not in (None, 0, "0"):
            parts.append(f"{label} {float(raw[key]):g} ₽")
    return "\n" + ", ".join(parts)


def _parse_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


async def today_sales_from_playerok(acc: Account) -> dict | None:
    """Продажи за сегодня прямо из истории Playerok; None — если Playerok не отдал."""
    deals, after = [], None
    day = datetime.now(TZ).date()
    for _ in range(20):  # до 100 последних сделок
        try:
            page = await acc.deals.search(filter={"userId": ME["id"], "direction": "OUT"},
                                          first=PAGE_SIZE, after=after)
        except Exception:
            log.warning("История сделок недоступна, считаю статистику по событиям", exc_info=True)
            if not deals:
                return None
            break
        batch = list(page)
        deals.extend(batch)
        dates = [_parse_dt(getattr(d, "created_at", None)) for d in batch]
        older = [dt for dt in dates if dt and dt.astimezone(TZ).date() < day]
        if older or not getattr(page, "has_next_page", False) or not getattr(page, "end_cursor", None):
            break
        after = page.end_cursor
    res = {"sales": 0, "revenue": 0.0, "confirmed": 0, "confirmed_sum": 0.0, "refunds": 0}
    for d in deals:
        dt = _parse_dt(getattr(d, "created_at", None))
        if not dt or dt.astimezone(TZ).date() != day:
            continue
        status, price = sv(d.status), deal_price(d)
        if status in ("ROLLED_BACK",):
            res["refunds"] += 1
            continue
        if status in ("PAID", "SENT", "CONFIRMED"):
            res["sales"] += 1
            res["revenue"] += price
        if status == "CONFIRMED":
            res["confirmed"] += 1
            res["confirmed_sum"] += price
    return res


async def stats_text(acc: Account) -> str:
    s = dict(state["stats"]) if state["stats"].get("date") == today() else {}
    real = await today_sales_from_playerok(acc)
    if real is not None:
        s.update(real)
    reviews = s.get("reviews", 0)
    avg = f" (ср. {s.get('rating_sum', 0) / reviews:.1f}★)" if reviews else ""
    balance = await balance_text(acc)
    return (f"📊 Сегодня, {datetime.now(TZ):%d.%m}\n"
            f"🛒 Продаж: {s.get('sales', 0)} на {s.get('revenue', 0):g} ₽\n"
            f"✅ Подтверждено: {s.get('confirmed', 0)} на {s.get('confirmed_sum', 0):g} ₽\n"
            f"↩️ Возвратов: {s.get('refunds', 0)}\n"
            f"⭐ Отзывов: {reviews}{avg}\n"
            f"⏳ Ждут оплаты на Plati: {len(state['awaiting'])}"
            f"{balance}")


def review_rating(t) -> int:
    try:
        return max(0, min(5, round(float(t.rating or 0))))
    except (TypeError, ValueError):
        return 0


async def reviews_about_me(acc: Account) -> list:
    """Отзывы, которые оставили ВАМ (а не вы другим продавцам)."""
    page = await acc.testimonials.search(filter={"userId": ME["id"]}, first=PAGE_SIZE)
    out = []
    for t in page:
        author_id = getattr(getattr(t, "creator", None), "id", None)
        target_id = getattr(getattr(t, "user", None), "id", None)
        if author_id == ME["id"]:
            continue  # это ваш отзыв о ком-то
        if target_id and target_id != ME["id"]:
            continue
        out.append(t)
    return out


async def review_text(acc: Account, t) -> str:
    rating = review_rating(t)
    who = getattr(getattr(t, "creator", None), "username", "") or "покупатель"
    item = ""
    deal_id = getattr(getattr(t, "deal", None), "id", None)
    if deal_id:  # в самом отзыве нет названия товара — берём из сделки
        try:
            item = (await acc.deals.get(deal_id)).item.name or ""
        except Exception:
            pass
    return (f"⭐ Новый отзыв {'★' * rating}{'☆' * (5 - rating)} ({rating}/5)\n"
            f"От: {who}{f'{chr(10)}Товар: {item}' if item else ''}\n\n"
            f"{t.text or '(без текста)'}")


async def background_loop(acc: Account, http) -> None:
    """Новые отзывы (опрос раз в 2 минуты) и итог дня в 23:59."""
    first_run = not state["reviews_seen"]
    while True:
        try:
            for t in await reviews_about_me(acc):
                if t.id in state["reviews_seen"]:
                    continue
                state["reviews_seen"].append(t.id)
                if first_run:
                    continue  # старые отзывы при первом запуске не присылаем
                await say(http, await review_text(acc, t))
                stat("reviews")
                stat("rating_sum", review_rating(t))
            first_run = False
            save_state()
        except Exception:
            log.exception("Ошибка проверки отзывов")
        now = datetime.now(TZ)
        if now.hour == 23 and now.minute >= 59 and state["summary_sent"] != today():
            state["summary_sent"] = today()
            save_state()
            try:
                await say(http, "🌙 Итог дня\n\n" + await stats_text(acc))
            except Exception:
                log.exception("Не удалось отправить итог дня")
        await asyncio.sleep(30 if now.hour == 23 and now.minute >= 55 else 120)


# ================= почта Plati =================

def check_mail_login() -> None:
    with imaplib.IMAP4_SSL(IMAP_HOST) as imap:
        imap.login(MAIL_USER, MAIL_PASSWORD)
    log.info("Почта %s подключена", MAIL_USER)


def _fetch_new_mail() -> list[tuple[str, str]]:
    out = []
    since = (datetime.now() - timedelta(days=2)).strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(IMAP_HOST) as imap:
        imap.login(MAIL_USER, MAIL_PASSWORD)
        imap.select("INBOX")
        # только письма со ссылкой oplata.info; BODY.PEEK не помечает их прочитанными
        _, data = imap.uid("SEARCH", None, "SINCE", since, "TEXT", "oplata.info")
        for uid in data[0].split():
            key = uid.decode()
            if key in state["mail_seen"]:
                continue
            state["mail_seen"].append(key)
            _, raw = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            if not raw or not isinstance(raw[0], tuple):
                continue
            msg = email.message_from_bytes(raw[0][1], policy=email.policy.default)
            rcpt = " ".join(str(msg.get(h, "")) for h in ("To", "Delivered-To", "X-Original-To", "Cc"))
            parts = []
            for part in msg.walk():
                if part.get_content_type() in ("text/plain", "text/html"):
                    try:
                        parts.append(part.get_content())
                    except Exception:
                        pass
            out.append((rcpt.lower(), "\n".join(parts)))
    return out


async def fetch_goods(http, id_i: str, uid: str) -> str:
    for _ in range(10):
        r = await http.get(PURCHASE_URL, params={"id_i": id_i, "uid": uid}, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        retval = (root.findtext("retval") or "").strip()
        if retval == "0":
            text = (root.findtext("text") or "").strip()
            link = (root.findtext("file/link") or root.findtext("file/link2") or "").strip()
            if text or link:
                return text or link
            raise RuntimeError("товар пустой")
        if retval != "66":
            raise RuntimeError(f"purchase.asp вернул {retval} {(root.findtext('retdesc') or '').strip()}")
        await asyncio.sleep(5)
    raise RuntimeError("Plati не подтвердил оплату")


async def mail_loop(acc: Account, http) -> None:
    while True:
        try:
            if state["awaiting"] and MAIL_USER:
                for rcpt, body in await asyncio.to_thread(_fetch_new_mail):
                    tag = next((t for t in state["awaiting"]
                                if f"+{t}@" in rcpt or f"+{t}@" in body.lower()), None)
                    if not tag:
                        continue
                    order = state["awaiting"][tag]
                    m = BUY_LINK_RE.search(body)
                    if not m or m.group(1) in state["used_purchases"]:
                        continue
                    id_i, uid = m.group(1), m.group(2)
                    state["used_purchases"].append(id_i)
                    save_state()
                    try:
                        key = await fetch_goods(http, id_i, uid)
                        await deliver(acc, order["deal"], order["chat"], key)
                        await say(http, f"📦 Ключ выдан: {order['name']}")
                    except Exception as e:
                        log.exception("Не удалось забрать товар")
                        link = f"https://www.oplata.info/info/buy.asp?id_i={id_i}&uid={uid}"
                        await tg_manual(http, order["deal"], order["chat"],
                                        f"⚠️ Оплата прошла, но товар не забрался автоматически ({e}).\n"
                                        f"Товар: {link}\nЗаказ: {order['name']}")
                        state["awaiting"].pop(tag, None)
                        save_state()
            now = time.time()
            for order in state["awaiting"].values():
                if not order["reminded"] and now - order["since"] > REMIND_AFTER:
                    order["reminded"] = True
                    save_state()
                    await say(http, f"⏰ «{order['name']}» ждёт оплаты на Plati уже 20 минут.")
        except Exception:
            log.exception("Ошибка проверки почты")
        await asyncio.sleep(MAIL_POLL)


# ================= входящие из Telegram =================

async def send_debug(acc: Account, http) -> None:
    """Сырые ответы Playerok — чтобы понять, почему баланс и лоты пустые."""
    async def probe(title: str, coro):
        try:
            res = await coro
            if isinstance(res, list) or hasattr(res, "total_count"):
                items = list(res)
                body = {"count": len(items), "total": getattr(res, "total_count", None),
                        "first": getattr(items[0], "raw", str(items[0])) if items else None}
            else:
                body = getattr(res, "raw", res)
            text = json.dumps(body, ensure_ascii=False, default=str, indent=1)
        except Exception as e:
            text = f"ОШИБКА: {type(e).__name__}: {e}"
        await say(http, f"🔧 {title}\n{text[:3500]}")

    await probe("balance", acc.get_balance())
    me = await acc.get_me()
    await say(http, "🔧 me.balance\n" + json.dumps(getattr(me.balance, "raw", None), ensure_ascii=False,
                                                   default=str)[:3000])
    await probe("items(userId)", acc.items.search(user_id=ME["id"], first=5))
    await probe("items(userId, APPROVED)", acc.items.search(user_id=ME["id"], status="APPROVED", first=5))
    await probe("deals userId+OUT", acc.deals.search(filter={"userId": ME["id"], "direction": "OUT"}, first=2))
    await probe("testimonials", acc.testimonials.search(filter={"userId": ME["id"]}, first=2))
    items, errors = await my_items(acc)
    await say(http, f"🔧 my_items: {len(items)} лотов, ошибки: {errors or 'нет'}")
    try:
        reviews = await reviews_about_me(acc)
        await say(http, "🔧 последний отзыв о вас:\n" + (await review_text(acc, reviews[0]) if reviews else "нет"))
    except Exception as e:
        await say(http, f"🔧 отзывы: ОШИБКА {short(str(e), 300)}")
    await say(http, "🔧 статистика\n" + await stats_text(acc))


HELP = ("Команды:\n/lots — лоты и их редактирование\n/stats — статистика за день\n"
        "/cancel — отменить ввод\n\nНа сообщение покупателя отвечайте reply — уйдёт в чат Playerok.\n"
        "Чтобы обновить бота, пришлите сюда файл bot.py (или lots.json, replies.json, env.txt).")


async def on_callback(acc: Account, http, cq: dict) -> None:
    if cq.get("from", {}).get("id") != TG_OWNER:
        return
    data, msg_id = cq.get("data", ""), cq["message"]["message_id"]
    note = ""
    try:
        if data == "lots":
            await show_lots(acc, http, edit_msg=msg_id)
        elif data == "lots_fresh":
            await cached_items(acc, fresh=True)
            await show_lots(acc, http, edit_msg=msg_id)
            note = "Обновлено"
        elif data.startswith("cat:"):
            await show_category(acc, http, data[4:], msg_id)
        elif data.startswith("l:"):
            await show_lot(acc, http, data[2:], msg_id)
        elif data.startswith("off:"):
            _items_cache["at"] = 0
            await acc.items.discontinue(data[4:])
            note = "Снят с продажи"
            await show_lot(acc, http, data[4:], msg_id)
        elif data.startswith("on:"):
            _items_cache["at"] = 0
            await acc.items.republish(data[3:])
            note = "Возвращён в продажу"
            await show_lot(acc, http, data[3:], msg_id)
        elif data.startswith("e:"):
            _, field, item_id = data.split(":", 2)
            state["input"] = {"field": field, "item": item_id}
            save_state()
            label = {"price": "новую цену (числом)", "name": "новое название",
                     "description": "новое описание",
                     "plati": "ссылку на товар Plati (или его номер)"}[field]
            await say(http, f"Пришлите {label}. /cancel — отмена.")
        elif data.startswith("r:"):
            _, idx, chat_id = data.split(":", 2)
            reply = load_replies()[int(idx)]
            await acc.chats.send(chat_id, reply)
            note = "Отправлено"
    except Exception as e:
        log.exception("Ошибка кнопки %s", data)
        note = f"Ошибка: {short(str(e), 150)}"
    try:
        await tg(http, "answerCallbackQuery", callback_query_id=cq["id"], text=note[:190])
    except Exception:
        pass  # ответ на нажатие не важен


UPDATABLE = {"bot.py", "lots.json", "replies.json", "env.txt", "requirements.txt"}
RESTART_NEEDED = {"bot.py", "env.txt"}


async def on_document(http, doc: dict, update_id: int) -> None:
    """Файл, присланный боту, заменяет одноимённый файл на сервере."""
    name = doc.get("file_name") or ""
    if name not in UPDATABLE:
        await say(http, f"Не знаю, куда положить «{name}». Можно: {', '.join(sorted(UPDATABLE))}.")
        return
    info = await tg(http, "getFile", file_id=doc["file_id"])
    r = await http.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}", timeout=60)
    r.raise_for_status()
    data = r.content
    try:  # проверяем, что файл не битый, прежде чем заменить
        if name.endswith(".py"):
            compile(data.decode("utf-8-sig"), name, "exec")
        elif name.endswith(".json"):
            json.loads(data.decode("utf-8-sig"))
    except Exception as e:
        await say(http, f"❌ {name} не заменён — в нём ошибка:\n{short(str(e), 500)}")
        return
    target = BASE / name
    if target.exists() and target.read_bytes() == data:
        await say(http, f"ℹ️ {name} уже такой же, ничего не меняю.")
        return
    if target.exists():
        target.with_name(name + ".bak").write_bytes(target.read_bytes())
    target.write_bytes(data)
    if name in RESTART_NEEDED:
        await say(http, f"✅ {name} обновлён, перезапускаюсь… (старая версия — {name}.bak)")
        save_state()
        # подтверждаем Telegram это сообщение, иначе после рестарта файл придёт снова
        await tg(http, "getUpdates", offset=update_id + 1, timeout=0)
        os.execv(sys.executable, [sys.executable, str(BASE / "bot.py")])
    await say(http, f"✅ {name} обновлён, перезапуск не нужен.")


async def on_owner_text(acc: Account, http, msg: dict, update_id: int = 0) -> None:
    if msg.get("document"):
        await on_document(http, msg["document"], update_id)
        return
    text = (msg.get("text") or "").strip()
    reply = msg.get("reply_to_message")
    reply_id = str(reply["message_id"]) if reply else None

    if text in ("/start", "/help"):
        await say(http, HELP)
    elif text == "/lots":
        await show_lots(acc, http)
    elif text == "/stats":
        await say(http, await stats_text(acc))
    elif text == "/debug":
        await send_debug(acc, http)
    elif text == "/cancel":
        state["input"] = None
        save_state()
        await say(http, "Отменено.")
    elif reply_id and reply_id in state["pending"] and text:
        order = state["pending"][reply_id]
        m = BUY_LINK_RE.search(text)
        if m and "oplata" in text.lower():  # прислали ссылку на покупку Plati -> забираем ключ сами
            try:
                key = await fetch_goods(http, m.group(1), m.group(2))
            except Exception as e:
                await say(http, f"❌ Не удалось забрать ключ по ссылке ({short(str(e), 200)}). "
                                "Откройте ссылку и пришлите сам ключ ответом на сообщение о заказе.")
                return
            state["used_purchases"].append(m.group(1))
            await deliver(acc, order["deal"], order["chat"], key)
            await say(http, f"📦 Ключ забран и выдан:\n{short(key, 300)}")
        else:
            await deliver(acc, order["deal"], order["chat"], text)
            await say(http, "✅ Выдано вручную.")
    elif reply_id and reply_id in state["chat_links"] and text:
        await acc.chats.send(state["chat_links"][reply_id], text)
        await say(http, "✉️ Отправлено в чат.")
    elif state["input"] and text:
        await apply_edit(acc, http, state["input"]["field"], state["input"]["item"], text)
    else:
        await say(http, HELP)


async def telegram_loop(acc: Account, http) -> None:
    await tg(http, "setMyCommands", commands=[
        {"command": "lots", "description": "Лоты"},
        {"command": "stats", "description": "Статистика за день"},
        {"command": "cancel", "description": "Отменить ввод"},
    ])
    offset = 0
    while True:
        try:
            updates = await tg(http, "getUpdates", offset=offset, timeout=50)
        except Exception:
            log.exception("Telegram недоступен, повтор через 5 с")
            await asyncio.sleep(5)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                if "callback_query" in upd:
                    await on_callback(acc, http, upd["callback_query"])
                elif upd.get("message", {}).get("from", {}).get("id") == TG_OWNER:
                    await on_owner_text(acc, http, upd["message"], upd["update_id"])
            except Exception as e:
                log.exception("Ошибка обработки команды")
                try:
                    await say(http, f"❌ Ошибка: {short(str(e), 300)}")
                except Exception:
                    pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore"):  # в их логах виден токен бота
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if MAIL_USER:
        await asyncio.to_thread(check_mail_login)
    async with Account(token=PLAYEROK_TOKEN) as acc, httpx.AsyncClient() as http:
        me = await acc.get_me()
        ME["id"] = me.id
        log.info("Playerok: %s", me.username)
        listener = acc.listener(events=[EventType.NEW_DEAL, EventType.DEAL_UPDATED, EventType.NEW_MESSAGE])

        @listener.on(EventType.NEW_DEAL, EventType.DEAL_UPDATED)
        async def _deal(event) -> None:
            await handle_deal(acc, http, event.deal.id)

        @listener.on(EventType.NEW_MESSAGE)
        async def _msg(event) -> None:
            await on_message(acc, http, event.message)

        try:
            await say(http, f"🤖 Бот запущен ({me.username}).\n\n{HELP}")
        except Exception as e:
            log.error("Не могу написать вам в Telegram (%s). Откройте бота и нажмите Start, "
                      "проверьте TG_OWNER_ID в env.txt. Бот продолжает работать.", e)
        await asyncio.gather(listener.run(), telegram_loop(acc, http),
                             mail_loop(acc, http), background_loop(acc, http))


if __name__ == "__main__":
    asyncio.run(main())
