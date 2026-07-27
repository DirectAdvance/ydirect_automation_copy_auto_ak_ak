"""
Direct API v501 (Unified Performance Campaign / ЕПК).

Клиент DirectV501Client: создание/управление кампаниями UNIFIED_CAMPAIGN,
группами объявлений, объявлениями (ShoppingAd/ListingAd/TextAd),
быстрыми ссылками, уточнениями через JSON v501 API.
Авторизация: Bearer OAuth-токен + Client-Login.

Вынесено из campaign.py (был monolith 2047 строк) через re-export фасад:
  campaign.py re-exports всё отсюда, импортёры не меняются.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests
import urllib3

try:
    from .text_norm import _strip_href_fragment
except ImportError:                     # плоский запуск (локальные тесты из direct/)
    from text_norm import _strip_href_fragment

try:                                    # STAGE_TIMING: пер-стадийный замер (только замер + лог)
    from . import stage_timing as _timing
except ImportError:                     # плоский запуск (локальные тесты из direct/)
    import stage_timing as _timing

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Per-token rate limiter (межпотоковый троттл v501) ───────────────────────
# Лимит Директа: 5 req/s на один OAuth-токен. При двух A-суб-потоках (A1+A2) без
# троттла суммарная частота ≈6 req/s → 429 → реактивный backoff → общее замедление.
# Цель: ≤4.5 req/s суммарно для ОДНОГО токена — запас к лимиту 5 req/s.
# При DIRECT_TOKEN_THREADS=1 только один поток вызывает _call → интервал между
# вызовами >> 222ms (сетевая задержка), троттл практически не срабатывает.

_V501_MAX_RATE: float = 4.5           # req/s на токен
_v501_limiters: dict[str, "_V501RateLimiter"] = {}
_v501_limiters_lock = threading.Lock()


class _V501RateLimiter:
    """Token-bucket: минимальный интервал между вызовами = 1/max_rate секунды.

    Гарантирует, что любые два вызова _call для одного OAuth-токена
    разделены не менее чем ``min_interval`` секунд. Thread-safe через Lock.
    """

    def __init__(self, max_rate: float = _V501_MAX_RATE) -> None:
        self._min_interval = 1.0 / max_rate
        self._lock = threading.Lock()
        self._last_call: float = 0.0

    def acquire(self) -> None:
        """Заблокировать, если вызов раньше разрешённого времени."""
        with self._lock:
            now = time.monotonic()
            wait = self._last_call + self._min_interval - now
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()


def _get_v501_limiter(token: str) -> _V501RateLimiter:
    """Вернуть (создав при необходимости) лимитер для данного OAuth-токена."""
    if token not in _v501_limiters:
        with _v501_limiters_lock:
            if token not in _v501_limiters:      # double-checked locking
                _v501_limiters[token] = _V501RateLimiter()
    return _v501_limiters[token]


# ─── Direct API v501 (UNIFIED_CAMPAIGN / ЕПК) ────────────────────────────────

V501_BASE = "https://api.direct.yandex.com/json/v501"

# Режимы канала ЕПК (v501 UNIFIED_CAMPAIGN).
# mode="search"      → tp2/tp4 «Поиск»:          Search=HIGHEST_POSITION, Network=SERVING_OFF
# mode="network"     → tp1 «РСЯ» (cpc-срез):     Search=SERVING_OFF, Network=AVERAGE_CPC
#                      tp3 «Товарная галерея»:    тот же режим кампании (Search=OFF, Network=AverageCpc);
#                      «товарность» задаётся типом объявления + фидом на уровне групп/объявлений,
#                      НЕ отдельным типом кампании.
# mode="network_cpa"     → tp1 «РСЯ» (cpc-вариант, целевой CPA): Search=SERVING_OFF, Network=AVERAGE_CPA
#                          Стратегия оплаты за клики (CPC) с целевым CPA — из «Глобальных правил».
#                          goal_id обязателен. counter_ids обязателен. maps=False (без показов на картах).
#                          Это режим для CPC-варианта tp1 (имя кампании tp1_cpc_site).
# mode="network_payconv"  → tp1 «РСЯ» (cpa-вариант, оплата за конверсии): Search=SERVING_OFF, Network=PAY_FOR_CONVERSION
#                          goal_id обязателен. Cpa — целевая (микро). BudgetType НЕ передаём в Network
#                          (API v501 не принимает его в PayForConversion для Network: проверено 2026-06-21).
#                          Это режим для CPA-варианта tp1 (имя кампании tp1_cpa_site).
# mode="combined"         → ЛЕГАСИ tp5 «Поиск + Сети»: Search=AVERAGE_CPC, Network=NETWORK_DEFAULT
#                          ОБА канала активны. ⚠️ НЕ для боевого tp5 — нарушает канон (включает РСЯ
#                          и ручной CPC). Оставлен только для совместимости. Боевой tp5 = "search_cpa"/"search_payconv".
# ── КАНОН (только 2 конверсионные стратегии, поиск-only для tp2/tp4/tp5) ──
# mode="search_cpa"     → cpc-вариант: Search=AVERAGE_CPA,       Network=SERVING_OFF (только поиск)
#                         goal_id обязателен. Бюджет — WeeklySpendLimit (микро) + BudgetType=WEEKLY_BUDGET.
# mode="search_payconv" → cpa-вариант: Search=PAY_FOR_CONVERSION, Network=SERVING_OFF (только поиск)
#                         goal_id обязателен. Cpa (целевая за конверсию, микро) + WeeklySpendLimit.
#                         Используются для tp5 (поиск+товарная галерея в поиске), а также tp2/tp4.
_UC_CHANNEL_MODES = ("search", "network", "network_cpa", "network_payconv", "combined",
                     "search_cpa", "search_payconv")

# Размер пачки для add_feed_ads_batch: ShoppingAd+ListingAd пар за один вызов ads.add.
# Официальный лимит ads.add не подтверждён (developer-reference не выгружен).
# Консервативный дефолт = 10 пар (20 объявлений/вызов). Менять здесь при необходимости.
_FEED_ADS_BATCH_SIZE = 10

# Размер пачки для add_sitelinks_sets: сколько НАБОРОВ быстрых ссылок кладём в один
# sitelinks.add. Официальный лимит не подтверждён живым API → консервативный дефолт 50
# (одиночный путь add_sitelinks_set по-прежнему шлёт ровно 1 набор).
_SITELINKS_SETS_BATCH_SIZE = 50


@dataclass
class UnifiedCampaignSpec:
    """Спецификация Единой Перформанс Кампании (UNIFIED_CAMPAIGN) для v501.

    Параметризация канала через ``mode``:
    * ``"search"``      — tp2/tp4 «Поиск»: Search=HIGHEST_POSITION, Network=SERVING_OFF
    * ``"network"``     — tp1 «РСЯ» / tp3 «Товарная галерея»: Search=SERVING_OFF, Network=AVERAGE_CPC
      (для tp3 «товарность» — это тип объявления + фид на уровне групп, НЕ тип кампании)
    * ``"network_cpa"``     — tp1 «РСЯ» (cpc-вариант): Search=SERVING_OFF, Network=AVERAGE_CPA
      (оплата за клики CPC, ставки автоматом по целевому CPA; goal_id обязателен; имя кампании tp1_cpc_site)
    * ``"network_payconv"`` — tp1 «РСЯ» (cpa-вариант): Search=SERVING_OFF, Network=PAY_FOR_CONVERSION
      (оплата за конверсии; goal_id обязателен; BudgetType НЕ передаём в Network.PayForConversion;
       проверено live 2026-06-21 на porg-psm5h7q6; имя кампании tp1_cpa_site)
    * ``"combined"``        — ЛЕГАСИ «Поиск + Сети»: Search=AVERAGE_CPC, Network=NETWORK_DEFAULT
      (ОБА канала; НЕ для tp5 — включает РСЯ против канона)
    * ``"search_cpa"``      — КАНОН cpc: Search=AVERAGE_CPA, Network=SERVING_OFF (поиск-only)
    * ``"search_payconv"``  — КАНОН cpa: Search=PAY_FOR_CONVERSION, Network=SERVING_OFF (поиск-only)
      (tp5 = пара search_cpa + search_payconv; проверено live 2026-06-21)

    Минимальный набор полей для валидного черновика через v501.
    Счётчик Метрики и цели не обязательны для черновика — не добавляем без нужды.
    """

    # --- обязательное ---
    name: str                               # имя кампании
    client_login: str                       # Client-Login (аккаунт)
    oauth_token: str                        # Bearer-токен (должен иметь доступ к client_login)

    # --- канал ---
    mode: str = "search"                    # "search" | "network" | "network_cpa" | "combined"

    # --- гео ---
    region_ids: list[int] = field(default_factory=lambda: [225])  # 225 = Россия

    # --- бюджет (опц. для черновика — если задан, добавим) ---
    daily_budget_amount: int | float | None = None  # дневной бюджет, мкд (микродолей, × 1_000_000)
    # Примечание: v501 принимает сумму в МИКРОРУБЛЯХ (1 руб = 1_000_000 мкруб).
    # Если None — черновик создаётся без бюджета.

    # --- сетевая стратегия (mode="network") ---
    # Средняя цена клика для Network=AVERAGE_CPC, в микрорублях (1 руб = 1_000_000).
    # None → дефолт 1_000_000 мкруб (1₽ — минимально валидно для черновика).
    network_average_cpc: int | None = None

    # --- стратегия CPA (mode="network_cpa") ---
    # Целевая цена конверсии для Network=AVERAGE_CPA, в микрорублях (1 руб = 1_000_000).
    # None → использует network_average_cpc как fallback.
    # goal_id ОБЯЗАТЕЛЕН при mode="network_cpa" (иначе стратегия не работает).
    network_average_cpa: int | None = None

    # --- конверсионные стратегии поиска (mode="search_cpa" / "search_payconv") ---
    # Канон: cpc→AVERAGE_CPA, cpa→PAY_FOR_CONVERSION; обе — на стороне Search, Network=SERVING_OFF.
    # Целевая цена конверсии (микро): для AVERAGE_CPA и для PAY_FOR_CONVERSION (Cpa).
    # None → fallback на network_average_cpa → network_average_cpc.
    search_cpa: int | None = None
    # Недельный бюджет стратегии (микро) — WeeklySpendLimit. Из «Глобальных правил» (напр. 20000₽).
    # None → бюджет в стратегию не кладём (API возьмёт дефолт/минимум).
    weekly_spend_limit: int | None = None

    # --- Яндекс.Метрика ---
    counter_ids: list[int] | None = None    # счётчики Метрики (список id)
    goal_id: int | None = None              # id цели для оптимизации

    # --- инварианты (применяются через Settings в UNIFIED_CAMPAIGN) ---
    # #3 Персонализация (адаптивные тексты) ВЫКЛ = ALTERNATIVE_TEXTS_ENABLED NO
    # #4 Мониторинг сайта ВКЛ = ENABLE_SITE_MONITORING YES
    # #5 Расширенный гео ВЫКЛ = ENABLE_AREA_OF_INTEREST_TARGETING NO
    # По умолчанию все инварианты включены (соответствует CAMPAIGN_INVARIANTS.md).
    apply_invariants: bool = True

    # --- даты ---
    start_date: str | None = None           # YYYY-MM-DD; None → сегодня

    def __post_init__(self) -> None:
        if self.mode not in _UC_CHANNEL_MODES:
            raise ValueError(f"mode должен быть одним из {_UC_CHANNEL_MODES}, а не {self.mode!r}")
        if not self.name:
            raise ValueError("name не может быть пустым")
        if not self.client_login:
            raise ValueError("client_login обязателен")
        if not self.oauth_token:
            raise ValueError("oauth_token обязателен")


def _dedup_keep_order(seq) -> list:
    """Дедуп без потери порядка (по точному значению)."""
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


class DirectV501Error(RuntimeError):
    """Ошибка API v501."""
    def __init__(self, method: str, code: int, message: str, detail: str = ""):
        self.method, self.code, self.message, self.detail = method, code, message, detail
        super().__init__(f"[v501:{method}] {code}: {message}" + (f" | {detail}" if detail else ""))


class DirectV501Client:
    """Клиент Direct API v501 для работы с UNIFIED_CAMPAIGN (ЕПК).

    Авторизация: ``Authorization: Bearer <token>`` + ``Client-Login`` в заголовках.
    Endpoint: ``https://api.direct.yandex.com/json/v501/campaigns``
    """

    def __init__(self, oauth_token: str, client_login: str, *, timeout: int = 30):
        self.client_login = client_login
        self.timeout = timeout
        self._token = oauth_token          # ключ для per-token лимитера
        self.sess = requests.Session()
        self.sess.headers.update({
            "Authorization": f"Bearer {oauth_token}",
            "Client-Login": client_login,
            "Content-Type": "application/json",
            "Accept-Language": "ru",
            "Use-Operator-Units": "true",
        })

    # -- низкий уровень --

    def _call(self, service: str, method: str, params: dict) -> dict:
        """Вызов метода v501. Возвращает result-словарь или бросает DirectV501Error."""
        # Межпотоковый троттл: ≤4.5 req/s суммарно для одного OAuth-токена.
        # При двух A-суб-потоках (A1+A2) без троттла суммарная частота может превысить
        # 5 req/s → 429. Acquire один раз перед циклом; при 429 retry ждёт Retry-After.
        # STAGE_TIMING: замер одной v501-операции целиком (включая троттл и ретраи) — только
        # внутри item-контекста создания набора. Поведение не меняется, это чистый замер.
        with _timing.stage(f"v501:{service}.{method}", only_in_item=True):
            _get_v501_limiter(self._token).acquire()
            url = f"{V501_BASE}/{service}"
            body = {"method": method, "params": params}
            _transient_err = None
            for attempt in range(3):
                resp = self.sess.post(url, json=body, timeout=self.timeout)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 5))
                    time.sleep(retry_after)
                    continue
                data = resp.json()
                if "error" in data:
                    err = data["error"]
                    _code = err.get("error_code", 0)
                    _msg = err.get("error_string", "") or ""
                    # Транзиентные ошибки Яндекса («Сервис/операция временно недоступна», code 1000) —
                    # РЕТРАИМ с backoff, а не роняем создание РК на моргании API (живой кейс 5de58f8ad0d9:
                    # [v501:campaigns.add] 1000 → одна РК failed). До 3 попыток (как 429), потом бросаем.
                    if (_code == 1000 or "временно недоступ" in _msg.lower()) and attempt < 2:
                        _transient_err = DirectV501Error(f"{service}.{method}", _code, _msg,
                                                         err.get("error_detail", ""))
                        time.sleep(3 * (attempt + 1))     # backoff 3с, 6с
                        continue
                    raise DirectV501Error(
                        f"{service}.{method}",
                        _code,
                        _msg,
                        err.get("error_detail", ""),
                    )
                return data.get("result", {})
            if _transient_err is not None:
                raise _transient_err
            raise DirectV501Error(f"{service}.{method}", 429, "превышен лимит запросов (3 ретрая)")

    # -- высокий уровень --

    def get_campaigns(self, campaign_ids: list[int] | None = None,
                      field_names: list[str] | None = None) -> list[dict]:
        """Получить кампании. Если campaign_ids=None — все. Пагинация встроена."""
        fn = field_names or ["Id", "Name", "Type", "State", "Status"]
        params: dict = {"FieldNames": fn, "Page": {"Limit": 1000}}
        if campaign_ids:
            params["SelectionCriteria"] = {"Ids": campaign_ids}
        else:
            params["SelectionCriteria"] = {}

        results = []
        offset = 0
        while True:
            params["Page"]["Offset"] = offset
            result = self._call("campaigns", "get", params)
            batch = result.get("Campaigns", [])
            results.extend(batch)
            if "LimitedBy" not in result:
                break
            offset = result["LimitedBy"]
        return results

    def create_unified_campaign(self, spec: UnifiedCampaignSpec, *, launch: bool = False) -> int:
        """Создать UNIFIED_CAMPAIGN (ЕПК) через v501. Возвращает campaign_id (int).

        ``launch=False`` (по умолчанию) — кампания остаётся черновиком (State=OFF/DRAFT).

        Режимы:
        * ``"search"``   — Search=HIGHEST_POSITION, Network=SERVING_OFF (только поиск)
        * ``"network"``  — Search=SERVING_OFF, Network=AVERAGE_CPC (только сети/галерея)
        * ``"combined"`` — Search=AVERAGE_CPC, Network=NETWORK_DEFAULT (ОБА канала)
          Обход: прямой ADD с HIGHEST_POSITION + AVERAGE_CPC даёт 4000 «Стратегии не совместимы».
          Комбинация AVERAGE_CPC + NETWORK_DEFAULT проходит без ошибок (проверено 2026-06-21).
        """
        import datetime

        start_date = spec.start_date or datetime.date.today().isoformat()

        # Стратегия в зависимости от mode.
        # tp2/tp4 «Поиск»:        Search=HIGHEST_POSITION, Network=SERVING_OFF
        # tp1 «РСЯ»:              Search=SERVING_OFF,       Network=AVERAGE_CPC
        # tp3 «Товарная галерея»: РСЯ-режим кампании, товарность — через тип объявления+фид в группах
        # tp5 «Поиск + Сети»:     Search=AVERAGE_CPC,       Network=NETWORK_DEFAULT (оба канала)
        if spec.mode == "search":
            bidding_strategy = {
                "Search": {
                    "BiddingStrategyType": "HIGHEST_POSITION",
                },
                "Network": {
                    "BiddingStrategyType": "SERVING_OFF",
                },
            }
        elif spec.mode == "network":
            # tp1 РСЯ и tp3 Товарная галерея: Search=SERVING_OFF, Network=AVERAGE_CPC
            # Проверено: WB_MAXIMUM_CLICKS для Network тоже OK, используем AVERAGE_CPC
            bidding_strategy = {
                "Search": {
                    "BiddingStrategyType": "SERVING_OFF",
                },
                "Network": {
                    "BiddingStrategyType": "AVERAGE_CPC",
                    "AverageCpc": {
                        "AverageCpc": spec.network_average_cpc or 1000000,  # мкруб, дефолт 1₽
                    },
                },
            }
        elif spec.mode == "network_cpa":
            # tp1 «РСЯ» с целевым CPA (cpc-срез из «Глобальных правил»):
            # Search=SERVING_OFF — поиск отключён (только РСЯ).
            # Network=AVERAGE_CPA — стратегия «Средняя цена конверсии» (оплата за КЛИКИ,
            # но ставки управляются автоматом по целевому CPA). Это НЕ PAY_FOR_CONVERSION.
            # goal_id ОБЯЗАТЕЛЕН для AVERAGE_CPA (GoalId=0 = «все цели» → ошибка 4000).
            # BIM=NO — показы на картах ОТКЛЮЧЕНЫ (инвариант tp1: без карт).
            cpa_target = spec.network_average_cpa or (spec.network_average_cpc or 2000000)
            goal = spec.goal_id or 0
            if not goal:
                raise ValueError("goal_id обязателен для mode='network_cpa' (AVERAGE_CPA требует конкретной цели)")
            avg_cpa_block: dict = {
                "GoalId": goal,
                "AverageCpa": cpa_target,  # мкруб
                # BidCeiling опускаем — API сам выставит дефолт (минимум 0.3₽)
            }
            bidding_strategy = {
                "Search": {
                    "BiddingStrategyType": "SERVING_OFF",
                },
                "Network": {
                    "BiddingStrategyType": "AVERAGE_CPA",
                    "AverageCpa": avg_cpa_block,
                },
            }
        elif spec.mode == "network_payconv":
            # tp1 «РСЯ» (cpa-вариант из «Глобальных правил»):
            # Search=SERVING_OFF — поиск отключён (только РСЯ).
            # Network=PAY_FOR_CONVERSION — оплата за конверсии в сетях.
            # goal_id ОБЯЗАТЕЛЕН. BudgetType НЕ передаём в Network.PayForConversion
            # (API v501 возвращает 8000 на BudgetType здесь — проверено 2026-06-21).
            # Имя кампании: tp1_cpa_site.
            cpa_target = spec.search_cpa or spec.network_average_cpa or (spec.network_average_cpc or 2000000)
            goal = spec.goal_id or 0
            if not goal:
                raise ValueError("goal_id обязателен для mode='network_payconv' (PAY_FOR_CONVERSION требует конкретной цели)")
            pay_conv_block: dict = {
                "GoalId": goal,
                "Cpa": cpa_target,  # мкруб
            }
            bidding_strategy = {
                "Search": {
                    "BiddingStrategyType": "SERVING_OFF",
                },
                "Network": {
                    "BiddingStrategyType": "PAY_FOR_CONVERSION",
                    "PayForConversion": pay_conv_block,
                },
            }
        elif spec.mode == "combined":
            # tp5 «Поиск + Сети» — ОБА канала активны.
            # IMPORTANT: HIGHEST_POSITION + AVERAGE_CPC → 4000 «Стратегии не совместимы».
            # Рабочий обход (проверено 2026-06-21): AVERAGE_CPC (Search) + NETWORK_DEFAULT (Network).
            # NETWORK_DEFAULT ≠ SERVING_OFF → сети показываются, но без явной ставки CPC.
            bidding_strategy = {
                "Search": {
                    "BiddingStrategyType": "AVERAGE_CPC",
                    "AverageCpc": {
                        "AverageCpc": spec.network_average_cpc or 1000000,  # мкруб, дефолт 1₽
                    },
                },
                "Network": {
                    "BiddingStrategyType": "NETWORK_DEFAULT",
                },
            }
        elif spec.mode == "search_cpa":
            # КАНОН cpc-вариант (tp2/tp4/tp5): Search=AVERAGE_CPA, Network=SERVING_OFF (только поиск).
            # goal_id обязателен (GoalId=0 → ошибка 4000). WeeklySpendLimit — недельный бюджет (микро).
            goal = spec.goal_id or 0
            if not goal:
                raise ValueError("goal_id обязателен для mode='search_cpa' (AVERAGE_CPA требует цели)")
            avg_cpa: dict = {
                "GoalId": goal,
                "AverageCpa": spec.search_cpa or spec.network_average_cpa or 2000000,  # микро
            }
            if spec.weekly_spend_limit:
                avg_cpa["WeeklySpendLimit"] = spec.weekly_spend_limit  # микро
            bidding_strategy = {
                "Search": {"BiddingStrategyType": "AVERAGE_CPA", "AverageCpa": avg_cpa},
                "Network": {"BiddingStrategyType": "SERVING_OFF"},
            }
        elif spec.mode == "search_payconv":
            # КАНОН cpa-вариант (tp2/tp4/tp5): Search=PAY_FOR_CONVERSION, Network=SERVING_OFF.
            # Оплата за КОНВЕРСИИ. goal_id обязателен. Cpa — цена конверсии (микро).
            goal = spec.goal_id or 0
            if not goal:
                raise ValueError("goal_id обязателен для mode='search_payconv' (PAY_FOR_CONVERSION требует цели)")
            pay_conv: dict = {
                "GoalId": goal,
                "Cpa": spec.search_cpa or spec.network_average_cpa or 2000000,  # микро
            }
            if spec.weekly_spend_limit:
                pay_conv["WeeklySpendLimit"] = spec.weekly_spend_limit  # микро
            bidding_strategy = {
                "Search": {"BiddingStrategyType": "PAY_FOR_CONVERSION", "PayForConversion": pay_conv},
                "Network": {"BiddingStrategyType": "SERVING_OFF"},
            }
        else:
            raise ValueError(f"неизвестный mode: {spec.mode!r}")

        # Инварианты (CAMPAIGN_INVARIANTS.md) для UNIFIED_CAMPAIGN через Settings:
        # #3 Персонализация (адаптивные тексты) ВЫКЛ
        # #4 Мониторинг сайта ВКЛ
        # #5 Расширенный гео ВЫКЛ
        # #7 «Карты и список организаций» ВЫКЛ (ENABLE_COMPANY_INFO=NO) — иначе места показа
        #    по умолчанию = «Поиск, Карты и список организаций». Проверено live 2026-06-21:
        #    update принят, места показа без Карт. Товарной галерее на поиске org не нужен.
        # Применяются при apply_invariants=True (дефолт).
        uc_settings: list[dict] = []
        if spec.apply_invariants:
            uc_settings = [
                {"Option": "ALTERNATIVE_TEXTS_ENABLED", "Value": "NO"},         # #3 персонализация ВЫКЛ
                {"Option": "ENABLE_SITE_MONITORING", "Value": "YES"},           # #4 мониторинг ВКЛ
                {"Option": "ENABLE_AREA_OF_INTEREST_TARGETING", "Value": "NO"}, # #5 расш.гео ВЫКЛ
                {"Option": "ENABLE_COMPANY_INFO", "Value": "NO"},               # Карты/список орг. ВЫКЛ
            ]

        unified_campaign_block: dict = {"BiddingStrategy": bidding_strategy}
        if uc_settings:
            unified_campaign_block["Settings"] = uc_settings

        campaign: dict = {
            "Name": spec.name,
            "StartDate": start_date,
            # Примечание: RegionIds в v501 задаётся на уровне групп объявлений,
            # НЕ на уровне кампании — поле не передаём здесь.
            "UnifiedCampaign": unified_campaign_block,
        }

        # Примечание: в v501 UNIFIED_CAMPAIGN поля CounterIds и GoalIds в корне кампании
        # НЕ поддерживаются (ошибка 8000 «неизвестный параметр»).
        # GoalId задаётся внутри стратегии (AverageCpa.GoalId) при mode=network_cpa.
        # Привязка счётчика Метрики выполняется отдельным шагом через v5 campaigns.update
        # (CounterIds принимается в v5-формате). Для черновика счётчик не обязателен.

        # Дневной бюджет — только если задан явно
        if spec.daily_budget_amount is not None:
            campaign["DailyBudget"] = {
                "Amount": int(spec.daily_budget_amount),
                "Mode": "STANDARD",
            }

        result = self._call("campaigns", "add", {"Campaigns": [campaign]})

        add_results = result.get("AddResults", [])
        if not add_results:
            raise DirectV501Error("campaigns.add", 0, "пустой AddResults", str(result))

        first = add_results[0]
        errors = first.get("Errors", [])
        warnings = first.get("Warnings", [])

        if errors:
            err = errors[0]
            raise DirectV501Error(
                "campaigns.add",
                err.get("Code", 0),
                err.get("Message", ""),
                err.get("Details", ""),
            )

        campaign_id = first.get("Id")
        if not campaign_id:
            raise DirectV501Error("campaigns.add", 0, "нет Id в ответе", str(first))

        if warnings:
            for w in warnings:
                print(f"  WARNING {w.get('Code')}: {w.get('Message')}")

        # launch=True — перевести в ON (НЕ используем в тестах)
        if launch:
            self._call("campaigns", "resume", {"SelectionCriteria": {"Ids": [campaign_id]}})

        return campaign_id

    def archive_campaigns(self, campaign_ids: list[int]) -> dict:
        """Архивировать кампании по списку id."""
        return self._call("campaigns", "archive", {"SelectionCriteria": {"Ids": campaign_ids}})

    def delete_campaigns(self, campaign_ids: list[int]) -> dict:
        """Удалить кампании по списку id."""
        return self._call("campaigns", "delete", {"SelectionCriteria": {"Ids": campaign_ids}})

    # -- товарная ЕПК: группа + ShoppingAd --

    def add_product_adgroup(
        self,
        campaign_id: int,
        name: str = "Товарная группа",
        region_ids: list[int] | None = None,
    ) -> int:
        """Создать группу объявлений в UNIFIED_CAMPAIGN (tp3 товарная).

        В v501 ЕПК группа не требует явного AdGroupType — тип определяется
        типом добавляемых объявлений (SHOPPING_AD → «товарная»).
        Возвращает adgroup_id (int).
        """
        params = {
            "AdGroups": [
                {
                    "Name": name,
                    "CampaignId": campaign_id,
                    "RegionIds": region_ids or [225],  # 225 = Россия
                }
            ]
        }
        result = self._call("adgroups", "add", params)
        add_results = result.get("AddResults", [])
        if not add_results:
            raise DirectV501Error("adgroups.add", 0, "пустой AddResults", str(result))
        first = add_results[0]
        errors = first.get("Errors", [])
        if errors:
            err = errors[0]
            raise DirectV501Error(
                "adgroups.add",
                err.get("Code", 0),
                err.get("Message", ""),
                err.get("Details", ""),
            )
        adgroup_id = first.get("Id")
        if not adgroup_id:
            raise DirectV501Error("adgroups.add", 0, "нет Id в ответе", str(first))
        return adgroup_id

    def add_product_adgroups_batch(
        self,
        campaign_id: int,
        groups: list[dict],
    ) -> list[int | None]:
        """Batch adgroups.add: создать несколько групп одним вызовом.

        groups: [{"name": str, "region_ids": [int]}, ...] (region_ids опционален; дефолт [225])
        Возвращает позиционный список adgroup_ids (None для групп с ошибкой в AddResults).
        Позиционное соответствие: result[i] ↔ groups[i].

        Экономия баллов: N×(20+20) → 1×(20+N×20) за вызов при N>1.
        Официальный лимит adgroups.add: до 1000 групп (как в tp1 hot-path, _AC_CHUNK_AG=100).
        """
        if not groups:
            return []
        params = {
            "AdGroups": [
                {
                    "Name": g["name"],
                    "CampaignId": campaign_id,
                    "RegionIds": g.get("region_ids") or [225],
                }
                for g in groups
            ]
        }
        result = self._call("adgroups", "add", params)
        add_results = result.get("AddResults", [])
        out: list[int | None] = []
        for i in range(len(groups)):
            r = add_results[i] if i < len(add_results) else {}
            errs = r.get("Errors", [])
            if errs:
                out.append(None)
            else:
                out.append(r.get("Id") or None)
        return out

    def add_shopping_ad(self, adgroup_id: int, feed_id: int,
                        collection_id: str | None = None, vendor: str | None = None) -> int:
        """Добавить товарное объявление (ShoppingAd) в группу UNIFIED_CAMPAIGN.

        vendor (для группы по МАРКЕ): фильтр фида по бренду — «марка без модели» (правило
        пользователя; HAR19: field=vendor, operator=CONTAINS_ANY). Приоритетнее collection_id.

        ShoppingAd — тип товарного объявления ЕПК, привязанный к фиду (feed_id).
        Это «товарное объявление» в терминах ЕПК v501: объявление генерируется
        автоматически на основе фида. Аналог смарт-баннера в смарт-кампаниях.

        collection_id (как в слепках): фильтр фида по модели — товарное показывает
        ТОЛЬКО эту модель. collectionId берётся из listings фида (id вида 'model_N',
        name содержит имя модели). FeedFilterConditions проверено live в v501.

        Поддерживается в v501 (подтверждено 2026-06-21: code=0, errors=[]).
        В v5 тип объявления называется иначе — SMART_AD (не поддерживается в ЕПК).

        Возвращает ad_id (int).
        """
        shopping: dict = {"FeedId": feed_id}
        if vendor:                                       # группа по МАРКЕ → фильтр по бренду (без модели)
            shopping["FeedFilterConditions"] = [
                {"Operand": "vendor", "Operator": "CONTAINS_ANY", "Arguments": [vendor]}]
        elif collection_id:
            shopping["FeedFilterConditions"] = [
                {"Operand": "collectionId", "Operator": "EQUALS_ANY", "Arguments": [collection_id]}]
        params = {"Ads": [{"AdGroupId": adgroup_id, "ShoppingAd": shopping}]}
        result = self._call("ads", "add", params)
        add_results = result.get("AddResults", [])
        if not add_results:
            raise DirectV501Error("ads.add(ShoppingAd)", 0, "пустой AddResults", str(result))
        first = add_results[0]
        errors = first.get("Errors", [])
        if errors:
            err = errors[0]
            raise DirectV501Error(
                "ads.add(ShoppingAd)",
                err.get("Code", 0),
                err.get("Message", ""),
                err.get("Details", ""),
            )
        ad_id = first.get("Id")
        if not ad_id:
            raise DirectV501Error("ads.add(ShoppingAd)", 0, "нет Id в ответе", str(first))
        return ad_id

    def add_listing_ad(self, adgroup_id: int, feed_id: int,
                       collection_id: str | None = None, vendor: str | None = None) -> int:
        """Добавить «объявление по каталогу» (ListingAd) из фида в группу ЕПК.

        vendor (группа по МАРКЕ): фильтр по бренду (марка без модели). Приоритетнее collection_id.

        В товарной группе tp5/tp7 живут ОБА фид-объявления по одному фиду:
        ShoppingAd («товарное», товарная галерея) + ListingAd («по каталогу»).
        collection_id — фильтр по модели (collectionId из listings фида), как в слепках.
        Проверено live 2026-06-21: ListingAd требует FeedId (id=…), code=0;
        FeedFilterConditions — валидное поле ListingAd в v501.
        """
        listing: dict = {"FeedId": feed_id}
        if vendor:                                       # группа по МАРКЕ → фильтр по бренду (без модели)
            listing["FeedFilterConditions"] = [
                {"Operand": "vendor", "Operator": "CONTAINS_ANY", "Arguments": [vendor]}]
        elif collection_id:
            listing["FeedFilterConditions"] = [
                {"Operand": "collectionId", "Operator": "EQUALS_ANY", "Arguments": [collection_id]}]
        result = self._call("ads", "add",
                            {"Ads": [{"AdGroupId": adgroup_id, "ListingAd": listing}]})
        first = (result.get("AddResults") or [{}])[0]
        errs = first.get("Errors", [])
        if errs:
            raise DirectV501Error("ads.add(ListingAd)", errs[0].get("Code", 0),
                                  errs[0].get("Message", ""), errs[0].get("Details", ""))
        ad_id = first.get("Id")
        if not ad_id:
            raise DirectV501Error("ads.add(ListingAd)", 0, "нет Id в ответе", str(first))
        return ad_id

    def add_feed_ads_batch(
        self,
        feed_groups: list[dict],
    ) -> list[tuple]:
        """Batch ads.add: создать ShoppingAd+ListingAd пары для N feed-групп одним вызовом.

        feed_groups: [{"adgroup_id": int, "feed_id": int, "vendor"?: str, "collection_id"?: str}, ...]
        Возвращает позиционный список: [(shop_id, listing_id, error_msg), ...] — result[i] ↔ feed_groups[i].
        shop_id/listing_id = None если AddResults содержит ошибку для этого объявления.
        error_msg = None при успехе обоих объявлений; иначе строка с описанием ошибки.

        Порядок в Ads-массиве: Ads[2*i] = ShoppingAd(feed_groups[i]), Ads[2*i+1] = ListingAd(feed_groups[i]).
        Позиционное соответствие гарантировано: API v501 возвращает AddResults в том же порядке.
        Чанкование: пачки по _FEED_ADS_BATCH_SIZE пар (дефолт=10, дефолт официального лимита нет).

        Экономия баллов: 2×N вызовов ads.add → ceil(N/_FEED_ADS_BATCH_SIZE) вызовов.
        """
        if not feed_groups:
            return []
        out: list[tuple] = []
        for chunk_start in range(0, len(feed_groups), _FEED_ADS_BATCH_SIZE):
            chunk = feed_groups[chunk_start : chunk_start + _FEED_ADS_BATCH_SIZE]
            ads_payload: list[dict] = []
            for fg in chunk:
                ag = fg["adgroup_id"]
                fid = fg["feed_id"]
                vendor = fg.get("vendor")
                collection_id = fg.get("collection_id")
                # --- ShoppingAd ---
                shopping: dict = {"FeedId": fid}
                if vendor:
                    shopping["FeedFilterConditions"] = [
                        {"Operand": "vendor", "Operator": "CONTAINS_ANY", "Arguments": [vendor]}]
                elif collection_id:
                    shopping["FeedFilterConditions"] = [
                        {"Operand": "collectionId", "Operator": "EQUALS_ANY", "Arguments": [collection_id]}]
                # --- ListingAd ---
                listing: dict = {"FeedId": fid}
                if vendor:
                    listing["FeedFilterConditions"] = [
                        {"Operand": "vendor", "Operator": "CONTAINS_ANY", "Arguments": [vendor]}]
                elif collection_id:
                    listing["FeedFilterConditions"] = [
                        {"Operand": "collectionId", "Operator": "EQUALS_ANY", "Arguments": [collection_id]}]
                ads_payload.append({"AdGroupId": ag, "ShoppingAd": shopping})
                ads_payload.append({"AdGroupId": ag, "ListingAd": listing})

            result = self._call("ads", "add", {"Ads": ads_payload})
            add_results = result.get("AddResults", [])

            for i, _fg in enumerate(chunk):
                shop_r = add_results[i * 2] if i * 2 < len(add_results) else {}
                list_r = add_results[i * 2 + 1] if i * 2 + 1 < len(add_results) else {}
                shop_errs = shop_r.get("Errors", [])
                list_errs = list_r.get("Errors", [])
                shop_id = shop_r.get("Id") if not shop_errs else None
                listing_id = list_r.get("Id") if not list_errs else None
                err_msg = None
                if shop_errs:
                    e = shop_errs[0]
                    err_msg = f"ShoppingAd: {e.get('Message', '')} (code={e.get('Code', 0)})"
                elif list_errs:
                    e = list_errs[0]
                    err_msg = f"ListingAd: {e.get('Message', '')} (code={e.get('Code', 0)})"
                out.append((shop_id, listing_id, err_msg))
        return out

    def get_adgroups(self, campaign_id: int) -> list[dict]:
        """Получить группы объявлений кампании."""
        params = {
            "SelectionCriteria": {"CampaignIds": [campaign_id]},
            "FieldNames": ["Id", "Name", "CampaignId", "RegionIds", "Status"],
            "Page": {"Limit": 1000, "Offset": 0},
        }
        result = self._call("adgroups", "get", params)
        return result.get("AdGroups", [])

    def get_ads(self, adgroup_ids: list[int]) -> list[dict]:
        """Получить объявления по списку adgroup_id."""
        if not adgroup_ids:
            return []
        params = {
            "SelectionCriteria": {"AdGroupIds": adgroup_ids},
            "FieldNames": ["Id", "AdGroupId", "State", "Status", "Type"],
            "TextAdFieldNames": ["Title", "Text", "Href"],
            "ShoppingAdFieldNames": ["FeedId"],
            "Page": {"Limit": 1000, "Offset": 0},
        }
        result = self._call("ads", "get", params)
        return result.get("Ads", [])

    @staticmethod
    def _sitelinks_set_payload(sitelinks: list[dict]) -> dict:
        """Один набор в формате v5: {"Sitelinks": [{Title, Href, Description}, ...]}."""
        return {
            "Sitelinks": [
                {
                    "Title": s.get("Title", s.get("title", "")),
                    # #якорь — внутренняя метка этапа сборки, в живой Href не уходит.
                    "Href": _strip_href_fragment(s.get("Href", s.get("href", ""))),
                    "Description": s.get("Description", s.get("description", "")),
                }
                for s in sitelinks
            ]
        }

    def add_sitelinks_sets(self, sets: list[list[dict]],
                           *, chunk: int = _SITELINKS_SETS_BATCH_SIZE) -> list[dict]:
        """Создать НЕСКОЛЬКО наборов быстрых ссылок за один запрос (API v5 принимает массив).

        sets — список наборов, каждый набор — список {Title, Href, Description}.
        Возвращает список dict'ов ПОЗИЦИОННО по sets: {"id": int|None, "code": int,
        "message": str, "details": str}. Ошибка отдельного набора НЕ роняет весь батч
        (в отличие от одиночного add_sitelinks_set) — вызывающий сам решает по code
        (например 152 → Grid-фолбэк без баллов).
        Транспортная/общая ошибка `_call` пробрасывается наружу как и раньше.

        ВАЖНО: имя параметра — SitelinksSets (двойное s), НЕ SitelinkSets.
        Проверено 2026-06-21: SitelinkSets → ошибка 8000; SitelinksSets → OK.
        """
        if not sets:
            return []
        out: list[dict] = []
        step = max(1, int(chunk or 1))
        for i in range(0, len(sets), step):
            part = sets[i:i + step]
            params = {"SitelinksSets": [self._sitelinks_set_payload(sl) for sl in part]}
            result = self._call("sitelinks", "add", params)
            add_results = result.get("AddResults", []) or []
            for k in range(len(part)):
                if k >= len(add_results):
                    out.append({"id": None, "code": 0, "message": "пустой AddResults",
                                "details": str(result)[:400]})
                    continue
                item = add_results[k] or {}
                errors = item.get("Errors") or []
                if errors:
                    err = errors[0]
                    out.append({"id": None, "code": err.get("Code", 0),
                                "message": err.get("Message", ""),
                                "details": err.get("Details", "")})
                    continue
                sid = item.get("Id")
                out.append({"id": sid, "code": 0, "message": "", "details": ""} if sid
                           else {"id": None, "code": 0, "message": "нет Id в ответе",
                                 "details": str(result)[:400]})
        return out

    def add_sitelinks_set(self, sitelinks: list[dict]) -> int:
        """Создать набор быстрых ссылок. sitelinks — список {Title, Href, Description}.
        Возвращает SitelinkSetId. Сигнатура и поведение (raise на любой ошибке,
        в т.ч. code=152 → Grid-фолбэк у вызывающего) сохранены 1:1.
        """
        res = self.add_sitelinks_sets([sitelinks])
        first = res[0] if res else {"id": None, "code": 0, "message": "пустой AddResults",
                                    "details": ""}
        if not first.get("id"):
            raise DirectV501Error(
                "sitelinks.add",
                first.get("code", 0),
                first.get("message", ""),
                first.get("details", ""),
            )
        return first["id"]

    def get_callouts(self) -> dict[str, int]:
        """Существующие уточнения аккаунта → {текст: AdExtensionId}.
        Переиспользуем, чтобы не плодить дубли ассетов."""
        params = {
            "SelectionCriteria": {"Types": ["CALLOUT"]},
            "FieldNames": ["Id", "Type"],
            "CalloutFieldNames": ["CalloutText"],
            "Page": {"Limit": 1000},
        }
        out: dict[str, int] = {}
        offset = 0
        while True:
            params["Page"]["Offset"] = offset
            result = self._call("adextensions", "get", params)
            for it in result.get("AdExtensions", []):
                txt = (it.get("Callout", {}) or {}).get("CalloutText", "").strip()
                if txt and txt not in out:
                    out[txt] = it["Id"]
            if "LimitedBy" not in result:
                break
            offset = result["LimitedBy"]
        return out

    def ensure_callouts(self, texts: list[str]) -> list[int]:
        """Гарантировать уточнения (CALLOUT) для texts → список AdExtensionId.

        Существующие переиспользуем по тексту, недостающие создаём (adextensions.add).
        ⚠️ Уточнения — АККАУНТНЫЙ ассет; «на уровне кампании» = привязать
        полученные id к КАЖДОМУ объявлению через add_text_ad(callout_ids=...).
        """
        texts = _dedup_keep_order([t.strip() for t in texts if t and t.strip()])
        if not texts:
            return []
        existing = self.get_callouts()
        ids: list[int] = []
        to_create = [t for t in texts if t not in existing]
        if to_create:
            params = {"AdExtensions": [{"Callout": {"CalloutText": t}}
                                       for t in to_create]}
            result = self._call("adextensions", "add", params)
            add_results = result.get("AddResults", [])
            for t, r in zip(to_create, add_results):
                errs = r.get("Errors", [])
                if errs:
                    raise DirectV501Error(
                        "adextensions.add", errs[0].get("Code", 0),
                        errs[0].get("Message", ""), errs[0].get("Details", str(t)))
                existing[t] = r["Id"]
        for t in texts:
            if t in existing:
                ids.append(existing[t])
        return ids

    def filter_new_campaign_names(self, names: list[str]) -> tuple[list[str], list[str]]:
        """Защита от дублей: разделить имена на (новые, уже_существующие).

        Сверяет с ВСЕМИ кампаниями аккаунта (любой State, вкл. черновики/архив)
        по точному имени. Возвращает (создавать, пропустить).
        """
        existing = {c["Name"] for c in self.get_campaigns(field_names=["Id", "Name"])}
        new, skip = [], []
        for n in names:
            (skip if n in existing else new).append(n)
        return new, skip

    def add_text_ad(
        self,
        adgroup_id: int,
        title: str,
        text: str,
        href: str,
        title2: str | None = None,
        sitelink_set_id: int | None = None,
        callout_ids: list[int] | None = None,
    ) -> int:
        """Добавить текстово-графическое объявление (TextAd) в группу UNIFIED_CAMPAIGN.

        Используется в tp4 «Поиск + Динамика» и tp5 «Поиск + Динамика + Товарная».
        В ЕПК TextAd работает на поисковом канале (Search≠SERVING_OFF).

        title:           заголовок 1 объявления (до 35 символов).
        title2:          заголовок 2 (опц., до 30 символов).
        text:            текст объявления (до 81 символа).
        href:            URL посадочной страницы.
        sitelink_set_id: id набора быстрых ссылок (опц.).
        callout_ids:     id уточнений (CALLOUT AdExtension), опц.

        ⚠️ В API НЕТ расширений на уровне кампании (проверено: валидные поля ЕПК —
        CounterIds/Settings/BiddingStrategy/PriorityGoals/TrackingParams/...
        /NegativeKeywordSharedSetIds — расширений среди них нет). Уточнения и быстрые
        ссылки крепятся ТОЛЬКО к объявлению. «На уровне кампании» = один и тот же
        sitelink_set_id + callout_ids привязываем к КАЖДОМУ объявлению набора.

        Возвращает ad_id (int).
        """
        text_ad: dict = {
            "Title": title,
            "Text": text,
            "Href": href,
        }
        if title2:
            text_ad["Title2"] = title2
        if sitelink_set_id:
            text_ad["SitelinkSetId"] = sitelink_set_id
        if callout_ids:
            text_ad["AdExtensions"] = [{"AdExtensionId": i} for i in callout_ids]

        params = {
            "Ads": [
                {
                    "AdGroupId": adgroup_id,
                    "TextAd": text_ad,
                }
            ]
        }
        result = self._call("ads", "add", params)
        add_results = result.get("AddResults", [])
        if not add_results:
            raise DirectV501Error("ads.add(TextAd)", 0, "пустой AddResults", str(result))
        first = add_results[0]
        errors = first.get("Errors", [])
        if errors:
            err = errors[0]
            raise DirectV501Error(
                "ads.add(TextAd)",
                err.get("Code", 0),
                err.get("Message", ""),
                err.get("Details", ""),
            )
        ad_id = first.get("Id")
        if not ad_id:
            raise DirectV501Error("ads.add(TextAd)", 0, "нет Id в ответе", str(first))
        return ad_id

    def setup_search_dynamic_campaign(
        self,
        campaign_id: int,
        feed_id: int,
        href: str,
        text_title: str = "Объявление",
        text_body: str = "Текст объявления",
        group_name: str = "Поиск + Динамика",
        region_ids: list[int] | None = None,
    ) -> tuple[int, int, int]:
        """tp4 «Поиск + Динамика»: добавить одну группу с TextAd + ShoppingAd.

        ЕПК должна быть создана через create_unified_campaign(mode='search').
        «Динамика» в ЕПК = ShoppingAd (товарное объявление с фидом) на поисковом канале.
        DynamicTextAd (v5/DYNAMIC_TEXT_CAMPAIGN) в ЕПК не поддерживается (ошибка 6000
        «Тип объявления не соответствует группе» — проверено 2026-06-21).

        Возвращает (adgroup_id, text_ad_id, shopping_ad_id).
        """
        adgroup_id = self.add_product_adgroup(
            campaign_id, name=group_name, region_ids=region_ids
        )
        # Batch TextAd + ShoppingAd в одном ads.add (3 вызова → 2)
        ads_payload = [
            {"AdGroupId": adgroup_id, "TextAd": {"Title": text_title, "Text": text_body, "Href": href}},
            {"AdGroupId": adgroup_id, "ShoppingAd": {"FeedId": feed_id}},
        ]
        result = self._call("ads", "add", {"Ads": ads_payload})
        add_results = result.get("AddResults", [])
        text_r = add_results[0] if add_results else {}
        shop_r = add_results[1] if len(add_results) > 1 else {}
        text_errs = text_r.get("Errors", [])
        if text_errs:
            raise DirectV501Error("ads.add(TextAd)", text_errs[0].get("Code", 0),
                                  text_errs[0].get("Message", ""), text_errs[0].get("Details", ""))
        text_ad_id = text_r.get("Id")
        if not text_ad_id:
            raise DirectV501Error("ads.add(TextAd)", 0, "нет Id в ответе", str(text_r))
        shop_errs = shop_r.get("Errors", [])
        if shop_errs:
            raise DirectV501Error("ads.add(ShoppingAd)", shop_errs[0].get("Code", 0),
                                  shop_errs[0].get("Message", ""), shop_errs[0].get("Details", ""))
        shopping_ad_id = shop_r.get("Id")
        if not shopping_ad_id:
            raise DirectV501Error("ads.add(ShoppingAd)", 0, "нет Id в ответе", str(shop_r))
        return adgroup_id, text_ad_id, shopping_ad_id

    def setup_combined_campaign(
        self,
        campaign_id: int,
        feed_id: int,
        href: str,
        text_title: str = "Объявление",
        text_body: str = "Текст объявления",
        search_group_name: str = "Поиск (текст)",
        product_group_name: str = "Товарная галерея",
        region_ids: list[int] | None = None,
    ) -> tuple[int, int, int, int]:
        """tp5: добавить две группы — поисковую (TextAd = «Продвижение в поисковой выдаче»)
        и товарную (ShoppingAd = «Товарная галерея на поиске», по ВСЕМУ фиду без фильтра).

        КАНОН tp5 (проверено live 2026-06-21, черновики 710909545/710909551):
        ЕПК создаётся через mode='search_cpa' (AVERAGE_CPA) ИЛИ 'search_payconv'
        (PAY_FOR_CONVERSION) — поиск-only, Network=SERVING_OFF. Ручные стратегии ЗАПРЕЩЕНЫ.
        «Места показа» = Товарная галерея на поиске + Продвижение в выдаче; динамические места,
        РСЯ, Карты — ВЫКЛ. (Старый mode='combined' для tp5 НЕ использовать — он включает РСЯ.)
        Каждая tp5 = пара кампаний cpc(AVERAGE_CPA) + cpa(PAY_FOR_CONVERSION).

        Возвращает (search_ag_id, text_ad_id, product_ag_id, shopping_ad_id).
        """
        # Batch: 2 группы в одном adgroups.add, затем TextAd+ShoppingAd в одном ads.add
        # (4 вызова → 2; AdGroupId из ответа берётся ПОЗИЦИОННО: result[0]=search, result[1]=product)
        ag_ids = self.add_product_adgroups_batch(campaign_id, [
            {"name": search_group_name, "region_ids": region_ids},
            {"name": product_group_name, "region_ids": region_ids},
        ])
        search_ag = ag_ids[0]
        product_ag = ag_ids[1]
        if not search_ag:
            raise DirectV501Error("adgroups.add(batch)", 0, "не создана поисковая группа", "")
        if not product_ag:
            raise DirectV501Error("adgroups.add(batch)", 0, "не создана товарная группа", "")
        # Batch: TextAd(search_ag) + ShoppingAd(product_ag) в одном ads.add
        ads_payload = [
            {"AdGroupId": search_ag, "TextAd": {"Title": text_title, "Text": text_body, "Href": href}},
            {"AdGroupId": product_ag, "ShoppingAd": {"FeedId": feed_id}},
        ]
        result = self._call("ads", "add", {"Ads": ads_payload})
        add_results = result.get("AddResults", [])
        text_r = add_results[0] if add_results else {}
        shop_r = add_results[1] if len(add_results) > 1 else {}
        text_errs = text_r.get("Errors", [])
        if text_errs:
            raise DirectV501Error("ads.add(TextAd)", text_errs[0].get("Code", 0),
                                  text_errs[0].get("Message", ""), text_errs[0].get("Details", ""))
        text_ad_id = text_r.get("Id")
        if not text_ad_id:
            raise DirectV501Error("ads.add(TextAd)", 0, "нет Id в ответе", str(text_r))
        shop_errs = shop_r.get("Errors", [])
        if shop_errs:
            raise DirectV501Error("ads.add(ShoppingAd)", shop_errs[0].get("Code", 0),
                                  shop_errs[0].get("Message", ""), shop_errs[0].get("Details", ""))
        shopping_ad_id = shop_r.get("Id")
        if not shopping_ad_id:
            raise DirectV501Error("ads.add(ShoppingAd)", 0, "нет Id в ответе", str(shop_r))
        return search_ag, text_ad_id, product_ag, shopping_ad_id

    def setup_product_campaign(
        self,
        campaign_id: int,
        feed_id: int,
        group_name: str = "Товарная группа",
        region_ids: list[int] | None = None,
    ) -> tuple[int, int]:
        """Полный сценарий: добавить товарную группу + ShoppingAd в существующую ЕПК.

        Используется для tp3 «Товарная галерея» — кампания уже создана через
        create_unified_campaign(mode='network'), товарность задаётся здесь:
        группой + ShoppingAd(feed_id).

        Кампания остаётся черновиком (State=OFF/DRAFT) — запуска нет.

        Возвращает (adgroup_id, ad_id).
        """
        adgroup_id = self.add_product_adgroup(
            campaign_id, name=group_name, region_ids=region_ids
        )
        ad_id = self.add_shopping_ad(adgroup_id, feed_id=feed_id)
        return adgroup_id, ad_id

    def upload_image(self, file_path: str) -> str | None:
        """Загрузить изображение в библиотеку Директа через adimages.add (base64).
        Возвращает AdImageHash (str) или None при ошибке.

        Формат передачи: base64-строка файла в поле ImageData.
        Ограничения Директа: JPG/PNG, ≤10 МБ, мин 450×450px для баннеров РСЯ.
        """
        import base64
        import os
        try:
            with open(file_path, "rb") as f:
                data = base64.b64encode(f.read()).decode("utf-8")
            name = os.path.basename(file_path)[:255]
            result = self._call("adimages", "add", {
                "AdImages": [{"ImageData": data, "Name": name}]
            })
            add_results = result.get("AddResults", [])
            if not add_results:
                return None
            first = add_results[0]
            if first.get("Errors"):
                return None
            return first.get("AdImageHash") or None
        except Exception:  # noqa: BLE001
            return None

    def add_image_ad(
        self,
        adgroup_id: int,
        title: str,
        text: str,
        href: str,
        ad_image_hash: str,
        title2: str | None = None,
        sitelink_set_id: int | None = None,
        tracking_params: str | None = None,
    ) -> int:
        """Добавить текстово-графическое объявление (TextAd с ImageHash) для РСЯ (tp1).

        adgroup_id:     id группы объявлений
        ad_image_hash:  хэш изображения из adimages.add (поле AdImageHash)
        title:          заголовок 1 (≤35 символов для ЕПК, ≤56 для TextCampaign)
        text:           текст объявления (≤81 символ)
        href:           URL посадочной страницы
        title2:         заголовок 2 (опц., ≤30 символов)
        sitelink_set_id: id набора быстрых ссылок (опц.)
        tracking_params: UTM-параметры (опц.)

        Возвращает ad_id (int).
        """
        text_ad: dict = {
            "Title": title,
            "Text": text,
            "Href": href,
            "AdImageHash": ad_image_hash,
        }
        if title2:
            text_ad["Title2"] = title2
        if sitelink_set_id:
            text_ad["SitelinkSetId"] = sitelink_set_id

        ad: dict = {"AdGroupId": adgroup_id, "TextAd": text_ad}
        if tracking_params:
            ad["TextAd"]["TurboPageId"] = None  # нет турбо
            # TrackingParams — на уровне AdGroup (не TextAd) в v501; передаём как внешнее поле
            # Примечание: в v501 TrackingParams для групп ЕПК задаётся через adgroups.add/update,
            # НЕ в объявлении. Здесь поле игнорируем — UTM ставится при создании группы.

        result = self._call("ads", "add", {"Ads": [ad]})
        add_results = result.get("AddResults", [])
        if not add_results:
            raise DirectV501Error("ads.add(TextAd+Image)", 0, "пустой AddResults", str(result))
        first = add_results[0]
        errors = first.get("Errors", [])
        if errors:
            err = errors[0]
            raise DirectV501Error(
                "ads.add(TextAd+Image)",
                err.get("Code", 0),
                err.get("Message", ""),
                err.get("Details", ""),
            )
        ad_id = first.get("Id")
        if not ad_id:
            raise DirectV501Error("ads.add(TextAd+Image)", 0, "нет Id в ответе", str(first))
        return ad_id


def build_v501_client(spec: UnifiedCampaignSpec) -> DirectV501Client:
    """Готовый v501-клиент из UnifiedCampaignSpec."""
    return DirectV501Client(spec.oauth_token, spec.client_login)
