import os
import logging
import sqlite3
import asyncio
import random
import uuid
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    BotCommand,
    BotCommandScopeDefault,
)
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

DB_PATH = os.getenv("DB_PATH", "bot_data.db")
ADMIN_SESSION_MINUTES = 30

# Состояния ConversationHandler
(
    WAITING_LOGIN,
    WAITING_PASSWORD,
    WAITING_CHANNEL_USERNAME,
    WAITING_CHAT_LINK,
    WAITING_REPLY_TEXT,
    WAITING_SUPPORT_MSG,
) = range(6)
WAITING_DUEL_SHOT = 10
WAITING_GIF_UPLOAD = 11
WAITING_GAME_SETTINGS = 12

# Настройки «Дуэли Клюшек»
STICK_DUEL_SEARCH_SECONDS = 45
STICK_DUEL_TOTAL_TURNS = 6  # По 3 буллита каждому
INITIAL_MMR = 1000
BULLET_COOLDOWN_SECONDS = 5 * 60  # КД 5 минут

# Глобальное состояние игр храним в памяти
stick_duel_searches = {}  # user_id -> search dict
stick_duel_games = {}     # game_id -> game dict
stick_duel_by_user = {}   # user_id -> game_id
stick_duel_lock = asyncio.Lock()

SHOT_LABELS = {
    "right": "Правая девятка",
    "left": "Левая девятка",
    "home": "Домик",
}
SAVE_LABELS = {
    "nines": "Девятки",
    "home": "Домик",
}


# ---------- БД ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS source_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER UNIQUE,
        username TEXT,
        added_by INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS target_chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER UNIQUE,
        link TEXT,
        added_by INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS support_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        text TEXT,
        timestamp TEXT,
        answered INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        last_activity INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS duel_stats (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        total_shots INTEGER DEFAULT 0,
        goals INTEGER DEFAULT 0,
        last_bullet_time INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS mmr_stats (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        mmr INTEGER DEFAULT 1000,
        games_played INTEGER DEFAULT 0
    )''')

    # Миграция: добавляем last_bullet_time в duel_stats, если нет
    c.execute("PRAGMA table_info(duel_stats)")
    columns = [col[1] for col in c.fetchall()]
    if "last_bullet_time" not in columns:
        c.execute("ALTER TABLE duel_stats ADD COLUMN last_bullet_time INTEGER DEFAULT 0")

    # Инициализация GIF конфигов (3 для голов, 3 для сейвов)
    for i in range(1, 4):
        c.execute('INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)', (f'gif_goal_{i}', ''))
        c.execute('INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)', (f'gif_save_{i}', ''))

    conn.commit()
    conn.close()


init_db()


def update_duel_stats(user_id, username, first_name, is_goal):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    goal_inc = 1 if is_goal else 0
    now_ts = int(datetime.now().timestamp())
    c.execute('''INSERT INTO duel_stats (user_id, username, first_name, total_shots, goals, last_bullet_time)
                 VALUES (?, ?, ?, 1, ?, ?)
                 ON CONFLICT(user_id) DO UPDATE SET
                 username = excluded.username,
                 first_name = excluded.first_name,
                 total_shots = total_shots + 1,
                 goals = goals + ?,
                 last_bullet_time = ?''',
              (user_id, username, first_name, goal_inc, now_ts, goal_inc, now_ts))
    conn.commit()
    conn.close()


def check_bullet_cooldown(user_id):
    """Проверяет кулдаун на Дуэль Буллитов (5 минут)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT last_bullet_time FROM duel_stats WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        return True, 0

    elapsed = int(datetime.now().timestamp()) - row[0]
    if elapsed < BULLET_COOLDOWN_SECONDS:
        return False, BULLET_COOLDOWN_SECONDS - elapsed
    return True, 0


def get_top_rating():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT first_name, username, goals, total_shots,
                 (CAST(goals AS FLOAT) / total_shots * 100) AS percent
                 FROM duel_stats
                 WHERE total_shots >= 7
                 ORDER BY percent DESC LIMIT 10''')
    rows = c.fetchall()
    conn.close()
    return rows


def reset_bullet_duel_ratings():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM duel_stats')
    conn.commit()
    conn.close()


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


def get_all_gifs(prefix):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    gifs = []
    for i in range(1, 4):
        c.execute('SELECT value FROM bot_config WHERE key = ?', (f'{prefix}_{i}',))
        row = c.fetchone()
        if row and row[0]:
            gifs.append(row[0])
    conn.close()
    return gifs if gifs else None


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
    msg_id = c.lastrowid
    conn.close()
    return msg_id


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
        remove_admin(user_id)
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
    credentials = {"goyda1488": "goydarpl", "rzk1488": "rzksigma"}
    return credentials.get(login) == password


# ---------- MMR функции ----------
def get_mmr_user(user_id, username, first_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT mmr, games_played FROM mmr_stats WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    if row:
        mmr, games_played = row
    else:
        mmr, games_played = INITIAL_MMR, 0
        c.execute('INSERT INTO mmr_stats (user_id, username, first_name, mmr, games_played) VALUES (?, ?, ?, ?, ?)',
                  (user_id, username, first_name, mmr, games_played))
        conn.commit()
    conn.close()
    return {"mmr": mmr, "games_played": games_played}


def update_mmr(user_id, username, first_name, mmr_change):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO mmr_stats (user_id, username, first_name, mmr, games_played)
                 VALUES (?, ?, ?, ?, 1)
                 ON CONFLICT(user_id) DO UPDATE SET
                 username = excluded.username,
                 first_name = excluded.first_name,
                 mmr = mmr + ?,
                 games_played = games_played + 1''',
              (user_id, username, first_name, INITIAL_MMR + mmr_change, mmr_change))
    conn.commit()
    conn.close()


def calculate_mmr_delta(winner_mmr, loser_mmr):
    win_delta = round(15 + (1000 - winner_mmr) / 20)
    winner_gain = max(5, min(35, win_delta))

    loss_delta = round(15 + (loser_mmr - 1000) / 20)
    loser_loss = max(5, min(35, loss_delta))

    return winner_gain, loser_loss


def update_stick_duel_mmr_stats(winner_player_data, loser_player_data):
    if winner_player_data["is_bot"] or loser_player_data["is_bot"]:
        return 0, 0

    winner_id = winner_player_data["id"]
    winner_username = winner_player_data.get("username")
    winner_name = winner_player_data["name"]

    loser_id = loser_player_data["id"]
    loser_username = loser_player_data.get("username")
    loser_name = loser_player_data["name"]

    winner_mmr = get_mmr_user(winner_id, winner_username, winner_name)["mmr"]
    loser_mmr = get_mmr_user(loser_id, loser_username, loser_name)["mmr"]

    w_gain, l_loss = calculate_mmr_delta(winner_mmr, loser_mmr)

    update_mmr(winner_id, winner_username, winner_name, w_gain)
    update_mmr(loser_id, loser_username, loser_name, -l_loss)

    return w_gain, l_loss


def get_top_mmr_rating():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT first_name, username, mmr, games_played
                 FROM mmr_stats
                 WHERE games_played > 0
                 ORDER BY mmr DESC LIMIT 10''')
    rows = c.fetchall()
    conn.close()
    return rows


def reset_stick_duel_mmr_ratings():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM mmr_stats')
    conn.commit()
    conn.close()


# ---------- Клавиатуры ----------
def main_menu_keyboard():
    return ReplyKeyboardMarkup([["🏠 Главное меню"]], resize_keyboard=True)


def admin_menu_keyboard():
    return ReplyKeyboardMarkup([
        ["➕ Добавить каналы", "➕ Добавить чаты"],
        ["📩 Проверить поддержку", "⚙️ Настройки"],
        ["🎮 Настройки игры", "🚪 Выйти"],
    ], resize_keyboard=True)


def welcome_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Наш Discord", callback_data="discord")],
        [InlineKeyboardButton("🌐 Наш Сайт", callback_data="website")],
        [InlineKeyboardButton("🆘 Обратиться в поддержку", callback_data="support")],
        [InlineKeyboardButton("🏒 Дуэль Буллитов", callback_data="duel")],
        [InlineKeyboardButton("⚔️ Дуэль Клюшек", callback_data="stick_duel")],
        [InlineKeyboardButton("👤 Профиль", callback_data="profile")],
    ])


def duel_shot_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🥅 Левая девятка", callback_data="shot_left")],
        [InlineKeyboardButton("🥅 Правая девятка", callback_data="shot_right")],
        [InlineKeyboardButton("🧤 Домик", callback_data="shot_five")],
        [InlineKeyboardButton("🥅 Низ в угол", callback_data="shot_low")],
    ])


def stick_search_keyboard(owner_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Принять", callback_data=f"rpl_accept:{owner_id}")],
        [InlineKeyboardButton("🤖 Играть с ИИ", callback_data=f"rpl_ai:{owner_id}")],
        [InlineKeyboardButton("❌ Отменить поиск", callback_data=f"rpl_cancel:{owner_id}")],
    ])


def stick_shot_keyboard(game_id, turn):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Правая девятка", callback_data=f"rpl_shot:{game_id}:{turn}:right")],
        [InlineKeyboardButton("Левая девятка", callback_data=f"rpl_shot:{game_id}:{turn}:left")],
        [InlineKeyboardButton("Домик", callback_data=f"rpl_shot:{game_id}:{turn}:home")],
    ])


def stick_save_keyboard(game_id, turn):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Девятки", callback_data=f"rpl_save:{game_id}:{turn}:nines")],
        [InlineKeyboardButton("Домик", callback_data=f"rpl_save:{game_id}:{turn}:home")],
    ])


def game_settings_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Настройки Дуэли Буллитов", callback_data="game_settings_bullet")],
        [InlineKeyboardButton("Настройки Дуэли Клюшек", callback_data="game_settings_stick")],
        [InlineKeyboardButton("⬅️ Назад в Админ-панель", callback_data="admin_back_to_main")],
    ])


def bullet_duel_settings_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("GOAL GIF 1", callback_data="set_gif_goal_1"),
            InlineKeyboardButton("GOAL GIF 2", callback_data="set_gif_goal_2"),
            InlineKeyboardButton("GOAL GIF 3", callback_data="set_gif_goal_3"),
        ],
        [
            InlineKeyboardButton("SAVE GIF 1", callback_data="set_gif_save_1"),
            InlineKeyboardButton("SAVE GIF 2", callback_data="set_gif_save_2"),
            InlineKeyboardButton("SAVE GIF 3", callback_data="set_gif_save_3"),
        ],
        [InlineKeyboardButton("♻️ Обнулить рейтинг «Дуэли Буллитов»", callback_data="reset_bullet_rating")],
        [InlineKeyboardButton("⬅️ Назад в Настройки игр", callback_data="game_settings_back")],
    ])


def stick_duel_settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("♻️ Обнулить рейтинг MMR «Дуэли Клюшек»", callback_data="reset_stick_mmr_rating")],
        [InlineKeyboardButton("⬅️ Назад в Настройки игр", callback_data="game_settings_back")],
    ])


# ---------- Общие обработчики ----------
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
        user = query.from_user
        can_play, remaining = check_bullet_cooldown(user.id)
        if not can_play:
            m = remaining // 60
            s = remaining % 60
            await query.edit_message_text(f"⏳ Кулдаун на Дуэль Буллитов!\nПодожди ещё {m} мин {s} сек.")
            await query.message.reply_text("📌 Выберите другой раздел:", reply_markup=welcome_inline_keyboard())
            return
        await query.edit_message_text("🏒 Дуэль Буллитов! Выбери зону для броска:", reply_markup=duel_shot_keyboard())
        return WAITING_DUEL_SHOT
    elif data == "stick_duel":
        await regrpl_command(update, context)
    elif data == "profile":
        await profile_command(update, context)


# ---------- Профиль пользователя (ОБНОВЛЕНО) ----------
async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('SELECT goals, total_shots FROM duel_stats WHERE user_id = ?', (user.id,))
    bullet_row = c.fetchone()

    c.execute('SELECT mmr, games_played FROM mmr_stats WHERE user_id = ?', (user.id,))
    mmr_row = c.fetchone()
    conn.close()

    lines = [f"👤 **ПРОФИЛЬ ИГРОКА**", f"Ник: {user.first_name}" + (f" (@{user.username})" if user.username else "")]
    lines.append(f"🆔 ID: `{user.id}`")

    # Пункт 1: % забитых голов (если менее 7 бросков - не показывает)
    if bullet_row and bullet_row[1] >= 7:
        goals, total = bullet_row
        percent = (goals / total) * 100
        lines.append(f"🏒 **Дуэль Буллитов:** {percent:.1f}% забитых ({goals}/{total})")

    # Пункт 2: MMR в Дуэли клюшек (если не играл - не показывает)
    if mmr_row and mmr_row[1] > 0:
        mmr_val, g_played = mmr_row
        lines.append(f"⚔️ **Дуэль Клюшек (MMR):** {mmr_val} ({g_played} игр)")

        if mmr_val >= 1200:
            rank = "🏆 Легенда RPL"
        elif mmr_val >= 1050:
            rank = "⚡️ Мастер Лиги"
        elif mmr_val >= 950:
            rank = "🏒 Игрок RPL"
        else:
            rank = "🐣 Новичок"
        lines.append(f"🎖 **Звание:** {rank}")

    can_play, remaining = check_bullet_cooldown(user.id)
    if can_play:
        lines.append("⏱ **Дуэль Буллитов:** Готова к игре!")
    else:
        m, s = remaining // 60, remaining % 60
        lines.append(f"⏱ **Дуэль Буллитов:** Перезарядка ({m}м {s}с)")

    text = "\n".join(lines)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
        await update.callback_query.message.reply_text("📌 Выберите другой раздел:", reply_markup=welcome_inline_keyboard())
    else:
        await update.message.reply_text(text, parse_mode="Markdown")


# ---------- Старая одиночная «Дуэль Буллитов» ----------
async def start_duel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    can_play, remaining = check_bullet_cooldown(user.id)
    if not can_play:
        m = remaining // 60
        s = remaining % 60
        await update.message.reply_text(f"⏳ Кулдаун на Дуэль Буллитов!\nПодожди ещё {m} мин {s} сек.")
        return ConversationHandler.END

    if update.effective_chat.type != "private":
        context.user_data[f"in_duel_{user.id}"] = True
        await update.message.reply_text(
            f"🏒 {user.first_name}, твоя очередь! Выбери зону:",
            reply_markup=duel_shot_keyboard(),
        )
    else:
        await update.message.reply_text("🏒 Дуэль Буллитов! Выбери зону:", reply_markup=duel_shot_keyboard())
    return WAITING_DUEL_SHOT


async def rating_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = get_top_rating()
    if not top:
        await update.message.reply_text("📊 Рейтинг Дуэли Буллитов пуст. Нужно минимум 7 бросков для попадания в ТОП!")
        return

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    text = "🏆 **ТОП-10 ИГРОКОВ RPL (Дуэль Буллитов)**\n\n"
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

    update_duel_stats(user.id, user.username, user.first_name, scored)

    if scored:
        gifs = get_all_gifs('gif_goal')
        gif = random.choice(gifs) if gifs else "https://media.giphy.com/media/3o7aTskHEUdgCQAXde/giphy.gif"
        result_text = "⚡️ **ГОЛ!** Вы точно попали в девятку!"
    else:
        gifs = get_all_gifs('gif_save')
        gif = random.choice(gifs) if gifs else "https://media.giphy.com/media/3o6Ztq5cG6GZj5F9uo/giphy.gif"
        result_text = "🧤 **СЕЙВ!** Вратарь отразил бросок!"

    await query.edit_message_text(
        f"{result_text}\n"
        f"Ваш бросок: {shot_zone.replace('shot_', '').capitalize()}\n"
        f"Вратарь выбрал: {goalie_choice.replace('shot_', '').capitalize()}"
    )

    try:
        await query.message.reply_animation(gif)
    except Exception as e:
        logger.error("Ошибка отправки GIF: %s", e)

    if update.effective_chat.type != "private":
        context.user_data.pop(f"in_duel_{user.id}", None)

    if update.effective_chat.type == "private":
        await query.message.reply_text("📌 Выберите другой раздел:", reply_markup=welcome_inline_keyboard())

    return ConversationHandler.END


# ---------- Новая мини-игра «Дуэль Клюшек» (С задержкой 5 сек на удаление) ----------
def stick_player(user, chat_id):
    return {
        "id": user.id,
        "name": user.first_name or user.username or str(user.id),
        "username": user.username,
        "chat_id": chat_id,
        "is_bot": False,
    }


def stick_ai_player():
    return {
        "id": 0,
        "name": "ИИ Вратарь",
        "username": None,
        "chat_id": None,
        "is_bot": True,
    }


def cancel_search_task(search):
    task = search.get("task")
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()


async def safe_delete_and_send(bot, chat_id, old_msg_id, text, reply_markup=None):
    """Удаляет старое сообщение через 5 секунд и отправляет новое."""
    if old_msg_id:
        async def delayed_delete():
            await asyncio.sleep(5)
            try:
                await bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
            except Exception as e:
                logger.debug("Не удалось удалить старое сообщение %s в %s: %s", old_msg_id, chat_id, e)
        asyncio.create_task(delayed_delete())

    try:
        new_msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return new_msg.message_id
    except Exception as e:
        logger.error("Не удалось отправить новое сообщение в %s: %s", chat_id, e)
        return None


async def send_to_stick_game(bot, game, text, reply_markup=None):
    if "chat_msg_ids" not in game:
        game["chat_msg_ids"] = {}

    sent_chats = set()
    for player in game["players"]:
        chat_id = player.get("chat_id")
        if player["is_bot"] or chat_id is None or chat_id in sent_chats:
            continue
        sent_chats.add(chat_id)

        old_id = game["chat_msg_ids"].get(chat_id)
        current_markup = reply_markup if game.get("stage") != "finished" else None
        new_id = await safe_delete_and_send(bot, chat_id, old_id, text, current_markup)
        if new_id:
            game["chat_msg_ids"][chat_id] = new_id


async def begin_stick_game_locked(bot, first_player, second_player, searches):
    for search in searches:
        cancel_search_task(search)
        old_id = search.get("message_id")
        chat_id = search.get("chat_id")
        if old_id and chat_id:
            async def delayed_search_delete():
                await asyncio.sleep(5)
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=old_id)
                except Exception:
                    pass
            asyncio.create_task(delayed_search_delete())

    game_id = uuid.uuid4().hex[:12]
    game = {
        "id": game_id,
        "players": [first_player, second_player],
        "turn": 0,
        "stage": "starting",
        "shot": None,
        "score": [0, 0],
        "chat_msg_ids": {},
    }
    stick_duel_games[game_id] = game

    for player in game["players"]:
        if not player["is_bot"]:
            stick_duel_by_user[player["id"]] = game_id

    match_text = (
        "🏒 Соперник найден! Начинается «Дуэль Клюшек».\n"
        f"{first_player['name']} против {second_player['name']}.\n"
        f"Первым бросает {first_player['name']}."
    )

    await send_to_stick_game(bot, game, match_text)
    await send_stick_turn_locked(bot, game)


async def finish_stick_game_locked(bot, game):
    p1, p2 = game["players"]
    score1, score2 = game["score"]

    winner_player = None
    loser_player = None

    if score1 > score2:
        ending = f"🏆 Победитель: {p1['name']}!"
        winner_player, loser_player = p1, p2
    elif score2 > score1:
        ending = f"🏆 Победитель: {p2['name']}!"
        winner_player, loser_player = p2, p1
    else:
        ending = "🤝 Ничья!"

    mmr_text = ""
    if winner_player and loser_player and not winner_player["is_bot"] and not loser_player["is_bot"]:
        w_gain, l_loss = update_stick_duel_mmr_stats(winner_player, loser_player)
        w_info = get_mmr_user(winner_player['id'], winner_player.get('username'), winner_player['name'])
        l_info = get_mmr_user(loser_player['id'], loser_player.get('username'), loser_player['name'])
        mmr_text = (
            f"\n\n📊 **Изменение MMR:**\n"
            f"🏆 {winner_player['name']}: +{w_gain} MMR (Всего: {w_info['mmr']})\n"
            f"📉 {loser_player['name']}: -{l_loss} MMR (Всего: {l_info['mmr']})"
        )

    text = (
        "🏁 Дуэль Клюшек завершена!\n\n"
        f"Итоговый счёт: {p1['name']} {score1}:{score2} {p2['name']}\n"
        f"{ending}"
        f"{mmr_text}"
    )
    game["stage"] = "finished"
    await send_to_stick_game(bot, game, text)

    for player in game["players"]:
        if not player["is_bot"]:
            stick_duel_by_user.pop(player["id"], None)
    stick_duel_games.pop(game["id"], None)


async def resolve_stick_shot_locked(bot, game, save_choice):
    attacker_index = game["turn"] % 2
    goalie_index = 1 - attacker_index
    attacker = game["players"][attacker_index]
    goalie = game["players"][goalie_index]
    shot = game["shot"]

    saved = (
        (save_choice == "nines" and shot in ("right", "left"))
        or (save_choice == "home" and shot == "home")
    )
    is_goal = not saved

    if is_goal:
        game["score"][attacker_index] += 1
        result = "🚨 ГОЛ!"
    else:
        result = "🧤 СЕЙВ!"

    if not attacker["is_bot"]:
        update_duel_stats(
            attacker["id"],
            attacker.get("username"),
            attacker["name"],
            is_goal,
        )

    score1, score2 = game["score"]
    p1, p2 = game["players"]
    text = (
        f"{result}\n"
        f"Нападающий {attacker['name']}: {SHOT_LABELS[shot]}.\n"
        f"Вратарь {goalie['name']}: {SAVE_LABELS[save_choice]}.\n\n"
        f"Счёт: {p1['name']} {score1}:{score2} {p2['name']}"
    )
    await send_to_stick_game(bot, game, text)

    game["turn"] += 1
    game["shot"] = None
    if game["turn"] >= STICK_DUEL_TOTAL_TURNS:
        await finish_stick_game_locked(bot, game)
    else:
        await send_stick_turn_locked(bot, game)


async def send_stick_turn_locked(bot, game):
    turn = game["turn"]
    attacker_index = turn % 2
    goalie_index = 1 - attacker_index
    attacker = game["players"][attacker_index]
    goalie = game["players"][goalie_index]
    personal_shot_number = turn // 2 + 1

    header = (
        f"🏒 Буллит {personal_shot_number}/3 для {attacker['name']}\n"
        f"Вратарь — {goalie['name']}, Нападающий — {attacker['name']}\n\n"
    )

    if attacker["is_bot"]:
        game["shot"] = random.choice(list(SHOT_LABELS.keys()))
        game["stage"] = "save"
        await send_to_stick_game(
            bot,
            game,
            header
            + f"Сейчас {attacker['name']} выбирает, куда бросить.\n"
            + f"Сейчас {goalie['name']} выбирает, как отбить.",
            reply_markup=stick_save_keyboard(game["id"], turn),
        )
    else:
        game["stage"] = "shot"
        await send_to_stick_game(
            bot,
            game,
            header + f"Сейчас {attacker['name']} выбирает, куда бросить.",
            reply_markup=stick_shot_keyboard(game["id"], turn),
        )


async def stick_search_timeout(application, owner_id):
    try:
        await asyncio.sleep(STICK_DUEL_SEARCH_SECONDS)
        async with stick_duel_lock:
            search = stick_duel_searches.pop(owner_id, None)
            if not search:
                return

            player = search["player"]
            await begin_stick_game_locked(
                application.bot,
                player,
                stick_ai_player(),
                [search],
            )
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Ошибка таймера поиска Дуэли Клюшек")


async def regrpl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    async with stick_duel_lock:
        if user.id in stick_duel_by_user:
            msg = "⚠️ Ты уже участвуешь в Дуэли Клюшек."
            if update.callback_query:
                await update.callback_query.answer(msg, show_alert=True)
            else:
                await update.message.reply_text(msg)
            return

        if user.id in stick_duel_searches:
            msg = "🔎 Ты уже ищешь соперника."
            if update.callback_query:
                await update.callback_query.answer(msg, show_alert=True)
            else:
                await update.message.reply_text(msg)
            return

        opponent_id = next((uid for uid in stick_duel_searches if uid != user.id), None)
        if opponent_id is not None:
            opponent_search = stick_duel_searches.pop(opponent_id)
            cancel_search_task(opponent_search)
            await begin_stick_game_locked(
                context.bot,
                opponent_search["player"],
                stick_player(user, chat_id),
                [opponent_search],
            )
            return

        text = (
            f"🔎 {user.first_name}, начат поиск соперника!\n"
            f"Ожидание: {STICK_DUEL_SEARCH_SECONDS} секунд.\n"
            "Если никто не найдётся, игра начнётся с ИИ."
        )

        if update.callback_query:
            await update.callback_query.answer()
            msg = await update.callback_query.message.reply_text(
                text, reply_markup=stick_search_keyboard(user.id)
            )
        else:
            msg = await update.message.reply_text(
                text, reply_markup=stick_search_keyboard(user.id)
            )

        search = {
            "player": stick_player(user, chat_id),
            "chat_id": chat_id,
            "message_id": msg.message_id,
            "task": None,
        }
        stick_duel_searches[user.id] = search
        search["task"] = asyncio.create_task(stick_search_timeout(context.application, user.id))


async def stick_duel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user = query.from_user

    async with stick_duel_lock:
        if data.startswith("rpl_accept:"):
            try:
                owner_id = int(data.split(":", 1)[1])
            except ValueError:
                await query.answer("Ошибка поиска.", show_alert=True)
                return

            search = stick_duel_searches.get(owner_id)
            if not search:
                await query.answer("Этот поиск уже завершён.", show_alert=True)
                return
            if user.id == owner_id:
                await query.answer("Нельзя принять собственный поиск 🙂", show_alert=True)
                return
            if user.id in stick_duel_by_user:
                await query.answer("Ты уже участвуешь в другой игре.", show_alert=True)
                return

            await query.answer("Принято!")
            stick_duel_searches.pop(owner_id, None)
            cancel_search_task(search)
            searches = [search]

            own_search = stick_duel_searches.pop(user.id, None)
            if own_search:
                cancel_search_task(own_search)
                searches.append(own_search)

            await begin_stick_game_locked(
                context.bot,
                search["player"],
                stick_player(user, query.message.chat.id),
                searches,
            )
            return

        if data.startswith("rpl_ai:"):
            try:
                owner_id = int(data.split(":", 1)[1])
            except ValueError:
                await query.answer("Ошибка поиска.", show_alert=True)
                return

            if user.id != owner_id:
                await query.answer("Игру с ИИ может запустить только автор поиска.", show_alert=True)
                return

            search = stick_duel_searches.pop(owner_id, None)
            if not search:
                await query.answer("Этот поиск уже завершён.", show_alert=True)
                return

            await query.answer("Старт с ИИ!")
            cancel_search_task(search)
            await begin_stick_game_locked(
                context.bot,
                search["player"],
                stick_ai_player(),
                [search],
            )
            return

        if data.startswith("rpl_cancel:"):
            try:
                owner_id = int(data.split(":", 1)[1])
            except ValueError:
                await query.answer("Ошибка поиска.", show_alert=True)
                return

            if user.id != owner_id:
                await query.answer("Отменить поиск может только его автор.", show_alert=True)
                return

            search = stick_duel_searches.pop(owner_id, None)
            if not search:
                await query.answer("Этот поиск уже завершён.", show_alert=True)
                return

            await query.answer("Отменено!")
            cancel_search_task(search)
            
            async def delayed_cancel_delete():
                await asyncio.sleep(5)
                try:
                    await context.bot.delete_message(chat_id=search["chat_id"], message_id=search["message_id"])
                except Exception:
                    pass
            asyncio.create_task(delayed_cancel_delete())

            await context.bot.send_message(chat_id=search["chat_id"], text="❌ Поиск соперника отменён.")
            return

        parts = data.split(":")
        if len(parts) != 4 or parts[0] not in ("rpl_shot", "rpl_save"):
            await query.answer("Некорректное действие.", show_alert=True)
            return

        action, game_id, turn_text, choice = parts
        game = stick_duel_games.get(game_id)
        if not game:
            await query.answer("Эта игра уже завершена.", show_alert=True)
            return

        try:
            button_turn = int(turn_text)
        except ValueError:
            await query.answer("Ошибка хода.", show_alert=True)
            return

        if button_turn != game["turn"]:
            await query.answer("Эта кнопка устарела.", show_alert=True)
            return

        attacker_index = game["turn"] % 2
        goalie_index = 1 - attacker_index
        attacker = game["players"][attacker_index]
        goalie = game["players"][goalie_index]

        if action == "rpl_shot":
            if game["stage"] != "shot":
                await query.answer("Сейчас не этап броска.", show_alert=True)
                return
            if user.id != attacker["id"]:
                await query.answer(f"Сейчас бросает {attacker['name']}.", show_alert=True)
                return
            if choice not in SHOT_LABELS:
                await query.answer("Неизвестное направление.", show_alert=True)
                return

            await query.answer("Направление выбрано!")
            game["shot"] = choice
            game["stage"] = "save"

            header = (
                f"Вратарь — {goalie['name']}, Нападающий — {attacker['name']}\n\n"
                f"{attacker['name']} выбрал направление броска.\n"
                f"Сейчас {goalie['name']} выбирает, как отбить."
            )

            if goalie["is_bot"]:
                await send_to_stick_game(context.bot, game, header)

                if random.random() < 0.8:
                    ai_save = "nines" if choice in ("right", "left") else "home"
                else:
                    ai_save = "home" if choice in ("right", "left") else "nines"

                await resolve_stick_shot_locked(context.bot, game, ai_save)
            else:
                await send_to_stick_game(
                    context.bot,
                    game,
                    header,
                    reply_markup=stick_save_keyboard(game_id, game["turn"]),
                )
            return

        if action == "rpl_save":
            if game["stage"] != "save":
                await query.answer("Сейчас не этап сейва.", show_alert=True)
                return
            if user.id != goalie["id"]:
                await query.answer(f"Сейчас отбивает {goalie['name']}.", show_alert=True)
                return
            if choice not in SAVE_LABELS:
                await query.answer("Неизвестный сейв.", show_alert=True)
                return

            await query.answer("Сейв выбран!")
            game["stage"] = "resolving"
            await resolve_stick_shot_locked(context.bot, game, choice)
            return


async def mymmr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    mmr_info = get_mmr_user(user.id, user.username, user.first_name)
    await update.message.reply_text(
        f"📊 Твой MMR в Дуэли Клюшек: {mmr_info['mmr']}\n"
        f"Сыграно матчей: {mmr_info['games_played']}"
    )


async def ratingmmr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = get_top_mmr_rating()
    if not top:
        await update.message.reply_text("📊 Рейтинг MMR Дуэли Клюшек пуст. Нужно сыграть хотя бы 1 матч.")
        return

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    text = "🏆 **ТОП-10 ИГРОКОВ RPL (MMR Дуэли Клюшек)**\n\n"
    for i, row in enumerate(top):
        first_name, username, mmr, games_played = row
        user_label = f"(@{username})" if username else ""
        text += f"{medals[i]} {first_name}{user_label}: {mmr} MMR ({games_played} матчей)\n"

    await update.message.reply_text(text, parse_mode="Markdown")


# ---------- Админ-панель (ИСПРАВЛЕН ВХОД) ----------
async def adminkarpl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return ConversationHandler.END
    if is_admin(update.effective_user.id):
        await update.message.reply_text("✅ Вы уже в админ-панели", reply_markup=admin_menu_keyboard())
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("🔑 Введите логин:")
    return WAITING_LOGIN


async def wait_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["admin_login"] = update.message.text.strip()
    await update.message.reply_text("🔒 Введите пароль:")
    return WAITING_PASSWORD


async def wait_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    login = context.user_data.pop("admin_login", None)
    password = update.message.text.strip()
    
    if login and check_credentials(login, password):
        add_admin(update.effective_user.id)
        await update.message.reply_text("✅ Авторизован!", reply_markup=admin_menu_keyboard())
        return ConversationHandler.END
    else:
        await update.message.reply_text("❌ Неверный логин или пароль. Попробуйте снова /adminkarpl")
        return ConversationHandler.END


async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
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
        await update.message.reply_text("⚙️ Выберите настройки для игры:", reply_markup=game_settings_main_keyboard())
        return WAITING_GAME_SETTINGS
    elif text == "🚪 Выйти":
        remove_admin(user_id)
        await update.message.reply_text("🚪 Вышли", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def add_channel_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip()
    if not username.startswith('@'):
        username = '@' + username
    try:
        chat = await context.bot.get_chat(username)
        add_source_channel(chat.id, username, update.effective_user.id)
        await update.message.reply_text("✅ Добавлен", reply_markup=admin_menu_keyboard())
    except Exception:
        await update.message.reply_text("❌ Ошибка")
    return ConversationHandler.END


async def add_chat_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text.strip()
    try:
        add_target_chat(int(link), link, update.effective_user.id)
        await update.message.reply_text("✅ Добавлен", reply_markup=admin_menu_keyboard())
    except Exception:
        await update.message.reply_text("❌ Ошибка")
    return ConversationHandler.END


async def support_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_support_message(user.id, user.username or str(user.id), update.message.text)
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
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Ответить", callback_data=f"reply_{m[0]}"),
        InlineKeyboardButton("❌ Закрыть", callback_data=f"close_{m[0]}"),
    ]])
    await update.message.reply_text(f"📩 #{m[0]} от {m[2]}:\n\n{m[3]}", reply_markup=kb)


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sources = get_source_channels()
    targets = get_target_chats()
    text = (
        "📋 Настройки:\n\n📢 Источники:\n"
        + "\n".join([f"- {s[1]}" for s in sources])
        + "\n\n📥 Чаты:\n"
        + "\n".join([f"- {t[1]}" for t in targets])
    )
    await update.message.reply_text(text, reply_markup=admin_menu_keyboard())


async def show_bullet_duel_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    gifs_goal = [get_config(f'gif_goal_{i}') for i in range(1, 4)]
    gifs_save = [get_config(f'gif_save_{i}') for i in range(1, 4)]

    text = "🎮 Настройки GIF для Дуэли Буллитов:\n\n"
    text += "GOAL GIFs:\n"
    for i, gif_id in enumerate(gifs_goal):
        text += f"  {i+1}: {'Задана' if gif_id else 'Не задана'}\n"
    text += "\nSAVE GIFs:\n"
    for i, gif_id in enumerate(gifs_save):
        text += f"  {i+1}: {'Задана' if gif_id else 'Не задана'}\n"

    await query.edit_message_text(text, reply_markup=bullet_duel_settings_keyboard())
    return WAITING_GIF_UPLOAD


async def show_stick_duel_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🎮 Настройки Дуэли Клюшек:", reply_markup=stick_duel_settings_keyboard())
    return WAITING_GAME_SETTINGS


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "game_settings_bullet":
        await show_bullet_duel_settings(update, context)
        return WAITING_GIF_UPLOAD
    elif data == "game_settings_stick":
        await show_stick_duel_settings(update, context)
        return WAITING_GAME_SETTINGS
    elif data == "game_settings_back":
        await query.edit_message_text("⚙️ Выберите настройки для игры:", reply_markup=game_settings_main_keyboard())
        return WAITING_GAME_SETTINGS
    elif data == "admin_back_to_main":
        await query.edit_message_text("✅ Вы в админ-панели", reply_markup=admin_menu_keyboard())
        return ConversationHandler.END
    elif data.startswith("set_gif_"):
        context.user_data["gif_type_key"] = data.replace("set_gif_", "gif_")
        await query.edit_message_text("📤 Отправьте GIF:")
        return WAITING_GIF_UPLOAD
    elif data == "reset_bullet_rating":
        reset_bullet_duel_ratings()
        await query.edit_message_text("♻️ Рейтинг «Дуэли Буллитов» обнулен!", reply_markup=bullet_duel_settings_keyboard())
        return WAITING_GIF_UPLOAD
    elif data == "reset_stick_mmr_rating":
        reset_stick_duel_mmr_ratings()
        await query.edit_message_text("♻️ Рейтинг MMR «Дуэли Клюшек» обнулен!", reply_markup=stick_duel_settings_keyboard())
        return WAITING_GAME_SETTINGS
    return ConversationHandler.END


async def receive_gif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = update.message.animation.file_id if update.message.animation else None
    if file_id:
        key_to_set = context.user_data.get("gif_type_key")
        if key_to_set:
            set_config(key_to_set, file_id)
            await update.message.reply_text("✅ Сохранено")

            gifs_goal = [get_config(f'gif_goal_{i}') for i in range(1, 4)]
            gifs_save = [get_config(f'gif_save_{i}') for i in range(1, 4)]

            text = "🎮 Настройки GIF для Дуэли Буллитов:\n\n"
            text += "GOAL GIFs:\n"
            for i, g_id in enumerate(gifs_goal):
                text += f"  {i+1}: {'Задана' if g_id else 'Не задана'}\n"
            text += "\nSAVE GIFs:\n"
            for i, s_id in enumerate(gifs_save):
                text += f"  {i+1}: {'Задана' if s_id else 'Не задана'}\n"
            await update.message.reply_text(text, reply_markup=bullet_duel_settings_keyboard())

            return WAITING_GIF_UPLOAD
        else:
            await update.message.reply_text("❌ Ошибка: не удалось определить тип GIF.")
            return WAITING_GIF_UPLOAD
    await update.message.reply_text("❌ Вы отправили не GIF. Попробуйте еще раз или /cancel.")
    return WAITING_GIF_UPLOAD


async def forward_from_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cp = update.channel_post
    if not cp:
        return
    sources = [s[0] for s in get_source_channels()]
    if cp.chat_id in sources:
        targets = get_target_chats()
        for target in targets:
            try:
                await cp.copy(chat_id=target[0])
            except Exception:
                pass


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("regrpl", "Дуэль Клюшек — найти соперника"),
        BotCommand("mymmr", "Мой MMR в Дуэли Клюшек"),
        BotCommand("ratingmmr", "Топ-10 MMR в Дуэли Клюшек"),
        BotCommand("duelrpl", "Дуэль Буллитов с ИИ"),
        BotCommand("rating", "Топ-10 игроков (Дуэль Буллитов)"),
        BotCommand("profile", "Мой профиль в RPL"),
    ], scope=BotCommandScopeDefault())


# ---------- MAIN ----------
def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # Поиск и кнопки «Дуэль Клюшек»
    app.add_handler(CommandHandler("regrpl", regrpl_command))
    app.add_handler(CommandHandler("mymmr", mymmr_command))
    app.add_handler(CommandHandler("ratingmmr", ratingmmr_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CallbackQueryHandler(stick_duel_callback, pattern=r"^rpl_"))

    # Старая одиночная «Дуэль Буллитов»
    app.add_handler(ConversationHandler(
        entry_points=[
            CallbackQueryHandler(inline_callback, pattern="^duel$"),
            CommandHandler("duelrpl", start_duel_command),
        ],
        states={
            WAITING_DUEL_SHOT: [CallbackQueryHandler(duel_shot, pattern="^shot_")],
        },
        fallbacks=[CommandHandler("cancel", start)],
        allow_reentry=True,
    ))

    # Поддержка
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(inline_callback, pattern="^support$")],
        states={
            WAITING_SUPPORT_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, support_receive)],
        },
        fallbacks=[CommandHandler("cancel", support_cancel)],
        allow_reentry=True,
    ))

    # Админка — НАСТРОЙКИ ИГР
    app.add_handler(ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🎮 Настройки игры$"), admin_buttons),
            CallbackQueryHandler(admin_callback, pattern="^game_settings_"),
            CallbackQueryHandler(admin_callback, pattern="^set_gif_"),
            CallbackQueryHandler(admin_callback, pattern="^reset_bullet_rating$"),
            CallbackQueryHandler(admin_callback, pattern="^reset_stick_mmr_rating$"),
            CallbackQueryHandler(admin_callback, pattern="^admin_back_to_main$"),
        ],
        states={
            WAITING_GAME_SETTINGS: [CallbackQueryHandler(admin_callback, pattern="^game_settings_")],
            WAITING_GIF_UPLOAD: [
                MessageHandler(filters.ANIMATION | filters.Document.ALL, receive_gif),
                CallbackQueryHandler(admin_callback, pattern="^game_settings_bullet$|^game_settings_back$|^reset_bullet_rating$"),
                CommandHandler("cancel", adminkarpl)
            ],
        },
        fallbacks=[CommandHandler("cancel", adminkarpl)],
        allow_reentry=True,
    ))

    # Авторизация администратора (ИСПРАВЛЕНА)
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("adminkarpl", adminkarpl)],
        states={
            WAITING_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, wait_login)],
            WAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, wait_password)],
        },
        fallbacks=[CommandHandler("cancel", start)],
        allow_reentry=True,
    ))

    # Добавление каналов/чатов
    app.add_handler(ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Добавить каналы$"), admin_buttons),
            MessageHandler(filters.Regex("^➕ Добавить чаты$"), admin_buttons),
        ],
        states={
            WAITING_CHANNEL_USERNAME: [MessageHandler(filters.TEXT, add_channel_username)],
            WAITING_CHAT_LINK: [MessageHandler(filters.TEXT, add_chat_link)],
        },
        fallbacks=[CommandHandler("cancel", adminkarpl)],
        allow_reentry=True,
    ))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rating", rating_command))
    app.add_handler(MessageHandler(filters.Regex("^🏠 Главное меню$"), main_menu))
    app.add_handler(MessageHandler(filters.Regex("^📩 Проверить поддержку$"), admin_buttons))
    app.add_handler(MessageHandler(filters.Regex("^⚙️ Настройки$"), admin_buttons))
    app.add_handler(MessageHandler(filters.Regex("^🚪 Выйти$"), admin_buttons))
    app.add_handler(CallbackQueryHandler(inline_callback, pattern="^(discord|website|stick_duel|profile)$"))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, forward_from_channels))

    app.run_polling()


if __name__ == "__main__":
    main()
