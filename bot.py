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
from flask import Flask
import threading

# ───────────────────────────────────────
#   НАСТРОЙКИ
# ───────────────────────────────────────
DB_NAME = "users.db"
REQUIRED_CHANNEL = "@rustycave"
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")

# ID администратора – замените на свой
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
        self.last_cleanup = datetime.now()

    def check_limit(self, user_id, action, limit=5, period=60):
        now = datetime.now()
        if (now - self.last_cleanup) > timedelta(minutes=10):
            self.cleanup_old_requests()
            self.last_cleanup = now

        key = f"{user_id}_{action}"
        self.requests.setdefault(key, [])
        self.requests[key] = [t for t in self.requests[key] if now - t < timedelta(seconds=period)]

        if len(self.requests[key]) >= limit:
            return False
        self.requests[key].append(now)
        return True

    def cleanup_old_requests(self):
        now = datetime.now()
        keys_to_remove = []
        for key, timestamps in self.requests.items():
            fresh = [t for t in timestamps if (now - t) < timedelta(hours=1)]
            if fresh:
                self.requests[key] = fresh
            else:
                keys_to_remove.append(key)
        for key in keys_to_remove:
            del self.requests[key]
        logger.info(f"RateLimiter cleanup: removed {len(keys_to_remove)} old keys")

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
    try:
        if isinstance(steam_id, str):
            if not steam_id.isdigit():
                return False
            steam_id = int(steam_id)
        return 76561197960265728 <= steam_id <= 76561197960265728 + 2**32
    except (ValueError, TypeError):
        return False

def validate_hours(hours):
    return isinstance(hours, int) and 0 <= hours <= 20000

def validate_age(age):
    return isinstance(age, int) and 10 <= age <= 100

def validate_bio(bio):
    return isinstance(bio, str) and 5 <= len(bio) <= 500

# ───────────────────────────────────────
#   ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ───────────────────────────────────────
def safe_db_execute(func):
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

def has_profile(user_id: int) -> bool:
    return get_user_profile(user_id) is not None

@safe_db_execute
def get_all_active_partners(cur, exclude_id):
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
    logger.info(f"Like added: {from_id} → {to_id}, match: {match}")
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
    logger.info(f"Report added: {reporter_id} reported {reported_id}")

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
    logger.warning(f"User {user_id} banned for {days} days")

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
        "SELECT banned_until FROM temp_bans WHERE user_id = ?", (user_id,)
    )
    row = cur.fetchone()
    return row[0] if row else None

@safe_db_execute
def clear_reports_for(cur, user_id):
    cur.execute("DELETE FROM reports WHERE reported_id = ?", (user_id,))

@safe_db_execute
def get_reports_summary(cur):
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
    allowed_fields = ["viewed_profiles", "likes_given", "matches"]
    if field not in allowed_fields:
        logger.error(f"Invalid field: {field}")
        return
    cur.execute("INSERT OR IGNORE INTO stats (user_id) VALUES (?)", (user_id,))
    if field == "viewed_profiles":
        cur.execute("UPDATE stats SET viewed_profiles = viewed_profiles + 1 WHERE user_id = ?", (user_id,))
    elif field == "likes_given":
        cur.execute("UPDATE stats SET likes_given = likes_given + 1 WHERE user_id = ?", (user_id,))
    elif field == "matches":
        cur.execute("UPDATE stats SET matches = matches + 1 WHERE user_id = ?", (user_id,))

@safe_db_execute
def get_stats(cur, user_id):
    cur.execute(
        "SELECT viewed_profiles, likes_given, matches FROM stats WHERE user_id = ?",
        (user_id,),
    )
    result = cur.fetchone()
    return result or (0, 0, 0)

def verify_user_steam(tg_id, steam_id):
    try:
        if not validate_steam_id(steam_id):
            return "invalid_id"
        if not STEAM_API_KEY:
            return "no_api_key"

        url = "http://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
        params = {
            "key": STEAM_API_KEY,
            "steamid": steam_id,
            "format": "json",
            "appids_filter[0]": 252490,
        }
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        games = data.get("response", {}).get("games", [])
        for game in games:
            if game["appid"] == 252490:
                hours = game.get("playtime_forever", 0) // 60
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

def admin_main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔍 Найти напарника"), KeyboardButton("🔄 Обновить анкету")],
            [KeyboardButton("👤 Профиль"), KeyboardButton("📊 Статистика")],
            [KeyboardButton("❤️ Посмотреть лайки"), KeyboardButton("🔕 Скрыть анкету")],
            [KeyboardButton("⚙️ Админ-панель")],
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
#   ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ КЛАВИАТУРЫ
# ───────────────────────────────────────
def get_user_keyboard(user_id: int):
    """Возвращает нужную клавиатуру: админ‑или обычную."""
    return admin_main_keyboard() if user_id in ADMIN_IDS else main_keyboard()

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
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user

        # Проверка временного бана
        if is_user_banned(user.id):
            banned_until = get_banned_until(user.id)
            dt = datetime.fromisoformat(banned_until)
            if update.message:
                await update.message.reply_text(
                    f"⏳ Вы временно ограничены в использовании бота до {dt.strftime('%d.%m %H:%M')}.\n"
                    "Спасибо за понимание.",
                    reply_markup=ReplyKeyboardMarkup([[KeyboardButton("ℹ️ Помощь")]], resize_keyboard=True)
                )
            else:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(
                    f"⏳ Вы временно ограничены в использовании бота до {dt.strftime('%d.%m %H:%M')}.\n"
                    "Спасибо за понимание.",
                    reply_markup=ReplyKeyboardMarkup([[KeyboardButton("ℹ️ Помощь")]], resize_keyboard=True)
                )
            return

        # Проверка подписки
        if not await check_subscription(user.id, context):
            text = (
                f"❌ Чтобы пользоваться ботом, подпишитесь на канал:\n"
                f"{REQUIRED_CHANNEL}\n\n"
                "После этого нажмите кнопку ниже, чтобы проверить:"
            )
            if update.message:
                await update.message.reply_text(text, reply_markup=subscribe_keyboard())
            else:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(text, reply_markup=subscribe_keyboard())
            return
        return await func(update, context)
    return wrapper

# ───────────────────────────────────────
#   ДЕКОРАТОР ДЛЯ АДМИНОВ
# ───────────────────────────────────────
def admin_only(func):
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

    if has_profile(user.id):
        welcome_text = f"👋 С возвращением, {user.first_name}!"
    else:
        welcome_text = f"👋 Привет, {user.first_name}! Создайте свою анкету, чтобы начать поиск напарника."

    # Выдаём нужную клавиатуру сразу после старта
    await update.message.reply_text(
        welcome_text,
        reply_markup=get_user_keyboard(user.id)
    )

@subscription_required
async def start_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📛 Как вас зовут?")
    context.user_data["step"] = "name"

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
        reply_markup=get_user_keyboard(update.effective_user.id),
    )

# ── ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ──
@subscription_required
async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user_profile(user.id)
    if not data:
        await update.message.reply_text(
            "❌ У вас нет анкеты. Нажмите «🔄 Обновить анкету».",
            reply_markup=get_user_keyboard(user.id),
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
        reply_markup=get_user_keyboard(update.effective_user.id),
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

    if not limiter.check_limit(user.id, "find_partner", 10, 60):
        await update.message.reply_text("⚠️ Слишком много запросов. Подождите минуту.")
        return

    if not has_profile(user.id):
        await context.bot.send_message(
            chat_id=chat_id,
            text="📝 Сначала создайте анкету. Нажмите «🔄 Обновить анкету».",
            reply_markup=get_user_keyboard(user.id),
        )
        return

    profile = get_user_profile(user.id)
    if not profile:
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Ошибка загрузки профиля.",
            reply_markup=get_user_keyboard(user.id),
        )
        return

    cur_hours, cur_age = profile[1], profile[2]

    partners = get_all_active_partners(user.id)
    if not partners:
        await context.bot.send_message(
            chat_id=chat_id,
            text="😢 Пока нет доступных участников.",
            reply_markup=get_user_keyboard(user.id),
        )
        return

    partners_sorted = sorted(partners, key=lambda p: advanced_similarity(cur_hours, cur_age, p))
    context.user_data["partner_queue"] = [p[0] for p in partners_sorted]
    context.user_data["partner_data"] = {p[0]: p for p in partners_sorted}
    context.user_data["current_partner_index"] = 0
    context.user_data["original_partners"] = [p[0] for p in partners_sorted]

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

# ── ОБРАБОТЧИК ТЕКСТА (сообщения от пользователя) ──
@subscription_required
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    step = context.user_data.get("step")

    # --------------------
    # 1️⃣ Кнопка «Обновить анкету» работает ВСЕГДА
    # --------------------
    if text == "🔄 Обновить анкету":
        await start_profile(update, context)
        return

    # --------------------
    # 2️⃣ Пошаговое создание/обновление анкеты
    # --------------------
    if step == "name":
        context.user_data["name"] = text
        await update.message.reply_text("🎂 Укажите ваш возраст:")
        context.user_data["step"] = "age"
        return

    if step == "age":
        try:
            age = int(text)
            if not validate_age(age):
                raise ValueError
            context.user_data["age"] = age
            await update.message.reply_text("⏰ Как указать часы в Rust?", reply_markup=steam_keyboard())
            context.user_data["step"] = "choose_method"
        except ValueError:
            await update.message.reply_text("Возраст — число от 10 до 100.")
        return

    if step == "hours_manual":
        try:
            hours = int(text)
            if not validate_hours(hours):
                raise ValueError
            context.user_data["hours"] = hours
            await update.message.reply_text("💬 Расскажите немного о себе:")
            context.user_data["step"] = "bio"
        except ValueError:
            await update.message.reply_text("Часы — число от 0 до 20000.")
        return

    if step == "bio":
        if not validate_bio(text):
            await update.message.reply_text("Текст — от 5 до 500 символов.")
            return
        context.user_data["bio"] = text

        save_user(
            user.id,
            context.user_data["name"],
            context.user_data["hours"],
            context.user_data["age"],
            context.user_data["bio"],
            user.username,
            is_active=1,
            is_verified=0,
        )

        await update.message.reply_text(
            "✅ Анкета успешно создана! Теперь вы можете искать напарников.",
            reply_markup=get_user_keyboard(user.id),
        )
        context.user_data.clear()
        return

    # --------------------
    # 3️⃣ Обработка остальных пунктов меню (если профиль уже есть)
    # --------------------
    if has_profile(user.id):
        if text == "🔍 Найти напарника":
            await find_partner(update, context)
        elif text == "👤 Профиль":
            await profile_command(update, context)
        elif text == "📊 Статистика":
            await stats_command(update, context)
        elif text == "❤️ Посмотреть лайки":
            await show_likes_command(update, context)
        elif text == "🔕 Скрыть анкету":
            deactivate_user(user.id)
            await update.message.reply_text(
                "❌ Ваша анкета скрыта из поиска.", reply_markup=get_user_keyboard(user.id)
            )
        elif text == "⚙️ Админ-панель":
            if user.id in ADMIN_IDS:
                await show_admin_panel(update, context)
            else:
                await update.message.reply_text("❌ У вас нет прав для этого действия.")
        else:
            await update.message.reply_text(
                "Не понял. Выберите действие из меню.", reply_markup=get_user_keyboard(user.id)
            )
        return

    # Если профиль отсутствует и пользователь нажал что‑то другое
    await update.message.reply_text(
        "❗ Для начала создайте анкету, нажав «🔄 Обновить анкету».",
        reply_markup=get_user_keyboard(user.id)
    )

# ── ОБРАБОТЧИК STEAM ID ──
@subscription_required
async def handle_steam_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    step = context.user_data.get("step")

    if step == "waiting_steam_id":
        steam_id = update.message.text.strip()
        if not steam_id.isdigit():
            await update.message.reply_text("⚠️ Введите только цифры вашего Steam-ID.")
            return

        result = verify_user_steam(user.id, steam_id)
        if isinstance(result, int):
            context.user_data["hours"] = result
            await update.message.reply_text(f"✅ Получено {result} часов из Steam.\n💬 Теперь расскажите немного о себе:")
            context.user_data["step"] = "bio"
        else:
            await update.message.reply_text("❌ Не удалось получить данные. Введите часы вручную:", reply_markup=steam_keyboard())
            context.user_data["step"] = "hours_manual"

# ── ОБРАБОТЧИК КНОПОК (CallbackQuery) ──
@subscription_required
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    step = context.user_data.get("step")

    # Если профиль нет, но пользователь пытается нажать любые кнопки, кроме разрешённых — выводим подсказку
    if not has_profile(user.id) and step not in {"waiting_steam_id", "choose_method", "hours_manual"}:
        allowed_data = ["link_steam", "manual_hours", "steam_help", "back_to_hours", "check_subscription"]
        if data not in allowed_data:
            await query.edit_message_text(
                "❗ Для начала создайте анкету, нажав «🔄 Обновить анкету».",
                reply_markup=get_user_keyboard(user.id)
            )
            return

    # Кнопки, связанные с вводом часов
    if data == "link_steam":
        await query.edit_message_text(
            "🔗 *Привязка Steam аккаунта*\n\n"
            "Отправьте ваш Steam-ID (только цифры).\n"
            "❓ Как найти ID? — нажмите кнопку ниже.",
            parse_mode="Markdown",
            reply_markup=steam_help_keyboard()
        )
        context.user_data["step"] = "waiting_steam_id"
        return

    if data == "manual_hours":
        await query.edit_message_text(
            "✍️ Введите количество часов в Rust (0–20000):",
            parse_mode="Markdown",
        )
        context.user_data["step"] = "hours_manual"
        return

    if data == "steam_help":
        await query.edit_message_text(
            "🎮 *Как найти ваш Steam ID:*\n\n"
            "1️⃣ Откройте Steam → ваш профиль.\n"
            "2️⃣ Скопируйте цифры из ссылки:\n"
            "`https://steamcommunity.com/profiles/76561198000000000`\n"
            "Ваш ID — числа после */profiles/*.\n\n"
            "⬅️ Вернуться",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_hours")]])
        )
        return

    if data == "back_to_hours":
        await query.edit_message_text(
            "⏰ Как указать часы в Rust?",
            reply_markup=steam_keyboard()
        )
        context.user_data["step"] = "choose_method"
        return

    # Остальные действия делегируем в общий обработчик
    await handle_callback(update, context)

# ── ПАГИНАЦИЯ ЛАЙКОВ ──
async def pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data.split("_")
    direction = data[0]          # prev / next
    idx = int(data[1])           # текущий индекс

    pending = context.user_data.get("pending_likes", [])
    if not pending:
        return

    new_idx = max(0, idx - 1) if direction == "prev" else min(len(pending) - 1, idx + 1)
    context.user_data["current_like_index"] = new_idx
    await show_next_like(query.message, context)

# ── ЛАЙКИ И ПАГИНАЦИЯ ──
@subscription_required
async def show_likes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    pending = get_pending_likes(user.id)
    if not pending:
        await update.message.reply_text("❌ Пока нет новых лайков.", reply_markup=get_user_keyboard(user.id))
        return
    context.user_data["pending_likes"] = pending
    context.user_data["current_like_index"] = 0
    await show_next_like(update, context)

async def show_next_like(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending_likes", [])
    idx = context.user_data.get("current_like_index", 0)

    if idx >= len(pending):
        await update.message.reply_text("✅ Все лайки просмотрены!", reply_markup=get_user_keyboard(update.effective_user.id))
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
        await context.bot.send_message(chat_id=user_a, text=f"🎉 *Матч!* {link_b} тоже вас лайкнул!", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to send match notification to user {user_a}: {e}")

    try:
        await context.bot.send_message(chat_id=user_b, text=f"🎉 *Матч!* {link_a} тоже вас лайкнул!", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to send match notification to user {user_b}: {e}")

# ── АДМИН-ПАНЕЛЬ ──
async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Посмотреть жалобы", callback_data="admin_action_reports")],
            [InlineKeyboardButton("🚫 Заблокировать пользователя", callback_data="admin_action_block")],
            [InlineKeyboardButton("🔓 Разблокировать пользователя", callback_data="admin_action_unblock")],
            [InlineKeyboardButton("📋 Список заблокированных", callback_data="admin_action_blocked_list")],
        ]
    )
    await update.message.reply_text("⚙️ *Админ-панель:*", parse_mode="Markdown", reply_markup=keyboard)

# ── ОСНОВНОЙ ОБРАБОТЧИК CALLBACK'ОВ ──
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

        if action == "like":
            is_match = add_like(user_id, partner_id)
            update_stat(user_id, "likes_given")
            if is_match:
                await query.edit_message_text("🎉 *У вас взаимный матч!*", parse_mode="Markdown")
                await notify_match(context, user_id, partner_id)
            else:
                await query.edit_message_text("❤️ Вы поставили лайк. Ищем дальше…")
                add_pending_like(user_id, partner_id, query.from_user.first_name)
                await next_partner(query.message.chat_id, context, user_id)
        else:
            await query.edit_message_text("👎 Вы поставили дизлайк. Ищем следующего…")
            await next_partner(query.message.chat_id, context, user_id)

    # ---------- ОТВЕТ НА ПРИХОДЯЩИЙ ЛАЙК ----------
    elif action == "respond":
        resp_type = data[1]
        from_id = int(data[2])
        user_id = query.from_user.id

        if resp_type == "like":
            is_match = add_like(user_id, from_id)
            remove_pending_like(from_id, user_id)
            if is_match:
                await query.edit_message_text("🎉 *У вас взаимный матч!*", parse_mode="Markdown")
                await notify_match(context, user_id, from_id)
            else:
                await query.edit_message_text("❤️ Вы ответили лайком!")
        else:
            remove_pending_like(from_id, user_id)
            await query.edit_message_text("👎 Вы отклонили лайк.")
        await show_next_like(query.message, context)

    # ---------- ЖАЛОБА ----------
    elif action == "report":
        reported_id = int(data[1])
        reporter_id = query.from_user.id
        add_report(reporter_id, reported_id)
        await query.edit_message_text("🚨 Жалоба отправлена. Спасибо!")
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(admin_id, f"🚨 Новая жалоба на пользователя {reported_id} от {reporter_id}")
            except Exception:
                pass

    # ---------- АКТИВАЦИЯ/ДЕАКТИВАЦИЯ ПРОФИЛЯ ----------
    elif action == "activate_profile":
        activate_user(query.from_user.id)
        await query.edit_message_text("✅ Профиль снова виден в поиске.")
    elif action == "deactivate_profile":
        deactivate_user(query.from_user.id)
        await query.edit_message_text("❌ Профиль скрыт из поиска.")

    # ---------- АДМИН: ЖАЛОБЫ И БАНЫ ----------
    elif action == "admin" and data[1] == "action":
        admin_action = data[2]

        if admin_action == "reports":
            await reports_command(update, context)
            await query.delete_message()
        elif admin_action == "block":
            await query.edit_message_text("🚫 Введите ID пользователя для блокировки в формате:\n`/block 123456789`", parse_mode="Markdown")
        elif admin_action == "unblock":
            await query.edit_message_text("🔓 Введите ID пользователя для разблокировки в формате:\n`/unblock 123456789`", parse_mode="Markdown")
        elif admin_action == "blocked_list":
            await blocked_list_cmd(update, context)
            await query.delete_message()

    elif action == "admin_clear_reports":
        target_id = int(data[1])
        clear_reports_for(target_id)
        await query.edit_message_text(
            f"🗑️ Жалобы на пользователя {target_id} сняты.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_back_to_reports")]])
        )

    elif action == "admin_ban_5d":
        target_id = int(data[1])
        ban_user_temporarily(target_id, days=5)
        banned_until = get_banned_until(target_id)
        dt = datetime.fromisoformat(banned_until)

        try:
            await context.bot.send_message(target_id, "⏳ Вы временно ограничены в использовании бота на 5 дней.")
        except:
            pass

        await query.edit_message_text(
            f"⏳ Пользователь {target_id} заблокирован до {dt.strftime('%d.%m %H:%M')}.\nЖалобы сняты.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔓 Принудительно разблокировать", callback_data=f"admin_unban_{target_id}")]])
        )
        clear_reports_for(target_id)

    elif action == "admin_unban":
        target_id = int(data[1])
        unban_user(target_id)
        try:
            await context.bot.send_message(target_id, "🔓 Ваша блокировка снята.")
        except:
            pass
        await query.edit_message_text(
            f"🔓 Пользователь {target_id} разблокирован вручную.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_back_to_reports")]])
        )

    elif action == "admin_back_to_reports":
        await reports_command(update, context)
        await query.delete_message()

    # ---------- ПРОВЕРКА ПОДПИСКИ ----------
    elif action == "check_subscription":
        user_id = query.from_user.id
        if await check_subscription(user_id, context):
            await query.edit_message_text(
                "✅ Спасибо за подписку! Теперь вы можете пользоваться ботом.\n\nВыберите действие:",
                reply_markup=get_user_keyboard(user_id)
            )
        else:
            await query.edit_message_text(
                f"❌ Вы ещё не подписаны на {REQUIRED_CHANNEL}.\n\nПодпишитесь и нажмите кнопку ниже, чтобы проверить:",
                reply_markup=subscribe_keyboard(),
            )

    # ---------- ПЕРЕЗАПУСК ПОИСКА ----------
    elif action == "restart_search":
        user_id = query.from_user.id
        original_partners = context.user_data.get("original_partners", [])
        partner_data = context.user_data.get("partner_data", {})

        if not original_partners:
            await query.edit_message_text("❌ Нет доступных анкет для повторного просмотра.", reply_markup=get_user_keyboard(user_id))
            return

        context.user_data["partner_queue"] = original_partners.copy()
        first_partner_id = original_partners[0]
        partner = partner_data.get(first_partner_id)
        if partner:
            await query.edit_message_text("🔄 Начинаем поиск заново...", reply_markup=None)
            await show_partner(query.message.chat_id, context, partner)
        else:
            await query.edit_message_text("❌ Ошибка при повторном запуске поиска.", reply_markup=get_user_keyboard(user_id))

    elif action == "main_menu":
        await query.edit_message_text("🏠 Возвращаемся в главное меню...", reply_markup=None)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Выберите действие:",
            reply_markup=get_user_keyboard(query.from_user.id)
        )

# ── АДМИН: СПИСОК ЖАЛОБ ──
@admin_only
async def reports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reports = get_reports_summary()
    if not reports:
        await update.message.reply_text("📭 Нет активных жалоб.", reply_markup=get_user_keyboard(update.effective_user.id))
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
            [InlineKeyboardButton("⏳ Забанить на 5 дней", callback_data=f"admin_ban_5d_{reported_id}")],
        ]
        if is_banned:
            kb.append([InlineKeyboardButton("🔓 Принудительно разблокировать", callback_data=f"admin_unban_{reported_id}")])

        markup = InlineKeyboardMarkup(kb)

        await update.message.reply_text(
            f"🛑 *Жалобы*: {cnt}\n"
            f"👤 *Пользователь*: {preview}\n"
            f"🔗 *Telegram*: {link}\n"
            f"📊 *Статус*: {status}{time_left}",
            parse_mode="Markdown",
            reply_markup=markup,
        )

# ── АДМИН: БЛОКИРОВКА/РАЗБЛОКИРОВКА ──
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
    with Database() as cur:
        cur.execute(
            "SELECT user_id, banned_until FROM temp_bans WHERE banned_until > ?",
            (datetime.now().isoformat(),)
        )
        rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("📭 Список блокировок пуст.", reply_markup=get_user_keyboard(update.effective_user.id))
        return

    text = "🚫 *Заблокированные пользователи*:\n"
    for uid, banned_until in rows:
        dt = datetime.fromisoformat(banned_until)
        text += f"• {uid} (до {dt.strftime('%d.%m %H:%M')})\n"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_user_keyboard(update.effective_user.id))

# ── ОБРАБОТЧИК ОШИБОК ──
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Исключение при обработке обновления:", exc_info=context.error)

# ───────────────────────────────────────
#   FLASK‑СЕРВЕР (для Render)
# ───────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot is alive! ✅"

def run():
    port = int(os.getenv("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

# ───────────────────────────────────────
#   ЗАПУСК БОТА
# ───────────────────────────────────────
def main():
    init_db()
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not TOKEN:
        logger.error("❌ Не найден токен в переменной TELEGRAM_TOKEN")
        return
    if not STEAM_API_KEY:
        logger.error("❌ Не задан STEAM_API_KEY")
        return

    # Запускаем Flask‑сервер в отдельном потоке
    t = threading.Thread(target=run)
    t.daemon = True
    t.start()

    logger.info("⏳ Ожидание 10 секунд перед запуском бота...")
    time.sleep(10)

    app = ApplicationBuilder().token(TOKEN).build()

    # Удаляем возможный вебхук, чтобы long‑polling работал без конфликта
    app.bot.delete_webhook(drop_pending_updates=True)
    logger.info("🧹 Вебхук удалён, запускаем polling…")

    # Основные команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # Админ‑команды
    app.add_handler(CommandHandler("reports", reports_command))
    app.add_handler(CommandHandler("block", block_cmd))
    app.add_handler(CommandHandler("unblock", unblock_cmd))
    app.add_handler(CommandHandler("blocked", blocked_list_cmd))

    # Обработчики текста и callback‑кнопок
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_steam_id))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(CallbackQueryHandler(handle_callback,
                                         pattern="^(like|dislike|respond|report|activate_profile|deactivate_profile|check_subscription|restart_search|main_menu|admin_.*)"))
    app.add_handler(CallbackQueryHandler(pagination_callback, pattern="^(prev|next)_"))

    # Ошибки
    app.add_error_handler(error_handler)

    logger.info("✅ Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
