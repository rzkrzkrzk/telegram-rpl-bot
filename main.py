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
WAITING_LOGIN, WAITING_PASSWORD, WAITING_CHANNEL_USERNAME, WAITING_CHAT_LINK, WAITING_REPLY_TEXT, WAITING_SUPPORT_MSG = range(6)
WAITING_DUEL_SHOT = 10
WAITING_GIF_UPLOAD = 11

# Настройки «Дуэли Клюшек»
STICK_DUEL_SEARCH_SECONDS = 45
STICK_DUEL_TOTAL_TURNS = 6  # По 3 буллита каждому

# Глобальная очередь позволяет находить игроков даже в разных чатах.
# Состояние игр хранится в памяти и сбрасывается при перезапуске бота.
stick_duel_searches = {}  # user_id -> search
stick_duel_games = {}     # game_id -> game
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
        goals INTEGER DEFAULT 0
    )''')

    c.execute('INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)', ('gif_goal', ''))
    c.execute('INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)', ('gif_save', ''))
    conn.commit()
    conn.close()


init_db()


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
                 goals = goals + ?''',
              (user_id, username, first_name, goal_inc, goal_inc))
    conn.commit()
    conn.close()


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


def reset_all_ratings():
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


# ---------- Клавиатуры ----------
def main_menu_keyboard():
    return ReplyKeyboardMarkup([["🏠 Главное меню"]], resize_keyboard=True)


def admin_menu_keyboard():
    return ReplyKeyboardMarkup([
        ["➕ Добавить каналы", "➕ Добавить чаты"],
        ["📩 Проверить поддержку", "⚙️ Настройки"],
        ["🎮 Настройки игры", "🧹 Обнулить рейтинг"],
        ["🚪 Выйти"],
    ], resize_keyboard=True)


def welcome_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Наш Discord", callback_data="discord")],
        [InlineKeyboardButton("🌐 Наш Сайт", callback_data="website")],
        [InlineKeyboardButton("🆘 Обратиться в поддержку", callback_data="support")],
        [InlineKeyboardButton("🏒 Дуэль Буллитов", callback_data="duel")],
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
        await query.edit_message_text("🏒 Дуэль Буллитов! Выбери зону для броска:", reply_markup=duel_shot_keyboard())
        return WAITING_DUEL_SHOT


# ---------- Старая одиночная «Дуэль Буллитов» ----------
async def start_duel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
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
        logger.error("Ошибка отправки GIF: %s", e)

    if update.effective_chat.type != "private":
        context.user_data.pop(f"in_duel_{user.id}", None)

    if update.effective_chat.type == "private":
        await query.message.reply_text("📌 Выберите другой раздел:", reply_markup=welcome_inline_keyboard())

    return ConversationHandler.END


# ---------- Новая мини-игра «Дуэль Клюшек» ----------
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
        "name": "ИИ",
        "username": None,
        "chat_id": None,
        "is_bot": True,
    }


def cancel_search_task(search):
    task = search.get("task")
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()


async def safe_edit_search(bot, search, text):
    try:
        await bot.edit_message_text(
            chat_id=search["chat_id"],
            message_id=search["message_id"],
            text=text,
        )
    except Exception as e:
        logger.debug("Не удалось изменить сообщение поиска: %s", e)


async def send_to_stick_game(bot, game, text, reply_markup=None):
    """Отправляет одно сообщение в каждый уникальный чат участников."""
    sent_chats = set()
    for player in game["players"]:
        chat_id = player.get("chat_id")
        if player["is_bot"] or chat_id is None or chat_id in sent_chats:
            continue
        sent_chats.add(chat_id)
        try:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        except Exception as e:
            logger.error("Не удалось отправить состояние Дуэли Клюшек в чат %s: %s", chat_id, e)


async def begin_stick_game_locked(bot, first_player, second_player, searches):
    """Создаёт игру. Вызывается только внутри stick_duel_lock."""
    for search in searches:
        cancel_search_task(search)

    game_id = uuid.uuid4().hex[:12]
    game = {
        "id": game_id,
        "players": [first_player, second_player],
        "turn": 0,
        "stage": "starting",
        "shot": None,
        "score": [0, 0],
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
    for search in searches:
        await safe_edit_search(bot, search, match_text)

    await send_to_stick_game(bot, game, match_text)
    await send_stick_turn_locked(bot, game)


async def finish_stick_game_locked(bot, game):
    p1, p2 = game["players"]
    score1, score2 = game["score"]

    if score1 > score2:
        ending = f"🏆 Победитель: {p1['name']}!"
    elif score2 > score1:
        ending = f"🏆 Победитель: {p2['name']}!"
    else:
        ending = "🤝 Ничья!"

    text = (
        "🏁 Дуэль Клюшек завершена!\n\n"
        f"Итоговый счёт: {p1['name']} {score1}:{score2} {p2['name']}\n"
        f"{ending}"
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
        game["shot"] = random.choice(list(SHOT_LABELS))
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
            await safe_edit_search(
                application.bot,
                search,
                "⌛ Соперник не найден за 45 секунд. Начинается игра с ИИ!",
            )
            await begin_stick_game_locked(
                application.bot,
                player,
                stick_ai_player(),
                [],
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
            await update.message.reply_text("⚠️ Ты уже участвуешь в Дуэли Клюшек.")
            return
        if user.id in stick_duel_searches:
            await update.message.reply_text("🔎 Ты уже ищешь соперника.")
            return

        # Берём самого первого ожидающего игрока из общей очереди всех чатов.
        opponent_id = next(iter(stick_duel_searches), None)
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

        message = await update.message.reply_text(
            f"🔎 {user.first_name}, начат поиск соперника!\n"
            f"Ожидание: {STICK_DUEL_SEARCH_SECONDS} секунд.\n"
            "Если никто не найдётся, игра начнётся с ИИ.",
            reply_markup=stick_search_keyboard(user.id),
        )
        search = {
            "player": stick_player(user, chat_id),
            "chat_id": chat_id,
            "message_id": message.message_id,
            "task": None,
        }
        stick_duel_searches[user.id] = search
        search["task"] = asyncio.create_task(stick_search_timeout(context.application, user.id))


async def stick_duel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user

    async with stick_duel_lock:
        # Кнопки поиска
        if data.startswith("rpl_accept:"):
            owner_id = int(data.split(":", 1)[1])
            search = stick_duel_searches.get(owner_id)
            if not search:
                await query.answer("Поиск уже завершён.", show_alert=True)
                return
            if user.id == owner_id:
                await query.answer("Нельзя принять собственный поиск 🙂", show_alert=True)
                return
            if user.id in stick_duel_by_user:
                await query.answer("Ты уже участвуешь в другой игре.", show_alert=True)
                return

            await query.answer()
            stick_duel_searches.pop(owner_id, None)
            cancel_search_task(search)
            searches = [search]

            # Если принявший игрок сам искал соперника в другом чате,
            # его старый поиск также закрывается.
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
            owner_id = int(data.split(":", 1)[1])
            if user.id != owner_id:
                await query.answer("Игру с ИИ может запустить только автор поиска.", show_alert=True)
                return
            search = stick_duel_searches.pop(owner_id, None)
            if not search:
                await query.answer("Поиск уже завершён.", show_alert=True)
                return

            await query.answer()
            cancel_search_task(search)
            await begin_stick_game_locked(
                context.bot,
                search["player"],
                stick_ai_player(),
                [search],
            )
            return

        if data.startswith("rpl_cancel:"):
            owner_id = int(data.split(":", 1)[1])
            if user.id != owner_id:
                await query.answer("Отменить поиск может только его автор.", show_alert=True)
                return
            search = stick_duel_searches.pop(owner_id, None)
            if not search:
                await query.answer("Поиск уже завершён.", show_alert=True)
                return

            await query.answer()
            cancel_search_task(search)
            await safe_edit_search(context.bot, search, "❌ Поиск соперника отменён.")
            return

        # Игровые кнопки имеют формат:
        # rpl_shot:<game_id>:<turn>:<zone>
        # rpl_save:<game_id>:<turn>:<zone>
        parts = data.split(":")
        if len(parts) != 4:
            await query.answer("Некорректная кнопка.", show_alert=True)
            return

        action, game_id, turn_text, choice = parts
        game = stick_duel_games.get(game_id)
        if not game:
            await query.answer("Эта игра уже завершена.", show_alert=True)
            return

        try:
            button_turn = int(turn_text)
        except ValueError:
            await query.answer("Некорректный ход.", show_alert=True)
            return

        if button_turn != game["turn"]:
            await query.answer("Этот ход уже завершён.", show_alert=True)
            return

        attacker_index = game["turn"] % 2
        goalie_index = 1 - attacker_index
        attacker = game["players"][attacker_index]
        goalie = game["players"][goalie_index]

        if action == "rpl_shot":
            if game["stage"] != "shot":
                await query.answer("Сейчас не выбирают направление броска.", show_alert=True)
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
                ai_save = random.choice(list(SAVE_LABELS))
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
                await query.answer("Сейчас не выбирают действие вратаря.", show_alert=True)
                return
            if user.id != goalie["id"]:
                await query.answer(f"Сейчас отбивает {goalie['name']}.", show_alert=True)
                return
            if choice not in SAVE_LABELS:
                await query.answer("Неизвестный вариант сейва.", show_alert=True)
                return

            await query.answer("Вариант сейва выбран!")
            game["stage"] = "resolving"
            await resolve_stick_shot_locked(context.bot, game, choice)
            return

        await query.answer("Неизвестное действие.", show_alert=True)


# ---------- Админ-панель ----------
async def adminkarpl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return ConversationHandler.END
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
        await show_game_settings(update, context)
    elif text == "🧹 Обнулить рейтинг":
        reset_all_ratings()
        await update.message.reply_text("♻️ Весь рейтинг игроков был успешно обнулен!")
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


async def show_game_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    g, s = get_config('gif_goal'), get_config('gif_save')
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Изменить GOAL", callback_data="set_goal"),
        InlineKeyboardButton("🔄 Изменить SAVE", callback_data="set_save"),
    ]])
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


async def handle_unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if context.user_data.get("in_conversation_support") or is_admin(update.effective_user.id):
        return


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("regrpl", "Дуэль Клюшек — найти соперника"),
        BotCommand("duelrpl", "Дуэль Буллитов с ИИ-вратарём"),
        BotCommand("rating", "Топ-10 игроков лиги"),
    ], scope=BotCommandScopeDefault())


# ---------- MAIN ----------
def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # Новая глобальная «Дуэль Клюшек»
    app.add_handler(CommandHandler("regrpl", regrpl_command))
    app.add_handler(CallbackQueryHandler(stick_duel_callback, pattern=r"^rpl_"))

    # Старая система одиночной дуэли
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

    # Админка — GIF
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern="^set_")],
        states={
            WAITING_GIF_UPLOAD: [MessageHandler(filters.ANIMATION | filters.Document.ALL, receive_gif)],
        },
        fallbacks=[CommandHandler("cancel", adminkarpl)],
        allow_reentry=True,
    ))

    # Авторизация администратора
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("adminkarpl", adminkarpl)],
        states={
            WAITING_LOGIN: [MessageHandler(filters.TEXT, wait_login)],
            WAITING_PASSWORD: [MessageHandler(filters.TEXT, wait_password)],
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
    app.add_handler(MessageHandler(filters.Regex("^🎮 Настройки игры$"), admin_buttons))
    app.add_handler(MessageHandler(filters.Regex("^🧹 Обнулить рейтинг$"), admin_buttons))
    app.add_handler(MessageHandler(filters.Regex("^🚪 Выйти$"), admin_buttons))
    app.add_handler(CallbackQueryHandler(inline_callback, pattern="^(discord|website)$"))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, forward_from_channels))

    app.run_polling()


if __name__ == "__main__":
    main()
