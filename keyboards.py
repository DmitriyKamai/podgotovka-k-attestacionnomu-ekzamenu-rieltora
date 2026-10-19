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
BTN_ID = "🆔 Мой ID"

MENU_TEXTS = {
    BTN_EXAM, BTN_LIST, BTN_NEXT, BTN_ASK, BTN_STOP, BTN_RESET, BTN_ID
}


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_EXAM), KeyboardButton(text=BTN_LIST)],
            [KeyboardButton(text=BTN_NEXT), KeyboardButton(text=BTN_ASK)],
            [KeyboardButton(text=BTN_STOP), KeyboardButton(text=BTN_RESET)],
            [KeyboardButton(text=BTN_ID)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие или напишите ответ",
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
