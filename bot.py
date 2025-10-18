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

# ───────────────────────────────────────
#   ЛОГИРОВАНИЕ
# ───────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ───────────────────────────────────────
#   БАЗА ДАННЫХ
# ───────────────────────────────────────
DB_NAME = "users.db"


def init_db() -> None:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            name TEXT,
            hours INTEGER,
            age INTEGER,
            bio TEXT,
            username TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
    conn.commit()
    conn.close()


def save_user(tg_id, name, hours, age, bio, username):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO users
        (telegram_id, name, hours, age, bio, username)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (tg_id, name, hours, age, bio, username),
    )
    conn.commit()
    conn.close()


def get_user_profile(tg_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT name, hours, age, bio, username
        FROM users
        WHERE telegram_id = ?
        """,
        (tg_id,),
    )
    result = cur.fetchone()
    conn.close()
    return result


def get_all_partners(exclude_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT telegram_id, name, hours, age, bio, username
        FROM users
        WHERE telegram_id != ?
        """,
        (exclude_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def is_profile_complete(tg_id):
    return get_user_profile(tg_id) is not None


def add_like(from_id, to_id):
    """Возвращает True, если получен взаимный лайк (матч)."""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO likes (from_id, to_id) VALUES (?, ?)",
        (from_id, to_id),
    )
    cur.execute(
        "SELECT 1 FROM likes WHERE from_id = ? AND to_id = ?", (to_id, from_id)
    )
    match = cur.fetchone() is not None
    conn.commit()
    conn.close()
    return match


# ───────────────────────────────────────
#   КЛАВИАТУРА (команды)
# ───────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔍 Найти напарника"), KeyboardButton("🔄 Обновить анкету")],
            [KeyboardButton("👤 Профиль"), KeyboardButton("ℹ️ Помощь")],
        ],
        resize_keyboard=True,
    )


# ───────────────────────────────────────
#   ХЭНДЛЕРЫ (обработчики команд и кнопок)
# ───────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}! Я помогу тебе найти напарника по Rust.",
        reply_markup=main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Помощь*\n\n"
        "• 🔍 *Найти напарника* — начать поиск.\n"
        "• 🔄 *Обновить анкету* — изменить свои данные.\n"
        "• 👤 *Профиль* — посмотреть свою анкету.\n\n"
        "Просто нажимай на кнопки ниже!",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


async def start_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск создания / обновления анкеты."""
    await update.message.reply_text("📝 Сколько часов ты уже откатал в Rust?")
    context.user_data["step"] = "hours"


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user_profile(user.id)
    if not data:
        await update.message.reply_text(
            "❌ У тебя ещё нет анкеты. Нажми «🔄 Обновить анкету».",
            reply_markup=main_keyboard(),
        )
        return

    name, hours, age, bio, username = data
    await update.message.reply_text(
        f"📋 *Твой профиль*\n\n"
        f"📛 Имя: {name}\n"
        f"⏰ Часы в Rust: {hours}\n"
        f"🎂 Возраст: {age}\n"
        f"💬 О себе: {bio}\n"
        f"🔗 Telegram: @{username if username else 'не указано'}",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )

# ---------- ЕДИНЫЙ ОБРАБОТЧИК ТЕКСТА И КНОПОК ----------
async def handle_text_and_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Эта функция обрабатывает ВСЁ: и кнопки главного меню, и шаги заполнения анкеты.
    Это самый надёжный способ избежать конфликтов.
    """
    text = update.message.text.strip()
    step = context.user_data.get("step")

    # --- 1. Обрабатываем шаги анкеты, если они активны ---
    if step == "hours":
        try:
            hours = int(text)
            if hours < 0:
                raise ValueError()
            context.user_data["hours"] = hours
            await update.message.reply_text("📅 Укажи свой возраст:")
            context.user_data["step"] = "age"
        except ValueError:
            await update.message.reply_text("Пожалуйста, введи число (например: 150).")
        return

    if step == "age":
        try:
            age = int(text)
            if not (10 <= age <= 100):
                raise ValueError()
            context.user_data["age"] = age
            await update.message.reply_text("💬 Напиши что-нибудь о себе...")
            context.user_data["step"] = "bio"
        except ValueError:
            await update.message.reply_text("Пожалуйста, введи корректный возраст (например: 27).")
        return

    if step == "bio":
        context.user_data["bio"] = text
        user = update.effective_user
        data = context.user_data

        save_user(
            user.id,
            user.first_name,
            data["hours"],
            data["age"],
            data["bio"],
            user.username,
        )
        await update.message.reply_text(
            "✅ Анкета сохранена! Теперь можно искать напарников.",
            reply_markup=main_keyboard(),
        )
        context.user_data["step"] = None
        return

    # --- 2. Если не в анкете, обрабатываем нажатия кнопок меню ---
    if text == "🔍 Найти напарника":
        await find_partner(update, context)
    elif text == "🔄 Обновить анкету":
        await start_profile(update, context)
    elif text == "👤 Профиль":
        await profile_command(update, context)
    elif text == "ℹ️ Помощь":
        await help_command(update, context)
    else:
        # Если пользователь ввёл что-то левое
        await update.message.reply_text(
            "Не понял. Пожалуйста, выбери действие из меню ниже.",
            reply_markup=main_keyboard(),
        )

# ---------- ПОИСК ----------
def similarity(current_hours, current_age, partner):
    _, _, hours, age, _, _ = partner
    return abs(hours - current_hours) + abs(age - current_age)


async def find_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not is_profile_complete(user.id):
        await context.bot.send_message(
            chat_id=chat_id,
            text="📝 Сначала нужно создать анкету. Нажми «🔄 Обновить анкету».",
        )
        return

    profile = get_user_profile(user.id)
    cur_hours, cur_age = profile[1], profile[2]

    partners = get_all_partners(user.id)
    if not partners:
        await context.bot.send_message(chat_id=chat_id, text="😢 Пока нет других участников.")
        return

    partners_sorted = sorted(partners, key=lambda p: similarity(cur_hours, cur_age, p))
    context.user_data["partner_queue"] = [p[0] for p in partners_sorted]
    context.user_data["partner_data"] = {p[0]: p for p in partners_sorted}

    await show_partner(chat_id, context, partners_sorted[0])


async def show_partner(chat_id, context: ContextTypes.DEFAULT_TYPE, partner):
    partner_id, name, hours, age, bio, username = partner
    context.user_data["current_partner_id"] = partner_id

    kb = [
        [
            InlineKeyboardButton("❤️ Лайк", callback_data=f"like_{partner_id}"),
            InlineKeyboardButton("👎 Дизлайк", callback_data=f"dislike_{partner_id}"),
        ]
    ]
    markup = InlineKeyboardMarkup(kb)

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"👤 *Найден напарник*\n\n"
            f"📛 Имя: {name}\n"
            f"⏰ Часы в Rust: {hours}\n"
            f"🎂 Возраст: {age}\n"
            f"💬 О себе: {bio}"
        ),
        parse_mode="Markdown",
        reply_markup=markup,
    )

# ---------- ОБРАБОТКА ЛАЙКОВ (ИСПРАВЛЕНО) ----------
async def handle_like_dislike(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Надёжный обработчик нажатий на inline-кнопки (Лайк/Дизлайк).
    Теперь он всегда правильно получает данные, даже если библиотека ведёт себя странно.
    """
    query = update.callback_query
    if not query:
        return # На всякий случай, если callback_query не пришёл

    await query.answer() # Быстро отвечаем Telegram, чтобы кнопка не "зависала"

    data = query.data.split("_")
    action = data[0]
    partner_id = int(data[1])
    user_id = query.from_user.id
    user_name = query.from_user.first_name

    if action == "like":
        is_match = add_like(user_id, partner_id)
        if is_match:
            await query.edit_message_text("🎉 *У вас взаимный матч!*", parse_mode="Markdown")
            await notify_match(context, user_id, partner_id)
        else:
            await query.edit_message_text("❤️ Ты поставил лайк. Ищем дальше…")
            await notify_like(context, partner_id, user_name)
            await next_partner(query.message.chat_id, context, user_id)

    elif action == "dislike":
        await query.edit_message_text("👎 Ты поставил дизлайк. Ищем следующего…")
        await next_partner(query.message.chat_id, context, user_id)


async def next_partner(chat_id, context: ContextTypes.DEFAULT_TYPE, user_id):
    queue = context.user_data.get("partner_queue", [])
    if not queue:
        await context.bot.send_message(chat_id=chat_id, text="🎉 Ты просмотрел всех!")
        return

    next_id = queue.pop(0)
    context.user_data["partner_queue"] = queue
    partner = context.user_data.get("partner_data", {}).get(next_id)
    if partner:
        await show_partner(chat_id, context, partner)


# ---------- УВЕДОМЛЕНИЯ ----------
async def notify_like(context: ContextTypes.DEFAULT_TYPE, to_user_id, liker_name):
    try:
        await context.bot.send_message(
            chat_id=to_user_id,
            text=f"❤️ Твою анкету лайкнул(а) {liker_name}!",
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление о лайке {to_user_id}: {e}")


async def notify_match(context: ContextTypes.DEFAULT_TYPE, user_a, user_b):
    profile_a = get_user_profile(user_a)
    profile_b = get_user_profile(user_b)
    if not profile_a or not profile_b:
        return

    name_a, _, _, _, username_a = profile_a
    name_b, _, _, _, username_b = profile_b
    link_a = f"@{username_a}" if username_a else "неизвестен"
    link_b = f"@{username_b}" if username_b else "неизвестен"

    try:
        await context.bot.send_message(
            chat_id=user_a,
            text=f"🎉 *Матч!* 🎉\n\n🔥 {name_b}\n💬 {link_b}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"Ошибка отправки матча {user_a}: {e}")

    try:
        await context.bot.send_message(
            chat_id=user_b,
            text=f"🎉 *Матч!* 🎉\n\n🔥 {name_a}\n💬 {link_a}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"Ошибка отправки матча {user_b}: {e}")


# ---------- ОБРАБОТЧИК ОШИБОК ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Исключение во время обработки обновления:", exc_info=context.error)


# ───────────────────────────────────────
#   MAIN (ЗАПУСК БОТА)
# ───────────────────────────────────────
def main():
    init_db()
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not TOKEN:
        logger.error("❌ TELEGRAM_TOKEN не найден!")
        return

    app = ApplicationBuilder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # Единый обработчик для всего текста и кнопок ReplyKeyboard
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_and_buttons))

    # Обработчик для inline-кнопок (Лайк/Дизлайк)
    app.add_handler(CallbackQueryHandler(handle_like_dislike))

    # Обработчик ошибок
    app.add_error_handler(error_handler)

    logger.info("✅ Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
