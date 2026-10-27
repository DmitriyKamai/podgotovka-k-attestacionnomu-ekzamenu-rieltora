import os
import shutil

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GO_API_KEY = os.getenv("GO_API_KEY", "").strip()
GO_BASE_URL = os.getenv("GO_BASE_URL", "https://opencode.ai/inference/go/openai/v1").strip()
MODEL = os.getenv("MODEL", "deepseek-v4.1-flash").strip()

YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "").strip()
YANDEX_STT_URL = os.getenv(
    "YANDEX_STT_URL", "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
).strip()

# Платный доступ: значения по умолчанию. Меняются админом в меню «⚙️ Настройки».
DEFAULT_PAID_MODE = True
DEFAULT_PRICE_STARS = 500
DEFAULT_DEMO_LIMIT = 5
# Telegram разрешает для звёздных подписок только 30 дней (2592000 секунд).
SUBSCRIPTION_DAYS = 30
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/exam_admin").strip()

# Администратор: статистика и диагностика доступны только этому ID.
ADMIN_ID = 7930922924

ALLOWED_IDS = {
    int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x
}
_default_data = os.path.join(BASE_DIR, "knowledge")
if not os.path.isdir(_default_data) and os.path.isdir(os.path.join(BASE_DIR, "data")):
    _default_data = os.path.join(BASE_DIR, "data")


def _resolve_dir(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not os.path.isabs(value):
        value = os.path.join(BASE_DIR, value)
    return os.path.normpath(value)


_env_data = _resolve_dir(os.getenv("DATA_DIR", ""))
_candidates = [
    _env_data,
    os.path.join(BASE_DIR, "knowledge"),
    os.path.join(BASE_DIR, "data"),
    os.path.join(os.getcwd(), "knowledge"),
    os.path.join(os.getcwd(), "data"),
]


def _has_answers(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    return any(
        os.path.isfile(os.path.join(path, name))
        for name in ("answers.md", "Ответы_частые_вопросы.md")
    )


DATA_DIR = next((p for p in _candidates if _has_answers(p)), None)
if not DATA_DIR:
    DATA_DIR = next(
        (p for p in _candidates if p and os.path.isdir(p)),
        os.path.join(BASE_DIR, "knowledge"),
    )
def _resolve_db_path() -> str:
    """База — в папке storage внутри проекта (вне Git)."""
    folder = os.path.join(BASE_DIR, "storage")
    path = os.path.join(folder, "bot.sqlite3")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        return os.path.join(BASE_DIR, "bot.sqlite3")
    # переносим старую базу при первом запуске
    for old in (
        os.path.join(BASE_DIR, "bot.sqlite3"),
        os.path.join(os.path.expanduser("~"), ".exam-bot", "bot.sqlite3"),
    ):
        if os.path.isfile(old) and not os.path.isfile(path):
            try:
                shutil.copy2(old, path)
                break
            except Exception:
                pass
    return path


DB_PATH = _resolve_db_path()
# Внешняя база (Postgres). Если задана — SQLite не используется.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "8"))
USER_AGENT = os.getenv("USER_AGENT", "exam-prep-bot/1.0")
