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
            hours INTEGER,
            age INTEGER,
            bio TEXT,
            username TEXT,
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
def save_user(telegram_id, name, hours, age, bio, username):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users (telegram_id, name, hours, age, bio, username)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (telegram_id, name, hours, age, bio, username))
    conn.commit()
    conn.close()

# Проверка, заполнена ли анкета
def is_profile_complete(telegram_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM users WHERE telegram_id = ?', (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

# Получить профиль
def get_user_profile(telegram_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT name, hours, age, bio, username FROM users WHERE telegram_id = ?', (telegram_id,))
    user = cursor.fetchone()
    conn.close()
    return user

# Получить всех пользователей (кроме текущего)
def get_all_partners(exclude_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT telegram_id, name, hours, age, bio, username 
        FROM users 
        WHERE telegram_id != ?
    ''', (exclude_id,))
    partners = cursor.fetchall()
    conn.close()
    return partners

# Проверка на матч
def is_match_in_db(user_id, partner_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM likes WHERE from_id = ? AND to_id = ?', (partner_id, user_id))
    liked_back = cursor.fetchone() is not None
    conn.close()
    return liked_back

# Сохраняем лайк и проверяем матч
def add_like(from_id, to_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO likes (from_id, to_id) VALUES (?, ?)', (from_id, to_id))
    
    # Проверяем, есть ли взаимный лайк
    cursor.execute('SELECT 1 FROM likes WHERE from_id = ? AND to_id = ?', (to_id, from_id))
    is_match_found = cursor.fetchone() is not None
    
    conn.commit()
    conn.close()
    return is_match_found

# Отправка уведомления о лайке
async def notify_user_about_like(context: ContextTypes.DEFAULT_TYPE, to_user_id, liker_name):
    try:
        await context.bot.send_message(
            chat_id=to_user_id,
            text=f"❤️ Кто-то поставил лайк твоей анкете!\n"
                 f"Это {liker_name} — посмотри и ты!"
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление {to_user_id}: {e}")

# Отправка уведомления о матче
async def notify_users_about_match(context: ContextTypes.DEFAULT_TYPE, user_id, partner_id):
    user_profile = get_user_profile(user_id)
    partner_profile = get_user_profile(partner_id)
    
    if not user_profile or not partner_profile:
        return

    name1, _, _, _, username1 = user_profile
    name2, _, _, _, username2 = partner_profile

    link1 = f"@{username1}" if username1 else "неизвестен"
    link2 = f"@{username2}" if username2 else "неизвестен"

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🎉 УРА! У вас МАТЧ!\n\n"
                 f"🔥 Напарник: {name2}\n"
                 f"💬 {link2}\n\n"
                 f"Напишите друг другу!"
        )
    except Exception as e:
        logger.warning(f"Ошибка при отправке матча {user_id}: {e}")

    try:
        await context.bot.send_message(
            chat_id=partner_id,
            text=f"🎉 УРА! У вас МАТЧ!\n\n"
                 f"🔥 Напарник: {name1}\n"
                 f"💬 {link1}\n\n"
                 f"Напишите другу!"
        )
    except Exception as e:
        logger.warning(f"Ошибка при отправке матча {partner_id}: {e}")

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keyboard = [[InlineKeyboardButton("🔍 Найти напарника по Rust", callback_data="find_partner")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}! Я помогу найти тебе напарника по Rust.",
        reply_markup=reply_markup
    )

# Команда /update — обновить анкету
async def update_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔄 Начинаем обновление анкеты!\n\n"
        "Сколько часов ты уже откатал в Rust?"
    )
    context.user_data['step'] = 'update_hours'

# Обработка кнопки
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "find_partner":
        if not is_profile_complete(query.from_user.id):
            await query.message.reply_text(
                "📝 Сначала нужно создать анкету!\n\n"
                "Сколько часов ты уже откатал в Rust?"
            )
            context.user_data['step'] = 'hours'
        else:
            await find_partner(query, context)
    
    elif query.data.startswith("like_") or query.data.startswith("dislike_"):
        await handle_like_dislike(query, context)

# Обработка сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    step = context.user_data.get('step')

    # === Основная регистрация ===
    if step == 'hours':
        try:
            hours = int(text)
            if hours < 0:
                raise ValueError()
            context.user_data['hours'] = hours
            await update.message.reply_text("📅 Укажи свой возраст:")
            context.user_data['step'] = 'age'
        except:
            await update.message.reply_text("Введите число (например: 50)")
    
    elif step == 'age':
        try:
            age = int(text)
            if age < 10 or age > 100:
                raise ValueError()
            context.user_data['age'] = age
            await update.message.reply_text("💬 Напиши что-нибудь о себе (о своих целях, стиле игры и т.д.):")
            context.user_data['step'] = 'bio'
        except:
            await update.message.reply_text("Введите возраст (например: 25)")
    
    elif step == 'bio':
        context.user_data['bio'] = text
        user_data = context.user_data
        
        save_user(
            user.id, user.first_name,
            user_data['hours'], user_data['age'],
            user_data['bio'], user.username
        )
        
        await update.message.reply_text("✅ Анкета готова! Теперь ты можешь искать напарников.")
        context.user_data['step'] = None
        
        keyboard = [[InlineKeyboardButton("🔍 Найти напарника", callback_data="find_partner")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Готов начать?", reply_markup=reply_markup)

    # === Обновление анкеты ===
    elif step == 'update_hours':
        try:
            hours = int(text)
            if hours < 0:
                raise ValueError()
            context.user_data['update_hours'] = hours
            await update.message.reply_text("📅 Укажи свой возраст:")
            context.user_data['step'] = 'update_age'
        except:
            await update.message.reply_text("Введите число (например: 50)")
    
    elif step == 'update_age':
        try:
            age = int(text)
            if age < 10 or age > 100:
                raise ValueError()
            context.user_data['update_age'] = age
            await update.message.reply_text("💬 Расскажи что-нибудь о себе:")
            context.user_data['step'] = 'update_bio'
        except:
            await update.message.reply_text("Введите возраст (например: 25)")
    
    elif step == 'update_bio':
        context.user_data['update_bio'] = text
        data = context.user_data
        
        save_user(
            user.id, user.first_name,
            data['update_hours'], data['update_age'],
            data['update_bio'], user.username
        )
        
        await update.message.reply_text("✅ Анкета успешно обновлена!")
        context.user_data['step'] = None

# Поиск напарника (с приоритетом похожих)
async def find_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Получаем профиль текущего пользователя
    user_profile = get_user_profile(user.id)
    if not user_profile:
        await update.effective_message.reply_text("❌ Сначала заполните анкету!")
        return

    current_hours = user_profile[2]
    current_age = user_profile[3]

    # Получаем всех других
    all_partners = get_all_partners(user.id)
    if not all_partners:
        await update.effective_message.reply_text("😢 Пока нет других участников")
        return

    # Сортируем по близости
    def similarity(partner):
        _, _, hours, age, _, _ = partner
        hours_diff = abs(hours - current_hours)
        age_diff = abs(age - current_age)
        return hours_diff + age_diff  # чем меньше — тем ближе

    sorted_partners = sorted(all_partners, key=similarity)

    # Сохраняем очередь
    context.user_data['partner_queue'] = [p[0] for p in sorted_partners]
    context.user_data['current_partner_list'] = {p[0]: p for p in sorted_partners}

    # Показываем первого
    await show_partner(update, context, sorted_partners[0])

# Показать напарника
async def show_partner(update: Update, context: ContextTypes.DEFAULT_TYPE, partner):
    partner_id, name, hours, age, bio, username = partner

    context.user_data['current_partner_id'] = partner_id

    keyboard = [
        [
            InlineKeyboardButton("❤️ Лайк", callback_data=f"like_{partner_id}"),
            InlineKeyboardButton("👎 Дизлайк", callback_data=f"dislike_{partner_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.effective_message.reply_text(
        f"👤 Найден напарник:\n\n"
        f"📛 Имя: {name}\n"
        f"⏰ Часов в Rust: {hours}\n"
        f"🎂 Возраст: {age}\n"
        f"💬 О себе: {bio}",
        reply_markup=reply_markup
    )

# Обработка лайка/дизлайка
async def handle_like_dislike(query, context: ContextTypes.DEFAULT_TYPE):
    data = query.data.split('_')
    action = data[0]
    partner_id = int(data[1])
    user_id = query.from_user.id
    user_name = query.from_user.first_name

    if action == 'like':
        is_match_found = add_like(user_id, partner_id)
        
        if is_match_found:
            await query.edit_message_text("🎉 У вас ВЗАИМНЫЙ МАТЧ! Оба уведомлены.")
            await notify_users_about_match(context, user_id, partner_id)
        else:
            await query.edit_message_text("❤️ Ты поставил лайк! Ищем дальше...")
            await notify_user_about_like(context, partner_id, user_name)
            await find_partner_after_action(query, context, user_id)
    
    elif action == 'dislike':
        await query.edit_message_text("👎 Ты поставил дизлайк. Ищем следующего...")
        await find_partner_after_action(query, context, user_id)

# Автоматический поиск следующего
async def find_partner_after_action(query, context, user_id):
    queue = context.user_data.get('partner_queue', [])
    
    if not queue:
        await context.bot.send_message(chat_id=user_id, text="🎉 Ты просмотрел всех напарников!")
        return

    next_id = queue.pop(0)
    context.user_data['partner_queue'] = queue

    partner_data = context.user_data['current_partner_list'].get(next_id)
    if partner_data:
        await show_partner(query, context, partner_data)
    else:
        await find_partner_after_action(query, context, user_id)

# Главная функция
def main():
    init_db()
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    
    if not TOKEN:
        logger.error("❌ Не задан TELEGRAM_TOKEN")
        return

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("update", update_profile))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ Бот запущен!")
    app.run_polling()

if __name__ == '__main__':
    main()
