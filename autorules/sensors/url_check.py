"""Сенсор: битые ссылки 404/5xx в объявлениях.

Собирает посадочные URL из активных объявлений через Direct API v5,
выполняет HEAD/GET-запросы с таймаутом и ограничением числа проверок.
"""
from __future__ import annotations

import urllib3
# verify=False используется намеренно: нас интересует HTTP-статус, а не TLS-верификация;
# проверяемые URL могут иметь самоподписанные сертификаты или нестандартную PKI.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_MAX_URLS = 20       # максимум URL для проверки за один прогон
_TIMEOUT = 8         # секунд на запрос
_BAD_CODES = {400, 404, 405, 410, 500, 502, 503, 504}  # 403 исключён: бот-фильтр сайта, ложные срабатывания
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; DirectAutoRules/1.0; "
        "+https://seoadvanced.ru)"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


def run(login: str, ctx: dict) -> dict:
    """Проверяет посадочные URL активных объявлений на доступность.

    Returns:
        {"found": int, "details": list, "error": str|None}
    """
    from ...yandex_gateway import v5_get, v5_err  # direct.yandex_gateway

    token = ctx.get("token")
    if not token:
        return {"found": 0, "details": [], "error": "нет токена"}

    # Получаем активные объявления
    resp = v5_get(
        "ads", token, login,
        ["Id", "CampaignId", "AdGroupId", "TextAd", "DynamicTextAd"],
        criteria={"States": ["ON"]},
    )
    if "error" in resp:
        return {"found": 0, "details": [], "error": v5_err(resp)}

    ads = (resp.get("result") or {}).get("Ads") or []

    # Извлекаем уникальные URL
    urls = _collect_urls(ads)
    if not urls:
        return {"found": 0, "details": []}

    # Ограничиваем выборку и проверяем
    sample = list(urls)[:_MAX_URLS]
    bad = _check_urls(sample)

    return {"found": len(bad), "details": bad}


def _collect_urls(ads: list) -> set:
    urls = set()
    for ad in ads:
        for field in ("TextAd", "DynamicTextAd"):
            block = ad.get(field) or {}
            href = block.get("Href") or block.get("DisplayHref") or ""
            if href and href.startswith("http"):
                urls.add(href.strip())
    return urls


def _check_urls(urls: list) -> list:
    import concurrent.futures as cf
    import requests
    from requests.exceptions import RequestException

    bad = []

    def _probe(url: str) -> dict | None:
        try:
            r = requests.head(
                url, headers=_HEADERS, timeout=_TIMEOUT,
                allow_redirects=True, verify=False,
            )
            code = r.status_code
            if code in _BAD_CODES:
                return {"url": url[:200], "status_code": code, "method": "HEAD"}
            return None
        except RequestException as e:
            # Некоторые серверы не поддерживают HEAD → пробуем GET
            try:
                r2 = requests.get(
                    url, headers=_HEADERS, timeout=_TIMEOUT,
                    allow_redirects=True, stream=True, verify=False,
                )
                r2.close()
                code = r2.status_code
                if code in _BAD_CODES:
                    return {"url": url[:200], "status_code": code, "method": "GET"}
                return None
            except RequestException:
                return {"url": url[:200], "status_code": None, "method": "err", "note": str(e)[:80]}

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for result in ex.map(_probe, urls):
            if result is not None:
                bad.append(result)

    return bad
