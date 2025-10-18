# -*- coding: utf-8 -*-
# --------------------------------------------------------------
# Rust Match Bot – поиск напарников в игре Rust
# --------------------------------------------------------------
# Токен бота берётся из переменной окружения BOT_TOKEN.
# Если переменная не задана, можно вписать токен вручную,
# но безопаснее хранить его в Settings → Variables.
# --------------------------------------------------------------

import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
import sqlite3
from datetime import datetime

# -------------------- Токен --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # берём из переменной окружения
if not BOT_TOKEN:
    # Если переменная не найдена, бросаем ошибку, чтобы сразу увидеть в логах
    raise RuntimeError("❌ Переменная BOT_TOKEN не задана! Установи её в Railway → Settings → Variables")

# -------------------- Конфиги --------------------
MAX_DAILY_LIKES = 10  # лимит лайков в сутки

# -------------------- База данных --------------------
class Database:
    def __init__(self, db_name="rust_match.db"):
        # check_same_thread=False позволяет использовать соединение в разных потоках Updater'а
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.create_tables()
    
    def create_tables(self):
        cur = self.conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                nickname TEXT,
                experience TEXT,
                play_style TEXT,
                timezone TEXT,
                skills TEXT,
                description TEXT,
                photo_id TEXT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS likes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                liker_id INTEGER,
                liked_id INTEGER
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS daily_likes (
                user_id INTEGER,
                date DATE,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            )
        ''')
        self.conn.commit()
    
    # ----- Профиль -----
    def create_profile(self, user_id, data):
        cur = self.conn.cursor()
        cur.execute('''
            INSERT OR REPLACE INTO profiles
            (user_id, username, nickname, experience, play_style, timezone, skills, description, photo_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id, data.get('username'), data.get('nickname'), data.get('experience'),
            data.get('play_style'), data.get('timezone'), data.get('skills'),
            data.get('description'), data.get('photo_id')
        ))
        self.conn.commit()
    
    def get_profile(self, user_id):
        cur = self.conn.cursor()
        cur.execute('SELECT * FROM profiles WHERE user_id = ?', (user_id,))
        return cur.fetchone()
    
    def get_random_profile(self, exclude_user_id):
        cur = self.conn.cursor()
        cur.execute('''
            SELECT * FROM profiles
            WHERE user_id != ?
            AND user_id NOT IN (SELECT liked_id FROM likes WHERE liker_id = ?)
            ORDER BY RANDOM()
            LIMIT 1
        ''', (exclude_user_id, exclude_user_id))
        return cur.fetchone()
    
    # ----- Лайки -----
    def add_like(self, liker_id, liked_id):
        cur = self.conn.cursor()
        cur.execute('INSERT INTO likes (liker_id, liked_id) VALUES (?, ?)', (liker_id, liked_id))
        self.conn.commit()
        
        # Проверяем взаимный лайк
        cur.execute('SELECT * FROM likes WHERE liker_id = ? AND liked_id = ?', (liked_id, liker_id))
        return bool(cur.fetchone())
    
    # ----- Дневной лимит -----
    def get_daily_likes_count(self, user_id):
        cur = self.conn.cursor()
        today = datetime.now().date().isoformat()
        cur.execute('SELECT count FROM daily_likes WHERE user_id = ? AND date = ?', (user_id, today))
        row = cur.fetchone()
        return row[0] if row else 0
    
    def increment_daily_likes(self, user_id):
        cur = self.conn.cursor()
        today = datetime.now().date().isoformat()
        cur.execute('INSERT OR IGNORE INTO daily_likes (user_id, date) VALUES (?, ?)', (user_id, today))
        cur.execute('UPDATE daily_likes SET count = count + 1 WHERE user_id = ? AND date = ?', (user_id, today))
        self.conn.commit()

db = Database()

# -------------------- Клавиатуры --------------------
def get_main_keyboard():
    keyboard = [
        ['🔍 Поиск напарника', '👤 Мой профиль'],
        ['❤️ Мои мэтчи', '⚙️ Настройки']
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# -------------------- Команды --------------------
def start(update: Update, context: CallbackContext):
    user = update.effective_user
    txt = f"""🎮 Привет, {user.first_name}!

Добро пожаловать в **Rust Match Bot** – ищем напарников для выживания.

🛠️ Чтобы начать, создай профиль:
   /create

Дальше — нажимай кнопки:
🔍 Поиск напарника
👤 Мой профиль
❤️ Мои мэтчи
⚙️ Настройки
"""
    update.message.reply_text(txt, parse_mode='Markdown', reply_markup=get_main_keyboard())

def create_profile(update: Update, context: CallbackContext):
    """Запускаем последовательный опрос для создания профиля."""
    context.user_data.clear()
    context.user_data['step'] = 'nickname'
    update.message.reply_text('🎮 Какой у тебя ник в Rust? (пример: xXProKillerXx)')

def handle_profile(update: Update, context: CallbackContext):
    """Обрабатываем ответы пользователя в процессе создания профиля."""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    step = context.user_data.get('step')
    
    # Если пользователь отправил фото вместо текста – просто игнорируем (не нужен)
    if step == 'photo' and update.message.photo:
        # сохраняем id фото
        photo_id = update.message.photo[-1].file_id
        context.user_data['photo_id'] = photo_id
        finish_profile(update, context, user_id)
        return
    
    if step == 'photo' and text.lower() == 'нет':
        # пользователь отказался от фото
        context.user_data['photo_id'] = None
        finish_profile(update, context, user_id)
        return
    
    # --- Шаги опроса ---
    if step == 'nickname':
        context.user_data['nickname'] = text
        context.user_data['step'] = 'experience'
        keyboard = [['Новичок', 'Опытный', 'Профи']]
        reply = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        update.message.reply_text('🏆 Какой у тебя опыт в Rust?', reply_markup=reply)
    
    elif step == 'experience':
        context.user_data['experience'] = text
        context.user_data['step'] = 'play_style'
        keyboard = [['Соло', 'Дуэт', 'Клан']]
        reply = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        update.message.reply_text('👥 Что ты ищешь?', reply_markup=reply)
    
    elif step == 'play_style':
        context.user_data['play_style'] = text
        context.user_data['step'] = 'timezone'
        keyboard = [['Утро (6‑12)', 'День (12‑18)', 'Вечер (18‑24)', 'Ночь (0‑6)']]
        reply = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        update.message.reply_text('⏰ Когда обычно играешь?', reply_markup=reply)
    
    elif step == 'timezone':
        context.user_data['timezone'] = text
        context.user_data['step'] = 'skills'
        update.message.reply_text('🎯 Какие у тебя навыки? (строительство, PvP, крафт, сбор ресурсов)')
    
    elif step == 'skills':
        context.user_data['skills'] = text
        context.user_data['step'] = 'description'
        update.message.reply_text('📝 Расскажи о себе (цели, требования к напарнику и т.д.)')
    
    elif step == 'description':
        context.user_data['description'] = text
        context.user_data['step'] = 'photo'
        update.message.reply_text('📸 Пришли фото (или напиши «нет»)')
    
    else:
        # Если попали в неизвестный шаг – просто игнорируем.
        pass

def finish_profile(update: Update, context: CallbackContext, user_id: int):
    """Сохраняет профиль в БД и завершает диалог."""
    data = {
        'username': update.effective_user.username,
        'nickname': context.user_data.get('nickname'),
        'experience': context.user_data.get('experience'),
        'play_style': context.user_data.get('play_style'),
        'timezone': context.user_data.get('timezone'),
        'skills': context.user_data.get('skills'),
        'description': context.user_data.get('description'),
        'photo_id': context.user_data.get('photo_id')
    }
    db.create_profile(user_id, data)
    context.user_data.clear()
    update.message.reply_text('✅ Профиль успешно создан!', reply_markup=get_main_keyboard())
    show_profile(update, context, user_id)

def show_profile(update: Update, context: CallbackContext, user_id: int):
    """Отправляет пользователю его же профиль."""
    p = db.get_profile(user_id)
    if not p:
        update.message.reply_text('❌ Профиль не найден. Сначала создайте его: /create')
        return
    
    txt = f'''📛 <b>Ник:</b> {p[2]}
🏆 <b>Опыт:</b> {p[3]}
👥 <b>Ищет:</b> {p[4]}
⏰ <b>Время игры:</b> {p[5]}
🎯 <b>Навыки:</b> {p[6]}

📝 <b>О себе:</b>
{p[7]}'''
    
    if p[8]:  # фото есть
        context.bot.send_photo(chat_id=update.effective_chat.id,
                               photo=p[8],
                               caption=txt,
                               parse_mode='HTML',
                               reply_markup=get_main_keyboard())
    else:
        update.message.reply_text(txt, parse_mode='HTML', reply_markup=get_main_keyboard())

def profile_cmd(update: Update, context: CallbackContext):
    """Команда /profile – показывает ваш профиль."""
    show_profile(update, context, update.effective_user.id)

def search(update: Update, context: CallbackContext):
    """Поиск случайного профиля (с лайком/пропуском)."""
    user_id = update.effective_user.id
    
    # проверяем лимит лайков
    if db.get_daily_likes_count(user_id) >= MAX_DAILY_LIKES:
        update.message.reply_text(f'❌ Вы исчерпали дневной лимит лайков ({MAX_DAILY_LIKES}). Завтра будет снова.')
        return
    
    profile = db.get_random_profile(user_id)
    if not profile:
        update.message.reply_text('📭 Пока нет доступных профилей. Попробуйте позже.')
        return
    
    txt = f'''🎮 <b>Ник:</b> {profile[2]}
🏆 <b>Опыт:</b> {profile[3]}
👥 <b>Ищет:</b> {profile[4]}
⏰ <b>Время игры:</b> {profile[5]}
🎯 <b>Навыки:</b> {profile[6]}

📝 <b>О себе:</b>
{profile[7]}'''
    
    keyboard = [[
        InlineKeyboardButton('❤️ Нравится', callback_data=f'like_{profile[0]}'),
        InlineKeyboardButton('⏭️ Пропустить', callback_data=f'skip_{profile[0]}')
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if profile[8]:
        context.bot.send_photo(chat_id=update.effective_chat.id,
                               photo=profile[8],
                               caption=txt,
                               parse_mode='HTML',
                               reply_markup=reply_markup)
    else:
        update.message.reply_text(txt, parse_mode='HTML', reply_markup=reply_markup)

def button_handler(update: Update, context: CallbackContext):
    """Обрабатываем лайк/пропуск."""
    query = update.callback_query
    user_id = query.from_user.id
    action, target_id_str = query.data.split('_')
    target_id = int(target_id_str)
    
    if action == 'like':
        # проверка лимита
        if db.get_daily_likes_count(user_id) >= MAX_DAILY_LIKES:
            query.answer('❌ Лимит лайков исчерпан!')
            return
        
        db.increment_daily_likes(user_id)
        is_match = db.add_like(user_id, target_id)
        if is_match:
            query.answer('🎉 Взаимный мэтч!')
            # Уведомляем обоих участников
            context.bot.send_message(user_id,
                                     f'🎉 Вы нашли мэтч с {db.get_profile(target_id)[2]}! Пишите в чат.')
            context.bot.send_message(target_id,
                                     f'🎉 Вы нашли мэтч с {db.get_profile(user_id)[2]}! Пишите в чат.')
        else:
            query.answer('👍 Лайк отправлен')
    
    elif action == 'skip':
        query.answer('⏭️ Пропущено')
    
    # Удаляем сообщение с карточкой (чистый UI)
    query.message.delete()
    # Сразу показываем следующий профиль
    search(update, context)

def matches(update: Update, context: CallbackContext):
    """Показывает список всех ваших мэтчей."""
    user_id = update.effective_user.id
    cur = db.conn.cursor()
    cur.execute('''
        SELECT p.* FROM (
            SELECT user1_id AS uid FROM matches WHERE user2_id = ?
            UNION
            SELECT user2_id AS uid FROM matches WHERE user1_id = ?
        ) AS m
        JOIN profiles p ON p.user_id = m.uid
    ''', (user_id, user_id))
    rows = cur.fetchall()
    
    if not rows:
        update.message.reply_text('📭 У вас пока нет мэтчей. Ищите дальше! 🎯')
        return
    
    update.message.reply_text(f'❤️ У вас {len(rows)} мэтчей:')
    
    for p in rows:
        txt = f'''🎮 <b>Ник:</b> {p[2]}
🏆 <b>Опыт:</b> {p[3]}
👥 <b>Ищет:</b> {p[4]}
⏰ <b>Время игры:</b> {p[5]}
🎯 <b>Навыки:</b> {p[6]}

📝 <b>О себе:</b>
{p[7]}'''
        if p[8]:
            context.bot.send_photo(chat_id=update.effective_chat.id,
                                   photo=p[8],
                                   caption=txt,
                                   parse_mode='HTML')
        else:
            update.message.reply_text(txt, parse_mode='HTML')

def settings(update: Update, context: CallbackContext):
    """Показываем простые настройки (пока только сброс лимита)."""
    keyboard = [[InlineKeyboardButton('🔄 Сбросить лимит лайков (на сегодня)', callback_data='reset_likes')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text('⚙️ Настройки бота', reply_markup=reply_markup)

def settings_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    if query.data == 'reset_likes':
        # Сбросим счётчик только у вызывающего пользователя
        cur = db.conn.cursor()
        today = datetime.now().date().isoformat()
        cur.execute('DELETE FROM daily_likes WHERE user_id = ? AND date = ?', (query.from_user.id, today))
        db.conn.commit()
        query.answer('🔄 Лимит лайков сброшен')
        query.message.delete()
    else:
        query.answer('❓ Неизвестная команда')

# -------------------- Запуск --------------------
def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    
    # Команды
    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(CommandHandler('create', create_profile))
    dp.add_handler(CommandHandler('profile', profile_cmd))
    dp.add_handler(CommandHandler('search', search))
    dp.add_handler(CommandHandler('matches', matches))
    dp.add_handler(CommandHandler('settings', settings))
    
    # Текстовые сообщения – часть создания профиля
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_profile))
    dp.add_handler(MessageHandler(Filters.photo, handle_profile))
    
    # Inline‑кнопки
    dp.add_handler(CallbackQueryHandler(button_handler, pattern='^(like|skip)_'))
    dp.add_handler(CallbackQueryHandler(settings_callback, pattern='^reset_likes$'))
    
    # Запускаем
    updater.start_polling()
    print('🚂 Bot started!')   # будет видно в логах Railway
    updater.idle()

if __name__ == '__main__':
    main()
