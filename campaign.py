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

    def add_sitelinks_set(self, sitelinks: list[dict]) -> int:
        """Создать набор быстрых ссылок. sitelinks — список {Title, Href, Description}.
        Возвращает SitelinkSetId.

        ВАЖНО: имя параметра — SitelinksSets (двойное s), НЕ SitelinkSets.
        Проверено 2026-06-21: SitelinkSets → ошибка 8000; SitelinksSets → OK.
        """
        params = {
            "SitelinksSets": [
                {
                    "Sitelinks": [
                        {
                            "Title": s.get("Title", s.get("title", "")),
                            "Href": s.get("Href", s.get("href", "")),
                            "Description": s.get("Description", s.get("description", "")),
                        }
                        for s in sitelinks
                    ]
                }
            ]
        }
        result = self._call("sitelinks", "add", params)
        add_results = result.get("AddResults", [])
        if not add_results:
            raise DirectV501Error("sitelinks.add", 0, "пустой AddResults", str(result))
        first = add_results[0]
        errors = first.get("Errors", [])
        if errors:
            err = errors[0]
            raise DirectV501Error(
                "sitelinks.add",
                err.get("Code", 0),
                err.get("Message", ""),
                err.get("Details", ""),
            )
        sitelink_set_id = first.get("Id")
        if not sitelink_set_id:
            raise DirectV501Error("sitelinks.add", 0, "нет Id в ответе", str(first))
        return sitelink_set_id

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
        text_ad_id = self.add_text_ad(adgroup_id, title=text_title, text=text_body, href=href)
        shopping_ad_id = self.add_shopping_ad(adgroup_id, feed_id=feed_id)
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
        search_ag = self.add_product_adgroup(
            campaign_id, name=search_group_name, region_ids=region_ids
        )
        text_ad_id = self.add_text_ad(search_ag, title=text_title, text=text_body, href=href)

        product_ag = self.add_product_adgroup(
            campaign_id, name=product_group_name, region_ids=region_ids
        )
        shopping_ad_id = self.add_shopping_ad(product_ag, feed_id=feed_id)

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
    #   [{"conditions":[{"field":"collectionId","operator":"CONTAINS","value":"[\"model_26\"]"}]}]  (по имени модели НЕ фильтрует!)

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


def fetch_cookie_glavpotok(login: str) -> str | None:
    """Свежая строка кук агентского аккаунта из главпотока — ОСНОВНОЙ источник.

    GET <base_url>/<login> с Authorization: Bearer <token> → cookie_string.
    None, если конфиг недоступен / у главпотока нет куки (404) / пустой ответ.
    (cookies.json быстро протухает — потому по умолчанию берём отсюда.)
    """
    try:
        cfg = _glavpotok_cfg()
    except Exception:  # noqa: BLE001 — нет конфига → пусть сработает fallback на cookies.json
        return None
    if not cfg.get("token"):
        return None
    r = requests.get(f"{cfg['base_url']}/{login}",
                     headers={"Authorization": f"Bearer {cfg['token']}"}, timeout=20)
    if r.status_code != 200:
        return None
    try:
        cs = (r.json().get("cookie_string") or "").strip()
    except Exception:  # noqa: BLE001
        return None
    return cs or None


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
        with p.open("rb") as fh:
            _file_bytes = fh.read()
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
        from .routes_content_editor import _grid_tp67_campaigns

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
        # Яндекс лимит: не более 100 goals в одном condition_rules.
        # _slepok_audiences_for объединяет все категории → может дать 200+ id → INVALID_COLLECTION_SIZE.
        _all_goals = _audience_goals(spec)
        goals = _all_goals[:100]
        if len(_all_goals) > 100:                 # реальный лимит API — режем, но НЕ молча
            print(
                f"WARNING build_payload: аудиторных сегментов {len(_all_goals)} > лимита Яндекса 100 "
                f"в одном condition_rules — {len(_all_goals) - 100} отброшено "
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
        content_ids: list[str] = []
        image_limit = max(0, int(getattr(spec, "image_limit", 5) or 0))
        image_count = 0
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
        for u in spec.video_urls:                       # видео по URL
            try:
                content_ids.append(self.upload_content(u, "video", spec.adv_type))
            except UacApiError:
                pass
            time.sleep(0.3)
        for path in spec.video_files[:2]:               # ЛОКАЛЬНЫЕ видео (multipart, лимит 2)
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
            elif e.status == 400 and "DUPLICATE_SITELINK_DESCS" in e.body:
                # UAC API отклонил набор быстрых ссылок как содержащий дубли description.
                # Sitelinks не критичны — retry без них (кампания создаётся, ссылки добавить позже).
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


# ─── Хелперы ──────────────────────────────────────────────────────────────────


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
        out.append({
            "title": title,
            "description": desc,
            "href": s.get("href") or base_href,
        })
    return out


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
    """Рабочая агентская кука для ulogin — ПРОВЕРЯЕТСЯ ОДИН РАЗ НА АККАУНТ (link_info → 200), потом
    кэшируется на TTL (правило: не дёргать главпоток на каждую кампанию). Порядок подбора:
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
    if _mng and _mng not in ("none", ""):
        accounts = tuple(dict.fromkeys((_mng, *accounts)))
    last_err: Exception | None = None
    expired_accs: list[str] = []   # агентства, чья кука на главпотоке ПРОТУХЛА (сессия истекла)
    for acc in accounts:
        # Сначала локальная (проверка «работает ли на аккаунте»), затем свежая с главпотока.
        for getter in (load_cookie_local, fetch_cookie_glavpotok):
            try:
                cookie = getter(acc)
            except Exception:  # noqa: BLE001 — нет такого аккаунта/конфига → следующий источник
                continue
            if not cookie:
                continue
            try:
                UacClient(cookie, ulogin).link_info("https://ya.ru")
                _ACCOUNT_COOKIE_CACHE[ulogin] = (cookie, time.time())
                return cookie
            except Exception as e:  # noqa: BLE001 — кука не подошла → следующий источник/аккаунт
                last_err = e
                # Сессия Яндекса протухла (главпоток отдал мёртвую куку): need_reset / «Истёк срок».
                if any(s in str(e) for s in ("need_reset", "Истек срок", "Истёк срок", "session")):
                    if acc not in expired_accs:
                        expired_accs.append(acc)
    if expired_accs:
        # Чёткое actionable-сообщение → видно в UI/джобе: куку надо ОБНОВИТЬ на главпотоке (перелогин).
        raise RuntimeError(
            f"кука протухла на главпотоке (сессия Яндекса истекла) для агентств: "
            f"{', '.join(expired_accs)}. Обновите куку в главпотоке (перелогиньтесь в этом "
            f"агентском аккаунте) — текущая для ulogin={ulogin} мертва [need_reset].")
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
        print(f"❌ Ошибка {e.step}: HTTP {e.status}\n{e.body[:600]}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ Создана кампания id={cid} (ulogin={ulogin}, "
          f"{'ЗАПУЩЕНА' if launch else 'черновик'})")
