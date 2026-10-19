import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GO_API_KEY = os.getenv("GO_API_KEY", "").strip()
GO_BASE_URL = os.getenv("GO_BASE_URL", "https://opencode.ai/inference/go/openai/v1").strip()
MODEL = os.getenv("MODEL", "deepseek-v4.1-flash").strip()

ALLOWED_IDS = {
    int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x
}
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "bot.sqlite3"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "8"))
USER_AGENT = os.getenv("USER_AGENT", "exam-prep-bot/1.0")
