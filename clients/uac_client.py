"""
UAC (Мастер кампаний / Товарная) клиент для Яндекс.Директа.

Создание кампаний tp6 (Мастер кампаний) и tp7 (Товарка) через приватный
REST API ``/web-api/uac/`` на куках браузерной сессии.

Вынесено из campaign.py (был monolith 2047 строк) через re-export фасад:
  campaign.py re-exports всё отсюда, импортёры не меняются.
"""

from __future__ import annotations

import json
import mimetypes
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://direct.yandex.ru/web-api/uac"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# Версия фронта мастера — её Директ проверяет в заголовке.
# Если начнёт отдавать 426/«обновите страницу» — снять свежую из HAR.
UAC_CLIENT_VERSION = '[{"uac":"893"}]'

# Приоритет агентских кук для подбора рабочей сессии.
DEFAULT_COOKIE_ACCOUNTS = (
    "victoryagency-direct1618440",
    "victorylotsofads1",
    "victoryagency14",
    "y-direct-victory",
    "victoryagencydirect",
    "useful-call-agency",
)

# Имя кампании по умолчанию (пока ВСЕГДА так — тестовый режим).
DEFAULT_DISPLAY_NAME = "Тест API"

# UTM-метка — ВСЕГДА кладётся в ОТДЕЛЬНОЕ поле payload `tracking_params`
# (в интерфейсе мастера — «Дополнительные параметры → UTM-метки и параметры URL»),
# а НЕ приклеивается к href. {...} — динамические макросы Директа (подставляет он сам).
UTM_TEMPLATE = (
    "utm_source=s:{source}&utm_medium=cpc"
    "&utm_campaign={campaign_id}|{campaign_name}"
    "&utm_term={keyword}"
    "&utm_content=g:{gbid}|geoname:{region_name}|geoid:{region_id}"
    "|dev:{device_type}|r:{retargeting_id}|cor:{coef_goal_context_id}"
)

# Полный «зелёный» time_target из браузера: 7 дней × 24 часа = коэффициент 100 (показ всегда).
_TIME_BOARD_ALWAYS = [[100] * 24 for _ in range(7)]


# ─── Спецификация кампании (всё «для объявлений») ─────────────────────────────


@dataclass
class MasterCampaignSpec:
    """Входные данные «Мастер кампаний». Обязательное — без дефолтов, остальное — дефолты браузера."""

    # --- обязательное ---
    href: str                       # ссылка на продвигаемую страницу
    titles: list[str]               # заголовки объявлений (1..N)
    texts: list[str]                # тексты объявлений (1..N)
    region_ids: list[int]           # гео показа (225 = Россия, 213 = Москва, 2 = СПб ...)
    counter_id: int                 # id счётчика Яндекс.Метрики
    goal_id: int                    # id цели для оптимизации
    cpa: int | float                # целевая цена конверсии, ₽
    week_budget: int | float        # недельный бюджет, ₽

    # --- креативы (необязательно) ---
    image_dir: str | None = None                          # ПАПКА с картинками — грузятся все из неё
    image_files: list[str] = field(default_factory=list)  # отдельные локальные файлы картинок
    image_urls: list[str] = field(default_factory=list)   # прямые ссылки на картинки (по URL)
    image_limit: int = 5                                  # грузим до N успешно принятых картинок
    content_ids: list[str] = field(default_factory=list)  # уже загруженные UAC content ids (prepare-фаза)
    visual_dedup: bool = True                             # pHash-дедуп картинок. False → ТОЛЬКО path+md5
    #   (для dmp/не-авто лидогена: баннеры одного шаблона с разным текстом — РАЗНЫЕ объявления, Директ их
    #   принимает; pHash их ошибочно схлопывал → в UAC доезжало 2 из ~50). Авто tp6/tp7 = True (защита от клонов).
    video_urls: list[str] = field(default_factory=list)   # прямые ссылки на видео (mp4)
    video_files: list[str] = field(default_factory=list)  # ЛОКАЛЬНЫЕ mp4 — multipart (лимит 2 на мастер)

    # --- тип кампании ---
    campaign_type: str = "master"                         # "master" (Мастер кампаний) | "product" (Товарная)

    # --- ТОВАРНАЯ РК (ecom): нужен товарный фид ИЛИ листинги ---
    feed_id: int | None = None                            # фид «объявления для товаров»; его наличие → ecom:true
    feed_filters: list[dict] = field(default_factory=list)  # фильтр товарного по полю "model" (operator CONTAINS):
    #   [{"conditions":[{"field":"model","operator":"CONTAINS","value":"[\"Jolion\"]"}]}]  value = JSON-строка-массив!
    listings_feed_id: int | None = None                   # фид «страницы каталога» (ecom-листинги)
    listings_feed_filters: list[dict] = field(default_factory=list)  # фильтр листингов ТОЛЬКО по "collectionId":
    #   [{"conditions":[{"field":"collectionId","operator":"EQUALS","values":["mark_6"],"value":"[\"mark_6\"]"}]}]  (по имени модели НЕ фильтрует!)

    # --- опционально, с дефолтами как в интерфейсе мастера ---
    display_name: str | None = None                       # имя кампании (по умолчанию из href+даты)
    goal_type: str = "OTHER"
    pricing: str = "PER_CLICK"                            # PER_CLICK | PER_ACTION
    adv_type: str = "text"                                # тип «Конверсии и трафик» (сайт)
    device_types: list[str] = field(default_factory=lambda: ["all"])
    limit_period: str = "week"                            # период бюджета
    keywords: list[str] = field(default_factory=list)
    minus_keywords: list[str] = field(default_factory=lambda: ["отзывы"])  # минус-слова (всегда «отзывы»)
    minus_regions: list[int] = field(default_factory=list)
    sitelinks: list[dict] = field(default_factory=list)
    # Аудитории (INTERESTS/HOST/APPLICATION) → ca_retargeting_condition.goals[{id}].
    # Принимает id'шники ИЛИ объекты {"id":...,"name":...} (имя для читаемости YAML).
    audiences: list = field(default_factory=list)
    audience_interest_type: str = "short-term"             # short-term | all (как в интерфейсе)
    genders: list[str] = field(default_factory=lambda: ["female", "male"])
    age_lower: str = "age_18"
    age_upper: str = "age_inf"
    id_time_zone: int = 130                               # 130 = Москва
    utm_template: str = UTM_TEMPLATE                       # UTM → поле tracking_params ("" = без UTM)
    relevance_match_categories: list[str] = field(default_factory=lambda: [
        # COMPETITOR_MARK ОБЯЗАТЕЛЕН: его отсутствие = категория исключена → в UI чип
        # «Запросы с упоминанием брендов конкурентов» в минус-словах (НЕ убирать повторно!)
        "ALTERNATIVE_MARK", "ACCESSORY_MARK", "BROADER_MARK", "COMPETITOR_MARK", "EXACT_MARK",
    ])
    alternative_texts_enabled: bool = True
    ml_banners_enabled: bool = False
    yandex_maps_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.href.startswith("http"):
            raise ValueError(f"href должен быть полным URL: {self.href!r}")
        if not self.titles:
            raise ValueError("нужен хотя бы один заголовок (titles)")
        if not self.texts:
            raise ValueError("нужен хотя бы один текст (texts)")
        if not self.region_ids:
            raise ValueError("нужен хотя бы один регион (region_ids)")
        if self.campaign_type not in ("master", "product"):
            raise ValueError(f"campaign_type: master|product, а не {self.campaign_type!r}")
        if self.campaign_type == "product" and not (self.feed_id or self.listings_feed_id):
            raise ValueError("товарная РК (product): нужен feed_id или listings_feed_id "
                             "(товарный фид / листинги ecom-сайта)")


# ─── Хелперы (используются внутри UacClient) ──────────────────────────────────

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def _guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


_IMG_PHASH_CACHE: dict[str, int | None] = {}


def _image_phash(path: str, size: int = 32, low: int = 8) -> int | None:
    """Перцептивный hash (DCT, как во фронте: 32×32 серый → 8×8 низких частот без DC → медиана).
    → int (63 бита) | None. Нужен для дедупа ВИЗУАЛЬНЫХ дублей: один баннер, пересохранённый в
    разных файлах, даёт разный md5/имя, но одинаковый pHash. Кэш по пути. Нет Pillow/битый файл → None."""
    if path in _IMG_PHASH_CACHE:
        return _IMG_PHASH_CACHE[path]
    val: int | None = None
    try:
        from PIL import Image
        import numpy as np
        img = Image.open(path).convert("L").resize((size, size))
        a = np.asarray(img, dtype=np.float64)
        k = np.arange(size)
        c = np.cos(np.pi * (2 * k[:, None] + 1) * k[None, :] / (2 * size))   # DCT-II базис
        d = c @ a @ c.T
        block = d[:low, :low].flatten()[1:]              # low*low-1 = 63 коэффициента без DC
        med = float(np.median(block))
        bits = 0
        for coeff in block:
            bits = (bits << 1) | (1 if coeff >= med else 0)
        val = bits
    except Exception:  # noqa: BLE001 — нет Pillow/numpy или битый файл → визуальный дедуп пропускаем
        val = None
    _IMG_PHASH_CACHE[path] = val
    return val


def collect_image_files(spec: MasterCampaignSpec, *, visual_threshold: int = 10) -> list[Path]:
    """Собирает локальные картинки: из spec.image_dir (все изображения) + spec.image_files.
    Трёхуровневый ДЕДУП, чтобы в UAC-кампанию (tp6/tp7, лимит 5) не попал один баннер дважды:
      1) по пути; 2) по СОДЕРЖИМОМУ (md5 байтов) — тот же файл под другим именем;
      3) ВИЗУАЛЬНО (pHash, hamming ≤ visual_threshold) — тот же баннер, пересохранённый в другой
         файл (разный md5, но одинаковая картинка). Порог 10 (из 63 бит) — типовой для pHash: ловит
         пересжатый ОДИН баннер, но НЕ схлопывает разные баннеры одного шаблона (иначе UAC недобирал
         бы 5 картинок). Нечитаемые файлы оставляем (решает upload). Порядок сохраняем.
    Для dmp/не-авто лидогена (spec.visual_dedup=False) pHash-уровень ОТКЛЮЧЁН: доменные баннеры
    одного шаблона (тёмный фон + разный текст) — РАЗНЫЕ объявления, Директ их принимает; pHash их
    ошибочно схлопывал (dmp МК доезжало 2 из ~50). Тогда остаётся ТОЛЬКО path+md5 (байт-дедуп).
    Авто tp6/tp7 (visual_dedup=True) — pHash сохранён (защита от клонов)."""
    import hashlib
    do_visual = visual_threshold > 0 and getattr(spec, "visual_dedup", True)
    raw: list[Path] = []
    if spec.image_dir:
        d = Path(spec.image_dir).expanduser()
        if not d.is_dir():
            raise NotADirectoryError(f"image_dir не папка: {d}")
        raw.extend(sorted(p for p in d.iterdir()
                          if p.is_file() and p.suffix.lower() in _IMAGE_EXTS))
    raw.extend(Path(f).expanduser() for f in spec.image_files)
    files: list[Path] = []
    seen_paths: set[str] = set()
    seen_hashes: set[str] = set()
    kept_phashes: list[int] = []
    for p in raw:
        key = str(p)
        if key in seen_paths:                            # тот же путь
            continue
        seen_paths.add(key)
        try:
            h = hashlib.md5(p.read_bytes()).hexdigest()  # noqa: S324 — дедуп, не криптография
        except Exception:  # noqa: BLE001 — нечитаемый файл: оставляем, upload разберётся
            files.append(p)
            continue
        if h in seen_hashes:                             # байт-идентичный дубль под другим именем
            continue
        seen_hashes.add(h)
        ph = _image_phash(key) if do_visual else None    # do_visual=False (dmp) → pHash пропускаем
        # bin(...).count('1') вместо int.bit_count() — портативно (bit_count есть только в py3.10+).
        if ph is not None and any(bin(ph ^ kh).count("1") <= visual_threshold for kh in kept_phashes):
            continue                                     # визуальный дубль (тот же баннер, иной файл)
        if ph is not None:
            kept_phashes.append(ph)
        files.append(p)
    return files


def _audience_goals(spec: MasterCampaignSpec) -> list[dict]:
    """Аудитории → [{id}]. Принимает id (str/int) или объекты {"id":...}."""
    goals = []
    for a in spec.audiences:
        aid = a.get("id") if isinstance(a, dict) else a
        if aid:
            goals.append({"id": str(aid)})
    return goals


def _norm_sitelinks(spec: MasterCampaignSpec) -> list[dict]:
    """Приводит быстрые ссылки к виду {title, description, href}.

    href по умолчанию — главная страница сайта (spec.href без UTM).
    Дедуп по title И description (после обрезки) — защита от DUPLICATE_SITELINK_DESCS.
    """
    base_href = spec.href
    out, seen_titles, seen_descs = [], set(), set()
    for s in spec.sitelinks:
        # ЛИМИТЫ Директа на быстрые ссылки: заголовок ≤30, описание ≤60 (как в _norm_sitelinks_for_v501).
        # Без кэпа UAC Мастер/Товарка отбивает длинный (M3-сгенерированный) заголовок:
        # SitelinkDefectIds.Strings.SITELINK_TITLE_TOO_LONG.
        title = (s.get("title") or "").strip()[:30]
        if not title:
            continue
        tk = title.lower()
        if tk in seen_titles:
            continue
        desc = (s.get("description") or "").strip()[:60]
        dk = desc.lower()
        if dk and dk in seen_descs:
            continue
        seen_titles.add(tk)
        if dk:
            seen_descs.add(dk)
        item = {"title": title, "href": s.get("href") or base_href}
        if desc:
            item["description"] = desc
        out.append(item)
    return out


# ─── Клиент UAC ───────────────────────────────────────────────────────────────

# Фиды в ERROR-состоянии — помечаются при успешном retry без feedId.
# blueprint._first_url_feed использует этот сет чтобы пропускать мёртвые фиды.
_dead_feed_ids: set[int] = set()


class UacApiError(RuntimeError):
    def __init__(self, step: str, status: int, body: str):
        self.step, self.status, self.body = step, status, body
        super().__init__(f"[{step}] HTTP {status}: {body[:400]}")


class UacClient:
    """Тонкий клиент приватного ``/web-api/uac/`` API на куках."""

    def __init__(self, cookie: str, ulogin: str, *, timeout: int = 60):
        self.ulogin = ulogin
        self.timeout = timeout
        self.csrf: str | None = None
        self.sess = requests.Session()
        self.sess.verify = False
        self.sess.headers.update({
            "Cookie": cookie,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Origin": "https://direct.yandex.ru",
            "Referer": f"https://direct.yandex.ru/wizard/campaigns/new/?ulogin={ulogin}",
            "x-direct-api": "1",
            "x-detected-locale": "ru",
            "x-client-versions": UAC_CLIENT_VERSION,
        })

    # -- низкий уровень --

    def _absorb_csrf(self, resp: requests.Response) -> None:
        token = resp.cookies.get("_direct_csrf_token")
        if not token:
            m = re.search(r"_direct_csrf_token=([^;,\s]+)", resp.headers.get("Set-Cookie", ""))
            token = m.group(1) if m else None
        if token:
            self.csrf = token

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json_body: Any = None, step: str = "") -> Any:
        url = f"{BASE}{path}"
        p = {"ulogin": self.ulogin, **(params or {})}
        headers = {}
        if json_body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.csrf:
            headers["x-csrf-token"] = self.csrf

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = self.sess.request(method, url, params=p, json=json_body,
                                         headers=headers, timeout=self.timeout)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(1.0 + attempt)
                    continue
                raise
            self._absorb_csrf(resp)
            # Протухший CSRF → один ретрай со свежим токеном.
            if resp.status_code in (401, 403) and attempt == 0 and self.csrf:
                headers["x-csrf-token"] = self.csrf
                continue
            if resp.status_code >= 400:
                raise UacApiError(step or path, resp.status_code, resp.text)
            return resp.json() if resp.content else {}
        if last_exc:
            raise last_exc
        raise UacApiError(step or path, resp.status_code, resp.text)

    # -- шаги флоу --

    def link_info(self, url: str) -> dict:
        """Шаг 1: тип лендинга + bootstrap CSRF (GET ставит _direct_csrf_token)."""
        return self._request("GET", "/linkinfo", params={"url": url}, step="linkinfo")

    def upload_content(self, source_url: str, content_type: str, adv_type: str = "text") -> str:
        """Шаг 2: регистрация креатива по прямой ссылке → content_id.

        content_type: 'image' | 'video'.
        """
        fname = source_url.rsplit("/", 1)[-1] or f"USER.{content_type}"
        body = {"source_url": source_url, "type": content_type, "file_name": fname}
        data = self._request(
            "POST", "/content",
            params={"adv_type": adv_type, "creative_type": "tgo"},
            json_body=body, step=f"content:{content_type}",
        )
        cid = (data.get("result") or {}).get("id")
        if not cid:
            raise UacApiError("content", 200, json.dumps(data)[:400])
        return str(cid)

    def upload_image_file(self, path: str | Path, adv_type: str = "text") -> str:
        """Шаг 2 (локальный файл): multipart-загрузка картинки с диска → content_id.

        Эндпоинт тот же — POST /uac/content?adv_type=&creative_type=tgo,
        но тело multipart/form-data с полем "upload" = файл (так делает «Изображения» в мастере).
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"картинка не найдена: {p}")
        if self.csrf is None:                 # multipart-запрос требует CSRF — добираем
            self.link_info("https://ya.ru")
        headers = {"Accept": "application/json"}
        if self.csrf:
            headers["x-csrf-token"] = self.csrf
        resp = None
        # Файл в память ДО POST: путь бывает на sshfs (Мак) — стрим fh в requests растягивает
        # отправку тела бесконечно (timeout меряет ответ, не отправку) → зависание воркера.
        # 2026-07-18: сам read() по FUSE тоже висит вечно (таймаута нет by design) — читаем
        # с пределом времени, как в grid_finalize.upload_image.
        try:                                  # пакетный контекст / standalone
            from .. import kontent_pack as _kpf
        except ImportError:
            import kontent_pack as _kpf       # type: ignore[no-redef]
        _file_bytes = _kpf.read_bytes_bounded(str(p))
        if not _file_bytes:
            # UacApiError (а не OSError): вызывающий (upload-цикл creative-ов) ловит именно её
            # и пропускает ОДНУ картинку, не роняя черновик целиком.
            raise UacApiError("content", 0,
                              f"картинка не прочитана за {_kpf._FS_OP_TIMEOUT:.0f}с: {p}")
        for attempt in range(3):
            try:
                files = {"upload": (p.name, _file_bytes, _guess_mime(p.name))}
                resp = self.sess.post(
                    f"{BASE}/content",
                    params={"ulogin": self.ulogin, "adv_type": adv_type, "creative_type": "tgo"},
                    files=files, headers=headers, timeout=self.timeout,
                )
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                if attempt >= 2:
                    raise
                time.sleep(1.0 + attempt)
        if resp is None:
            raise UacApiError(f"content-file:{p.name}", 0, "empty response")
        self._absorb_csrf(resp)
        if resp.status_code >= 400:
            raise UacApiError(f"content-file:{p.name}", resp.status_code, resp.text)
        data = resp.json() if resp.content else {}
        cid = (data.get("result") or {}).get("id")
        if not cid:
            raise UacApiError("content-file", 200, json.dumps(data)[:400])
        return str(cid)

    def _upload_video_result(self, path: str | Path, adv_type: str = "text") -> dict:
        """Multipart-загрузка ВИДЕО (mp4) с диска → ``result`` из ответа /uac/content.

        Тот же эндпоинт и поле "upload", что для картинок, но mime video/mp4.
        В ``result`` есть И ``id`` (content_id, для content_ids UAC-кампаний), И
        ``meta.creative_id`` (для creativeIds ЕПК-объявлений через Grid UpdateAdaptiveTextAds).
        Официальный API v5 видео не умеет — этот путь работает. Лимит 2 видео на мастер."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"видео не найдено: {p}")
        if self.csrf is None:
            self.link_info("https://ya.ru")
        headers = {"Accept": "application/json"}
        if self.csrf:
            headers["x-csrf-token"] = self.csrf
        # ВЕСЬ файл в память ДО POST: sshfs-источник (/opt/neuro_kontent) стримился прямо в
        # сокет — read-timeout не ограничивает фазу отправки, и загрузка висла бессрочно
        # (stall-trace 02-03.07: _upload_video_result → ssl.read; watchdog killed джобу Павлова).
        # Тот же фикс, что у картинок (_upload_image_result, fh.read()).
        _file_bytes = p.read_bytes()
        resp = None
        # 2 ретрая при таймауте с бэкоффом (решение Семёна 2026-07-08, отменяет прежнее
        # «Timeout НЕ ретраим — решение Семёна 03.07»). Per-ct circuit-breaker гарантирует
        # изоляцию: другие ct не блокируются если этот таймаутит 3× подряд.
        # Бэкофф: 3с после 1-го таймаута, 6с после 2-го. ConnectionError — 1 повтор без паузы.
        _timeout_cnt = 0
        for attempt in range(5):
            try:
                files = {"upload": (p.name, _file_bytes, "video/mp4")}
                resp = self.sess.post(
                    f"{BASE}/content",
                    params={"ulogin": self.ulogin, "adv_type": adv_type, "creative_type": "tgo"},
                    files=files, headers=headers, timeout=max(self.timeout, 180),
                )
                break
            except requests.exceptions.Timeout:
                _timeout_cnt += 1
                if _timeout_cnt >= 3:
                    raise UacApiError(f"content-video:{p.name}", 0,
                                      "video-upload timeout 180s ×3 — ct помечается failed")
                time.sleep(3 * _timeout_cnt)  # 3с после 1-го таймаута, 6с после 2-го
            except requests.exceptions.ConnectionError:
                if attempt >= 1:
                    raise
                time.sleep(1.5)
        if resp is None:
            raise UacApiError(f"content-video:{p.name}", 0, "empty response")
        self._absorb_csrf(resp)
        if resp.status_code >= 400:
            raise UacApiError(f"content-video:{p.name}", resp.status_code, resp.text)
        return (resp.json() if resp.content else {}).get("result") or {}

    def upload_video_file(self, path: str | Path, adv_type: str = "text") -> str:
        """Загрузка видео → content_id (result.id) — для content_ids UAC-кампаний (tp6/tp7)."""
        res = self._upload_video_result(path, adv_type)
        cid = res.get("id")
        if not cid:
            raise UacApiError("content-video", 200, json.dumps(res, ensure_ascii=False)[:400])
        return str(cid)

    def upload_video_creative(self, path: str | Path, adv_type: str = "text") -> str:
        """Загрузка видео → creative_id (result.meta.creative_id) — для attach в ЕПК-объявление
        через Grid UpdateAdaptiveTextAds ``creativeIds`` (HAR53, tp1 РСЯ).

        ВАЖНО: это НЕ content_id (result.id). Для creativeIds Директ ждёт именно
        meta.creative_id (в HAR: content id 1252…881, но creativeIds:["1163020076"] = meta)."""
        res = self._upload_video_result(path, adv_type)
        cid = (res.get("meta") or {}).get("creative_id")
        if not cid:
            raise UacApiError("content-video-creative", 200, json.dumps(res, ensure_ascii=False)[:400])
        return str(cid)

    def create_campaign(self, payload: dict) -> str:
        """Шаг 3: создание черновика → campaign id."""
        last_err: UacApiError | None = None
        for attempt in range(3):
            try:
                data = self._request("POST", "/campaigns", json_body=payload, step="create")
                break
            except UacApiError as e:
                last_err = e
                if e.status >= 500 and attempt < 2:
                    time.sleep(1.0 + attempt)
                    continue
                raise
        else:
            if last_err:
                raise last_err
            raise UacApiError("create", 0, "empty response")
        cid = (data.get("result") or {}).get("id")
        if not cid:
            raise UacApiError("create", 200, json.dumps(data)[:400])
        return str(cid)

    def list_campaigns(self, *, status: str | None = None) -> list[dict]:
        """Список UAC-кампаний клиента (Мастер tp6 / Товарка tp7).

        UAC-кампании НЕВИДИМЫ в v5 — только через Grid. Приватный
        GET /web-api/uac/campaigns отвечает 405 Method Not Allowed, поэтому список
        берём тем же рабочим Grid-путём, что и content-editor
        (routes_content_editor._grid_tp67_campaigns): GraphQL client.campaigns,
        фильтр tp6_/tp7_ по имени, без архивных.
        status: 'DRAFT' | 'ACTIVE' | None (=все). dict с полями id, name, typename, status.
        """
        # lazy import: routes_content_editor → uac_read → campaign (иначе цикл на импорте).
        from ..web.routes_content_editor import _grid_tp67_campaigns

        rows = _grid_tp67_campaigns(self.ulogin)
        if status:
            rows = [r for r in rows if (r.get("status") or "") == status]
        return rows

    def set_status(self, campaign_id: str, target_status: str) -> dict:
        """Шаг 4: смена статуса. 'started' = запустить (на модерацию), 'stopped' = остановить."""
        return self._request("POST", f"/campaign/{campaign_id}/status/",
                             json_body={"target_status": target_status},
                             step=f"status:{target_status}")

    def delete_campaign(self, campaign_id: str | int) -> dict:
        """Удалить UAC-кампанию (Мастер tp6 / Товарка tp7).

        ⚠️ UAC-кампании НЕВИДИМЫ в v5 campaigns.get и НЕ удаляются v5 campaigns.delete
        (тихий no-op). Рабочий путь — DELETE /web-api/uac/campaign/{id}/ (проверено 2026-06-21).
        Найти их можно только через Grid API (web-api/grid/api), не через v5.

        Идемпотентно: 404 = «уже удалена» → success. На 5xx — один ретрай: Yandex
        иногда отдаёт HTTP 500, хотя кампания фактически удаляется (после ретрая видим 404).
        """
        if self.csrf is None:
            self.link_info("https://ya.ru")
        url = f"{BASE}/campaign/{campaign_id}/"
        last_status, last_body = 0, ""
        for attempt in range(3):
            resp = self.sess.request("DELETE", url, params={"ulogin": self.ulogin},
                                     headers={"x-csrf-token": self.csrf or ""},
                                     timeout=self.timeout)
            self._absorb_csrf(resp)
            if resp.status_code == 404:                 # уже удалена — считаем успехом
                return {"ok": True, "already_gone": True}
            if resp.status_code < 400:
                return resp.json() if resp.content else {"ok": True}
            last_status, last_body = resp.status_code, resp.text
            if resp.status_code in (401, 403) and attempt == 0:
                continue                                # протух CSRF — ретрай со свежим
            if resp.status_code >= 500 and attempt < 2:
                continue                                # транзиентная 5xx — ещё попытка
            break
        raise UacApiError(f"delete:{campaign_id}", last_status, last_body)

    # -- сборка payload + полный сценарий --

    def build_payload(self, spec: MasterCampaignSpec, content_ids: list[str]) -> dict:
        """Из spec + загруженных креативов собирает тело POST /campaigns (как в браузере)."""
        display_name = spec.display_name or DEFAULT_DISPLAY_NAME
        payload = {
            "adv_type": spec.adv_type,
            "counters": [spec.counter_id],
            "goals": [{"goal_id": spec.goal_id, "goal_type": spec.goal_type, "cpa": spec.cpa}],
            "pricing": spec.pricing,
            "crr": None,
            "device_types": spec.device_types,
            "display_name": display_name,
            "href": spec.href,                                    # чистая ссылка (без UTM)
            "tracking_params": (spec.utm_template or "").strip(),  # UTM → отдельное поле «UTM-метки и параметры URL»
            "hyperlocal_geo_segments": None,
            "keywords": spec.keywords,
            "minus_keywords": spec.minus_keywords,
            "limit_period": spec.limit_period,
            "regions": spec.region_ids,
            "minus_regions": spec.minus_regions,
            "sitelinks": _norm_sitelinks(spec),
            "socdem": {
                "genders": spec.genders,
                "age_lower": spec.age_lower,
                "age_upper": spec.age_upper,
            },
            "relevance_match": {"active": True, "categories": spec.relevance_match_categories,
                                # ВСЕ ТРИ бренд-настройки ЯВНО: без WITH_COMPETITOR_BRAND Яндекс
                                # отключает категорию → в UI группы появляется чип-исключение
                                # «Запросы с упоминанием брендов конкурентов» (жалоба Семёна ×3)
                                "brand_settings": ["WITHOUT_BRAND", "WITH_BRAND", "WITH_COMPETITOR_BRAND"]},
            "texts": spec.texts,
            "titles": spec.titles,
            "week_limit": str(int(spec.week_budget)),
            "content_ids": content_ids,
            "time_target": {
                "enabled_holidays_mode": False,
                "id_time_zone": spec.id_time_zone,
                "time_board": _TIME_BOARD_ALWAYS,
                "use_working_weekends": True,
            },
            "recommendations_management_enabled": False,
            "price_recommendations_management_enabled": False,
            "ml_banners_enabled": spec.ml_banners_enabled,
            "alternative_texts_enabled": spec.alternative_texts_enabled,
            "ecom": False,
            "erir_ad_description": None,
            "yandex_maps_enabled": spec.yandex_maps_enabled,
            "use_discounts": False,
            "reserve_landing_id": None,
        }
        # Фид «объявления для товаров» — его наличие включает ecom (фильтр по "model")
        if spec.feed_id:
            payload.update({
                "ecom": True,
                "show_ecom_listings": True,
                "ecom_listings_enabled": True,
                "feed_id": spec.feed_id,
            })
            if spec.feed_filters:
                payload["feed_filters"] = spec.feed_filters
        # Фид «страницы каталога» (ecom-листинги) — фильтр ТОЛЬКО по "collectionId"
        if spec.listings_feed_id:
            payload["listings_feed_id"] = spec.listings_feed_id
            if spec.listings_feed_filters:
                payload["listings_feed_filters"] = spec.listings_feed_filters
        # Аудитории → ca_retargeting_condition
        # Яндекс лимит блока «Интересы и поисковые запросы» в МК/UAC: не более 30 интересов.
        # (API condition_rules принимает до 100, но UI Яндекса и кабинет ограничивают блок до 30:
        # превышение даёт "-52" и «интересов больше разрешённого».)
        _all_goals = _audience_goals(spec)
        goals = _all_goals[:30]
        if len(_all_goals) > 30:                 # UI-лимит МК/UAC — режем safety-net, но НЕ молча
            print(
                f"WARNING build_payload: аудиторных сегментов {len(_all_goals)} > лимита Яндекса 30 "
                f"в блоке «Интересы и поисковые запросы» МК/UAC — {len(_all_goals) - 30} отброшено "
                f"(display_name={spec.display_name or DEFAULT_DISPLAY_NAME!r}).",
                file=sys.stderr,
            )
        if goals:
            payload["ca_retargeting_condition"] = {
                "condition_rules": [{
                    "type": "OR",
                    "interestType": spec.audience_interest_type,
                    "goals": goals,
                }],
            }
        return payload

    def create_master_campaign(self, spec: MasterCampaignSpec, *, launch: bool = False) -> str:
        """Полный сценарий: linkinfo → upload креативов → create → (опц.) launch. Возвращает id."""
        # 1. bootstrap CSRF + проверка лендинга
        self.link_info(spec.href)

        # 2. креативы → content_ids
        content_ids: list[str] = [str(x).strip() for x in (spec.content_ids or []) if str(x).strip()]
        image_limit = max(0, int(getattr(spec, "image_limit", 5) or 0))
        image_count = 0
        if not content_ids:
            for path in collect_image_files(spec):          # локальные картинки (папка/файлы)
                if image_limit and image_count >= image_limit:
                    break
                try:
                    content_ids.append(self.upload_image_file(path, spec.adv_type))
                    image_count += 1
                except UacApiError:
                    # Direct иногда отклоняет отдельный файл (битый/неподходящий баннер/500 на upload).
                    # Картинка не должна валить весь черновик: пробуем следующий файл до image_limit.
                    pass
                time.sleep(0.3)
            for u in spec.image_urls:                       # картинки по URL
                if image_limit and image_count >= image_limit:
                    break
                try:
                    content_ids.append(self.upload_content(u, "image", spec.adv_type))
                    image_count += 1
                except UacApiError:
                    pass
                time.sleep(0.3)
        for u in spec.video_urls:                           # видео по URL
            try:
                content_ids.append(self.upload_content(u, "video", spec.adv_type))
            except UacApiError:
                pass
            time.sleep(0.3)
        for path in spec.video_files[:2]:                   # ЛОКАЛЬНЫЕ видео (multipart, лимит 2)
            try:
                content_ids.append(self.upload_video_file(path, spec.adv_type))
            except UacApiError:
                pass
            time.sleep(0.3)

        # 3. создание черновика
        payload = self.build_payload(spec, content_ids)
        try:
            campaign_id = self.create_campaign(payload)
        except UacApiError as e:
            # Фид в ERROR-состоянии: Директ возвращает HTTP 400 FEED_NOT_EXIST — фид существует
            # в списке, но непригоден для создания кампаний. Retry без feed_id/listings_feed_id.
            if e.status == 400 and "FEED_NOT_EXIST" in e.body:
                # #ФИКС-C1: для ТОВАРНОЙ РК (tp7, campaign_type=="product") фид/каталог —
                # суть кампании. Раньше pop фида молча превращал её в обычный Мастер без
                # товаров → валим item (raise), пусть уйдёт в defer/фолбэк вызывающего.
                # Для ОБЫЧНОГО Мастера feedless-fallback оставлен как был.
                if getattr(spec, "campaign_type", "master") == "product":
                    raise
                _bad_fid = payload.get("feed_id") or payload.get("listings_feed_id")
                for _k in ("feed_id", "listings_feed_id", "feed_filters", "listings_feed_filters",
                           "ecom", "show_ecom_listings", "ecom_listings_enabled"):
                    payload.pop(_k, None)
                campaign_id = self.create_campaign(payload)
                # Retry прошёл — запомнить мёртвый фид (blueprint._first_url_feed пропустит его).
                if _bad_fid:
                    try:
                        _dead_feed_ids.add(int(_bad_fid))
                    except (TypeError, ValueError):
                        pass
            elif (e.status == 400 and
                  ("DUPLICATE_SITELINK_DESCS" in e.body
                   or "SITELINK_DESCRIPTION_CANNOT_BE_EMPTY" in e.body)):
                # UAC API отклонил набор быстрых ссылок. Sitelinks не критичны — retry без них
                # (кампания создаётся, ссылки можно добавить позже).
                payload["sitelinks"] = []
                campaign_id = self.create_campaign(payload)
            elif (e.status == 400 and "MUST_BE_NULL" in e.body
                  and ("feedFilters" in e.body or "listingsFeed" in e.body or "feedId" in e.body)):
                # Тип UAC-кампании запрещает фид-фильтры/каталог (feedFilters MUST_BE_NULL — live
                # 712112280, ревью 03.07 #24). Правило Семёна: такие пропускаем — retry без фильтров
                # (кампания создаётся, фильтр там непроставим в принципе).
                for _k in ("feed_filters", "listings_feed_filters"):
                    payload.pop(_k, None)
                campaign_id = self.create_campaign(payload)
            else:
                raise

        # 4. запуск (опц.)
        if launch:
            self.set_status(campaign_id, "started")
        return campaign_id
