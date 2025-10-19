import os
import logging
import sqlite3
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import requests
from datetime import datetime, timedelta

# ───────────────────────────────────────
#   НАСТРОЙКИ
# ───────────────────────────────────────
DB_NAME = "users.db"
REQUIRED_CHANNEL = "@rustycave"
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")  # Получите здесь: https://steamcommunity.com/dev/apikey

# ID администратора
ADMIN_IDS = {904487148}

# ───────────────────────────────────────
#   ЛОГИРОВАНИЕ
# ───────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ───────────────────────────────────────
#   СИСТЕМА ОГРАНИЧЕНИЯ ЗАПРОСОВ
# ───────────────────────────────────────
class RateLimiter:
    def __init__(self):
        self.requests = {}
    
    def check_limit(self, user_id, action, limit=5, period=60):
        now = datetime.now()
        key = f"{user_id}_{action}"
        
        if key not in self.requests:
            self.requests[key] = []
        
        # Удаляем старые запросы
        self.requests[key] = [t for t in self.requests[key] if now - t < timedelta(seconds=period)]
        
        if len(self.requests[key]) >= limit:
            return False
        
        self.requests[key].append(now)
        return True

limiter = RateLimiter()

# ───────────────────────────────────────
#   МЕНЕДЖЕР БАЗЫ ДАННЫХ
# ───────────────────────────────────────
class Database:
    def __enter__(self):
        self.conn = sqlite3.connect(DB_NAME)
        return self.conn.cursor()
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.conn.commit()
        except:
            self.conn.rollback()
        finally:
            self.conn.close()

# ───────────────────────────────────────
#   БАЗА ДАННЫХ
# ───────────────────────────────────────
def init_db() -> None:
    with Database() as cur:
        # Таблица пользователей
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                name TEXT,
                hours INTEGER,
                age INTEGER,
                bio TEXT,
                username TEXT,
                is_active INTEGER DEFAULT 1,
                is_verified INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Таблица статистики
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS stats (
                user_id INTEGER PRIMARY KEY,
                viewed_profiles INTEGER DEFAULT 0,
                likes_given INTEGER DEFAULT 0,
                matches INTEGER DEFAULT 0
            )
            """
        )

        # Лайки и ожидающие лайки
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS likes (
                from_id INTEGER,
                to_id INTEGER,
                PRIMARY KEY (from_id, to_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_likes (
                from_id INTEGER,
                to_id INTEGER,
                from_name TEXT,
                PRIMARY KEY (from_id, to_id)
            )
            """
        )

        # Жалобы
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                reporter_id INTEGER,
                reported_id INTEGER,
                reported_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Уведомления о новых пользователях
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS new_user_notifications (
                user_id INTEGER,
                notified_new_id INTEGER,
                PRIMARY KEY (user_id, notified_new_id)
            )
            """
        )

        # Таблица заблокированных пользователей
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY,
                blocked INTEGER DEFAULT 1
            )
            """
        )

# ───────────────────────────────────────
#   ВАЛИДАЦИЯ
# ───────────────────────────────────────
def validate_steam_id(steam_id):
    """Проверка корректности Steam ID"""
    if not isinstance(steam_id, (int, str)):
        return False
    if isinstance(steam_id, str) and not steam_id.isdigit():
        return False
    steam_id = int(steam_id)
    # Проверяем диапазон Steam ID
    return 76561197960265728 <= steam_id <= 76561197960265728 + 2**32

def validate_hours(hours):
    """Проверка корректности часов"""
    return isinstance(hours, int) and 0 <= hours <= 20000

def validate_age(age):
    """Проверка корректности возраста"""
    return isinstance(age, int) and 10 <= age <= 100

def validate_bio(bio):
    """Проверка корректности био"""
    return isinstance(bio, str) and 5 <= len(bio) <= 500

# ───────────────────────────────────────
#   ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ───────────────────────────────────────
def safe_db_execute(func):
    """Декоратор для безопасного выполнения запросов к БД"""
    def wrapper(*args, **kwargs):
        try:
            with Database() as cur:
                return func(cur, *args, **kwargs)
        except sqlite3.Error as e:
            logger.error(f"Database error in {func.__name__}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in {func.__name__}: {e}")
            return None
    return wrapper

@safe_db_execute
def save_user(cur, tg_id, name, hours, age, bio, username, is_active=1, is_verified=0):
    cur.execute(
        """
        INSERT OR REPLACE INTO users
        (telegram_id, name, hours, age, bio, username, is_active, is_verified)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (tg_id, name, hours, age, bio, username, is_active, is_verified),
    )

@safe_db_execute
def get_user_profile(cur, tg_id):
    cur.execute(
        """
        SELECT name, hours, age, bio, username, is_active, is_verified
        FROM users
        WHERE telegram_id = ?
        """,
        (tg_id,),
    )
    return cur.fetchone()

@safe_db_execute
def get_all_active_partners(cur, exclude_id):
    """Только активные и НЕ заблокированные пользователи."""
    cur.execute(
        """
        SELECT u.telegram_id, u.name, u.hours, u.age, u.bio, u.username, u.is_verified
        FROM users u
        LEFT JOIN blocked_users b ON u.telegram_id = b.user_id
        WHERE u.telegram_id != ?
          AND u.is_active = 1
          AND (b.blocked IS NULL OR b.blocked = 0)
        """,
        (exclude_id,),
    )
    return cur.fetchall()

def is_profile_complete(tg_id):
    return get_user_profile(tg_id) is not None

@safe_db_execute
def add_like(cur, from_id, to_id):
    cur.execute(
        "INSERT OR IGNORE INTO likes (from_id, to_id) VALUES (?, ?)",
        (from_id, to_id),
    )
    cur.execute(
        "SELECT 1 FROM likes WHERE from_id = ? AND to_id = ?", (to_id, from_id)
    )
    match = cur.fetchone() is not None
    if match:
        update_stat(from_id, "matches")
        update_stat(to_id, "matches")
    return match

@safe_db_execute
def add_pending_like(cur, from_id, to_id, from_name):
    cur.execute(
        "INSERT OR REPLACE INTO pending_likes (from_id, to_id, from_name) VALUES (?, ?, ?)",
        (from_id, to_id, from_name),
    )

@safe_db_execute
def get_pending_likes(cur, to_id):
    cur.execute(
        "SELECT from_id, from_name FROM pending_likes WHERE to_id = ?", (to_id,)
    )
    return cur.fetchall()

@safe_db_execute
def remove_pending_like(cur, from_id, to_id):
    cur.execute(
        "DELETE FROM pending_likes WHERE from_id = ? AND to_id = ?", (from_id, to_id)
    )

@safe_db_execute
def add_report(cur, reporter_id, reported_id):
    cur.execute(
        "INSERT INTO reports (reporter_id, reported_id) VALUES (?, ?)",
        (reporter_id, reported_id),
    )

@safe_db_execute
def deactivate_user(cur, tg_id):
    cur.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (tg_id,))

@safe_db_execute
def activate_user(cur, tg_id):
    cur.execute("UPDATE users SET is_active = 1 WHERE telegram_id = ?", (tg_id,))

@safe_db_execute
def block_user(cur, tg_id):
    """Поместить пользователя в чёрный список."""
    cur.execute(
        "INSERT OR REPLACE INTO blocked_users (user_id, blocked) VALUES (?, 1)",
        (tg_id,),
    )

@safe_db_execute
def unblock_user(cur, tg_id):
    """Снять блокировку."""
    cur.execute(
        "UPDATE blocked_users SET blocked = 0 WHERE user_id = ?", (tg_id,)
    )

@safe_db_execute
def get_blocked_list(cur):
    """Список всех заблокированных ID."""
    cur.execute("SELECT user_id FROM blocked_users WHERE blocked = 1")
    rows = cur.fetchall()
    return [r[0] for r in rows] if rows else []

@safe_db_execute
def clear_reports_for(cur, user_id):
    """Удалить все жалобы, связанные с этим пользователем."""
    cur.execute("DELETE FROM reports WHERE reported_id = ?", (user_id,))

@safe_db_execute
def get_reports_summary(cur):
    """Возвращает список кортежей (reported_id, count)."""
    cur.execute(
        """
        SELECT reported_id, COUNT(*) AS cnt
        FROM reports
        GROUP BY reported_id
        HAVING cnt > 0
        ORDER BY cnt DESC
        """
    )
    return cur.fetchall()

@safe_db_execute
def update_stat(cur, user_id, field):
    """Увеличивает один из счётчиков (viewed_profiles, likes_given, matches)."""
    allowed_fields = ["viewed_profiles", "likes_given", "matches"]
    if field not in allowed_fields:
        logger.error(f"Invalid field: {field}")
        return
    
    cur.execute("INSERT OR IGNORE INTO stats (user_id) VALUES (?)", (user_id,))
    cur.execute(f"UPDATE stats SET {field} = {field} + 1 WHERE user_id = ?", (user_id,))

@safe_db_execute
def get_stats(cur, user_id):
    cur.execute(
        "SELECT viewed_profiles, likes_given, matches FROM stats WHERE user_id = ?",
        (user_id,),
    )
    result = cur.fetchone()
    return result or (0, 0, 0)

def verify_user_steam(tg_id, steam_id):
    """Получить часы из Steam и поставить статус верификации."""
    try:
        # Валидация Steam ID
        if not validate_steam_id(steam_id):
            return "invalid_id"
        
        # Проверка наличия API ключа
        if not STEAM_API_KEY:
            return "no_api_key"
        
        # Запрос к API
        url = "http://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
        params = {
            "key": STEAM_API_KEY,
            "steamid": steam_id,
            "format": "json",
            "appids_filter[0]": 252490,  # Rust
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        games = data.get("response", {}).get("games", [])
        
        for game in games:
            if game["appid"] == 252490:
                hours = game.get("playtime_forever", 0) // 60
                
                # Обновляем профиль
                try:
                    with Database() as cur:
                        cur.execute(
                            "UPDATE users SET hours = ?, is_verified = 1 WHERE telegram_id = ?",
                            (hours, tg_id),
                        )
                    return hours
                except sqlite3.Error as e:
                    logger.error(f"Database error updating user profile: {e}")
                    return "db_error"
        
        return "no_game"
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Steam API request failed: {e}")
        return "api_error"
    except (KeyError, ValueError) as e:
        logger.error(f"Steam API data parsing failed: {e}")
        return "data_error"
    except Exception as e:
        logger.error(f"Unexpected error in verify_user_steam: {e}")
        return "error"

# ───────────────────────────────────────
#   КЛАВИАТУРА (кнопки)
# ───────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔍 Найти напарника"), KeyboardButton("🔄 Обновить анкету")],
            [KeyboardButton("👤 Профиль"), KeyboardButton("📊 Статистика")],
            [KeyboardButton("❤️ Посмотреть лайки"), KeyboardButton("🔕 Скрыть анкету")],
        ],
        resize_keyboard=True,
    )

def profile_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Показывать", callback_data="activate_profile")],
            [InlineKeyboardButton("❌ Скрыть", callback_data="deactivate_profile")],
        ]
    )

def steam_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎮 Привязать Steam", callback_data="link_steam")],
            [InlineKeyboardButton("✍️ Ввести часы вручную", callback_data="manual_hours")],
        ]
    )

def steam_help_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❓ Как найти Steam ID", callback_data="steam_help")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_hours")]
    ])

def subscribe_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")],
        [InlineKeyboardButton("🔄 Проверить подписку", callback_data="check_subscription")],
    ])

# ───────────────────────────────────────
#   ПРОВЕРКА ПОДПИСКИ
# ───────────────────────────────────────
async def check_subscription(user_id, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False

async def ask_to_subscribe(update: Update):
    await update.message.reply_text(
        f"👋 Привет!\n\n"
        f"Чтобы пользоваться нашим ботом, подпишитесь на канал:\n"
        f"👉 {REQUIRED_CHANNEL}\n\n"
        f"Это поможет нам развивать сообщество. Спасибо! ❤️",
        reply_markup=subscribe_keyboard(),
    )

# ───────────────────────────────────────
#   ДЕКОРАТОР ДЛЯ ПРОВЕРКИ ПОДПИСКИ
# ───────────────────────────────────────
def subscription_required(func):
    """Декоратор для проверки подписки перед выполнением функции"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not await check_subscription(user.id, context):
            await update.message.reply_text(
                f"❌ Чтобы пользоваться ботом, подпишитесь на канал:\n"
                f"{REQUIRED_CHANNEL}\n\n"
                "После этого нажмите кнопку ниже, чтобы проверить:",
                reply_markup=subscribe_keyboard(),
            )
            return
        return await func(update, context)
    return wrapper

# ───────────────────────────────────────
#   ДЕКОРАТОР ДЛЯ АДМИНОВ
# ───────────────────────────────────────
def admin_only(func):
    """Разрешает выполнить функцию только администратору."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("❌ У вас нет прав для этой команды.")
            return
        return await func(update, context)
    return wrapper

# ───────────────────────────────────────
#   ХЭНДЛЕРЫ
# ───────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_subscription(user.id, context):
        await ask_to_subscribe(update)
        return

    if get_user_profile(user.id):
        await update.message.reply_text(
            f
