import os
import re
import csv
import json
import asyncio
import logging
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
import threading
import requests
from flask import Flask, render_template_string, jsonify, request, session, redirect

# --- Загрузка переменных окружения из файла .env (если он существует) ---
def load_dotenv():
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'").strip('"')
                        if k not in os.environ:
                            os.environ[k] = v
        except Exception as e:
            logging.warning(f"Не удалось загрузить .env: {e}")

load_dotenv()

# ================= НАСТРОЙКИ БОТА И БЕЗОПАСНОСТИ =================
ROLE_ID = os.environ.get("ROLE_ID", "1447219553259094219").strip()
SHEET_ID = os.environ.get("SHEET_ID", "1B8Ts_DHQ11878tw1Qa8mUdjxFdCb249v78R10n9czBw").strip().replace("'", "").replace('"', "").replace(" ", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "WN063")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "super-secret-key-12345")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1543334681406214327/UVIbPuFdYS5Isa4zi13UQ9qNvHDuY2VOr2pbTvDsYAyNREtqxKw0TA1nyBxDGig7fwZH").strip()

CACHE_TIMEOUT_SECONDS = int(os.environ.get("CACHE_TIMEOUT_SECONDS", 60))
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
# ================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

# --- Сохранение и считывание состояния системы (Persistence) ---
def load_system_state():
    default_state = {
        "is_paused": False,
        "paused_contracts": []
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                return {
                    "is_paused": bool(saved.get("is_paused", False)),
                    "paused_contracts": list(saved.get("paused_contracts", []))
                }
        except Exception as e:
            logger.error(f"Ошибка считывания {STATE_FILE}: {e}")
    return default_state

def save_system_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(system_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка записи в {STATE_FILE}: {e}")

system_state = load_system_state()

# Кэш для Google Таблицы
db_cache = {
    "data": [],
    "last_updated": None
}

def get_msk_time():
    """Текущее время (UTC+3) без устаревшего utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)

def parse_date_range_bounds(date_range_str, now):
    """
    Парсит диапазон дат формата 'ДД.ММ - ДД.ММ'.
    Корректно обрабатывает переход через Новый год (например, '28.12 - 04.01').
    """
    try:
        dates = date_range_str.split('-')
        if len(dates) == 2:
            start_str, end_str = dates[0].strip(), dates[1].strip()
            current_year = now.year
            start_date = datetime.strptime(f"{start_str}.{current_year}", "%d.%m.%Y").date()
            end_date = datetime.strptime(f"{end_str}.{current_year}", "%d.%m.%Y").date()

            if end_date < start_date:
                if now.month == 1 and start_date.month == 12:
                    start_date = start_date.replace(year=current_year - 1)
                else:
                    end_date = end_date.replace(year=current_year + 1)
            return start_date, end_date
    except Exception:
        pass
    return None, None

def fetch_spreadsheet_rows():
    """
    Универсальная загрузка данных из таблицы:
    1. Через Google Service Account (service_account.json), если настроено для сервера.
    2. Через gsheets CLI (с текущим авторизованным Google аккаунтом).
    3. Через публичный CSV export (если открыт доступ по ссылке).
    """
    clean_sheet_id = SHEET_ID.strip().replace("'", "").replace('"', "").replace(" ", "")

    # 1. Google Service Account (для хостинга / серверов)
    sa_filename = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    sa_path = sa_filename if os.path.isabs(sa_filename) else os.path.join(os.path.dirname(os.path.abspath(__file__)), sa_filename)
    if os.path.exists(sa_path):
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            creds = service_account.Credentials.from_service_account_file(
                sa_path, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
            )
            service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
            res = service.spreadsheets().values().get(spreadsheetId=clean_sheet_id, range='A1:E500').execute()
            rows = res.get('values', [])
            if rows:
                logger.info(f"Загружено {len(rows)} строк через Google Service Account")
                return rows
        except Exception as e:
            logger.warning(f"Ошибка при чтении через Service Account: {e}")

    # 2. gsheets CLI (локально авторизованный Google аккаунт)
    gsheets_bin = shutil.which('gsheets') or shutil.which('gsheets.cmd')
    if gsheets_bin:
        try:
            cmd = [gsheets_bin, 'readonly', 'read', clean_sheet_id, 'A1:E500', '--json']
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=12, shell=True)
            if proc.returncode == 0 and proc.stdout.strip():
                rows = json.loads(proc.stdout)
                if isinstance(rows, list) and len(rows) > 0:
                    logger.info(f"Загружено {len(rows)} строк через авторизованный gsheets CLI")
                    return rows
        except Exception as e:
            logger.debug(f"gsheets CLI ошибка: {e}")

    # 3. Публичный CSV экспорт
    try:
        export_url = f"https://docs.google.com/spreadsheets/d/{clean_sheet_id}/export?format=csv&gid=0".strip()
        response = requests.get(export_url, timeout=7)
        if response.status_code == 200 and "html" not in response.headers.get("Content-Type", "").lower():
            csv_data = response.content.decode('utf-8').splitlines()
            reader = csv.reader(csv_data)
            rows = list(reader)
            if rows:
                logger.info(f"Загружено {len(rows)} строк через публичный CSV")
                return rows
        else:
            logger.warning(f"Публичный запрос к таблице вернул код {response.status_code}")
    except Exception as e:
        logger.error(f"Ошибка публичного запроса: {e}")

    return None

def parse_database(force_update=False):
    now = get_msk_time()

    if not force_update and db_cache["last_updated"] and (now - db_cache["last_updated"]).total_seconds() < CACHE_TIMEOUT_SECONDS:
        return db_cache["data"]

    contracts = []
    try:
        raw_rows = fetch_spreadsheet_rows()
        if raw_rows is None:
            return db_cache["data"] if db_cache["data"] else contracts

        for row in raw_rows:
            if len(row) < 2:
                continue
            times_str = str(row[0]).strip()
            text = str(row[1]).strip()
            date_range = str(row[2]).strip() if len(row) > 2 else ""
            contract_code = str(row[3]).strip() if len(row) > 3 and str(row[3]).strip() else "Без названия"

            if "время" in times_str.lower() or "текст" in text.lower():
                continue

            clean_text = text.replace('`', '').strip('"').strip()
            times = [t.strip() for t in times_str.split() if t.strip()]

            if times and clean_text:
                date_status = "active"
                if date_range:
                    start_date, end_date = parse_date_range_bounds(date_range, now)
                    today = now.date()

                    if end_date:
                        if today > end_date:
                            date_status = "expired"
                        elif today == end_date:
                            date_status = "last_day"

                contracts.append({
                    "times": times,
                    "text": clean_text,
                    "date_range": date_range if date_range else "Активен",
                    "date_status": date_status,
                    "code": contract_code
                })

        db_cache["data"] = contracts
        db_cache["last_updated"] = now

    except Exception as e:
        logger.error(f"Ошибка при обработке Google Таблицы: {e}")
        if db_cache["data"]:
            return db_cache["data"]

    return contracts

def get_next_contract_info():
    now = get_msk_time()
    contracts = parse_database()
    upcoming_contracts = []

    for contract in contracts:
        if contract["date_status"] == "expired":
            continue

        if contract["date_range"] and contract["date_range"] != "Активен":
            start_date, end_date = parse_date_range_bounds(contract["date_range"], now)
            if start_date and end_date:
                if not (start_date <= now.date() <= end_date):
                    continue

        for t_str in contract["times"]:
            try:
                t_time = datetime.strptime(t_str, "%H:%M").time()
                t_datetime = datetime.combine(now.date(), t_time)

                if t_datetime < now:
                    t_datetime += timedelta(days=1)

                is_skipped = (contract["code"] != "Без названия" and contract["code"] in system_state["paused_contracts"])

                upcoming_contracts.append({
                    "datetime": t_datetime,
                    "time_str": t_str,
                    "text": contract["text"],
                    "date_range": contract["date_range"],
                    "is_skipped": is_skipped,
                    "code": contract["code"]
                })
            except Exception as e:
                logger.error(f"Ошибка парсинга времени {t_str}: {e}")

    if not upcoming_contracts:
        return None

    upcoming_contracts.sort(key=lambda x: x["datetime"])
    return upcoming_contracts[0]

def send_discord_webhook(text):
    if not WEBHOOK_URL:
        return False
    try:
        full_content = f"<@&{ROLE_ID}>\n```\n{text}\n```"
        payload = {
            "content": full_content,
            "username": "Secretary Masha"
        }
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        return resp.status_code in (200, 204)
    except Exception as e:
        logger.error(f"Ошибка отправки в Discord Webhook: {e}")
        return False

# --- FLASK WEBSERVER ---

@app.route('/')
def dashboard():
    html_template = """
    <!DOCTYPE html>
    <html lang="ru" class="dark-theme">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Secretary Masha</title>
        <script>
            // Загрузка темы без мерцания
            (function() {
                const savedTheme = localStorage.getItem('alice_theme') || 'dark';
                if (savedTheme === 'light') {
                    document.documentElement.classList.remove('dark-theme');
                    document.documentElement.classList.add('light-theme');
                } else {
                    document.documentElement.classList.remove('light-theme');
                    document.documentElement.classList.add('dark-theme');
                }
            })();
        </script>
        <script src="https://cdn.tailwindcss.com"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            :root {
                --bg-body: #09090b;
                --bg-header: #121215;
                --bg-card: #18181b;
                --bg-subcard: #202024;
                --border-color: #27272a;
                --border-subtle: #1f1f23;
                --border-header: #27272a;
                --text-main: #f4f4f5;
                --text-muted: #a1a1aa;
                --text-dim: #71717a;
                --hover-row: rgba(255, 255, 255, 0.03);
                --table-border: #27272a;
                --modal-bg: #18181b;
                --time-badge-bg: #27272a;
                --time-badge-text: #e4e4e7;
                --time-badge-border: #3f3f46;
                --card-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.35);
            }

            html.light-theme {
                --bg-body: #f8fafc;
                --bg-header: #ffffff;
                --bg-card: #ffffff;
                --bg-subcard: #f1f5f9;
                --border-color: #e2e8f0;
                --border-subtle: #f1f5f9;
                --border-header: #e2e8f0;
                --text-main: #0f172a;
                --text-muted: #475569;
                --text-dim: #94a3b8;
                --hover-row: rgba(0, 0, 0, 0.02);
                --table-border: #e2e8f0;
                --modal-bg: #ffffff;
                --time-badge-bg: #f1f5f9;
                --time-badge-text: #334155;
                --time-badge-border: #cbd5e1;
                --card-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.08);
            }

            body {
                background-color: var(--bg-body);
                color: var(--text-main);
                font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                transition: background-color 0.2s ease, color 0.2s ease;
            }

            .app-card {
                background-color: var(--bg-card);
                border: 1px solid var(--border-color);
                box-shadow: var(--card-shadow);
                transition: background-color 0.2s ease, border-color 0.2s ease;
            }

            .app-subcard {
                background-color: var(--bg-subcard);
                border: 1px solid var(--border-color);
                transition: background-color 0.2s ease, border-color 0.2s ease;
            }

            .app-header {
                background-color: var(--bg-header);
                border-bottom: 1px solid var(--border-header);
                transition: background-color 0.2s ease, border-color 0.2s ease;
            }

            .time-badge {
                background-color: var(--time-badge-bg);
                color: var(--time-badge-text);
                border: 1px solid var(--time-badge-border);
            }

            .theme-toggle-btn {
                background: var(--bg-subcard);
                border: 1px solid var(--border-color);
                color: var(--text-main);
                transition: border-color 0.2s ease, background 0.2s ease;
            }
            .theme-toggle-btn:hover {
                border-color: var(--text-muted);
            }
        </style>
    </head>
    <body class="min-h-screen flex flex-col pb-10">

        <!-- ШАПКА -->
        <header class="app-header p-4 sticky top-0 z-30">
            <div class="max-w-6xl mx-auto flex justify-between items-center">
                <div class="flex items-center space-x-3">
                    <div class="w-9 h-9 rounded-lg bg-zinc-800/80 border border-zinc-700 flex items-center justify-center text-zinc-200 text-base">
                        <i class="fa-solid fa-file-invoice-dollar"></i>
                    </div>
                    <div>
                        <h1 class="text-lg md:text-xl font-bold tracking-tight">
                            <span>Secretary Masha</span>
                        </h1>
                    </div>
                </div>

                <div class="flex items-center space-x-3">
                    <!-- КНОПКА ПЕРЕКЛЮЧЕНИЯ ТЕМЫ -->
                    <button onclick="toggleTheme()" id="header-theme-btn" class="theme-toggle-btn px-3 py-1.5 rounded-lg text-xs font-medium flex items-center space-x-2 cursor-pointer shadow-sm" title="Переключить тему">
                        <i id="theme-btn-icon" class="fa-solid fa-sun text-amber-500"></i>
                        <span id="theme-btn-text" class="hidden sm:inline">Тема</span>
                    </button>

                    <!-- ОТДЕЛЬНАЯ КНОПКА ПАНЕЛИ УПРАВЛЕНИЯ -->
                    <button onclick="handleControlClick()" class="theme-toggle-btn px-3 py-1.5 rounded-lg text-xs font-medium flex items-center space-x-2 cursor-pointer shadow-sm hover:border-zinc-400 transition" title="Панель управления">
                        <i class="fa-solid fa-sliders text-zinc-400"></i>
                        <span>Управление</span>
                    </button>

                    <!-- АВТОРИЗАЦИЯ -->
                    {% if session.get('authorized') %}
                    <span class="text-xs text-emerald-500 flex items-center space-x-1.5 font-medium bg-emerald-500/10 px-2.5 py-1 rounded-md border border-emerald-500/20">
                        <span class="h-1.5 w-1.5 rounded-full bg-emerald-500"></span>
                        <span>Админ</span>
                    </span>
                    <a href="/logout" class="text-xs text-zinc-400 hover:text-zinc-200 transition font-medium">Выйти</a>
                    {% else %}
                    <button onclick="openLoginModal()" class="bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-200 font-medium py-1.5 px-4 rounded-lg text-xs transition cursor-pointer">
                        Войти
                    </button>
                    {% endif %}
                </div>
            </div>
        </header>

        <!-- ГЛАВНЫЙ КОНТЕНТ -->
        <main class="max-w-6xl w-full mx-auto px-4 mt-6 flex-grow space-y-6">
            
            <!-- ВЕРХНЯЯ СТАТИСТИКА -->
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div class="app-card rounded-xl p-5 flex items-center justify-between">
                    <div>
                        <p class="text-xs uppercase tracking-wider font-semibold opacity-70">Серверное время</p>
                        <p id="server-time" class="text-2xl font-bold font-mono mt-0.5">--:--:--</p>
                    </div>
                    <div class="w-10 h-10 rounded-lg bg-zinc-800/60 border border-zinc-700/60 flex items-center justify-center text-zinc-400 text-lg">
                        <i class="fa-regular fa-clock"></i>
                    </div>
                </div>
                <div class="app-card rounded-xl p-5 flex items-center justify-between">
                    <div>
                        <p class="text-xs uppercase tracking-wider font-semibold opacity-70">Всего контрактов в базе</p>
                        <p id="total-contracts-count" class="text-2xl font-bold font-mono mt-0.5">0</p>
                    </div>
                    <div class="w-10 h-10 rounded-lg bg-zinc-800/60 border border-zinc-700/60 flex items-center justify-center text-zinc-400 text-lg">
                        <i class="fa-solid fa-list-check"></i>
                    </div>
                </div>
            </div>

            <!-- БЛОК СЛЕДУЮЩЕГО КОНТРАКТА -->
            <div class="app-card rounded-xl p-6">
                <div class="flex justify-between items-center mb-3">
                    <h2 class="text-xs uppercase tracking-wider font-semibold opacity-70 flex items-center gap-2">
                        <i class="fa-solid fa-bullhorn text-zinc-400"></i>
                        <span>Следующая отправка</span>
                    </h2>
                    <span id="next-timer-badge" class="text-[11px] font-mono font-medium"></span>
                </div>
                <div id="next-contract-container">
                    <div class="text-center py-6 text-zinc-500 text-sm">Загрузка данных...</div>
                </div>
            </div>

            <!-- ТАБЛИЦА ВСЕХ КОНТРАКТОВ -->
            <div class="app-card rounded-xl p-6">
                <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 mb-4">
                    <h2 class="text-base font-semibold flex items-center gap-2">
                        <i class="fa-solid fa-table-list text-zinc-400"></i>
                        <span>Все контракты в базе</span>
                    </h2>
                    <div class="text-xs text-zinc-500 flex items-center gap-3">
                        <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-emerald-500"></span> Активен</span>
                        <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-amber-500"></span> Последний день</span>
                        <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-zinc-500"></span> Пауза</span>
                        <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-rose-500"></span> Просрочен</span>
                    </div>
                </div>
                
                <div class="overflow-x-auto rounded-lg border border-zinc-800">
                    <table class="w-full text-left border-collapse table-auto">
                        <thead>
                            <tr class="app-subcard border-b border-zinc-800 text-xs uppercase tracking-wider font-semibold opacity-75">
                                <th class="py-3 px-4 whitespace-nowrap">Код</th>
                                <th class="py-3 px-4 w-[200px]">Время выхода</th>
                                <th class="py-3 px-4 w-full">Текст объявления</th>
                                <th class="py-3 px-4 whitespace-nowrap">Действует до</th>
                                <th class="py-3 px-4 text-center whitespace-nowrap w-[130px]">Статус</th>
                            </tr>
                        </thead>
                        <tbody id="contracts-table-body" class="divide-y divide-zinc-800/60 text-sm">
                            <tr><td colspan="5" class="text-center py-8 text-zinc-500">Загрузка расписания...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

        </main>

        <!-- МОДАЛЬНОЕ ОКНО АВТОРИЗАЦИИ -->
        <div id="login-modal" class="hidden fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
            <div class="app-card rounded-xl max-w-sm w-full p-6 space-y-4 shadow-xl">
                <div class="flex justify-between items-center">
                    <h3 class="font-semibold text-base flex items-center gap-2">
                        <i class="fa-solid fa-lock text-zinc-400"></i>
                        <span>Вход в админ-панель</span>
                    </h3>
                    <button onclick="closeLoginModal()" class="text-zinc-400 hover:text-zinc-200 text-xl font-bold">&times;</button>
                </div>
                <form action="/login" method="POST" class="space-y-3">
                    <div>
                        <label class="block text-xs uppercase font-medium text-zinc-500 mb-1">Пароль администратора</label>
                        <input type="password" name="password" required placeholder="Введите пароль..." 
                               class="app-subcard w-full rounded-lg p-2.5 text-sm focus:outline-none focus:border-zinc-400">
                    </div>
                    <button type="submit" class="w-full bg-zinc-800 hover:bg-zinc-700 border border-zinc-600 text-zinc-100 font-medium py-2.5 rounded-lg text-sm transition">
                        Войти
                    </button>
                </form>
            </div>
        </div>

        <!-- БОКОВАЯ ПАНЕЛЬ УПРАВЛЕНИЯ (ВЫЕЗЖАЕТ СПРАВА) -->
        <div id="control-backdrop" onclick="closeControlModal()" 
             class="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 transition-opacity duration-300 opacity-0 pointer-events-none">
        </div>

        <div id="control-drawer" 
             class="fixed top-0 right-0 h-full w-full max-w-md app-card border-l border-zinc-800 shadow-2xl z-50 transform translate-x-full transition-transform duration-300 ease-in-out flex flex-col p-6 overflow-y-auto">
            
            <div class="flex justify-between items-center border-b border-zinc-800 pb-4 mb-5">
                <div class="flex items-center space-x-2.5">
                    <i class="fa-solid fa-sliders text-zinc-400 text-lg"></i>
                    <h3 class="font-bold text-sm tracking-wider uppercase">Панель управления рассылкой</h3>
                </div>
                <button onclick="closeControlModal()" class="w-8 h-8 rounded-lg bg-zinc-800/60 hover:bg-zinc-700 flex items-center justify-center text-zinc-400 hover:text-zinc-200 transition text-lg">&times;</button>
            </div>
            
            <div class="space-y-5 flex-grow">
                <!-- ГЛОБАЛЬНАЯ ПАУЗА: АКТИВНО (ЗЕЛЕНЫЙ) / НА ПАУЗЕ (КРАСНЫЙ) -->
                <div>
                    <p class="text-[10px] text-zinc-500 uppercase tracking-wider mb-2 font-semibold">Глобальный статус рассылки</p>
                    <div id="ctrl-btn-pause" onclick="togglePause()" class="w-full h-[52px] flex items-center justify-between app-subcard rounded-lg cursor-pointer select-none transition border-r-4 border-emerald-600">
                        <div class="flex flex-col justify-center pl-4 flex-grow">
                            <span id="ctrl-pause-title" class="font-semibold text-xs tracking-wider">СИСТЕМА АКТИВНА</span>
                            <span id="ctrl-pause-desc" class="text-[10px] text-zinc-500">Нажмите для паузы рассылки</span>
                        </div>
                        <div class="w-12 h-full flex items-center justify-center border-l border-zinc-700/30">
                            <i id="ctrl-pause-icon" class="fa-solid fa-circle-check text-emerald-500 text-base"></i>
                        </div>
                    </div>
                </div>

                <!-- ВЫБОРОЧНАЯ ПРИОСТАНОВКА ПО КОДУ КОНТРАКТА -->
                <div>
                    <p class="text-[10px] text-zinc-500 uppercase tracking-wider mb-2 font-semibold">Выборочная приостановка по коду контракта</p>
                    <div id="paused-contracts-list" class="space-y-2 max-h-60 overflow-y-auto pr-1">
                        <p class="text-xs text-zinc-500">Загрузка контрактов...</p>
                    </div>
                </div>
            </div>

            <!-- РЕДАКТИРОВАНИЕ GOOGLE ТАБЛИЦЫ -->
            <div class="pt-4 border-t border-zinc-800 mt-6">
                <a href="https://docs.google.com/spreadsheets/d/{{ sheet_id }}/edit?gid=0#gid=0" target="_blank" 
                   class="w-full bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-200 font-medium py-2.5 px-4 rounded-lg text-xs flex justify-between items-center transition shadow-sm">
                    <span>Открыть Google Таблицу для редактирования</span>
                    <i class="fa-solid fa-arrow-up-right-from-square text-zinc-400"></i>
                </a>
            </div>
        </div>

        <script>
            let isUpdating = false;
            let isAuthorized = {% if session.get('authorized') %}true{% else %}false{% endif %};

            // Функция безопасного экранирования текста для защиты от XSS
            function escapeHtml(text) {
                if (!text) return '';
                const map = {
                    '&': '&amp;',
                    '<': '&lt;',
                    '>': '&gt;',
                    '"': '&quot;',
                    "'": '&#039;'
                };
                return String(text).replace(/[&<>"']/g, function(m) { return map[m]; });
            }

            // Управление темой (Dark / Light)
            function updateThemeUI(theme) {
                const headerBtnIcon = document.getElementById('theme-btn-icon');
                const headerBtnText = document.getElementById('theme-btn-text');

                if (theme === 'light') {
                    if (headerBtnIcon) headerBtnIcon.className = 'fa-solid fa-moon text-zinc-600';
                    if (headerBtnText) headerBtnText.innerText = 'Тёмная';
                } else {
                    if (headerBtnIcon) headerBtnIcon.className = 'fa-solid fa-sun text-amber-500';
                    if (headerBtnText) headerBtnText.innerText = 'Светлая';
                }
            }

            function applyTheme(theme) {
                if (theme === 'light') {
                    document.documentElement.classList.remove('dark-theme');
                    document.documentElement.classList.add('light-theme');
                } else {
                    document.documentElement.classList.remove('light-theme');
                    document.documentElement.classList.add('dark-theme');
                }
                localStorage.setItem('alice_theme', theme);
                updateThemeUI(theme);
            }

            function toggleTheme() {
                const isLight = document.documentElement.classList.contains('light-theme');
                applyTheme(isLight ? 'dark' : 'light');
            }

            // Инициализация темы
            updateThemeUI(document.documentElement.classList.contains('light-theme') ? 'light' : 'dark');

            function openLoginModal() { document.getElementById('login-modal').classList.remove('hidden'); }
            function closeLoginModal() { document.getElementById('login-modal').classList.add('hidden'); }

            function handleControlClick() {
                if (!isAuthorized) {
                    openLoginModal();
                } else {
                    openControlModal();
                }
            }

            function openControlModal() {
                const backdrop = document.getElementById('control-backdrop');
                const drawer = document.getElementById('control-drawer');
                if (backdrop && drawer) {
                    backdrop.classList.remove('opacity-0', 'pointer-events-none');
                    backdrop.classList.add('opacity-100');
                    drawer.classList.remove('translate-x-full');
                    drawer.classList.add('translate-x-0');
                    document.body.classList.add('overflow-hidden');
                }
            }

            function closeControlModal() {
                const backdrop = document.getElementById('control-backdrop');
                const drawer = document.getElementById('control-drawer');
                if (backdrop && drawer) {
                    backdrop.classList.remove('opacity-100');
                    backdrop.classList.add('opacity-0', 'pointer-events-none');
                    drawer.classList.remove('translate-x-0');
                    drawer.classList.add('translate-x-full');
                    document.body.classList.remove('overflow-hidden');
                }
            }

            document.addEventListener('keydown', function(e) {
                if (e.key === 'Escape') {
                    closeControlModal();
                    closeLoginModal();
                }
            });

            async function toggleContractPause(checkboxElement) {
                isUpdating = true;
                checkboxElement.disabled = true;
                const contractCode = checkboxElement.dataset.code;
                const previousState = !checkboxElement.checked;

                try {
                    const response = await fetch('/api/toggle-contract-pause', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ code: contractCode })
                    });
                    if (!response.ok) {
                        checkboxElement.checked = previousState;
                    }
                } catch (e) {
                    console.error("Ошибка изменения статуса паузы контракта:", e);
                    checkboxElement.checked = previousState;
                } finally {
                    checkboxElement.disabled = false;
                    setTimeout(async () => {
                        isUpdating = false;
                        await updateStats(true);
                    }, 250);
                }
            }

            async function togglePause() {
                isUpdating = true;
                const btnPause = document.getElementById('ctrl-btn-pause');
                if (btnPause) btnPause.style.pointerEvents = 'none';

                try {
                    await fetch('/api/toggle-pause', { method: 'POST' });
                } catch(e) {
                    console.error("Ошибка при переключении глобальной паузы:", e);
                } finally {
                    setTimeout(() => {
                        isUpdating = false;
                        if (btnPause) btnPause.style.pointerEvents = 'auto';
                        updateStats(true);
                    }, 250);
                }
            }

            async function updateStats(force = false) {
                if (isUpdating && !force) return;

                try {
                    const response = await fetch('/api/stats');
                    if (!response.ok) return;
                    const data = await response.json();

                    document.getElementById('server-time').innerText = data.server_time;
                    document.getElementById('total-contracts-count').innerText = data.total_contracts;

                    const ctrlBtnPause = document.getElementById('ctrl-btn-pause');
                    const ctrlTitle = document.getElementById('ctrl-pause-title');
                    const ctrlDesc = document.getElementById('ctrl-pause-desc');
                    const ctrlIcon = document.getElementById('ctrl-pause-icon');

                    // НА ПАУЗЕ -> КРАСНЫЙ; АКТИВНО -> ЗЕЛЕНЫЙ
                    if (data.is_paused) {
                        if (ctrlBtnPause) {
                            ctrlBtnPause.className = "w-full h-[52px] flex items-center justify-between app-subcard border-r-4 border-rose-600 rounded-lg cursor-pointer select-none transition";
                        }
                        if (ctrlTitle) {
                            ctrlTitle.innerText = "РАССЫЛКА НА ПАУЗЕ";
                            ctrlTitle.className = "font-semibold text-xs tracking-wider text-rose-500";
                        }
                        if (ctrlDesc) ctrlDesc.innerText = "Нажмите, чтобы запустить рассылку";
                        if (ctrlIcon) ctrlIcon.className = "fa-solid fa-pause text-rose-500 text-base";
                    } else {
                        if (ctrlBtnPause) {
                            ctrlBtnPause.className = "w-full h-[52px] flex items-center justify-between app-subcard border-r-4 border-emerald-600 rounded-lg cursor-pointer select-none transition";
                        }
                        if (ctrlTitle) {
                            ctrlTitle.innerText = "СИСТЕМА АКТИВНА";
                            ctrlTitle.className = "font-semibold text-xs tracking-wider text-emerald-500";
                        }
                        if (ctrlDesc) ctrlDesc.innerText = "Нажмите, чтобы приостановить рассылку";
                        if (ctrlIcon) ctrlIcon.className = "fa-solid fa-circle-check text-emerald-500 text-base";
                    }

                    // Список контрактов для выборочной паузы
                    const uniqueCodes = [...new Set(data.all_contracts.map(c => c.code))].filter(code => code && code !== "Без названия");
                    const pausedListContainer = document.getElementById('paused-contracts-list');
                    if (pausedListContainer) {
                        if (uniqueCodes.length > 0) {
                            pausedListContainer.innerHTML = uniqueCodes.map(code => {
                                const isChecked = data.paused_contracts.includes(code);
                                const safeCode = escapeHtml(code);
                                return `
                                    <label class="flex items-center justify-between app-subcard p-3 rounded-lg border border-zinc-800 cursor-pointer select-none hover:border-zinc-600 transition">
                                        <span class="text-xs font-medium text-zinc-300">${safeCode}</span>
                                        <input type="checkbox" 
                                               data-code="${safeCode}" 
                                               onchange="toggleContractPause(this)" 
                                               ${isChecked ? 'checked' : ''} 
                                               class="w-4 h-4 rounded text-zinc-600 focus:ring-0 cursor-pointer">
                                    </label>
                                `;
                            }).join('');
                        } else {
                            pausedListContainer.innerHTML = '<p class="text-xs text-zinc-500">Нет контрактов с кодовыми названиями</p>';
                        }
                    }

                    // Следующий контракт
                    const nextContainer = document.getElementById('next-contract-container');
                    const nextTimerBadge = document.getElementById('next-timer-badge');
                    if (data.next_contract) {
                        const nc = data.next_contract;
                        let badge = nc.is_skipped 
                            ? '<span class="bg-zinc-800 text-zinc-400 border border-zinc-700 text-[10px] px-2 py-0.5 rounded font-medium">НА ПАУЗЕ</span>' 
                            : `<span class="bg-zinc-800 text-zinc-300 border border-zinc-700 text-[10px] px-2 py-0.5 rounded font-medium">ЧЕРЕЗ ${escapeHtml(nc.time_left)}</span>`;
                        let codeLabel = nc.code !== "Без названия" ? ` — <strong class="text-zinc-300 font-mono">[${escapeHtml(nc.code)}]</strong>` : '';
                        
                        if (nextTimerBadge) nextTimerBadge.innerHTML = badge;
                        nextContainer.innerHTML = `
                            <div class="space-y-3">
                                <div class="text-xs opacity-75">
                                    Слот: <strong>${escapeHtml(nc.time_str)}</strong> (${escapeHtml(nc.date_range)})${codeLabel}
                                </div>
                                <div class="app-subcard p-4 rounded-xl border border-zinc-800 text-sm font-sans ${nc.is_skipped ? 'line-through opacity-50' : ''} break-words whitespace-pre-wrap leading-relaxed">${escapeHtml(nc.text)}</div>
                            </div>
                        `;
                    } else {
                        if (nextTimerBadge) nextTimerBadge.innerHTML = '';
                        nextContainer.innerHTML = '<div class="text-center text-zinc-500 py-6 text-sm">Нет запланированных контрактов</div>';
                    }

                    // Таблица контрактов
                    const tableBody = document.getElementById('contracts-table-body');
                    if (data.all_contracts && data.all_contracts.length > 0) {
                        tableBody.innerHTML = data.all_contracts.map(c => {
                            let statusBadge = '<span class="inline-block bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[10px] font-medium px-2 py-0.5 rounded">Активен</span>';
                            let rowOpacity = "";

                            const isIndividualPaused = data.paused_contracts.includes(c.code);

                            if (isIndividualPaused) {
                                statusBadge = '<span class="inline-block bg-zinc-500/10 text-zinc-400 border border-zinc-500/20 text-[10px] font-medium px-2 py-0.5 rounded">Пауза</span>';
                                rowOpacity = "opacity-60";
                            } else if (c.date_status === 'last_day') {
                                statusBadge = '<span class="inline-block bg-amber-500/10 text-amber-400 border border-amber-500/20 text-[10px] font-medium px-2 py-0.5 rounded">Последний день</span>';
                            } else if (c.date_status === 'expired') {
                                statusBadge = '<span class="inline-block bg-rose-500/10 text-rose-400 border border-rose-500/20 text-[10px] font-medium px-2 py-0.5 rounded">Просрочен</span>';
                                rowOpacity = "opacity-40";
                            }

                            let timeBadges = c.times.map(t => `<span class="time-badge inline-block text-xs px-2 py-0.5 rounded font-mono font-medium">${escapeHtml(t)}</span>`).join(' ');
                            let formattedCode = c.code !== "Без названия" ? `[${escapeHtml(c.code)}]` : '—';

                            return `
                                <tr class="${rowOpacity} hover:bg-zinc-500/5 transition">
                                    <td class="py-3 px-4 font-mono text-zinc-400 text-xs whitespace-nowrap">${formattedCode}</td>
                                    <td class="py-3 px-4"><div class="flex flex-wrap gap-1.5 max-w-[200px]">${timeBadges}</div></td>
                                    <td class="py-3 px-4 break-words max-w-xs md:max-w-xl text-sm">${escapeHtml(c.text)}</td>
                                    <td class="py-3 px-4 opacity-75 whitespace-nowrap font-mono text-xs">${escapeHtml(c.date_range)}</td>
                                    <td class="py-3 px-4 text-center">${statusBadge}</td>
                                </tr>
                            `;
                        }).join('');
                    } else {
                        tableBody.innerHTML = '<tr><td colspan="5" class="text-center py-8 text-zinc-500">В базе нет контрактов</td></tr>';
                    }
                } catch (e) {
                    console.error("Ошибка обновления данных:", e);
                }
            }

            setInterval(() => updateStats(false), 2000);
            updateStats(true);
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template, sheet_id=SHEET_ID)

@app.route('/login', methods=['POST'])
def login():
    if request.form.get("password") == ADMIN_PASSWORD:
        session['authorized'] = True
    return redirect('/')

@app.route('/logout')
def logout():
    session.pop('authorized', None)
    return redirect('/')

@app.route('/api/stats')
def api_stats():
    now = get_msk_time()
    contracts = parse_database()
    next_c = get_next_contract_info()
    next_contract_data = None
    if next_c:
        diff = next_c["datetime"] - now
        hours, remainder = divmod(int(diff.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        time_left_str = f"{minutes}м {seconds}с" if hours == 0 else f"{hours}ч {minutes}м"
        next_contract_data = {
            "time_str": next_c["time_str"],
            "text": next_c["text"],
            "date_range": next_c["date_range"],
            "time_left": time_left_str,
            "is_skipped": next_c["is_skipped"],
            "code": next_c["code"]
        }
    return jsonify({
        "server_time": now.strftime("%H:%M:%S"),
        "total_contracts": len(contracts),
        "is_paused": system_state["is_paused"],
        "paused_contracts": system_state["paused_contracts"],
        "next_contract": next_contract_data,
        "all_contracts": contracts
    })

@app.route('/api/toggle-pause', methods=['POST'])
def toggle_pause_api():
    if not session.get('authorized'):
        return jsonify({"error": "Unauthorized"}), 401

    system_state["is_paused"] = not system_state["is_paused"]
    save_system_state()
    return jsonify({"success": True, "is_paused": system_state["is_paused"]})

@app.route('/api/toggle-contract-pause', methods=['POST'])
def toggle_contract_pause_api():
    if not session.get('authorized'):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    contract_code = data.get("code")

    if contract_code and contract_code != "Без названия":
        if contract_code in system_state["paused_contracts"]:
            system_state["paused_contracts"].remove(contract_code)
        else:
            system_state["paused_contracts"].append(contract_code)
        save_system_state()

    return jsonify({"success": True, "paused_contracts": system_state["paused_contracts"]})

async def schedule_loop():
    last_sent_minute = -1
    while True:
        try:
            now = get_msk_time()
            if now.minute != last_sent_minute:
                current_time_str = now.strftime("%H:%M")
                # Обновление данных из таблицы каждую минуту
                contracts = await asyncio.to_thread(parse_database)
                if not system_state["is_paused"]:
                    for contract in contracts:
                        if contract["date_status"] == "expired":
                            continue
                        if contract["date_range"] and contract["date_range"] != "Активен":
                            start_date, end_date = parse_date_range_bounds(contract["date_range"], now)
                            if start_date and end_date and not (start_date <= now.date() <= end_date):
                                continue
                        if current_time_str in contract["times"]:
                            if contract["code"] not in system_state["paused_contracts"]:
                                logger.info(f"Отправка контракта: {contract.get('code')}")
                                await asyncio.to_thread(send_discord_webhook, contract["text"])
                last_sent_minute = now.minute
        except Exception as e:
            logger.error(f"Ошибка в планировщике: {e}")
        await asyncio.sleep(15)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False), daemon=True).start()
    asyncio.run(schedule_loop())
