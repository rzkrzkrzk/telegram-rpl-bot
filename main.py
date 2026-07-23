import os
import logging
import sqlite3
import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
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
ADMIN_SESSION_MINUTES = 30

# Состояния для диалогов
WAITING_LOGIN, WAITING_PASSWORD, WAITING_CHANNEL_USERNAME, WAITING_CHAT_LINK, WAITING_REPLY_TEXT, WAITING_SUPPORT_MSG = range(6)

# ---------- БД ----------
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
            user_id INTEGER PRIMARY KEY,
            last_activity INTEGER
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ---------- Функции БД ----------
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
    c.execute('SELECT last_activity FROM admins WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        last_activity = row[0]
        if last_activity and (datetime.now().timestamp() - last_activity) < ADMIN_SESSION_MINUTES * 60:
            return True
        else:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('DELETE FROM admins WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
            return False
    return False

def add_admin(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO admins (user_id, last_activity) VALUES (?, ?)',
              (user_id, int(datetime.now().timestamp())))
    conn.commit()
    conn.close()

def update_admin_activity(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE admins SET last_activity = ? WHERE user_id = ?',
              (int(datetime.now().timestamp()), user_id))
    conn.commit()
    conn.close()

def remove_admin(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM admins WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def check_credentials(login, password):
    credentials = {
        "goyda1488": "goydarpl",
        "rzk1488": "rzksigma",
    }
    return credentials.get(login) == password

# ---------- Клавиатуры ----------
def main_menu_keyboard():
    """Нижняя клавиатура – только одна кнопка 'Главное меню'."""
    return ReplyKeyboardMarkup([["🏠 Главное меню"]], resize_keyboard=True)

def admin_menu_keyboard():
    """Клавиатура для админ-панели."""
    return ReplyKeyboardMarkup([
        ["➕ Добавить каналы", "➕ Добавить чаты"],
        ["📩 Проверить поддержку", "⚙️ Настройки"],
        ["🚪 Выйти"]
    ], resize_keyboard=True)

# ---------- Inline-меню для приветствия ----------
def welcome_inline_keyboard():
    keyboard = [
        [InlineKeyboardButton("💬 Наш Discord", callback_data="discord")],
        [InlineKeyboardButton("🌐 Наш Сайт", callback_data="website")],
        [InlineKeyboardButton("🆘 Обратиться в поддержку", callback_data="support")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ---------- Обработчики ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Добро пожаловать в **Russian Puck League**!\n"
        "Выберите действие с помощью кнопок ниже.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    # Отправляем сообщение с inline-кнопками
    await update.message.reply_text(
        "📌 Выберите раздел:",
        reply_markup=welcome_inline_keyboard()
    )

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Главное меню' – возвращает приветствие."""
    await update.message.reply_text(
        "👋 Добро пожаловать в **Russian Puck League**!\n"
        "Выберите действие с помощью кнопок ниже.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    await update.message.reply_text(
        "📌 Выберите раздел:",
        reply_markup=welcome_inline_keyboard()
    )

# ---------- Inline-колбэки для главного меню ----------
async def inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "discord":
        await query.edit_message_text(
            "💬 **Discord Server RPL:** https://discord.gg/dgkFMCgDwx",
            parse_mode="Markdown"
        )
        # Показываем inline-кнопки снова под этим сообщением
        await query.message.reply_text(
            "📌 Выберите другой раздел:",
            reply_markup=welcome_inline_keyboard()
        )
    elif data == "website":
        await query.edit_message_text(
            "🌐 **Сайт Russian Puck League:** rplpuck.ru",
            parse_mode="Markdown"
        )
        await query.message.reply_text(
            "📌 Выберите другой раздел:",
            reply_markup=welcome_inline_keyboard()
        )
    elif data == "support":
        # Запускаем диалог поддержки
        context.user_data["in_conversation"] = True  # флаг для отключения автоудаления
        await query.edit_message_text(
            "✍️ Напишите ваше сообщение для поддержки.\n"
            "Мы ответим вам как можно скорее.\n\n"
            "Для отмены отправьте /cancel"
        )
        return WAITING_SUPPORT_MSG
    return ConversationHandler.END

# ---------- Поддержка (диалог) ----------
async def support_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    if not text:
        await update.message.reply_text("Пожалуйста, напишите текст сообщения.")
        return WAITING_SUPPORT_MSG

    msg_id = add_support_message(user.id, user.username or str(user.id), text)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT user_id FROM admins')
    admins = [row[0] for row in c.fetchall()]
    conn.close()

    if admins:
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
    else:
        await update.message.reply_text("⚠️ Нет активных администраторов. Сообщение сохранено.")

    await update.message.reply_text("✅ Сообщение отправлено в поддержку.")
    # Сбрасываем флаг диалога
    context.user_data["in_conversation"] = False
    # Возвращаем в главное меню
    await main_menu(update, context)
    return ConversationHandler.END

async def support_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["in_conversation"] = False
    await update.message.reply_text("❌ Отправка отменена.")
    await main_menu(update, context)
    return ConversationHandler.END

# ---------- Админ-панель ----------
async def adminkarpl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Команда только в личных сообщениях.")
        return ConversationHandler.END
    context.user_data["in_conversation"] = True  # флаг для автоудаления
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
        context.user_data["in_conversation"] = False  # снимаем флаг
        await update.message.reply_text("✅ Авторизован!", reply_markup=admin_menu_keyboard())
        return ConversationHandler.END
    else:
        await update.message.reply_text("❌ Неверный логин или пароль. Попробуйте /adminkarpl")
        return WAITING_PASSWORD

# ---------- Обработчик кнопок админ-меню ----------
async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Сессия истекла. Авторизуйтесь через /adminkarpl.")
        await main_menu(update, context)
        return

    text = update.message.text
    if text == "➕ Добавить каналы":
        context.user_data["in_conversation"] = True
        await update.message.reply_text("Введите @username канала (бот должен быть админом):")
        return WAITING_CHANNEL_USERNAME
    elif text == "➕ Добавить чаты":
        context.user_data["in_conversation"] = True
        await update.message.reply_text(
            "Введите числовой ID чата или @username.\n"
            "Бот должен состоять в чате.\n"
            "Узнать ID можно через /getid в нужном чате."
        )
        return WAITING_CHAT_LINK
    elif text == "📩 Проверить поддержку":
        await show_support_messages(update, context)
        return
    elif text == "⚙️ Настройки":
        await show_settings(update, context)
        return
    elif text == "🚪 Выйти":
        remove_admin(user_id)
        await update.message.reply_text("🚪 Вы вышли из админ-панели.", reply_markup=main_menu_keyboard())
        return
    else:
        await update.message.reply_text("Используйте кнопки меню.", reply_markup=admin_menu_keyboard())
        return
    return ConversationHandler.END

# ---------- Добавление канала (диалог) ----------
async def add_channel_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip()
    if not username.startswith('@'):
        username = '@' + username
    try:
        chat = await context.bot.get_chat(username)
        chat_id = chat.id
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if bot_member.status not in ['administrator', 'creator']:
            await update.message.reply_text("❌ Бот не администратор.")
            context.user_data["in_conversation"] = False
            return ConversationHandler.END
        add_source_channel(chat_id, username, update.effective_user.id)
        await update.message.reply_text(f"✅ Канал {username} добавлен.", reply_markup=admin_menu_keyboard())
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
    context.user_data["in_conversation"] = False
    return ConversationHandler.END

# ---------- Добавление чата (диалог) ----------
async def add_chat_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text.strip()
    chat_id = None
    username = None

    if link.startswith('@'):
        username = link
    elif link.startswith('https://t.me/'):
        parts = link.split('/')
        if len(parts) >= 4:
            candidate = parts[-1]
            if candidate and not candidate.startswith('joinchat') and not candidate.startswith('+'):
                username = '@' + candidate
            else:
                await update.message.reply_text("❌ Приватная ссылка не поддерживается. Используйте ID.")
                context.user_data["in_conversation"] = False
                return ConversationHandler.END
    else:
        try:
            chat_id = int(link)
        except ValueError:
            username = '@' + link

    try:
        if username:
            chat = await context.bot.get_chat(username)
            chat_id = chat.id
        elif chat_id is not None:
            chat = await context.bot.get_chat(chat_id)
        else:
            await update.message.reply_text("❌ Неверный формат.")
            context.user_data["in_conversation"] = False
            return ConversationHandler.END

        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if bot_member.status not in ['member', 'administrator', 'creator']:
            await update.message.reply_text("❌ Бот не состоит в чате.")
            context.user_data["in_conversation"] = False
            return ConversationHandler.END

        add_target_chat(chat_id, link, update.effective_user.id)
        await update.message.reply_text(f"✅ Чат {link} добавлен.", reply_markup=admin_menu_keyboard())
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
    context.user_data["in_conversation"] = False
    return ConversationHandler.END

# ---------- Просмотр поддержки и настроек ----------
async def show_support_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    messages = get_unanswered_messages()
    if not messages:
        await update.message.reply_text("📭 Новых обращений нет.", reply_markup=admin_menu_keyboard())
        return
    msg = messages[0]
    msg_id, user_id, username, text, timestamp = msg
    display_text = (
        f"📩 Обращение #{msg_id}\n"
        f"👤 {username or user_id}\n"
        f"🕒 {timestamp}\n\n"
        f"{text}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Ответить", callback_data=f"reply_{msg_id}")],
        [InlineKeyboardButton("✅ Закрыть", callback_data=f"close_{msg_id}")],
        [InlineKeyboardButton("⏩ Следующее", callback_data="next_support")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_admin")]
    ])
    await update.message.reply_text(display_text, reply_markup=keyboard)

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=admin_menu_keyboard())

# ---------- Инлайн-колбэки для админа ----------
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if not is_admin(user_id):
        await query.edit_message_text("⛔ Сессия истекла. Авторизуйтесь заново.")
        return

    if data.startswith("reply_"):
        msg_id = int(data.split("_")[1])
        context.user_data["reply_to"] = msg_id
        context.user_data["in_conversation"] = True
        await query.edit_message_text("✏️ Введите текст ответа (в личку боту):")
        return WAITING_REPLY_TEXT
    elif data.startswith("close_"):
        msg_id = int(data.split("_")[1])
        mark_answered(msg_id)
        await query.edit_message_text("✅ Обращение закрыто.")
        messages = get_unanswered_messages()
        if messages:
            await show_support_messages(update, context)
        else:
            await query.message.reply_text("📭 Больше нет обращений.", reply_markup=admin_menu_keyboard())
    elif data == "next_support":
        await query.message.delete()
        messages = get_unanswered_messages()
        if messages:
            msg = messages[0]
            msg_id, user_id, username, text, timestamp = msg
            display_text = (
                f"📩 Обращение #{msg_id}\n"
                f"👤 {username or user_id}\n"
                f"🕒 {timestamp}\n\n"
                f"{text}"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Ответить", callback_data=f"reply_{msg_id}")],
                [InlineKeyboardButton("✅ Закрыть", callback_data=f"close_{msg_id}")],
                [InlineKeyboardButton("⏩ Следующее", callback_data="next_support")],
                [InlineKeyboardButton("🔙 Назад", callback_data="back_to_admin")]
            ])
            await query.message.reply_text(display_text, reply_markup=keyboard)
        else:
            await query.message.reply_text("📭 Больше нет обращений.", reply_markup=admin_menu_keyboard())
    elif data == "back_to_admin":
        await query.message.delete()
        await query.message.reply_text("🔙 Возврат в админ-панель.", reply_markup=admin_menu_keyboard())
    return ConversationHandler.END

# ---------- Ответ на обращение (из диалога) ----------
async def reply_to_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_text = update.message.text
    msg_id = context.user_data.get("reply_to")
    if not msg_id:
        await update.message.reply_text("❌ Нет обращения для ответа.")
        context.user_data["in_conversation"] = False
        return ConversationHandler.END

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT user_id FROM support_messages WHERE id = ?', (msg_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("❌ Обращение не найдено.")
        context.user_data["in_conversation"] = False
        return ConversationHandler.END

    user_id = row[0]
    try:
        await context.bot.send_message(chat_id=user_id, text=f"📨 Ответ поддержки:\n{reply_text}")
        mark_answered(msg_id)
        await update.message.reply_text("✅ Ответ отправлен.", reply_markup=admin_menu_keyboard())
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка отправки: {e}")
    context.user_data["in_conversation"] = False
    return ConversationHandler.END

# ---------- Пересылка из каналов (только с хэштегами) ----------
async def forward_from_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_post = update.channel_post
    if not channel_post:
        return
    chat_id = channel_post.chat_id
    sources = get_source_channels()
    source_ids = [s[0] for s in sources]
    if chat_id not in source_ids:
        return

    text = channel_post.text or channel_post.caption or ""
    if not any(tag in text for tag in ["#MatchDay", "#Results", "#rplpuck"]):
        return

    targets = get_target_chats()
    for target_id, _ in targets:
        try:
            await channel_post.copy(chat_id=target_id)
            logger.info(f"Переслано из {chat_id} в {target_id}")
        except Exception as e:
            logger.error(f"Ошибка пересылки в {target_id}: {e}")

# ---------- Автоудаление неизвестных сообщений ----------
async def handle_unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Игнорируем, если чат не личный
    if update.effective_chat.type != "private":
        return

    # Если сообщение начинается с "/" – пропускаем (это команды)
    if update.message.text and update.message.text.startswith('/'):
        return

    # Если активен диалог – ничего не делаем
    if context.user_data.get("in_conversation", False):
        return

    # Если текст совпадает с кнопками (и главного меню, и админского) – пропускаем
    text = update.message.text
    if text in ["🏠 Главное меню", "➕ Добавить каналы", "➕ Добавить чаты", "📩 Проверить поддержку", "⚙️ Настройки", "🚪 Выйти"]:
        return

    # Удаляем сообщение пользователя и отправляем ошибку, через 3 секунды удаляем оба
    try:
        user_msg = update.message
        error_msg = await update.message.reply_text("❌ Ошибка! Не выбран модуль запроса. Попробуйте снова.")
        await asyncio.sleep(3)
        await user_msg.delete()
        await error_msg.delete()
    except Exception as e:
        logger.error(f"Ошибка удаления: {e}")

# ---------- Команда /getid ----------
async def getid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(f"🆔 ID этого чата: `{chat.id}`", parse_mode="Markdown")

# ---------- MAIN ----------
def main():
    app = Application.builder().token(TOKEN).build()

    # Диалог авторизации админа
    conv_auth = ConversationHandler(
        entry_points=[CommandHandler("adminkarpl", adminkarpl)],
        states={
            WAITING_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, wait_login)],
            WAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, wait_password)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: u.message.reply_text("Отменено."))],
        allow_reentry=True,
    )
    app.add_handler(conv_auth)

    # Диалог добавления канала (вход – кнопка админ-меню)
    conv_channel = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить каналы$") & filters.ChatType.PRIVATE, admin_buttons)],
        states={
            WAITING_CHANNEL_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_channel_username)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: u.message.reply_text("Отменено."))],
        allow_reentry=True,
    )
    app.add_handler(conv_channel)

    # Диалог добавления чата
    conv_chat = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить чаты$") & filters.ChatType.PRIVATE, admin_buttons)],
        states={
            WAITING_CHAT_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_chat_link)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: u.message.reply_text("Отменено."))],
        allow_reentry=True,
    )
    app.add_handler(conv_chat)

    # Диалог ответа на обращение (из callback админа)
    conv_reply = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern="^reply_")],
        states={
            WAITING_REPLY_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reply_to_support)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: u.message.reply_text("Отменено."))],
        allow_reentry=True,
    )
    app.add_handler(conv_reply)

    # Диалог поддержки (из inline-кнопки)
    conv_support = ConversationHandler(
        entry_points=[CallbackQueryHandler(inline_callback, pattern="^support$")],
        states={
            WAITING_SUPPORT_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, support_receive)],
        },
        fallbacks=[CommandHandler("cancel", support_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv_support)

    # Обработчики остальных кнопок админ-меню (без диалогов)
    app.add_handler(MessageHandler(filters.Regex("^📩 Проверить поддержку$") & filters.ChatType.PRIVATE, admin_buttons))
    app.add_handler(MessageHandler(filters.Regex("^⚙️ Настройки$") & filters.ChatType.PRIVATE, admin_buttons))
    app.add_handler(MessageHandler(filters.Regex("^🚪 Выйти$") & filters.ChatType.PRIVATE, admin_buttons))

    # Обработчик кнопки "Главное меню"
    app.add_handler(MessageHandler(filters.Regex("^🏠 Главное меню$") & filters.ChatType.PRIVATE, main_menu))

    # Inline-колбэки для главного меню (кроме support, который уже в диалоге)
    app.add_handler(CallbackQueryHandler(inline_callback, pattern="^(discord|website)$"))

    # Inline-колбэки админа (close, next, back)
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^(close_|next_support|back_to_admin)$"))

    # Пересылка из каналов
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, forward_from_channels))

    # Автоудаление неизвестных сообщений (группа 999 – низкий приоритет)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_unknown_message), group=999)

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getid", getid))
    app.add_handler(CommandHandler("cancel", lambda u,c: u.message.reply_text("Отменено.")))

    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
