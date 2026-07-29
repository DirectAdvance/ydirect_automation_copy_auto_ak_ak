"""
create_master_campaign.py
=========================
Создание «Мастер кампаний» (Универсальная кампания, UAC) в Яндекс.Директ
через ВНУТРЕННИЙ REST API ``/web-api/uac/`` на куках браузерной сессии.

Почему так
----------
Официальный Direct API v5 (``campaigns.add``) НЕ умеет создавать «Мастер
кампаний» — отдельного типа в нём нет. Веб-интерфейс мастера создаёт кампанию
через приватный REST ``/web-api/uac/``. Здесь воспроизведена ровно эта цепочка
(реверс снят с HAR браузера, аккаунт porg-h27zek57).

Цепочка (всё на куках агентского аккаунта + ``ulogin`` клиента)
----------------------------------------------------------------
1. ``GET  /web-api/uac/linkinfo?ulogin=&url=``   — тип лендинга + bootstrap CSRF
2. ``POST /web-api/uac/content?...``             — регистрация картинки/видео → content_id (опц.)
3. ``POST /web-api/uac/campaigns?ulogin=``       — СОЗДАНИЕ черновика → ``result.id``
4. ``POST /web-api/uac/campaign/{id}/status/``   — запуск (опц., ``target_status=started``)

Авторизация
-----------
* ``Cookie``        — строка кук агентского аккаунта (ОСНОВНОЙ источник — главпоток
                      ``glavpotok.ru/api/cookies/yandex-direct/<login>``; fallback — ``.secret/cookies.json``)
* ``x-csrf-token``  — из куки ``_direct_csrf_token`` (берётся из ответа первого запроса)
* ``x-direct-api: 1``, ``x-client-versions: [{"uac":"893"}]``
* query-параметр ``ulogin`` — логин клиента, от чьего имени создаём (для агентств)

ВАЖНО
-----
Создаётся РЕАЛЬНАЯ кампания (черновик). Без шага 4 (status=started) она не
запускается и денег не тратит. Это приватный недокументированный API — может
сломаться при изменении схемы Яндексом. Куки протухают — обновлять.

Архитектура (2026-07-18)
------------------------
campaign.py — RE-EXPORT ХАБ. Реальный код живёт в:
  direct_v501_client.py — DirectV501Client, UnifiedCampaignSpec, build_v501_client
  uac_client.py         — UacClient, MasterCampaignSpec, UacApiError, collect_image_files и др.
Все 30+ импортёров продолжают делать ``from . import campaign as cmc`` — namespace не изменился.

Пример
------
    from create_master_campaign import MasterCampaignSpec, build_client

    spec = MasterCampaignSpec(
        href="https://autobu-tula.ru",
        display_name="autobu-tula.ru от 17.06.26",
        titles=["Автовыкуп в Туле", "Продать авто быстро"],
        texts=["Деньги сразу. Оценка за 5 минут."],
        region_ids=[225],            # 225 = Россия
        counter_id=107942930,        # счётчик Метрики
        goal_id=535537104,           # цель
        cpa=2000,                    # ₽ за конверсию
        week_budget=5000,            # ₽/неделя
        image_urls=[],               # ссылки на картинки (необяз.)
        video_urls=["https://storage.mds.yandex.net/.../x.mp4"],
    )
    client = build_client(ulogin="porg-h27zek57")  # куку подберёт автоматически
    campaign_id = client.create_master_campaign(spec, launch=False)
    print("Создано, id =", campaign_id)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# ─── Re-export: v501 клиент ───────────────────────────────────────────────────
# Все 30+ импортёров используют ``from . import campaign as cmc`` и берут
# символы через cmc.*. Re-export через явный импорт (не *) гарантирует, что
# приватные символы (_dead_feed_ids, _UC_CHANNEL_MODES и др.) тоже в namespace.

from .direct_v501_client import (        # noqa: E402, F401
    V501_BASE,
    _UC_CHANNEL_MODES,
    UnifiedCampaignSpec,
    _dedup_keep_order,
    DirectV501Error,
    DirectV501Client,
    build_v501_client,
)

# ─── Re-export: UAC клиент ────────────────────────────────────────────────────

from .uac_client import (                # noqa: E402, F401
    BASE,
    USER_AGENT,
    UAC_CLIENT_VERSION,
    DEFAULT_COOKIE_ACCOUNTS,
    DEFAULT_DISPLAY_NAME,
    UTM_TEMPLATE,
    _TIME_BOARD_ALWAYS,
    MasterCampaignSpec,
    _IMAGE_EXTS,
    _guess_mime,
    _IMG_PHASH_CACHE,
    _image_phash,
    collect_image_files,
    _audience_goals,
    _norm_sitelinks,
    _dead_feed_ids,
    UacApiError,
    UacClient,
)

# ─── Управляющее агентство (DI-синглтон) ──────────────────────────────────────
#
# Task #34 (2026-07-10): резолвер УПРАВЛЯЮЩЕГО агентства для саб-логина (porg-*).
# Куку с главпотока по САБ-логину брать НЕЛЬЗЯ (fetch_cookie_glavpotok("porg-*") = 404 → None) —
# ВСЕГДА берётся кука АГЕНТСТВА-оператора. pick_working_cookie перебирает DEFAULT_COOKIE_ACCOUNTS,
# но раньше без приоритета: первая ЖИВАЯ агентская кука побеждала, даже если это агентство НЕ
# управляет данным саб-логином → Grid-чтение/ремонт получали «No rights» и верификатор был СЛЕП
# на агентских саб-аккаунтах (7 дефектов R2-8 не поймала автоматика). Blueprint инъектит сюда
# _resolve_agency_hint (кэш БД + local_gsheet_sites), чтобы УПРАВЛЯЮЩЕЕ агентство пробовалось ПЕРВЫМ.
_AGENCY_RESOLVER = None   # type: ignore[var-annotated]


def set_agency_resolver(fn) -> None:
    """Инъекция резолвера агентства (login → agency_login|''). Ставит blueprint при импорте,
    чтобы campaign.py не импортировал blueprint (циклический импорт) и не лез в БД напрямую."""
    global _AGENCY_RESOLVER
    _AGENCY_RESOLVER = fn


def _resolve_managing_agency(ulogin: str) -> str:
    """Управляющее агентство для ulogin через инъектированный резолвер (best-effort, '' при сбое)."""
    fn = _AGENCY_RESOLVER
    if not fn or not ulogin:
        return ""
    try:
        return (fn(ulogin) or "").strip().lower()
    except Exception:  # noqa: BLE001 — резолвер best-effort: сбой → перебор всех агентств как раньше
        return ""


# ─── Куки и CSRF ──────────────────────────────────────────────────────────────


def _find_secret_dir(start: Path | None = None) -> Path:
    cur = (start or Path(__file__)).resolve()
    for parent in cur.parents:
        if (parent / ".secret" / "loader.py").exists():
            return parent / ".secret"
    raise FileNotFoundError(".secret/ не найден (ищу вверх от модуля)")


def _glavpotok_cfg() -> dict:
    """Конфиг доступа к glavpotok.ru (base_url + bearer token).

    Совместимо со СТАРЫМ loader.py (на LXC 101 нет load_glavpotok_cookies) —
    тогда читаем переменные .env напрямую через loader._get.
    """
    sd = str(_find_secret_dir())
    if sd not in sys.path:
        sys.path.insert(0, sd)
    try:
        from loader import load_glavpotok_cookies  # noqa: E402
        return load_glavpotok_cookies()
    except (ImportError, AttributeError):
        from loader import _get  # noqa: E402
        return {
            "base_url": _get("GLAVPOTOK_COOKIES_URL", "https://glavpotok.ru/api/cookies/yandex-direct"),
            "token": _get("GLAVPOTOK_COOKIES_TOKEN"),
        }


def _cookie_retry_delay_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("DIRECT_COOKIE_RETRY_DELAY_SECONDS", "30")))
    except (TypeError, ValueError):
        return 30.0


def _cookie_retryable_auth_text(text: str) -> bool:
    """True only for stale/login-page signals, not for per-client rights errors.

    ``No rights``/``Нет прав`` means the agency session is alive but does not manage the
    requested ``ulogin``; waiting/re-fetching the same agency cookie will not fix that.
    """
    s = (text or "").lower()
    return (
        "need_reset" in s
        or "passport.yandex" in s
        or "истек срок" in s
        or "истёк срок" in s
        or "<title>log in</title>" in s
        or ("<html" in s and "login" in s and "direct.yandex" not in s)
    )


def fetch_cookie_glavpotok(login: str) -> str | None:
    """Свежая строка кук агентского аккаунта из главпотока — ОСНОВНОЙ источник.

    GET <base_url>/<login> с Authorization: Bearer <token> → cookie_string.
    None, если конфиг недоступен / у главпотока нет куки (404) / пустой ответ.
    (cookies.json быстро протухает — потому по умолчанию берём отсюда.)
    Транспортные сбои/5xx главпотока ретраятся один раз после 30с (env:
    DIRECT_COOKIE_RETRY_DELAY_SECONDS). 404/пустой ответ не ретраим.
    """
    try:
        cfg = _glavpotok_cfg()
    except Exception:  # noqa: BLE001 — нет конфига → пусть сработает fallback на cookies.json
        return None
    if not cfg.get("token"):
        return None
    for attempt in range(2):
        try:
            r = requests.get(f"{cfg['base_url']}/{login}",
                             headers={"Authorization": f"Bearer {cfg['token']}"},
                             timeout=20)
        except requests.RequestException:
            if attempt == 0:
                time.sleep(_cookie_retry_delay_seconds())
                continue
            return None
        if r.status_code == 200:
            try:
                cs = (r.json().get("cookie_string") or "").strip()
            except Exception:  # noqa: BLE001
                return None
            return cs or None
        if r.status_code >= 500 and attempt == 0:
            time.sleep(_cookie_retry_delay_seconds())
            continue
        return None
    return None


def load_cookie_local(account: str) -> str:
    """Fallback: строка кук из .secret/cookies.json (может быть протухшей)."""
    secret_dir = _find_secret_dir()
    paths = [
        secret_dir / "cookies.json",
        secret_dir / "yandex_direct" / "cookies.json",
        secret_dir / "yandex_direct" / "cookies" / "cookies.json",
    ]
    cookie_path = next((p for p in paths if p.exists()), paths[0])
    data = json.loads(cookie_path.read_text(encoding="utf-8"))
    cookie = data.get(account)
    if not cookie:
        raise KeyError(f"в cookies.json нет аккаунта {account!r}; есть: {list(data)}")
    return cookie


def load_cookie(account: str) -> str:
    """Кука аккаунта: сначала главпоток (свежая), при неудаче — локальный cookies.json."""
    cs = fetch_cookie_glavpotok(account)
    if cs:
        return cs
    return load_cookie_local(account)


# ─── Фиды ─────────────────────────────────────────────────────────────────────


def load_feeds_catalog(path: str | Path | None = None) -> dict:
    """Каталог товарных фидов (feeds_catalog.yaml) — для выпадающего списка и товарной РК."""
    import yaml
    p = Path(path) if path else Path(__file__).resolve().parent / "feeds_catalog.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def feed_url(site: str, path: str) -> str:
    """URL фида под домен клиента: site + path (хост-плейсхолдер каталога не используется)."""
    host = re.sub(r"^https?://", "", site).strip("/").split("/")[0]
    return f"https://{host}{path}"


def list_feeds_for_site(site: str, catalog: dict | None = None) -> list[dict]:
    """Плоский список фидов под домен: [{group, label, kind, url}] — для дропдауна."""
    cat = catalog or load_feeds_catalog()
    out = []
    for g in cat.get("groups", []):
        for it in g.get("items", []):
            out.append({"group": g["group"], "label": it["label"],
                        "kind": it.get("kind"), "url": feed_url(site, it["path"])})
    return out


# ─── Cookie-store ─────────────────────────────────────────────────────────────
# Кэш рабочей куки НА АККАУНТ (ulogin → (cookie, ts)). Проверка куки делается ОДИН раз на аккаунт
# перед созданием всех кампаний (а не на каждую) — правило пользователя. TTL короче времени жизни
# сессии Директа (куки живут часами; джоба — минуты), при истечении/сбое 403 — force_refresh.

_ACCOUNT_COOKIE_CACHE: dict[str, tuple[str, float]] = {}
_ACCOUNT_COOKIE_TTL = 20 * 60   # 20 минут


def remember_working_cookie(ulogin: str, cookie: str) -> None:
    """Запомнить уже проверенную куку для ulogin.

    Нужна после preflight: он берёт свежую агентскую куку из главпотока, а Grid-клиенты
    ниже по цепочке вызывают pick_working_cookie(login). Без этого они могут попасть
    в старую cached/local куку и получить HTML Login вместо JSON.
    """
    if ulogin and cookie:
        _ACCOUNT_COOKIE_CACHE[ulogin] = (cookie, time.time())


def pick_working_cookie(ulogin: str, accounts: tuple[str, ...] = DEFAULT_COOKIE_ACCOUNTS,
                        *, force_refresh: bool = False) -> str:
    """Рабочая агентская кука для ulogin. Фаза 2 gateway: не-брокерные процессы берут её через
    единый direct-gateway (:5025) — ОДИН probe главпотока/кэш на все 6 процессов вместо шести
    независимых. Сам брокер (DIRECT_GATEWAY_SELF=1) и ЛЮБОЙ сбой брокера → локальная логика
    (_pick_working_cookie_local). Рекурсии нет: gateway_client.gw_cookie фолбэчит в _local, не сюда."""
    if os.environ.get("DIRECT_GATEWAY_SELF") != "1":
        try:
            from . import gateway_client as _gwc
            ck = _gwc.gw_cookie(ulogin, accounts=accounts, force_refresh=force_refresh)
            if ck:
                return ck
        except Exception:  # noqa: BLE001 — брокер недоступен/ошибка → локальная логика ниже
            pass
    return _pick_working_cookie_local(ulogin, accounts, force_refresh=force_refresh)


def _pick_working_cookie_local(ulogin: str, accounts: tuple[str, ...] = DEFAULT_COOKIE_ACCOUNTS,
                               *, force_refresh: bool = False) -> str:
    """Локальная логика подбора куки (probe главпотока + кэш ЭТОГО процесса). Прямой источник
    правды для брокера и аварийный фолбэк. Порядок подбора:
      1) уже проверенная кука из кэша (если не force_refresh и не протухла);
      2) ЛОКАЛЬНАЯ кука агентства (cookies.json) — проверяем link_info: работает → берём её;
      3) не работает / нет локальной → СВЕЖАЯ с ГЛАВПОТОКА для этого агентства, снова проверяем.
    force_refresh=True (например после 403 Grid) — игнорировать кэш и перепроверить с нуля."""
    if not force_refresh:
        hit = _ACCOUNT_COOKIE_CACHE.get(ulogin)
        if hit and (time.time() - hit[1]) < _ACCOUNT_COOKIE_TTL:
            return hit[0]
    # Task #34: УПРАВЛЯЮЩЕЕ агентство саб-логина — ПЕРВЫМ в переборе (иначе первая живая, но
    # НЕуправляющая агентская кука победит → Grid «No rights» → слепой верификатор). Дубли
    # убираем, сохраняя порядок; остальные агентства остаются фолбэком.
    _mng = _resolve_managing_agency(ulogin)
    if not _mng and len(accounts) == 1:
        # Explicit single-account probes (for example from a token/agency override
        # or a gateway fallback) are already scoped to the intended agency. Preserve
        # its terminal rights error instead of degrading to generic "no cookie fit".
        _mng = str(accounts[0] or "").strip().lower()
    if _mng and _mng not in ("none", ""):
        accounts = tuple(dict.fromkeys((_mng, *accounts)))
    last_err: Exception | None = None
    managing_errs: list[str] = []
    expired_accs: list[str] = []   # only when the fresh Glavpotok source also confirms expiry
    fresh_seen: set[str] = set()
    for acc in accounts:
        # Главпоток is the source of truth. Local cookies are static fallbacks and
        # may be expired even while the live agency session is healthy.
        for getter in (fetch_cookie_glavpotok, load_cookie_local):
            is_fresh = getter is fetch_cookie_glavpotok
            max_probe_attempts = 2 if is_fresh else 1
            for probe_attempt in range(max_probe_attempts):
                try:
                    cookie = getter(acc)
                except Exception:  # noqa: BLE001 — нет такого аккаунта/конфига → следующий источник
                    break
                if not cookie:
                    break
                if is_fresh:
                    fresh_seen.add(acc)
                try:
                    UacClient(cookie, ulogin).link_info("https://ya.ru")
                    _ACCOUNT_COOKIE_CACHE[ulogin] = (cookie, time.time())
                    return cookie
                except Exception as e:  # noqa: BLE001 — кука не подошла → следующий источник/аккаунт
                    last_err = e
                    if _mng and acc == _mng:
                        body = str(getattr(e, "body", "") or e)
                        label = "fresh" if is_fresh else "local"
                        managing_errs.append(f"{label}: {body[:240]}")
                    # Retry только когда свежая кука с главпотока выглядит протухшей/login-page.
                    # ``No rights``/``Нет прав`` НЕ ретраим: это доступ агентства к ulogin.
                    retryable_auth = is_fresh and _cookie_retryable_auth_text(
                        str(getattr(e, "body", "") or e)
                    )
                    if retryable_auth and probe_attempt == 0:
                        time.sleep(_cookie_retry_delay_seconds())
                        continue
                    if retryable_auth and acc not in expired_accs:
                        expired_accs.append(acc)
                    break
    if expired_accs and not fresh_seen:
        # Чёткое actionable-сообщение → видно в UI/джобе: куку надо ОБНОВИТЬ на главпотоке (перелогин).
        raise RuntimeError(
            f"кука протухла на главпотоке (сессия Яндекса истекла) для агентств: "
            f"{', '.join(expired_accs)}. Обновите куку в главпотоке (перелогиньтесь в этом "
            f"агентском аккаунте) — текущая для ulogin={ulogin} мертва [need_reset].")
    if _mng and managing_errs:
        details = " | ".join(managing_errs)
        raise RuntimeError(
            f"кука управляющего агентства {_mng} не имеет web/Grid-прав к ulogin={ulogin}: "
            f"{details}")
    raise RuntimeError(f"ни одна кука не подошла к ulogin={ulogin}: {last_err}")


def build_client(ulogin: str, *, account: str | None = None) -> UacClient:
    """Готовый клиент: если account не задан — автоподбор рабочей куки."""
    cookie = load_cookie(account) if account else pick_working_cookie(ulogin)
    if account and cookie:
        remember_working_cookie(ulogin, cookie)
    return UacClient(cookie, ulogin)


# ─── Входной файл (YAML/JSON) ─────────────────────────────────────────────────

# Поля управления (не часть объявления) — отделяем от полей MasterCampaignSpec.
_CONTROL_KEYS = {"ulogin", "account", "launch"}

# Алиасы: имена во входном файле → имена полей MasterCampaignSpec.
_INPUT_ALIASES = {
    "minus_region_ids": "minus_regions",
}


def spec_from_dict(data: dict) -> MasterCampaignSpec:
    """Собирает MasterCampaignSpec из словаря входного файла (игнорит ulogin/account/launch)."""
    valid = {f for f in MasterCampaignSpec.__dataclass_fields__}  # type: ignore[attr-defined]
    kwargs = {}
    for k, v in data.items():
        if k in _CONTROL_KEYS:
            continue
        key = _INPUT_ALIASES.get(k, k)
        if key in valid:
            kwargs[key] = v
    return MasterCampaignSpec(**kwargs)


def load_input(path: str | Path) -> tuple[MasterCampaignSpec, str, str | None, bool]:
    """Читает YAML/JSON входной файл → (spec, ulogin, account, launch)."""
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        import yaml  # локальный импорт: нужен только при YAML-входе
        data = yaml.safe_load(raw)
    else:
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"входной файл {p} должен быть объектом (dict), а не {type(data).__name__}")
    ulogin = data.get("ulogin")
    if not ulogin:
        raise ValueError("во входном файле обязателен 'ulogin'")
    return spec_from_dict(data), ulogin, data.get("account"), bool(data.get("launch", False))


# ─── CLI для теста ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Создать «Мастер кампаний» через приватный UAC API")
    ap.add_argument("--input", default=None,
                    help="входной YAML/JSON файл (campaign_input.yaml) — берёт все данные оттуда")
    ap.add_argument("--ulogin", help="логин клиента (напр. porg-h27zek57)")
    ap.add_argument("--account", default=None, help="агентский аккаунт куки (иначе автоподбор)")
    ap.add_argument("--href")
    ap.add_argument("--title", action="append", help="заголовок (можно несколько)")
    ap.add_argument("--text", action="append", help="текст (можно несколько)")
    ap.add_argument("--region", action="append", type=int)
    ap.add_argument("--counter", type=int)
    ap.add_argument("--goal", type=int)
    ap.add_argument("--cpa", type=int)
    ap.add_argument("--budget", type=int, help="недельный бюджет, ₽")
    ap.add_argument("--name", default=None)
    ap.add_argument("--image", action="append", default=[])
    ap.add_argument("--video", action="append", default=[])
    ap.add_argument("--launch", action="store_true", help="сразу запустить (на модерацию)")
    args = ap.parse_args()

    if args.input:
        spec, ulogin, account, launch = load_input(args.input)
    else:
        required = {"ulogin": args.ulogin, "href": args.href, "title": args.title,
                    "text": args.text, "counter": args.counter, "goal": args.goal,
                    "cpa": args.cpa, "budget": args.budget}
        missing = [k for k, v in required.items() if not v]
        if missing:
            ap.error(f"без --input обязательны: {', '.join('--'+m for m in missing)}")
        spec = MasterCampaignSpec(
            href=args.href, titles=args.title, texts=args.text,
            region_ids=args.region or [225], counter_id=args.counter, goal_id=args.goal,
            cpa=args.cpa, week_budget=args.budget, display_name=args.name,
            image_urls=args.image, video_urls=args.video,
        )
        ulogin, account, launch = args.ulogin, args.account, args.launch

    client = build_client(ulogin, account=account)
    try:
        cid = client.create_master_campaign(spec, launch=launch)
    except UacApiError as e:
        print(f"Ошибка {e.step}: HTTP {e.status}\n{e.body[:600]}", file=sys.stderr)
        sys.exit(1)
    print(f"Создана кампания id={cid} (ulogin={ulogin}, "
          f"{'ЗАПУЩЕНА' if launch else 'черновик'})")
