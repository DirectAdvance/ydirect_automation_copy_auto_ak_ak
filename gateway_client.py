"""Тонкий клиент внутреннего Direct-брокера (`direct-gateway.service` :5025).

ФАЗА 1 — этот модуль СОЗДАЁТСЯ, но call-sites на него ещё НЕ переключаются (это Фаза 2).
Каждая функция:
  1) короткий HTTP-запрос (таймаут ~4с) к брокеру на http://127.0.0.1:5025;
  2) при ЛЮБОЙ ошибке/недоступности брокера — ФОЛБЭК на локальную готовую функцию
     (campaign.pick_working_cookie / yandex_gateway.*).

⚠️ ФОЛБЭК ОБЯЗАТЕЛЕН: без него рестарт direct-gateway.service (частые деплои) ронял бы
создание РК. С фолбэком брокер — это лишь оптимизация (единый probe/кэш кук на все процессы),
а не единая точка отказа.

Базовый URL: env GATEWAY_URL (default http://127.0.0.1:5025); если GATEWAY_URL не задан, но
заданы DIRECT_GATEWAY_HOST/DIRECT_GATEWAY_PORT — URL собирается из них (удобно для смоука
«битый порт → фолбэк»).
"""
from __future__ import annotations

import os

_DEFAULT_TIMEOUT = 4.0


def _self_mode() -> bool:
    """Внутри самого direct-gateway процесса (он ставит DIRECT_GATEWAY_SELF=1) HTTP к себе НЕ делаем —
    сразу уходим в локальную функцию. Страховка от самопетли, даже если брокер начнёт вызывать
    _-алиасы automation_runtime, переключённые на этот клиент."""
    return os.environ.get("DIRECT_GATEWAY_SELF") == "1"


def _base_url() -> str:
    url = (os.environ.get("GATEWAY_URL") or "").strip()
    if url:
        return url.rstrip("/")
    host = (os.environ.get("DIRECT_GATEWAY_HOST") or "127.0.0.1").strip()
    port = (os.environ.get("DIRECT_GATEWAY_PORT") or "5025").strip()
    return f"http://{host}:{port}"


def _get(path: str, params: dict, timeout: float = _DEFAULT_TIMEOUT) -> dict:
    """GET к брокеру. Бросает исключение при любой сетевой/HTTP-ошибке — вызывающая
    обёртка ловит и уходит в фолбэк. В self-режиме бросает сразу → фолбэк на локальную функцию."""
    if _self_mode():
        raise RuntimeError("gateway self-process → local")
    import requests  # noqa: PLC0415

    resp = requests.get(f"{_base_url()}{path}", params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, payload: dict, timeout: float = _DEFAULT_TIMEOUT) -> dict:
    if _self_mode():
        raise RuntimeError("gateway self-process → local")
    import requests  # noqa: PLC0415

    resp = requests.post(f"{_base_url()}{path}", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def gw_cookie(login: str, *, force_refresh: bool = False,
              timeout: float = _DEFAULT_TIMEOUT) -> str:
    try:
        data = _get("/gw/cookie", {"login": login,
                                   "force_refresh": "1" if force_refresh else "0"}, timeout)
        cookie = data.get("cookie")
        if cookie:
            return cookie
        # брокер ответил, но куки нет (протухла) → пусть локальная функция даст свой вердикт/ошибку
    except Exception:  # noqa: BLE001
        pass
    from direct import campaign as _cmc  # noqa: PLC0415
    return _cmc.pick_working_cookie(login, force_refresh=force_refresh)


def gw_token(login: str, agency: str = "",
             timeout: float = _DEFAULT_TIMEOUT) -> tuple[str | None, str | None]:
    try:
        data = _get("/gw/token", {"login": login, "agency": agency}, timeout)
        token = data.get("token")
        if token:
            return token, data.get("agency")
    except Exception:  # noqa: BLE001
        pass
    from direct import yandex_gateway as _yg  # noqa: PLC0415
    return _yg.token_for_login(login, agency, _yg.direct_tokens())


def gw_tokens(timeout: float = _DEFAULT_TIMEOUT) -> dict:
    try:
        data = _get("/gw/tokens", {}, timeout)
        if isinstance(data, dict) and "error" not in data:
            return data
    except Exception:  # noqa: BLE001
        pass
    from direct import yandex_gateway as _yg  # noqa: PLC0415
    return _yg.direct_tokens()


def gw_units_alive(login: str, agency: str = "",
                   timeout: float = _DEFAULT_TIMEOUT) -> bool | None:
    try:
        data = _get("/gw/units_alive", {"login": login, "agency": agency}, timeout)
        if "error" not in data:
            return data.get("alive")
    except Exception:  # noqa: BLE001
        pass
    from direct import yandex_gateway as _yg  # noqa: PLC0415
    return _yg.units_alive_for_login(login, agency)


def gw_resolve_agency(login: str, hint: str = "",
                      timeout: float = _DEFAULT_TIMEOUT) -> str:
    try:
        data = _get("/gw/resolve_agency", {"login": login, "hint": hint}, timeout)
        if "error" not in data:
            return data.get("agency")
    except Exception:  # noqa: BLE001
        pass
    from direct import yandex_gateway as _yg  # noqa: PLC0415
    return _yg.resolve_agency_hint(login, hint)


def gw_agency_override_get(login: str,
                           timeout: float = _DEFAULT_TIMEOUT) -> str | None:
    try:
        data = _get("/gw/agency_override", {"login": login}, timeout)
        if "error" not in data:
            return data.get("agency")
    except Exception:  # noqa: BLE001
        pass
    from direct import yandex_gateway as _yg  # noqa: PLC0415
    return _yg.agency_override_get(login)


def gw_agency_override_save(login: str, agency: str,
                            timeout: float = _DEFAULT_TIMEOUT) -> None:
    try:
        data = _post("/gw/agency_override", {"login": login, "agency": agency}, timeout)
        if data.get("ok"):
            return
    except Exception:  # noqa: BLE001
        pass
    from direct import yandex_gateway as _yg  # noqa: PLC0415
    _yg.agency_override_save(login, agency)
