import os
import logging
import sqlite3
import asyncio
import random
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, BotCommand, BotCommandScopeDefault
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN не задан!")

# Для Railway лучше использовать путь через переменную окружения, если подключен Volume
DB_PATH = os.getenv("DB_PATH", "bot_data.db")
ADMIN_SESSION_MINUTES = 30

# Состояния
WAITING_LOGIN, WAITING_PASSWORD, WAITING_CHANNEL_USERNAME, WAITING_CHAT_LINK, WAITING_REPLY_TEXT, WAITING_SUPPORT_MSG = range(6)
WAITING_DUEL_SHOT = 10
WAITING_GIF_UPLOAD = 11 

# ---------- БД ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS source_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE, username TEXT, added_by INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS target_chats (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE, link TEXT, added_by INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS support_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, text TEXT, timestamp TEXT, answered INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, last_activity INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_config (key TEXT PRIMARY KEY, value TEXT)''')
    
    # Новая таблица для статистики дуэлей
    c.execute('''CREATE TABLE IF NOT EXISTS duel_stats (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        total_shots INTEGER DEFAULT 0,
        goals INTEGER DEFAULT 0
    )''')
    
    c.execute('INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)', ('gif_goal', ''))
    c.execute('INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)', ('gif_save', ''))
    conn.commit()
    conn.close()

init_db()

# --- Функции статистики ---
def update_duel_stats(user_id, username, first_name, is_goal):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    goal_inc = 1 if is_goal else 0
    c.execute('''INSERT INTO duel_stats (user_id, username, first_name, total_shots, goals)
                 VALUES (?, ?, ?, 1, ?)
                 ON CONFLICT(user_id) DO UPDATE SET
                 username = excluded.username,
                 first_name = excluded.first_name,
                 total_shots = total_shots + 1,
                 goals = goals + ?''', (user_id, username, first_name, goal_inc, goal_inc))
    conn.commit()
    conn.close()

def get_top_rating():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Выбираем топ-10 игроков, у которых более 7 бросков, сортируем по проценту
    c.execute('''SELECT first_name, username, goals, total_shots,
                 (CAST(goals AS FLOAT) / total_shots * 100) as percent
                 FROM duel_stats 
                 WHERE total_shots >= 7 
                 ORDER BY percent DESC LIMIT 10''')
    rows = c.fetchall()
    conn.close()
    return rows

def reset_all_ratings():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM duel_stats')
    conn.commit()
    conn.close()

# Остальные функции БД (get_config, set_config и т.д.) остаются без изменений
def get_config(key):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT value FROM bot_config WHERE key = ?', (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else ''

def set_config(key, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()

def add_source_channel(chat_id, username, added_by):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO source_channels (chat_id, username, added_by) VALUES (?, ?, ?)', (chat_id, username, added_by))
    conn.commit()
    conn.close()

def get_source_channels():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT chat_id, username FROM source_channels')
    rows = c.fetchall()
    conn.close()
    return rows

def add_target_chat(chat_id, link, added_by):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO target_chats (chat_id, link, added_by) VALUES (?, ?, ?)', (chat_id, link, added_by))
    conn.commit()
    conn.close()

def get_target_chats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT chat_id, link FROM target_chats')
    rows = c.fetchall()
    conn.close()
    return rows

def add_support_message(user_id, username, text):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO support_messages (user_id, username, text, timestamp) VALUES (?, ?, ?, ?)', (user_id, username, text, datetime.now().isoformat()))
    conn.commit()
    return c.lastrowid

def get_unanswered_messages():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, user_id, username, text, timestamp FROM support_messages WHERE answered = 0 ORDER BY id')
    rows = c.fetchall()
    conn.close()
    return rows

def mark_answered(msg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE support_messages SET answered = 1 WHERE id = ?', (msg_id,))
    conn.commit()
    conn.close()

def is_admin(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT last_activity FROM admins WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        if (datetime.now().timestamp() - row[0]) < ADMIN_SESSION_MINUTES * 60:
            return True
        else:
            remove_admin(user_id) 
    return False

def add_admin(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO admins (user_id, last_activity) VALUES (?, ?)', (user_id, int(datetime.now().timestamp())))
    conn.commit()
    conn.close()

def update_admin_activity(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE admins SET last_activity = ? WHERE user_id = ?', (int(datetime.now().timestamp()), user_id))
    conn.commit()
    conn.close()

def remove_admin(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM admins WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def check_credentials(login, password):
    credentials = {"goyda1488": "goydarpl", "rzk1488": "rzksigma"}
    return credentials.get(login) == password

# ---------- Клавиатуры ----------
def main_menu_keyboard():
    return ReplyKeyboardMarkup([["🏠 Главное меню"]], resize_keyboard=True)

def admin_menu_keyboard():
    return ReplyKeyboardMarkup([
        ["➕ Добавить каналы", "➕ Добавить чаты"],
        ["📩 Проверить поддержку", "⚙️ Настройки"],
        ["🎮 Настройки игры", "🧹 Обнулить рейтинг"],
        ["🚪 Выйти"]
    ], resize_keyboard=True)

def welcome_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Наш Discord", callback_data="discord")],
        [InlineKeyboardButton("🌐 Наш Сайт", callback_data="website")],
        [InlineKeyboardButton("🆘 Обратиться в поддержку", callback_data="support")],
        [InlineKeyboardButton("🏒 Дуэль Буллитов", callback_data="duel")]
    ])

def duel_shot_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🥅 Левая девятка", callback_data="shot_left")],
        [InlineKeyboardButton("🥅 Правая девятка", callback_data="shot_right")],
        [InlineKeyboardButton("🧤 Домик", callback_data="shot_five")],
        [InlineKeyboardButton("🥅 Низ в угол", callback_data="shot_low")]
    ])

# ---------- Обработчики ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Добро пожаловать в Russian Puck League!", reply_markup=main_menu_keyboard())
    await update.message.reply_text("📌 Выберите раздел:", reply_markup=welcome_inline_keyboard())

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📌 Выберите раздел:", reply_markup=welcome_inline_keyboard())

async def inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "discord":
        await query.edit_message_text("💬 Discord Server RPL: https://discord.gg/dgkFMCgDwx")
        await query.message.reply_text("📌 Выберите другой раздел:", reply_markup=welcome_inline_keyboard())
    elif data == "website":
        await query.edit_message_text("🌐 Сайт Russian Puck League: rplpuck.ru")
        await query.message.reply_text("📌 Выберите другой раздел:", reply_markup=welcome_inline_keyboard())
    elif data == "support":
        context.user_data["in_conversation_support"] = True 
        await query.edit_message_text("✍️ Напишите сообщение поддержке или /cancel")
        return WAITING_SUPPORT_MSG
    elif data == "duel":
        await query.edit_message_text("🏒 Дуэль Буллитов! Выбери зону для броска:", reply_markup=duel_shot_keyboard())
        return WAITING_DUEL_SHOT

async def start_duel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает игру по команде /duelrpl."""
    user = update.effective_user
    if update.effective_chat.type != "private":
        context.user_data[f"in_duel_{user.id}"] = True
        await update.message.reply_text(f"🏒 {user.first_name}, твоя очередь! Выбери зону:", reply_markup=duel_shot_keyboard())
    else:
        await update.message.reply_text("🏒 Дуэль Буллитов! Выбери зону:", reply_markup=duel_shot_keyboard())
    return WAITING_DUEL_SHOT

async def rating_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выводит рейтинг по команде /rating."""
    top = get_top_rating()
    if not top:
        await update.message.reply_text("📊 Рейтинг пуст. Нужно минимум 7 бросков для попадания в ТОП!")
        return
    
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    text = "🏆 **ТОП-10 ИГРОКОВ RPL**\n\n"
    for i, row in enumerate(top):
        first_name, username, goals, total, percent = row
        user_label = f"(@{username})" if username else ""
        text += f"{medals[i]} {first_name}{user_label}: {percent:.1f}% забитых бросков ({goals}/{total})\n"
    
    await update.message.reply_text(text, parse_mode="Markdown")

async def duel_shot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    shot_zone = query.data
    user = query.from_user

    goalie_zones = ["shot_left", "shot_right", "shot_five", "shot_low"]
    goalie_choice = random.choice(goalie_zones)
    scored = random.random() < 0.35 if shot_zone != goalie_choice else False

    # ЗАПИСЫВАЕМ СТАТИСТИКУ
    update_duel_stats(user.id, user.username, user.first_name, scored)

    if scored:
        gif = get_config('gif_goal') or "https://media.giphy.com/media/3o7aTskHEUdgCQAXde/giphy.gif"
        result_text = "⚡️ **ГОЛ!** Вы точно попали в девятку!"
    else:
        gif = get_config('gif_save') or "https://media.giphy.com/media/3o6Ztq5cG6GZj5F9uo/giphy.gif"
        result_text = "🧤 **СЕЙВ!** Вратарь отразил бросок!"

    await query.edit_message_text(
        f"{result_text}\n"
        f"Ваш бросок: {shot_zone.replace('shot_', '').capitalize()}\n"
        f"Вратарь выбрал: {goalie_choice.replace('shot_', '').capitalize()}"
    )
    
    try:
        await query.message.reply_animation(gif)
    except Exception as e:
        logger.error(f"Ошибка отправки GIF: {e}")

    if update.effective_chat.type != "private":
        context.user_data.pop(f"in_duel_{user.id}", None)

    if update.effective_chat.type == "private":
        await query.message.reply_text("📌 Выберите другой раздел:", reply_markup=welcome_inline_keyboard())
    
    return ConversationHandler.END

# ---------- Админ-панель ----------
# (Методы авторизации остаются прежними)
async def adminkarpl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return ConversationHandler.END
    if is_admin(update.effective_user.id):
        await update.message.reply_text("✅ Вы в админ-панели", reply_markup=admin_menu_keyboard())
        return ConversationHandler.END
    await update.message.reply_text("🔑 Введите логин:")
    return WAITING_LOGIN

async def wait_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["login"] = update.message.text
    await update.message.reply_text("🔒 Введите пароль:")
    return WAITING_PASSWORD

async def wait_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if check_credentials(context.user_data.get("login"), update.message.text):
        add_admin(update.effective_user.id)
        await update.message.reply_text("✅ Авторизован!", reply_markup=admin_menu_keyboard())
        return ConversationHandler.END
    await update.message.reply_text("❌ Ошибка!")
    return WAITING_PASSWORD

async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return ConversationHandler.END
    update_admin_activity(user_id)
    text = update.message.text

    if text == "➕ Добавить каналы":
        await update.message.reply_text("Введите @username канала:")
        return WAITING_CHANNEL_USERNAME
    elif text == "➕ Добавить чаты":
        await update.message.reply_text("Введите ID чата:")
        return WAITING_CHAT_LINK
    elif text == "📩 Проверить поддержку":
        await show_support_messages(update, context)
    elif text == "⚙️ Настройки":
        await show_settings(update, context)
    elif text == "🎮 Настройки игры":
        await show_game_settings(update, context)
    elif text == "🧹 Обнулить рейтинг":
        reset_all_ratings()
        await update.message.reply_text("♻️ Весь рейтинг игроков был успешно обнулен!")
    elif text == "🚪 Выйти":
        remove_admin(user_id)
        await update.message.reply_text("🚪 Вышли", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# (Дополнительные методы поддержки, настроек и т.д. из вашего кода)
async def add_channel_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip()
    if not username.startswith('@'): username = '@' + username
    try:
        chat = await context.bot.get_chat(username)
        add_source_channel(chat.id, username, update.effective_user.id)
        await update.message.reply_text("✅ Добавлен", reply_markup=admin_menu_keyboard())
    except: await update.message.reply_text("❌ Ошибка")
    return ConversationHandler.END

async def add_chat_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text.strip()
    try:
        add_target_chat(int(link), link, update.effective_user.id)
        await update.message.reply_text("✅ Добавлен", reply_markup=admin_menu_keyboard())
    except: await update.message.reply_text("❌ Ошибка")
    return ConversationHandler.END

async def support_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg_id = add_support_message(user.id, user.username or str(user.id), update.message.text)
    await update.message.reply_text("✅ Сообщение в поддержке.")
    context.user_data.pop("in_conversation_support", None)
    return ConversationHandler.END

async def support_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("in_conversation_support", None)
    await update.message.reply_text("❌ Отмена.")
    return ConversationHandler.END

async def show_support_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    messages = get_unanswered_messages()
    if not messages:
        await update.message.reply_text("📭 Пусто", reply_markup=admin_menu_keyboard())
        return
    m = messages[0]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Ответить", callback_data=f"reply_{m[0]}"), InlineKeyboardButton("❌ Закрыть", callback_data=f"close_{m[0]}")]])
    await update.message.reply_text(f"📩 #{m[0]} от {m[2]}:\n\n{m[3]}", reply_markup=kb)

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sources = get_source_channels()
    targets = get_target_chats()
    text = "📋 Настройки:\n\n📢 Источники:\n" + "\n".join([f"- {s[1]}" for s in sources]) + "\n\n📥 Чаты:\n" + "\n".join([f"- {t[1]}" for t in targets])
    await update.message.reply_text(text, reply_markup=admin_menu_keyboard())

async def show_game_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    g, s = get_config('gif_goal'), get_config('gif_save')
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Изменить GOAL", callback_data="set_goal"), InlineKeyboardButton("🔄 Изменить SAVE", callback_data="set_save")]])
    await update.message.reply_text(f"🎮 Настройки GIF:\n\nGOAL: {g}\nSAVE: {s}", reply_markup=kb)

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data in ["set_goal", "set_save"]:
        context.user_data["gif_type"] = "gif_goal" if query.data == "set_goal" else "gif_save"
        await query.edit_message_text("📤 Отправьте GIF:")
        return WAITING_GIF_UPLOAD
    return ConversationHandler.END

async def receive_gif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = update.message.animation.file_id if update.message.animation else None
    if file_id:
        set_config(context.user_data.get("gif_type"), file_id)
        await update.message.reply_text("✅ Сохранено")
        return ConversationHandler.END
    return WAITING_GIF_UPLOAD

async def forward_from_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cp = update.channel_post
    if not cp: return
    sources = [s[0] for s in get_source_channels()]
    if cp.chat_id in sources:
        targets = get_target_chats()
        for t in targets:
            try: await cp.copy(chat_id=t[0])
            except: pass

async def handle_unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return
    if context.user_data.get("in_conversation_support") or is_admin(update.effective_user.id): return
    # Просто игнорим или удаляем неизвестное

async def post_init(application: Application):
    """Настройка подсказок команд в меню /"""
    await application.bot.set_my_commands([
        BotCommand("duelrpl", "Дуэль Буллитов с ИИ вратарём"),
        BotCommand("rating", "Топ 10 игроков лиги")
    ], scope=BotCommandScopeDefault())

# ---------- MAIN ----------
from telegram import BotCommand, BotCommandScopeDefault

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # СИСТЕМА ДУЭЛИ
    app.add_handler(ConversationHandler(
        entry_points=[
            CallbackQueryHandler(inline_callback, pattern="^duel$"),
            CommandHandler("duelrpl", start_duel_command)
        ],
        states={WAITING_DUEL_SHOT: [CallbackQueryHandler(duel_shot, pattern="^shot_")]},
        fallbacks=[CommandHandler("cancel", start)],
        allow_reentry=True
    ))

    # СИСТЕМА ПОДДЕРЖКИ
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(inline_callback, pattern="^support$")],
        states={WAITING_SUPPORT_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, support_receive)]},
        fallbacks=[CommandHandler("cancel", support_cancel)],
        allow_reentry=True
    ))

    # СИСТЕМА АДМИНКИ (ГИФ)
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern="^set_")],
        states={WAITING_GIF_UPLOAD: [MessageHandler(filters.ANIMATION | filters.Document.ALL, receive_gif)]},
        fallbacks=[CommandHandler("cancel", adminkarpl)],
        allow_reentry=True
    ))

    # АДМИН АВТОРИЗАЦИЯ
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("adminkarpl", adminkarpl)],
        states={
            WAITING_LOGIN: [MessageHandler(filters.TEXT, wait_login)],
            WAITING_PASSWORD: [MessageHandler(filters.TEXT, wait_password)]
        },
        fallbacks=[CommandHandler("cancel", start)],
        allow_reentry=True
    ))

    # АДМИН ДОБАВЛЕНИЕ КАНАЛОВ/ЧАТОВ
    app.add_handler(ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Добавить каналы$"), admin_buttons),
            MessageHandler(filters.Regex("^➕ Добавить чаты$"), admin_buttons)
        ],
        states={
            WAITING_CHANNEL_USERNAME: [MessageHandler(filters.TEXT, add_channel_username)],
            WAITING_CHAT_LINK: [MessageHandler(filters.TEXT, add_chat_link)]
        },
        fallbacks=[CommandHandler("cancel", adminkarpl)],
        allow_reentry=True
    ))

    # Хендлеры кнопок и команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rating", rating_command))
    app.add_handler(MessageHandler(filters.Regex("^🏠 Главное меню$"), main_menu))
    app.add_handler(MessageHandler(filters.Regex("^📩 Проверить поддержку$"), admin_buttons))
    app.add_handler(MessageHandler(filters.Regex("^⚙️ Настройки$"), admin_buttons))
    app.add_handler(MessageHandler(filters.Regex("^🎮 Настройки игры$"), admin_buttons))
    app.add_handler(MessageHandler(filters.Regex("^🧹 Обнулить рейтинг$"), admin_buttons))
    app.add_handler(MessageHandler(filters.Regex("^🚪 Выйти$"), admin_buttons))
    app.add_handler(CallbackQueryHandler(inline_callback, pattern="^(discord|website)$"))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, forward_from_channels))

    app.run_polling()

if __name__ == "__main__":
    main()
