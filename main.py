import asyncio
import io
import logging
import re
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import db
from config import ALLOWED_IDS, BOT_TOKEN, YANDEX_API_KEY
from kb import KnowledgeBase
from keyboards import (
    BTN_ASK,
    BTN_EXAM,
    BTN_ID,
    BTN_LIST,
    BTN_NEXT,
    BTN_RESET,
    BTN_STOP,
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
    "Структура ответа: 1) ✅ что верно; 2) ⚠️ что неточно или упущено (с пояснением); "
    "3) 📖 краткий эталон; 4) Оценка: 2–5 (зачёт — 4–5). "
    "Не выдумывай нормы, которых нет в эталоне. Пиши по-русски, кратко и по делу."
)

SYSTEM_ASK = (
    "Ты — помощник по подготовке к аттестационному экзамену риэлтера (Беларусь). "
    "Отвечай СТРОГО по предоставленному контексту из НПА. Указывай статьи и акты. "
    "Если в контексте нет ответа — честно скажи, что данных недостаточно. Не выдумывай."
)


def allowed(user_id: int) -> bool:
    return not ALLOWED_IDS or user_id in ALLOWED_IDS


def session_id(user_id: int) -> str:
    return f"tg-{user_id}"


async def send_long(message: Message, text: str, reply_markup=None):
    text = text or "(пусто)"
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or ["(пусто)"]
    for i, chunk in enumerate(chunks):
        await message.answer(chunk, reply_markup=reply_markup if i == len(chunks) - 1 else None)


def short_title(title: str) -> str:
    text = re.sub(r"^\d+\.\s*", "", title).replace("\n", " ").strip()
    if len(text) > 48:
        text = text[:47] + "…"
    return text


def questions_page(page: int = 0, per_page: int = 6):
    total = len(kb.questions) if kb else 0
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    start = page * per_page
    rows = []
    for i in range(start, min(start + per_page, total)):
        rows.append([
            InlineKeyboardButton(
                text=f"{i + 1}. {short_title(kb.questions[i]['title'])}",
                callback_data=f"q:{i}",
            )
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏮", callback_data="page:0"))
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"page:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"page:{page + 1}"))
        nav.append(InlineKeyboardButton(text="⏭", callback_data=f"page:{pages - 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔢 По номеру", callback_data="pick")])
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="close")])
    return f"📋 Выберите вопрос ({page + 1} из {pages}):", InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(
            f"Доступ не выдан.\nВаш Telegram ID: {uid}\n"
            "Передайте его администратору, чтобы вас добавили в белый список."
        )
        return
    await message.answer(
        "Бот для подготовки к аттестационному экзамену риэлтера.\n\n"
        "Пользуйтесь кнопками меню внизу или командами:\n"
        "🎓 Экзамен — вопрос → ваш ответ → проверка\n"
        "📋 Выбрать вопрос — список всех вопросов\n"
        "➡️ Следующий — следующий вопрос\n"
        "❓ Спросить по НПА — вопрос по законодательству\n"
        "⏹ Закончить — выйти из режима экзамена\n"
        "🔄 Сброс — очистить историю и прогресс\n"
        "🆔 Мой ID — показать ваш Telegram ID\n\n"
        "Можно отвечать текстом или голосовым сообщением.",
        reply_markup=main_menu(),
    )


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


async def send_question(message: Message, uid: int, idx: int):
    if not kb or not kb.questions:
        await message.answer("База вопросов не загружена. Проверьте папку knowledge.")
        return
    if not 0 <= idx < len(kb.questions):
        await message.answer("Вопрос не найден. /list — выбрать вопрос.")
        return
    db.set_user(uid, mode="exam", q_index=idx)
    q = kb.questions[idx]
    await send_long(
        message,
        f"Вопрос {idx + 1} из {len(kb.questions)}\n\n{q['title']}\n\nЖду ваш ответ.",
        reply_markup=main_menu(),
    )


@dp.message(Command("exam"))
async def cmd_exam(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    if not kb or not kb.questions:
        await message.answer("База вопросов не загружена. Проверьте папку knowledge.")
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
        await message.answer("База вопросов не загружена.")
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
    await message.answer("Вышли из режима экзамена. /exam — вернуться.", reply_markup=main_menu())


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        return
    db.clear_history(uid)
    db.reset_user(uid)
    await message.answer("История и прогресс сброшены. /exam — начать заново.", reply_markup=main_menu())


@dp.message(Command("ask"))
async def cmd_ask(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        db.set_user(uid, mode="ask")
        await message.answer("Напишите вопрос по НПА одним сообщением.", reply_markup=main_menu())
        return
    await answer_ask(message, parts[1])


@dp.message(Command("list"))
async def cmd_list(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    if not kb or not kb.questions:
        await message.answer("База вопросов не загружена. Проверьте папку knowledge.")
        return
    text, markup = questions_page(0)
    await message.answer(text, reply_markup=markup)


async def select_by_number(message: Message, text: str):
    if not kb or not kb.questions:
        await message.answer("База вопросов не загружена.")
        return
    digits = re.sub(r"\D", "", text or "")
    if not digits:
        await message.answer(f"Введите номер вопроса от 1 до {len(kb.questions)}.")
        return
    n = int(digits)
    if not 1 <= n <= len(kb.questions):
        await message.answer(f"Номер должен быть от 1 до {len(kb.questions)}.")
        return
    await send_question(message, message.from_user.id, n - 1)


@dp.message(Command("q"))
async def cmd_q(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    if not kb or not kb.questions:
        await message.answer("База вопросов не загружена. Проверьте папку knowledge.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        db.set_user(uid, mode="pick")
        await message.answer(
            f"Введите номер вопроса от 1 до {len(kb.questions)}.",
            reply_markup=main_menu(),
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
        db.set_user(uid, mode="ask")
        await message.answer("Напишите вопрос по НПА одним сообщением.", reply_markup=main_menu())
    elif text == BTN_STOP:
        await cmd_stop(message)
    elif text == BTN_RESET:
        await message.answer("Сбросить историю и прогресс?", reply_markup=reset_confirm())
    elif text == BTN_ID:
        await cmd_id(message)


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
    else:
        await message.answer(
            "Выберите действие кнопками меню или командой /exam.",
            reply_markup=main_menu(),
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
            max_tokens=3000,
        )
        db.add_history(uid, "user", answer)
        db.add_history(uid, "assistant", result)
        await send_long(message, result, reply_markup=after_answer())
    except LLMError as e:
        log.exception("LLM error")
        await message.answer(f"Ошибка модели: {e}")


async def answer_ask(message: Message, question: str):
    uid = message.from_user.id
    chunks = kb.search(question, k=6) if kb else []
    if not chunks:
        await message.answer("Не нашёл информации в базе. Уточните вопрос.")
        return
    context = "\n\n---\n\n".join(f"[{c['source']}]\n{c['text']}" for c in chunks)
    user_msg = f"КОНТЕКСТ ИЗ НПА:\n{context}\n\nВОПРОС:\n{question}"
    try:
        await message.answer("Ищу по НПА…")
        result = await chat(
            [{"role": "system", "content": SYSTEM_ASK}, {"role": "user", "content": user_msg}],
            session_id=session_id(uid),
            max_tokens=3000,
        )
        db.add_history(uid, "user", question)
        db.add_history(uid, "assistant", result)
        await send_long(message, result)
    except LLMError as e:
        log.exception("LLM error")
        await message.answer(f"Ошибка модели: {e}")


@dp.callback_query(F.data.startswith("page:"))
async def cb_page(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        await cb.answer("Доступ не выдан", show_alert=True)
        return
    if not kb or not kb.questions:
        await cb.answer("База вопросов не загружена", show_alert=True)
        return
    try:
        page = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    text, markup = questions_page(page)
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
    try:
        await cb.message.edit_text(f"Выбран вопрос {idx + 1} из {len(kb.questions)}.")
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
        await cb.message.edit_text(f"Введите номер вопроса от 1 до {len(kb.questions)} сообщением.")
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
        reply_markup=main_menu(),
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


async def main():
    global kb
    if not BOT_TOKEN:
        print("BOT_TOKEN не задан в .env")
        sys.exit(1)
    db.init()
    kb = KnowledgeBase()
    bot = Bot(token=BOT_TOKEN)
    await bot.set_my_commands([
        BotCommand(command="exam", description="🎓 Режим экзамена"),
        BotCommand(command="list", description="📋 Выбрать вопрос из списка"),
        BotCommand(command="q", description="🔢 Выбрать вопрос по номеру"),
        BotCommand(command="next", description="➡️ Следующий вопрос"),
        BotCommand(command="ask", description="❓ Спросить по НПА"),
        BotCommand(command="stop", description="⏹ Закончить экзамен"),
        BotCommand(command="reset", description="🔄 Сбросить прогресс"),
        BotCommand(command="id", description="🆔 Мой ID"),
    ])
    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
