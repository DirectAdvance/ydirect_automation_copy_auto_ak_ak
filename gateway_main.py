"""Standalone Flask entrypoint for the internal Direct access broker (ФАЗА 1).

Run it behind NOTHING — это loopback-only внутренний API:
    DIRECT_GATEWAY_PORT=5025 python -m direct.gateway_main

Зачем отдельный процесс (решение Семёна, опция C, 2026-07-17): сейчас КАЖДЫЙ из 6 direct-*
процессов ПОРОЗНЬ держит рабочую куку в своём campaign._ACCOUNT_COOKIE_CACHE и независимо
долбит главпоток (probe/валидация link_info) + порознь резолвит токены/агентства/units. Этот
брокер становится ЕДИНСТВЕННЫМ владельцем кук/токенов/главпотока/units: один probe на всех,
один TTL-кэш _ACCOUNT_COOKIE_CACHE (теперь ЕДИНЫЙ в этом процессе).

ФАЗА 1 — ТОЛЬКО greenfield: этот процесс лишь ОБОРАЧИВАЕТ уже готовые функции и отдаёт их по
HTTP. Никакие существующие call-sites (campaign.py / yandex_gateway.py / automation_runtime.py)
НЕ трогаются. Миграция потребителей на gateway_client — отдельная Фаза 2.

⚠️ БЕЗОПАСНОСТЬ: /gw/* отдаёт КУКИ и ТОКЕНЫ в теле ответа. Это допустимо ТОЛЬКО потому, что
порт биндится СТРОГО на 127.0.0.1 и НЕ проксируется nginx наружу. /gw/* НИКОГДА не должен
попасть в конфиг nginx — иначе утечёт кука/токен. Аутентификация на /gw/* намеренно НЕ
навешана (внутренний loopback между своими сервисами).

Оборачиваемые функции (все уже готовы — брокер их только ВЫЗЫВАЕТ):
  • campaign.pick_working_cookie(login, *, force_refresh) → рабочая агентская кука
  • yandex_gateway.token_for_login(login, agency, tokens) → (token, agency)
  • yandex_gateway.direct_tokens() → {agency: oauth_token}
  • yandex_gateway.units_alive_for_login(login, agency) → bool|None
  • yandex_gateway.resolve_agency_hint(login, agency_hint) → agency
  • yandex_gateway.agency_override_get(login) / agency_override_save(login, agency)
"""
import json
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break

# КРИТИЧНО: роль выставляем ДО импорта automation_runtime (он на импорте тянет queue_server).
# Дефолт роли — 'all' (queue_server._direct_role), а он поднял бы в ЭТОМ процессе воркеров и
# демоны создания РК (recover/sweep/resume/repair) — ровно то, от чего мы уходим. Тот же приём:
# content_main.py:35, slepki_main.py:48, accounts_main.py:49, worker_main.py.
os.environ.setdefault("DIRECT_ROLE", "web")
# Роуты редактора контента/копирования/слепков этому процессу не нужны.
os.environ.setdefault("DIRECT_REGISTER_CONTENT_EDITOR", "0")
os.environ.setdefault("DIRECT_REGISTER_COPY", "0")
# ЭТО сам брокер: gateway_client в этом процессе должен ходить в ЛОКАЛЬНЫЕ функции, а не по HTTP
# к самому себе (Фаза 2 перецеливает _-алиасы automation_runtime на gateway_client → без этого
# флага брокер звал бы свой же :5025 = самопетля). Ставим ДО импорта automation_runtime.
os.environ["DIRECT_GATEWAY_SELF"] = "1"

# Импорт automation_runtime на импорте выполняет ВСЮ DI-проводку
# (campaign / yandex_gateway / repository готовы) — как content_main.py берёт accounts/yandex.
# Сами обёрнутые функции берём напрямую из campaign / yandex_gateway (готовые реализации).
from direct import automation_runtime as _rt  # noqa: E402,F401  (import triggers DI configure)
from direct import campaign as _cmc  # noqa: E402
from direct import yandex_gateway as _yg  # noqa: E402


def _truthy(val: str | None) -> bool:
    return str(val or "").strip().lower() in ("1", "true", "yes", "on")


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/gw/health")
    def gw_health():
        return jsonify(ok=True)

    @app.get("/gw/cookie")
    def gw_cookie():
        login = (request.args.get("login") or "").strip()
        if not login:
            return jsonify(error="login required"), 400
        force = _truthy(request.args.get("force_refresh"))
        # accounts-подсказка от клиента (comma-join); пусто → дефолтный перебор агентств.
        _accs_raw = (request.args.get("accounts") or "").strip()
        _accs = tuple(a for a in _accs_raw.split(",") if a) or _cmc.DEFAULT_COOKIE_ACCOUNTS
        try:
            # прямой вызов _local: брокер — источник правды, свой probe/кэш (без захода в себя же).
            cookie = _cmc._pick_working_cookie_local(login, _accs, force_refresh=force)
        except Exception as exc:  # noqa: BLE001
            return jsonify(login=login, error=str(exc)), 502
        if not cookie:
            return jsonify(login=login, error="no working cookie"), 502
        return jsonify(login=login, cookie=cookie)

    @app.get("/gw/token")
    def gw_token():
        login = (request.args.get("login") or "").strip()
        if not login:
            return jsonify(error="login required"), 400
        agency = (request.args.get("agency") or "").strip()
        try:
            tokens = _yg.direct_tokens()
            token, resolved = _yg.token_for_login(login, agency, tokens)
        except Exception as exc:  # noqa: BLE001
            return jsonify(login=login, error=str(exc)), 502
        if not token:
            return jsonify(login=login, error="no token for login"), 502
        return jsonify(login=login, token=token, agency=resolved)

    @app.get("/gw/tokens")
    def gw_tokens():
        try:
            return jsonify(_yg.direct_tokens())
        except Exception as exc:  # noqa: BLE001
            return jsonify(error=str(exc)), 502

    @app.get("/gw/units_alive")
    def gw_units_alive():
        login = (request.args.get("login") or "").strip()
        if not login:
            return jsonify(error="login required"), 400
        agency = (request.args.get("agency") or "").strip()
        try:
            alive = _yg.units_alive_for_login(login, agency)
        except Exception as exc:  # noqa: BLE001
            return jsonify(login=login, error=str(exc)), 502
        return jsonify(login=login, alive=alive)

    @app.get("/gw/resolve_agency")
    def gw_resolve_agency():
        login = (request.args.get("login") or "").strip()
        if not login:
            return jsonify(error="login required"), 400
        hint = (request.args.get("hint") or "").strip()
        try:
            agency = _yg.resolve_agency_hint(login, hint)
        except Exception as exc:  # noqa: BLE001
            return jsonify(login=login, error=str(exc)), 502
        return jsonify(login=login, agency=agency)

    @app.get("/gw/agency_override")
    def gw_agency_override_get():
        login = (request.args.get("login") or "").strip()
        if not login:
            return jsonify(error="login required"), 400
        try:
            agency = _yg.agency_override_get(login)
        except Exception as exc:  # noqa: BLE001
            return jsonify(login=login, error=str(exc)), 502
        return jsonify(login=login, agency=agency)

    @app.post("/gw/agency_override")
    def gw_agency_override_save():
        payload = request.get_json(silent=True) or {}
        login = (payload.get("login") or "").strip()
        agency = (payload.get("agency") or "").strip()
        if not login or not agency:
            return jsonify(error="login and agency required"), 400
        try:
            _yg.agency_override_save(login, agency)
        except Exception as exc:  # noqa: BLE001
            return jsonify(login=login, error=str(exc)), 502
        return jsonify(ok=True)

    return app


app = create_app()


if __name__ == "__main__":
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    port = int(os.environ.get("DIRECT_GATEWAY_PORT") or cfg.get("direct_gateway_port") or 5025)
    # host строго 127.0.0.1 — /gw/* отдаёт куки/токены, наружу выставлять НЕЛЬЗЯ.
    app.run(host=os.environ.get("DIRECT_GATEWAY_HOST", "127.0.0.1"),
            port=port, debug=False, threaded=True, use_reloader=False)
