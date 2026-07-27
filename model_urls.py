"""URL-хелперы объявлений (slug, глубокие ссылки на модели, домен-фида) — вынесено из blueprint.py.

Чистый stdlib (re), без DI, без БД. Инвариант wiring-hub: НЕ импортирует blueprint.
"""
from __future__ import annotations

import re


_SITE_TYPE_URL_TPL: dict[str, str | None] = {
    "Мультибренд": "/auto/{brand_slug}/{model_slug}",
    "Монобренд":   "/auto/{brand_slug}/{model_slug}",
    "Квиз":        None,   # лендинг-квиз, страниц моделей нет → только главная
    "С пробегом":  "/catalog/{brand_slug}/{model_slug}",
    "Мульти + БУ": "/auto/{brand_slug}/{model_slug}",
    "Неопределено": None,
    "Не трогать!": None,
}


def _slugify(name: str) -> str:
    """Строка → slug URL: нижний регистр, пробелы/дефисы, убрать всё лишнее.
    Пример: 'Haval Jolion' → 'haval-jolion', 'LADA Granta' → 'lada-granta'.
    Кириллица транслитерируется по минимальной таблице авто-брендов."""
    _CYR = {
        "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"yo","ж":"zh",
        "з":"z","и":"i","й":"j","к":"k","л":"l","м":"m","н":"n","о":"o",
        "п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"kh","ц":"ts",
        "ч":"ch","ш":"sh","щ":"shch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
    }
    s = (name or "").strip().lower()
    out = []
    for ch in s:
        if ch in _CYR:
            out.append(_CYR[ch])
        elif ch.isalnum() or ch in "-_":
            out.append(ch)
        elif ch in " \t_/":
            out.append("-")
        # else: пропускаем (спецсимволы, скобки)
    slug = "-".join(p for p in "".join(out).split("-") if p)
    return slug[:60]


def _strip_url_query(u: str) -> str:
    """Срезать query-string (?...) и fragment (#...) из URL."""
    u = (u or "").strip()
    for sep in ("?", "#"):
        i = u.find(sep)
        if i >= 0:
            u = u[:i]
    return u.rstrip("/")


def _brand_level_url(u: str) -> str:
    """Обрезать абсолютный URL до уровня марки: домен + первые 2 сегмента пути.
    Пример: https://site/auto/baic/u5-plus/i/sedan?fid=x → https://site/auto/baic.
    Ожидает абсолютный URL из фида (targetUrl)."""
    u = _strip_url_query(u)
    if not u:
        return ""
    m = re.match(r"(https?://[^/]+)(.*)", u)
    if not m:
        return u
    origin, path = m.group(1), m.group(2)
    parts = [p for p in path.split("/") if p]
    brand_path = "/" + "/".join(parts[:2]) if len(parts) >= 2 else ("/" + parts[0] if parts else "")
    return origin + brand_path


def _is_degenerate_feed_url(u: str) -> bool:
    """True если URL ИЗ ФИДА не ведёт на страницу марки/модели и его нельзя брать как href.

    Вырожденные варианты: пусто, голый корень домена, путь `/quiz(/…)`.

    Зачем (AD_HREF_ROOT_INSTEAD_OF_MODEL): у части салонов единственный оффер в account-мёрже
    фидов приходит из КВИЗ-фида — `targetUrl = https://site/quiz?fid=…#anchor`. Такой URL
    проходит фид-first ветку builder'ов, а `link_check.strip_quiz_url` схлопывает любой `/quiz`
    в голый корень домена → объявление уходит на главную вместо страницы марки.

    Гард ставится ПОСЛЕ фид-лукапа и ДО link_check: фид игнорируется, href строится формулой
    `_model_page_href`. Проверяется ТОЛЬКО URL из фида, поэтому легальный фолбэк формулы
    (`/auto/uaz` → 404 → `_parent_path` → `/auto`) под гард не попадает — он рождается уже
    после гарда, в resolve-цепочке.
    """
    s = _strip_url_query(u)
    if not s:
        return True
    m = re.match(r"(https?://[^/]+)(.*)$", s)
    if not m:
        return False              # не абсолютный URL — не наша зона, поведение прежнее
    parts = [p for p in (m.group(2) or "").split("/") if p]
    if not parts:
        return True               # голый корень домена — марки/модели в нём нет
    return parts[0].lower() == "quiz"


def _href_host(href: str = "") -> str:
    """Hostname аккаунта из href, без схемы/www/пути/query. Пусто → ''."""
    h = (href or "").lower().strip()
    if not h:
        return ""
    for pfx in ("https://www.", "http://www.", "https://", "http://"):
        if h.startswith(pfx):
            h = h[len(pfx):]
            break
    if h.startswith("www."):
        h = h[4:]
    return h.split("/")[0].split("?")[0]


def _is_site_domain_name(f_name: str, href: str = "") -> bool:
    """True если f_name совпадает с hostname аккаунта (href) — пропустить в имени кампании.
    Защита от вставки домена (напр. «autos-kemerovo.site») вместо имени фида контента."""
    if not f_name or not href:
        return False
    nm = (f_name or "").strip().lower()
    if nm.startswith("www."):
        nm = nm[4:]
    host = _href_host(href)
    return bool(host and nm == host)


def _strip_site_domain_label(label: str, href: str = "") -> str:
    """Срезать домен аккаунта из МЕТКИ, которая реально уходит в имя кампании.

    Гард `_is_site_domain_name` сверял КОРОТКОЕ имя фида и только на ТОЧНОЕ равенство хосту,
    а в имя подставляется другое: URL фида без схемы («carsklad-126.site/yandex-catalog.xml»)
    или имя из кабинета с доменом-префиксом («carsklad-126.site — yandex-catalog-…»). Домен
    проходил в имя кампании. Режем ПРЕФИКС хоста + ведущие разделители.

    Пусто на выходе = метка была только доменом → суффикс к имени не добавлять.
    Различимость fan-out сохраняется: путь/имя фида после домена у фидов разные.
    """
    s = (label or "").strip()
    if not s:
        return ""
    host = _href_host(href)
    if not host:
        return s
    low = s.lower()
    stripped = False
    for cand in ("www." + host, host):
        if low.startswith(cand):
            s = s[len(cand):]
            stripped = True
            break
    if not stripped:
        return s          # хоста в метке нет → метка легитимна целиком, разделитель НЕ трогаем
    s = s.strip()
    # Режем РОВНО ОДИН разделитель между доменом и меткой. lstrip по НАБОРУ символов
    # («—–-/|:· ») съедал ведущий дефис легитимной метки («-catalog.xml» → «catalog.xml»),
    # поэтому дефис принимается только как «дефис + пробел», не как первый символ метки.
    for sep in ("— ", "– ", "- ", "—", "–", "/", "|", ":", "·"):
        if s.startswith(sep):
            s = s[len(sep):]
            break
    return s.strip()


def _model_page_href(base_href: str, site_type: str, model_name: str) -> str:
    """Построить deep-link страницы модели для объявления.

    base_href:  корневой URL сайта (например https://ac-aceauto.ru)
    site_type:  тип сайта из local_gsheet_sites (Мультибренд / Монобренд / С пробегом / …)
    model_name: название модели из ag_part1 (например «Haval Jolion», «LADA Granta»)

    Логика:
    - У модели «Haval Jolion» первое слово — бренд, остальное — модель.
    - Монобренд: бренд уже в домене, но URL та же /auto/{brand}/{model} (проверено live).
    - Нет шаблона для типа (Квиз/None) ИЛИ нет имени модели → возвращаем голый base_href.
    - Slugify: 'Haval Jolion' → brand_slug='haval', model_slug='jolion'.

    Примеры (проверено HEAD 2026-06-22):
      Мультибренд «LADA Granta»  → /auto/lada/granta
      Монобренд «Belgee X50»     → /auto/belgee/x50
      С пробегом «Haval Jolion»  → /catalog/haval/jolion
    """
    tpl = _SITE_TYPE_URL_TPL.get(site_type)
    if not tpl or not model_name:
        return base_href.rstrip("/")
    parts = (model_name or "").strip().split(None, 1)  # split по первому пробелу
    if len(parts) < 2:
        # Только МАРКА (группа сегмента «Марки», напр. «Lada»): deep-link на страницу марки
        # /auto/{brand} (без модели). Правило Семёна: марочное комбо-объявление ведёт на марку,
        # не на главную. Пример: Lada → https://site/auto/lada.
        _bs = _slugify(parts[0]) if parts else ""
        if not _bs:
            return base_href.rstrip("/")
        return base_href.rstrip("/") + tpl.format(brand_slug=_bs, model_slug="").rstrip("/")
    brand_slug = _slugify(parts[0])
    model_slug = _slugify(parts[1])
    if not brand_slug or not model_slug:
        return base_href.rstrip("/")
    path = tpl.format(brand_slug=brand_slug, model_slug=model_slug)
    return base_href.rstrip("/") + path

