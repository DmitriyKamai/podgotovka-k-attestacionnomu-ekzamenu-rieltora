import asyncio
import html
import io
import logging
import os
import random
import re
import sys
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

import db
from config import (
    ADMIN_ID,
    ALLOWED_IDS,
    BASE_DIR,
    BOT_TOKEN,
    DATA_DIR,
    DB_PATH,
    DEFAULT_DEMO_LIMIT,
    DEFAULT_PAID_MODE,
    DEFAULT_PRICE_STARS,
    SUBSCRIPTION_DAYS,
    SUPPORT_URL,
    YANDEX_API_KEY,
)
from formatting import extract_score, md_to_html, split_html
from kb import KnowledgeBase
from keyboards import (
    BTN_ASK,
    BTN_DIAG,
    BTN_EXAM,
    BTN_GRANT,
    BTN_ID,
    BTN_LIST,
    BTN_NEXT,
    BTN_RESET,
    BTN_SETTINGS,
    BTN_STATS,
    BTN_STOP,
    BTN_SUBSCRIBE,
    BTN_USERS,
    MENU_TEXTS,
    after_answer,
    main_menu,
    reset_confirm,
)
from llm import LLMError, chat
from stt import STTError, recognize

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("exam-bot")

dp = Dispatcher()
kb: KnowledgeBase | None = None

SYSTEM_EXAM = (
    "Ты — член экзаменационной комиссии по аттестации риэлтеров (Республика Беларусь). "
    "Тебе дают вопрос, эталонный ответ и ответ претендента. Проверяй ПО СМЫСЛУ, не требуя дословного совпадения. "
    "Сначала мысленно определи оценку. Если оценка 3 или выше — начни ответ ОДНОЙ строкой-реакцией: "
    "'💬 ' и до 160 знаков. В ней: (1) назови конкретную деталь этого ответа, которая удалась, и "
    "(2) дай конкретный следующий шаг — что добавить или уточнить, чтобы ответ стал сильнее. "
    "Если оценка 5 — отметь, что именно сделало ответ сильным, и что держать в таком же духе. "
    "Тон — поддерживающий и рабочий, как у наставника: без лести, восторженных слов и восклицаний, "
    "но и без сухого перечисления «есть/нет». "
    "Хорошо: «Передаточный акт и запрет замены вы поймали верно — добавьте момент исполнения и госрегистрацию, и ответ станет полным.» "
    "Плохо: «Есть передаточный акт, нет госрегистрации.» "
    "При оценке 2 и ниже строку '💬' НЕ добавляй. Затем — разбор строго так, с пустой строкой между разделами:\n"
    "1) ✅ **Что верно**\n- ...\n\n"
    "2) ⚠️ **Что неточно или упущено**\n- ...\n\n"
    "3) 📖 **Краткий эталон**\n— пункт\n— пункт\n\n"
    "4) **Оценка: N/5** (зачёт — 4–5)\n"
    "В разделе 3 — 4–7 отдельных пунктов, каждый с новой строки и с «— »; не писать сплошным абзацем. "
    "Каждый пункт — с новой строки. Не используй таблицы. "
    "Не выдумывай нормы, которых нет в эталоне. Пиши по-русски, кратко и по делу."
)

SYSTEM_ASK = (
    "Ты — помощник по подготовке к аттестационному экзамену риэлтера (Республика Беларусь). "
    "В контексте тебе дают эталонный ответ по Программе (если вопрос из Программы) и полные тексты статей НПА. "
    "Отвечай ПОЛНО: бери эталонный ответ за основу, дополняй его конкретными нормами из статей, "
    "называй статьи и акты. Не сокращай ответ до одного абзаца, если вопрос этого требует. "
    "Оформляй аккуратно: короткие абзацы, списки с новой строки, ключевые термины — **жирным**. "
    "Не используй таблицы. "
    "НЕ начинай ответ с оговорок и НЕ пиши, что данных недостаточно, если ответ можно собрать из контекста. "
    "Если чего-то действительно нет в контексте — одной короткой строкой в конце укажи, что стоит уточнить. "
    "Не выдумывай нормы, которых нет в контексте."
)


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def allowed(user_id: int) -> bool:
    return is_admin(user_id) or not ALLOWED_IDS or user_id in ALLOWED_IDS


def menu(uid: int):
    return main_menu(is_admin(uid))


def session_id(user_id: int) -> str:
    return f"tg-{user_id}"


# ---------- платный доступ ----------

_invoice_cache: dict = {}


def paid_mode() -> bool:
    return db.get_setting("paid_mode", "1" if DEFAULT_PAID_MODE else "0") == "1"


def price_stars() -> int:
    try:
        return max(1, int(db.get_setting("price_stars", str(DEFAULT_PRICE_STARS))))
    except ValueError:
        return DEFAULT_PRICE_STARS


def demo_limit() -> int:
    try:
        return max(0, int(db.get_setting("demo_limit", str(DEFAULT_DEMO_LIMIT))))
    except ValueError:
        return DEFAULT_DEMO_LIMIT


def has_access(uid: int) -> bool:
    return is_admin(uid) or db.is_paid(uid)


def demo_left(uid: int) -> int:
    return max(0, demo_limit() - db.access(uid)["demo_used"])


def total_questions() -> int:
    return len(kb.questions) if kb else 192


PRAISE = {
    5: [
        "Сильный ответ — тема разобрана по существу, а не по верхам.",
        "Уверенно и точно: видно понимание норм, а не заучивание.",
        "Такой ответ и на экзамене прозвучит убедительно.",
    ],
    4: [
        "Крепкий ответ: основное на месте, не хватило пары деталей.",
        "Логика верная, до полного ответа — один шаг.",
        "Хорошо: суть поймана, осталось отшлифовать формулировки.",
    ],
    3: [
        "Основа есть, но формулировки пока приблизительные — стоит повторить.",
        "Зачёт на грани: главное уловили, детали просядут.",
        "Три из пяти — двигаетесь верно, но к этой теме лучше вернуться.",
    ],
}


def praise_line(score: int | None) -> str:
    if score and score >= 3:
        return random.choice(PRAISE.get(score, []))
    return ""


REACTION_RE = re.compile(r"(?m)^\s*(?:\*\*)?💬(?:\*\*)?\s*(.+?)\s*$")


def split_reaction(text: str) -> tuple[str, str]:
    """Отделяет сгенерированную строку-реакцию '💬 …' от разбора."""
    text = text or ""
    m = REACTION_RE.search(text)
    if not m:
        return "", text
    reaction = m.group(1).strip()
    cleaned = (text[:m.start()] + text[m.end():]).strip()
    return reaction, cleaned


ETALON_RE = re.compile(
    r"(?s)(3\)\s*📖\s*(?:\*\*)?Краткий эталон(?:\*\*)?)\s*(.*?)(?=\n\s*4\)\s*(?:\*\*)?Оценка|\Z)"
)


def structure_etalon(text: str) -> str:
    """Приводит раздел «Краткий эталон» к пунктам, если модель выдала сплошной абзац."""
    text = text or ""
    m = ETALON_RE.search(text)
    if not m:
        return text
    body = m.group(2).strip()
    lines = [l.strip() for l in body.split("\n") if l.strip()]
    bullets = [l for l in lines if l.startswith(("-", "—", "•", "*"))]
    if len(bullets) >= 3:
        return text
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z])", body) if s.strip()]
    if len(sentences) < 2:
        return text
    items = []
    for s in sentences:
        if items and len(s) < 45:
            items[-1] = (items[-1] + " " + s).strip()
        else:
            items.append(s)
    new_body = "\n".join(f"— {it}" for it in items)
    return text[:m.start(2)] + "\n" + new_body + "\n" + text[m.end(2):]


async def get_invoice_link(bot: Bot) -> str:
    price, days = price_stars(), SUBSCRIPTION_DAYS
    key = (price, days)
    if key not in _invoice_cache:
        _invoice_cache[key] = await bot.create_invoice_link(
            title="Полный доступ к тренажёру риэлтера",
            description=f"Все {total_questions()} вопросов программы + ответы по НПА + «Спросить по НПА». Подписка {days} дней.",
            payload="sub",
            currency="XTR",
            prices=[LabeledPrice(label=f"Подписка {days} дней", amount=price)],
            subscription_period=days * 86400,
        )
    return _invoice_cache[key]


async def send_paywall(message: Message, uid: int, need_paid: bool = False):
    try:
        link = await get_invoice_link(message.bot)
    except Exception:
        log.exception("invoice link failed")
        await message.answer(
            "Оплата сейчас недоступна. Напишите @exam_admin."
        )
        return
    price, days = price_stars(), SUBSCRIPTION_DAYS
    head = "Этот раздел доступен только по подписке." if need_paid else "Бесплатные вопросы закончились."
    await message.answer(
        f"{head}\n\n"
        f"Подписка открывает все {total_questions()} вопросов программы, "
        f"ответы по НПА и режим «Спросить по НПА» на {days} дней.\n"
        f"Цена — {price} ⭐. Продлевается автоматически, отменить можно в Telegram → Настройки → Telegram Stars → Подписки.\n"
        f"Условия: /terms.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"💳 Оплатить {price} ⭐", url=link)
        ]]),
    )


async def ensure_access(message: Message, uid: int) -> bool:
    """Допуск к вопросу: демо или подписка."""
    if not paid_mode() or has_access(uid):
        return True
    if demo_left(uid) > 0:
        db.inc_demo(uid)
        return True
    await send_paywall(message, uid)
    return False


async def ensure_paid(message: Message, uid: int) -> bool:
    """Допуск к платным разделам (без демо): «Спросить по НПА»."""
    if not paid_mode() or has_access(uid):
        return True
    await send_paywall(message, uid, need_paid=True)
    return False


async def show_subscription(message: Message, uid: int):
    if not paid_mode():
        await message.answer("Сейчас платный режим отключён — все вопросы доступны бесплатно.")
        return
    if is_admin(uid):
        await message.answer("Вы администратор — полный доступ уже есть, оплата не нужна.")
        return
    a = db.access(uid)
    if a["granted"]:
        await message.answer("У вас доступ, выданный администратором. Оплата не нужна.")
        return
    if a["paid_until"] and a["paid_until"] > time.time():
        exp = time.strftime("%d.%m.%Y", time.localtime(a["paid_until"]))
        await message.answer(
            f"✅ Подписка активна до {exp}.\n"
            "Продлевается автоматически. Отменить можно в Telegram → Настройки → Telegram Stars → Подписки."
        )
        return
    try:
        link = await get_invoice_link(message.bot)
    except Exception:
        log.exception("invoice link failed")
        await message.answer("Оплата сейчас недоступна. Напишите @exam_admin.")
        return
    left = demo_left(uid)
    demo_note = f"Бесплатных вопросов осталось: {left}.\n\n" if left > 0 else ""
    await message.answer(
        f"{demo_note}Подписка на {SUBSCRIPTION_DAYS} дней — {price_stars()} ⭐.\n"
        "Открывает все вопросы программы, проверку ответов и «Спросить по НПА».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"💳 Оплатить {price_stars()} ⭐", url=link)
        ]]),
    )


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    await show_subscription(message, uid)


async def send_long(message: Message, text: str, reply_markup=None, parse_mode: str | None = None):
    text = text or "(пусто)"
    if parse_mode == "HTML":
        chunks = split_html(text)
    else:
        chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or ["(пусто)"]
    for i, chunk in enumerate(chunks):
        await message.answer(
            chunk,
            parse_mode=parse_mode,
            reply_markup=reply_markup if i == len(chunks) - 1 else None,
        )


def short_title(title: str) -> str:
    text = re.sub(r"^\d+\.\s*", "", title).replace("\n", " ").strip()
    if len(text) > 48:
        text = text[:47] + "…"
    return text


def program_number(title: str) -> int | None:
    m = re.match(r"^\s*(\d+)\.", title)
    return int(m.group(1)) if m else None


def find_program_index(num: int) -> int | None:
    if not kb:
        return None
    for i, q in enumerate(kb.questions):
        if program_number(q["title"]) == num:
            return i
    return None


def sections_page():
    total = len(kb.sections) if kb else 0
    rows = []
    for i, sec in enumerate(kb.sections):
        cnt = len(kb.section_questions(i))
        rows.append([
            InlineKeyboardButton(text=f'{sec["short"]} · {cnt}', callback_data=f"sec:{i}")
        ])
    rows.append([InlineKeyboardButton(text="🔢 По номеру", callback_data="pick")])
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="close")])
    return (
        f"📚 Выберите раздел Программы ({total}):",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def questions_page(sec_index: int, page: int = 0, per_page: int = 6):
    idxs = kb.section_questions(sec_index) if kb else []
    total = len(idxs)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    start = page * per_page
    rows = []
    for i in idxs[start:start + per_page]:
        title = kb.questions[i]["title"]
        num = program_number(title)
        label = f"{num}. {short_title(title)}" if num is not None else f"• {short_title(title)}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"q:{i}")])
    nav = []
    if pages > 1:
        if page > 0:
            nav.append(InlineKeyboardButton(text="⏮", callback_data=f"page:{sec_index}:0"))
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"page:{sec_index}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"page:{sec_index}:{page + 1}"))
            nav.append(InlineKeyboardButton(text="⏭", callback_data=f"page:{sec_index}:{pages - 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Разделы", callback_data="secs")])
    rows.append([InlineKeyboardButton(text="🔢 По номеру", callback_data="pick")])
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="close")])
    sec_title = kb.sections[sec_index]["title"] if kb else ""
    last = min(start + per_page, total)
    return (
        f"📂 {sec_title}\nВопросы {start + 1}–{last} из {total}:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(
            f"Доступ не выдан.\nВаш Telegram ID: {uid}\n"
            "Передайте его администратору, чтобы вас добавили в белый список."
        )
        return
    db.set_user(uid, mode="idle")
    text = (
        "Бот для подготовки к аттестационному экзамену риэлтера.\n\n"
        "Пользуйтесь кнопками меню внизу или командами:\n"
        "🎓 Экзамен — вопрос → ваш ответ → проверка\n"
        "📋 Выбрать вопрос — раздел → вопрос\n"
        "➡️ Следующий — следующий вопрос\n"
        "❓ Спросить по НПА — вопрос по законодательству\n"
        "⏹ Закончить — выйти из режима экзамена\n"
        "🔄 Сброс — очистить историю и прогресс\n"
        "💳 Подписка — купить или проверить доступ\n\n"
    )
    if YANDEX_API_KEY:
        text += "🎤 Можно отвечать голосовым сообщением — просто запишите голосовое вместо текста."
    else:
        text += "Можно отвечать текстом."
    if paid_mode():
        text += (
            f"\n\n🔒 Бесплатно — первые {demo_limit()} вопросов. "
            f"Полный доступ — по подписке {price_stars()} ⭐ / {SUBSCRIPTION_DAYS} дней. "
            "«Спросить по НПА» — только по подписке."
        )
        text += "\n\n📄 Условия — /terms, поддержка — /support."
    if is_admin(uid):
        text += "\n\nАдмин-панель: 📊 Статистика, 🔧 Диагностика, ⚙️ Настройки."
    await message.answer(text, reply_markup=menu(uid))


@dp.message(Command("terms"))
async def cmd_terms(message: Message):
    await message.answer(
        "📄 Условия подписки\n\n"
        f"• Услуга: доступ к тренажёру риэлтера — все {total_questions()} вопросов, проверка ответов и «Спросить по НПА».\n"
        f"• Срок: {SUBSCRIPTION_DAYS} дней с момента оплаты, продлевается автоматически.\n"
        f"• Цена: {price_stars()} ⭐ за период.\n"
        "• Отмена: в Telegram → Настройки → Telegram Stars → Подписки (до даты следующего списания).\n"
        "• Поддержка: @exam_admin.\n"
        "• Доступ цифровой, без материального носителя.\n\n"
        "Оплачивая подписку, вы подтверждаете, что ознакомились с условиями и согласны с ними."
    )


def support_link(text: str = "@exam_admin") -> str:
    url = SUPPORT_URL
    if url.startswith("http") or url.startswith("tg://"):
        return f'<a href="{html.escape(url, quote=True)}">{text}</a>'
    return html.escape(url)


@dp.message(Command("support"))
async def cmd_support(message: Message):
    await message.answer(
        "🛟 Поддержка\n\n"
        f"По всем вопросам — {support_link()}.",
        parse_mode="HTML",
    )


@dp.message(Command("paysupport"))
async def cmd_paysupport(message: Message):
    await message.answer(
        "💳 Поддержка по оплате\n\n"
        "Если оплата прошла, а доступ не открылся — "
        f"{support_link()}.\n\n"
        "Приложите скриншот чека из Telegram (раздел «Мои звёзды» → история платежей).",
        parse_mode="HTML",
    )


@dp.message(Command("id"))
async def cmd_id(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("Команда доступна только администратору.")
        return
    await message.answer(f"Ваш Telegram ID: {uid}")


async def grant_access(message: Message, text: str):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("Команда доступна только администратору.")
        return
    m = re.match(r"\s*(\d+)\s*(?:(\d+))?\s*$", text)
    if not m:
        await message.answer(
            "Формат: номер ID и (необязательно) срок в днях.\n"
            "Например:\n`123456789` — доступ навсегда\n`123456789 30` — на 30 дней",
        )
        return
    target = int(m.group(1))
    days = int(m.group(2)) if m.group(2) else None
    db.grant(target, days=days)
    if days:
        await message.answer(f"✅ Выдан доступ на {days} дн. пользователю {target}.")
    else:
        await message.answer(f"✅ Выдан постоянный доступ пользователю {target}.")


@dp.message(Command("grant"))
async def cmd_grant(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        db.set_user(uid, mode="grant")
        await message.answer(
            "Пришлите ID пользователя (и, если нужно, срок в днях):\n"
            "`123456789` — навсегда\n`123456789 30` — на 30 дней",
            reply_markup=menu(uid),
        )
        return
    await grant_access(message, parts[1])


def settings_view():
    rows = [
        [InlineKeyboardButton(text="🔁 Платный режим", callback_data="set:paid")],
        [
            InlineKeyboardButton(text="💰 Цена", callback_data="set:price"),
            InlineKeyboardButton(text="🎁 Демо", callback_data="set:demo"),
        ],
        [InlineKeyboardButton(text="✖️ Закрыть", callback_data="close")],
    ]
    text = (
        "⚙️ Настройки\n\n"
        f"Платный режим: {'включён' if paid_mode() else 'выключен'}\n"
        f"Цена подписки: {price_stars()} ⭐\n"
        f"Бесплатных вопросов: {demo_limit()}\n"
        f"Срок подписки: {SUBSCRIPTION_DAYS} дней (фиксирован Telegram)\n\n"
        "Нажмите, что изменить:"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def show_settings(message: Message):
    text, markup = settings_view()
    await message.answer(text, reply_markup=markup)


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("Команда доступна только администратору.")
        return
    await show_settings(message)


@dp.callback_query(F.data == "set:paid")
async def cb_set_paid(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Только администратор", show_alert=True)
        return
    db.set_setting("paid_mode", "0" if paid_mode() else "1")
    text, markup = settings_view()
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    await cb.answer("Переключено")


@dp.callback_query(F.data.startswith("set:"))
async def cb_set_value(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Только администратор", show_alert=True)
        return
    key = cb.data.split(":", 1)[1]
    prompts = {
        "price": ("setting_price", f"Введите цену подписки в звёздах (сейчас {price_stars()}):"),
        "demo": ("setting_demo", f"Сколько вопросов давать бесплатно (сейчас {demo_limit()}):"),
    }
    if key not in prompts:
        await cb.answer()
        return
    mode, text = prompts[key]
    db.set_user(cb.from_user.id, mode=mode)
    try:
        await cb.message.edit_text(text + "\n\nОтправьте число сообщением.")
    except Exception:
        pass
    await cb.answer()


async def save_setting(message: Message, mode: str, text: str):
    digits = re.sub(r"\D", "", text or "")
    if not digits:
        await message.answer("Нужно число. Откройте /settings и попробуйте снова.")
        return
    n = int(digits)
    if mode == "setting_price":
        if not 1 <= n <= 10000:
            await message.answer("Цена должна быть от 1 до 10 000 ⭐.")
            return
        db.set_setting("price_stars", str(n))
    else:
        if not 0 <= n <= 1000:
            await message.answer("Демо — от 0 до 1000 вопросов.")
            return
        db.set_setting("demo_limit", str(n))
    _invoice_cache.clear()
    await message.answer("✅ Сохранено.")
    await show_settings(message)


@dp.message(Command("diag"))
async def cmd_diag(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("Команда доступна только администратору.")
        return
    db_exists = os.path.isfile(DB_PATH)
    db_size = os.path.getsize(DB_PATH) if db_exists else 0
    db_in_base = os.path.dirname(os.path.abspath(DB_PATH)) == os.path.abspath(BASE_DIR)
    lines = [
        "🔧 Диагностика базы",
        f"БД: {'внешняя Postgres' if db.USE_PG else 'локальная SQLite'}",
        f"BASE_DIR: {BASE_DIR}",
        f"DATA_DIR: {DATA_DIR}",
        f"DB: {DB_PATH}" if not db.USE_PG else "DB: DATABASE_URL (внешняя)",
        f"DB exists: {db_exists} ({db_size} байт)" if not db.USE_PG else "DB: подключение внешнее",
        f"пользователей: {db.count_users()} · ответов: {db.count_answers()}",
        f"exists: {os.path.isdir(DATA_DIR)}",
        f"cwd: {os.getcwd()}",
        f"answers: {kb.answers_path if kb else None}",
        f"questions: {len(kb.questions) if kb else 0}",
        f"chunks: {len(kb.docs) if kb else 0}",
    ]
    if db_in_base and not db.USE_PG:
        lines.append(
            "⚠️ БД лежит в папке бота — при передеплое она может удалиться. "
            "Задайте DATABASE_URL на внешнюю Postgres."
        )
    try:
        lines.append("files: " + ", ".join(sorted(os.listdir(DATA_DIR))[:20]))
    except Exception as e:
        lines.append(f"files: error {e}")
    await message.answer("\n".join(lines))


@dp.message(Command("stars"))
async def cmd_stars(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("Команда доступна только администратору.")
        return
    try:
        res = await message.bot.get_star_transactions(limit=50)
    except Exception as e:
        await message.answer(f"Не удалось получить операции: {e}")
        return
    lines = ["⭐ Операции бота (последние 50):", ""]
    incoming = 0
    for t in res.transactions:
        when = time.strftime("%d.%m.%Y %H:%M", time.localtime(t.date))
        user = getattr(t.source, "user", None)
        if user is not None:
            who = f" — @{user.username}" if user.username else f" — ID {user.id}"
        else:
            who = ""
        incoming += max(0, t.amount)
        lines.append(f"{when}  {t.amount:+d} ⭐{who}")
    lines.append("")
    lines.append(f"Поступления в выборке: {incoming} ⭐")
    lines.append("Баланс и вывод — в профиле бота (карандаш → «Баланс») или на Fragment.")
    await send_long(message, "\n".join(lines))


@dp.message(Command("check"))
async def cmd_check(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("Команда доступна только администратору.")
        return
    if not kb:
        await message.answer("База не загружена.")
        return
    issues = kb.check()
    lines = [
        "🧪 Проверка базы ответов",
        f"вопросов: {len(kb.questions)}",
        f"фрагментов НПА: {len(kb.docs)}",
        f"статей: {sum(len(v) for v in kb.articles.values())}",
        "",
    ]
    if issues:
        lines.append(f"Замечаний: {len(issues)}")
        lines += [f"• {x}" for x in issues]
    else:
        lines.append("Замечаний нет — все ответы на месте.")
    await send_long(message, "\n".join(lines))


def format_access(row) -> str:
    uid = row["user_id"]
    if is_admin(uid):
        return "админ"
    if row["granted"]:
        return "бесплатно (навсегда)"
    pu = row["paid_until"]
    if pu and pu > time.time():
        days = max(1, int((pu - time.time() + 86399) // 86400))
        return f"платно, {days} дн."
    return f"демо {row['demo_used']}/{demo_limit()}"


def users_text(rows) -> str:
    lines = [f"👥 Пользователи ({len(rows)}):", ""]
    for i, r in enumerate(rows, 1):
        if r["username"]:
            name = f"@{r['username']}"
        elif r["first_name"]:
            name = r["first_name"]
        else:
            name = f"ID {r['user_id']}"
        avg = f"{r['avg']:.1f}" if r["avg"] is not None else "—"
        lines.append(f"{i}. {name} — {format_access(r)} · {r['count']} отв., ср. {avg}")
    return "\n".join(lines)


@dp.message(Command("users"))
async def cmd_users(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("Команда доступна только администратору.")
        return
    rows = db.users_overview()
    if not rows:
        await message.answer("Пользователей пока нет.")
        return
    await send_long(message, users_text(rows))


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("Команда доступна только администратору.")
        return
    totals = db.stats_totals()
    rows = db.stats()
    if not totals["count"]:
        await message.answer("📊 Статистика пока пуста — ни один ответ ещё не проверен.")
        return
    avg_all = f"{totals['avg']:.2f}" if totals["avg"] is not None else "—"
    lines = [
        "📊 Статистика",
        f"Всего ответов: {totals['count']}",
        f"Средний балл: {avg_all}",
        f"Пользователей: {totals['users']}",
        "",
    ]
    for i, r in enumerate(rows, 1):
        if r["username"]:
            name = f"@{r['username']}"
        elif r["first_name"]:
            name = r["first_name"]
        else:
            name = f"ID {r['user_id']}"
        avg = f"{r['avg']:.2f}" if r["avg"] is not None else "—"
        lines.append(f"{i}. {name} — {r['count']} отв., средний {avg}")
    await send_long(message, "\n".join(lines))


async def send_question(message: Message, uid: int, idx: int):
    if not kb or not kb.questions:
        await message.answer("База вопросов не загружена. Проверьте папку knowledge или отправьте /diag.")
        return
    if not 0 <= idx < len(kb.questions):
        await message.answer("Вопрос не найден. /list — выбрать вопрос.")
        return
    if not await ensure_access(message, uid):
        return
    db.set_user(uid, mode="exam", q_index=idx)
    q = kb.questions[idx]
    demo_note = ""
    if paid_mode() and not has_access(uid):
        demo_note = f"\n\n🔒 Бесплатных вопросов осталось: {demo_left(uid)}"
    voice_note = " Можно текстом или голосовым 🎤." if YANDEX_API_KEY else ""
    await send_long(
        message,
        f"Вопрос {idx + 1} из {len(kb.questions)}\n\n{q['title']}\n\nЖду ваш ответ.{voice_note}{demo_note}",
        reply_markup=menu(uid),
    )


@dp.message(Command("exam"))
async def cmd_exam(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    if not kb or not kb.questions:
        await message.answer("База вопросов не загружена. Проверьте папку knowledge или отправьте /diag.")
        return
    user = db.get_user(uid)
    idx = user["q_index"] if user["q_index"] is not None else 0
    if idx >= len(kb.questions):
        await message.answer("Вопросы закончились. /reset — начать заново.")
        return
    await send_question(message, uid, idx)


@dp.message(Command("next"))
async def cmd_next(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    if not kb or not kb.questions:
        await message.answer("База вопросов не загружена. Отправьте /diag.")
        return
    user = db.get_user(uid)
    idx = (user["q_index"] or 0) + 1
    if idx >= len(kb.questions):
        await message.answer("Вопросы закончились. /reset — начать заново.")
        return
    await send_question(message, uid, idx)


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        return
    db.set_user(uid, mode="idle")
    await message.answer("Вышли из режима экзамена. /exam — вернуться.", reply_markup=menu(uid))


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        return
    db.clear_history(uid)
    db.reset_user(uid)
    await message.answer("История и прогресс сброшены. /exam — начать заново.", reply_markup=menu(uid))


@dp.message(Command("ask"))
async def cmd_ask(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    if not await ensure_paid(message, uid):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        db.set_user(uid, mode="ask")
        await message.answer("Напишите вопрос по НПА одним сообщением.", reply_markup=menu(uid))
        return
    await answer_ask(message, parts[1])


@dp.message(Command("list"))
async def cmd_list(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    if not kb or not kb.questions:
        await message.answer("База вопросов не загружена. Проверьте папку knowledge или отправьте /diag.")
        return
    text, markup = sections_page()
    await message.answer(text, reply_markup=markup)


async def select_by_number(message: Message, text: str):
    if not kb or not kb.questions:
        await message.answer("База вопросов не загружена. Отправьте /diag.")
        return
    digits = re.sub(r"\D", "", text or "")
    if not digits:
        await message.answer("Введите номер пункта Программы, например 43.")
        return
    n = int(digits)
    idx = find_program_index(n)
    if idx is None:
        await message.answer(
            f"Вопроса с номером {n} нет в списке. Откройте /list и выберите из списка."
        )
        return
    await send_question(message, message.from_user.id, idx)


@dp.message(Command("q"))
async def cmd_q(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    if not kb or not kb.questions:
        await message.answer("База вопросов не загружена. Проверьте папку knowledge или отправьте /diag.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        db.set_user(uid, mode="pick")
        await message.answer(
            "Введите номер пункта Программы, например 43.",
            reply_markup=menu(uid),
        )
        return
    await select_by_number(message, parts[1])


@dp.message(F.text.in_(MENU_TEXTS))
async def on_menu(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    text = message.text
    if text == BTN_EXAM:
        await cmd_exam(message)
    elif text == BTN_LIST:
        await cmd_list(message)
    elif text == BTN_NEXT:
        await cmd_next(message)
    elif text == BTN_ASK:
        if not await ensure_paid(message, uid):
            return
        db.set_user(uid, mode="ask")
        await message.answer("Напишите вопрос по НПА одним сообщением.", reply_markup=menu(uid))
    elif text == BTN_STOP:
        await cmd_stop(message)
    elif text == BTN_RESET:
        await message.answer("Сбросить историю и прогресс?", reply_markup=reset_confirm())
    elif text == BTN_SUBSCRIBE:
        await show_subscription(message, uid)
    elif text == BTN_STATS:
        await cmd_stats(message)
    elif text == BTN_USERS:
        await cmd_users(message)
    elif text == BTN_DIAG:
        await cmd_diag(message)
    elif text == BTN_ID:
        await cmd_id(message)
    elif text == BTN_GRANT:
        db.set_user(uid, mode="grant")
        await message.answer(
            "Пришлите ID пользователя (и, если нужно, срок в днях):\n"
            "`123456789` — навсегда\n`123456789 30` — на 30 дней",
            reply_markup=menu(uid),
        )
    elif text == BTN_SETTINGS:
        await show_settings(message)


@dp.message(F.text)
async def on_text(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    text = (message.text or "").strip()
    if not text:
        return
    await handle_text(message, text)


@dp.message(F.voice)
async def on_voice(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    if not YANDEX_API_KEY:
        await message.answer(
            "Голосовые сообщения не настроены.\n"
            "Добавьте YANDEX_API_KEY и YANDEX_FOLDER_ID в .env и перезапустите бота."
        )
        return
    voice = message.voice
    try:
        await message.answer("🎤 Распознаю…")
        tg_file = await message.bot.get_file(voice.file_id)
        buf = io.BytesIO()
        await message.bot.download_file(tg_file.file_path, destination=buf)
        text = await recognize(buf.getvalue(), duration=voice.duration or 0)
    except STTError as e:
        log.warning("STT error: %s", e)
        await message.answer(f"Не удалось распознать: {e}")
        return
    except Exception:
        log.exception("voice download failed")
        await message.answer("Ошибка при загрузке голосового. Попробуйте ещё раз.")
        return
    text = (text or "").strip()
    if not text:
        await message.answer("Не разобрал речь. Попробуйте ещё раз или напишите текстом.")
        return
    await send_long(message, f"🎤 {text}")
    await handle_text(message, text)


async def handle_text(message: Message, text: str):
    uid = message.from_user.id
    user = db.get_user(uid)
    if user["mode"] == "exam" and user["q_index"] is not None:
        await grade_answer(message, text, user["q_index"])
    elif user["mode"] == "ask":
        await answer_ask(message, text)
    elif user["mode"] == "pick":
        await select_by_number(message, text)
    elif user["mode"] == "grant":
        db.set_user(uid, mode="idle")
        await grant_access(message, text)
    elif user["mode"] in ("setting_price", "setting_demo"):
        mode = user["mode"]
        db.set_user(uid, mode="idle")
        await save_setting(message, mode, text)
    else:
        await message.answer(
            "Выберите действие кнопками меню или командой /exam.",
            reply_markup=menu(uid),
        )


async def grade_answer(message: Message, answer: str, idx: int):
    uid = message.from_user.id
    if not kb or idx >= len(kb.questions):
        await message.answer("Вопрос не найден. /exam — начать заново.")
        return
    q = kb.questions[idx]
    user_msg = (
        f"ВОПРОС:\n{q['title']}\n\n"
        f"ЭТАЛОННЫЙ ОТВЕТ:\n{q['body']}\n\n"
        f"ОТВЕТ ПРЕТЕНДЕНТА:\n{answer}"
    )
    try:
        await message.answer("Проверяю…")
        result = await chat(
            [{"role": "system", "content": SYSTEM_EXAM}, {"role": "user", "content": user_msg}],
            session_id=session_id(uid),
            max_tokens=4000,
        )
        reaction, cleaned = split_reaction(result)
        cleaned = structure_etalon(cleaned)
        score = extract_score(cleaned)
        db.add_history(uid, "user", answer)
        db.add_history(uid, "assistant", cleaned)
        db.record_answer(
            uid,
            message.from_user.username,
            message.from_user.full_name,
            idx,
            q["title"],
            score,
        )
        html = md_to_html(cleaned)
        if score and score >= 3:
            line = html.escape(reaction or praise_line(score))
            html = f"{line}\n\n{html}"
        await send_long(
            message,
            html,
            reply_markup=after_answer(),
            parse_mode="HTML",
        )
    except LLMError as e:
        log.exception("LLM error")
        await message.answer(f"Ошибка модели: {e}")


async def answer_ask(message: Message, question: str):
    uid = message.from_user.id
    if not await ensure_paid(message, uid):
        return
    context = kb.build_context(question) if kb else ""
    if not context:
        await message.answer("Не нашёл информации в базе. Уточните вопрос.")
        return
    user_msg = f"КОНТЕКСТ (эталонный ответ и НПА):\n{context}\n\nВОПРОС:\n{question}"
    try:
        await message.answer("Ищу по НПА…")
        result = await chat(
            [{"role": "system", "content": SYSTEM_ASK}, {"role": "user", "content": user_msg}],
            session_id=session_id(uid),
            max_tokens=3500,
        )
        db.add_history(uid, "user", question)
        db.add_history(uid, "assistant", result)
        await send_long(message, md_to_html(result), parse_mode="HTML")
    except LLMError as e:
        log.exception("LLM error")
        await message.answer(f"Ошибка модели: {e}")


@dp.callback_query(F.data.startswith("sec:"))
async def cb_section(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        await cb.answer("Доступ не выдан", show_alert=True)
        return
    if not kb or not kb.questions:
        await cb.answer("База вопросов не загружена", show_alert=True)
        return
    try:
        sec = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    if not 0 <= sec < len(kb.sections):
        await cb.answer("Раздел не найден", show_alert=True)
        return
    text, markup = questions_page(sec, 0)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data == "secs")
async def cb_sections(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        await cb.answer("Доступ не выдан", show_alert=True)
        return
    if not kb or not kb.questions:
        await cb.answer("База вопросов не загружена", show_alert=True)
        return
    text, markup = sections_page()
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("page:"))
async def cb_page(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        await cb.answer("Доступ не выдан", show_alert=True)
        return
    if not kb or not kb.questions:
        await cb.answer("База вопросов не загружена", show_alert=True)
        return
    parts = cb.data.split(":")
    try:
        if len(parts) >= 3:
            sec, page = int(parts[1]), int(parts[2])
        else:
            sec, page = 0, int(parts[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    if not 0 <= sec < len(kb.sections):
        await cb.answer("Раздел не найден", show_alert=True)
        return
    text, markup = questions_page(sec, page)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("q:"))
async def cb_question(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        await cb.answer("Доступ не выдан", show_alert=True)
        return
    if not kb or not kb.questions:
        await cb.answer("База вопросов не загружена", show_alert=True)
        return
    try:
        idx = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    if not 0 <= idx < len(kb.questions):
        await cb.answer("Вопрос не найден", show_alert=True)
        return
    await cb.answer()
    sec = kb.section_of(idx)
    try:
        await cb.message.edit_text(
            f"Выбран вопрос {idx + 1} из {len(kb.questions)}.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="⬅️ К разделу", callback_data=f"sec:{sec}")
                ]]
            ),
        )
    except Exception:
        pass
    await send_question(cb.message, cb.from_user.id, idx)


@dp.callback_query(F.data == "pick")
async def cb_pick(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        await cb.answer("Доступ не выдан", show_alert=True)
        return
    if not kb or not kb.questions:
        await cb.answer("База вопросов не загружена", show_alert=True)
        return
    db.set_user(cb.from_user.id, mode="pick")
    try:
        await cb.message.edit_text("Введите номер пункта Программы сообщением, например 43.")
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@dp.callback_query(F.data == "close")
async def cb_close(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except Exception:
        try:
            await cb.message.edit_text("Список закрыт.")
        except Exception:
            pass
    await cb.answer()


@dp.callback_query(F.data == "next")
async def cb_next(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        await cb.answer("Доступ не выдан", show_alert=True)
        return
    if not kb or not kb.questions:
        await cb.answer("База вопросов не загружена", show_alert=True)
        return
    user = db.get_user(cb.from_user.id)
    idx = (user["q_index"] or 0) + 1
    if idx >= len(kb.questions):
        await cb.answer("Вопросы закончились. /reset — начать заново.", show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await send_question(cb.message, cb.from_user.id, idx)


@dp.callback_query(F.data == "stop")
async def cb_stop(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        await cb.answer("Доступ не выдан", show_alert=True)
        return
    db.set_user(cb.from_user.id, mode="idle")
    await cb.answer("Ок")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer(
        "Вышли из режима экзамена. /exam — вернуться.",
        reply_markup=menu(cb.from_user.id),
    )


@dp.callback_query(F.data == "reset_yes")
async def cb_reset_yes(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        await cb.answer("Доступ не выдан", show_alert=True)
        return
    db.clear_history(cb.from_user.id)
    db.reset_user(cb.from_user.id)
    await cb.answer("Сброшено")
    try:
        await cb.message.edit_text("История и прогресс сброшены. /exam — начать заново.")
    except Exception:
        pass


@dp.callback_query(F.data == "reset_no")
async def cb_reset_no(cb: CallbackQuery):
    await cb.answer("Отменено")
    try:
        await cb.message.edit_text("Сброс отменён.")
    except Exception:
        pass


@dp.pre_checkout_query()
async def on_pre_checkout(pcq: PreCheckoutQuery):
    await pcq.answer(ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    uid = message.from_user.id
    sp = message.successful_payment
    until = sp.subscription_expiration_date or (int(time.time()) + SUBSCRIPTION_DAYS * 86400)
    db.set_paid(uid, until)
    db.record_payment(
        uid,
        sp.telegram_payment_charge_id,
        sp.total_amount,
        sp.currency,
        bool(sp.is_recurring or sp.is_first_recurring),
        until,
    )
    exp = time.strftime("%d.%m.%Y", time.localtime(until))
    await message.answer(
        f"✅ Оплата принята!\nПодписка активна до {exp}.\n"
        f"Все вопросы программы и «Спросить по НПА» теперь доступны.",
        reply_markup=menu(uid),
    )


async def main():
    global kb
    if not BOT_TOKEN:
        print("BOT_TOKEN не задан в .env")
        sys.exit(1)
    db.init()
    kb = KnowledgeBase()
    bot = Bot(token=BOT_TOKEN)
    public_commands = [
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="exam", description="🎓 Режим экзамена"),
        BotCommand(command="list", description="📋 Выбрать вопрос из списка"),
        BotCommand(command="q", description="🔢 Выбрать вопрос по номеру Программы"),
        BotCommand(command="next", description="➡️ Следующий вопрос"),
        BotCommand(command="ask", description="❓ Спросить по НПА"),
        BotCommand(command="stop", description="⏹ Закончить экзамен"),
        BotCommand(command="reset", description="🔄 Сбросить прогресс"),
        BotCommand(command="subscribe", description="💳 Подписка"),
        BotCommand(command="terms", description="📄 Условия подписки"),
        BotCommand(command="support", description="🛟 Поддержка"),
        BotCommand(command="paysupport", description="💳 Поддержка по оплате"),
    ]
    admin_commands = public_commands + [
        BotCommand(command="stats", description="📊 Статистика ответов"),
        BotCommand(command="users", description="👥 Пользователи и доступ"),
        BotCommand(command="check", description="🧪 Проверка базы ответов"),
        BotCommand(command="diag", description="🔧 Диагностика базы"),
        BotCommand(command="grant", description="➕ Выдать доступ по ID"),
        BotCommand(command="settings", description="⚙️ Настройки оплаты"),
        BotCommand(command="stars", description="⭐ Операции в звёздах"),
        BotCommand(command="id", description="🆔 Мой ID"),
    ]
    await bot.set_my_commands(public_commands)
    try:
        await bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=ADMIN_ID),
        )
    except Exception:
        log.exception("Не удалось установить команды для администратора")
    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
