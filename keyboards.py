from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

BTN_EXAM = "🎓 Экзамен"
BTN_LIST = "📋 Выбрать вопрос"
BTN_NEXT = "➡️ Следующий"
BTN_ASK = "❓ Спросить по НПА"
BTN_STOP = "⏹ Закончить"
BTN_RESET = "🔄 Сброс"
BTN_SUBSCRIBE = "💳 Подписка"
BTN_ID = "🆔 Мой ID"
BTN_STATS = "📊 Статистика"
BTN_DIAG = "🔧 Диагностика"
BTN_GRANT = "➕ Выдать доступ"
BTN_SETTINGS = "⚙️ Настройки"
BTN_USERS = "👥 Пользователи"

MENU_TEXTS = {
    BTN_EXAM, BTN_LIST, BTN_NEXT, BTN_ASK, BTN_STOP, BTN_RESET, BTN_SUBSCRIBE,
    BTN_ID, BTN_STATS, BTN_DIAG, BTN_GRANT, BTN_SETTINGS, BTN_USERS,
}


def main_menu(admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_EXAM), KeyboardButton(text=BTN_LIST)],
        [KeyboardButton(text=BTN_NEXT), KeyboardButton(text=BTN_ASK)],
        [KeyboardButton(text=BTN_STOP), KeyboardButton(text=BTN_RESET)],
        [KeyboardButton(text=BTN_SUBSCRIBE)],
    ]
    if admin:
        rows.append([KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_USERS)])
        rows.append([KeyboardButton(text=BTN_GRANT), KeyboardButton(text=BTN_SETTINGS)])
        rows.append([KeyboardButton(text=BTN_DIAG), KeyboardButton(text=BTN_ID)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Ответьте текстом или голосовым 🎤",
    )


def after_answer() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=BTN_NEXT, callback_data="next"),
                InlineKeyboardButton(text=BTN_STOP, callback_data="stop"),
            ]
        ]
    )


def reset_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, сбросить", callback_data="reset_yes"),
                InlineKeyboardButton(text="Отмена", callback_data="reset_no"),
            ]
        ]
    )
