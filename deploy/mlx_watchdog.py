#!/usr/bin/env python3
"""
mlx_watchdog.py — health-watchdog для mlx_lm.server на M3, живёт на LXC101.

ЗАЧЕМ (root-cause 2026-07-08, набор psm5h7q6):
  ka_mlx.sh на M3 уже имеет `while true; ...; sleep 5` — он ловит ТОЛЬКО ВЫХОД процесса
  (exit). Он НЕ ловит:
    - ЗАВИСАНИЕ mlx (процесс жив, но не отвечает: RAM-thrash / spec-decoding deadlock)
    - смерть самого шелла ka_mlx.sh (LaunchAgents на M3 нет — ничего не переживёт reboot/logout)
  Health-гейт пайплайна (check_content_pipeline_health) проверяет провайдеров ТОЛЬКО ДО старта.
  Итог: 72B :8086 молча обваливается В СЕРЕДИНЕ прогона → мусорный контент.

ЧТО ДЕЛАЕТ:
  Раз в тик (systemd timer, дефолт 60с) GET /v1/models по каждому порту через уже поднятый
  на LXC101 SSH -L туннель (127.0.0.1:808x). Это ТОТ ЖЕ путь, что использует пайплайн —
  поэтому ловит и зависший mlx, и обрыв туннеля.
  При N подряд фейлах порта → тихая ремедиация на M3 через `ssh m3-relay` (без Telegram —
  SOFT/HARD self-healing не алертится, только пишется в лог systemd-юнита).

РЕМЕДИАЦИЯ (двухуровневая):
  SOFT: pkill конкретного mlx по `--port <P>` → while-true в ka_mlx.sh сам поднимет за ~5с.
  HARD: если после SOFT+грейс порт всё ещё мёртв (значит ka_mlx.sh-шелл сам умер) →
        перезапуск всего ka_mlx.sh (nohup setsid) — восстанавливает и цикл, и все инстансы.

АНТИ-ФЛАППИНГ:
  - COOLDOWN между рестартами одного порта (дефолт 300с).
  - FLAP-guard: >MAX_RESTARTS_PER_HOUR за час → авто-рестарт ПАУЗИТСЯ (тихо, лог-only).

Состояние: JSON рядом со скриптом (переживает тики systemd-таймера).
ssh — через subprocess (ssh m3-relay), без PySocks-зависимости.

Read-only к пайплайну: НЕ трогает очередь/БД Директа. Только health-probe + рестарт mlx на M3.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ── Конфиг (env-переопределяемо) ──────────────────────────────────────────────
PORTS            = [int(p) for p in os.environ.get("MLX_WATCHDOG_PORTS", "8086").split(",") if p.strip()]
PROBE_TIMEOUT    = float(os.environ.get("MLX_WATCHDOG_PROBE_TIMEOUT", "8"))
FAIL_THRESHOLD   = int(os.environ.get("MLX_WATCHDOG_FAIL_THRESHOLD", "2"))    # подряд фейлов до рестарта
COOLDOWN_SEC     = int(os.environ.get("MLX_WATCHDOG_COOLDOWN", "300"))        # между рестартами одного порта
HARD_ESCALATE    = int(os.environ.get("MLX_WATCHDOG_HARD_ESCALATE", "150"))   # SOFT→HARD если ещё мёртв дольше
MAX_PER_HOUR     = int(os.environ.get("MLX_WATCHDOG_MAX_PER_HOUR", "4"))      # flap-guard
SSH_HOST         = os.environ.get("MLX_WATCHDOG_SSH_HOST", "m3-relay")
KA_MLX_PATH      = os.environ.get("MLX_WATCHDOG_KA_MLX", "~/llm/ka_mlx.sh")
STATE_PATH       = Path(os.environ.get("MLX_WATCHDOG_STATE",
                        str(Path(__file__).resolve().parent / ".mlx_watchdog_state.json")))
LOG_PREFIX       = "[mlx_watchdog]"


def _log(msg: str) -> None:
    print(f"{LOG_PREFIX} {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# ── Состояние ─────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _log(f"state save failed: {e}")


def _port_state(st: dict, port: int) -> dict:
    key = str(port)
    if key not in st:
        st[key] = {"fails": 0, "last_restart": 0.0, "restarts": [], "first_soft": 0.0}
    return st[key]


# ── Health-probe (stdlib, localhost через SSH -L, без прокси) ──────────────────
def _probe(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/v1/models"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            if r.status != 200:
                return False
            body = r.read(4096).decode("utf-8", "replace")
            return '"data"' in body
    except Exception:
        return False


# ── Ремедиация на M3 через ssh m3-relay ───────────────────────────────────────
def _ssh_m3(remote_cmd: str) -> tuple[bool, str]:
    try:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", SSH_HOST, remote_cmd],
            capture_output=True, text=True, timeout=40,
        )
        return p.returncode == 0, (p.stdout + p.stderr).strip()[:300]
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def _soft_restart(port: int) -> tuple[bool, str]:
    # Убиваем ТОЛЬКО инстанс этого порта — while-true в ka_mlx.sh поднимет за ~5с.
    return _ssh_m3(f'pkill -f "mlx_lm.server.*--port {port}"; echo killed')


def _hard_restart() -> tuple[bool, str]:
    # ka_mlx.sh-шелл, видимо, умер: перезапускаем весь скрипт (он сам pkill-ит и поднимает всё).
    return _ssh_m3(f'nohup setsid bash {KA_MLX_PATH} >> ~/llm/ka_mlx_run.log 2>&1 & echo restarted')


# ── Основной проход ───────────────────────────────────────────────────────────
def main() -> int:
    st = _load_state()
    now = time.time()
    any_change = False

    for port in PORTS:
        ps = _port_state(st, port)
        alive = _probe(port)

        if alive:
            if ps["fails"] > 0:
                _log(f"port {port} recovered after {ps['fails']} fail(s)")
                ps["fails"] = 0
                ps["first_soft"] = 0.0
                any_change = True
            continue

        # порт мёртв
        ps["fails"] += 1
        any_change = True
        _log(f"port {port} DOWN (consecutive fails={ps['fails']})")
        if ps["fails"] < FAIL_THRESHOLD:
            continue

        # flap-guard: чистим окно 1ч
        ps["restarts"] = [t for t in ps["restarts"] if now - t < 3600]
        if len(ps["restarts"]) >= MAX_PER_HOUR:
            # не флапаем бесконечно — пауза (авто-рестарт ждёт остывания окна)
            if now - ps["last_restart"] >= COOLDOWN_SEC:
                ps["last_restart"] = now
                _log(f"port {port} ФЛАППИНГ — {len(ps['restarts'])} рестартов за час, "
                     f"авто-рестарт приостановлен")
            continue

        # cooldown между рестартами
        if now - ps["last_restart"] < COOLDOWN_SEC:
            _log(f"port {port} still down, but within cooldown ({int(now - ps['last_restart'])}s) — skip")
            continue

        # выбор уровня: SOFT, либо HARD если SOFT уже был и не помог
        do_hard = ps["first_soft"] > 0 and (now - ps["first_soft"]) >= HARD_ESCALATE
        if do_hard:
            ok, out = _hard_restart()
            level = "HARD (перезапуск ka_mlx.sh целиком)"
            ps["first_soft"] = 0.0
        else:
            ok, out = _soft_restart(port)
            level = "SOFT (pkill порта → автоподъём ka_mlx.sh)"
            if ps["first_soft"] == 0:
                ps["first_soft"] = now

        ps["last_restart"] = now
        ps["restarts"].append(now)
        _log(f"port {port} remediation {level} ok={ok} :: {out}")

    if any_change:
        _save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
