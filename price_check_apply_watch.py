"""Watchdog ночной заливки цен ``price_check_cron apply`` (крон 20:00, LXC101).

Запускает ``python -m direct.price_check_cron apply`` и шлёт в ЛИЧНЫЙ Telegram, если:
  • apply УПАЛ — процесс завершился с ненулевым кодом либо в логе Traceback/status=error;
  • apply ВСТАЛ — процесс жив, но лог ``price_check_cron.log`` не обновлялся дольше
    ``STUCK_SECONDS`` (1 час «без логов»). Тогда процесс убивается (SIGKILL) и шлётся алерт.

Крон-строка (root crontab LXC101):
    0 20 * * * cd /opt/scripts/home/seoadvanced && /root/venv/bin/python3 \
        -m direct.price_check_apply_watch >> /var/log/price_check_cron.log 2>&1

apply наследует stdout/stderr → его вывод уходит в тот же лог (через cron-редирект),
поэтому mtime лога = «последний признак жизни». Уведомление best-effort:
сбой отправки в TG не влияет на код возврата заливки.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # /opt/scripts/home/seoadvanced
LOG = Path("/var/log/price_check_cron.log")
PY = sys.executable or "/root/venv/bin/python3"
STUCK_SECONDS = 3600                                 # 1 час без новых строк лога = «встал»
POLL_SECONDS = 60                                    # как часто проверяем


def _log(msg: str) -> None:
    print(f"[apply-watch] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _log_tail(n: int = 12) -> str:
    try:
        return "\n".join(LOG.read_text(errors="replace").splitlines()[-n:]) or "(лог пуст)"
    except Exception:
        return "(лог недоступен)"


def _notify(text: str) -> None:
    """Личное TG-уведомление (образец — sync_content_m3._notify_telegram). Best-effort."""
    try:
        import requests
        for _p in Path(__file__).resolve().parents:
            if (_p / ".secret" / "loader.py").exists():
                sys.path.insert(0, str(_p / ".secret"))
                break
        from loader import load_telegram

        cfg = load_telegram("personal")
        token, chat_id = cfg["bot_token"], cfg["chat_id"]
        for proxy in cfg.get("proxies") or [cfg.get("proxy")]:
            proxies = {"https": proxy, "http": proxy} if proxy else None
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                    proxies=proxies, timeout=15,
                )
                if r.ok:
                    return
            except Exception:
                continue
        _log("TG notify: все прокси не ответили")
    except Exception as e:
        _log(f"TG notify failed (не критично): {e}")


def main() -> int:
    start = time.time()
    _log("старт watchdog: запускаю apply")
    # stdout/stderr наследуются → вывод apply попадает в price_check_cron.log через cron-редирект.
    proc = subprocess.Popen([PY, "-m", "direct.price_check_cron", "apply"], cwd=str(ROOT))

    while True:
        try:
            rc = proc.wait(timeout=POLL_SECONDS)
            break                                    # apply завершился сам
        except subprocess.TimeoutExpired:
            try:
                idle = time.time() - LOG.stat().st_mtime
            except OSError:
                idle = 0.0
            if idle > STUCK_SECONDS:
                proc.send_signal(signal.SIGKILL)
                try:
                    proc.wait(timeout=30)
                except Exception:
                    pass
                mins = int((time.time() - start) // 60)
                idle_min = int(idle // 60)
                _log(f"apply ВСТАЛ: лог не обновлялся {idle_min} мин — убил процесс")
                _notify(
                    "⚠️ <b>price_check apply ВСТАЛ</b> (LXC101, крон 20:00)\n"
                    f"Лог не обновлялся &gt; {STUCK_SECONDS // 60} мин "
                    f"(процесс жил {mins} мин), убит по таймауту.\n"
                    f"<pre>{_log_tail()}</pre>"
                )
                return 2

    tail = _log_tail()
    crashed = rc != 0 or "Traceback" in tail or "status=error" in tail
    if crashed:
        _log(f"apply УПАЛ: rc={rc}")
        _notify(
            f"❌ <b>price_check apply УПАЛ</b> (LXC101, крон 20:00), rc={rc}\n"
            f"<pre>{tail}</pre>"
        )
        return 1

    _log(f"apply завершился штатно (rc={rc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
