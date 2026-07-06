"""Одноразовый парсер справочника «марка → модели» из XML-фидов (YML) аккаунтов.

Источник: товарные фиды клиентов (yml_catalog / offers). Поля <vendor> (марка) и <model>
(модель, но с «хвостом» цены/CTA — «Vesta Седан от 839 000 Р. Звоните»). Чистим хвост, получаем
короткое имя модели. Результат кладём в постоянный JSON-справочник (blueprint пишет файл), обновляем
ТОЛЬКО по явной команде (кнопка «обновить список»), не на каждую загрузку страницы.

Модуль без зависимостей от проекта — только stdlib. Канонизацию vendor→mark делает blueprint
(у него _brand_canon), сюда отдаём сырые vendor-лейблы.
"""
from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ET

# Кандидатные пути фидов (плейсхолдер-хост подставляется доменом клиента). Модельные листинги —
# только у «универсального модельного» каталога; оффер-лендинги моделей не содержат. Пробуем по
# порядку, берём первый, где распарсились offers с vendor/model.
MODEL_FEED_PATHS: tuple[str, ...] = (
    "/yandex-catalog-model-design-custom-name.xml",
    "/yandex-catalog-custom-name.xml",
    "/yandex-used-auto.xml",
)

# «Хвост» модели: всё, начиная с « от <цена>» (кириллическое «от» + пробел). Плюс маркер «Новый».
_TAIL_RE = re.compile(r"\s+от\s+\d.*$", re.IGNORECASE)
_NOVIY_RE = re.compile(r"\s+новый\s*$", re.IGNORECASE)


def clean_model_name(raw: str) -> str:
    """«Vesta Седан от 839 000 Р. Звоните» → «Vesta Седан»; «X70  Новый от 1 416 000 …» → «X70»."""
    s = (raw or "").strip()
    if not s:
        return ""
    s = _TAIL_RE.sub("", s)
    s = _NOVIY_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fetch(url: str, timeout: int = 25) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (feed-models)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — доверенные домены клиентов
            return r.read()
    except Exception:  # noqa: BLE001 — сетевой сбой домена не валит остальные
        return None


def parse_feed_bytes(data: bytes) -> dict[str, set[str]]:
    """XML YML-фид → {vendor_label: {clean_model, …}}. Пустой dict, если это не YML/нет offers."""
    out: dict[str, set[str]] = {}
    try:
        root = ET.fromstring(data)
    except Exception:  # noqa: BLE001 — не XML (404-html и т.п.)
        return out
    offers = root.find(".//offers")
    if offers is None:
        return out
    for o in offers.findall("offer"):
        vendor = (o.findtext("vendor") or "").strip()
        model = clean_model_name(o.findtext("model") or "")
        if not vendor or not model:
            continue
        out.setdefault(vendor, set()).add(model)
    return out


def build_from_domains(domains: list[str], *, timeout: int = 25) -> dict:
    """Обходит домены, тянет первый рабочий модельный фид, сливает vendor→models.

    → {"vendors": {vendor_label: sorted[models]}, "diag": [{domain, path, http|error, offers}]}.
    """
    merged: dict[str, set[str]] = {}
    diag: list[dict] = []
    for dom in domains:
        dom = (dom or "").strip().strip("/")
        if not dom:
            continue
        hit = False
        for path in MODEL_FEED_PATHS:
            url = f"https://{dom}{path}"
            data = _fetch(url, timeout=timeout)
            if data is None:
                diag.append({"domain": dom, "path": path, "error": "fetch_failed"})
                continue
            vendors = parse_feed_bytes(data)
            n_off = sum(len(v) for v in vendors.values())
            if not vendors:
                diag.append({"domain": dom, "path": path, "http": 200, "offers": 0})
                continue
            for v, models in vendors.items():
                merged.setdefault(v, set()).update(models)
            diag.append({"domain": dom, "path": path, "http": 200,
                         "vendors": len(vendors), "models": n_off})
            hit = True
            break
        if not hit:
            diag.append({"domain": dom, "path": "*", "error": "no_model_feed"})
    return {"vendors": {v: sorted(ms) for v, ms in merged.items()}, "diag": diag}
