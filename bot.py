# -*- coding: utf-8 -*-
"""
Telegram-бот для платного доступа в закрытый канал.

Один файл: bot.py + .env
Библиотека: aiogram 3.x

Возможности:
- Тарифы: тест 1 минута ($0.5), месяц ($5), 3 месяца ($15)
- Оплата через CryptoBot (Crypto Pay API) и прямой перевод (с подтверждением админом)
- Промокоды: админ генерирует через админ-панель промокод на любую сумму ($),
  каждый промокод одноразовый. Пользователь активирует промокод -> сумма падает на баланс,
  балансом можно оплатить любой тариф.
- После активации тарифа бот выдаёт одноразовую пригласительную ссылку в канал.
- За 2 дня до окончания подписки бот предупреждает о необходимости продления.
- После окончания срока бот удаляет (кикает) пользователя из канала.

Запуск:
    pip install aiogram aiosqlite aiohttp python-dotenv aiohttp-socks
    python bot.py

Прокси (Xray/V2Ray): запустите VPN-клиент с локальным SOCKS5-портом
и укажите его в PROXY_URL в .env (например socks5://127.0.0.1:10808).
"""

import asyncio
import logging
import os
import secrets
import string
import time

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

try:
    # Нужен для SOCKS5/HTTP прокси (Xray / V2Ray / Shadowsocks и т.п.)
    from aiohttp_socks import ProxyConnector
except ImportError:  # библиотека опциональна, если прокси не используется
    ProxyConnector = None  # type: ignore[assignment]

# --------------------------------------------------------------------------- #
#                                КОНФИГ                                        #
# --------------------------------------------------------------------------- #

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# ID администраторов через запятую, например: 123456789,987654321
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
# ID закрытого канала (бот должен быть админом канала). Пример: -1001234567890
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
# Токен из @CryptoBot -> Crypto Pay -> Create App
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
# Основной API: https://pay.crypt.bot/api , тестовый: https://testnet-pay.crypt.bot/api
CRYPTO_PAY_API = os.getenv("CRYPTO_PAY_API", "https://pay.crypt.bot/api")
# Реквизиты для прямого перевода (текст, который увидит пользователь)
DIRECT_PAYMENT_INFO = os.getenv(
    "DIRECT_PAYMENT_INFO",
    "Переведите сумму на карту 0000 0000 0000 0000 (Иван И.)\n"
    "После перевода нажмите кнопку \"Я оплатил\" и пришлите чек.",
)
DB_PATH = os.getenv("DB_PATH", "bot.db")

# Прокси для доступа к Telegram, если он заблокирован (Xray/V2Ray локальный SOCKS5).
# Пример: socks5://127.0.0.1:10808  |  socks5://user:pass@host:port  |  http://127.0.0.1:8080
# Пустое значение = работать напрямую без прокси.
PROXY_URL = os.getenv("PROXY_URL", "").strip()

# Сколько секунд действует пригласительная ссылка (на вступление)
INVITE_TTL = 3600

# Тарифы. seconds — длительность подписки в секундах.
PLANS = {
    "test": {"title": "Тест — 1 минута", "price": 0.5, "seconds": 60},
    "month": {"title": "1 месяц", "price": 5.0, "seconds": 30 * 24 * 3600},
    "quarter": {"title": "3 месяца", "price": 15.0, "seconds": 90 * 24 * 3600},
}

WARN_BEFORE = 2 * 24 * 3600  # за сколько секунд предупреждать (2 дня)
CHECK_INTERVAL = 20  # как часто проверять подписки/оплаты (сек)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("subbot")

def _build_session() -> AiohttpSession | None:
    """Создаёт HTTP-сессию aiogram через прокси, если задан PROXY_URL."""
    if PROXY_URL:
        log.info("Using proxy for Telegram API: %s", PROXY_URL)
        return AiohttpSession(proxy=PROXY_URL)
    return None


bot = Bot(
    token=BOT_TOKEN,
    session=_build_session(),
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)


# --------------------------------------------------------------------------- #
#                                  FSM                                         #
# --------------------------------------------------------------------------- #


class AdminStates(StatesGroup):
    promo_amount = State()


class UserStates(StatesGroup):
    promo_code = State()


# --------------------------------------------------------------------------- #
#                                  БАЗА                                        #
# --------------------------------------------------------------------------- #


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                username  TEXT,
                balance   REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id     INTEGER PRIMARY KEY,
                plan        TEXT,
                expires_at  INTEGER NOT NULL,
                warned      INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS promocodes (
                code        TEXT PRIMARY KEY,
                amount      REAL NOT NULL,
                used        INTEGER NOT NULL DEFAULT 0,
                used_by     INTEGER,
                created_at  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id  TEXT,
                user_id     INTEGER NOT NULL,
                plan        TEXT NOT NULL,
                method      TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  INTEGER NOT NULL
            );
            """
        )
        await db.commit()


async def ensure_user(user_id: int, username: str | None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, username) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (user_id, username or ""),
        )
        await db.commit()


async def get_balance(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT balance FROM users WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return float(row[0]) if row else 0.0


async def add_balance(user_id: int, amount: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id=?",
            (amount, user_id),
        )
        await db.commit()


async def get_subscription(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, plan, expires_at, warned FROM subscriptions WHERE user_id=?",
            (user_id,),
        ) as cur:
            return await cur.fetchone()


# --------------------------------------------------------------------------- #
#                              КЛАВИАТУРЫ                                       #
# --------------------------------------------------------------------------- #


def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оформить подписку", callback_data="buy")
    kb.button(text="🎟 Ввести промокод", callback_data="promo")
    kb.button(text="👤 Моя подписка", callback_data="status")
    kb.adjust(1)
    return kb.as_markup()


def plans_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, plan in PLANS.items():
        kb.button(
            text=f"{plan['title']} — ${plan['price']:g}",
            callback_data=f"plan:{key}",
        )
    kb.button(text="⬅️ Назад", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def pay_menu(plan_key: str, balance: float, price: float) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🪙 CryptoBot", callback_data=f"crypto:{plan_key}")
    kb.button(text="💸 Прямой перевод", callback_data=f"direct:{plan_key}")
    if balance >= price:
        kb.button(text=f"💰 С баланса (${balance:g})", callback_data=f"balance:{plan_key}")
    kb.button(text="⬅️ Назад", callback_data="buy")
    kb.adjust(1)
    return kb.as_markup()


def admin_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать промокод", callback_data="adm_promo")
    kb.button(text="📊 Статистика", callback_data="adm_stats")
    kb.adjust(1)
    return kb.as_markup()


# --------------------------------------------------------------------------- #
#                          ВЫДАЧА ПОДПИСКИ / КАНАЛ                              #
# --------------------------------------------------------------------------- #


async def grant_subscription(user_id: int, plan_key: str) -> str:
    """Продлевает/создаёт подписку и возвращает пригласительную ссылку."""
    plan = PLANS[plan_key]
    now = int(time.time())

    sub = await get_subscription(user_id)
    base = max(now, sub[2]) if sub else now
    expires_at = base + plan["seconds"]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO subscriptions (user_id, plan, expires_at, warned) "
            "VALUES (?, ?, ?, 0) "
            "ON CONFLICT(user_id) DO UPDATE SET plan=excluded.plan, "
            "expires_at=excluded.expires_at, warned=0",
            (user_id, plan_key, expires_at),
        )
        await db.commit()

    # Снимаем возможный бан, чтобы пользователь смог вступить заново
    try:
        await bot.unban_chat_member(CHANNEL_ID, user_id, only_if_banned=True)
    except Exception as e:  # noqa: BLE001
        log.warning("unban before invite failed: %s", e)

    invite = await bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        member_limit=1,
        expire_date=now + INVITE_TTL,
        name=f"sub_{user_id}_{now}",
    )
    return invite.invite_link


async def kick_user(user_id: int) -> None:
    try:
        await bot.ban_chat_member(CHANNEL_ID, user_id)
        await bot.unban_chat_member(CHANNEL_ID, user_id, only_if_banned=True)
    except Exception as e:  # noqa: BLE001
        log.warning("kick failed for %s: %s", user_id, e)


async def deliver_access(user_id: int, plan_key: str) -> None:
    """Выдаёт доступ после подтверждённой оплаты и уведомляет пользователя."""
    try:
        link = await grant_subscription(user_id, plan_key)
        sub = await get_subscription(user_id)
        until = time.strftime("%d.%m.%Y %H:%M", time.localtime(sub[2]))
        await bot.send_message(
            user_id,
            f"✅ Оплата получена! Тариф: <b>{PLANS[plan_key]['title']}</b>\n"
            f"Подписка активна до <b>{until}</b>.\n\n"
            f"Ваша персональная ссылка для входа в канал:\n{link}",
        )
    except Exception as e:  # noqa: BLE001
        log.error("deliver_access failed: %s", e)
        await bot.send_message(
            user_id,
            "Оплата получена, но не удалось выдать ссылку автоматически. "
            "Свяжитесь с администратором.",
        )


# --------------------------------------------------------------------------- #
#                              CRYPTO PAY API                                   #
# --------------------------------------------------------------------------- #


async def crypto_request(method: str, payload: dict) -> dict:
    url = f"{CRYPTO_PAY_API}/{method}"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    connector = None
    if PROXY_URL:
        if ProxyConnector is None:
            raise RuntimeError(
                "Для работы через прокси установите: pip install aiohttp-socks"
            )
        connector = ProxyConnector.from_url(PROXY_URL)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            return await resp.json()


async def create_crypto_invoice(amount: float, description: str, payload: str) -> dict:
    data = {
        "currency_type": "fiat",
        "fiat": "USD",
        "amount": f"{amount:.2f}",
        "description": description,
        "payload": payload,
        "expires_in": 3600,
    }
    res = await crypto_request("createInvoice", data)
    if not res.get("ok"):
        raise RuntimeError(f"CryptoBot error: {res}")
    return res["result"]


async def get_crypto_invoice(invoice_id: str) -> dict | None:
    res = await crypto_request("getInvoices", {"invoice_ids": str(invoice_id)})
    if not res.get("ok"):
        return None
    items = res["result"].get("items", [])
    return items[0] if items else None


# --------------------------------------------------------------------------- #
#                            ПОЛЬЗОВАТЕЛЬ                                       #
# --------------------------------------------------------------------------- #


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await ensure_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "👋 Приветствую! Это официальный бот <b>Арсена Маркаряна</b>, "
        "который поможет <b>узнать больше о закрытом канале База и вступить в него.</b>\n\n"
        "💳 <b>Подписка — ежемесячная 1500₽ или ~15$</b>, "
        "оплату принимаем в любой валюте и крипте.\n\n"
        "Нажимай кнопку ниже ⬇️",
        reply_markup=main_menu(),
    )


@router.callback_query(F.data == "home")
async def cb_home(call: CallbackQuery) -> None:
    await call.message.edit_text("Главное меню:", reply_markup=main_menu())
    await call.answer()


@router.callback_query(F.data == "buy")
async def cb_buy(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "Выберите тариф подписки:", reply_markup=plans_menu()
    )
    await call.answer()


@router.callback_query(F.data == "status")
async def cb_status(call: CallbackQuery) -> None:
    sub = await get_subscription(call.from_user.id)
    balance = await get_balance(call.from_user.id)
    if sub and sub[2] > int(time.time()):
        until = time.strftime("%d.%m.%Y %H:%M", time.localtime(sub[2]))
        text = (
            f"📋 Тариф: <b>{PLANS.get(sub[1], {}).get('title', sub[1])}</b>\n"
            f"Активна до: <b>{until}</b>\n"
            f"Баланс: <b>${balance:g}</b>"
        )
    else:
        text = f"У вас нет активной подписки.\nБаланс: <b>${balance:g}</b>"
    await call.message.edit_text(text, reply_markup=main_menu())
    await call.answer()


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: CallbackQuery) -> None:
    plan_key = call.data.split(":", 1)[1]
    plan = PLANS.get(plan_key)
    if not plan:
        await call.answer("Тариф не найден", show_alert=True)
        return
    balance = await get_balance(call.from_user.id)
    await call.message.edit_text(
        f"Тариф: <b>{plan['title']}</b>\nСтоимость: <b>${plan['price']:g}</b>\n\n"
        "Выберите способ оплаты:",
        reply_markup=pay_menu(plan_key, balance, plan["price"]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("crypto:"))
async def cb_crypto(call: CallbackQuery) -> None:
    plan_key = call.data.split(":", 1)[1]
    plan = PLANS[plan_key]
    try:
        invoice = await create_crypto_invoice(
            amount=plan["price"],
            description=f"Подписка: {plan['title']}",
            payload=f"{call.from_user.id}:{plan_key}",
        )
    except Exception as e:  # noqa: BLE001
        log.error("create invoice failed: %s", e)
        await call.answer("Не удалось создать счёт. Попробуйте позже.", show_alert=True)
        return

    invoice_id = str(invoice["invoice_id"])
    pay_url = invoice.get("bot_invoice_url") or invoice.get("pay_url")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO payments (invoice_id, user_id, plan, method, status, created_at) "
            "VALUES (?, ?, ?, 'crypto', 'pending', ?)",
            (invoice_id, call.from_user.id, plan_key, int(time.time())),
        )
        await db.commit()

    kb = InlineKeyboardBuilder()
    kb.button(text="🪙 Оплатить", url=pay_url)
    kb.button(text="🔄 Проверить оплату", callback_data=f"check:{invoice_id}")
    kb.button(text="⬅️ Назад", callback_data="buy")
    kb.adjust(1)
    await call.message.edit_text(
        f"Счёт на <b>${plan['price']:g}</b> создан.\n"
        "Оплатите его в CryptoBot и нажмите «Проверить оплату».",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("check:"))
async def cb_check(call: CallbackQuery) -> None:
    invoice_id = call.data.split(":", 1)[1]
    invoice = await get_crypto_invoice(invoice_id)
    if not invoice:
        await call.answer("Счёт не найден.", show_alert=True)
        return
    if invoice.get("status") != "paid":
        await call.answer("Оплата ещё не поступила.", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, plan, status FROM payments WHERE invoice_id=?",
            (invoice_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await call.answer("Платёж не найден.", show_alert=True)
            return
        if row[2] == "paid":
            await call.answer("Этот счёт уже активирован.", show_alert=True)
            return
        await db.execute(
            "UPDATE payments SET status='paid' WHERE invoice_id=?", (invoice_id,)
        )
        await db.commit()

    await deliver_access(row[0], row[1])
    await call.message.edit_text("✅ Оплата подтверждена!", reply_markup=main_menu())
    await call.answer()


@router.callback_query(F.data.startswith("balance:"))
async def cb_balance(call: CallbackQuery) -> None:
    plan_key = call.data.split(":", 1)[1]
    plan = PLANS[plan_key]
    balance = await get_balance(call.from_user.id)
    if balance < plan["price"]:
        await call.answer("Недостаточно средств на балансе.", show_alert=True)
        return
    await add_balance(call.from_user.id, -plan["price"])
    await deliver_access(call.from_user.id, plan_key)
    await call.message.edit_text(
        "✅ Оплачено с баланса!", reply_markup=main_menu()
    )
    await call.answer()


@router.callback_query(F.data.startswith("direct:"))
async def cb_direct(call: CallbackQuery) -> None:
    plan_key = call.data.split(":", 1)[1]
    plan = PLANS[plan_key]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO payments (invoice_id, user_id, plan, method, status, created_at) "
            "VALUES (NULL, ?, ?, 'direct', 'pending', ?)",
            (call.from_user.id, plan_key, int(time.time())),
        )
        payment_id = cur.lastrowid
        await db.commit()

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Я оплатил", callback_data=f"paid:{payment_id}")
    kb.button(text="⬅️ Назад", callback_data="buy")
    kb.adjust(1)
    await call.message.edit_text(
        f"Тариф: <b>{plan['title']}</b> — <b>${plan['price']:g}</b>\n\n"
        f"{DIRECT_PAYMENT_INFO}",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("paid:"))
async def cb_paid(call: CallbackQuery) -> None:
    payment_id = int(call.data.split(":", 1)[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, plan, status FROM payments WHERE id=?", (payment_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row or row[2] != "pending":
        await call.answer("Заявка не найдена или уже обработана.", show_alert=True)
        return

    plan = PLANS[row[1]]
    uname = call.from_user.username or call.from_user.full_name
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"approve:{payment_id}")
    kb.button(text="❌ Отклонить", callback_data=f"reject:{payment_id}")
    kb.adjust(2)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"💸 Заявка на прямой перевод #{payment_id}\n"
                f"Пользователь: @{uname} (<code>{row[0]}</code>)\n"
                f"Тариф: {plan['title']} — ${plan['price']:g}",
                reply_markup=kb.as_markup(),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("notify admin failed: %s", e)

    await call.message.edit_text(
        "Заявка отправлена администратору. Ожидайте подтверждения. "
        "Вы можете прислать сюда чек об оплате.",
        reply_markup=main_menu(),
    )
    await call.answer()


@router.callback_query(F.data == "promo")
async def cb_promo(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserStates.promo_code)
    await call.message.edit_text("Введите промокод одним сообщением:")
    await call.answer()


@router.message(UserStates.promo_code)
async def msg_promo(message: Message, state: FSMContext) -> None:
    await state.clear()
    code = (message.text or "").strip().upper()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT code, amount, used FROM promocodes WHERE code=?", (code,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await message.answer("❌ Промокод не найден.", reply_markup=main_menu())
            return
        if row[2]:
            await message.answer("❌ Промокод уже использован.", reply_markup=main_menu())
            return
        await db.execute(
            "UPDATE promocodes SET used=1, used_by=? WHERE code=?",
            (message.from_user.id, code),
        )
        await db.commit()

    await ensure_user(message.from_user.id, message.from_user.username)
    await add_balance(message.from_user.id, float(row[1]))
    balance = await get_balance(message.from_user.id)
    await message.answer(
        f"✅ Промокод активирован! Начислено <b>${row[1]:g}</b>.\n"
        f"Баланс: <b>${balance:g}</b>\n\n"
        "Теперь можно оформить подписку и оплатить её с баланса.",
        reply_markup=main_menu(),
    )


# --------------------------------------------------------------------------- #
#                                АДМИН                                         #
# --------------------------------------------------------------------------- #


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer("🔧 Админ-панель:", reply_markup=admin_menu())


@router.callback_query(F.data == "adm_promo")
async def cb_adm_promo(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await state.set_state(AdminStates.promo_amount)
    await call.message.edit_text(
        "Введите сумму промокода в долларах (например: 5 или 12.5):"
    )
    await call.answer()


@router.message(AdminStates.promo_amount)
async def msg_adm_promo(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").replace(",", ".").strip()
    try:
        amount = round(float(raw), 2)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Некорректная сумма. Введите число, например 5")
        return
    await state.clear()

    code = "PROMO-" + "".join(
        secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8)
    )
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO promocodes (code, amount, used, created_at) VALUES (?, ?, 0, ?)",
            (code, amount, int(time.time())),
        )
        await db.commit()

    await message.answer(
        f"✅ Промокод на <b>${amount:g}</b> создан (одноразовый):\n"
        f"<code>{code}</code>",
        reply_markup=admin_menu(),
    )


@router.callback_query(F.data == "adm_stats")
async def cb_adm_stats(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            users = (await c.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE expires_at > ?", (now,)
        ) as c:
            active = (await c.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM promocodes WHERE used=0"
        ) as c:
            promos = (await c.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM payments WHERE status='paid'"
        ) as c:
            paid = (await c.fetchone())[0]
    await call.message.edit_text(
        f"📊 Статистика\n\n"
        f"Пользователей: <b>{users}</b>\n"
        f"Активных подписок: <b>{active}</b>\n"
        f"Неиспользованных промокодов: <b>{promos}</b>\n"
        f"Успешных оплат: <b>{paid}</b>",
        reply_markup=admin_menu(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    payment_id = int(call.data.split(":", 1)[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, plan, status FROM payments WHERE id=?", (payment_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row or row[2] != "pending":
            await call.answer("Уже обработано.", show_alert=True)
            return
        await db.execute(
            "UPDATE payments SET status='paid' WHERE id=?", (payment_id,)
        )
        await db.commit()
    await deliver_access(row[0], row[1])
    await call.message.edit_text(f"✅ Заявка #{payment_id} подтверждена.")
    await call.answer()


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    payment_id = int(call.data.split(":", 1)[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, status FROM payments WHERE id=?", (payment_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row or row[1] != "pending":
            await call.answer("Уже обработано.", show_alert=True)
            return
        await db.execute(
            "UPDATE payments SET status='rejected' WHERE id=?", (payment_id,)
        )
        await db.commit()
    try:
        await bot.send_message(row[0], "❌ Ваша оплата отклонена администратором.")
    except Exception:  # noqa: BLE001
        pass
    await call.message.edit_text(f"❌ Заявка #{payment_id} отклонена.")
    await call.answer()


# --------------------------------------------------------------------------- #
#                          ФОНОВЫЕ ЗАДАЧИ                                       #
# --------------------------------------------------------------------------- #


async def subscription_watcher() -> None:
    """Предупреждает за 2 дня и кикает по окончании срока."""
    while True:
        try:
            now = int(time.time())
            async with aiosqlite.connect(DB_PATH) as db:
                # Предупреждения за 2 дня (только для тарифов длиннее 2 дней)
                async with db.execute(
                    "SELECT user_id, plan, expires_at FROM subscriptions "
                    "WHERE warned=0 AND expires_at > ? AND expires_at - ? <= ?",
                    (now, now, WARN_BEFORE),
                ) as cur:
                    to_warn = await cur.fetchall()
                for user_id, plan_key, expires_at in to_warn:
                    if PLANS.get(plan_key, {}).get("seconds", 0) <= WARN_BEFORE:
                        # короткий тариф (тест) — не предупреждаем, просто помечаем
                        await db.execute(
                            "UPDATE subscriptions SET warned=1 WHERE user_id=?",
                            (user_id,),
                        )
                        continue
                    until = time.strftime(
                        "%d.%m.%Y %H:%M", time.localtime(expires_at)
                    )
                    try:
                        await bot.send_message(
                            user_id,
                            f"⏰ Ваша подписка заканчивается <b>{until}</b> "
                            "(через ~2 дня). Продлите доступ, чтобы остаться в канале.",
                            reply_markup=main_menu(),
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning("warn failed: %s", e)
                    await db.execute(
                        "UPDATE subscriptions SET warned=1 WHERE user_id=?",
                        (user_id,),
                    )
                await db.commit()

                # Истёкшие подписки -> кик
                async with db.execute(
                    "SELECT user_id FROM subscriptions WHERE expires_at <= ?", (now,)
                ) as cur:
                    expired = await cur.fetchall()
                for (user_id,) in expired:
                    await kick_user(user_id)
                    await db.execute(
                        "DELETE FROM subscriptions WHERE user_id=?", (user_id,)
                    )
                    try:
                        await bot.send_message(
                            user_id,
                            "🚫 Срок подписки истёк, доступ к каналу закрыт. "
                            "Оформите подписку снова, чтобы вернуться.",
                            reply_markup=main_menu(),
                        )
                    except Exception:  # noqa: BLE001
                        pass
                await db.commit()
        except Exception as e:  # noqa: BLE001
            log.error("watcher error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)


# --------------------------------------------------------------------------- #
#                                ЗАПУСК                                        #
# --------------------------------------------------------------------------- #


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env")
    await init_db()
    asyncio.create_task(subscription_watcher())
    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
