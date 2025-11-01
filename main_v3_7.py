# main_v3_7.py
"""
Telegram bot v3.7 — "Помічник" (updated for python-telegram-bot v20+, ready for PythonAnywhere)
Place this file into: /home/<youruser>/bot/main_v3_7.py
Also place: config.py, backup_utils.py, credentials.json in same folder.

Main changes from v3.6:
- Uses ApplicationBuilder() and app.job_queue (PTB v20+)
- Safe matplotlib import (won't crash if not installed)
- Monthly job scheduled via job_queue.run_monthly
- Keeps previous feature set: roles, tasks, pending tasks, board, reports, Google Sheets optional
"""

import os
import json
import io
import logging
from datetime import datetime, timedelta, time as dtime
import pytz

import pandas as pd

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# Safe matplotlib import (headless-friendly)
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAVE_MATPLOTLIB = True
except Exception:
    HAVE_MATPLOTLIB = False

# Optional Google Sheets
try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
except Exception:
    gspread = None

# Load config
try:
    import config
except Exception as e:
    raise RuntimeError("Create config.py next to main_v3_7.py with required settings") from e

TOKEN = getattr(config, "BOT_TOKEN", None)
SPREADSHEET_ID = getattr(config, "SPREADSHEET_ID", None)
GOOGLE_CREDS_FILE = getattr(config, "GOOGLE_CREDS_FILE", None)
SUPERADMIN_USERNAME = getattr(config, "SUPERADMIN_USERNAME", "zooloopa").lstrip("@")
TIMEZONE = getattr(config, "TIMEZONE", "Europe/Kiev")
REPORT_HOUR = getattr(config, "REPORT_HOUR", 9)

if not TOKEN:
    raise RuntimeError("BOT_TOKEN must be set in config.py")

# Paths (assume script runs from /home/<user>/bot)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROLES_FILE = os.path.join(BASE_DIR, "roles.json")
TASKS_FILE = os.path.join(BASE_DIR, "tasks.json")
PENDING_TASKS_FILE = os.path.join(BASE_DIR, "pending_tasks.json")
BOARD_FILE = os.path.join(BASE_DIR, "board.json")
REPORTS_CSV = os.path.join(BASE_DIR, "reports.csv")

# Ensure default files exist
DEFAULT_TASKS = {"tasks": ["🧹 Помыл пол", "🌿 Полил цветы", "🪑 Протёр мебель"]}
for path, default in [
    (ROLES_FILE, {}),
    (TASKS_FILE, DEFAULT_TASKS),
    (PENDING_TASKS_FILE, {"pending": []}),
    (BOARD_FILE, {"posts": []}),
]:
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(default, fh, ensure_ascii=False, indent=2)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# JSON helpers
def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)

# Roles/tasks/board helpers
def load_roles():
    return load_json(ROLES_FILE)

def save_roles(d):
    save_json(ROLES_FILE, d)

def load_tasks():
    return load_json(TASKS_FILE)

def save_tasks(d):
    save_json(TASKS_FILE, d)

def load_pending():
    return load_json(PENDING_TASKS_FILE)

def save_pending(d):
    save_json(PENDING_TASKS_FILE, d)

def load_board():
    return load_json(BOARD_FILE)

def save_board(d):
    save_json(BOARD_FILE, d)

# Google Sheets helpers
def gsheets_client():
    if not gspread or not GOOGLE_CREDS_FILE or not SPREADSHEET_ID:
        return None
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    client = gspread.authorize(creds)
    return client

def append_to_sheet(row):
    client = gsheets_client()
    if not client:
        return False
    ss = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = ss.worksheet("Reports")
    except Exception:
        ws = ss.add_worksheet(title="Reports", rows=1000, cols=20)
        ws.append_row(["Дата", "Имя", "Роль", "Задача", "Количество", "Технолог"])
    ws.append_row(row)
    return True

# CSV backup helper
def append_csv_row(row):
    cols = ["Дата", "Имя", "Роль", "Задача", "Количество", "Технолог"]
    if os.path.exists(REPORTS_CSV):
        df = pd.read_csv(REPORTS_CSV)
        df_new = pd.DataFrame([row], columns=cols)
        df = pd.concat([df, df_new], ignore_index=True)
    else:
        df = pd.DataFrame([row], columns=cols)
    df.to_csv(REPORTS_CSV, index=False)

# Superadmin detection: by roles flag or username
def is_superadmin_by_roles(uid_str):
    roles = load_roles()
    info = roles.get(str(uid_str))
    return bool(info and info.get("is_superadmin", False))

def is_superadmin_by_username(username):
    if not username:
        return False
    return username.lower().lstrip("@") == SUPERADMIN_USERNAME.lower()

def is_superadmin(user):
    try:
        uid = str(user.id)
    except Exception:
        uid = None
    return (uid and is_superadmin_by_roles(uid)) or is_superadmin_by_username(user.username)

# Permission levels
ROLE_LEVEL = {"сотрудник": 1, "технолог": 2, "начальник": 3}

def has_role_permission(user_obj, required_role_name):
    if is_superadmin(user_obj):
        return True
    roles = load_roles()
    uid = str(user_obj.id)
    info = roles.get(uid)
    if not info:
        return False
    cur = info.get("role", "сотрудник")
    return ROLE_LEVEL.get(cur, 0) >= ROLE_LEVEL.get(required_role_name, 0)

# i18n simple
MESSAGES = {
    "welcome_ru": "Привет! Я бот «Помічник». Выберите пункт меню или используйте команды.",
    "welcome_uk": "Привіт! Я бот «Помічник». Оберіть пункт меню або використовуйте команди.",
    "no_access_ru": "⛔ У вас нет доступа к этой команде.",
    "no_access_uk": "⛔ У вас немає доступу до цієї команди.",
}

def user_lang(uid_str):
    roles = load_roles()
    info = roles.get(str(uid_str))
    if info:
        return info.get("lang", "ru")
    return "ru"

def format_msg(key, uid=None):
    lang = "ru"
    if uid:
        lang = user_lang(str(uid))
    return MESSAGES.get(f"{key}_{lang}", "") if key in ["welcome","no_access"] else ""

# Conversation states
REPORT_CHOOSE_TASK, REPORT_OTHER_NAME, REPORT_SELECT_TECH, REPORT_COUNT = range(4)
POST_TEXT, POST_PHOTO = range(2)

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    roles = load_roles()
    if uid not in roles:
        roles[uid] = {
            "name": user.full_name or user.first_name,
            "role": "сотрудник",
            "summary_enabled": False,
            "username": user.username or "",
            "lang": "ru"
        }
        save_roles(roles)

    kb = [
        [InlineKeyboardButton("📢 Доска объявлений", callback_data="menu_board")],
        [InlineKeyboardButton("🧾 Отправить отчёт", callback_data="menu_report"),
         InlineKeyboardButton("📄 Мои отчёты", callback_data="menu_my_reports")],
        [InlineKeyboardButton("👥 Сотрудники", callback_data="menu_employees"),
         InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")],
    ]
    text = MESSAGES["welcome_ru"] if roles[uid].get("lang","ru")=="ru" else MESSAGES["welcome_uk"]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

# Report conversation
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = load_tasks().get("tasks", [])
    kb = [[InlineKeyboardButton(t, callback_data=f"task::{t}")] for t in tasks]
    kb.append([InlineKeyboardButton("Другое", callback_data="task::other")])
    await update.message.reply_text("Выберите задачу (или \"Другое\"):", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data["report"] = {}
    return REPORT_CHOOSE_TASK

async def report_task_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    selection = q.data.split("::",1)[1]
    if selection == "other":
        await q.message.reply_text("Напишите название задачи:")
        return REPORT_OTHER_NAME
    context.user_data["report"]["task"] = selection
    # choose technologist
    roles = load_roles()
    tech_buttons = []
    for uid, info in roles.items():
        if info.get("role") in ["технолог", "начальник"]:
            tech_buttons.append([InlineKeyboardButton(info.get("name"), callback_data=f"tech::{uid}")])
    tech_buttons.append([InlineKeyboardButton("Не назначать", callback_data="tech::none")])
    await q.message.reply_text("Выберите технолога (кому назначить):", reply_markup=InlineKeyboardMarkup(tech_buttons))
    return REPORT_SELECT_TECH

async def report_other_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Название не может быть пустым. Повторите:")
        return REPORT_OTHER_NAME
    pending = load_pending()
    entry = {"name": text, "from": update.effective_user.full_name or update.effective_user.first_name,
             "user_id": update.effective_user.id, "date": datetime.utcnow().isoformat()}
    pending["pending"].append(entry)
    save_pending(pending)
    # notify techs and начальник
    roles = load_roles()
    for uid, info in roles.items():
        if info.get("role") in ["технолог", "начальник"]:
            try:
                await context.bot.send_message(int(uid),
                    f"🆕 Новая предложенная задача: \"{text}\" от {entry['from']}. Просмотрите /pending_tasks")
            except Exception:
                logger.exception("notify pending failed")
    await update.message.reply_text("Спасибо — предложение отправлено на проверку.")
    return ConversationHandler.END

async def report_select_tech_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.data.split("::",1)[1]
    if uid == "none":
        context.user_data["report"]["tech"] = ""
    else:
        context.user_data["report"]["tech"] = uid
    await q.message.reply_text("Сколько раз выполнили задачу? (введите число)")
    return REPORT_COUNT

async def report_count_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        cnt = int(text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите число.")
        return REPORT_COUNT
    rpt = context.user_data.get("report", {})
    task = rpt.get("task", "—")
    tech = rpt.get("tech", "")
    user_name = update.effective_user.full_name or update.effective_user.first_name
    date_str = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
    row = [date_str, user_name, "сотрудник", task, cnt, tech]
    # append CSV and optionally Google Sheets
    append_csv_row(row)
    try:
        append_to_sheet(row)
    except Exception:
        logger.exception("gsheets append failed")
    await update.message.reply_text(f"✅ Записано: {task} — {cnt} раз(а).")
    if tech:
        try:
            await context.bot.send_message(int(tech), f"📩 От {user_name}: задача \"{task}\" — {cnt} раз(а).")
        except Exception:
            pass
    return ConversationHandler.END

# Pending moderation
async def cmd_pending_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_role_permission(update.effective_user, "технолог") and not has_role_permission(update.effective_user, "начальник"):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    pending = load_pending().get("pending", [])
    if not pending:
        await update.message.reply_text("Нет предложенных задач.")
        return
    for i, p in enumerate(pending):
        kb = [[InlineKeyboardButton("✅ Одобрить", callback_data=f"pending::approve::{i}"),
               InlineKeyboardButton("❌ Отклонить", callback_data=f"pending::reject::{i}")]]
        await update.message.reply_text(f'#{i+1}: "{p["name"]}" от {p["from"]} ({p["date"]})', reply_markup=InlineKeyboardMarkup(kb))

async def pending_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("::")
    if len(parts) != 3:
        return
    action = parts[1]
    idx = int(parts[2])
    pending = load_pending()
    items = pending.get("pending", [])
    if idx < 0 or idx >= len(items):
        await q.message.reply_text("Неверный индекс.")
        return
    entry = items.pop(idx)
    save_pending(pending)
    if action == "approve":
        tasks = load_tasks()
        if entry["name"] not in tasks["tasks"]:
            tasks["tasks"].append(entry["name"])
            save_tasks(tasks)
        try:
            await context.bot.send_message(int(entry["user_id"]), f"✅ Ваша задача \"{entry['name']}\" одобрена.")
        except Exception:
            pass
        await q.message.reply_text("Добавлено в список задач.")
    else:
        try:
            await context.bot.send_message(int(entry["user_id"]), f"❌ Ваша задача \"{entry['name']}\" отклонена.")
        except Exception:
            pass
        await q.message.reply_text("Отклонено.")

# Tasks management
async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = load_tasks().get("tasks", [])
    text = "Список задач:\n" + "\n".join(f"- {t}" for t in tasks)
    await update.message.reply_text(text)

async def cmd_add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_role_permission(update.effective_user, "технолог"):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /add_task Название задачи")
        return
    name = " ".join(context.args)
    tasks = load_tasks()
    if name in tasks["tasks"]:
        await update.message.reply_text("Такая задача уже есть.")
        return
    tasks["tasks"].append(name)
    save_tasks(tasks)
    await update.message.reply_text("Добавлено.")

async def cmd_remove_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_role_permission(update.effective_user, "технолог"):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /remove_task Название задачи")
        return
    name = " ".join(context.args)
    tasks = load_tasks()
    if name not in tasks["tasks"]:
        await update.message.reply_text("Нет такой задачи.")
        return
    tasks["tasks"].remove(name)
    save_tasks(tasks)
    await update.message.reply_text("Удалено.")

# Employees list
async def cmd_employees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    roles = load_roles()
    buttons = []
    for uid, info in roles.items():
        if info.get("hidden", False):
            continue
        label = f"{info.get('name')} ({info.get('role')})"
        if info.get("username"):
            url = f"https://t.me/{info.get('username')}"
            buttons.append([InlineKeyboardButton(label, url=url)])
        else:
            buttons.append([InlineKeyboardButton(label, url=f"tg://user?id={uid}")])
    if not buttons:
        await update.message.reply_text("Список сотрудников пуст.")
        return
    await update.message.reply_text("👥 Сотрудники:", reply_markup=InlineKeyboardMarkup(buttons))

# Board (announcements)
async def cmd_board(update: Update, context: ContextTypes.DEFAULT_TYPE):
    board = load_board().get("posts", [])
    if not board:
        await update.message.reply_text("Нет объявлений.")
        return
    for post in board:
        text = f"{post.get('text')}\n\n— {post.get('author')} | {post.get('date')}"
        row = []
        for em in ["❤️","👍","🎉","😮","👏"]:
            count = len(post.get("reactions", {}).get(em, [])) if isinstance(post.get("reactions", {}), dict) else post.get("reactions", {}).get(em, 0)
            row.append(InlineKeyboardButton(f"{em} {count}", callback_data=f"react::{post['id']}::{em}"))
        kb = [row]
        if post.get("photo"):
            await update.message.reply_photo(post.get("photo"), caption=text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_role_permission(update.effective_user, "технолог"):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    await update.message.reply_text("Отправьте текст объявления:")
    return POST_TEXT

async def post_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Текст не может быть пустым.")
        return POST_TEXT
    context.user_data["post_text"] = text
    await update.message.reply_text("Если хотите прикрепить фото — отправьте его сейчас. Иначе напишите /skip")
    return POST_PHOTO

async def post_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file_id = photo.file_id
    text = context.user_data.get("post_text", "")
    board = load_board()
    pid = 1
    if board["posts"]:
        pid = max(p["id"] for p in board["posts"]) + 1
    post = {"id": pid, "text": text, "author": update.effective_user.full_name or update.effective_user.first_name,
            "date": datetime.now().strftime("%Y-%m-%d"), "photo": file_id, "reactions": {}}
    board["posts"].insert(0, post)
    save_board(board)
    roles = load_roles()
    for uid, info in roles.items():
        try:
            await context.bot.send_message(int(uid), f"📢 Новое объявление: {text[:100]}...")
        except Exception:
            pass
    await update.message.reply_text("Опубликовано.")
    return ConversationHandler.END

async def post_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = context.user_data.get("post_text", "")
    board = load_board()
    pid = 1
    if board["posts"]:
        pid = max(p["id"] for p in board["posts"]) + 1
    post = {"id": pid, "text": text, "author": update.effective_user.full_name or update.effective_user.first_name,
            "date": datetime.now().strftime("%Y-%m-%d"), "photo": None, "reactions": {}}
    board["posts"].insert(0, post)
    save_board(board)
    roles = load_roles()
    for uid, info in roles.items():
        try:
            await context.bot.send_message(int(uid), f"📢 Новое объявление: {text[:100]}...")
        except Exception:
            pass
    await update.message.reply_text("Опубликовано.")
    return ConversationHandler.END

async def react_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("::")
    if len(parts) != 3:
        return
    pid = int(parts[1])
    em = parts[2]
    board = load_board()
    post = next((p for p in board["posts"] if p["id"] == pid), None)
    if not post:
        await q.message.reply_text("Пост не найден.")
        return
    reactions = post.setdefault("reactions", {})
    em_list = reactions.setdefault(em, [])
    uid = str(q.from_user.id)
    if uid in em_list:
        em_list.remove(uid)
    else:
        em_list.append(uid)
    save_board(board)
    await q.message.reply_text("Ваша реакция сохранена.")

# Monthly summary job
async def monthly_summary_job(context: ContextTypes.DEFAULT_TYPE):
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    prev = (now.replace(day=1) - timedelta(days=1))
    month_key = prev.strftime("%Y-%m")
    if not os.path.exists(REPORTS_CSV):
        return
    df = pd.read_csv(REPORTS_CSV)
    df["Дата"] = pd.to_datetime(df["Дата"], errors="coerce")
    dfm = df[df["Дата"].dt.strftime("%Y-%m") == month_key]
    if dfm.empty:
        return
    fname = os.path.join(BASE_DIR, f"monthly_{month_key}.xlsx")
    dfm.to_excel(fname, index=False)
    total_actions = int(dfm["Количество"].sum()) if "Количество" in dfm.columns else int(dfm.iloc[:,4].sum())
    records = len(dfm)
    unique_users = dfm["Имя"].nunique() if "Имя" in dfm.columns else dfm.iloc[:,1].nunique()
    top = (dfm.groupby("Имя")["Количество"].sum().sort_values(ascending=False).head(3)
           if "Имя" in dfm.columns and "Количество" in dfm.columns
           else dfm.groupby(dfm.columns[1])[dfm.columns[4]].sum().sort_values(ascending=False).head(3))
    top_lines = "\n".join([f"{i+1}. {name} — {val}" for i,(name,val) in enumerate(zip(top.index, top.values))]) if not top.empty else "—"
    # plot if available
    if HAVE_MATPLOTLIB:
        daily = dfm.groupby(dfm["Дата"].dt.strftime("%Y-%m-%d"))[dfm.columns[4]].sum()
        plt.figure(figsize=(8,4))
        daily.plot(kind="bar")
        plt.title(f"Активность за {month_key}")
        plt.tight_layout()
        img_buf = io.BytesIO()
        plt.savefig(img_buf, format="png")
        img_buf.seek(0)
        plt.close()
    # send to users with summary_enabled
    roles = load_roles()
    for uid, info in roles.items():
        if info.get("summary_enabled"):
            try:
                text = f"📅 Автоматический отчёт за {month_key}\n👥 Сотрудников: {unique_users}\n📋 Записей: {records}\n✅ Всего действий: {total_actions}\n\n🏆 ТОП-3:\n{top_lines}"
                await context.bot.send_message(int(uid), text)
                await context.bot.send_document(int(uid), open(fname, "rb"))
                if HAVE_MATPLOTLIB:
                    await context.bot.send_photo(int(uid), img_buf)
            except Exception:
                logger.exception("monthly send failed")

# Admin / user management commands
async def cmd_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_superadmin(update.effective_user):
        await update.message.reply_text("Только начальник/суперадмин может добавлять пользователей.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /add_user TELEGRAM_ID Имя [роль]")
        return
    uid = context.args[0]
    name = context.args[1]
    role = context.args[2] if len(context.args)>=3 else "сотрудник"
    roles = load_roles()
    roles[uid] = {"name": name, "role": role, "summary_enabled": False, "username": ""}
    save_roles(roles)
    await update.message.reply_text("Добавлен пользователь.")

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_role_permission(update.effective_user, "начальник"):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    roles = load_roles()
    lines = []
    for uid, info in roles.items():
        if info.get("hidden", False):
            continue
    lines = [f"{uid}: {info.get('name')} ({info.get('role')})" for uid, info in roles.items() if not info.get("hidden", False)]
    await update.message.reply_text("\n".join(lines) if lines else "Нет пользователей")

async def cmd_assign_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_role_permission(update.effective_user, "начальник"):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /assign_user USER_ID TECH_ID")
        return
    user_id = context.args[0]; tech_id = context.args[1]
    roles = load_roles()
    if user_id not in roles or tech_id not in roles:
        await update.message.reply_text("Проверьте ID.")
        return
    roles[user_id]["manager"] = tech_id
    save_roles(roles)
    await update.message.reply_text("Назначено.")

async def cmd_toggle_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_role_permission(update.effective_user, "начальник"):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /toggle_summary USER_ID")
        return
    uid = context.args[0]
    roles = load_roles()
    if uid not in roles:
        await update.message.reply_text("Нет такого пользователя")
        return
    roles[uid]["summary_enabled"] = not roles[uid].get("summary_enabled", False)
    save_roles(roles)
    await update.message.reply_text(f"summary_enabled = {roles[uid]['summary_enabled']}")

# Language selection
async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("Русский (ru)", callback_data="lang::ru"), InlineKeyboardButton("Українська (uk)", callback_data="lang::uk")]]
    await update.message.reply_text("Выберите язык / Оберіть мову:", reply_markup=InlineKeyboardMarkup(kb))

async def lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    code = q.data.split("::",1)[1]
    roles = load_roles(); uid = str(q.from_user.id)
    if uid in roles:
        roles[uid]["lang"] = code; save_roles(roles)
    await q.message.reply_text("Язык изменён." if code=="ru" else "Мову змінено.")

# Summary on demand
async def cmd_zvit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_role_permission(update.effective_user, "технолог"):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    if not os.path.exists(REPORTS_CSV):
        await update.message.reply_text("Нет данных.")
        return
    df = pd.read_csv(REPORTS_CSV)
    fname = os.path.join(BASE_DIR, f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
    df.to_excel(fname, index=False)
    await update.message.reply_document(open(fname, "rb"), caption="📊 Общий отчёт")

# Wiring and startup
def main():
    app = ApplicationBuilder().token(TOKEN).build()
    jobq = app.job_queue

    # Handlers registration
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CallbackQueryHandler(report_task_cb, pattern="^task::"))
    app.add_handler(CallbackQueryHandler(report_select_tech_cb, pattern="^tech::"))
    app.add_handler(CallbackQueryHandler(pending_action_cb, pattern="^pending::"))
    app.add_handler(CommandHandler("pending_tasks", cmd_pending_tasks))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("add_task", cmd_add_task))
    app.add_handler(CommandHandler("remove_task", cmd_remove_task))
    app.add_handler(CommandHandler("employees", cmd_employees))
    app.add_handler(CommandHandler("board", cmd_board))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("post", cmd_post)],
                                        states={POST_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, post_text_received)],
                                                POST_PHOTO: [MessageHandler(filters.PHOTO, post_photo_received), CommandHandler("skip", post_skip_photo)]
                                                },
                                        fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)]))
    app.add_handler(CallbackQueryHandler(react_cb, pattern="^react::"))
    app.add_handler(CommandHandler("add_user", cmd_add_user))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("assign_user", cmd_assign_user))
    app.add_handler(CommandHandler("toggle_summary", cmd_toggle_summary))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CallbackQueryHandler(lang_cb, pattern="^lang::"))
    app.add_handler(CommandHandler("zvit", cmd_zvit))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("report", cmd_report)],
                                        states={REPORT_CHOOSE_TASK: [CallbackQueryHandler(report_task_cb, pattern="^task::")],
                                                REPORT_OTHER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_other_name)],
                                                REPORT_SELECT_TECH: [CallbackQueryHandler(report_select_tech_cb, pattern="^tech::")],
                                                REPORT_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_count_received)],
                                                },
                                        fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)]))

    # Schedule monthly job: 1st day every month at REPORT_HOUR
    tz = pytz.timezone(TIMEZONE)
    when = dtime(REPORT_HOUR, 0, 0, tzinfo=tz)
    try:
        jobq.run_monthly(monthly_summary_job, when=when, day=1, name="monthly_summary_job")
    except Exception as e:
        logger.exception("Cannot schedule monthly job: %s", e)

    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
