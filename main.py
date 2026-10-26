import asyncio
import io
import logging
import os
import re
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import db
from config import ADMIN_ID, ALLOWED_IDS, BASE_DIR, BOT_TOKEN, DATA_DIR, YANDEX_API_KEY
from formatting import extract_score, md_to_html, split_html
from kb import KnowledgeBase
from keyboards import (
    BTN_ASK,
    BTN_DIAG,
    BTN_EXAM,
    BTN_ID,
    BTN_LIST,
    BTN_NEXT,
    BTN_RESET,
    BTN_STATS,
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
    "Оформляй ответ строго так, с пустой строкой между разделами:\n"
    "1) ✅ **Что верно**\n- ...\n\n"
    "2) ⚠️ **Что неточно или упущено**\n- ...\n\n"
    "3) 📖 **Краткий эталон**\n...\n\n"
    "4) **Оценка: N/5** (зачёт — 4–5)\n"
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
    text = (
        "Бот для подготовки к аттестационному экзамену риэлтера.\n\n"
        "Пользуйтесь кнопками меню внизу или командами:\n"
        "🎓 Экзамен — вопрос → ваш ответ → проверка\n"
        "📋 Выбрать вопрос — раздел → вопрос\n"
        "➡️ Следующий — следующий вопрос\n"
        "❓ Спросить по НПА — вопрос по законодательству\n"
        "⏹ Закончить — выйти из режима экзамена\n"
        "🔄 Сброс — очистить историю и прогресс\n\n"
        "Можно отвечать текстом или голосовым сообщением."
    )
    if is_admin(uid):
        text += "\n\nАдмин-панель: 📊 Статистика, 🔧 Диагностика."
    await message.answer(text, reply_markup=menu(uid))


@dp.message(Command("id"))
async def cmd_id(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("Команда доступна только администратору.")
        return
    await message.answer(f"Ваш Telegram ID: {uid}")


@dp.message(Command("diag"))
async def cmd_diag(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("Команда доступна только администратору.")
        return
    lines = [
        "🔧 Диагностика базы",
        f"BASE_DIR: {BASE_DIR}",
        f"DATA_DIR: {DATA_DIR}",
        f"exists: {os.path.isdir(DATA_DIR)}",
        f"cwd: {os.getcwd()}",
        f"answers: {kb.answers_path if kb else None}",
        f"questions: {len(kb.questions) if kb else 0}",
        f"chunks: {len(kb.docs) if kb else 0}",
    ]
    try:
        lines.append("files: " + ", ".join(sorted(os.listdir(DATA_DIR))[:20]))
    except Exception as e:
        lines.append(f"files: error {e}")
    await message.answer("\n".join(lines))


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
    db.set_user(uid, mode="exam", q_index=idx)
    q = kb.questions[idx]
    num = program_number(q["title"])
    label = f" (п. {num} Программы)" if num is not None else ""
    await send_long(
        message,
        f"Вопрос {idx + 1} из {len(kb.questions)}{label}\n\n{q['title']}\n\nЖду ваш ответ.",
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
        db.set_user(uid, mode="ask")
        await message.answer("Напишите вопрос по НПА одним сообщением.", reply_markup=menu(uid))
    elif text == BTN_STOP:
        await cmd_stop(message)
    elif text == BTN_RESET:
        await message.answer("Сбросить историю и прогресс?", reply_markup=reset_confirm())
    elif text == BTN_STATS:
        await cmd_stats(message)
    elif text == BTN_DIAG:
        await cmd_diag(message)
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
            max_tokens=3000,
        )
        db.add_history(uid, "user", answer)
        db.add_history(uid, "assistant", result)
        db.record_answer(
            uid,
            message.from_user.username,
            message.from_user.full_name,
            idx,
            q["title"],
            extract_score(result),
        )
        await send_long(
            message,
            md_to_html(result),
            reply_markup=after_answer(),
            parse_mode="HTML",
        )
    except LLMError as e:
        log.exception("LLM error")
        await message.answer(f"Ошибка модели: {e}")


async def answer_ask(message: Message, question: str):
    uid = message.from_user.id
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
    ]
    admin_commands = public_commands + [
        BotCommand(command="stats", description="📊 Статистика ответов"),
        BotCommand(command="check", description="🧪 Проверка базы ответов"),
        BotCommand(command="diag", description="🔧 Диагностика базы"),
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
