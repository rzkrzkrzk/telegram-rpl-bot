import os
import logging
import sqlite3
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

DB_PATH = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS source_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER UNIQUE,
            username TEXT,
            added_by INTEGER
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS target_chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER UNIQUE,
            link TEXT,
            added_by INTEGER
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS support_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            text TEXT,
            timestamp TEXT,
            answered INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def add_source_channel(chat_id, username, added_by):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO source_channels (chat_id, username, added_by) VALUES (?, ?, ?)',
              (chat_id, username, added_by))
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
    c.execute('INSERT OR IGNORE INTO target_chats (chat_id, link, added_by) VALUES (?, ?, ?)',
              (chat_id, link, added_by))
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
    c.execute('INSERT INTO support_messages (user_id, username, text, timestamp) VALUES (?, ?, ?, ?)',
              (user_id, username, text, datetime.now().isoformat()))
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
    c.execute('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

def add_admin(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

WAITING_LOGIN, WAITING_PASSWORD, WAITING_CHANNEL_USERNAME, WAITING_CHAT_LINK, WAITING_REPLY_TEXT = range(5)

def check_credentials(login, password):
    credentials = {
        "goyda1488": "goydarpl",
        "rzk1488": "rzksigma",
    }
    return credentials.get(login) == password

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для пересылки сообщений из каналов и поддержки пользователей.\n"
        "Администраторы, авторизуйтесь через /adminkarpl."
    )

async def adminkarpl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Эта команда доступна только в личных сообщениях.")
        return ConversationHandler.END
    await update.message.reply_text("🔑 Введите логин:")
    return WAITING_LOGIN

async def wait_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["login"] = update.message.text
    await update.message.reply_text("🔒 Введите пароль:")
    return WAITING_PASSWORD

async def wait_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    login = context.user_data.get("login")
    password = update.message.text
    if check_credentials(login, password):
        add_admin(update.effective_user.id)
        await update.message.reply_text("✅ Вы авторизованы!")
        await show_admin_menu(update, context)
    else:
        await update.message.reply_text("❌ Неверный логин или пароль. Попробуйте /adminkarpl")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Диалог отменён.")
    return ConversationHandler.END

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📩 Проверить поддержку", callback_data="support_list")],
        [InlineKeyboardButton("➕ Добавить каналы", callback_data="add_channel")],
        [InlineKeyboardButton("➕ Добавить чаты", callback_data="add_chat")],
        [InlineKeyboardButton("📋 Настройки", callback_data="show_settings")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text("🏠 Главное меню:", reply_markup=reply_markup)
        await update.callback_query.answer()
    else:
        await update.message.reply_text("🏠 Главное меню:", reply_markup=reply_markup)

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if not is_admin(user_id):
        await query.edit_message_text("⛔ Доступ запрещён.")
        return

    if data == "support_list":
        await show_support_list(query)
    elif data == "add_channel":
        await query.edit_message_text("Введите @username канала (бот должен быть админом):")
        return WAITING_CHANNEL_USERNAME
    elif data == "add_chat":
        await query.edit_message_text("Введите ссылку или @username чата (бот должен состоять):")
        return WAITING_CHAT_LINK
    elif data == "show_settings":
        await show_settings(query)
    elif data.startswith("reply_"):
        msg_id = int(data.split("_")[1])
        context.user_data["reply_to"] = msg_id
        await query.edit_message_text("✏️ Введите текст ответа:")
        return WAITING_REPLY_TEXT
    elif data.startswith("close_"):
        msg_id = int(data.split("_")[1])
        mark_answered(msg_id)
        await query.edit_message_text("✅ Обращение закрыто.")
        await show_support_list(query, refresh=True)
    elif data == "back_to_menu":
        await show_admin_menu(update, context)
    return ConversationHandler.END

async def show_support_list(query, refresh=False):
    messages = get_unanswered_messages()
    if not messages:
        text = "📭 Новых обращений нет."
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]]))
        return
    msg = messages[0]
    msg_id, user_id, username, text, timestamp = msg
    display_text = (
        f"📩 Обращение #{msg_id}\n"
        f"👤 {username or user_id}\n"
        f"🕒 {timestamp}\n\n"
        f"{text}"
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Ответить", callback_data=f"reply_{msg_id}")],
        [InlineKeyboardButton("✅ Закрыть", callback_data=f"close_{msg_id}")],
        [InlineKeyboardButton("⏩ Следующее", callback_data="support_list")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")],
    ]
    await query.edit_message_text(display_text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_settings(query):
    sources = get_source_channels()
    targets = get_target_chats()
    text = "📋 **Настройки**\n\n📢 **Каналы-источники:**\n"
    if sources:
        for chat_id, username in sources:
            text += f"  - {username or chat_id} (ID: {chat_id})\n"
    else:
        text += "  (нет)\n"
    text += "\n📥 **Целевые чаты:**\n"
    if targets:
        for chat_id, link in targets:
            text += f"  - {link or chat_id} (ID: {chat_id})\n"
    else:
        text += "  (нет)\n"
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]]
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def add_channel_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip()
    if not username.startswith('@'):
        username = '@' + username
    try:
        chat = await context.bot.get_chat(username)
        chat_id = chat.id
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if bot_member.status not in ['administrator', 'creator']:
            await update.message.reply_text("❌ Бот не администратор этого канала.")
            return ConversationHandler.END
        add_source_channel(chat_id, username, update.effective_user.id)
        await update.message.reply_text(f"✅ Канал {username} добавлен.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
    return ConversationHandler.END

async def add_chat_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text.strip()
    username = None
    chat_id = None
    if link.startswith('@'):
        username = link
    elif link.startswith('https://t.me/'):
        parts = link.split('/')
        username = '@' + parts[-1]
    else:
        try:
            chat_id = int(link)
        except:
            await update.message.reply_text("❌ Неверный формат.")
            return ConversationHandler.END
    try:
        if username:
            chat = await context.bot.get_chat(username)
            chat_id = chat.id
        else:
            chat = await context.bot.get_chat(chat_id)
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if bot_member.status not in ['member', 'administrator', 'creator']:
            await update.message.reply_text("❌ Бот не состоит в этом чате.")
            return ConversationHandler.END
        add_target_chat(chat_id, link, update.effective_user.id)
        await update.message.reply_text(f"✅ Чат {link} добавлен.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
    return ConversationHandler.END

async def reply_to_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_text = update.message.text
    msg_id = context.user_data.get("reply_to")
    if not msg_id:
        await update.message.reply_text("❌ Нет обращения для ответа.")
        return ConversationHandler.END

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT user_id, username FROM support_messages WHERE id = ?', (msg_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("❌ Обращение не найдено.")
        return ConversationHandler.END

    user_id, username = row
    try:
        await context.bot.send_message(chat_id=user_id, text=f"📨 Ответ поддержки:\n{reply_text}")
        mark_answered(msg_id)
        await update.message.reply_text("✅ Ответ отправлен, обращение закрыто.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка отправки: {e}")
    return ConversationHandler.END

async def forward_from_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_post = update.channel_post
    if not channel_post:
        return
    chat_id = channel_post.chat_id
    sources = get_source_channels()
    source_ids = [s[0] for s in sources]
    if chat_id not in source_ids:
        return

    targets = get_target_chats()
    for target_id, _ in targets:
        try:
            await channel_post.copy(chat_id=target_id)
            logger.info(f"Переслано из {chat_id} в {target_id}")
        except Exception as e:
            logger.error(f"Ошибка пересылки в {target_id}: {e}")

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    text = update.message.text
    if not text:
        return

    msg_id = add_support_message(user.id, user.username or str(user.id), text)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT user_id FROM admins')
    admins = [row[0] for row in c.fetchall()]
    conn.close()

    if not admins:
        await update.message.reply_text("Нет активных администраторов.")
        return

    for admin_id in admins:
        try:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Ответить", callback_data=f"reply_{msg_id}")],
                [InlineKeyboardButton("❌ Закрыть", callback_data=f"close_{msg_id}")]
            ])
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"📩 Новое обращение #{msg_id} от {user.username or user.id}:\n\n{text}",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Не удалось отправить админу {admin_id}: {e}")

    await update.message.reply_text("✅ Сообщение отправлено в поддержку.")

def main():
    app = Application.builder().token(TOKEN).build()

    conv_auth = ConversationHandler(
        entry_points=[CommandHandler("adminkarpl", adminkarpl)],
        states={
            WAITING_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, wait_login)],
            WAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, wait_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_auth)

    conv_channel = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_callback, pattern="^add_channel$")],
        states={
            WAITING_CHANNEL_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_channel_username)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_channel)

    conv_chat = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_callback, pattern="^add_chat$")],
        states={
            WAITING_CHAT_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_chat_link)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_chat)

    conv_reply = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_callback, pattern="^reply_")],
        states={
            WAITING_REPLY_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reply_to_support)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_reply)

    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^(support_list|show_settings|close_|back_to_menu)$"))

    # Правильный фильтр для каналов
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, forward_from_channels))

    # Правильный фильтр для личных сообщений
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_message))
    app.add_handler(CommandHandler("start", start))

    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
