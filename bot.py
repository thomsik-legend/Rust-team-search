import os
import logging
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    
    # Пользователи
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            name TEXT,
            bio TEXT,
            level TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Лайки
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS likes (
            from_id INTEGER,
            to_id INTEGER,
            PRIMARY KEY (from_id, to_id)
        )
    ''')
    
    conn.commit()
    conn.close()

# Сохранение пользователя
def save_user(telegram_id, name, bio, level):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users (telegram_id, name, bio, level)
        VALUES (?, ?, ?, ?)
    ''', (telegram_id, name, bio, level))
    conn.commit()
    conn.close()

# Получить случайного пользователя
def get_random_user(exclude_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT telegram_id, name, bio, level FROM users
        WHERE telegram_id != ?
        ORDER BY RANDOM() LIMIT 1
    ''', (exclude_id,))
    user = cursor.fetchone()
    conn.close()
    return user

# Проверка на матч
def is_match(user_id, partner_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    
    # Проверяем, лайкнул ли partner user_id
    cursor.execute('SELECT 1 FROM likes WHERE from_id = ? AND to_id = ?', (partner_id, user_id))
    liked_back = cursor.fetchone() is not None
    
    conn.close()
    return liked_back

# Сохраняем лайк
def add_like(from_id, to_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO likes (from_id, to_id) VALUES (?, ?)', (from_id, to_id))
    conn.commit()
    conn.close()

# Получить профиль по ID
def get_user_profile(telegram_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT name, bio, level FROM users WHERE telegram_id = ?', (telegram_id,))
    user = cursor.fetchone()
    conn.close()
    return user

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}! Я помогу найти тебе напарника по Rust.\n\n"
        "Напиши, кто ты (например: изучаю Rust, ищу напарника для проектов):"
    )
    context.user_data['step'] = 'bio'

# Обработка сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    step = context.user_data.get('step')

    if step == 'bio':
        context.user_data['bio'] = text
        await update.message.reply_text(
            "🎯 Укажи свой уровень знаний Rust:\n"
            "• Новичок\n"
            "• Средний\n"
            "• Эксперт"
        )
        context.user_data['step'] = 'level'
    
    elif step == 'level':
        context.user_data['level'] = text
        save_user(user.id, user.first_name, text, context.user_data['bio'])
        
        await update.message.reply_text(
            "✅ Отлично! Ты зарегистрирован!\n\n"
            "Команды:\n"
            "/find - найти напарника\n"
            "/profile - посмотреть профиль"
        )
        context.user_data['step'] = None

# Поиск напарника
async def find_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    partner = get_random_user(user.id)
    
    if not partner:
        await update.message.reply_text("😢 Пока нет других участников")
        return

    context.user_data['current_partner_id'] = partner[0]
    
    keyboard = [
        [
            InlineKeyboardButton("❤️ Лайк", callback_data=f"like_{partner[0]}"),
            InlineKeyboardButton("👎 Дизлайк", callback_data=f"dislike_{partner[0]}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"👤 Найден напарник:\n\n"
        f"📛 Имя: {partner[1]}\n"
        f"📖 О себе: {partner[2]}\n"
        f"🔧 Уровень: {partner[3]}",
        reply_markup=reply_markup
    )

# Обработка кнопок
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data.split('_')
    action = data[0]
    partner_id = int(data[1])
    user_id = query.from_user.id
    
    if action == 'like':
        add_like(user_id, partner_id)
        
        # Проверка на матч
        if is_match(user_id, partner_id):
            partner_profile = get_user_profile(partner_id)
            if partner_profile:
                await query.edit_message_text(
                    f"🎉 УРА! У вас МАТЧ!\n\n"
                    f"🔥 Напарник: {partner_profile[0]}\n"
                    f"💬 {partner_profile[1]}\n"
                    f"🎯 Уровень: {partner_profile[2]}\n\n"
                    f"🔗 Написать ему: t.me/{query.from_user.username or 'username_not_set'}"
                )
            else:
                await query.edit_message_text("🎉 У вас матч! Но профиль недоступен.")
        else:
            await query.edit_message_text("❤️ Ты поставил лайк! Ищем дальше...")
            await find_partner_after_action(query, context, user_id)
    
    elif action == 'dislike':
        await query.edit_message_text("👎 Ты поставил дизлайк. Ищем следующего...")
        await find_partner_after_action(query, context, user_id)

# Автоматический поиск после действия
async def find_partner_after_action(query, context, user_id):
    partner = get_random_user(user_id)
    if not partner:
        await context.bot.send_message(chat_id=user_id, text="😢 Больше нет участников")
        return
    
    keyboard = [
        [
            InlineKeyboardButton("❤️ Лайк", callback_data=f"like_{partner[0]}"),
            InlineKeyboardButton("👎 Дизлайк", callback_data=f"dislike_{partner[0]}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(
        chat_id=user_id,
        text=f"👤 Следующий напарник:\n\n"
             f"📛 Имя: {partner[1]}\n"
             f"📖 О себе: {partner[2]}\n"
             f"🔧 Уровень: {partner[3]}",
        reply_markup=reply_markup
    )

# Просмотр профиля
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    profile_data = get_user_profile(user.id)
    
    if profile_data:
        await update.message.reply_text(
            f"📋 Твой профиль:\n\n"
            f"📛 Имя: {profile_data[0]}\n"
            f
