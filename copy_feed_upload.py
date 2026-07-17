"""Загрузка фида в целевой аккаунт Директа через feeds.add (v5).

Используется роутом POST /direct/api/copy_feed_upload.

DI: lazy read из copy_engine (уже сконфигурирован automation_runtime
к моменту первого вызова — импорт происходит внутри функции).

Алгоритм upload_feed_to_target:
  1. Получить URL исходного фида через _grid_feeds (возвращает поле url).
  2. Заменить домен в URL на target_domain (схема + путь сохраняются).
  3. Получить BusinessType из v5 feeds.get исходного аккаунта (fallback: AUTO).
  4. Создать фид в целевом аккаунте через v5 feeds.add.
  5. Вернуть {feed_id, name, url}.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse


def _replace_domain_in_url(url: str, target_domain: str) -> str:
    """Заменяет netloc в URL на target_domain; схема + путь + query сохраняются.

    target_domain принимается как «site.ru» или «https://site.ru» — оба варианта.
    При ошибке парсинга возвращает url без изменений.
    """
    if not url or not target_domain:
        return url
    td = target_domain.strip().rstrip("/")
    td = re.sub(r"^https?://", "", td)  # убрать схему если передали с ней
    td = td.split("/")[0]               # только hostname[:port], без пути
    try:
        parsed = urlparse(url)
        return urlunparse(parsed._replace(netloc=td))
    except Exception:  # noqa: BLE001
        return url


def upload_feed_to_target(
    source_login: str,
    target_login: str,
    source_feed_id: int,
    target_domain: str,
) -> dict:
    """Загружает фид из source в target: берёт URL из Grid, меняет домен, вызывает feeds.add.

    Args:
        source_login: логин источника.
        target_login: логин цели (туда добавляем фид).
        source_feed_id: id фида в source-аккаунте.
        target_domain: целевой домен («site.ru» или «https://site.ru»), используется
            для замены домена в URL фида.

    Returns:
        {"feed_id": int, "name": str, "url": str}

    Raises:
        RuntimeError: с кратким описанием ошибки (роут переводит в {error}).
    """
    # ── Ленивый DI: к моменту вызова copy_engine уже сконфигурирован ───────────
    from direct import copy_engine as _ce  # noqa: PLC0415

    v5_call = _ce._v5_call
    grid_feeds = _ce._grid_feeds
    token_for_login = _ce._token_for_login
    direct_tokens = _ce._direct_tokens
    resolve_agency_hint = _ce._resolve_agency_hint

    if not all([v5_call, grid_feeds, token_for_login, direct_tokens, resolve_agency_hint]):
        raise RuntimeError(
            "copy_engine DI не инициализирован (automation_runtime.configure не вызван)"
        )

    # ── Шаг 1: URL + имя исходного фида через Grid ───────────────────────────
    src_agency = resolve_agency_hint(source_login, "")
    src_feeds = grid_feeds(source_login, src_agency) or []
    src_feed = next(
        (f for f in src_feeds if str(f.get("id", "")) == str(source_feed_id)),
        None,
    )
    if src_feed is None:
        raise RuntimeError(
            f"фид #{source_feed_id} не найден в аккаунте {source_login} "
            f"(Grid вернул {len(src_feeds)} фидов)"
        )

    src_url = (src_feed.get("url") or "").strip()
    src_name = (src_feed.get("name") or f"feed {source_feed_id}").strip()
    if not src_url:
        raise RuntimeError(
            f"у фида #{source_feed_id} ({src_name!r}) нет URL — "
            "загрузка по URL невозможна (фид может быть файловым или иного типа)"
        )

    # ── Шаг 2: Адаптация URL под целевой домен ───────────────────────────────
    new_url = _replace_domain_in_url(src_url, target_domain) if target_domain else src_url

    # ── Шаг 3: BusinessType через v5 feeds.get на source (best-effort) ───────
    business_type = "AUTO"
    _KNOWN_BUSINESS_TYPES = frozenset(
        {"AUTO", "RETAIL", "HOTELS", "REALTY", "FLIGHTS", "BOATS", "AGRICULTURE"}
    )
    try:
        src_token, _ = token_for_login(source_login, src_agency, direct_tokens())
        if src_token:
            jbt = v5_call(
                "feeds", "get", src_token, source_login,
                {
                    "SelectionCriteria": {"Ids": [int(source_feed_id)]},
                    "FieldNames": ["Id", "BusinessType", "SourceType"],
                },
            )
            raw_feeds = (jbt.get("result") or {}).get("Feeds") or []
            if raw_feeds:
                bt = (raw_feeds[0].get("BusinessType") or "").strip()
                if bt in _KNOWN_BUSINESS_TYPES:
                    business_type = bt
    except Exception:  # noqa: BLE001 — best-effort; fallback AUTO достаточен для авто-дилеров
        pass

    # ── Шаг 4: Токен целевого аккаунта ───────────────────────────────────────
    tgt_agency = resolve_agency_hint(target_login, "")
    tgt_token, _ = token_for_login(target_login, tgt_agency, direct_tokens())
    if not tgt_token:
        raise RuntimeError(
            f"нет рабочего токена для целевого аккаунта {target_login!r}"
        )

    # ── Шаг 5: feeds.add в целевом аккаунте ─────────────────────────────────
    resp = v5_call(
        "feeds", "add", tgt_token, target_login,
        {
            "Feeds": [
                {
                    "Name": src_name,
                    "BusinessType": business_type,
                    "SourceType": "URL",
                    "UrlFeed": {"Url": new_url},
                }
            ]
        },
    )

    if resp.get("error"):
        try:
            from direct import yandex_gateway as _yg  # noqa: PLC0415
            err_str = _yg.v5_err(resp)
        except Exception:  # noqa: BLE001
            err_str = str(resp["error"])[:300]
        raise RuntimeError(f"feeds.add вернул ошибку: {err_str}")

    add_results = (resp.get("result") or {}).get("AddResults") or []
    if not add_results:
        raise RuntimeError("feeds.add: пустой AddResults в ответе Директа")

    item = add_results[0]
    item_errors = item.get("Errors") or []
    if item_errors:
        e0 = item_errors[0]
        msg = e0.get("Message") or e0.get("Details") or str(e0)
        raise RuntimeError(f"feeds.add: ошибка элемента: {msg[:200]}")

    new_feed_id = item.get("Id")
    if not new_feed_id:
        raise RuntimeError("feeds.add: Id отсутствует в ответе AddResults[0]")

    return {
        "feed_id": int(new_feed_id),
        "name": src_name,
        "url": new_url,
    }
