"""Telegram-бот «Слепки Мастер» — приём заданий на создание/обновление слепков.

Контракт: `SLEPKI_BOT_PLAN.md` (входы, слои хранения, именование, кнопки).
Этап 1 (этот файл): приём входов, очередь, кнопки, статус. Бот САМ ничего не применяет —
разбор и apply делают воркер и `codex exec` на LXC 101 (этап 2), и только после кнопки «Применить».

Запуск: `python3 -m direct.slepki_bot` из /opt/scripts/home/seoadvanced (юнит direct-slepki-bot.service).

Почему свой polling, а не aiogram: на LXC 101 api.telegram.org НЕдоступен напрямую
(`Network is unreachable`) — ходим через SOCKS-цепочку из `.secret` (`tg_proxy_variants()`),
она же используется остальными скриптами проекта. Лишняя зависимость не нужна.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import requests

log = logging.getLogger("slepki_bot")

DIRECT_DIR = Path(__file__).resolve().parent
SLEPKI_DIR = DIRECT_DIR / "slepki"
INBOX_DIR = DIRECT_DIR / "_inbox"

# Telegram отдаёт боту файл не больше 20 МБ (Bot API getFile). Крупное — через _inbox/.
TG_FILE_LIMIT_MB = 20
POLL_TIMEOUT = 50


def _secret_dir() -> Path:
    """Путь к .secret/ вверх по дереву (loader — единственный источник секретов проекта)."""
    for p in [DIRECT_DIR, *DIRECT_DIR.parents]:
        if (p / ".secret" / "loader.py").exists():
            return p / ".secret"
    raise RuntimeError("не найден .secret/loader.py — секреты недоступны")


sys.path.insert(0, str(_secret_dir()))
from loader import _load_env, load_db, tg_proxy_variants  # noqa: E402


# ── доступ ───────────────────────────────────────────────────────────────────

def _allowlist() -> set[int]:
    """Кто может писать боту. Пусто → бот не отвечает никому (безопасный дефолт)."""
    raw = _load_env().get("TG_SLEPKI_MASTER_CHAT", "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}


# ── транспорт ────────────────────────────────────────────────────────────────

class Bot:
    """Минимальный клиент Bot API с липким прокси: найденный рабочий больше не перебираем."""

    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self._proxies = None
        self._proxy_known = False

    def call(self, method: str, **params):
        variants = [self._proxies] if self._proxy_known else tg_proxy_variants()
        last = None
        for proxies in variants:
            try:
                r = requests.post(f"{self.base}/{method}", json=params, proxies=proxies,
                                  timeout=POLL_TIMEOUT + 15)
                data = r.json()
                if not data.get("ok"):
                    raise RuntimeError(f"{method}: {data.get('description')}")
                self._proxies, self._proxy_known = proxies, True
                return data["result"]
            except Exception as e:                        # noqa: BLE001 — перебираем всю цепочку
                last = e
                self._proxy_known = False
        raise RuntimeError(f"{method} не прошёл ни через один прокси: {last}")

    def download(self, file_path: str) -> bytes:
        variants = [self._proxies] if self._proxy_known else tg_proxy_variants()
        last = None
        for proxies in variants:
            try:
                r = requests.get(f"{self.file_base}/{file_path}", proxies=proxies, timeout=180)
                r.raise_for_status()
                return r.content
            except Exception as e:                        # noqa: BLE001
                last = e
        raise RuntimeError(f"скачивание не прошло: {last}")

    def send(self, chat_id: int, text: str, markup=None):
        params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if markup is not None:
            params["reply_markup"] = markup
        return self.call("sendMessage", **params)


# ── очередь заданий (своя таблица; очередь создания РК не трогаем) ───────────

DDL = """
CREATE TABLE IF NOT EXISTS public.direct_slepki_bot_jobs (
    id          text PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    chat_id     bigint      NOT NULL,
    kind        text        NOT NULL,   -- harvest_login | upload | inbox_folder
    status      text        NOT NULL,   -- queued | running | proposed | applied | failed | rejected
    payload     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    result      jsonb,
    error       text
)
"""


def _conn():
    cfg = load_db("victory")
    import psycopg2
    return psycopg2.connect(host=cfg["host"], port=cfg["port"], database=cfg["database"],
                            user=cfg["user"], password=cfg["password"], connect_timeout=15)


def db_init() -> None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
    finally:
        conn.close()


def job_new(chat_id: int, kind: str, payload: dict) -> str:
    import uuid
    jid = uuid.uuid4().hex[:12]
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.direct_slepki_bot_jobs(id, chat_id, kind, status, payload) "
                "VALUES (%s, %s, %s, 'queued', %s)",
                (jid, chat_id, kind, json.dumps(payload, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    return jid


def jobs_recent(limit: int = 5) -> list[tuple]:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, kind, status, created_at, error "
                        "FROM public.direct_slepki_bot_jobs ORDER BY created_at DESC LIMIT %s",
                        (limit,))
            return cur.fetchall()
    finally:
        conn.close()


# ── справочник слепков (структура — источник правды на диске) ────────────────

def known_slepki() -> list[str]:
    """Ключи существующих слепков: имена part-файлов, служебные (_order, _backup…) не в счёт."""
    return sorted(p.stem for p in SLEPKI_DIR.glob("*.json") if not p.stem.startswith("_"))


# ── клавиатуры ───────────────────────────────────────────────────────────────

MENU = {"keyboard": [["🆕 Новый слепок", "♻️ Обновить слепок"],
                     ["📦 Слепки", "📋 Очередь"],
                     ["❓ Помощь"]],
        "resize_keyboard": True}


def kb_source(kind: str) -> dict:
    """Выбор входа. callback_data ≤64 байт — кладём только код действия, состояние в БД/памяти."""
    return {"inline_keyboard": [
        [{"text": "🔑 По логину", "callback_data": f"v1:src_login:{kind}"}],
        [{"text": "📎 Файлы (xlsx/картинки)", "callback_data": f"v1:src_files:{kind}"}],
        [{"text": "📂 Папка в _inbox", "callback_data": f"v1:src_inbox:{kind}"}],
    ]}


def kb_slepki(prefix: str, page: int = 0, per: int = 9) -> dict:
    keys = known_slepki()
    chunk = keys[page * per:(page + 1) * per]
    rows = [[{"text": k, "callback_data": f"v1:{prefix}:{k}"} for k in chunk[i:i + 3]]
            for i in range(0, len(chunk), 3)]
    nav = []
    if page:
        nav.append({"text": "◀", "callback_data": f"v1:page_{prefix}:{page - 1}"})
    if (page + 1) * per < len(keys):
        nav.append({"text": "▶", "callback_data": f"v1:page_{prefix}:{page + 1}"})
    if nav:
        rows.append(nav)
    return {"inline_keyboard": rows}


# ── состояние диалога ────────────────────────────────────────────────────────
# В памяти процесса: при рестарте бот просто переспросит. Принятые задания живут в БД,
# они рестарт переживают — теряется только «на каком шаге мы были».
STATE: dict[int, dict] = {}

HELP = (
    "«Слепки Мастер» — приём заданий на слепки нейродиректолога.\n\n"
    "🆕 Новый слепок — собрать слепок, которого ещё нет.\n"
    "♻️ Обновить слепок — дозалить контент в существующий.\n\n"
    "Вход: логин кабинета · файлы (xlsx/картинки) · папка в _inbox.\n"
    f"⚠️ Telegram отдаёт боту файл не больше {TG_FILE_LIMIT_MB} МБ. Крупный архив — положи в "
    "home/seoadvanced/direct/_inbox/<папка>/ (синкается Mutagen) и пришли имя папки.\n\n"
    "Ничего не применяется без твоей кнопки «Применить» — сначала показываю diff."
)


def _ack(bot: Bot, chat_id: int, jid: str, what: str) -> None:
    bot.send(chat_id,
             f"✅ Задание принято: {what}\nID: {jid}\n\n"
             "Разбор и proposal сделает воркер на LXC 101 — он подключается следующим шагом "
             "(этап 2). Пока задание просто стоит в очереди, посмотреть — «📋 Очередь».",
             MENU)


def on_text(bot: Bot, chat_id: int, text: str) -> None:
    st = STATE.get(chat_id) or {}
    t = text.strip()

    if t in ("/start", "❓ Помощь", "/help"):
        STATE.pop(chat_id, None)
        bot.send(chat_id, HELP, MENU)
        return

    if t == "🆕 Новый слепок":
        STATE[chat_id] = {"mode": "new"}
        bot.send(chat_id, "Новый слепок. Откуда берём?", kb_source("new"))
        return

    if t == "♻️ Обновить слепок":
        STATE[chat_id] = {"mode": "update"}
        keys = known_slepki()
        if not keys:
            bot.send(chat_id, "На диске нет ни одного слепка — нечего обновлять.", MENU)
            return
        bot.send(chat_id, f"Какой слепок обновляем? Всего на диске: {len(keys)}",
                 kb_slepki("upd", 0))
        return

    if t == "📦 Слепки":
        keys = known_slepki()
        bot.send(chat_id, f"Слепков на диске: {len(keys)}\n" + ", ".join(keys), MENU)
        return

    if t == "📋 Очередь":
        rows = jobs_recent(5)
        if not rows:
            bot.send(chat_id, "Очередь пуста.", MENU)
            return
        lines = [f"{r[0]} · {r[1]} · {r[2]} · {r[3]:%d.%m %H:%M}" + (f"\n   ⚠️ {r[4]}" if r[4] else "")
                 for r in rows]
        bot.send(chat_id, "Последние задания:\n" + "\n".join(lines), MENU)
        return

    if st.get("await") == "login":
        jid = job_new(chat_id, "harvest_login",
                      {"login": t, "mode": st.get("mode"), "slepok": st.get("slepok")})
        STATE.pop(chat_id, None)
        _ack(bot, chat_id, jid, f"харвест кабинета «{t}»")
        return

    if st.get("await") == "inbox":
        folder = INBOX_DIR / t
        if not folder.is_dir():
            bot.send(chat_id, f"Папки «{t}» в _inbox нет. Проверь имя и пришли ещё раз.")
            return
        files = sum(1 for _ in folder.rglob("*") if _.is_file())
        jid = job_new(chat_id, "inbox_folder",
                      {"folder": t, "files": files, "mode": st.get("mode"),
                       "slepok": st.get("slepok")})
        STATE.pop(chat_id, None)
        _ack(bot, chat_id, jid, f"папка _inbox/{t} ({files} файл(ов))")
        return

    bot.send(chat_id, "Не понял. Выбери действие кнопкой ниже.", MENU)


def on_document(bot: Bot, chat_id: int, doc: dict) -> None:
    st = STATE.get(chat_id) or {}
    size_mb = (doc.get("file_size") or 0) / 1024 / 1024
    if size_mb > TG_FILE_LIMIT_MB:
        bot.send(chat_id,
                 f"Файл {size_mb:.1f} МБ — Telegram не отдаёт боту больше {TG_FILE_LIMIT_MB} МБ.\n"
                 "Положи его в home/seoadvanced/direct/_inbox/<папка>/ и пришли имя папки.", MENU)
        return
    meta = bot.call("getFile", file_id=doc["file_id"])
    blob = bot.download(meta["file_path"])
    jid = job_new(chat_id, "upload",
                  {"file_name": doc.get("file_name"), "size": doc.get("file_size"),
                   "mode": st.get("mode"), "slepok": st.get("slepok")})
    dest = INBOX_DIR / jid
    dest.mkdir(parents=True, exist_ok=True)
    (dest / (doc.get("file_name") or "file.bin")).write_bytes(blob)
    STATE.pop(chat_id, None)
    _ack(bot, chat_id, jid, f"файл {doc.get('file_name')} → _inbox/{jid}/")


def on_callback(bot: Bot, cq: dict) -> None:
    chat_id = cq["message"]["chat"]["id"]
    data = cq.get("data") or ""
    bot.call("answerCallbackQuery", callback_query_id=cq["id"])
    # Гасим клавиатуру нажатого сообщения: защита от повторного нажатия той же кнопки.
    try:
        bot.call("editMessageReplyMarkup", chat_id=chat_id,
                 message_id=cq["message"]["message_id"], reply_markup={"inline_keyboard": []})
    except Exception:                                     # noqa: BLE001 — косметика, не ломать поток
        pass

    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "v1":
        return
    _, action, arg = parts
    st = STATE.setdefault(chat_id, {})

    if action == "src_login":
        st["await"] = "login"
        bot.send(chat_id, "Пришли логин кабинета Яндекс.Директа одним сообщением.")
    elif action == "src_files":
        st["await"] = "files"
        bot.send(chat_id, f"Пришли файлы (xlsx/картинки), до {TG_FILE_LIMIT_MB} МБ каждый.")
    elif action == "src_inbox":
        st["await"] = "inbox"
        bot.send(chat_id, "Пришли имя папки внутри _inbox/ (без пути).")
    elif action == "upd":
        st["mode"], st["slepok"] = "update", arg
        bot.send(chat_id, f"Слепок «{arg}». Откуда берём контент?", kb_source("update"))
    elif action == "page_upd":
        bot.send(chat_id, "Какой слепок обновляем?", kb_slepki("upd", int(arg or 0)))


def handle(bot: Bot, upd: dict, allow: set[int]) -> None:
    if "callback_query" in upd:
        cq = upd["callback_query"]
        if cq["message"]["chat"]["id"] in allow:
            on_callback(bot, cq)
        return
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    if chat_id not in allow:
        log.warning("чужой chat_id=%s — игнор", chat_id)   # молча: не подтверждаем существование бота
        return
    if msg.get("document"):
        log.info("chat=%s документ %s", chat_id, (msg["document"] or {}).get("file_name"))
        on_document(bot, chat_id, msg["document"])
    elif msg.get("text"):
        log.info("chat=%s текст %r", chat_id, msg["text"][:60])
        on_text(bot, chat_id, msg["text"])


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    allow = _allowlist()
    if not allow:
        log.error("TG_SLEPKI_MASTER_CHAT пуст — бот не станет отвечать никому. Останов.")
        return 1
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    db_init()
    bot = Bot(_load_env()["TG_SLEPKI_MASTER_BOT"])
    me = bot.call("getMe")
    log.info("бот @%s запущен, allowlist=%s", me.get("username"), sorted(allow))

    offset = None
    while True:
        try:
            updates = bot.call("getUpdates", offset=offset, timeout=POLL_TIMEOUT)
        except Exception as e:                            # noqa: BLE001 — сеть/прокси, ждём и снова
            log.warning("getUpdates: %s", e)
            time.sleep(5)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                handle(bot, upd, allow)
            except Exception as e:                        # noqa: BLE001 — один сбойный апдейт не роняет бота
                log.exception("upd %s: %s", upd.get("update_id"), e)


if __name__ == "__main__":
    raise SystemExit(main())
