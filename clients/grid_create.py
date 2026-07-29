"""Куки-движок СОЗДАНИЯ кампаний (Grid GraphQL на куках агентства, БЕЗ баллов API).

Назначение: когда у агентства исчерпаны баллы (error 152), а пользователь ЯВНО согласился
через поп-ап «создать по куки (небезопасно)» — создаём tp1–tp5 не через v5/v501 (баллы),
а через приватный web-api/grid (куки). Мутации реверс-инжинирены из HAR (см. direct/_har/):
  AddCampaigns        — кампания (unifiedCampaign: стратегия/платформы/счётчик)
  AddUnifiedAdGroups  — группы + ключи + таргетинг + минус-слова
  AddAdaptiveTextAds  — комбинаторные объявления (ResponsiveAd) + adPrice

⚠️ НЕБЕЗОПАСНО: приватный интерфейс Директа, риск временной блокировки. Запускается ТОЛЬКО
по явному согласию пользователя (флаг via_cookie из поп-апа), НИКОГДА автоматически.
"""
from __future__ import annotations

import re
import time
from typing import Any

import requests

from .. import campaign as cmc
from ..create import stage_timing as _timing   # STAGE_TIMING: пер-стадийный замер (только замер + лог)
from ..text_norm import _trim_clean  # используется в add_shopping_content_to_existing (line ~878)
from ..create.create_set_audiences import group_notes as _audience_notes
from .grid_create_payloads import (   # ре-экспорт: gc.build_ad и др. остаются доступны для внешних импортёров
    _PLATFORMS_OFF, build_unified_campaign, build_adgroup,
    _campaign_minus_kw, _safe_old_price,
    _dedup_keep, _fill_titles, _fill_bodies,
    build_ad, build_shopping_ad,
)

GRID_URL = "https://direct.yandex.ru/web-api/grid/api"
COMMON_IMAGE_CTS = {
    "ct0000", "ct0001", "ct0002", "ct0003", "ct0004", "ct0005", "ct0006",
    "ct0007", "ct0008", "ct0009", "ct0010", "ct0013", "ct0014",
}

_ADD_CAMPAIGNS_Q = (
    "mutation AddCampaigns($input:GdAddCampaignsInput!$login:String!){reqId:getReqId "
    "addCampaigns(input:$input){addedCampaigns{id}validationResult{errors{code params path}"
    "warnings{code params path}}}getClientMutationId(input:{login:$login}){mutationId}}")
_ADD_GROUPS_Q = (
    "mutation AddUnifiedAdGroups($unifiedAddInput:[GdAddUnifiedAdGroupItemInput!]!){reqId:getReqId "
    "addUnifiedAdGroups(input:{addItems:$unifiedAddInput}){addedAdGroupItems{adGroupId}"
    "validationResult{errors{code params path}warnings{code params path}}}}")
_ADD_ADS_Q = (
    "mutation AddAdaptiveTextAds($addInput:GdAddAdaptiveTextAdsInput!){reqId:getReqId "
    "addAdaptiveTextAds(input:$addInput){addedAds{id}validationResult{errors{code params path}"
    "warnings{code params path}}}}")
_DELETE_CAMPAIGNS_Q = (
    "mutation DeleteCampaigns($input:GdCampaignIdsListInput!){reqId:getReqId "
    "deleteCampaigns(input:$input){validationResult{errors{code params path}"
    "warnings{code params path}}}}")
_ADD_SHOPPING_ADS_Q = (   # товарное объявление (Товарная галерея tp3/tp5) по фиду — реверс из HAR17
    "mutation AddShoppingAds($input:GdAddShoppingAdsInput!){reqId:getReqId "
    "addShoppingAds(input:$input){addedAds{id}validationResult{errors{code params path}"
    "warnings{code params path}}}}")
_ADD_KEYWORDS_Q = (       # заливка ключевых фраз через Grid (без баллов API)
    "mutation AddKeywords($input:GdAddKeywordsInput!){"
    "addKeywords(input:$input){addedItems{adGroupId keywordId}"
    "validationResult{errors{code params path}warnings{code params path}}}}")
_ADGROUP_NAMES_Q = (      # read-back name→id (фикс позиционного сдвига, см. _read_adgroup_name_to_id)
    "query AdGroupNames($login:String!,$inp:GdAdGroupsContainerInput!){"
    "client(searchBy:{login:$login}){"
    "adGroups(input:$inp){rowset{id name}}}}")
_CAMPAIGN_NAMES_Q = (     # read-back name→id кампаний аккаунта (сверка факта после потери ответа)
    # status{primaryStatus archived} — форма, проверенная живым yandex_gateway.grid_list_campaigns:321.
    # archived нужен, чтобы одноимённая ПУСТАЯ АРХИВНАЯ кампания прошлого прогона не была
    # «усыновлена» сверкой (она проходит обе проверки — ровно одна и без групп — и группы
    # текущего набора уехали бы в архив).
    "query CampaignNames($login:String!,$inp:GdCampaignsContainerInput!){"
    "client(searchBy:{login:$login}){"
    "campaigns(input:$inp){rowset{id name status{primaryStatus archived}}}}}")


class GridCreateError(RuntimeError):
    """Ошибка Grid-создания.

    ``transient=True`` — ответ Grid потерян или сервер сбоил (5xx / «Внутренняя ошибка сервера» /
    не-JSON): объект МОГ быть уже создан. Такую ошибку нельзя лечить слепым повтором мутации —
    только сверкой фактического состояния (см. ``_add_adgroups_reconcile``).
    """

    def __init__(self, *args, transient: bool = False):
        super().__init__(*args)
        self.transient = bool(transient)


def _response_lost(exc: BaseException) -> bool:
    """True, если ответ Grid не получен/не осмыслен → мутация МОГЛА закоммититься на сервере."""
    if isinstance(exc, GridCreateError):
        return bool(getattr(exc, "transient", False))
    return isinstance(exc, (requests.RequestException, OSError))


def _gate_groups_created(rep: dict, expected: int) -> None:
    """Гейт «создано групп ≠ отправлено» — РАСХОЖДЕНИЕ ВИДНО, но НЕ РАЗРУШИТЕЛЬНО.

    Раньше расхождение проходило МОЛЧА: ``rep["groups"]`` просто оказывался меньше, кампания
    считалась успешной. А это единственный внешний признак частичного коммита групп / потери
    ответа AddUnifiedAdGroups.

    ⛔ В ``rep["errors"]`` расхождение класть НЕЛЬЗЯ: в куки-пути tp2/tp4/tp5
    (`create_set_feed_builders._create_text_via_cookie`) любой непустой ``errors`` = приговор —
    `_delete_partial_campaign` + `defer`. «Создано 13 из 14» сносило бы кампанию с 13 рабочими
    группами. Поэтому: число ушедших групп — в ``rep["groups_expected"]`` (сравнивает верификатор,
    код `GROUPS_CREATED_LESS_THAN_SENT`, severity=error, report-only), текст — в ``rep["warnings"]``.
    """
    made = int(rep.get("groups") or 0)
    exp = int(expected or 0)
    rep["groups_expected"] = exp
    if exp and made != exp:
        rep["groups_shortfall"] = exp - made
        rep.setdefault("warnings", []).append(
            f"группы(AddUnifiedAdGroups): создано {made} из {exp} отправленных")


class GridCreateClient:
    """Тонкий клиент создания через web-api/grid на куках (как grid_finalize.GridClient)."""

    def __init__(self, login: str, cookie: str | None = None, *, timeout: int = 60):
        self.login = login
        self.cookie = cookie or cmc.pick_working_cookie(login)
        self.csrf: str | None = None
        self.timeout = timeout
        self.sess = requests.Session()
        self.sess.verify = False

    @staticmethod
    def _looks_like_login_page(resp: requests.Response) -> bool:
        text = (resp.text or "")[:800].lower()
        return (
            "need_reset" in text
            or "passport.yandex" in text
            or "<title>log in</title>" in text
            or ("<html" in text and "login" in text)
        )

    def _refresh_cookie(self) -> None:
        self.cookie = cmc.pick_working_cookie(self.login, force_refresh=True)
        self.csrf = None
        self.sess = requests.Session()
        self.sess.verify = False
        # Сбрасываем кэш get_grid_client для этого логина: после смены куки
        # закэшированные GridClient-инстансы держат протухший cookie/csrf и будут
        # получать 403 до принудительной инвалидации.
        try:
            from . import grid_finalize as _gf_ref
            _gf_ref.reset_grid_client_cache(self.login)
        except Exception:  # noqa: BLE001 — best-effort, кэш-инвалидация не критична
            pass

    # ── низкий уровень ───────────────────────────────────────────────────────
    def _post(self, op: str, query: str, variables: dict) -> dict:
        headers = {
            "Cookie": self.cookie, "dna-operation-name": op, "x-direct-api": "1",
            "x-detected-locale": "ru", "Content-Type": "application/json",
            "User-Agent": cmc.USER_AGENT, "Origin": "https://direct.yandex.ru",
            "Referer": f"https://direct.yandex.ru/dna/grid/campaigns?ulogin={self.login}",
        }
        if self.csrf:
            headers["x-csrf-token"] = self.csrf
        url = f"{GRID_URL}?operationName={op}&ulogin={self.login}"
        # БЕЗ транспортного ретрая: AddCampaigns/AddGroups/AddAds — НЕ идемпотентны. Обрыв ответа
        # после commit + ретрай = ДУБЛЬ кампании/группы/объявления. Единичный обрыв ловит
        # per-iteration try/except в _copy_grid_unified_campaigns (кампания падает чисто, ре-ран добьёт).
        r = self.sess.post(url, json={"operationName": op, "query": query, "variables": variables},
                           headers=headers, timeout=self.timeout)
        m = re.search(r"_direct_csrf_token=([^;,\s]+)", r.headers.get("Set-Cookie", ""))
        tok = r.cookies.get("_direct_csrf_token") or (m.group(1) if m else None)
        if tok:
            self.csrf = tok
        return r

    def _bootstrap_csrf(self) -> None:
        q = ("query Callouts($login:String!){callouts(input:{searchBy:{login:$login}"
             "filter:{deleted:false}}){id}}")
        r = self._post("Callouts", q, {"login": self.login})
        if r.status_code == 403:
            self._post("Callouts", q, {"login": self.login})

    # Транзиентные серверные ошибки Яндекса в top-level errors (НЕ валидация) — ретраим с backoff.
    # Приходят как {'message':'Внутренняя ошибка сервера ... reqId=...'} → group=0 → hard-fail айтема.
    # Валидационные ошибки (res.validationResult.errors) сюда НЕ попадают — их не ретраим (детерминизм).
    _TRANSIENT_ERR = ("внутренняя ошибка сервера", "internal server error", "internal error",
                      "timeout", "timed out", "temporarily", "try again", "503", "502", "504")

    # ⛔ Слепой повтор Add*-мутации ЗАПРЕЩЁН: сервер мог УЖЕ закоммитить запрос и не успеть
    # ответить → повтор даёт ДУБЛЬ (боевой porg-nqavjicg, camp 713102313: 14 пустых групп-сирот
    # 5777472935..948 рядом с 14 полными 5777472963..976; журнал
    # RETRY_ON_NETWORK_LOSS_DUPLICATES_ADD). Тот же принцип, что yandex_gateway._creates_objects
    # (v5) и запрет ретрая в grid_finalize._post / post_idempotent (Grid-докрутка).
    # Покрывает AddCampaigns / AddUnifiedAdGroups / AddAdaptiveTextAds / AddShoppingAds и любую
    # НЕизвестную Add*-мутацию. Устойчивость к реальной потере ответа даёт не ретрай, а сверка
    # фактического состояния (_add_adgroups_reconcile).
    # Исключение — AddKeywords: Директ схлопывает одинаковые фразы и на повтор возвращает
    # keywordId УЖЕ существующей (живой зонд, см. unique_keyword_ids) → дубля в кабинете нет.
    _RETRY_SAFE_ADD_OPS = ("AddKeywords",)

    # ⚠️ Классификация — ЧЕРЕЗ ALLOW-LIST идемпотентных, а не через префикс `Add`
    # (тот же консервативный принцип, что в yandex_gateway._creates_objects: неизвестный метод
    # считается СОЗДАЮЩИМ, «дубль дороже отказа»). Иначе будущая создающая мутация не на `Add*`
    # (`CopyCampaigns`, `CreateSitelinkSet`, `ImportFeed`…) молча получила бы слепой ретрай.
    # В списке только операции, повтор которых заведомо не плодит объектов:
    #   • чтения этого модуля (`AdGroupNames`, `AdsAgid`, `CampaignNames`);
    #   • `Delete*` (повторное удаление идемпотентно);
    #   • `AddKeywords` (схлопывание фраз Директом — см. _RETRY_SAFE_ADD_OPS выше).
    # Только реально существующие в grid_create операции: чужие имена в списке (был скопированный
    # из yandex_gateway `Callouts`, которого здесь нет) вводят в заблуждение при чтении правила.
    _IDEMPOTENT_OPS = frozenset(_RETRY_SAFE_ADD_OPS) | frozenset({
        "AdGroupNames", "AdsAgid", "CampaignNames",
    })

    @classmethod
    def _creates_objects(cls, op: str) -> bool:
        """Мутация создаёт объекты и потому неидемпотентна → повторять её нельзя."""
        name = str(op or "")
        if name in cls._IDEMPOTENT_OPS or name.startswith("Delete"):
            return False
        return True

    def _mutate(self, op: str, query: str, variables: dict) -> dict:
        """POST мутации с ретраем на 403 (свежий CSRF) И на транзиентную серверную ошибку Яндекса
        ('Внутренняя ошибка сервера' в top-level errors) — tries+backoff. → JSON. GridCreateError на сбое."""
        # STAGE_TIMING: замер одной Grid-мутации целиком (включая CSRF/транзиент-ретраи) — только
        # внутри item-контекста создания набора. Поведение не меняется, это чистый замер.
        with _timing.stage(f"grid:{op}", only_in_item=True):
            _last_transient = None
            for srv_try in range(3):                     # до 3 попыток на транзиентную 5xx-подобную ошибку
                for attempt in range(2):                 # внутренний ретрай: 403(CSRF)/страница логина
                    if self.csrf is None:
                        self._bootstrap_csrf()
                    r = self._post(op, query, variables)
                    if r.status_code == 403:
                        r = self._post(op, query, variables)
                    if self._looks_like_login_page(r) and attempt == 0:
                        self._refresh_cookie()
                        continue
                    break
                try:
                    j = r.json()
                except Exception as e:  # noqa: BLE001
                    # Ответ не разобран (HTML 502/обрыв) — состояние на сервере НЕИЗВЕСТНО.
                    raise GridCreateError(f"{op}: не-JSON HTTP {r.status_code}: {r.text[:160]}",
                                          transient=True) from e
                errs = j.get("errors")
                if errs:
                    _msg = str(errs).lower()
                    _is_transient = any(t in _msg for t in self._TRANSIENT_ERR)
                    if _is_transient and not self._creates_objects(op) and srv_try < 2:
                        _last_transient = str(errs)[:240]
                        time.sleep(0.6 * (srv_try + 1))  # backoff 0.6s → 1.2s перед повтором
                        continue                         # транзиент Яндекса → повторяем мутацию
                    # Add*: повтор запрещён (дубли) → отдаём ошибку с пометкой transient,
                    # вызывающий решает по ФАКТИЧЕСКОМУ состоянию, а не вслепую.
                    raise GridCreateError(f"{op}: {str(errs)[:240]}", transient=_is_transient)
                return j
            raise GridCreateError(f"{op}: транзиент Яндекса не ушёл за 3 попытки: {_last_transient}",
                                  transient=True)

    # ── шаги создания ────────────────────────────────────────────────────────
    def add_campaign(self, unified_campaign: dict) -> int:
        """AddCampaigns → id созданной кампании. unified_campaign — полный GdUnifiedCampaign-блок.

        Ответ потерян (транзиент 5xx / не-JSON / обрыв) → кампания МОГЛА закоммититься, а id мы
        не знаем: слепой повтор дал бы кампанию-дубль, а отказ — кампанию-сироту, которую даже
        нечем удалить (cleanup зовётся по cid). Поэтому сверяем ФАКТ по имени (_add_campaign_reconcile).
        """
        try:
            return self._add_campaign_once(unified_campaign)
        except Exception as exc:  # noqa: BLE001 — не-потерянный ответ пробрасываем как раньше
            if not _response_lost(exc):
                raise
            return self._add_campaign_reconcile(unified_campaign, exc)

    def _add_campaign_once(self, unified_campaign: dict) -> int:
        """Один AddCampaigns → id кампании (без сверки факта)."""
        j = self._mutate("AddCampaigns", _ADD_CAMPAIGNS_Q,
                         {"login": self.login, "input": {"campaignAddItems": [{"unifiedCampaign": unified_campaign}]}})
        res = (j.get("data") or {}).get("addCampaigns") or {}
        vr = res.get("validationResult") or {}
        if vr.get("errors"):
            raise GridCreateError(f"AddCampaigns validation: {str(vr['errors'])[:240]}")
        added = res.get("addedCampaigns") or []
        if not added or not added[0].get("id"):
            raise GridCreateError(f"AddCampaigns: нет id ({str(res)[:160]})")
        return int(added[0]["id"])

    def _add_campaign_reconcile(self, unified_campaign: dict, exc: BaseException) -> int:
        """Ответ AddCampaigns потерян: решаем по ФАКТУ (read-back по имени), а не повтором вслепую.

          * ровно одна кампания с таким именем И она ещё БЕЗ групп → это наша, отдаём её id;
          * ни одной (после 2 чтений с паузой — реплика Grid отстаёт) → коммита не было → повтор;
          * несколько одноимённых / у найденной уже есть группы / чтение не удалось → исходная
            ошибка наружу: пересоздавать вслепую нельзя (дубль кампании).
        Проверка «групп нет» — защита от совпадения имени со СТАРОЙ кампанией прошлого прогона:
        свежесозданная кампания всегда пуста (группы добавляются следующим шагом).
        """
        name = str((unified_campaign or {}).get("name") or "")
        if not name:
            raise exc
        found: list[int] = []
        read_ok = False
        for _ in range(2):                       # реплика Grid отстаёт от коммита (~2 с)
            time.sleep(_RECONCILE_SETTLE_SEC)
            try:
                found = self._read_campaign_ids_by_name_strict(name)
            except Exception as read_exc:  # noqa: BLE001 — сверка не удалась, решаем ниже
                print(f"[grid] AddCampaigns: сверка кампании «{name[:60]}» не удалась: "
                      f"{str(read_exc)[:120]}", flush=True)
                continue
            read_ok = True
            if found:
                break
        if not read_ok:                          # состояние неизвестно → вслепую не создаём
            raise exc
        if not found:
            print(f"[grid] AddCampaigns: коммита не было (кампании «{name[:60]}» нет) → "
                  f"безопасный повтор ({str(exc)[:120]})", flush=True)
            return self._add_campaign_once(unified_campaign)
        if len(found) > 1:                       # одноимённые — сверка по имени неоднозначна
            raise exc
        cid = found[0]
        try:
            _groups = self._read_adgroup_name_to_id_strict(cid)
        except Exception as read_exc:  # noqa: BLE001
            raise exc from read_exc
        if _groups:                              # не наша свежая кампания (у неё уже есть группы)
            raise exc
        print(f"[grid] AddCampaigns: ответ потерян, но кампания «{name[:60]}» уже создана "
              f"(id={cid}) → повтор НЕ выполняем ({str(exc)[:120]})", flush=True)
        return cid

    def _read_campaign_ids_by_name_strict(self, name: str) -> list[int]:
        """id ЖИВЫХ (не архивных) кампаний аккаунта с точно таким именем. БЕЗ проглатывания ошибок.

        Форма input — как в живом `yandex_gateway.grid_list_campaigns` (фильтра по имени в
        GdCampaignsContainerInput нет, поэтому листаем страницами по 200 и матчим локально).
        Зовётся ТОЛЬКО в редком пути потери ответа AddCampaigns, не в штатном.

        ⛔ Скан оборвался о предохранитель ``_CAMPAIGN_SCAN_MAX`` (аккаунт больше 4000 кампаний) →
        состояние НЕИЗВЕСТНО, и «ничего не нашли» здесь означает «не досмотрели». Возвращать
        пустой список нельзя: для `_add_campaign_reconcile` это «коммита не было» → повторный
        AddCampaigns → ДУБЛЬ кампании. Поднимаем ошибку — сверка не удалась, наружу уходит
        исходный транзиент, вслепую не создаём (тот же принцип, что «read упал» vs «read вернул {}»).

        Архивные одноимённые отбрасываем: пустая архивная кампания прошлого прогона прошла бы обе
        проверки сверки (ровно одна + без групп) и была бы «усыновлена» — группы уехали бы в архив.
        """
        want = str(name or "")
        out: list[int] = []
        offset = 0
        while True:
            j = self._mutate("CampaignNames", _CAMPAIGN_NAMES_Q, {
                "login": self.login,
                "inp": {"filter": {},
                        "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [],
                                             "useCampaignGoalIds": True},
                        "limitOffset": {"limit": 200, "offset": offset},
                        "orderBy": [{"order": "ASC", "field": "STATUS"}]},
            })
            rows = (((j.get("data") or {}).get("client") or {}).get("campaigns") or {}).get("rowset") or []
            for row in rows:
                if str(row.get("name") or "") != want:
                    continue
                if bool((row.get("status") or {}).get("archived")):
                    continue                     # архивная одноимённая — не наша свежая
                try:
                    cid = int(row.get("id") or 0)
                except (TypeError, ValueError):
                    cid = 0
                if cid:
                    out.append(cid)
            if len(rows) < 200:
                return out                       # страница неполная = аккаунт досмотрен до конца
            offset += 200
            if offset > _CAMPAIGN_SCAN_MAX:      # не досмотрели → «не найдено» было бы ложью
                raise GridCreateError(
                    f"CampaignNames: скан кампаний оборван на offset={offset} "
                    f"(предохранитель _CAMPAIGN_SCAN_MAX={_CAMPAIGN_SCAN_MAX}) — "
                    f"состояние аккаунта неизвестно, сверка по имени невозможна")

    def add_adgroups(self, items: list[dict], *, campaign_is_new: bool = False) -> list[int | None]:
        """AddUnifiedAdGroups → список adGroupId (в порядке items; None для упавших).

        Ответ потерян (транзиент 5xx / не-JSON / обрыв соединения) → мутация МОГЛА закоммититься.
        Слепого повтора здесь нет (он давал 14 групп-сирот на porg-nqavjicg, camp 713102313):
        сверяем фактическое состояние кампании и досоздаём только реально отсутствующие группы.

        ``campaign_is_new=True`` — кампания создана прямо перед вызовом (create_full /
        create_shopping_full), значит групп в ней заведомо 0 и предмутационный снимок имён читать
        не надо (экономим один Grid-запрос в горячем пути).
        """
        if not items:
            return []
        ctx = self._adgroups_reconcile_ctx(items, campaign_is_new=campaign_is_new)
        return self._add_adgroups_chunked(items, ctx)

    def _adgroups_reconcile_ctx(self, items: list[dict], *, campaign_is_new: bool) -> dict:
        """Контекст сверки, снятый ДО первой мутации (см. `_add_adgroups_reconcile`).

        Держит два факта, без которых сверка по имени неверна при коллизиях имён:
          * ``names_sent`` — имена ВСЕГО вызова (а не одного чанка по 50): одноимённые группы в
            одной кампании реальны (слепки с коллизиями `gk` — 194 таких на porg-nqavjicg), и
            имя, созданное чанком 1, нельзя засчитать чанку 3;
          * ``live_before`` — имена групп кампании ДО мутации: совпадение с уже существующей
            группой (`add_text_content_to_existing`) иначе выглядело бы как «наша уже создана».
        ``live_before is None`` = состояние неизвестно → сверка отключается (ошибка наружу).
        """
        names = [str((it or {}).get("name") or "") for it in items]
        cids = {str((it or {}).get("campaignId") or "").strip() for it in items}
        cids.discard("")
        cid_raw = next(iter(cids)) if len(cids) == 1 else ""
        ctx: dict = {"names_sent": names, "live_before": None}
        if not cid_raw.isdigit():
            return ctx
        if campaign_is_new:
            ctx["live_before"] = {}
            return ctx
        try:
            ctx["live_before"] = self._read_adgroup_name_to_id_strict(int(cid_raw))
        except Exception as read_exc:  # noqa: BLE001 — снимок best-effort, сверка просто отключится
            print(f"[grid] AddUnifiedAdGroups: предмутационный снимок кампании {cid_raw} "
                  f"не снят: {str(read_exc)[:120]}", flush=True)
        return ctx

    def _add_adgroups_chunked(self, items: list[dict], ctx: dict) -> list[int | None]:
        """Резка по _GRID_MUTATION_CHUNK + сверка факта на потерянном ответе (общий ctx на вызов)."""
        if len(items) > _GRID_MUTATION_CHUNK:
            out: list[int | None] = []
            for i in range(0, len(items), _GRID_MUTATION_CHUNK):
                out.extend(self._add_adgroups_chunked(items[i:i + _GRID_MUTATION_CHUNK], ctx))
                time.sleep(0.15)
            return out
        try:
            return self._add_adgroups_once(items)
        except Exception as exc:  # noqa: BLE001 — разбор ниже: не-потерянный ответ пробрасываем как раньше
            if not _response_lost(exc):
                raise
            return self._add_adgroups_reconcile(items, exc, ctx)

    def _add_adgroups_reconcile(self, items: list[dict], exc: BaseException,
                                ctx: dict | None = None) -> list[int | None]:
        """Ответ AddUnifiedAdGroups потерян: решаем по ФАКТУ, а не повтором вслепую.

        Читаем name→id кампании (``_read_adgroup_name_to_id``) и сравниваем с отправленными именами:
          * все имена уже в кабинете → коммит прошёл, НИЧЕГО не создаём (иначе дубль-сироты);
          * ни одного → коммита не было (реальная сетевая потеря до записи) → безопасный повтор;
          * часть → досоздаём ТОЛЬКО отсутствующие.
        Сверить нельзя (несколько кампаний в чанке / пустые имена / имя неуникально по ВСЕМУ
        вызову / имя уже было в кампании до мутации / нет предмутационного снимка / read-back не
        ответил) → пробрасываем исходную ошибку: пересоздавать вслепую нельзя.
        ⚠️ Сверка по имени — та же посылка, на которой уже держится анти-сдвиг ag_ids в create_full.
        """
        cids = {str((it or {}).get("campaignId") or "").strip() for it in items}
        cids.discard("")
        cid_raw = next(iter(cids)) if len(cids) == 1 else ""
        names = [str((it or {}).get("name") or "") for it in items]
        if not cid_raw.isdigit() or not all(names) or len(set(names)) != len(names):
            raise exc
        _ctx = ctx or {}
        _all_sent = list(_ctx.get("names_sent") or names)
        if any(_all_sent.count(n) != 1 for n in names):   # одноимённые группы в другом чанке
            raise exc
        _live_before = _ctx.get("live_before")
        if _live_before is None:                          # состояние ДО мутации неизвестно
            raise exc
        if any(n in _live_before for n in names):         # имя было занято ещё до мутации
            raise exc
        cid = int(cid_raw)
        live: dict[str, int] = {}
        read_ok = False
        for _ in range(2):                       # реплика Grid отстаёт от коммита (~2 с)
            time.sleep(_RECONCILE_SETTLE_SEC)
            try:
                live.update(self._read_adgroup_name_to_id_strict(cid))
            except Exception as read_exc:  # noqa: BLE001 — сверка не удалась, решаем ниже
                print(f"[grid] AddUnifiedAdGroups: сверка состояния кампании {cid} не удалась: "
                      f"{str(read_exc)[:120]}", flush=True)
                continue
            read_ok = True
            if all(n in live for n in names):
                break
        if not read_ok:                          # состояние неизвестно → вслепую не создаём
            raise exc
        missing = [i for i, n in enumerate(names) if n not in live]
        if not missing:
            print(f"[grid] AddUnifiedAdGroups: ответ потерян, но все {len(names)} групп уже в "
                  f"кампании {cid} → повтор НЕ выполняем ({str(exc)[:120]})", flush=True)
            return [live.get(n) for n in names]
        if len(missing) == len(names):
            print(f"[grid] AddUnifiedAdGroups: коммита не было (0 из {len(names)} групп в кампании "
                  f"{cid}) → безопасный повтор ({str(exc)[:120]})", flush=True)
            return self._add_adgroups_once(items)
        print(f"[grid] AddUnifiedAdGroups: частичный коммит в кампании {cid} "
              f"({len(names) - len(missing)} из {len(names)}) → досоздаём {len(missing)}", flush=True)
        made = self._add_adgroups_once([items[i] for i in missing])
        out: list[int | None] = [live.get(n) for n in names]
        for pos, idx in enumerate(missing):
            out[idx] = made[pos] if pos < len(made) else None
        if any(out[i] is None for i in missing):   # ответ Grid короче входа → добираем по имени
            live2 = self._read_adgroup_name_to_id(cid) or {}
            for i in missing:
                if out[i] is None:
                    out[i] = live2.get(names[i])
        return out

    def _add_adgroups_once(self, items: list[dict]) -> list[int | None]:
        """Один AddUnifiedAdGroups (+ ретрай CAMPAIGN_NOT_FOUND) → adGroupId в порядке items."""
        # Grid eventual-consistency: только что созданная (addCampaigns / token v501) кампания ещё
        # не видна валидатору AddUnifiedAdGroups (читает отставшую реплику) → CAMPAIGN_NOT_FOUND.
        # Реплика догоняет за <~2с — ретраим с бэкоффом (2026-07-06 группа D: porg-7bqj56f4 ×3,
        # porg-psm5h7q6 ×1). Прочие ошибки валидации не транзиентны → отдаём сразу.
        vr = {}
        for _ag_try in range(3):
            j = self._mutate("AddUnifiedAdGroups", _ADD_GROUPS_Q, {"unifiedAddInput": items})
            res = (j.get("data") or {}).get("addUnifiedAdGroups") or {}
            vr = res.get("validationResult") or {}
            _errs = vr.get("errors") or []
            if _errs and any("CAMPAIGN_NOT_FOUND" in str(e) for e in _errs) and _ag_try < 2:
                time.sleep(1.2 * (_ag_try + 1))
                continue
            break
        if vr.get("errors"):
            raise GridCreateError(f"AddUnifiedAdGroups validation: {str(vr['errors'])[:240]}")
        out: list[int | None] = []
        for a in (res.get("addedAdGroupItems") or []):
            try:
                out.append(int(a["adGroupId"]))
            except (TypeError, ValueError, KeyError):
                out.append(None)
        return out

    def add_keywords(self, items: list[dict]) -> list[dict]:
        """AddKeywords через Grid (без баллов). items: [{adGroupId, keyword}]. → addedItems.

        ЕДИНСТВЕННЫЙ источник ключей: build_adgroup передаёт keywords=[], а фразы льются этой
        мутацией. Раньше keywords дублировались в спеке группы — Grid создавал их ДВАЖДЫ для
        групп <~140 ключей (крупные AddUnifiedAdGroups keywords игнорирует, отсюда была иллюзия
        «молча игнорирует»). addKeywords работает для ЕПК и любого объёма (проверено live).
        Пропускает ---autotargeting спецключ (он живёт в relevanceMatch, не в keywords).
        """
        clean = [
            {"adGroupId": str(it.get("adGroupId") or ""), "keyword": str(it.get("keyword") or "")}
            for it in (items or [])
            if str(it.get("keyword") or "").strip() and not str(it.get("keyword") or "").startswith("---")
               and str(it.get("adGroupId") or "").strip()
        ]
        if not clean:
            return []
        if len(clean) > 1000:
            out: list[dict] = []
            for i in range(0, len(clean), 1000):
                out.extend(self.add_keywords(clean[i:i + 1000]))
                time.sleep(0.1)
            return out
        j = self._mutate("AddKeywords", _ADD_KEYWORDS_Q, {"input": {"addItems": clean}})
        res = (j.get("data") or {}).get("addKeywords") or {}
        # #ФИКС-7: surface validationResult.errors (как add_campaign/add_adgroups). МЯГКО —
        # НЕ raise (copy_steps/repair_executor толерантны к partial), но не молчим: раньше ошибка
        # валидатора глоталась → кампания «ok» с 0 ключей. Гейт кол-ва — на стороне build-сайтов.
        _vr = res.get("validationResult") or {}
        if _vr.get("errors"):
            print(f"WARNING add_keywords: validation errors ({len(clean)} keys): "
                  f"{str(_vr['errors'])[:240]}", file=__import__("sys").stderr)
        return res.get("addedItems") or []

    def _read_ads_agid_map(self, campaign_id: int) -> dict[str, int]:
        """Read-back adGroupId→adId для кампании после add_ads.

        Нужен когда Grid возвращает addedAds КОРОЧЕ входного списка: упавшие объявления
        пропускаются в ответе без null-заглушки (тот же класс сдвига, что X35↔X40 у групп,
        ревью 03.07 #5/#21) — картинки/цены/видео группы N уезжали на объявление группы N+1.
        → dict{adGroupId(str): adId(int)} или {} при ошибке (best-effort; у комбинаторных
        tp1/tp2 — одно объявление на группу)."""
        try:
            return self._read_ads_agid_map_strict(campaign_id)
        except Exception:  # noqa: BLE001
            return {}

    def _read_ads_agid_map_strict(self, campaign_id: int) -> dict[str, int]:
        """То же чтение adGroupId→adId, но БЕЗ проглатывания ошибки.

        Нужен сверке после потери ответа AddAdaptiveTextAds/AddShoppingAds: «{} при сбое чтения»
        и «в кампании реально 0 объявлений» — противоположные решения (не трогать vs безопасно
        повторить), а best-effort-версия их не различает."""
        q = ("query AdsAgid($login:String!,$inp:GdAdsContainerInput!){"
             "client(searchBy:{login:$login}){ads(input:$inp){rowset{id adGroupId}}}}")
        j = self._mutate("AdsAgid", q, {
            "login": self.login,
            "inp": {"filter": {"campaignIdIn": [str(campaign_id)]},
                    "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [],
                                         "useCampaignGoalIds": True},
                    "limitOffset": {"limit": 10000, "offset": 0},
                    "orderBy": [{"order": "ASC", "field": "ID"}]},
        })
        rows = (((j.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or []
        out: dict[str, int] = {}
        for row in rows:
            agid = str(row.get("adGroupId") or "")
            try:
                aid = int(row.get("id") or 0)
            except (TypeError, ValueError):
                aid = 0
            if agid and aid and agid not in out:   # первое объявление группы (комбинаторное)
                out[agid] = aid
        return out

    def _read_adgroup_name_to_id(self, campaign_id: int) -> dict[str, int]:
        """Read-back name→adGroupId для кампании после add_adgroups.

        Нужен когда Grid возвращает addedAdGroupItems КОРОЧЕ входного списка: упавшие группы
        просто пропускаются в ответе (без null-заглушки), и zip(use_groups, ag_ids) смещает
        маппинг — ключи группы N попадают в adGroupId группы N+1. Читаем актуальный name→id и
        перестраиваем список строго по именам. → dict{name: id} или {} при ошибке (best-effort)."""
        try:
            return self._read_adgroup_name_to_id_strict(campaign_id)
        except Exception:  # noqa: BLE001
            return {}

    def _read_adgroup_name_to_id_strict(self, campaign_id: int) -> dict[str, int]:
        """То же чтение, но БЕЗ проглатывания ошибки.

        Нужен сверке после потери ответа (`_add_adgroups_reconcile`): там «{} при сбое чтения» и
        «в кампании реально 0 групп» — противоположные решения (не трогать vs безопасно повторить),
        а best-effort-версия их не различает."""
        j = self._mutate("AdGroupNames", _ADGROUP_NAMES_Q, {
            "login": self.login,
            "inp": {"filter": {"campaignIdIn": [str(campaign_id)]},
                    "limitOffset": {"limit": 10000, "offset": 0}},
        })
        rows = (((j.get("data") or {}).get("client") or {}).get("adGroups") or {}).get("rowset") or []
        out: dict[str, int] = {}
        for row in rows:
            name = str(row.get("name") or "")
            try:
                gid = int(row.get("id") or 0)
            except (TypeError, ValueError):
                gid = 0
            if name and gid:
                out[name] = gid
        return out

    def add_ads(self, items: list[dict], *, save_draft: bool = True,
                campaign_id: int = 0) -> list[int | None]:
        """AddAdaptiveTextAds → список id объявлений (в порядке items; None для упавших).

        Ответ потерян → объявления МОГЛИ закоммититься. Слепого повтора нет (дубли объявлений),
        но и отказ дорог: в tp1 (`create_set_tp1_builders`) «0 ads» ведёт к `delete_campaigns` и
        потере позиции. При переданном ``campaign_id`` сверяем ФАКТ по adGroupId→adId
        (`_read_ads_agid_map_strict`) и досоздаём только реально отсутствующие.
        """
        if not items:
            return []
        if len(items) > _GRID_MUTATION_CHUNK:
            out: list[int | None] = []
            for i in range(0, len(items), _GRID_MUTATION_CHUNK):
                out.extend(self.add_ads(items[i:i + _GRID_MUTATION_CHUNK],
                                        save_draft=save_draft, campaign_id=campaign_id))
                time.sleep(0.15)
            return out
        try:
            return self._add_ads_once(items, save_draft=save_draft)
        except Exception as exc:  # noqa: BLE001 — не-потерянный ответ пробрасываем как раньше
            if not _response_lost(exc):
                raise
            return self._ads_reconcile(
                "AddAdaptiveTextAds", items, exc, campaign_id,
                lambda sub: self._add_ads_once(sub, save_draft=save_draft))

    def _add_ads_once(self, items: list[dict], *, save_draft: bool = True) -> list[int | None]:
        """Один AddAdaptiveTextAds → id объявлений в порядке items."""
        j = self._mutate("AddAdaptiveTextAds", _ADD_ADS_Q,
                         {"addInput": {"adAddItems": items, "saveDraft": bool(save_draft)}})
        res = (j.get("data") or {}).get("addAdaptiveTextAds") or {}
        vr = res.get("validationResult") or {}
        if vr.get("errors"):
            raise GridCreateError(f"AddAdaptiveTextAds validation: {str(vr['errors'])[:240]}")
        out: list[int | None] = []
        for a in (res.get("addedAds") or []):
            try:
                out.append(int(a["id"]))
            except (TypeError, ValueError, KeyError):
                out.append(None)
        return out

    def _ads_reconcile(self, op: str, items: list[dict], exc: BaseException,
                       campaign_id: int, create_fn) -> list[int | None]:
        """Ответ Add*Ads потерян: решаем по ФАКТУ (adGroupId→adId), а не повтором вслепую.

          * объявления всех отправленных групп уже в кабинете → НИЧЕГО не создаём;
          * ни одного → коммита не было → безопасный повтор;
          * часть → досоздаём ТОЛЬКО для групп без объявления.
        Сверить нельзя (нет campaign_id / пустые или повторяющиеся adGroupId / read-back не
        ответил) → исходная ошибка наружу. Посылка «одна группа = одно объявление» — та же, на
        которой держится `_aligned_ad_ids` (комбинаторные tp1/tp2 и товарка создаются 1:1).
        """
        cid = int(campaign_id or 0)
        agids = [str((it or {}).get("adGroupId") or "").strip() for it in items]
        if not cid or not all(agids) or len(set(agids)) != len(agids):
            raise exc
        live: dict[str, int] = {}
        read_ok = False
        for _ in range(2):                       # реплика Grid отстаёт от коммита (~2 с)
            time.sleep(_RECONCILE_SETTLE_SEC)
            try:
                live.update(self._read_ads_agid_map_strict(cid))
            except Exception as read_exc:  # noqa: BLE001 — сверка не удалась, решаем ниже
                print(f"[grid] {op}: сверка объявлений кампании {cid} не удалась: "
                      f"{str(read_exc)[:120]}", flush=True)
                continue
            read_ok = True
            if all(a in live for a in agids):
                break
        if not read_ok:                          # состояние неизвестно → вслепую не создаём
            raise exc
        missing = [i for i, a in enumerate(agids) if a not in live]
        if not missing:
            print(f"[grid] {op}: ответ потерян, но все {len(agids)} объявлений уже в кампании "
                  f"{cid} → повтор НЕ выполняем ({str(exc)[:120]})", flush=True)
            return [live.get(a) for a in agids]
        if len(missing) == len(agids):
            print(f"[grid] {op}: коммита не было (0 из {len(agids)} объявлений в кампании {cid}) "
                  f"→ безопасный повтор ({str(exc)[:120]})", flush=True)
            return create_fn(items)
        print(f"[grid] {op}: частичный коммит в кампании {cid} "
              f"({len(agids) - len(missing)} из {len(agids)}) → досоздаём {len(missing)}", flush=True)
        made = create_fn([items[i] for i in missing])
        out: list[int | None] = [live.get(a) for a in agids]
        for pos, idx in enumerate(missing):
            out[idx] = made[pos] if pos < len(made) else None
        if any(out[i] is None for i in missing):   # ответ Grid короче входа → добираем по группе
            live2 = self._read_ads_agid_map(cid) or {}
            for i in missing:
                if out[i] is None:
                    out[i] = live2.get(agids[i])
        return out

    def add_shopping_ads(self, items: list[dict], *, save_draft: bool = True,
                         campaign_id: int = 0) -> list[int | None]:
        """AddShoppingAds → список id товарных объявлений (Товарная галерея tp3/tp5). items —
        [{adGroupId, feedId, bodies, ...}] из build_shopping_ad. None для упавших.

        Ответ потерян → как и у add_ads: сверка факта по adGroupId→adId при переданном
        ``campaign_id`` (`_read_ads_agid_map_strict` видит и товарные объявления), иначе —
        исходная ошибка наружу без слепого повтора."""
        if not items:
            return []
        try:
            return self._add_shopping_ads_once(items, save_draft=save_draft)
        except Exception as exc:  # noqa: BLE001 — не-потерянный ответ пробрасываем как раньше
            if not _response_lost(exc):
                raise
            return self._ads_reconcile(
                "AddShoppingAds", items, exc, campaign_id,
                lambda sub: self._add_shopping_ads_once(sub, save_draft=save_draft))

    def _add_shopping_ads_once(self, items: list[dict], *,
                               save_draft: bool = True) -> list[int | None]:
        """Один AddShoppingAds (+ retry без feedId на FEED_NOT_EXIST) → id в порядке items."""
        j = self._mutate("AddShoppingAds", _ADD_SHOPPING_ADS_Q,
                         {"input": {"adAddItems": items, "saveDraft": bool(save_draft)}})
        res = (j.get("data") or {}).get("addShoppingAds") or {}
        vr = res.get("validationResult") or {}
        vr_errors = vr.get("errors") or []
        if vr_errors:
            # Фид в ERROR-состоянии: Директ возвращает FEED_NOT_EXIST в validationResult.
            # Retry без feedId — товарка без фида лучше, чем падение всей кампании.
            has_feed_error = any("FEED_NOT_EXIST" in str(e.get("code") or "") for e in vr_errors)
            if has_feed_error:
                retry_items = [{k: v for k, v in it.items() if k != "feedId"} for it in items]
                j2 = self._mutate("AddShoppingAds", _ADD_SHOPPING_ADS_Q,
                                  {"input": {"adAddItems": retry_items, "saveDraft": bool(save_draft)}})
                res2 = (j2.get("data") or {}).get("addShoppingAds") or {}
                vr2 = res2.get("validationResult") or {}
                if vr2.get("errors"):
                    raise GridCreateError(f"AddShoppingAds validation(no-feed retry): {str(vr2['errors'])[:240]}")
                vr_errors = []   # retry прошёл — сбрасываем ошибки, берём результат
                res = res2
            else:
                raise GridCreateError(f"AddShoppingAds validation: {str(vr_errors)[:240]}")
        out: list[int | None] = []
        for a in (res.get("addedAds") or []):
            try:
                out.append(int(a["id"]))
            except (TypeError, ValueError, KeyError):
                out.append(None)
        return out

    def delete_campaigns(self, campaign_ids: list) -> dict:
        """deleteCampaigns(input:{campaignIds:[…]}) — удаление кампаний ПО КУКЕ (без баллов v5).
        Работает для ЛЮБЫХ типов, включая ЕПК (GdUnifiedCampaign), невидимые/недоступные v5 при 152.
        → {"deleted": [ids], "errors": [...]}. Бросает GridCreateError на транспортной ошибке."""
        ids = [int(c) for c in (campaign_ids or []) if str(c).strip()]
        if not ids:
            return {"deleted": [], "errors": []}
        j = self._mutate("DeleteCampaigns", _DELETE_CAMPAIGNS_Q,
                         {"input": {"campaignIds": ids}})
        res = (j.get("data") or {}).get("deleteCampaigns") or {}
        vr = res.get("validationResult") or {}
        errs = list(vr.get("errors") or [])
        # Удалёнными считаем все, по которым нет ошибки валидации (Grid возвращает per-id ошибки в errors[].path).
        bad = set()
        for e in errs:
            for p in (e.get("path") or []):
                if str(p).isdigit():
                    bad.add(int(p))
        deleted = [i for idx, i in enumerate(ids) if idx not in bad]
        return {"deleted": deleted, "errors": errs}


# ── Операционные константы (используются GridCreateClient и оркестраторами) ──
_AC_GROUP_CAP = 150        # макс. групп на кампанию за проход (как в v501-пути)
_GRID_MUTATION_CHUNK = 50  # приватный Grid нестабилен на пачках ~150: режем bulk-мутации
_RECONCILE_SETTLE_SEC = 2.0  # пауза перед сверкой факта: реплика Grid отстаёт от коммита (~2с)
_CAMPAIGN_SCAN_MAX = 4000  # предохранитель пагинации при read-back кампаний по имени (20 страниц)
_KW_BUDGET = 9800          # консервативный лимит ключей/кампанию (Яндекс: 10 000)
_KW_MAX_PER_GROUP = 200    # верхний предохранитель на группу (Яндекс лимит API)


def unique_keyword_ids(added_items) -> int:
    """Сколько РАЗНЫХ ключевых фраз реально осело по ответу Grid ``addKeywords.addedItems``.

    Директ схлопывает фразы, которые считает одинаковыми, и возвращает на дубль ``keywordId``
    УЖЕ существующей фразы (живой зонд 2026-07-19, аккаунт porg-ozge4ntu: 10 отправленных →
    10 строк addedItems, но только 5 разных keywordId и ровно 5 фраз в кабинете; на v5-пути тот же
    дубль приходит с Warning 10140 «Ключевое слово уже существует» и Id базовой фразы).
    Схлопываются: порядок слов, регистр, лишние пробелы, оператор ``+``, словоформа. НЕ схлопываются:
    ``!``/кавычки/``[]``, минус-слово в конце фразы.
    Поэтому ``len(addedItems)`` = число ОТПРАВЛЕННЫХ, а не созданных → сверка build⇄кабинет давала
    стабильный ложный недобор (BUILD_LIVE_UNDERCOUNT + бессмысленный keywords_repair).

    Fail-safe: строки есть, но ни у одной нет ``keywordId`` (смена Grid-схемы) → откат на старый
    счётчик ``len(rows)``, чтобы не выдать ложный «0 из N создано»."""
    rows = [r for r in (added_items or []) if isinstance(r, dict)]
    seen = {str(r.get("keywordId")) for r in rows
            if str(r.get("keywordId") or "").strip() not in ("", "0")}
    if rows and not seen:
        return len(rows)
    return len(seen)


def _alloc_kw_caps(groups: list) -> list[int]:
    """Two-pass keyword budget allocation (Яндекс лимит: ≤10 000 ключей/кампанию, берём 9800).

    Pass-1: base_cap = min(200, 9800 // n) — равномерная «точка старта».
    Pass-2: остаток бюджета (от групп, не дошедших до base_cap) перераспределяется между
            «плотными» группами (len > base_cap), увеличивая их cap.
    Гарантирует sum(caps[i]) ≤ 9800 при любом наборе групп.
    Гарантирует caps[i] ≤ _KW_MAX_PER_GROUP (200) — жёсткий per-group потолок (Яндекс API лимит).

    Примеры:
      150 × 200 kw  → base_cap=65, sum=9800 (50 групп по 66, 100 по 65).
      149×3 + 1×3000 → base_cap=65, крупная получает min(3000, 65+extra), но ≤200.
      10 × любые   → base_cap=200, урезания нет.
      1 × 500 kw   → base_cap=200, остаток 9600 НЕ раздаётся (>200 запрещено API).
    """
    n = len(groups)
    if n == 0:
        return []
    base_cap = min(_KW_MAX_PER_GROUP, _KW_BUDGET // n)
    kw_counts = [len(g.get("keywords") or []) for g in groups]
    caps = [min(cnt, base_cap) for cnt in kw_counts]
    remainder = _KW_BUDGET - sum(caps)
    if remainder > 0:
        capped_idxs = [i for i, cnt in enumerate(kw_counts) if cnt > base_cap]
        if capped_idxs:
            extra = remainder // len(capped_idxs)
            leftover = remainder - extra * len(capped_idxs)
            for i in capped_idxs:
                # FIX-Q4: клип по _KW_MAX_PER_GROUP — API Яндекса не принимает >200 ключей/группу.
                # До фикса pass-2 мог давать cap=500 при n=1 (extra=9600, base_cap=200),
                # что приводило к keywords_repair post-create (лишние ~380с добивки, cid 712408001/712406901).
                caps[i] = min(kw_counts[i], min(base_cap + extra, _KW_MAX_PER_GROUP))
            for i in capped_idxs[:leftover]:
                caps[i] = min(kw_counts[i], min(caps[i] + 1, _KW_MAX_PER_GROUP))
    return caps


def _kw_canon_for_campaign(phrase: Any) -> str:
    """Conservative campaign-level duplicate key.

    Direct collapses duplicate keywords inside one campaign across adgroups. If a later
    group receives only duplicates, Grid may still return keywordIds while the group stays
    live-empty. Keep the normalization intentionally modest: lowercase, strip plus-operators,
    collapse whitespace and sort tokens.
    """
    words = [w for w in re.sub(r"(^|\s)\+", " ", str(phrase or "").lower()).split() if w]
    return " ".join(sorted(words))


def _drop_cross_group_duplicate_keywords(groups: list) -> tuple[list, int]:
    """Remove campaign-level duplicate keywords and skip groups left with no unique phrases."""
    seen: set[str] = set()
    kept: list = []
    dropped = 0
    for g in groups or []:
        ng = dict(g or {})
        kws = []
        for kw in ng.get("keywords") or []:
            key = _kw_canon_for_campaign(kw)
            if not key or key in seen:
                continue
            seen.add(key)
            kws.append(kw)
        if kws:
            ng["keywords"] = kws
            kept.append(ng)
        else:
            dropped += 1
    return kept, dropped


def create_full(login: str, *, campaign_spec: dict, groups: list, region_ids: list,
                href: str, goal_id: int = 0, autotargeting: bool = True,
                price_map: dict | None = None, brand_price_fn=None) -> dict:
    """Создать кампанию + группы (+ключи) + комбинаторные объявления (+adPrice) ПО КУКЕ за один вызов.

    campaign_spec — kwargs для build_unified_campaign (name/counter_id/goal_id/cpa/weekly_budget/...).
    groups — [{name, keywords:[...], minus:[...], titles:[...], texts:[...], image_hashes:[...],
              href, brand}]. price_map + brand_price_fn(price_map, brand) → (current, old) для adPrice.
    → {campaign_id, groups: N, ads: N, prices_set: N, errors: [...]}.
    """
    rep = {"campaign_id": None, "groups": 0, "ads": 0, "keywords": 0, "ad_ids": [],
           "adgroup_ids": [], "prices_set": 0, "errors": []}
    cl = GridCreateClient(login)
    cl._bootstrap_csrf()
    try:
        cid = cl.add_campaign(build_unified_campaign(href=href, **campaign_spec))
    except GridCreateError as e:
        rep["errors"].append(f"кампания(куки): {str(e)[:200]}")
        return rep
    rep["campaign_id"] = cid

    # Группы пачкой (AddUnifiedAdGroups принимает список) — adGroupId выровнен по порядку.
    search_only = bool(campaign_spec.get("search")) and not bool(campaign_spec.get("network"))
    raw_groups = list(groups or [])
    if search_only and not autotargeting:
        raw_groups, _dup_drop = _drop_cross_group_duplicate_keywords(raw_groups)
        if _dup_drop:
            rep["groups_skipped_duplicate_keywords"] = _dup_drop
    use_groups = raw_groups[:_AC_GROUP_CAP]
    if len(raw_groups) > _AC_GROUP_CAP:
        rep["deferred"] = len(raw_groups) - _AC_GROUP_CAP

    # Two-pass аллокация ключей: _alloc_kw_caps даёт per-group cap при суммарном бюджете ≤9800.
    # Плотные группы получают максимум оставшегося бюджета (баги #3/#9 code-review).
    kw_caps = _alloc_kw_caps(use_groups)

    at_profile = "search_tp2" if (search_only and autotargeting) else ""
    # Группы БЕЗ ключей в поле AddUnifiedAdGroups. Раньше keywords передавались и сюда, и в
    # отдельный AddKeywords ниже — Grid СОЗДАВАЛ их ДВАЖДЫ для групп <~140 ключей (точные
    # дубли фраз, каждая со своим keywordId). Единственный источник ключей — AddKeywords ниже
    # (проверен на всех объёмах, вкл. крупные группы, где AddUnifiedAdGroups keywords игнорирует).
    # Аудитории структуры (g["audiences"] — id условий, уже резолвнутые под целевой кабинет
    # в _tp1_pack_groups). search_only → поиск tp2/tp4 → searchRetargetings; иначе сеть tp1.
    g_items = [build_adgroup(campaign_id=cid, name=g.get("name") or "группа",
                             region_ids=region_ids, keywords=[],
                             minus_keywords=g.get("minus") or [], goal_id=goal_id,
                             autotargeting=autotargeting,
                             autotargeting_profile=at_profile,
                             retargeting_ids=g.get("audiences"),
                             retargeting_on_search=search_only) for g in use_groups]
    _aud_notes = _audience_notes(use_groups)
    if _aud_notes:
        rep.setdefault("warnings", []).extend(_aud_notes)
    try:
        # campaign_is_new: кампания создана строкой выше → групп в ней 0, снимок читать не надо.
        ag_ids = cl.add_adgroups(g_items, campaign_is_new=True)
    except GridCreateError as e:
        rep["errors"].append(f"группы(куки): {str(e)[:200]}")
        return rep
    rep["groups"] = sum(1 for x in ag_ids if x)
    rep["adgroup_ids"] = [int(x) if x else None for x in ag_ids]

    # Защита от позиционного сдвига: Grid пропускает упавшие группы в addedAdGroupItems
    # (не возвращает null-заглушку) → ag_ids короче use_groups → zip смещает маппинг.
    # При несовпадении длин делаем read-back name→id и выравниваем строго по имени группы.
    if len(ag_ids) != len(use_groups):
        _name_to_id = cl._read_adgroup_name_to_id(cid)
        if _name_to_id:
            ag_ids = [_name_to_id.get(g.get("name") or "") for g in use_groups]
        else:
            ag_ids = list(ag_ids) + [None] * (len(use_groups) - len(ag_ids))
            rep["errors"].append("позиционный сдвиг: read-back недоступен, ключи могут быть смещены")
        rep["adgroup_ids"] = [int(x) if x else None for x in ag_ids]
    else:
        time.sleep(0.6)
        _name_to_id = cl._read_adgroup_name_to_id(cid)
        if _name_to_id:
            ag_ids = [_name_to_id.get(g.get("name") or "") for g in use_groups]
            rep["groups"] = sum(1 for x in ag_ids if x)
            rep["adgroup_ids"] = [int(x) if x else None for x in ag_ids]
    _gate_groups_created(rep, len(g_items))

    # Ключи ЕДИНСТВЕННЫМ путём — отдельным AddKeywords (в build_adgroup keywords=[], иначе дубли).
    _kw_items = []
    for g, agid, cap in zip(use_groups, ag_ids, kw_caps):
        if not agid:
            continue
        for k in (g.get("keywords") or [])[:cap]:
            if str(k).strip() and not str(k).startswith("---"):
                _kw_items.append({"adGroupId": str(agid), "keyword": str(k)})
    if _kw_items:
        try:
            _sent_kw_agids = {str(it.get("adGroupId") or "") for it in _kw_items if it.get("adGroupId")}
            _added_kw_rows = cl.add_keywords(_kw_items)
            _added_kw_agids = {str(r.get("adGroupId") or "") for r in (_added_kw_rows or []) if r.get("adGroupId")}
            _missing_kw_agids = _sent_kw_agids - _added_kw_agids
            if _missing_kw_agids:
                time.sleep(0.6)
                _retry_items = [it for it in _kw_items if str(it.get("adGroupId") or "") in _missing_kw_agids]
                _retry_rows = cl.add_keywords(_retry_items) if _retry_items else []
                _added_kw_rows = list(_added_kw_rows or []) + list(_retry_rows or [])
                _added_kw_agids = {str(r.get("adGroupId") or "") for r in (_added_kw_rows or []) if r.get("adGroupId")}
                _missing_kw_agids = _sent_kw_agids - _added_kw_agids
            rep["keywords"] = unique_keyword_ids(_added_kw_rows)
            # #ФИКС-7: keyword-кампания задумана с ключами (_kw_items>0), но 0 создано →
            # add_keywords проглотил validationResult.errors и вернул []. Кампания без ключей
            # показываться не будет → выносим в errors (иначе ложный ok=True).
            if not rep["keywords"]:
                rep["errors"].append(
                    f"ключи(AddKeywords): 0 из {len(_kw_items)} создано (валидатор Grid отклонил)")
            if _missing_kw_agids:
                rep["errors"].append(
                    f"ключи(AddKeywords): {len(_missing_kw_agids)} групп без подтверждённых ключей")
        except Exception as e:  # noqa: BLE001 — группы созданы; но ключи теперь ЕДИНСТВЕННЫМ путём,
            # молчать нельзя: сбой = кампания без ключей (не будет показываться). Выносим в rep["errors"],
            # чтобы вызывающий (ЕПК-копир/feed/tp1/repair) увидел провал по стандартной проверке errors.
            rep["errors"].append(f"ключи(AddKeywords): {str(e)[:200]}")

    # Объявления пачкой + adPrice по бренду группы.
    ad_items, ad_brand = [], []
    for g, agid in zip(use_groups, ag_ids):
        if not agid:
            continue
        price = None
        if price_map and brand_price_fn:
            # seg группы ('Марки' → МИН цена по марке; иначе цена модели) — передаём третьим арг.
            # _group_ad_price принимает (prices, brand, seg); старый _ad_price_for_brand игнорил seg.
            try:
                cur, old = brand_price_fn(price_map, g.get("brand") or g.get("name") or "", g.get("seg") or "")
            except TypeError:                       # совместимость со старой 2-арг функцией
                cur, old = brand_price_fn(price_map, g.get("brand") or g.get("name") or "")
            if cur:
                # Старая цена только из фида (old_safe=0 → поле пустое, без зачёркнутой).
                old_safe = _safe_old_price(cur, old)
                price = {"price": str(cur), "priceOld": (str(old_safe) if old_safe else ""),
                         "prefix": "FROM", "currency": "RUB"}   # «от X» (HAR 29)
        # Common ct image safety is enforced before this point: callers may pass only
        # safe Manual/ct0000 hashes for common groups, never M3/feed/model hashes.
        image_hashes = g.get("image_hashes") or []
        ad_items.append(build_ad(adgroup_id=agid, href=g.get("href") or href,
                                 titles=g.get("titles") or [], bodies=g.get("texts") or [],
                                 image_hashes=image_hashes, ad_price=price))
        if price:
            rep["prices_set"] += 1
    try:
        a_ids = cl.add_ads(ad_items, campaign_id=cid)
        rep["ads"] = sum(1 for x in a_ids if x)
        # КОНТРАКТ (ревью 03.07 #5/#21): ad_ids СТРОГО 1:1 с ag_ids/группами, None для групп
        # без agid и упавших объявлений. Компакт-список смещал zip(ad_ids, groups) у потребителей
        # (картинки/цены/видео чужой группе — класс X35↔X40). При расхождении длин ответа Grid —
        # read-back adGroupId→adId.
        rep["ad_ids"] = _aligned_ad_ids(cl, cid, ad_items, a_ids, ag_ids)
    except GridCreateError as e:
        rep["errors"].append(f"объявления(куки): {str(e)[:200]}")
    return rep


def _aligned_ad_ids(cl, campaign_id: int, ad_items: list, a_ids: list, ag_ids: list) -> list:
    """ad_ids СТРОГО 1:1 с ag_ids (None для пропусков) — общий гард сдвига create_full /
    add_text_content_to_existing (ревью 03.07 #5/#21, класс X35↔X40).

    add_ads должен отдавать позиционный список, но Grid пропускает упавшие объявления в
    addedAds без null-заглушек → при len-расхождении восстанавливаем соответствие
    read-back'ом adGroupId→adId; иначе матчим по adGroupId отправленных items."""
    sent = [str(it.get("adGroupId") or "") for it in (ad_items or [])]
    ids = list(a_ids or [])
    if len(ids) != len(sent):
        ag2ad = cl._read_ads_agid_map(int(campaign_id or 0))
        ids = [ag2ad.get(s) for s in sent]
    by_agid = {s: i for s, i in zip(sent, ids) if s and i}
    return [by_agid.get(str(a)) if a else None for a in (ag_ids or [])]


def add_text_content_to_existing(login: str, *, campaign_id: int, groups: list,
                                 region_ids: list, href: str, goal_id: int = 0,
                                 autotargeting: bool = True, search_only: bool = False,
                                 price_map: dict | None = None, brand_price_fn=None) -> dict:
    """Добавить группы + adaptive text ads в УЖЕ существующую кампанию через Grid/cookie.

    Используется repair-gate для пустых tp2/tp4 черновиков: кампанию не пересоздаём и не тратим
    Direct API units, только добиваем missing content.
    """
    rep = {"campaign_id": int(campaign_id or 0), "groups": 0, "ads": 0, "keywords": 0,
           "ad_ids": [], "adgroup_ids": [], "prices_set": 0, "errors": []}
    cid = rep["campaign_id"]
    if not cid:
        rep["errors"].append("content-repair: нет campaign_id")
        return rep
    raw_groups = list(groups or [])
    if not raw_groups:
        rep["errors"].append("content-repair: нет групп для добавления")
        return rep

    cl = GridCreateClient(login)
    cl._bootstrap_csrf()
    at_profile = "search_tp2" if search_only else ""
    if search_only and not autotargeting:
        raw_groups, _dup_drop_tc = _drop_cross_group_duplicate_keywords(raw_groups)
        if _dup_drop_tc:
            rep["groups_skipped_duplicate_keywords"] = _dup_drop_tc
    use_groups = raw_groups[:_AC_GROUP_CAP]
    if not use_groups:
        rep["errors"].append("content-repair: нет групп для добавления")
        return rep
    if len(raw_groups) > _AC_GROUP_CAP:
        rep["deferred"] = len(raw_groups) - _AC_GROUP_CAP
    # Консервативный kw_cap: не знаем сколько ключей уже в кампании → аллоцируем только по
    # добавляемым группам (worst-case). Это та же формула что в create_full.
    kw_caps = _alloc_kw_caps(use_groups)
    # keywords=[] в группе: единственный источник ключей — AddKeywords ниже (иначе Grid создаёт
    # дубли для групп <~140 ключей — то же, что чинилось в create_full).
    g_items = [build_adgroup(campaign_id=cid, name=g.get("name") or "группа",
                             region_ids=region_ids, keywords=[],
                             minus_keywords=g.get("minus") or [], goal_id=goal_id,
                             autotargeting=autotargeting,
                             autotargeting_profile=at_profile) for g in use_groups]
    try:
        ag_ids = cl.add_adgroups(g_items)
    except GridCreateError as e:
        rep["errors"].append(f"группы(куки): {str(e)[:200]}")
        return rep
    rep["groups"] = sum(1 for x in ag_ids if x)
    rep["adgroup_ids"] = [int(x) if x else None for x in ag_ids]

    # Защита от позиционного сдвига (идентично create_full).
    if len(ag_ids) != len(use_groups):
        _name_to_id = cl._read_adgroup_name_to_id(cid)
        if _name_to_id:
            ag_ids = [_name_to_id.get(g.get("name") or "") for g in use_groups]
        else:
            ag_ids = list(ag_ids) + [None] * (len(use_groups) - len(ag_ids))
            rep["errors"].append("позиционный сдвиг: read-back недоступен, ключи могут быть смещены")
        rep["adgroup_ids"] = [int(x) if x else None for x in ag_ids]
    else:
        time.sleep(0.6)
        _name_to_id = cl._read_adgroup_name_to_id(cid)
        if _name_to_id:
            ag_ids = [_name_to_id.get(g.get("name") or "") for g in use_groups]
            rep["groups"] = sum(1 for x in ag_ids if x)
            rep["adgroup_ids"] = [int(x) if x else None for x in ag_ids]
    _gate_groups_created(rep, len(g_items))

    # Ключи ЕДИНСТВЕННЫМ путём — отдельным AddKeywords (в build_adgroup keywords=[], иначе дубли).
    _kw_items_tc = []
    for g, agid, cap in zip(use_groups, ag_ids, kw_caps):
        if not agid:
            continue
        for k in (g.get("keywords") or [])[:cap]:
            if str(k).strip() and not str(k).startswith("---"):
                _kw_items_tc.append({"adGroupId": str(agid), "keyword": str(k)})
    if _kw_items_tc:
        try:
            _sent_kw_agids_tc = {str(it.get("adGroupId") or "") for it in _kw_items_tc if it.get("adGroupId")}
            _added_kw_rows_tc = cl.add_keywords(_kw_items_tc)
            _added_kw_agids_tc = {str(r.get("adGroupId") or "") for r in (_added_kw_rows_tc or [])
                                  if r.get("adGroupId")}
            _missing_kw_agids_tc = _sent_kw_agids_tc - _added_kw_agids_tc
            if _missing_kw_agids_tc:
                time.sleep(0.6)
                _retry_items_tc = [it for it in _kw_items_tc
                                   if str(it.get("adGroupId") or "") in _missing_kw_agids_tc]
                _retry_rows_tc = cl.add_keywords(_retry_items_tc) if _retry_items_tc else []
                _added_kw_rows_tc = list(_added_kw_rows_tc or []) + list(_retry_rows_tc or [])
                _added_kw_agids_tc = {str(r.get("adGroupId") or "") for r in (_added_kw_rows_tc or [])
                                      if r.get("adGroupId")}
                _missing_kw_agids_tc = _sent_kw_agids_tc - _added_kw_agids_tc
            rep["keywords"] = unique_keyword_ids(_added_kw_rows_tc)
            # #ФИКС-7: задумано с ключами, но 0 создано (проглоченный validationResult.errors) →
            # кампания без ключей не показывается → выносим в errors (иначе ложный ok=True).
            if not rep["keywords"]:
                rep["errors"].append(
                    f"ключи(AddKeywords): 0 из {len(_kw_items_tc)} создано (валидатор Grid отклонил)")
            if _missing_kw_agids_tc:
                rep["errors"].append(
                    f"ключи(AddKeywords): {len(_missing_kw_agids_tc)} групп без подтверждённых ключей")
        except Exception as e:  # noqa: BLE001 — группы созданы; ключи теперь ЕДИНСТВЕННЫМ путём,
            # сбой = группы без ключей. Выносим в rep["errors"] (repair-gate проверяет not errors).
            rep["errors"].append(f"ключи(AddKeywords): {str(e)[:200]}")

    ad_items = []
    for g, agid in zip(use_groups, ag_ids):
        if not agid:
            continue
        price = None
        if price_map and brand_price_fn:
            try:
                cur, old = brand_price_fn(price_map, g.get("brand") or g.get("name") or "", g.get("seg") or "")
            except TypeError:
                cur, old = brand_price_fn(price_map, g.get("brand") or g.get("name") or "")
            if cur:
                old_safe = _safe_old_price(cur, old)
                price = {"price": str(cur), "priceOld": (str(old_safe) if old_safe else ""),
                         "prefix": "FROM", "currency": "RUB"}
        ad_items.append(build_ad(adgroup_id=agid, href=g.get("href") or href,
                                 titles=g.get("titles") or [], bodies=g.get("texts") or [],
                                 image_hashes=(g.get("image_hashes") or []), ad_price=price))
        if price:
            rep["prices_set"] += 1
    try:
        a_ids = cl.add_ads(ad_items, campaign_id=cid)
        rep["ads"] = sum(1 for x in a_ids if x)
        # тот же выровненный контракт 1:1, что в create_full (ревью 03.07 #5/#21)
        rep["ad_ids"] = _aligned_ad_ids(cl, cid, ad_items, a_ids, ag_ids)
    except GridCreateError as e:
        rep["errors"].append(f"объявления(куки): {str(e)[:200]}")
    return rep


def add_shopping_content_to_existing(login: str, *, campaign_id: int, groups: list,
                                     feed_id: int, region_ids: list, body_text: str = "",
                                     goal_id: int = 0) -> dict:
    """Добавить товарные группы + ShoppingAd/ListingAd в существующую tp3/tp5 кампанию.

    ``groups``: [{name, vendor?, model?, collection_id?, listing_name?}].
    Фильтры ShoppingAd ставятся через ``grid_finalize.GridClient.add_shopping_ads``:
    vendor/model для товаров, затем отдельный name-фильтр для ListingAd.
    """
    rep = {"campaign_id": int(campaign_id or 0), "groups": 0, "shopping_ads": 0,
           "listing_ads": 0, "adgroup_ids": [], "shopping_ad_ids": [],
           "listing_ad_ids": [], "listing_name_set": 0, "errors": []}
    cid = rep["campaign_id"]
    fid = int(feed_id or 0)
    use_groups = list(groups or [])[:_AC_GROUP_CAP]
    if not cid:
        rep["errors"].append("shopping-content-repair: нет campaign_id")
        return rep
    if not fid:
        rep["errors"].append("shopping-content-repair: нет feed_id")
        return rep
    if not use_groups:
        rep["errors"].append("shopping-content-repair: нет групп для добавления")
        return rep

    cl = GridCreateClient(login)
    cl._bootstrap_csrf()
    g_items = [build_adgroup(campaign_id=cid, name=g.get("name") or "Товарная галерея",
                             region_ids=region_ids, keywords=[], minus_keywords=[],
                             goal_id=goal_id, autotargeting=True) for g in use_groups]
    try:
        ag_ids = cl.add_adgroups(g_items)
    except GridCreateError as e:
        rep["errors"].append(f"группы(куки): {str(e)[:200]}")
        return rep
    rep["groups"] = sum(1 for x in ag_ids if x)
    rep["adgroup_ids"] = [int(x) if x else None for x in ag_ids]
    _gate_groups_created(rep, len(g_items))

    shop_items = []
    listing_names = []
    for g, agid in zip(use_groups, ag_ids):
        if not agid:
            continue
        shop_items.append({
            "adgroup_id": int(agid),
            "feed_id": fid,
            "vendor": g.get("vendor"),
            "model": g.get("model") or [],
            "collection_id": g.get("collection_id"),
        })
        listing_names.append(g.get("listing_name"))
    if not shop_items:
        rep["errors"].append("shopping-content-repair: группы не вернули adGroupId")
        return rep

    try:
        from . import grid_finalize as gf

        # A3: grid_create = чистый cookie-путь (кампания/группы созданы Grid'ом), token→Grid lag нет.
        grid = gf.get_grid_client(login, cookie_only=True)
        raw_shop_ids = grid.add_shopping_ads(shop_items) or []
        shop_ids = [int(x) for x in raw_shop_ids if x]
        rep["shopping_ad_ids"] = shop_ids
        rep["shopping_ads"] = len(shop_ids)
        if not shop_ids:
            rep["errors"].append("товарные объявления(куки): Grid вернул 0 ShoppingAd")
            return rep

        text = _trim_clean(str(body_text or ""), 81)
        if text:
            filters_by_ad_id = {}
            for sid, src in zip(shop_ids, shop_items):
                conds = []
                if src.get("vendor"):
                    vv = str(src["vendor"])
                    variants = list(dict.fromkeys([vv, vv.lower(), vv.title()]))
                    import json
                    conds.append({"field": "vendor", "operator": "CONTAINS_ANY",
                                  "stringValue": json.dumps(variants, ensure_ascii=False)})
                if src.get("model"):
                    mvals = src["model"] if isinstance(src["model"], list) else [str(src["model"])]
                    mvals = [str(x) for x in mvals if str(x).strip()]
                    if mvals:
                        import json
                        conds.append({"field": "model", "operator": "CONTAINS_ANY",
                                      "stringValue": json.dumps(mvals, ensure_ascii=False)})
                if not conds and src.get("collection_id"):
                    import json
                    conds.append({"field": "collectionId", "operator": "EQUALS_ANY",
                                  "stringValue": json.dumps([str(src["collection_id"])], ensure_ascii=False)})
                try:                                     # глобальные минус-марки (фид): производитель/модель НЕ содержит
                    from ..create import create_set_feeds as _csf
                    # per-feed поля: yandex.xml = mark_id/folder_id, YML = vendor/model — дефолт 'vendor'/'model'
                    # на AUTO_RU-фиде 'vendor'/'model' давал UNKNOWN_FIELD и фильтр не вставал (ревью 03.07)
                    _bf = _csf._resolve_feed_field(login, fid, "brand") or "vendor"
                    _mf = _csf._resolve_feed_field(login, fid, "model") or "model"
                    conds.extend(_csf._minus_marks_grid_conditions(brand_field=_bf, model_field=_mf))
                except Exception:  # noqa: BLE001
                    pass
                if conds:
                    filters_by_ad_id[int(sid)] = {"tab": "CONDITION", "conditions": conds}
            grid.set_default_text(shop_ids, fid, text, filters_by_ad_id=filters_by_ad_id)

        listing_rows = grid.add_listing_ads_by_shopping_ads(shop_ids) or []
        lf_items = []
        for row in listing_rows:
            lid = row.get("id") if isinstance(row, dict) else row
            said = row.get("shoppingAdId") if isinstance(row, dict) else None
            if lid:
                rep["listing_ad_ids"].append(int(lid))
            if lid and said:
                try:
                    idx = shop_ids.index(int(said))
                except (ValueError, TypeError):
                    idx = -1
                if idx >= 0 and idx < len(listing_names) and listing_names[idx]:
                    lf_items.append({"id": lid, "feed_id": fid, "value": listing_names[idx],
                                     "bodies": ([text] if text else [])})
        rep["listing_ads"] = len(rep["listing_ad_ids"])
        if lf_items:
            rep["listing_name_set"] = grid.set_listing_name_filters(lf_items)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"shopping/listing(куки): {str(e)[:200]}")
    return rep


# _safe_old_price, _dedup_keep, _fill_titles, _fill_bodies, build_ad, build_shopping_ad
# вынесены в grid_create_payloads и ре-экспортированы выше.


def create_shopping_full(login: str, *, campaign_spec: dict, group_names: list, feed_id: int,
                         region_ids: list, href: str, body_text: str = "", goal_id: int = 0) -> dict:
    """Создать Товарную галерею (tp3 РСЯ / tp5 Поиск) ПО КУКЕ: кампания (gallery+organic) + группы
    (автотаргет, без ключей) + товарные объявления (ShoppingAd по фиду). → {campaign_id, groups, ads, errors}."""
    rep = {"campaign_id": None, "groups": 0, "ads": 0, "errors": []}
    if not feed_id:
        rep["errors"].append("товарная галерея(куки): нет фида (feed_id)")
        return rep
    cl = GridCreateClient(login)
    cl._bootstrap_csrf()
    try:
        # organic (поисковая органика) валиден ТОЛЬКО с search (tp5). Для tp3 (РСЯ/network) organic
        # запрещён (ORGANIC_PLACEMENT_TYPES_INVALID_COMBINATION) — берём его из campaign_spec.
        cid = cl.add_campaign(build_unified_campaign(href=href, gallery=True, **campaign_spec))
    except GridCreateError as e:
        rep["errors"].append(f"кампания(куки): {str(e)[:200]}")
        return rep
    rep["campaign_id"] = cid
    names = (group_names or ["Товарная галерея"])[:_AC_GROUP_CAP]
    # Группы товарной галереи: автотаргет ВКЛ, БЕЗ ключей (товары из фида) — как в HAR17.
    # tp5 (search=True, network=False) → search_tp2 профиль (Целевые + без упоминания бренда),
    # tp3 (РСЯ, network=True) → "" (все категории + все бренды, без изменений).
    _gc_at_profile = "search_tp2" if (campaign_spec.get("search") and not campaign_spec.get("network")) else ""
    g_items = [build_adgroup(campaign_id=cid, name=(nm or "Товарная галерея"), region_ids=region_ids,
                             keywords=[], minus_keywords=[], goal_id=goal_id, autotargeting=True,
                             autotargeting_profile=_gc_at_profile)
               for nm in names]
    try:
        # campaign_is_new: кампания создана выше в этой же функции → групп в ней 0.
        ag_ids = cl.add_adgroups(g_items, campaign_is_new=True)
    except GridCreateError as e:
        rep["errors"].append(f"группы(куки): {str(e)[:200]}")
        return rep
    rep["groups"] = sum(1 for x in ag_ids if x)
    _gate_groups_created(rep, len(g_items))
    ad_items = [build_shopping_ad(adgroup_id=agid, feed_id=feed_id, body=body_text, login=login)
                for agid in ag_ids if agid]
    try:
        a_ids = cl.add_shopping_ads(ad_items, campaign_id=cid)
        rep["ads"] = sum(1 for x in a_ids if x)
        rep["shopping_ad_ids"] = [int(x) for x in a_ids if x]
    except GridCreateError as e:
        rep["errors"].append(f"товарные объявления(куки): {str(e)[:200]}")
    return rep
