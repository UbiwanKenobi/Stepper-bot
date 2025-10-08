import os
import json
import re
import base64
import requests
from pathlib import Path
from filelock import FileLock
import logging
from datetime import datetime, timedelta
import csv

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters


def push_to_github():
    """Заливает data.json в твой GitHub"""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("⚠️ GITHUB_TOKEN не установлен, пропускаю пуш")
        return

    repo = "vsachilov/stepper-bot"  # ⚠️ замени на свой репозиторий
    path = "data.json"
    message = "Обновление данных шагов"

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        content_b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")

        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        headers = {"Authorization": f"token {token}"}

        r = requests.get(url, headers=headers)
        sha = r.json().get("sha")

        data = {
            "message": message,
            "content": content_b64,
            "branch": "main",
        }
        if sha:
            data["sha"] = sha

        response = requests.put(url, headers=headers, json=data)
        if response.status_code in (200, 201):
            print("✅ Данные успешно сохранены в GitHub")
        else:
            print("❌ Ошибка при сохранении в GitHub:", response.text)
    except Exception as e:
        print("❌ Ошибка при пуше в GitHub:", e)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_FILE = Path("data.json")
LOCK_FILE = str(DATA_FILE) + ".lock"

# --- функции для работы с данными ---
def load_data():
    if not DATA_FILE.exists():
        return {}
    with FileLock(LOCK_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

def save_data(data):
    with FileLock(LOCK_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

# --- регулярка для #шаги внутри текста ---
STEP_RE = re.compile(r'#шаги\s+(\d+)\s+(\d{2}\.\d{2})', re.IGNORECASE)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    # ищем текст либо в обычном сообщении, либо в подписи к фото
    text = update.message.text or update.message.caption
    if not text:
        return

    match = STEP_RE.search(text)  # ищем тег где угодно
    if not match:
        return

    steps = int(match.group(1))
    day_month = match.group(2)
    year = datetime.now().year

    try:
        date_obj = datetime.strptime(f"{day_month}.{year}", "%d.%m.%Y")
    except ValueError:
        await update.message.reply_text("⚠️ Неверный формат даты. Используй ДД.MM (например: 01.10)")
        return

    date_str = date_obj.strftime("%Y-%m-%d")
    user = update.effective_user
    user_id = str(user.id)
    username = user.username or f"{user.first_name or ''} {user.last_name or ''}".strip()

    data = load_data()
    if user_id not in data:
        data[user_id] = {"username": username, "records": []}
    else:
        data[user_id]["username"] = username
        if "records" not in data[user_id]:
            data[user_id]["records"] = []

    # проверяем, есть ли уже запись на эту дату
    existing = next((r for r in data[user_id]["records"] if r["date"] == date_str), None)
    if existing:
        existing["steps"] = steps
    else:
        data[user_id]["records"].append({"date": date_str, "steps": steps})

    save_data(data)
    push_to_github()
    await update.message.reply_text(f"✅ Записал {steps} шагов за {date_obj.strftime('%d.%m.%y')} для {username}")

# --- команды ---
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not data:
        await update.message.reply_text("Пока нет данных.")
        return

    totals = []
    for uid, info in data.items():
        records = info.get("records", [])
        total = sum(r["steps"] for r in records)
        totals.append((info.get("username", "?"), total))

    totals.sort(key=lambda x: x[1], reverse=True)
    text_lines = [f"{i+1}. {u}: {t}" for i, (u, t) in enumerate(totals)]
    await update.message.reply_text("📊 Общая статистика:\n" + "\n".join(text_lines))

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    records = data.get(user_id, {}).get("records", [])
    if not records:
        await update.message.reply_text("У тебя пока нет записей.")
        return

    records = sorted(records, key=lambda r: r["date"])
    text_lines = [f"{datetime.strptime(r['date'], '%Y-%m-%d').strftime('%d.%m.%y')}: {r['steps']}" for r in records]
    await update.message.reply_text("📅 Вся твоя история шагов:\n" + "\n".join(text_lines))

async def cmd_missed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    records = sorted(data.get(user_id, {}).get("records", []), key=lambda r: r["date"])
    if not records:
        await update.message.reply_text("У тебя пока нет записей.")
        return

    dates_recorded = {r["date"] for r in records}
    start_date = datetime.strptime(records[0]["date"], "%Y-%m-%d")
    end_date = datetime.strptime(records[-1]["date"], "%Y-%m-%d")

    missed_days = []
    current = start_date
    while current <= end_date:
        d_str = current.strftime("%Y-%m-%d")
        if d_str not in dates_recorded:
            missed_days.append(current.strftime("%d.%m.%y"))
        current += timedelta(days=1)

    if not missed_days:
        await update.message.reply_text("🎉 Нет пропущенных дней! Молодец!")
    else:
        await update.message.reply_text("❌ Пропущенные дни:\n" + "\n".join(missed_days))

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    lines = [["username", "date", "steps"]]

    for uid, info in data.items():
        username = info.get("username", "?")
        for r in info.get("records", []):
            lines.append([username, r["date"], r["steps"]])

    filename = "steps_export.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(lines)

    await update.message.reply_text(f"✅ Экспорт завершён: {filename}")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь сообщение с шагами вида:\n"
        "#шаги 1234 01.10\n\n"
        "Можно отправлять вместе с фото.\n"
        "Доступные команды:\n"
        "/stats — общая статистика\n"
        "/history — твоя история\n"
        "/missed — пропущенные дни\n"
        "/export — выгрузка в CSV"
    )

# --- запуск бота ---
def main():
    token = os.getenv("TG_TOKEN")
    if not token:
        print("Установите переменную окружения TG_TOKEN с токеном бота.")
        return

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("missed", cmd_missed))
    app.add_handler(CommandHandler("export", cmd_export))

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) | filters.PHOTO, handle_message))

    print("Запускаю бота...")
    app.run_polling()
