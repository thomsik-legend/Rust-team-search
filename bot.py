import time
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

# ID администратора - ЗАМЕНИТЕ НА СВОЙ ID
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
                reported_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(reporter_id, reported_id)
            )
            """
        )

        # Таблица временных банов
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS temp_bans (
                user_id INTEGER PRIMARY KEY,
                banned_until TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        LEFT JOIN temp_bans b ON u.telegram_id = b.user_id
        WHERE u.telegram_id != ?
          AND u.is_active = 1
          AND (b.banned_until IS NULL OR b.banned_until < ?)
        """,
        (exclude_id, datetime.now().isoformat()),
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
        "INSERT OR IGNORE INTO reports (reporter_id, reported_id) VALUES (?, ?)",
        (reporter_id, reported_id),
    )

@safe_db_execute
def deactivate_user(cur, tg_id):
    cur.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (tg_id,))

@safe_db_execute
def activate_user(cur, tg_id):
    cur.execute("UPDATE users SET is_active = 1 WHERE telegram_id = ?", (tg_id,))

@safe_db_execute
def ban_user_temporarily(cur, user_id, days=5):
    banned_until = datetime.now() + timedelta(days=days)
    cur.execute(
        "INSERT OR REPLACE INTO temp_bans (user_id, banned_until) VALUES (?, ?)",
        (user_id, banned_until.isoformat()),
    )
    deactivate_user(user_id)

@safe_db_execute
def unban_user(cur, user_id):
    cur.execute("DELETE FROM temp_bans WHERE user_id = ?", (user_id,))
    activate_user(user_id)

@safe_db_execute
def is_user_banned(cur, user_id):
    cur.execute(
        "SELECT banned_until FROM temp_bans WHERE user_id = ? AND banned_until > ?",
        (user_id, datetime.now().isoformat()),
    )
    return cur.fetchone() is not None

@safe_db_execute
def get_banned_until(cur, user_id):
    cur.execute(
        "SELECT banned_until FROM temp_bans WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None

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

def restart_search_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Начать сначала", callback_data="restart_search")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
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
# ───────────────────────────────────────
#   ДЕКОРАТОР ДЛЯ ПРОВЕРКИ ПОДПИСКИ (УЛУЧШЕННЫЙ)
# ───────────────────────────────────────
def subscription_required(func):
    """Декоратор для проверки подписки перед выполнением функции"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not await check_subscription(user.id, context):
            # Умный способ ответить: через сообщение или через callback-запрос
            text = (
                f"❌ Чтобы пользоваться ботом, подпишитесь на канал:\n"
                f"{REQUIRED_CHANNEL}\n\n"
                "После этого нажмите кнопку ниже, чтобы проверить:"
            )
            if update.message:
                await update.message.reply_text(text, reply_markup=subscribe_keyboard())
            elif update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(text, reply_markup=subscribe_keyboard())
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
@subscription_required
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Проверка на временный бан
    if is_user_banned(user.id):
        banned_until = get_banned_until(user.id)
        dt = datetime.fromisoformat(banned_until)
        await update.message.reply_text(
            f"⏳ Вы временно ограничены в использовании бота до {dt.strftime('%d.%m %H:%M')}.\n"
            "Спасибо за понимание.",
            reply_markup=ReplyKeyboardMarkup([[
                KeyboardButton("ℹ️ Помощь")
            ]], resize_keyboard=True)
        )
        return
    
    if get_user_profile(user.id):
        await update.message.reply_text(
            f"👋 С возвращением, {user.first_name}!", reply_markup=main_keyboard()
        )
        return

    await update.message.reply_text(
        f"👋 Привет, {user.first_name}! Давай создадим профиль.\n"
        "Как указать часы в Rust?",
        reply_markup=steam_keyboard(),
    )
    context.user_data["step"] = "choose_method"

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Помощь*\n\n"
        "• 🔍 *Найти напарника* — начать поиск.\n"
        "• 🔄 *Обновить анкету* — изменить данные.\n"
        "• 👤 *Профиль* — посмотреть свою анкету.\n"
        "• 📊 *Статистика* — ваша активность.\n"
        "• ❤️ *Посмотреть лайки* — кто вас лайкнул.\n"
        "• 🔕 *Скрыть анкету* — чтобы вас не показывали.\n\n"
        "🔧 **Команды для администратора:**\n"
        "`/reports` — список анкет с жалобами.\n",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )

@subscription_required
async def start_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Сколько часов ты откатал в Rust?", reply_markup=steam_keyboard()
    )
    context.user_data["step"] = "choose_method"

# ── ОБРАБОТКА ТЕКСТОВ И ШАГОВ АНКЕТЫ ──
@subscription_required
async def handle_text_and_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    text = update.message.text.strip()
    user = update.effective_user

    # ПРОВЕРЯЕМ: есть ли у пользователя анкета?
    profile = get_user_profile(user.id)

    # ЕСЛИ АНКЕТА ЕСТЬ — обрабатываем команды меню, игнорируя шаги
    if profile:
        context.user_data["step"] = None  # Сбрасываем шаг, чтобы не мешал

        if text == "🔍 Найти напарника":
            await find_partner(update, context)
        elif text == "🔄 Обновить анкету":
            await start_profile(update, context)
        elif text == "👤 Профиль":
            await profile_command(update, context)
        elif text == "📊 Статистика":
            await stats_command(update, context)
        elif text == "❤️ Посмотреть лайки":
            await show_likes_command(update, context)
        elif text == "🔕 Скрыть анкету":
            deactivate_user(user.id)
            await update.message.reply_text(
                "❌ Ваша анкета скрыта из поиска.", reply_markup=main_keyboard()
            )
        else:
            await update.message.reply_text(
                "Не понял. Выберите действие из меню.", reply_markup=main_keyboard()
            )
        return  # ВЫХОДИМ — дальше не идём

    # ЕСЛИ АНКЕТЫ НЕТ — работаем по шагам
    step = context.user_data.get("step")

    if step == "choose_method":
        # ... (оставь как есть)

    # ==============================
    # 1️⃣ ШАГИ СОЗДАНИЯ/ОБНОВЛЕНИЯ
    # ==============================
    if step == "choose_method":
        if text.isdigit() and len(text) > 5:            # скорее всего Steam‑ID
            result = verify_user_steam(user.id, text)
            if isinstance(result, int):
                await update.message.reply_text(
                    f"✅ Получено {result} часов из Steam.\n"
                    "Теперь укажите ваш возраст:",
                )
                context.user_data["hours"] = result
                context.user_data["step"] = "age"
            elif result == "invalid_id":
                await update.message.reply_text(
                    "⚠️ Неверный формат Steam ID. Проверьте ID и попробуйте снова.",
                    reply_markup=steam_help_keyboard()
                )
            else:
                await update.message.reply_text(
                    "⚠️ Не удалось получить часы из Steam. Введите их вручную."
                )
                context.user_data["step"] = "hours_manual"
            return
        else:
            await update.message.reply_text(
                "Введите ваш Steam‑ID (цифры) **или** нажмите кнопку «Ввести часы вручную».",
                parse_mode="Markdown",
            )
            return

    if step == "hours_manual":
        try:
            hours = int(text)
            if not validate_hours(hours):
                await update.message.reply_text("Введите корректное количество часов (0-20000).")
                return
            context.user_data["hours"] = hours
            await update.message.reply_text("📅 Укажите ваш возраст:")
            context.user_data["step"] = "age"
        except ValueError:
            await update.message.reply_text("Введите число, например: 150")
        return

    if step == "age":
        try:
            age = int(text)
            if not validate_age(age):
                await update.message.reply_text("Возраст должен быть от 10 до 100.")
                return
            context.user_data["age"] = age
            await update.message.reply_text("💬 Напишите немного о себе:")
            context.user_data["step"] = "bio"
        except ValueError:
            await update.message.reply_text("Введите число, например: 25")
        return

    if step == "bio":
        if not validate_bio(text):
            await update.message.reply_text("Текст должен быть от 5 до 500 символов.")
            return
        context.user_data["bio"] = text
        save_user(
            user.id,
            user.first_name,
            context.user_data["hours"],
            context.user_data["age"],
            context.user_data["bio"],
            user.username,
        )
        await update.message.reply_text(
            "✅ Профиль сохранён! Теперь можно искать напарников.",
            reply_markup=main_keyboard(),
        )
        context.user_data["step"] = None
        return

    # ==============================
    # 2️⃣ ОБРАБОТКА КНОПОК МЕНЮ
    # ==============================
    if text == "🔍 Найти напарника":
        await find_partner(update, context)
    elif text == "🔄 Обновить анкету":
        await start_profile(update, context)
    elif text == "👤 Профиль":
        await profile_command(update, context)
    elif text == "📊 Статистика":
        await stats_command(update, context)
    elif text == "❤️ Посмотреть лайки":
        await show_likes_command(update, context)
    elif text == "🔕 Скрыть анкету":
        deactivate_user(user.id)
        await update.message.reply_text(
            "❌ Ваша анкета скрыта из поиска.", reply_markup=main_keyboard()
        )
    else:
        await update.message.reply_text(
            "Не понял. Выберите действие из меню.", reply_markup=main_keyboard()
        )

# ── ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ──
@subscription_required
async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Проверка на временный бан
    if is_user_banned(user.id):
        banned_until = get_banned_until(user.id)
        dt = datetime.fromisoformat(banned_until)
        await update.message.reply_text(
            f"⏳ Вы временно ограничены в использовании бота до {dt.strftime('%d.%m %H:%M')}.\n"
            "Спасибо за понимание.",
            reply_markup=ReplyKeyboardMarkup([[
                KeyboardButton("ℹ️ Помощь")
            ]], resize_keyboard=True)
        )
        return
    
    data = get_user_profile(user.id)
    if not data:
        await update.message.reply_text(
            "❌ У вас нет анкеты. Нажмите «🔄 Обновить анкету».",
            reply_markup=main_keyboard(),
        )
        return

    name, hours, age, bio, username, is_active, is_verified = data
    status = "✅ Показывается" if is_active else "❌ Скрыта"
    verified = "✅ ВЕРИФИЦИРОВАН" if is_verified else ""

    await update.message.reply_text(
        f"📋 *Твой профиль {verified}*\n\n"
        f"📛 Имя: {name}\n"
        f"⏰ Часы в Rust: {hours}\n"
        f"🎂 Возраст: {age}\n"
        f"💬 О себе: {bio}\n"
        f"🔗 Telegram: @{username if username else 'не указано'}\n"
        f"👁️ Статус: {status}",
        parse_mode="Markdown",
        reply_markup=profile_keyboard(),
    )

# ── СТАТИСТИКА ──
@subscription_required
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    viewed, likes, matches = get_stats(update.effective_user.id)
    await update.message.reply_text(
        "📊 *Твоя статистика*\n\n"
        f"👁️ Просмотрено анкет: {viewed}\n"
        f"❤️ Лайков поставлено: {likes}\n"
        f"🔥 Взаимных матчей: {matches}",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )

# ── УМНЫЙ ПОДБОР ПАРТНЁРА ──
def advanced_similarity(current_hours, current_age, partner):
    _, _, hours, age, bio, _, is_verified = partner
    diff = abs(hours - current_hours) * 0.5 + abs(age - current_age) * 0.5
    verified_bonus = -20 if is_verified else 0
    keyword_bonus = -10 if bio and any(w in bio.lower() for w in ["спокойный", "тихий", "база", "дружелюбный"]) else 0
    return diff + verified_bonus + keyword_bonus

@subscription_required
async def find_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Проверка ограничения запросов
    if not limiter.check_limit(user.id, "find_partner", 10, 60):
        await update.message.reply_text("⚠️ Слишком много запросов. Подождите минуту.")
        return

    # Проверка на временный бан
    if is_user_banned(user.id):
        banned_until = get_banned_until(user.id)
        dt = datetime.fromisoformat(banned_until)
        await update.message.reply_text(
            f"⏳ Вы временно ограничены в использовании бота до {dt.strftime('%d.%m %H:%M')}.\n"
            "Спасибо за понимание.",
            reply_markup=ReplyKeyboardMarkup([[
                KeyboardButton("ℹ️ Помощь")
            ]], resize_keyboard=True)
        )
        return

    if not is_profile_complete(user.id):
        await context.bot.send_message(
            chat_id=chat_id,
            text="📝 Сначала создайте анкету. Нажмите «🔄 Обновить анкету».",
            reply_markup=main_keyboard(),
        )
        return

    profile = get_user_profile(user.id)
    if not profile:
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Ошибка загрузки профиля.",
            reply_markup=main_keyboard(),
        )
        return

    cur_hours, cur_age = profile[1], profile[2]

    partners = get_all_active_partners(user.id)
    if not partners:
        await context.bot.send_message(
            chat_id=chat_id,
            text="😢 Пока нет доступных участников.",
            reply_markup=main_keyboard(),
        )
        return

    partners_sorted = sorted(partners, key=lambda p: advanced_similarity(cur_hours, cur_age, p))
    context.user_data["partner_queue"] = [p[0] for p in partners_sorted]
    context.user_data["partner_data"] = {p[0]: p for p in partners_sorted}
    context.user_data["current_partner_index"] = 0
    context.user_data["original_partners"] = [p[0] for p in partners_sorted]  # Сохраняем оригинальный список

    await show_partner(chat_id, context, partners_sorted[0])

async def show_partner(chat_id, context: ContextTypes.DEFAULT_TYPE, partner):
    partner_id, name, hours, age, bio, username, is_verified = partner
    context.user_data["current_partner_id"] = partner_id
    verified_badge = "✅" if is_verified else ""

    kb = [
        [
            InlineKeyboardButton("❤️ Лайк", callback_data=f"like_{partner_id}"),
            InlineKeyboardButton("👎 Дизлайк", callback_data=f"dislike_{partner_id}"),
        ],
        [InlineKeyboardButton("🚨 Пожаловаться", callback_data=f"report_{partner_id}")],
    ]
    markup = InlineKeyboardMarkup(kb)

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"👤 *Найден напарник {verified_badge}*\n\n"
            f"📛 Имя: {name}\n"
            f"⏰ Часы в Rust: {hours}\n"
            f"🎂 Возраст: {age}\n"
            f"💬 О себе: {bio}"
        ),
        parse_mode="Markdown",
        reply_markup=markup,
    )

async def next_partner(chat_id, context: ContextTypes.DEFAULT_TYPE, user_id):
    queue = context.user_data.get("partner_queue", [])
    if not queue:
        # Если очередь закончилась, предлагаем начать сначала
        await context.bot.send_message(
            chat_id=chat_id,
            text="🎉 Вы просмотрели всех доступных напарников!\n\nХотите начать поиск заново?",
            reply_markup=restart_search_keyboard(),
        )
        return
    
    next_id = queue.pop(0)
    context.user_data["partner_queue"] = queue
    partner = context.user_data.get("partner_data", {}).get(next_id)
    if partner:
        await show_partner(chat_id, context, partner)

# ── ОТВЕТ НА ПРИХОДЯЩИЙ ЛАЙК, ПАГИНАЦИЯ И ЖАЛОБЫ ──
@subscription_required
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data.split("_")
    action = data[0]

    # ---------- ЛАЙК / ДИЗЛАЙК ----------
    if action in ("like", "dislike"):
        partner_id = int(data[1])
        user_id = query.from_user.id
        user_name = query.from_user.first_name

        if action == "like":
            is_match = add_like(user_id, partner_id)
            update_stat(user_id, "likes_given")
            if is_match:
                await query.edit_message_text("🎉 *У вас взаимный матч!*", parse_mode="Markdown")
                await notify_match(context, user_id, partner_id)
            else:
                await query.edit_message_text("❤️ Вы поставили лайк. Ищем дальше…")
                add_pending_like(user_id, partner_id, user_name)
                await next_partner(query.message.chat_id, context, user_id)
        else:  # dislike
            await query.edit_message_text("👎 Вы поставили дизлайк. Ищем следующего…")
            await next_partner(query.message.chat_id, context, user_id)

    # ---------- ОТВЕТ НА ПРИХОДЯЩИЙ ЛАЙК ----------
    elif action == "respond":
        resp_type = data[1]          # like / dislike
        from_id = int(data[2])       # кто лайкнул вас
        user_id = query.from_user.id

        if resp_type == "like":
            is_match = add_like(user_id, from_id)
            remove_pending_like(from_id, user_id)
            if is_match:
                await query.edit_message_text("🎉 *У вас взаимный матч!*", parse_mode="Markdown")
                await notify_match(context, user_id, from_id)
            else:
                await query.edit_message_text("❤️ Вы ответили лайком!")
        else:  # dislike
            remove_pending_like(from_id, user_id)
            await query.edit_message_text("👎 Вы отклонили лайк.")
        # Показать следующий полученный лайк
        await show_next_like(query.message, context)

    # ---------- ЖАЛОБА НА ПОЛЬЗОВАТЕЛЯ ----------
    elif action == "report":
        reported_id = int(data[1])
        reporter_id = query.from_user.id
        add_report(reporter_id, reported_id)
        await query.edit_message_text("🚨 Жалоба отправлена. Спасибо!")
        # Уведомляем администратора
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    f"🚨 Новая жалоба на пользователя {reported_id} от {reporter_id}",
                )
            except Exception:
                pass

    # ---------- АКТИВАЦИЯ/ДЕАКТИВАЦИЯ ПРОФИЛЯ (из профиля) ----------
    elif action == "activate_profile":
        activate_user(query.from_user.id)
        await query.edit_message_text("✅ Профиль снова виден в поиске.")
    elif action == "deactivate_profile":
        deactivate_user(query.from_user.id)
        await query.edit_message_text("❌ Профиль скрыт из поиска.")

    # ---------- АДМИН: ОБРАБОТКА ЖАЛОБ И БАНОВ ----------
    elif action == "admin_clear_reports":
        target_id = int(data[1])
        clear_reports_for(target_id)
        await query.edit_message_text(
            f"🗑️ Жалобы на пользователя {target_id} сняты.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin_back_to_reports")
            ]])
        )

    elif action == "admin_ban_5d":
        target_id = int(data[1])
        ban_user_temporarily(target_id, days=5)
        banned_until = get_banned_until(target_id)
        dt = datetime.fromisoformat(banned_until)
        
        # Уведомляем пользователя о бане
        try:
            await context.bot.send_message(
                target_id,
                "⏳ Вы временно ограничены в использовании бота на 5 дней из-за жалоб.\n\n"
                "Это помогает поддерживать порядок в сообществе. Спасибо за понимание."
            )
        except:
            pass
            
        await query.edit_message_text(
            f"⏳ Пользователь {target_id} заблокирован до {dt.strftime('%d.%m %H:%M')}.\n"
            "Жалобы сняты.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔓 Принудительно разблокировать", callback_data=f"admin_unban_{target_id}")
            ]])
        )
        clear_reports_for(target_id)

    elif action == "admin_unban":
        target_id = int(data[1])
        unban_user(target_id)
        
        # Уведомляем пользователя о разблокировке
        try:
            await context.bot.send_message(
                target_id,
                "🔓 Ваша временная блокировка снята. Можете продолжать пользоваться ботом!"
            )
        except:
            pass
            
        await query.edit_message_text(
            f"🔓 Пользователь {target_id} разблокирован вручную.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin_back_to_reports")
            ]])
        )

    elif action == "admin_back_to_reports":
        # Перезапускаем /reports
        await reports_command(update, context)
        await query.delete_message()

    # ---------- ПОМОЩЬ ПО STEAM И ПРОВЕРКА ПОДПИСКИ ──
    elif action == "steam_help":
        help_text = (
            "🎮 *Как найти ваш Steam ID:*\n\n"
            "1. Откройте Steam клиент\n"
            "2. Перейдите в свой профиль\n"
            "3. Скопируйте цифры из адресной строки\n"
            "Пример: https://steamcommunity.com/profiles/76561198000000000\n"
            "Ваш Steam ID: 76561198000000000\n\n"
            "Или:\n"
            "1. Откройте свой профиль в Steam\n"
            "2. Нажмите \"Копировать профиль URL\"\n"
            "3. Вставьте в любом текстовом редакторе\n"
            "4. Извлеките цифры после /profiles/"
        )
        await query.edit_message_text(help_text, parse_mode="Markdown", reply_markup=steam_help_keyboard())
    
    elif action == "back_to_hours":
        await query.edit_message_text(
            "Сколько часов ты откатал в Rust?", 
            reply_markup=steam_keyboard()
        )

    elif action == "check_subscription":
        user_id = query.from_user.id
        if await check_subscription(user_id, context):
            await query.edit_message_text(
                "✅ Спасибо за подписку! Теперь вы можете пользоваться ботом.\n\n"
                "Выберите действие:",
                reply_markup=main_keyboard()
            )
        else:
            await query.edit_message_text(
                f"❌ Вы ещё не подписаны на {REQUIRED_CHANNEL}.\n\n"
                "Подпишитесь и нажмите кнопку ниже, чтобы проверить:",
                reply_markup=subscribe_keyboard(),
            )

    # ---------- НАЧАТЬ ПОИСК ЗАНОВО ----------
    elif action == "restart_search":
        user_id = query.from_user.id
        original_partners = context.user_data.get("original_partners", [])
        partner_data = context.user_data.get("partner_data", {})
        
        if not original_partners:
            await query.edit_message_text(
                "❌ Нет доступных анкет для повторного просмотра.",
                reply_markup=main_keyboard()
            )
            return
            
        # Восстанавливаем очередь
        context.user_data["partner_queue"] = original_partners.copy()
        
        # Показываем первого партнера
        first_partner_id = original_partners[0]
        partner = partner_data.get(first_partner_id)
        if partner:
            await query.edit_message_text("🔄 Начинаем поиск заново...", reply_markup=None)
            await show_partner(query.message.chat_id, context, partner)
        else:
            await query.edit_message_text(
                "❌ Ошибка при повторном запуске поиска.",
                reply_markup=main_keyboard()
            )

    elif action == "main_menu":
        await query.edit_message_text(
            "🏠 Возвращаемся в главное меню...",
            reply_markup=None
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Выберите действие:",
            reply_markup=main_keyboard()
        )

# ── ПАГИНАЦИЯ ЛАЙКОВ ──
async def pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data.split("_")
    direction = data[0]  # prev / next
    idx = int(data[1])

    pending = context.user_data.get("pending_likes", [])
    if not pending:
        return

    if direction == "prev":
        new_idx = max(0, idx - 1)
    else:
        new_idx = min(len(pending) - 1, idx + 1)

    context.user_data["current_like_index"] = new_idx
    await show_next_like(query.message, context)

# ── ЛАЙКИ И ПАГИНАЦИЯ ──
@subscription_required
async def show_likes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Проверка на временный бан
    if is_user_banned(user.id):
        banned_until = get_banned_until(user.id)
        dt = datetime.fromisoformat(banned_until)
        await update.message.reply_text(
            f"⏳ Вы временно ограничены в использовании бота до {dt.strftime('%d.%m %H:%M')}.\n"
            "Спасибо за понимание.",
            reply_markup=ReplyKeyboardMarkup([[
                KeyboardButton("ℹ️ Помощь")
            ]], resize_keyboard=True)
        )
        return
    
    pending = get_pending_likes(user.id)
    if not pending:
        await update.message.reply_text("❌ Пока нет новых лайков.", reply_markup=main_keyboard())
        return
    context.user_data["pending_likes"] = pending
    context.user_data["current_like_index"] = 0
    await show_next_like(update, context)

async def show_next_like(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending_likes", [])
    idx = context.user_data.get("current_like_index", 0)

    if idx >= len(pending):
        await update.message.reply_text("✅ Все лайки просмотрены!", reply_markup=main_keyboard())
        return

    from_id, from_name = pending[idx]
    profile = get_user_profile(from_id)
    if not profile:
        context.user_data["current_like_index"] = idx + 1
        await show_next_like(update, context)
        return

    name, hours, age, bio, username, _, is_verified = profile
    verified_badge = "✅" if is_verified else ""

    kb = [
        [
            InlineKeyboardButton("❤️ Ответить", callback_data=f"respond_like_{from_id}"),
            InlineKeyboardButton("👎 Отклонить", callback_data=f"respond_dislike_{from_id}"),
        ],
        [InlineKeyboardButton("🚨 Пожаловаться", callback_data=f"report_{from_id}")],
        [
            InlineKeyboardButton("⬅️", callback_data=f"prev_{idx}"),
            InlineKeyboardButton(f"{idx+1}/{len(pending)}", callback_data="noop"),
            InlineKeyboardButton("➡️", callback_data=f"next_{idx}"),
        ],
    ]
    markup = InlineKeyboardMarkup(kb)

    await update.message.reply_text(
        f"❤️ *Тебя лайкнул(а) {from_name}! {verified_badge}*\n\n"
        f"👤 *Профиль*\n"
        f"📛 Имя: {name}\n"
        f"⏰ Часы: {hours}\n"
        f"🎂 Возраст: {age}\n"
        f"💬 О себе: {bio}\n"
        f"🔗 Telegram: @{username if username else 'не указано'}",
        parse_mode="Markdown",
        reply_markup=markup,
    )

# ── УВЕДОМЛЕНИЕ О МАТЧАХ ──
async def notify_match(context: ContextTypes.DEFAULT_TYPE, user_a: int, user_b: int):
    a_profile = get_user_profile(user_a)
    b_profile = get_user_profile(user_b)
    if not a_profile or not b_profile:
        return

    _, _, _, _, _, username_a = a_profile
    _, _, _, _, _, username_b = b_profile
    link_a = f"@{username_a}" if username_a else "не указано"
    link_b = f"@{username_b}" if username_b else "не указано"

    try:
        await context.bot.send_message(
            chat_id=user_a,
            text=f"🎉 *Матч!* {link_b} тоже вас лайкнул!",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Failed to send match notification to user {user_a}: {e}")
    
    try:
        await context.bot.send_message(
            chat_id=user_b,
            text=f"🎉 *Матч!* {link_a} тоже вас лайкнул!",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Failed to send match notification to user {user_b}: {e}")

# ── АДМИН: СПИСОК ЖАЛОБ ──
@admin_only
async def reports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает жалобы в виде кнопок"""
    reports = get_reports_summary()
    if not reports:
        await update.message.reply_text("📭 Нет активных жалоб.", reply_markup=main_keyboard())
        return

    for reported_id, cnt in reports:
        profile = get_user_profile(reported_id)
        banned_until = get_banned_until(reported_id)
        is_banned = banned_until is not None

        if profile:
            name, hours, age, bio, username, _, _ = profile
            preview = f"{name}, {hours}ч, {age} лет"
            link = f"@{username}" if username else "не указано"
        else:
            preview = "Пользователь удалён"
            link = "неизвестно"

        status = "🚫 Заблокирован" if is_banned else "🟢 Активен"
        time_left = ""
        if is_banned:
            dt = datetime.fromisoformat(banned_until)
            time_left = f"\n⏱ До разблокировки: {dt.strftime('%d.%m %H:%M')}"

        kb = [
            [InlineKeyboardButton(
                "✅ Снять жалобы и разблокировать" if is_banned else "🛡️ Снять жалобы",
                callback_data=f"admin_clear_reports_{reported_id}"
            )],
            [InlineKeyboardButton(
                "⏳ Забанить на 5 дней", callback_data=f"admin_ban_5d_{reported_id}"
            )],
        ]
        if is_banned:
            kb.append([InlineKeyboardButton(
                "🔓 Принудительно разблокировать", callback_data=f"admin_unban_{reported_id}"
            )])

        markup = InlineKeyboardMarkup(kb)

        await update.message.reply_text(
            f"🛑 *Жалобы*: {cnt}\n"
            f"👤 *Пользователь*: {preview}\n"
            f"🔗 *Telegram*: {link}\n"
            f"📊 *Статус*: {status}{time_left}",
            parse_mode="Markdown",
            reply_markup=markup,
        )

# ── АДМИН: БЛОКИРОВКА/РАЗБЛОКИРОВКА ЧЕРЕЗ ТЕКСТОВЫЕ КОМАНДЫ ──
@admin_only
async def block_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Укажите ID: /block 123456789")
        return
    try:
        tg_id = int(args[0])
        ban_user_temporarily(tg_id, days=5)
        await update.message.reply_text(f"✅ Пользователь {tg_id} заблокирован на 5 дней.")
    except ValueError:
        await update.message.reply_text("⚠️ Неверный ID.")

@admin_only
async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Укажите ID: /unblock 123456789")
        return
    try:
        tg_id = int(args[0])
        unban_user(tg_id)
        await update.message.reply_text(f"✅ Пользователь {tg_id} разблокирован.")
    except ValueError:
        await update.message.reply_text("⚠️ Неверный ID.")

@admin_only
async def blocked_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Получаем всех забаненных пользователей
    with Database() as cur:
        cur.execute(
            "SELECT user_id, banned_until FROM temp_bans WHERE banned_until > ?",
            (datetime.now().isoformat(),)
        )
        rows = cur.fetchall()
    
    if not rows:
        await update.message.reply_text("📭 Список блокировок пуст.", reply_markup=main_keyboard())
        return
    
    text = "🚫 *Заблокированные пользователи*:\n"
    for uid, banned_until in rows:
        dt = datetime.fromisoformat(banned_until)
        text += f"• {uid} (до {dt.strftime('%d.%m %H:%M')})\n"
    
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())

# ── ОБРАБОТЧИК ОШИБОК ──
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Исключение:", exc_info=context.error)

# ───────────────────────────────────────
#   ⚠️ ВАЖНО: ДОБАВЛЯЕМ FLASK-СЕРВЕР ДЛЯ RENDER
# ───────────────────────────────────────
from flask import Flask
import threading
import os

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive! ✅"

def run():
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# Запускаем веб-сервер в отдельном потоке
t = threading.Thread(target=run)
t.daemon = True
t.start()

# ───────────────────────────────────────
#   ЗАПУСК БОТА
# ───────────────────────────────────────
# ───────────────────────────────────────
#   ЗАПУСК БОТА
# ───────────────────────────────────────
def main():
    init_db()
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not TOKEN:
        logger.error("❌ Не найден токен в переменной TELEGRAM_TOKEN")
        return

    # Добавляем задержку, чтобы избежать конфликта с предыдущим экземпляром
    logger.info("⏳ Ожидание 10 секунд перед запуском бота...")
    time.sleep(10)

    app = ApplicationBuilder().token(TOKEN).build()

    # Основные команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # **Админ‑команды**
    app.add_handler(CommandHandler("reports", reports_command))
    app.add_handler(CommandHandler("block", block_cmd))
    app.add_handler(CommandHandler("unblock", unblock_cmd))
    app.add_handler(CommandHandler("blocked", blocked_list_cmd))

    # Обработчики текста и меню
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_and_buttons))

    # Inline‑кнопки (лайк, дизлайк, жалоба, ответы, пагинация, админ)
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(CallbackQueryHandler(pagination_callback, pattern="^(prev|next)_"))

    # Ошибки
    app.add_error_handler(error_handler)

    logger.info("✅ Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
