import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

import db
from config import ALLOWED_IDS, BOT_TOKEN
from kb import KnowledgeBase
from llm import LLMError, chat

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


async def send_long(message: Message, text: str):
    text = text or "(пусто)"
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000])


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
        "/exam — режим экзамена (вопрос → ваш ответ → проверка)\n"
        "/next — следующий вопрос\n"
        "/stop — выйти из режима экзамена\n"
        "/ask <вопрос> — спросить по НПА\n"
        "/reset — очистить историю\n"
        "/id — показать ваш ID"
    )


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


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
    q = kb.questions[idx]
    db.set_user(uid, mode="exam", q_index=idx)
    await send_long(message, f"Вопрос {idx + 1} из {len(kb.questions)}\n\n{q['title']}\n\nЖду ваш ответ.")


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
    q = kb.questions[idx]
    db.set_user(uid, mode="exam", q_index=idx)
    await send_long(message, f"Вопрос {idx + 1} из {len(kb.questions)}\n\n{q['title']}\n\nЖду ваш ответ.")


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        return
    db.set_user(uid, mode="idle")
    await message.answer("Вышли из режима экзамена. /exam — вернуться.")


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        return
    db.clear_history(uid)
    db.set_user(uid, mode="idle", q_index=None)
    await message.answer("История и прогресс сброшены. /exam — начать заново.")


@dp.message(Command("ask"))
async def cmd_ask(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        db.set_user(uid, mode="ask")
        await message.answer("Напишите вопрос по НПА одним сообщением.")
        return
    await answer_ask(message, parts[1])


@dp.message(F.text)
async def on_text(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        await message.answer(f"Доступ не выдан. Ваш ID: {uid}")
        return
    user = db.get_user(uid)
    text = (message.text or "").strip()
    if not text:
        return
    if user["mode"] == "exam" and user["q_index"] is not None:
        await grade_answer(message, text, user["q_index"])
    elif user["mode"] == "ask":
        await answer_ask(message, text)
    else:
        await message.answer("Выберите режим: /exam — экзамен, /ask <вопрос> — спросить по НПА.")


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
        await send_long(message, result + "\n\n/next — следующий вопрос.")
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


async def main():
    global kb
    if not BOT_TOKEN:
        print("BOT_TOKEN не задан в .env")
        sys.exit(1)
    db.init()
    kb = KnowledgeBase()
    bot = Bot(token=BOT_TOKEN)
    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
