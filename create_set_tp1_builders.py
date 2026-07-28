"""Create-set tp1/RSYA builders extracted from blueprint.py."""

from __future__ import annotations

from .text_norm import _trim_clean
from .link_check import resolve_or_fallback_url as _resolve_url, resolve_urls_batch as _resolve_urls_batch
from .model_urls import _is_degenerate_feed_url

import json
import os
import re
import threading
import time
from collections import Counter
from urllib.parse import urlsplit, urlunsplit

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by tp1 builders."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


def _site_root_href(href: str) -> str:
    """Root site URL for feed/model links even when campaign landing is /quiz."""
    raw = str(href or "").strip()
    try:
        p = urlsplit(raw)
        if p.scheme and p.netloc:
            return urlunsplit((p.scheme, p.netloc, "", "", ""))
    except Exception:  # noqa: BLE001
        pass
    return raw.rstrip("/")


def _pack_group_href(ct: str, brand: str, feed_urls: "dict | None", href: str, site_type: str) -> str:
    """Raw model_href для группы пака (до link_check resolve). Вызывается в pre-pass и основном цикле.

    feed_urls может быть None (tp1_pack_groups при feed_url_by_model=None) — в этом случае
    фид-URL не ищем и сразу строим формульный слаг.

    AD_HREF_ROOT_INSTEAD_OF_MODEL: вырожденный URL из фида (квиз-оффер `/quiz?fid=…`, голый
    корень) игнорируем — link_check.strip_quiz_url схлопнул бы его в главную. Обход
    `_multi and _uname` в call-sites спасал только multi-группы; гард закрывает и НЕ-multi.
    """
    site_href = _site_root_href(href)
    raw_feed = (_feed_url_for_model(feed_urls, brand, no_brand_fallback=(_ct_segment(ct) == "Модели"))
                if feed_urls else None)
    if raw_feed and not _is_degenerate_feed_url(raw_feed):
        return _brand_level_url(raw_feed) if _ct_segment(ct) == "Марки" else _strip_url_query(raw_feed)
    return _model_page_href(site_href, site_type, brand)


def _build_minus_mod_by_brand(pairs: list) -> dict:
    """Строит карту {mark_canon: set(model_lower_collapsed)} из пар (mark_canon, model_lower).
    Допущение: марки автодилеров односложные (BAIC/Chery/MG/Lada) — первый токен raw_brand = бренд.
    Точное равенство (не подстрока) предотвращает over-match для коротких моделей («5», «J7»)."""
    out: dict = {}
    for mark_c, model_l in (pairs or []):
        out.setdefault(mark_c, set()).add(model_l)
    return out


def _apply_global_feed_minus_for_site(site_type: str) -> bool:
    """Whether to add account-wide feed minus marks/models.

    For used-car sites these global rules remove offers that are valid used-car inventory, so
    builders keep only positive brand/model filters.
    """
    return (site_type or "").strip() != "С пробегом"


def _tp1_group_name(ct: str, r_code: str, brand: str, with_shopping: bool = False,
                    autotarget: bool = False, tp_code: str = "tp1") -> str:
    """Имя группы tp1/tp5 по CANON CODER.md (правило «кодер = реальный состав»).
    Суффикс _NN (было _11/_21/_22) убран по решению директолога (A2, 2026-06-22).

    with_shopping=False (TextAd only):  ct{N}_{aon/aoff}_n000_{r}_ct001_ag011_g00 — {Бренд}
    with_shopping=True, tp_code!="tp5"  (TextAd+ListingAd+ShoppingAd = «Т+Л+ТОВ», tp1/tp3):
                        ct{N}_aon_n000_{r}_ct010_ag011_g00 — {Бренд}
    with_shopping=True, tp_code=="tp5"  (TextAd+ListingAd+ShoppingAd = «Т+Л+ТОВ», как tp1/tp3):
                        ct{N}_aon_n000_{r}_ct010_ag011_g00 — {Бренд}
    Источник: справочник local_gsheet_naming (ag_part5): ct010 = «Комбинированный: ТГО + каталог/фид»,
    ct009 = «Товарное (Фид/каталог)» — БЕЗ TextAd; ag011 = с демо-корректировками TextAd, ag001 = без.
    Автотаргетинг для tp5+shopping: всегда aon и это ЕДИНСТВЕННОЕ корректное значение —
    в поисковой кампании Директа автотаргет выключить нельзя в принципе (подтверждено Семёном
    2026-07-27). Плановый autotarget-флаг для brand-групп tp5 означает «бренд-ключи вместо
    чистого автотаргетинга», а не «автотаргет выключен».
    """
    aud_code = "aon" if autotarget else "aoff"
    if with_shopping:
        if tp_code == "tp5":
            # tp5: TextAd+ShoppingAd+ListingAd (комбинированный «Т+Л+ТОВ»); aon — автотаргетинг
            # включён независимо от autotarget-флага (brand-группы имеют ключи + relevanceMatch).
            # (2026-07-07: убрана ветка ct009_ag001/без-TextAd — неверное промежуточное решение.)
            return f"{ct}_aon_n000_{r_code}_ct010_ag011_g00 — {brand}"
        return f"{ct}_{aud_code}_n000_{r_code}_ct010_ag011_g00 — {brand}"
    return f"{ct}_{aud_code}_n000_{r_code}_ct001_ag011_g00 — {brand}"

# Межкампанийный кэш загруженных видео-креативов: (login, ct) → [creative_id, ...].
# Без него КАЖДАЯ кампания набора заново качала ролики с M3 (sshfs, 5-9 МБ/шт) и заново
# грузила их в Яндекс — на Модели-кампании (150 групп × разные ct) фаза висла 15+ мин
# без прогресса → watchdog убивал джобу (2026-07-02, jobs 9126bf12fb3a/3b0804ef0497).
_VIDEO_CREATIVE_CACHE: dict = {}
# Тайм-бюджет видео-фазы на ОДНУ кампанию (сек): дольше — прекращаем скачивания/загрузки,
# attach'им что успели. Watchdog режет джобу после 15 мин тишины — фаза обязана быть короче.
_VIDEO_PHASE_BUDGET_SEC = 150
# Видео при СОЗДАНИИ выключено (решение Семёна 03.07.2026): каркас кампаний не ждёт медиа —
# медленные ответы Яндекса на /uac/content (таймауты 3×180с) растягивали item на 10+ минут.
# Видео добивается ПОСЛЕ создания спека-аудитом (VIDEO_MISSING → fix_video_missing, тот же
# _tp1_video_ads). Вернуть загрузку в создание — поставить True.
_VIDEO_AT_CREATE = False


def _tp1_video_ads(login: str, created_ad_meta: list, grid_cookie: str | None = None,
                   limit_per_group: int = 2, campaign_id: int = 0, slepok: str = "") -> dict:
    """Видео РСЯ (tp1 ЕПК) → creativeIds на КОМБИНАТОРНЫЕ объявления (Grid UpdateAdaptiveTextAds).

    Механика (HAR53 — тот же upload, что картинки tp6/tp7):
      1) видео ПО CT группы: ``kp.videos_for_ct(login, ct, slepok=…)`` — три ступени:
         СВОЙ слепок (``_slepki_data/<слепок>/videos``) → ОБЩИЙ per-ct пул M3
         ``/Users/Shared/agency/Video/<ct>/`` (``videos_pool_for_ct``) → ЧУЖОЙ слепок
         (последний фолбэк, правило Семёна 2026-07-28);
      2) ``UacClient.upload_video_creative`` (POST /web-api/uac/content?creative_type=tgo,
         multipart video/mp4) → ``result.meta.creative_id`` (НЕ content_id!);
      3) attach: Grid ``UpdateAdaptiveTextAds`` с ``creativeIds`` по реальному ad_id. Это
         full-replace, поэтому в payload шлём titles/bodies/imageHashes целиком (иначе затрёт).

    created_ad_meta: ``[{id, meta:{ct, href, titles, bodies, image_hashes, ...}}]``.
    Каждый ct-набор грузим ОДИН раз (кэш creative_id по ct), дедуп creative_id.
    Best-effort: сбой загрузки/attach НЕ роняет создание кампании.
    Порядок: вызывать ПОСЛЕ adPrice-фазы — иначе price-апдейт (creativeIds:[]) затрёт видео.
    → {video_groups, videos_uploaded, videos_attached, warnings}."""
    import os as _os
    import time as _time
    rep = {"video_groups": 0, "videos_uploaded": 0, "videos_attached": 0, "warnings": []}
    if not created_ad_meta:
        return rep
    _uc = {"c": None}
    _t0 = _time.monotonic()

    def _budget_left() -> bool:
        return (_time.monotonic() - _t0) < _VIDEO_PHASE_BUDGET_SEC

    def _uac():
        if _uc["c"] is None:
            _uc["c"] = cmc.UacClient(grid_cookie or cmc.pick_working_cookie(login), login)
        return _uc["c"]

    # Per-ct failed tracker (заменяет глобальный circuit-breaker 03.07.2026):
    # таймаут ОДНОГО ct больше не прерывает ВСЮ видео-фазу — только помечает ct как failed.
    # Другие ct продолжаются. Решение Семёна 2026-07-08.
    _failed_cts: set = set()

    def _creatives_for_ct(ct: str, brand_hint: str = "", site_type: str = "") -> list:
        ct = (ct or "").strip().lower()
        if not ct or ct == "ct0000":
            return []
        _ck = (login, ct, brand_hint, site_type, slepok)  # hint/site_type/slepok в ключе: пустые не блокируют точные
        if _ck in _VIDEO_CREATIVE_CACHE:                  # уже качали/грузили в этом процессе
            return _VIDEO_CREATIVE_CACHE[_ck]
        if ct in _failed_cts:                             # этот ct уже таймаутил — не повторяем
            return []
        if not _budget_left():                            # бюджет фазы исчерпан — новые ct не качаем
            return []
        cids: list = []
        _pool_read_ok = True
        try:
            # slepok: свой слепок → общий пул → чужой слепок (правило Семёна 2026-07-28).
            paths = kp.videos_for_ct(login, ct, limit=limit_per_group, brand_hint=brand_hint,
                                     slepok=slepok) or []
        except Exception as e:  # noqa: BLE001
            paths, _ = [], rep["warnings"].append(f"videos_for_ct {ct}: {str(e)[:80]}")
            _pool_read_ok = False   # транзиентный сбой чтения M3 ≠ «пул пуст» — не кэшировать
        if not paths:
            try:
                paths = (kp.read_videos(site_type, "tp1", ct)[:limit_per_group] if site_type else [])
            except Exception as e:  # noqa: BLE001
                rep["warnings"].append(f"read_videos tp1 {ct}: {str(e)[:80]}")
        for p in (paths or [])[:limit_per_group]:
            if not _budget_left() or ct in _failed_cts:
                break
            try:
                # видео-загрузка = прогресс: без heartbeat долгая легальная загрузка выглядела
                # «зависанием» и watchdog killed джобу (Павлов 13:46 03.07.2026)
                _hb = globals().get("_touch_running_jobs_heartbeat")
                if callable(_hb):
                    _hb()
                cid = _uac().upload_video_creative(p)
                if cid and cid not in cids:
                    cids.append(cid)
                    rep["videos_uploaded"] += 1
            except Exception as e:  # noqa: BLE001
                rep["warnings"].append(f"upload {_os.path.basename(str(p))}: {str(e)[:80]}")
                if "timeout" in str(e).lower():
                    # Per-ct failed (решение Семёна 2026-07-08, заменяет глобальный breaker 03.07):
                    # только ЭТОТ ct помечается failed — остальные ct продолжают обработку.
                    _failed_cts.add(ct)
                    rep["warnings"].append(
                        f"upload timeout для {ct} — ct помечен failed, другие ct продолжают")
        # Кэшируем ТОЛЬКО достоверный результат: есть креативы, либо пул ЛЕГИТИМНО пуст
        # (чтение M3 прошло и вернуло 0 путей). Пустоту от failed ct / сбоя чтения /
        # проваленных аплоадов НЕ кэшируем — иначе fix_video_missing в том же процессе
        # навсегда получал бы [] и «все видео в итоге загружены» не сходилось (ревью 03.07).
        if cids or (_pool_read_ok and not paths and ct not in _failed_cts):
            _VIDEO_CREATIVE_CACHE[_ck] = cids
        return cids

    attach_items, seen = [], set()
    for _rec in created_ad_meta:
        meta = _rec.get("meta") or {}
        cids = _creatives_for_ct(meta.get("ct") or "", brand_hint=meta.get("brand") or "",
                                 site_type=meta.get("site_type") or "")
        if not cids:
            continue
        ad_id = _rec.get("id")
        if not ad_id or ad_id in seen:
            continue
        seen.add(ad_id)
        attach_items.append({
            "id": ad_id, "href": meta.get("href") or "",
            "titles": meta.get("titles") or [], "bodies": meta.get("bodies") or [],
            **({"image_hashes": meta.get("image_hashes")} if meta.get("image_hashes") else {}),
            "creative_ids": list(cids[:limit_per_group]),
            "adPrice": meta.get("ad_price_payload"),  # fix price-C (2026-07-02): нести adPrice чтобы
            # Grid full-replace не затирал bannerPrice (verified live: без этого ключа цена = null)
        })
    rep["video_groups"] = len(attach_items)
    if attach_items:
        try:
            rep["videos_attached"] = _grid_update_adaptive_ads(
                login, attach_items,
                campaign_ids=[campaign_id] if campaign_id else None)
        except Exception as e:  # noqa: BLE001
            rep["warnings"].append(f"attach: {str(e)[:100]}")
    return rep

def _synthesize_tp1_build_error(rep: dict, tp_code: str, *,
                                autotarget: bool = False, keep_keywords: bool = False) -> None:
    """Синтез singular rep["error"] из rep["errors"] (plural) для структурных дефектов.

    Зачем: вызывающий код (create_set_tp1_builders ~:1481, create_set_feed_builders ~:838)
    гейтит вердикт позиции через rep["error"] (singular), но ДВЕНАДЦАТЬ мест пишут в
    rep["errors"] (plural) — без синтеза эти дефекты не влияют на вердикт и позиция
    уходит как ok=True.

    Фатальные (→ синтез singular error):
      tp5 (TEXT_CAMPAIGN / поиск): ключевые слова не добавлены при созданных группах
      И в errors есть keyword-ошибка (строка 470: "keywords(Grid AddKeywords tp5)").
      Без ключей в поисковой кампании нет трафика → позиция нефункциональна.

    Намеренно информационные (остаются в errors, НЕ синтезируют singular error):
      - "позиционный сдвиг" (line ~312): кампания работает, порядок групп shifted
      - shopping / all_feeds ошибки: TextAd-группы работают
      - частичные сбои групп/объявлений (adgroups > 0, ads > 0): позиция частично ok
      - tp1 RSY ключевые слова: РСЯ использует контекстный таргетинг без явных ключей
      - чистый автотаргет (autotarget=True, keep_keywords=False): keywords=0 штатно,
        таргетинг = relevanceMatch search_tp2 из Фазы 1 — синтез не нужен.

    Проверка not rep.get("error"): singular error мог быть выставлен раньше по ходу сборки —
    не затираем чужую причину.
    """
    if (tp_code == "tp5"
            and rep.get("adgroups")
            and not rep.get("keywords")
            and not rep.get("error")):
        # Чистый автотаргет: keywords=0 штатно (relevanceMatch без явных ключей).
        if autotarget and not keep_keywords:
            return
        _kw_errs = [e for e in rep.get("errors", [])
                    if "keyword" in e.lower() or "ключ" in e.lower() or "kw" in e.lower()]
        if _kw_errs:
            rep["error"] = ("tp5 ключи не созданы при наличии групп: "
                            + "; ".join(str(e) for e in _kw_errs[:2]))[:240]


def _build_tp1_adgroups(
    token: str,
    login: str,
    campaign_id: int,
    region_ids: list,
    href: str,
    groups: list,
    sitelink_set_id: int | None = None,
    feed_id: int = 0,
    with_shopping: bool = False,
    feed_models: dict | None = None,
    autotarget: bool = False,
    keep_keywords: bool = False,
    products_only: bool = False,
    grid_cookie: str | None = None,
    base_sitelinks: list | None = None,
    tp_code: str = "tp1",
    all_feeds_list: list | None = None,
    site_type: str = "",
    campaign_is_new: bool = False,
) -> dict:
    """Наполнить РСЯ (tp1 ЕПК) группами БАТЧЕМ через v501:
    adgroups.add (с TrackingParams и minus) → keywords.add → adimages.add → ads.add(TextAd+Image).

    feed_id + with_shopping (как в слепках Щербаковой): дополнительно к TextAd в КАЖДУЮ группу
    добавляем ListingAd (динамика) + ShoppingAd (товарное) по фиду — состав «Т+Л+ТОВ» в группе.
    Аддитивно: нет feed_id / with_shopping=False → создаём только TextAd (старое поведение).

    groups: [{name, ct, brand, keywords:[], minus:[], title, text, image_path?, callout_ext_ids?}].
    Анти-блок: операции батчами с паузами, групп ≤ _AC_GROUP_CAP за проход.
    Лимиты: ключей ≤200/группу, минус ≤100/группу, Title ≤35, Text ≤81.

    campaign_is_new: кампания создана вызывающим ШАГОМ ВЫШЕ и ещё пуста → Фаза 1 не читает
    предмутационный снимок имён групп (лишний Grid-запрос в горячем пути; при сбое чтения снимок
    равен None и сверка потерянного ответа AddUnifiedAdGroups отключилась бы совсем). Дефолт False
    оставлен намеренно: для непустой кампании снимок обязателен (коллизии имён групп).
    → {adgroups, keywords, ads, images_uploaded, sitelinks_set, errors, deferred}."""
    rep = {"adgroups": 0, "keywords": 0, "ads": 0, "images_uploaded": 0,
           "sitelinks_set": sitelink_set_id or 0, "errors": [], "deferred": 0}
    _apply_feed_minus = _apply_global_feed_minus_for_site(site_type)
    rids = [int(r) for r in (region_ids or []) if str(r).lstrip("-").isdigit()] or [225]
    if len(groups) > _AC_GROUP_CAP:
        rep["deferred"] = len(groups) - _AC_GROUP_CAP
        groups = groups[:_AC_GROUP_CAP]

    # ── Фаза 1: adgroups — АТОМАРНОЕ создание через Grid AddUnifiedAdGroups ──────────────────
    # relevanceMatch (автотаргет) выставляется ПРИ СОЗДАНИИ группы — единственный путь, который
    # держится живым фактом. v501 adgroups.add не умеет relevanceMatch: свежая группа получает
    # дефолт Директа, а пост-патч Grid UpdateUnifiedAdGroups по таким группам оказался ПОЛНЫМ
    # no-op (2026-07-27: кампании 713089308 «КС + Автотаргетинг» и 713089104 «КС» получили
    # ПРОТИВОПОЛОЖНЫЕ isActive, а живая картина вышла идентичной — 84 ON / 29 SUSPENDED в обеих).
    # Детект по живому аккаунту: 433 группы Grid-путём (tp2/tp4/tp5) → 0 дефектов; все 600
    # дефектов — в 14 tp1-кампаниях, шедших v501-путём. Фаза 1.5 удалена как неработающая.
    # Профиль relevanceMatch (grid_create_payloads.build_adgroup:107-129):
    #   tp5 (Поиск + галерея) → search_tp2: EXACT_V2_MARK + WITHOUT_BRAND, isActive=True ВСЕГДА.
    #     В поисковой кампании Директа автотаргет выключить нельзя в принципе (подтверждено
    #     Семёном 2026-07-27), поэтому плановый autotarget-флаг профиль НЕ отменяет: он значит
    #     лишь «бренд-ключи вместо чистого автотаргетинга» и управляет только Фазой 2 (ключи).
    #     Живое `isActive=True` у tp5-групп планового `aoff` — норма, а не дефект.
    #   tp1 (РСЯ)             → дефолтная ветка: все 5 категорий + 3 бренда при autotarget=True,
    #                           isActive=False с пустыми списками при autotarget=False.
    _gcl = gc.GridCreateClient(login, cookie=grid_cookie)
    _is_tp5 = (tp_code == "tp5")
    _g_items = [gc.build_adgroup(
        campaign_id=int(campaign_id),
        name=(g.get("name") or "группа")[:255],
        region_ids=rids,
        keywords=[],           # ключи — ТОЛЬКО через Фазу 2 (AddKeywords), без дублей
        minus_keywords=[],     # групповой минус не ставим: РСЯ режет охват, поиск — на кампании
        autotargeting=(True if _is_tp5 else bool(autotarget)),
        autotargeting_profile=("search_tp2" if _is_tp5 else ""),
    ) for g in groups]
    try:
        # campaign_is_new=True (кампания создана шагом выше и пуста) → снимок имён не читаем.
        # Фазе 4a ниже этот флаг НЕ передаём: там в кампании уже есть группы Фазы 1, и снимок —
        # единственная защита от совпадения имени «Товарная галерея · <фид>» с существующей группой.
        ag_ids = _gcl.add_adgroups(_g_items, campaign_is_new=bool(campaign_is_new))
        # Позиционный сдвиг: Grid пропускает упавшие группы (не возвращает null-заглушку)
        # → список может быть короче входного → выравниваем по имени (аналог create_full:615).
        if len(ag_ids) != len(groups):
            _n2id = _gcl._read_adgroup_name_to_id(int(campaign_id))
            if _n2id:
                ag_ids = [_n2id.get(g.get("name") or "") for g in groups]
            else:
                ag_ids = list(ag_ids) + [None] * (len(groups) - len(ag_ids))
                rep["errors"].append(
                    f"{tp_code} Grid: позиционный сдвиг групп — ключи могут быть смещены")
        rep["adgroups"] = sum(1 for x in ag_ids if x)
        rep["relevance_match_set"] = rep["adgroups"]  # атомарно при создании
    except gc.GridCreateError as _ge:
        # Группы не созданы → rep без adgroups → вызывающий (:1433) вызовет _cleanup_partial
        # и снесёт черновик. ok=True с неверным автотаргетом невозможен.
        rep["errors"].append(f"adgroups(Grid {tp_code}): {str(_ge)[:200]}")
        return rep

    # ── Фаза 2: keywords — ЕДИНЫЙ Grid-транспорт (AddKeywords тем же _gcl из Фазы 1) ─────────
    # Смешанный транспорт (Grid-группы + v5 keywords.add) даёт лаг репликации Grid→v5 →
    # ключи-фантомы, LIVE=0 (DMP_TP2_KEYWORDS_LOST_MIXED_TRANSPORT, ERRORS_JOURNAL ~3127).
    # Тот же паттерн уже закрыт для tp2/tp4 в create_set_text_builders.py:155-194.
    # Режимы ключей (relevanceMatch.isActive выставлен атомарно в Фазе 1, ключами НЕ управляется):
    #   • autotarget=True,  keep_keywords=False → ключей нет, таргетинг = relevanceMatch.
    #   • autotarget=False                      → реальные ключи (чистый КС).
    #   • autotarget=True,  keep_keywords=True  → реальные ключи (КС + автотаргет).
    # Псевдоключ "---autotargeting" не шлём: автотаргет живёт в relevanceMatch, а
    # gc.GridCreateClient.add_keywords сам режет фразы, начинающиеся с "---".
    kw_items: list = []
    _kw_raw_total = 0     # фраз пришло на вход очистки (только по группам, ЗАДУМАННЫМ с ключами)
    _kw_raw_groups = 0    # групп, задуманных с реальными ключами
    for i, g in enumerate(groups):
        if not ag_ids[i]:
            continue
        if autotarget and not keep_keywords:
            continue      # чистый автотаргет: пустой список ключей — норма by design, не дефект
        _raw = g.get("keywords") or []
        _kw_raw_groups += 1
        _kw_raw_total += len(_raw)
        for k in _kw_clean(_raw, 200):
            kw_items.append({"adGroupId": str(ag_ids[i]), "keyword": k})
    if not kw_items and _kw_raw_total:
        # #ФИКС-7b (2026-07-28): фразы на входе БЫЛИ, но очистка съела все до одной →
        # kw_items пуст → блок ниже (и его гейт «0 из N создано») недостижим, позиция уходила
        # ok=True с пустыми группами и полным молчанием (боевой случай: `_kw_clean` считал
        # минус-слова в лимит 7 слов). Автотаргет-групп это не касается — они отсеяны `continue`.
        rep["errors"].append(
            f"ключи({tp_code}): все {_kw_raw_total} фраз отсеяны очисткой на "
            f"{_kw_raw_groups} группах — группы созданы без ключей")
    if kw_items:
        try:
            # unique_keyword_ids считает РАЗНЫХ keywordId (Директ схлопывает дубли,
            # как на v5-пути; len(addedItems) давал BUILD_LIVE_UNDERCOUNT — porg-ozge4ntu).
            rep["keywords"] = gc.unique_keyword_ids(_gcl.add_keywords(kw_items))
            # #ФИКС-7 (тот же гейт, что на родном Grid-пути grid_create.py:591-599): группы
            # ЗАДУМАНЫ с ключами (kw_items>0), но создано 0 → add_keywords проглотил
            # validationResult.errors и вернул [] (grid_create.py:246-254 печатает в stderr и
            # НАМЕРЕННО не бросает). Без этой записи rep["errors"] оставался пустым → позиция
            # уходила ok=True с нулём ключей, а ловил её только пост-фактум NO_KEYWORDS_LIVE.
            if not rep["keywords"]:
                rep["errors"].append(
                    f"ключи(AddKeywords {tp_code}): 0 из {len(kw_items)} создано "
                    f"(валидатор Grid отклонил)")
        except Exception as _kwe:  # noqa: BLE001 — ключи единственный путь; сбой = группы без ключей
            rep["errors"].append(f"keywords(Grid AddKeywords {tp_code}): {str(_kwe)[:200]}")

    # ── Фаза 3: ads.add без предварительной token-загрузки картинок ──────────
    # Живой баг 2026-06-28: v501 upload_image на больших tp1 мог зависать до стадии ads.add.
    # Поэтому здесь не тратим время на upload_image вообще: ResponsiveAd создаём сразу,
    # а картинки добиваем post-create через Grid/куки по фактическим ad_id.

    # products_only (Смарт-Баннер/Фиды «без ТГО»): пропускаем КОМБИНАТОРНОЕ, оставляем только товарные (Фаза 4).
    # КОМБИНАТОРНОЕ (ResponsiveAd) через v501 — замена ТГО (отключают с 30.06): несколько заголовков/текстов
    # + картинки (AdImageHashes) + быстрые ссылки (SitelinkSetId). Уточнения наследуются (AdExtensions нет).
    _sl_set_cache: dict = {}   # ad_href → sitelink_set_id (per-group кэш, #ФИКС-3)
    _base_href = (href or "").rstrip("/")
    # ── Пре-пасс быстрых ссылок: N наборов ОДНИМ запросом vs N запросов ──────────────────────
    # Цикл ниже создавал набор НА КАЖДУЮ группу с собственным deep-link: 774 вызова
    # v501 sitelinks.add за прогон = 444 с (17% wall-clock). Здесь собираем уникальные
    # ad_href до цикла и заливаем их батчем `_get_or_reuse_sitelink_sets` (1 запрос на
    # кампанию; наборы, уже созданные на этом login с тем же СОДЕРЖИМЫМ, отдаются из
    # процессного кэша вообще без запроса). Одинаковые наборы → один id, разные → свои id
    # (ключ кэша = сигнатура содержимого, `automation_runtime._sitelink_set_sig`).
    # В цикле остаётся поштучный фолбэк — на href, который батч не покрыл (None/сбой).
    if base_sitelinks and not products_only:
        _sl_hrefs: list = []
        for i, g in enumerate(groups):
            if not ag_ids[i]:
                continue
            _ah = g.get("href") or href
            if _ah and _ah.rstrip("/") != _base_href and _ah not in _sl_hrefs:
                _sl_hrefs.append(_ah)
        if _sl_hrefs:
            try:
                _sl_ids = _get_or_reuse_sitelink_sets(
                    token, login,
                    [[{**s, "Href": _ah} for s in base_sitelinks] for _ah in _sl_hrefs])
            except Exception:  # noqa: BLE001 — батч best-effort, цикл ниже добьёт поштучно
                _sl_ids = []
            for _ah, _sid in zip(_sl_hrefs, _sl_ids or []):
                if _sid:
                    _sl_set_cache[_ah] = _sid
    ad_items = []
    ad_meta = []   # параллельно ad_items: {brand,href,titles,bodies,image_hashes} — для adPrice из фида
    for i, g in enumerate(groups):
        if products_only:
            break
        if not ag_ids[i]:
            continue
        ad_href = g.get("href") or href   # per-group deep-link приоритетнее общего href кампании
        img_paths = g.get("image_paths") or ([g.get("image_path")] if g.get("image_path") else [])
        ra = _responsive_ad(g.get("titles") or [g.get("title"), g.get("brand"), g.get("name")],
                            g.get("texts") or [g.get("text")], ad_href,
                            image_hashes=None,
                            # site_type нужен финальной сборке: `_upgrade_credit_*` подставляет
                            # хардкод-варианты про «новые авто» уже ПОСЛЕ всех `_cf`.
                            site_type=site_type or g.get("site_type") or "")
        if not ra:
            rep["errors"].append(f"{g.get('name', '?')}: пропущено объявление (нет заголовков/текстов/href)")
            continue
        # Per-group sitelink set (#ФИКС-3): href группы ≠ href кампании → создать/закэшировать набор
        _use_sl_id = sitelink_set_id
        if base_sitelinks and ad_href and ad_href.rstrip("/") != _base_href:
            if ad_href not in _sl_set_cache:
                try:
                    _grp_sls = [{**s, "Href": ad_href} for s in base_sitelinks]
                    _sl_set_cache[ad_href] = _get_or_reuse_sitelink_set(token, login, _grp_sls)
                except Exception:  # noqa: BLE001
                    _sl_set_cache[ad_href] = None
            _use_sl_id = _sl_set_cache.get(ad_href) or sitelink_set_id
        if _use_sl_id:
            ra["SitelinkSetId"] = _use_sl_id
        ad_items.append({"AdGroupId": int(ag_ids[i]), "ResponsiveAd": ra})
        ad_meta.append({"brand": g.get("brand") or g.get("name") or "", "href": ad_href,
                        "seg": _ct_segment(g.get("ct") or ""),   # 'Марки' → цена = МИН по марке
                        "ct": g.get("ct") or "",                 # для видео РСЯ (creativeIds по ct)
                        "site_type": site_type or g.get("site_type") or "",
                        "titles": ra.get("Titles") or [], "bodies": ra.get("Texts") or [],
                        "image_hashes": [],
                        "image_paths": img_paths[:5]})

    created_ad_meta = []   # [{id, meta}] созданных — для image backfill + adPrice
    repair_items: list[dict] = []
    _base = 0
    for chunk in _chunks(ad_items, _AC_CHUNK_AD):
        jd = _v501_svc("ads", "add", token, login, {"Ads": chunk})
        if "error" not in jd:
            for k, r in enumerate((jd.get("result") or {}).get("AddResults", [])):
                if r.get("Id"):
                    rep["ads"] += 1
                    gi = _base + k
                    if gi < len(ad_meta):
                        created_ad_meta.append({"id": int(r["Id"]), "meta": ad_meta[gi]})
                for e in (r.get("Errors") or []):
                    rep["errors"].append(f"ResponsiveAd(tp1): {e.get('Message')} {e.get('Details','')}".strip())
        else:
            rep["errors"].append(f"ads.add(tp1 ResponsiveAd) {_v5_err(jd)}")
        _base += len(chunk)
        time.sleep(_AC_BATCH_SLEEP)

    # ── Фаза 3.4: post-create repair через Grid/куки для token-path ──────────────────────────
    # Даже если v501 ads.add прошло, live payload в Директе может урезаться. После создания
    # всегда добиваем titles + bodies + imageHashes через Grid. Это основной fallback:
    # если token-path недогрузил объявление, куки-path дособирает именно его, а не оставляет брак.
    if created_ad_meta:
        try:
            import os as _os3
            _gc_img = gf.get_grid_client(login, cookie=grid_cookie)
            # ── Параллельная заливка картинок ──────────────────────────────────────────
            # Собираем все уникальные пути ПЕРЕД циклом, заливаем 8 потоками.
            # tp5 — только тексты, картинки у ShoppingAd/ListingAd → пропускаем.
            _uploaded_by_name: dict[str, str] = {}
            if tp_code != "tp5":
                _all_img_paths = [_pth for _rec in created_ad_meta
                                  for _pth in (_rec["meta"].get("image_paths") or [])]
                _uploaded_by_name = _parallel_upload_images(_gc_img, login, _all_img_paths)
                # Счётчик реально залитых картинок: раньше оставался 0 даже в успешных кампаниях
                # и подставлялся в текст ошибки «tp1 не дозаполнена» → уводил диагностику.
                rep["images_uploaded"] = len(_uploaded_by_name)
            _upd_items = []
            for _rec in created_ad_meta:
                _meta = _rec["meta"]
                _hashes = list(dict.fromkeys(_meta.get("image_hashes") or []))
                # tp5 комбинаторные — только тексты; картинки/видео остаются у ShoppingAd/ListingAd.
                if tp_code != "tp5":
                    for _pth in (_meta.get("image_paths") or []):
                        if len(_hashes) >= 5:
                            break
                        if not _pth or not _os3.path.isfile(_pth):
                            continue
                        _bn = _os3.path.basename(_pth)
                        _h = _uploaded_by_name.get(_bn)
                        if _h and _h not in _hashes:
                            _hashes.append(_h)
                    if _hashes:
                        _meta["image_hashes"] = _hashes[:5]
                _upd = {"id": _rec["id"], "href": _meta["href"],
                        "titles": _meta["titles"], "bodies": _meta["bodies"]}
                if tp_code != "tp5" and _meta.get("image_hashes") is not None:
                    _upd["image_hashes"] = _meta.get("image_hashes") or []
                _upd_items.append(_upd)
            if _upd_items:
                repair_items = list(_upd_items)
                rep["ads_repaired"] = _grid_update_adaptive_ads(
                    login, _upd_items, campaign_ids=[campaign_id] if campaign_id else None)
                # Считаем группы С КАРТИНКАМИ (как в остальных путях: :2252, text/feed builders),
                # а не все item'ы — иначе отчёт показывал 27/27 при реальных 25.
                rep["image_groups"] = sum(1 for _it in _upd_items if _it.get("image_hashes"))
        except Exception as _e:  # noqa: BLE001
            rep.setdefault("warnings", []).append(f"tp1 repair: {str(_e)[:100]}")

    # ── Фаза 3.5: ЦЕНА из фида в комбинаторное объявление (#2) — Grid по куке (без баллов).
    # adPrice = {current, old} самого дешёвого оффера фида по бренду/модели группы («от X · зачёркнуто old»).
    # FIX price-A (2026-07-02): _account_offer_prices (30 фидов, мердж) вместо одного defaultFeedId —
    # у porg-ozge4ntu defaultFeed имел 0 офферов в момент создания, поэтому цены не ставились.
    # FIX price-B: _grid_set_ad_prices уже несёт imageHashes → ads_repaired_after_price затирал только цену;
    # убран. FIX price-C: сохраняем ad_price_payload в meta → _tp1_video_ads несёт adPrice в attach.
    try:
        _pmap = _account_offer_prices(login, href)     # multi-feed мердж (fix price-A)
        if _pmap and created_ad_meta:
            _pitems = []
            for _rec in created_ad_meta:
                ad_id, meta = _rec["id"], _rec["meta"]
                cur, old = _group_ad_price(_pmap, meta.get("brand", ""), meta.get("seg", ""))
                # ПРАВИЛО СЕМЁНА 2026-07-05: цены из слепков НЕ берём НИКОГДА. cur=0
                # (модели нет в фиде) → item ВСЁ РАВНО шлём: _grid_set_ad_prices отправит
                # его БЕЗ adPrice → full-replace ЗАТИРАЕТ донорскую цену слепка (2 040 546
                # у BAIC X7 уцелела именно из-за старого пропуска `if cur:`).
                meta["ad_price_payload"] = _grid_ad_price_payload(cur, old) if cur else None
                _pitems.append({"id": ad_id, "href": meta["href"], "titles": meta["titles"],
                                "bodies": meta["bodies"], "image_hashes": meta["image_hashes"],
                                "current": cur, "old": old})
            rep["prices_set"] = _grid_set_ad_prices(login, _pitems)
            # ads_repaired_after_price убран (fix price-B): imageHashes уже в _grid_set_ad_prices,
            # повторный _grid_update_adaptive_ads без adPrice затирал цену.
        elif created_ad_meta:
            # FeedOffersPreview не дал НИ ОДНОЙ цены (сбой/пустой фид) — НЕ затираем и НЕ
            # ставим ничего вслепую; явный warning вместо тихого skip (донор мог уцелеть).
            rep.setdefault("warnings", []).append(
                "adPrice: фид не дал цен (FeedOffersPreview пуст) — цены не проставлены")
    except Exception as _e:  # noqa: BLE001 — цена не критична, объявление уже создано
        rep.setdefault("warnings", []).append(f"adPrice: {str(_e)[:100]}")

    # ── Фаза 3.6: ВИДЕО РСЯ → creativeIds на комбинаторные объявления (Grid, по куке, без баллов).
    # ПОСЛЕ цены: adPrice-апдейт шлёт creativeIds:[] и затёр бы видео, поэтому attach — последним.
    # _VIDEO_AT_CREATE=False: видео вынесено в добивку (спека-аудит VIDEO_MISSING) — каркас не ждёт.
    # tp5 комбинаторные — видео не нужно (только ShoppingAd/ListingAd); пропускаем целиком.
    if created_ad_meta and _VIDEO_AT_CREATE and tp_code != "tp5":
        try:
            _vr = _tp1_video_ads(login, created_ad_meta, grid_cookie=grid_cookie,
                                campaign_id=campaign_id)
            rep["videos_uploaded"] = _vr.get("videos_uploaded", 0)
            rep["videos_attached"] = _vr.get("videos_attached", 0)
            rep["video_groups"] = _vr.get("video_groups", 0)
            if _vr.get("warnings"):
                rep.setdefault("warnings", []).extend(_vr["warnings"])
        except Exception as _e:  # noqa: BLE001 — видео best-effort, объявления уже созданы
            rep.setdefault("warnings", []).append(f"tp1 video: {str(_e)[:100]}")
    elif created_ad_meta and tp_code != "tp5":
        rep["videos_deferred"] = True   # добьётся аудитом (VIDEO_MISSING) после создания

    # ── Фаза 4: товарные по фиду (как в слепках Щербаковой): ListingAd (динамика) + ShoppingAd (товарное)
    # в каждую группу → состав «Т+Л+ТОВ». ShoppingAd создаём ЧЕРЕЗ GRID (addShoppingAds, БЕЗ баллов) —
    # v501 ads.add(ShoppingAd) требовал units и валил докрутку в 152. Только если есть фид и флаг.
    if feed_id and with_shopping:
        rep["listing_ads"], rep["shopping_ads"], rep["shopping_skipped"] = 0, 0, 0
        rep["shopping_ad_ids"] = []   # собираем id для set_default_text (#6 фикс пустого текста)
        rep["shopping_filters"] = {}
        rep["listing_build_items"] = []
        rep["listing_name_by_shop"] = {}   # {shopping_ad_id: name_value} — для name-фильтра листинга
        _grid_shop_items = []         # батч для Grid addShoppingAds: [{adgroup_id, feed_id, vendor/coll}]
        # Коллекции фида (HAR: фильтр «Страницы каталога» = collectionId). Тянем РОВНО этот фид через
        # Grid op Listings (точечный per-feed запрос, не урезанный _grid_feeds). Для брендовых групп
        # резолвим бренд-уровневую коллекцию (id вроде '25' = «Новые автомобили BAIC»), для модельных —
        # model_*. Пустой список → фолбэк на feed_models из _account_model_feeds.
        _feed_colls = _feed_collections(login, int(feed_id), cookie=grid_cookie)
        _feed_models_eff = dict(feed_models or {})
        if not _feed_models_eff:
            _feed_models_eff = _feed_models_from_collections(_feed_colls)
        # Bug A fix: резолвим имена полей бренда/модели для ЭТОГО фида (AUTO_RU: mark_id/folder_id;
        # YANDEX_MARKET: vendor/model). Без этого AUTO_RU фиды получали UNKNOWN_FIELD → стрип фильтра.
        _brand_field = "vendor"
        _model_field = "model"
        try:
            from . import create_set_feeds as _csf_ff
            _brand_field = _csf_ff._resolve_feed_field(login, int(feed_id), "brand") or "vendor"
            _model_field = _csf_ff._resolve_feed_field(login, int(feed_id), "model") or "model"
            if _brand_field != "vendor" or _model_field != "model":
                print(f"[tp1] feed {feed_id}: brand_field={_brand_field!r} model_field={_model_field!r}",
                      flush=True)
        except Exception:  # noqa: BLE001
            pass
        for i in range(len(groups)):
            if not ag_ids[i]:
                continue
            # Фильтр по бренду/модели — ОБЯЗАТЕЛЕН для товарных объявлений в брендовой группе.
            # Без фильтра ShoppingAd/ListingAd показывает ВЕСЬ фид (все марки), что недопустимо
            # в группе конкретного бренда (например, Lada Granta → только Lada, не Haval/Changan).
            #
            # Алгоритм:
            #   feed_models передан   → попробовать collectionId по имени модели/бренда группы;
            #                           нет совпадения → пропускаем (нет коллекции этого бренда в фиде).
            #   feed_models is None   → фид без model-листингов / agency не передан;
            #                           «Vendor»-фильтр через FeedFilterConditions в v501 НЕ верифицирован
            #                           живым тестом → создавать объявление по всему фиду ЗАПРЕЩЕНО.
            # ДВА РАЗНЫХ фильтра по типу объявления (решение Семёна, HAR36):
            #   Товары (ShoppingAd)        → vendor CONTAINS_ANY [марка] (НЕ collectionId — task-6 сломал).
            #   Страницы каталога (Listing) → name CONTAINS_ANY [марка | марка+модель] (updateListingAds).
            #   ct0000/общее (без марки)   → без фильтра (вся витрина).
            _g_brand = (groups[i].get("brand") or "").strip()
            _g_seg = _ct_segment(groups[i].get("ct") or "")
            # Фильтр по производителю/названию валиден ТОЛЬКО для брендовых групп («Марки»/«Модели»).
            # Для «Общее» brand = имя темы («Автокредит» и т.п.) → vendor/name стали бы мусором
            # (vendor содержит «avtokredit» → 0 товаров). Общее → товарка по всему фиду, каталог — все стр.
            _is_brand_seg = _g_seg in ("Марки", "Модели")
            _vendor = _vendor_value(_g_brand) if (_g_brand and _is_brand_seg) else None
            _name_val = _listing_name_value(_g_brand, _g_seg) if (_g_brand and _is_brand_seg) else None
            _model_vals = _model_field_values(_g_brand, _g_seg) if (_g_brand and _is_brand_seg) else []  # Модели → +model
            _grid_shop_items.append({"adgroup_id": int(ag_ids[i]), "feed_id": int(feed_id),
                                     "vendor": _vendor, "collection_id": None, "model": _model_vals,
                                     "name": groups[i].get("name", "?"),
                                     "brand_field": _brand_field, "model_field": _model_field,
                                     "apply_global_minus": _apply_feed_minus})
            rep["listing_build_items"].append({
                "adgroup_id": int(ag_ids[i]),
                "feed_id": int(feed_id),
                "name_value": _name_val,                       # name-фильтр листинга (None для ct0000)
                "name": groups[i].get("name", "?"),
            })
        # Батч Grid addShoppingAds — без баллов (id в порядке adAddItems). При сбое всего батча
        # каждая группа уже имеет TextAd; товарка докрутится ретраем — не валим кампанию.
        if _grid_shop_items:
            try:
                # token→Grid replication lag: кампания+группы созданы через v501 (token), а
                # add_shopping_ads — Grid-мутация на ДРУГОЙ реплике, которая свежую кампанию/группы
                # ещё не догнала → *_NOT_FOUND → пустой shopping_ad_ids → gate «создана без ShoppingAd»
                # удалял часть tp5 одного аккаунта (2026-07-06 группа C: psm5h7q6/7bqj56f4/lzjk6p5m).
                # Реплика догоняет за <~2с — ретраим с бэкоффом; прочие ошибки — сразу в except ниже.
                # Ретрай replication-lag живёт ВНУТРИ add_shopping_ads (почанковый, только при
                # полном отказе чанка — grid_finalize). Внешний ретрай целого батча здесь
                # ДУБЛИРОВАЛ ShoppingAd уже успешных чанков при >50 групп, а матч по подстроке
                # 'NOT_FOUND' ловил перманентный FEED_NOT_FOUND (ревью 06.07) — убран.
                _ids = gf.get_grid_client(login, cookie=grid_cookie).add_shopping_ads(_grid_shop_items)
                rep["shopping_ad_ids"] = [int(x) for x in _ids if x]
                rep["shopping_ads"] = len(rep["shopping_ad_ids"])
                for _li, (_raw_id, _src) in enumerate(zip(_ids, _grid_shop_items)):
                    if not _raw_id:
                        continue
                    # листинг этой группы (by-shopping) получит name-фильтр по shopping_ad_id
                    _nv = (rep["listing_build_items"][_li] or {}).get("name_value") if _li < len(rep["listing_build_items"]) else None
                    if _nv:
                        rep["listing_name_by_shop"][int(_raw_id)] = _nv
                    _conds = []
                    _bf = _src.get("brand_field") or "vendor"
                    _mf = _src.get("model_field") or "model"
                    if _src.get("vendor"):
                        _conds.append({"field": _bf, "operator": "CONTAINS_ANY",
                                       "stringValue": json.dumps(_vendor_filter_values(_src["vendor"]), ensure_ascii=False)})
                    if _src.get("model"):
                        _mvals = _src["model"] if isinstance(_src["model"], list) else [str(_src["model"])]
                        _mvals = [str(x) for x in _mvals if str(x).strip()]
                        if _mvals:
                            _conds.append({"field": _mf, "operator": "CONTAINS_ANY",
                                           "stringValue": json.dumps(_mvals, ensure_ascii=False)})
                    # collectionId-фолбэк удалён (2026-07-07): collection_id всегда None;
                    # ListingAd фильтр = name CONTAINS_ANY [марка] через _grid_add_listings_with_name_filters.
                    # Для Общих/несегментированных групп без name_value — листинг без фильтра (весь каталог).
                    if _src.get("apply_global_minus", True) is not False:
                        try:                             # глобальные минус-марки: используем тот же brand_field
                            from . import create_set_feeds as _csf
                            _conds.extend(_csf._minus_marks_grid_conditions(brand_field=_bf, model_field=_mf))
                        except Exception:  # noqa: BLE001
                            pass
                    if _conds:
                        rep["shopping_filters"][int(_raw_id)] = {"tab": "CONDITION", "conditions": _conds}
            except Exception as e:  # noqa: BLE001
                rep["errors"].append(f"shopping(Grid addShoppingAds): {str(e)[:120]}")

    # ── Фаза 4a: «Все фиды» — ОДНА группа на каждый разрешённый фид (shopping + listing) ──────
    # Тег «все фиды»: вместо fan-out N кампаний (по фидам) — ОДНА кампания с group-on-feed.
    # Группы не несут TextAd/ключей — только ShoppingAd + ListingAd. Имя уникально по фиду.
    # Независимо от with_shopping: Phase 4a запускается как отдельный путь при наличии all_feeds_list.
    if all_feeds_list:
        rep.setdefault("listing_ads", 0)
        rep.setdefault("shopping_ads", 0)
        rep.setdefault("shopping_ad_ids", [])
        _af_default_text = "Новые автомобили в наличии. Большой выбор. Скидки до 45% при покупке. Тест-драйв."
        try:
            from .create_set_assets import SHOPPING_DEFAULT_TEXT as _af_sdt  # noqa: PLC0415
            _af_default_text = _af_sdt
        except Exception:  # noqa: BLE001
            pass
        for _feed_entry in all_feeds_list:
            if not _feed_entry:
                continue
            _fid = int(_feed_entry[0]) if _feed_entry[0] else 0
            _fnm = str(_feed_entry[1]) if len(_feed_entry) > 1 and _feed_entry[1] else ""
            if not _fid:
                continue
            _gn_af = f"Товарная галерея · {(_fnm or str(_fid))}"[:255]
            _new_ag = None
            if tp_code == "tp5":
                # tp5 (TEXT_CAMPAIGN): группа через GridClient с search-профилем (атомарно)
                try:
                    _gcl_af = gc.GridCreateClient(login, cookie=grid_cookie)
                    _gaf_items = [gc.build_adgroup(
                        campaign_id=int(campaign_id), name=_gn_af, region_ids=rids,
                        keywords=[], minus_keywords=[], autotargeting_profile="search_tp2")]
                    _gaf_ids = _gcl_af.add_adgroups(_gaf_items)
                    _new_ag = _gaf_ids[0] if _gaf_ids else None
                except Exception as _e:  # noqa: BLE001
                    rep.setdefault("errors", []).append(f"all_feeds grp tp5({_fid}): {str(_e)[:120]}")
            else:
                # tp1 ЕПК: группа через ТОТ ЖЕ Grid-транспорт, что и Фаза 1 — relevanceMatch
                # выставляется АТОМАРНО при создании. v501 adgroups.add не умеет relevanceMatch:
                # группа получала дефолт Директа (ACTIVE) даже в кампании планового `aoff` —
                # ровно тот дефект, который для основных групп уже закрыт Фазой 1. Детектору
                # grid_read.py:356-362 такая группа НЕ видна (нет токена `_aon_`/`_aoff_` в имени
                # «Товарная галерея · <фид>»), поэтому чинить надо в источнике, а не в замере.
                # UTM не теряется: build_adgroup кладёт trackingParams = cmc.UTM_TEMPLATE — тот же
                # макрос, что и _UTM_TEMPLATE_TP1 на прежнем v501-пути.
                try:
                    _gaf_items = [gc.build_adgroup(
                        campaign_id=int(campaign_id), name=_gn_af, region_ids=rids,
                        keywords=[], minus_keywords=[], autotargeting=bool(autotarget))]
                    _gaf_ids = _gcl.add_adgroups(_gaf_items)
                    _new_ag = _gaf_ids[0] if _gaf_ids else None
                    if not _new_ag:
                        rep.setdefault("errors", []).append(
                            f"all_feeds grp tp1({_fid}): группа не создана (Grid AddUnifiedAdGroups)")
                except Exception as _e:  # noqa: BLE001
                    rep.setdefault("errors", []).append(f"all_feeds grp tp1({_fid}): {str(_e)[:120]}")
            if not _new_ag:
                continue
            # ShoppingAd + ListingAd через Grid (без баллов). Без brand-фильтра — весь фид.
            _af_shop_item = [{"adgroup_id": int(_new_ag), "feed_id": _fid,
                              "vendor": None, "collection_id": None, "model": [],
                              "name": _gn_af, "brand_field": "vendor", "model_field": "model",
                              "apply_global_minus": _apply_feed_minus}]
            _af_build: dict = {"listing_name_by_shop": {}}
            try:
                _gcl_afs = gf.get_grid_client(login, cookie=grid_cookie)
                _af_ids = _gcl_afs.add_shopping_ads(_af_shop_item)
                _af_shop = [int(x) for x in _af_ids if x]
                rep["shopping_ad_ids"].extend(_af_shop)
                rep["shopping_ads"] += len(_af_shop)
                if _af_shop:
                    try:
                        _gcl_afs.set_default_text(_af_shop, _fid, _af_default_text)
                    except Exception:  # noqa: BLE001
                        pass
                    _grid_add_listings_with_name_filters(
                        gf.get_grid_client(login, cookie=grid_cookie),
                        _af_shop, _af_build, _fid, _af_default_text,
                        apply_global_minus=_apply_feed_minus)
                    rep["listing_ads"] += _af_build.get("listing_ads", 0)
            except Exception as _e:  # noqa: BLE001
                rep.setdefault("errors", []).append(f"all_feeds shop({_fid}): {str(_e)[:120]}")

    # Синтез singular error из errors (plural) для структурных дефектов.
    # Вынесено в _synthesize_tp1_build_error() для покрытия тестами.
    _synthesize_tp1_build_error(rep, tp_code, autotarget=autotarget, keep_keywords=keep_keywords)
    return rep

def _pack_read_glitch(key: str, site_type: str, pack_tp: str) -> bool:
    """Пустой пак: РЕАЛЬНЫЙ сбой чтения M3 (relay/sshfs) или пака легитимно нет у слепка?
    Двойной дискриминатор (не деферить легитимно-пустой автотаргет-пак, напр. gordeeva tp2):
      1) probe СОСЕДНЕГО tp-пака — если ЧИТАЕТСЯ (непуст), а целевой пуст → инфра жива,
         у слепка просто нет пака этого сегмента → False (НЕ дефер);
      2) сосед ТОЖЕ пуст — это ещё НЕ сбой (у слепка может легитимно не быть пака и в
         соседнем tp). Проверяем M3-relay напрямую тем же ssh-транспортом (kp.m3_reachable,
         без обхода каталогов): relay отвечает → инфра доступна, пака просто нет → False;
         relay недоступен → реальный сбой чтения → True (дефер на докрутку)."""
    probe_tp = "tp2" if pack_tp != "tp2" else "tp1"
    try:
        neighbor = kp.gather(key, site_type, probe_tp) or {}
    except Exception:  # noqa: BLE001
        neighbor = {}
    if neighbor:
        return False   # сосед читается непустым → инфра жива, целевой пак легитимно пуст
    # сосед тоже пуст — различаем реальный сбой инфры vs легитимно нет пака у слепка
    return not kp.m3_reachable()


def _drop_tp1_groups_without_images(groups: list, tp_code: str, products_only: bool = False) -> tuple[list, dict]:
    """For tp1 ResponsiveAd requires own images. Groups with no image pool are a data gap:
    skip them instead of creating naked РСЯ ads that live verification flags as NO_IMAGES_LIVE."""
    if tp_code != "tp1" or products_only:
        return groups, {}
    kept: list = []
    skipped: list = []
    for g in groups or []:
        if g.get("image_hashes") or g.get("image_paths") or g.get("image_path"):
            kept.append(g)
        else:
            skipped.append(str(g.get("ct") or "").strip() or "ct?")
    if not skipped:
        return groups, {}
    return kept, {
        "image_no_pool": True,
        "groups_skipped_no_images": len(skipped),
        "image_no_pool_cts": sorted(set(skipped)),
    }


def _build_tp1_from_pack(
    token: str,
    login: str,
    campaign_id: int,
    slepok: str,
    site_type: str,
    region_ids: list,
    href: str,
    r_code: str,
    titles: list | None,
    texts: list,
    counter_id: int = 0,
    feed_id: int = 0,
    with_shopping: bool = False,
    feed_models: dict | None = None,
    segment: str | None = None,
    ai_title2: str = "",
    city: str = "",
    autotarget: bool = False,
    keep_keywords: bool = False,
    products_only: bool = False,
    tp_code: str = "tp1",
    sitelinks: list | None = None,
    grid_cookie: str | None = None,
    only_gks: set | None = None,
    only_cts: set | None = None,
    all_feeds_list: list | None = None,
    campaign_is_new: bool = False,
) -> dict:
    """Наполнить РСЯ (tp1/tp5 ЕПК) бренд-группами из пака M3.

    all_feeds_list (тег «все фиды»): [(feed_id, feed_name, …), …] — в одной кампании создаётся
    ГРУППА НА КАЖДЫЙ фид (ShoppingAd + ListingAd) вместо fan-out N кампаний. Бренд-группы из
    пака получают только TextAd (without_shopping=True при all_feeds_list). Если all_feeds_list
    пуст или None — прежнее поведение (per-brand-group shopping через feed_id).

    tp_code: код пака M3 для gather() — 'tp1' для РСЯ-кампаний, 'tp5' для комбинированных
    поисковых. По умолчанию 'tp1' (обратная совместимость).
    segment ('Марки'|'Модели'|None): фильтр ct-папок по сегменту (как в боевых аккаунтах —
    марки и модели РАЗНЫМИ кампаниями). None → все группы (старое поведение).
    Каждая ct-папка пака = отдельная группа. Имя группы = КАНОН CODER.md.
    Ключи/минус/уточнения/картинки — из пака. Объявления: TextAd + AdImageHash.
    Быстрые ссылки (sitelinks) — из direct_slepok_content слепка: создаём набор ОДИН раз,
    привязываем через SitelinkSetId ко ВСЕМ объявлениям группы.
    Callouts — из пака (per-бренд) через AdExtensions на объявление.
    """
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    pack = kp.gather(key, site_type, tp_code)  # один ssh-вызов к M3
    if not pack:
        # Пустой пак (у слепка нет tp_code-пака, напр. pavlov/tp5; M3 при этом жив — tp1-пак есть).
        # Для ТОВАРНОЙ ГАЛЕРЕИ по фиду это НЕ блокер: фид-товарка не зависит от бренд-пака →
        # проваливаемся в фид-фолбэк ниже (создаст товарную галерею по фиду). Иначе — честный скип
        # (defer=True при глобальном сбое чтения M3 — пункт уйдёт на докрутку).
        # ИСКЛЮЧЕНИЕ: пак пуст ПО ЗАМЫСЛУ (ЕПК/аудиторные группы avtolajt_bu/sk_krs — нет ключей).
        # Два пути:
        #   (а) only_gks задан (tp5): ct0000+gk роутинг → дефолтный bypass.
        #   (б) only_gks=None (tp1): проверяем структуру — если у tp_code есть semantic-ct0000-gk items,
        #       тоже обходим «пак недоступен». При реальном глитче — дефолт-дефер в обоих случаях.
        if not (with_shopping and feed_id):
            _glitch = _pack_read_glitch(key, site_type, tp_code)
            _can_bypass = False
            if not _glitch:
                if only_gks:
                    _can_bypass = True          # tp5/camp_names-маршрут: only_gks задан
                else:
                    # tp1-путь: нет only_gks → смотрим структуру на ct0000+semantic-gk
                    try:
                        from .create_set_structure import _load_struct as _ls0, _slepok_key as _sk0
                        _sd0 = _ls0()
                        _dl0 = next((x for x in (_sd0.get("directologists") or [])
                                     if x.get("key") == _sk0(slepok)), None)
                        _st0 = next((s for s in ((_dl0.get("site_types") or []) if _dl0 else [])
                                     if s.get("name") == site_type), None)
                        for _tp0 in ((_st0.get("tp") or []) if _st0 else []):
                            if _tp0.get("code") != tp_code:
                                continue
                            _can_bypass = any(
                                (it.get("gk") or "").strip()
                                and not re.search(r"aon_n000", it.get("gk") or "")
                                for _g in (_tp0.get("groups") or [])
                                for it in (_g.get("items") or [])
                                if (it.get("gc") or "").startswith("ct0000")
                            )
                            break
                    except Exception:   # noqa: BLE001 — структура недоступна: дефолт-пропуск
                        pass
            if not _can_bypass:
                return {"skipped": "пак недоступен (мост M3?)", "defer": _glitch}
        pack = {}

    text0 = _trim_clean(texts[0] if texts else "", 81) or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии."
    ct_model = kp.feeds_ct_model()            # фид-картиночный индекс (ct0020+, модели) — фолбэк
    ct_name = _ag_part1_map()                 # ct→имя из gsheet_naming (ag_part1, полное покрытие 318)
    _sc_titles = _slepok_campaign_content(slepok, site_type).get("titles") or []  # пул слепка — 1 раз на кампанию
    # URL страниц моделей: account-level мёрж (все фиды, как цены) → покрывает марки без URL
    # в конкретном feed_id (#ФИКС-8).
    _feed_urls_tp1 = _account_offer_urls(login, _site_root_href(href))

    # Строим группы ТОЛЬКО для ct-папок у которых есть ключи слепка
    groups = []
    # Минус-марки/модели: вычисляем один раз на кампанию для O(1) проверки.
    # Марки: канонизируем в латиницу (_brand_canon), чтобы устоять против будущих кириллических записей.
    # Модели: карта {mark_canon → set(model_lower)} — точное равенство в пределах бренда,
    #   предотвращает over-match для коротких имён («5», «J7»). Срез [1:] = первый токен = бренд
    #   (допущение: марки автодилеров односложные — BAIC/Chery/MG/Lada).
    _minus_m_set: set = {_brand_canon(str(m).strip().lower()) for m in (_enabled_minus_marks() or []) if str(m).strip()}
    _minus_mod_by_brand: dict = _build_minus_mod_by_brand(_enabled_minus_model_pairs() or [])
    _img_rr = 0                                   # round-robin по пулу картинок ct (Павлов: «разбавить однотипными»)
    _struct_names = _struct_ct_names(slepok, site_type)   # не-авто: имя группы из структуры/выгрузки, не авто-фид
    # Группы 1в1 (per-adgroup). Гейт: в структуре у какого-то ct >1 группа (реальная ct-коллизия)
    # И это НЕ dmp (_struct_names) → строим по СТРУКТУРНЫМ items (не по дедуп-ct pack.keys()), беря
    # per-group пак pack[ct]["_groups"][gk] с фолбэком на общий pack[ct]. Иначе — прежний per-ct цикл.
    _items = [] if _struct_names else _struct_items(slepok, site_type, tp_code)
    if segment:
        _items = [it for it in _items if _ct_segment(it["ct"]) == segment]
    # camp_names-маршрутизация (задача 7): группы кампании ограничены её gk/ct (structure_to_campaigns).
    # gk-фильтр — приоритетен (per-adgroup, _multi); ct-фильтр — для нон-мульти (нет ct-коллизии).
    _og = {g for g in (only_gks or ()) if g} or None
    _oc = {c for c in (only_cts or ()) if c} or None
    if _og is not None:
        _items = [it for it in _items
                  if (it.get("gk") or "") in _og
                  or (not (it.get("gk") or "") and _oc is not None and it["ct"] in _oc)]
    _multi = bool(_items) and any(v > 1 for v in Counter(it["ct"] for it in _items).values())
    if _multi:
        _units = [(it["ct"], it.get("gk") or "", it.get("name") or "")
                  for it in _items if it["ct"] in pack]
    else:
        # Структурный узел ЦЕЛИКОМ на ct0000 («Агрегаторы», «… - Аудитории»): _struct_items ct0000
        # пропускает (там только модель-ct) → gk-фильтр выше дал пусто, и модель-ct у кампании нет
        # вовсе (_oc пуст). Пустой ct-фильтр тут НЕ значит «фильтра нет»: без этой ветки брался
        # ВЕСЬ пак и узел из одной группы давал 27 групп кабинета. Маркер узла — ровно тройка
        # «camp_names-маршрут задан (_og) + модель-ct нет (_oc) + структурных модель-items нет».
        _ct0_units = (_struct_ct0000_units(slepok, site_type, tp_code, _og)
                      if (_og is not None and _oc is None and not _items) else [])
        if _ct0_units:
            _units = _ct0_units
            _multi = True                         # имя группы = структурный item.t, а не авто-ct
        else:
            _pk = sorted(pack.keys())
            if _oc is not None:                   # нон-мульти camp_names: только ct кампании
                _pk = [ct for ct in _pk if ct in _oc]
            _units = [(ct, "", "") for ct in _pk]
    # Фолбэк для ЕПК/аудиторных групп (ct0000+явный семантичный gk, напр. avto_sk, avtolajt_bu):
    # _struct_items пропускает ct0000 → _items пуст → _units тоже пуст, но only_gks задаёт
    # конкретные структурные группы. Читаем их напрямую из структуры (кэш _load_struct).
    # Явный gk в JSON + нет "aon_n000" (gc-артефактов харвеста) = семантичный слаг.
    if not _units:
        try:
            from .create_set_structure import _load_struct as _ls_ct0, _slepok_key as _sk_ct0
            _sd_ct0 = _ls_ct0()
            _k_ct0 = _sk_ct0(slepok)
            _dl_ct0 = next((x for x in (_sd_ct0.get("directologists") or []) if x.get("key") == _k_ct0), None)
            _st_ct0 = next((s for s in ((_dl_ct0.get("site_types") or []) if _dl_ct0 else [])
                            if s.get("name") == site_type), None)
            _ct0_found: list = []
            for _tp_ct0 in ((_st_ct0.get("tp") or []) if _st_ct0 else []):
                if _tp_ct0.get("code") != tp_code:
                    continue
                for _g_ct0 in (_tp_ct0.get("groups") or []):
                    for _it_ct0 in (_g_ct0.get("items") or []):
                        _igk = (_it_ct0.get("gk") or "").strip()
                        # Только явный семантичный gk (не gc-производный вида aon_n000_…)
                        if _igk and not re.search(r"aon_n000", _igk) and (_og is None or _igk in _og):
                            _ct0_found.append({"ct": "ct0000", "gk": _igk,
                                               "name": (_it_ct0.get("t") or _g_ct0.get("name") or "").strip()})
            if _ct0_found:
                _units = [(it["ct"], it.get("gk") or "", it.get("name") or "") for it in _ct0_found]
                _multi = True   # group_name берётся из _uname (структурный item.t), а не авто-ct
        except Exception:       # noqa: BLE001 — фолбэк не должен ронять создание
            pass
    # Pre-pass: прогрев кэша link_check параллельно (6 потоков). Основной цикл ниже
    # вызывает _resolve_url(model_href) — теперь это будет cache-hit. (#LINK_CHECK_404_FALLBACK)
    _batch_hrefs: list[str] = []
    for _pct, _pgk, _puname in _units:
        _pgrp_pack = (pack.get(_pct, {}).get("_groups") or {}).get(_pgk) if _pgk else None
        _pdata = _pgrp_pack or pack.get(_pct) or {}
        if not _pdata.get("positive") and not (_pct == "ct0000" and _pgk):
            continue
        if segment and _ct_segment(_pct) != segment:
            continue
        _praw = ((_struct_names.get(_pct) or ct_name.get(_pct) or _pct) if _struct_names
                 else (ct_name.get(_pct) or ct_model.get(_pct) or _pct))
        _pbrand = _valid_pack_brand_name(_pct, _praw)
        # ФИКС-B pre-pass: per-model (_multi + _puname) → формульный href (ct0000 "Марки"
        # по умолчанию давал бы brand-level URL через feed-lookup).
        _batch_hrefs.append(_model_page_href(_site_root_href(href), site_type, _puname)
                            if (_multi and _puname)
                            else _pack_group_href(_pct, _pbrand, _feed_urls_tp1, href, site_type))
    _resolve_urls_batch(_batch_hrefs)

    for ct, _gk, _uname in _units:
        _grp_pack = (pack.get(ct, {}).get("_groups") or {}).get(_gk) if _gk else None
        data = _grp_pack or pack.get(ct) or {}
        if not data.get("positive") and not (ct == "ct0000" and _gk):
            continue  # ct0000+gk = ЕПК/аудиторная группа без ключей (норма); остальные → пропускаем
        if segment and _ct_segment(ct) != segment:
            continue                           # сегментный фильтр (Марки/Модели как в боевых)
        # не-авто (dmp): имя = структура t(выгрузка) → leadgen(описание кодера) → ct; НИКОГДА авто-фид «Авто».
        # Структура ПЕРВАЯ: _ag_part1_map мёржит авто-справочник gsheet_naming, и авто-ct может
        # совпасть с темой слепка (ct0084: авто «Faw Bestune T77» ↔ dmp «Конкуренты») → тема стала бы маркой.
        raw_brand = ((_struct_names.get(ct) or ct_name.get(ct) or ct) if _struct_names
                     else (ct_name.get(ct) or ct_model.get(ct) or ct))
        # Минус-фильтр групп: марка/модель отмечена в «Глобальных правилах» → группа не создаётся
        if _minus_m_set or _minus_mod_by_brand:
            _rb_tok = (raw_brand or "").strip().split()[0].lower()
            _rb_canon = _brand_canon(_rb_tok) if _rb_tok else ""
            _model_portion = " ".join((raw_brand or "").strip().split()[1:]).lower()
            if ((_rb_canon and _rb_canon in _minus_m_set) or
                    (_rb_canon and _model_portion and
                     _model_portion in _minus_mod_by_brand.get(_rb_canon, set()))):
                print(f"[minus-filter] group skipped: ct={ct} brand={raw_brand!r} tp={tp_code}", flush=True)
                continue
        brand = _valid_pack_brand_name(ct, raw_brand)   # логический бренд: пустой для «Общее»
        group_label = _pack_group_display_name(ct, raw_brand, brand)
        # multi-путь (camp_names/per-модель, tp5 «Товарная галерея - Модели»): _uname = структурное
        # имя модели («Jetta», «GAC Gs4»…) — используем как БРЕНД в кодере, а не как сырое имя.
        # Это гарантирует кодер-префикс первым (правило CODER.md: «кодер всегда первый в имени»).
        # До фикса: _uname отдавался напрямую как group_name — кодер пропадал в multi-ветке.
        _group_brand = (_uname if (_multi and _uname) else group_label)
        group_name = _tp1_group_name(ct, r_code, _group_brand, with_shopping=with_shopping,
                                     autotarget=autotarget, tp_code=tp_code)
        # Картинки: общие ct0000-ct0014 → общий пул ct0000; кузова ct0015-ct0018 → свой ct;
        # модели/марки → свой ct.
        all_images = _creative_images_for_ct(site_type, tp_code, ct, key)
        # Ротация по пулу картинок (а не всегда [0]) — чтобы РСЯ-объявления не были однотипными.
        # image_path — первая из ротации (совместимость); image_paths — все (для мульти-upload в Фазе 3).
        image_path = all_images[_img_rr % len(all_images)] if all_images else None
        _img_rr += 1
        # deep-link: фид → формульный слаг. ФИКС-A: Марки→/auto/{brand}, Модели→полный путь.
        # 404-фолбэк: кэш прогрет pre-pass (batch 6 потоков) → cache-hit. (#LINK_CHECK_404_FALLBACK)
        # ФИКС-B: per-model (_multi + _uname, ct0000): _valid_pack_brand_name возвращает "" для
        # ct0000 → _pack_group_href давал голый домен. Обходим feed-lookup, строим по формуле.
        model_href = (_resolve_url(_model_page_href(_site_root_href(href), site_type, _uname))
                      if (_multi and _uname)
                      else _resolve_url(_pack_group_href(ct, brand, _feed_urls_tp1, href, site_type)))
        # Title: шаблон «Новые {brand} в {город}. {акция}» (≤35 симв.) — фолбэк brand[:35].
        # ai_title2 — ИИ-заголовок (если дан), иначе round-robin из пула.
        is_brand_group = _ct_segment(ct) in ("Марки", "Модели")
        title = (_title_from_template(brand, city, slepok=slepok, site_type=site_type) if (is_brand_group and not ai_title2)
                 else (_GENERIC_AT_TITLES[0] if not is_brand_group else brand[:35]))
        ttl2 = (ai_title2[:30] if ai_title2 else _next_title2())   # ИИ-title2 или round-robin из пула
        # Внутри pack-групп не вызываем M3 per-ct: ИИ уже сгенерировал контент кампании/item.
        # Иначе tp1/tp5 на больших паках создаются неприемлемо долго и старт очереди "замирает".
        g_titles = _rsya_titles(brand, city, site_type, ai_title2=ai_title2, slepok=slepok,
                                base=(list(titles or []) + [title, ttl2] if is_brand_group
                                      else list(titles or []) + list(_GENERIC_AT_TITLES)),
                                pool=_sc_titles, is_brand=is_brand_group)
        g_texts = _rsya_texts(list(texts or []) + ([text0] if text0 else []), site_type, city, brand)
        groups.append({
            "name": group_name,
            "ct": ct,
            "brand": brand,
            # БАГ-13: для «Марки» — убрать ключи «марка+модель»; model= защищает «Модели» от чужих моделей.
            # В per-adgroup режиме (_multi) модель берём у САМОЙ ГРУППЫ (структурный `t`), иначе
            # под-модели одного ct дискриминируют друг друга и группа уезжает с 0 ключей.
            "keywords": _filter_group_keywords(data.get("positive", []), _ct_segment(ct), brand, city, site_type,
                                               model=(_uname if (_multi and _uname) else brand)),
            "minus": [],   # v5: _build_tp1_adgroups игнорирует g["minus"] (РСЯ/поиск — без охват-режущих группов. минусов)
            "titles": g_titles or ([title, brand] if brand else [title]),
            "texts": g_texts or ([text0] if text0 else []),
            "title": title, "title2": ttl2, "text": text0,   # совместимость
            "href": model_href,               # deep-link страницы модели
            "image_path": image_path,         # первая картинка (round-robin); совместимость
            "image_paths": all_images[:5],    # все пути (пак + Manual) — для мульти-upload в Фазе 3
            "callouts": data.get("callouts", []),
        })

    if not groups and segment and _pack_read_glitch(key, site_type, tp_code):
        # СЕГМЕНТНОЙ кампании (Марки/Модели/Общее) нужны группы из пака, а пак пуст из-за
        # ГЛОБАЛЬНОГО сбоя чтения M3 (probe соседнего tp-пака тоже пуст). Фолбэк «Товарная
        # галерея» дал бы пустышку — инцидент 03.07.2026: tp1 Модели/Общее с 1 группой (cts=0,
        # camp 712112065). → defer на докрутку. Легитимно пустой пак (pavlov tp5: probe tp2
        # читается) идёт ниже в фид-фолбэк как раньше.
        return {"skipped": f"пак пуст для сегмента {segment} ({tp_code}) — сбой чтения M3, "
                           "отложено на докрутку",
                "defer": True}
    if not groups and with_shopping and feed_id and (only_gks or only_cts or segment):
        _wanted = []
        if only_gks:
            _wanted.append("gk=[" + ", ".join(sorted(str(x) for x in only_gks if x)) + "]")
        if only_cts:
            _wanted.append("ct=[" + ", ".join(sorted(str(x) for x in only_cts if x)) + "]")
        if segment:
            _wanted.append(f"segment={segment}")
        return {"skipped": "content-gap: для структурной tp5 не найдены группы/ключи "
                           + ("; ".join(_wanted) if _wanted else ""),
                "defer": _pack_read_glitch(key, site_type, tp_code)}
    if not groups and with_shopping and feed_id:
        # Бренд-пак слепка для tp_code ПУСТ (напр. у pavlov нет tp5-пака), НО это товарная галерея
        # по фиду — фид-товарка (ShoppingAd/ListingAd) НЕ зависит от бренд-пака. Чтобы tp5/tp3 не
        # выходили пустыми, создаём ОДНУ товарную-галерею группу по всему фиду: автотаргет + общие
        # заголовки/тексты + товарные объявления (with_shopping ниже добавит ShoppingAd+ListingAd).
        # site_type-фильтр: сырой `_GENERIC_AT_TITLES`/`_GENERIC_TEXT_FILLERS` на Б/У-сайте
        # («С пробегом») протаскивал «Купить новое авто…»/«Кредит без взноса на новое авто…».
        # На не-Б/У `_drop_new_car` — no-op. После фильтра остаётся ≥5 заголовков / ≥3 текста.
        _gg_titles = _drop_new_car(list(_GENERIC_AT_TITLES), site_type)
        _gg_texts = _drop_new_car(list(_GENERIC_TEXT_FILLERS), site_type)
        groups = [{
            "name": "Товарная галерея", "ct": "ct0000", "brand": "",
            "keywords": [], "minus": [],
            "titles": _gg_titles,
            "texts": _gg_texts,
            "title": (_gg_titles[0] if _gg_titles else ""), "title2": "",
            "text": (_gg_texts[0] if _gg_texts else ""),
            "href": href, "image_path": None, "callouts": [],
        }]
        autotarget = True                                 # товарная галерея по фиду = автотаргет (нет бренд-ключей)
    if not groups:
        # defer только при СБОЕ чтения M3 (probe): у слепка может легитимно не быть пака —
        # безусловный defer гонял бы «ядовитый» пункт по 3 ресума (ревью 03.07 #22).
        return {"skipped": f"пак пуст: нет ct-папок с ключами слепка {key} для {tp_code}",
                "defer": _pack_read_glitch(key, site_type, tp_code)}
    groups, _img_skip = _drop_tp1_groups_without_images(groups, tp_code, products_only)
    if _img_skip:
        _cts_s = ", ".join((_img_skip.get("image_no_pool_cts") or [])[:12])
        _more = len(_img_skip.get("image_no_pool_cts") or []) - 12
        _note = (f"tp1: пропущено групп без картинок: {_img_skip['groups_skipped_no_images']} "
                 f"(ct: {_cts_s}{'; +' + str(_more) if _more > 0 else ''})")
        if not groups:
            return {**_img_skip, "skipped": _note}
    else:
        _note = ""

    # Быстрые ссылки: создаём набор ОДИН раз → SitelinkSetId на каждое объявление.
    # Важно: v5-only путь здесь молча оставлял tp1 без ссылок при пустом kind='sitelinks'.
    # Общий resolver берёт campaign-content слепка и умеет Grid/cookie fallback без баллов.
    sitelink_set_id = None
    base_sitelinks: list = []   # нормализованные ссылки для per-group наборов (#ФИКС-3)
    asset_warns = []
    try:
        _assets = _resolve_campaign_assets(token, login, href, sitelinks=sitelinks,
                                           slepok=slepok, site_type=site_type, grid_cookie=grid_cookie)
        sitelink_set_id = _assets.get("sitelink_set_id")
        base_sitelinks = _assets.get("asset_sitelinks") or []
        # Fix 8: диагностика сбоя набора быстрых ссылок из resolver'а — в отчёт кампании,
        # чтобы null-набор перестал быть «слепым» (раньше причина глохла в except: pass).
        asset_warns.extend(_assets.get("asset_warns") or [])
    except Exception as e:  # noqa: BLE001 — sitelinks не критичны, но должны быть видны в отчёте
        asset_warns.append(f"sitelinks(tp1): {str(e)[:120]}")

    # Callouts: создаём общий пул AdExtensions (уточнения из пака).
    # Правило Семёна 03.07.2026: грузим ТОЛЬКО используемые (первые 4 на группу — столько
    # и вешаем) + ЗАПАС 5 уточнений с ДРУГИМ УТП (которых нет среди используемых) — для
    # ручной замены в кабинете. Остальной пул слепка в аккаунт НЕ регистрируем (сироты).
    co_pool = {}
    try:
        import re as _re_co   # module-scope `re` в этом файле нет (только локальные импорты)
        used, seen_used = [], set()
        for g in groups:
            for c in (g.get("callouts") or [])[:4]:
                if c and c not in seen_used:
                    seen_used.add(c)
                    used.append(c)

        def _co_utp_key(t: str) -> str:
            # смысловой ключ УТП уточнения: без цифр/процентов, первые 2 слова
            base = _re_co.sub(r"[0-9%]+", " ", str(t or "").lower().replace("ё", "е"))
            return " ".join(base.split()[:2])

        used_keys = {_co_utp_key(c) for c in used}
        spare: list = []
        for c in (c for g in groups for c in (g.get("callouts") or [])[4:]):
            if not c or c in seen_used or c in spare or _co_utp_key(c) in used_keys:
                continue
            spare.append(c)
            used_keys.add(_co_utp_key(c))
            if len(spare) >= 5:
                break
        co_pool = _ensure_callout_exts(token, login, used + spare) if (used or spare) else {}
        for g in groups:
            ids = [co_pool[c] for c in (g.get("callouts") or [])[:4] if c in co_pool]
            if ids:
                g["callout_ext_ids"] = ids[:4]
    except Exception:  # noqa: BLE001
        co_pool = {}

    # products_only (Смарт-Баннер/Фиды «без ТГО»): пропускаем Phase 3 (TextAd/ComboAd).
    # tp5 TextAd/комбинированные создаются в той же Фазе 3 — условие tp5 убрано (2026-07-07).
    _skip_text_ads = products_only
    # all_feeds_list: бренд-группы NOT получают per-group shopping (Phase 4 off);
    # Phase 4a внутри _build_tp1_adgroups создаёт shopping+listing PER feed.
    _eff_shopping = with_shopping and not bool(all_feeds_list)
    rep = _build_tp1_adgroups(token, login, campaign_id, region_ids, href, groups,
                               sitelink_set_id=sitelink_set_id,
                               base_sitelinks=base_sitelinks or None,
                               feed_id=feed_id, with_shopping=_eff_shopping, feed_models=feed_models,
                               autotarget=autotarget, keep_keywords=keep_keywords,
                               products_only=_skip_text_ads,
                               grid_cookie=grid_cookie, tp_code=tp_code,
                               all_feeds_list=all_feeds_list, site_type=site_type,
                               campaign_is_new=bool(campaign_is_new))
    rep["cts"] = len(pack)
    rep["groups_built"] = len(groups)
    rep["callouts_pool"] = len(co_pool)
    rep["sitelinks_set_id"] = sitelink_set_id
    if _img_skip:
        rep.update(_img_skip)
        rep.setdefault("warnings", []).append(_note)
    if asset_warns:
        rep.setdefault("warnings", []).extend(asset_warns)
    # Видео РСЯ (tp1 ЕПК): загрузка по ct из пула M3 + attach creativeIds (Grid) — Фаза 3.6
    # внутри _build_tp1_adgroups. Счётчики уже в rep; здесь только гарантируем их наличие.
    rep.setdefault("videos_uploaded", 0)
    rep.setdefault("videos_attached", 0)
    rep.setdefault("video_groups", 0)
    return rep

def _grid_add_listings_with_name_filters(gcl, shop_ids: list, build: dict,
                                         feed_id: int, default_text: str,
                                         apply_global_minus: bool = True) -> None:
    """Листинги «Страницы каталога» через Grid by-shopping (без баллов) + name-фильтры.

    Общий путь tp1/tp5 (#ФИКС-1): создаём ListingAd по shopping_ad_ids, затем ставим
    name-фильтр CONTAINS_ANY [марка|марка+модель] из ``build['listing_build_items']``
    (HAR36 updateListingAds; by-shopping фильтр не наследует). saveDraft:True → addedAds
    может быть пуст → фолбэк построения items по adGroupId. Ошибки — в build['warnings'],
    кампанию НЕ валим (принцип «дозаполнять, не удалять»). Результаты в build:
    listing_ads (кол-во), listing_name_set."""
    try:
        _rows = gcl.add_listing_ads_by_shopping_ads(shop_ids) or []
        build["listing_ads"] = len(_rows)
        # fix-3 (08.07.2026): addListingAdsByShoppingAds теперь возвращает shoppingAdId.
        # listing_name_by_shop{shop_id→name_value} собирается при add_shopping_ads (до вызова).
        # adGroupId нет в GdUpdateListingAdInput → идентификатор листинга через id.
        _name_by_shop = {int(k): v for k, v in (build.get("listing_name_by_shop") or {}).items() if v}
        # Глобальные минус-марки для ListingAd — тот же brand_field/model_field что у ShoppingAd.
        _lad_minus_conds: list = []
        if apply_global_minus:
            try:
                from . import create_set_feeds as _csf
                _lad_bf = _csf._resolve_feed_field(gcl.login, feed_id, "brand") or "vendor"
                _lad_mf = _csf._resolve_feed_field(gcl.login, feed_id, "model") or "model"
                _lad_minus_conds = _csf._minus_marks_grid_conditions(brand_field=_lad_bf, model_field=_lad_mf)
            except Exception:  # noqa: BLE001
                pass
        _lf_items = []
        _lad_general_lids: list = []   # listing ad IDs без name-фильтра (Общее группы)
        for _row in _rows:
            _lid = _row.get("id") if isinstance(_row, dict) else _row
            _said = int(_row.get("shoppingAdId") or 0) if isinstance(_row, dict) else 0
            if not _lid:
                continue
            _val = _name_by_shop.get(_said) if _said else None
            if _val:
                # R2-4 2026-07-10 (b): брендовая группа = ПОЗИТИВНЫЙ name CONTAINS_ANY [своя марка]
                # ТОЛЬКО. Раньше сюда добавлялся негативный _lad_minus_conds (NOT_CONTAINS_ALL ~8
                # чужих марок) как extra_conds → если позитив падал (D4: AUTO_RU yandex.xml feed
                # 3537034 — поле name/folder_id не резолвится в fieldsForUseAs), в кабинете оставался
                # ТОЛЬКО негатив «Название каталога НЕ содержит knewstar,moskvich,omoda,…» (176/198
                # стр.) вместо «содержит BAIC». Позитив CONTAINS уже ограничивает страницы своей
                # маркой → негатив избыточен и вреден (для BAIC-группы должно быть «содержит BAIC»).
                # Общее/ct0000 (_val=None → ветка else) — негатив-глоб-минус сохранён без изменений.
                _lf_items.append({"id": _lid, "feed_id": feed_id, "value": _val,
                                  "bodies": [default_text]})
            else:
                # Без name-фильтра (Общее/ct0000): только глобальные минус-марки (весь каталог минус чужие).
                _lad_general_lids.append(_lid)
        if _lf_items:
            build["listing_name_set"] = gcl.set_listing_name_filters(_lf_items)
        # Общее группы (нет name-фильтра): проставляем только минус-марки через set_product_feed_filters.
        if _lad_general_lids and _lad_minus_conds:
            _gen_items = [{"id": _lid, "feed_id": feed_id,
                           "conditions": _lad_minus_conds, "bodies": [default_text]}
                          for _lid in _lad_general_lids]
            try:
                gcl.set_product_feed_filters(_gen_items, listing=True)
            except Exception as _ge:  # noqa: BLE001
                build.setdefault("warnings", []).append(f"listing-minus(grid): {str(_ge)[:120]}")
    except Exception as _le:  # noqa: BLE001
        build.setdefault("warnings", []).append(f"listing(grid): {str(_le)[:160]}")


def _add_listing_ads_v501(token: str, login: str, items: list[dict]) -> list[int]:
    """Создать ListingAd через v501 с явным FeedFilterConditions по collectionId.

    Для брендовых групп список collectionId уже развёрнут заранее. Пустой collection_ids
    считаем ошибкой данных и не создаём листинг по всему фиду.
    """
    out: list[int] = []
    for it in items or []:
        coll_ids = [str(x) for x in (it.get("collection_ids") or []) if str(x).strip()]
        if not coll_ids:
            continue
        payload = {
            "Ads": [{
                "AdGroupId": int(it["adgroup_id"]),
                "ListingAd": {
                    "FeedId": int(it["feed_id"]),
                    "FeedFilterConditions": [{
                        "Operand": "collectionId",
                        "Operator": "EQUALS_ANY",
                        "Arguments": coll_ids,
                    }],
                },
            }],
        }
        j = _v501_svc("ads", "add", token, login, payload)
        add_res = ((j.get("result") or {}).get("AddResults") or [{}])[0]
        if add_res.get("Id") and not (add_res.get("Errors") or []):
            out.append(int(add_res["Id"]))
            continue
        errs = add_res.get("Errors") or []
        msg = "; ".join((e.get("Message") or "") for e in errs if isinstance(e, dict)).strip()
        if not msg:
            msg = _v5_err(j)
        raise RuntimeError(f"{it.get('name', '?')}: listing v501 {msg[:180]}")
    return out

def _create_tp1_single(
    token: str,
    login: str,
    name: str,
    counter_id: int,
    goal_id: int,
    cpa_value_rub: int,
    mode: str,
    region_ids: list,
    href: str,
    slepok: str,
    site_type: str,
    r_code: str,
    titles: list | None,
    texts: list,
    feed_id: int = 0,
    with_shopping: bool = False,
    feed_models: dict | None = None,
    budget_rub: int = 0,
    segment: str | None = None,
    city: str = "",
    ai_title2: str = "",
    sitelinks: list | None = None,
    callout_texts: list | None = None,
    callout_ids: list | None = None,
    autotarget: bool = False,
    keep_keywords: bool = False,
    products_only: bool = False,
    grid_cookie: str | None = None,
    job=None,
    only_gks: set | None = None,
    only_cts: set | None = None,
    all_feeds_list: list | None = None,
) -> dict:
    """Создать ОДНУ кампанию tp1 (РСЯ) через ЕПК v501 с указанным mode.

    mode='network_cpa'     → cpc-вариант: AVERAGE_CPA в сетях (tp1_cpc_site)
    mode='network_payconv' → cpa-вариант: PAY_FOR_CONVERSION в сетях (tp1_cpa_site)

    Инварианты: персонализация ВЫКЛ, мониторинг ВКЛ, расш.гео ВЫКЛ.
    Кампания создаётся как DRAFT (launch=False).

    Возвращает {"ok": True, "campaign_id": ..., "tp1_build": {...}} или {"ok": False, ...}.
    """
    spec = cmc.UnifiedCampaignSpec(
        name=name,
        client_login=login,
        oauth_token=token,
        mode=mode,
        region_ids=region_ids,
        counter_ids=[counter_id] if counter_id else None,
        goal_id=goal_id or None,
        network_average_cpa=int(cpa_value_rub) * 1_000_000,  # руб → мкруб (для network_cpa)
        search_cpa=int(cpa_value_rub) * 1_000_000,            # руб → мкруб (для network_payconv)
        apply_invariants=True,                                  # #3/#4/#5 из CAMPAIGN_INVARIANTS.md
    )
    v501 = cmc.DirectV501Client(token, login)
    campaign_id = None

    def _cleanup_partial(reason: str) -> dict:
        deleted = False
        if campaign_id:
            try:
                v501.delete_campaigns([int(campaign_id)])
                deleted = True
            except Exception:  # noqa: BLE001
                try:
                    deleted = bool(campaign_id in (gc.GridCreateClient(login).delete_campaigns([campaign_id]).get("deleted") or []))
                except Exception:  # noqa: BLE001
                    deleted = False
        return {"ok": False, "name": name, "campaign_id": campaign_id,
                "partial_deleted": deleted, "error": reason[:240]}

    try:
        campaign_id = v501.create_unified_campaign(spec, launch=False)

        # Привязка счётчика Метрики через v501 campaigns.update.
        # Soft-операция: если упадёт — кампания создана, просто без счётчика.
        counter_note = None
        if counter_id:
            try:
                j_upd = _v501_call("update", token, login, {
                    "Campaigns": [{"Id": campaign_id,
                                   "UnifiedCampaign": {"CounterIds": {"Items": [int(counter_id)]}}}]
                })
                upd_errs = ((j_upd.get("result") or {}).get("UpdateResults") or [{}])[0].get("Errors") or []
                if upd_errs:
                    counter_note = f"счётчик {counter_id} не привязался: {upd_errs[0].get('Message','?')}"
            except Exception as e:  # noqa: BLE001
                counter_note = f"счётчик {counter_id} не привязался: {str(e)[:120]}"

        # Минус-слова campaign-level для tp1 (РСЯ) — НЕ ставятся по правилу проекта.
        # Семён: «в tp1 минус-слова не выставляются» (фикс F3 2026-07-22).
        # _apply_campaign_direct_minus пропускается намеренно; для других tp (tp2/tp4/tp5)
        # вызов остаётся в create_set_feed_builders.py и create_set_minus.py.
        _minus_note = None  # tp1: не ставим campaign-level минуса

        # Наполняем бренд-группами из пака M3.
        tp1_build = _build_tp1_from_pack(
            token, login, campaign_id, slepok, site_type, region_ids,
            href, r_code, titles, texts, counter_id=counter_id,
            feed_id=feed_id, with_shopping=with_shopping, feed_models=feed_models,
            segment=segment, ai_title2=ai_title2, city=city, autotarget=autotarget,
            keep_keywords=keep_keywords,
            products_only=products_only, sitelinks=sitelinks, grid_cookie=grid_cookie,
            only_gks=only_gks, only_cts=only_cts,
            all_feeds_list=all_feeds_list,
            # кампания создана строкой выше (v501.create_unified_campaign) → групп в ней 0
            campaign_is_new=True)
        if tp1_build.get("skipped") and tp1_build.get("image_no_pool"):
            _fail = _cleanup_partial(str(tp1_build.get("skipped")))
            _deleted_cid = _fail.get("campaign_id")
            _fail.update({
                "ok": True,
                "skipped": True,
                "image_no_pool": True,
                "campaign_id": None,
                "deleted_campaign_id": _deleted_cid,
                "url": "",
                "tp1_build": tp1_build,
            })
            return _fail
        if tp1_build.get("error") or tp1_build.get("skipped") or not tp1_build.get("adgroups"):
            _details = []
            for _k in ("adgroups", "groups_built", "cts", "keywords", "ads"):
                if tp1_build.get(_k) is not None:
                    _details.append(f"{_k}={tp1_build.get(_k)}")
            _errs = [str(x) for x in (tp1_build.get("errors") or []) if str(x).strip()]
            _warns = [str(x) for x in (tp1_build.get("warnings") or []) if str(x).strip()]
            if _errs:
                _details.append("errors: " + "; ".join(_errs[:3]))
            if _warns:
                _details.append("warnings: " + "; ".join(_warns[:2]))
            _reason = str(tp1_build.get("error") or tp1_build.get("skipped")
                          or ("группы не созданы" + (f" ({'; '.join(_details)})" if _details else "")))
            _fail = _cleanup_partial("tp1 не дозаполнена: " + _reason)
            if tp1_build.get("defer"):
                _fail["defer"] = True   # пустой пак M3 (временный сбой) → докрутка, не permanent-fail
            return _fail
        if not products_only and not tp1_build.get("ads"):
            _details = []
            for _k in ("adgroups", "keywords", "images_uploaded"):
                if tp1_build.get(_k) is not None:
                    _details.append(f"{_k}={tp1_build.get(_k)}")
            _errs = (tp1_build.get("errors") or [])[:3]
            _warns = (tp1_build.get("warnings") or [])[:2]
            if _errs:
                _details.append("errors: " + "; ".join(str(x) for x in _errs))
            if _warns:
                _details.append("warnings: " + "; ".join(str(x) for x in _warns))
            return _cleanup_partial("tp1 не дозаполнена: объявления не созданы"
                                    + (f" ({'; '.join(_details)})" if _details else ""))

        # #6 Фикс пустого текста товарных объявлений (ShoppingAd) в tp1.
        _tp1_default_text = (_trim_clean(texts[0] if texts else "", 81)
                             or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии.")
        _shop_ids = tp1_build.get("shopping_ad_ids") or []
        if with_shopping and feed_id and not _shop_ids:
            # Bug2 graceful (как tp7 whole-feed fallback): фид без офферов/модельных листингов
            # (напр. лендинг-фид) → НЕ удаляем кампанию — в ней валидные TextAd-группы РСЯ.
            # Оставляем как РСЯ без товарки + warning (hard-fail остаётся только «группы не созданы»).
            tp1_build.setdefault("warnings", []).append(
                "товарка не создана: фид без ShoppingAd — оставлена РСЯ без товарных объявлений")
        # Отмена до листингов: кампания+объявления уже в Директе (достаточно консистентны),
        # товарку и ассеты пропускаем — аудит добьёт при необходимости.
        if job and job.get("cancel"):
            print(f"[cancel] {name}: прервано пользователем перед листингами (cid={campaign_id})", flush=True)
            _rd = {"ok": True, "name": name, "campaign_id": campaign_id, "launched": False,
                   "tp1_build": tp1_build, "cancelled_mid": "listings",
                   "url": f"https://direct.yandex.ru/dna/campaign/{campaign_id}?ulogin={login}"}
            if counter_note:
                _rd["counter_note"] = counter_note
            return _rd
        if _shop_ids and feed_id:
            # A3: cookie-only — ShoppingAd создан Grid'ом по куке, token→Grid lag отсутствует → без пауз.
            _gcl = gf.get_grid_client(login, cookie=grid_cookie, cookie_only=True)
            # G review: set_default_text в СВОЁМ try — падение (Яндекс 500) НЕ должно блокировать листинги
            # (раньше оба в одном try → текст падал → листинги пропускались → 0 ListingAd).
            try:
                _gcl.set_default_text(
                    _shop_ids, feed_id, _tp1_default_text,
                    filters_by_ad_id=(tp1_build.get("shopping_filters") or {}),
                )
                tp1_build["shopping_text_set"] = len(_shop_ids)
            except Exception as _e:  # noqa: BLE001
                tp1_build.setdefault("warnings", []).append(f"shopping text: {str(_e)[:120]}")
            # Листинги «Страницы каталога» — НЕЗАВИСИМО от текста (Grid by-shopping, без баллов), затем
            # name-фильтр CONTAINS_ANY [марка|марка+модель] (HAR36 updateListingAds; by-shopping не наследует).
            _grid_add_listings_with_name_filters(
                _gcl, _shop_ids, tp1_build, feed_id, _tp1_default_text,
                apply_global_minus=_apply_global_feed_minus_for_site(site_type),
            )
        if with_shopping and feed_id and _shop_ids and not int(tp1_build.get("listing_ads") or 0):
            # Bug2 graceful: ShoppingAd есть, а листинги «Страницы каталога» пусты (фид-каталог без
            # готовых офферов) → НЕ удаляем кампанию, оставляем товарку без листингов + warning.
            tp1_build.setdefault("warnings", []).append(
                "листинги каталога: 0 ListingAd (по by-shopping) — оставлена товарка без листингов")
        # Гейт (fix-1): ListingAd созданы, но name-фильтр не выставлен ни на одном → каталог
        # показывает весь фид вместо бренда. Детектируется аудитом LISTING_POSITIVE_FILTER_MISSING.
        if (with_shopping and feed_id and _shop_ids
                and int(tp1_build.get("listing_ads") or 0)
                and not int(tp1_build.get("listing_name_set") or 0)):
            tp1_build.setdefault("warnings", []).append(
                "листинги каталога: ListingAd созданы, name-фильтр (listing_name_set=0) НЕ выставлен"
                " — весь фид в каталоге; добьёт аудит LISTING_POSITIVE_FILTER_MISSING")

        result_d = {"ok": True, "name": name, "campaign_id": campaign_id,
                    "launched": False, "tp1_build": tp1_build,
                    "url": f"https://direct.yandex.ru/dna/campaign/{campaign_id}?ulogin={login}"}
        if counter_note:
            result_d["counter_note"] = counter_note
        if _minus_note:
            result_d["minus_note"] = _minus_note
        # Отмена до ассетов: листинги уже выполнены (или пропущены), кампания консистентна.
        if job and job.get("cancel"):
            print(f"[cancel] {name}: прервано пользователем перед ассетами (cid={campaign_id})", flush=True)
            result_d["cancelled_mid"] = "assets"
            return result_d

        # ── Grid-докрутка РСЯ: уточнения/промо/быстрые ссылки на УРОВНЕ КАМПАНИИ ──
        # БАГ-1 FIX (2026-06-24): вынесена в ОТДЕЛЬНЫЙ try/except (ранее была внутри общего
        # try → GridFinalizeError → except Exception → _cleanup_partial УДАЛЯЛ кампанию с
        # 34+ объявлениями!). Теперь финализация best-effort: кампания остаётся, ошибка
        # пишется в result_d["finalize_warn"]. Grid принимает goalId="0" (проверено live).
        a = _resolve_campaign_assets(token, login, href, sitelinks=sitelinks,
                                     slepok=slepok, site_type=site_type,
                                     prefer_callout_texts=callout_texts,
                                     prefer_callout_ids=callout_ids,
                                     grid_cookie=grid_cookie)
        slset = a.get("sitelink_set_id")
        wkl = int(budget_rub) if budget_rub else int(cpa_value_rub) * 10
        _mp_disabled = _enabled_minus_places(slepok)      # #21 минус-площадки РСЯ (v5-путь tp1, общий список, slepok игнорируется)
        try:
            _finalize_rsya(
                login, campaign_id, name=name, goal_id=goal_id or 0,
                cpa_rub=cpa_value_rub, weekly_rub=wkl,
                counter_ids=[counter_id] if counter_id else [],
                pay_for_conversion=(mode == "network_payconv"),
                callout_ids=a["callout_ids"], sitelink_set_id=slset,
                promo_id=(a["promos"][0] if a["promos"] else None),
                minus_set_ids=None, grid_cookie=grid_cookie,
                disabled_places=_mp_disabled)
            result_d["rsya_finalized"] = True
            result_d["callouts_set"] = len(a["callout_ids"])
            result_d["sitelink_set_id"] = slset
        except Exception as _fe:  # noqa: BLE001 — Grid-ошибка не удаляет кампанию (она уже ok)
            result_d["rsya_finalized"] = False
            result_d["finalize_warn"] = f"Grid-финализация (ассеты) не прошла: {str(_fe)[:200]}"
        return result_d
    except cmc.DirectV501Error as e:
        if campaign_id:
            return _cleanup_partial(str(e))
        return {"ok": False, "name": name, "error": str(e)[:240]}
    except Exception as e:  # noqa: BLE001
        if campaign_id:
            return _cleanup_partial(str(e))
        return {"ok": False, "name": name, "error": str(e)[:240]}

def _create_tp1_campaign(
    token: str,
    login: str,
    name: str,
    counter_id: int,
    goal_id: int,
    cpc_cpa: int,
    region_ids: list,
    href: str,
    slepok: str,
    site_type: str,
    r_code: str,
    titles: list | None,
    texts: list,
    feed_id: int = 0,
    with_shopping: bool = False,
    feed_models: dict | None = None,
    budget_rub: int = 0,
    segment: str | None = None,
    ai_title2: str = "",
    sitelinks: list | None = None,
    callout_texts: list | None = None,
    callout_ids: list | None = None,
    city: str = "",
    autotarget: bool = False,
    keep_keywords: bool = False,
    products_only: bool = False,
    no_cpa: bool = False,
    grid_cookie: str | None = None,
    job=None,
    only_gks: set | None = None,
    only_cts: set | None = None,
    all_feeds_list: list | None = None,
) -> dict:
    """Создать ПАРУ кампаний tp1 (РСЯ): cpc-вариант (AVERAGE_CPA) + cpa-вариант (PAY_FOR_CONVERSION).

    no_cpa=True (галочка «под стиль сайта» снята) → создаём ТОЛЬКО cpc-вариант (без оплаты за конверсии).

    segment ('Марки'|'Модели'|None) — какие ct-группы класть в обе кампании пары.

    Канон CODER.md: каждый текстовый tp = ПАРА кампаний (cpc + cpa).
    - tp1_cpc_site: mode='network_cpa'     (Network=AVERAGE_CPA, оплата за клики)
    - tp1_cpa_site: mode='network_payconv' (Network=PAY_FOR_CONVERSION, оплата за конверсии)

    Имя кампании (аргумент name) интерпретируется как канон cpc-варианта:
      'tp1_cpc_site — РСЯ - {cat} - {targ}'
    cpa-вариант получает то же имя с заменой 'tp1_cpc_site' → 'tp1_cpa_site'.

    Группы из пака M3 наполняются в обе кампании (общий slepok/site_type).

    Возвращает {"ok": True, "campaigns": [cpc_result, cpa_result]} или {"ok": False, ...}.
    """
    # Генерим имя cpa-кампании из cpc: замена суффикса оплаты в кодере
    name_cpa = name.replace("tp1_cpc_site", "tp1_cpa_site", 1)

    cpc_result = _create_tp1_single(
        token=token, login=login, name=name, counter_id=counter_id,
        goal_id=goal_id, cpa_value_rub=cpc_cpa, mode="network_cpa",
        region_ids=region_ids, href=href, slepok=slepok,
        site_type=site_type, r_code=r_code, titles=titles, texts=texts,
        feed_id=feed_id, with_shopping=with_shopping, feed_models=feed_models,
        budget_rub=budget_rub, segment=segment, city=city,
        ai_title2=ai_title2, sitelinks=sitelinks,
        callout_texts=callout_texts, callout_ids=callout_ids,
        autotarget=autotarget, keep_keywords=keep_keywords, products_only=products_only,
        grid_cookie=grid_cookie, job=job, only_gks=only_gks, only_cts=only_cts,
        all_feeds_list=all_feeds_list,
    )
    cpa_result = None
    # no_cpa → пропускаем вариант оплаты за конверсии; отмена → cpa тоже пропускаем (cpc уже достроен).
    if not no_cpa and not (job and job.get("cancel")):
        cpa_result = _create_tp1_single(
            token=token, login=login, name=name_cpa, counter_id=counter_id,
            goal_id=goal_id, cpa_value_rub=cpc_cpa, mode="network_payconv",
            region_ids=region_ids, href=href, slepok=slepok,
            site_type=site_type, r_code=r_code, titles=titles, texts=texts,
            feed_id=feed_id, with_shopping=with_shopping, feed_models=feed_models,
            budget_rub=budget_rub, segment=segment, city=city,
            ai_title2=ai_title2, sitelinks=sitelinks,
            callout_texts=callout_texts, callout_ids=callout_ids,
            autotarget=autotarget, keep_keywords=keep_keywords, products_only=products_only,
            grid_cookie=grid_cookie, job=job, only_gks=only_gks, only_cts=only_cts,
            all_feeds_list=all_feeds_list,
        )
    # Сводный результат: ok=True если хоть одна создалась
    ok = cpc_result.get("ok") or (bool(cpa_result) and cpa_result.get("ok"))
    _children = [c for c in ([cpc_result] + ([cpa_result] if cpa_result else [])) if c]
    _all_skipped = bool(_children) and all(bool(c.get("skipped")) for c in _children)
    # Обратная совместимость с api_create_set: возвращаем campaign_id первой созданной
    first_id = cpc_result.get("campaign_id") or (cpa_result.get("campaign_id") if cpa_result else None)
    out = {
        "ok": ok, "name": name, "campaign_id": first_id,
        "launched": False,
        "campaigns": _children,
        "url": (cpc_result.get("url") or (cpa_result.get("url") if cpa_result else "") or ""),
    }
    if _all_skipped:
        out["skipped"] = True
        out["image_no_pool"] = any(bool(c.get("image_no_pool")) for c in _children)
    if not ok:
        # Обе кампании пары упали → поднимаем РЕАЛЬНУЮ причину наверх (иначе UI показывает пустое «()»).
        _errs = [c.get("error") for c in out["campaigns"] if c and c.get("error")]
        out["error"] = ("; ".join(dict.fromkeys(_errs))[:240]
                        or "tp1: кампании пары не создались (причина не определена)")
    return out

def _grid_account_image_hashes(login: str) -> dict:
    """{image_name: imageHash} картинок, УЖЕ загруженных в аккаунт — читается ПО КУКЕ через Grid
    (БЕЗ баллов). Name = basename файла M3 (upload_image кладёт Name=os.path.basename(path)).
    Нужно куки-пути РСЯ (tp1): при 0 баллов залить НОВУЮ картинку нельзя (adimages.add → 152),
    но ПЕРЕИСПОЛЬЗОВАТЬ хэш уже залитой (предыдущими v5-созданиями) — можно. Покрытие растёт по
    мере «созревания» аккаунта. Мягкая деградация: нет куки/ошибка → {} (создаём без картинок)."""
    import requests as _rqs
    import re as _re
    try:
        cookie = cmc.pick_working_cookie(login)
    except Exception:  # noqa: BLE001
        return {}
    if not cookie:
        return {}
    sess = _rqs.Session()
    sess.verify = False
    csrf = {"t": None}

    def _g(op, q, var):
        h = {"Cookie": cookie, "dna-operation-name": op, "x-direct-api": "1", "x-detected-locale": "ru",
             "Content-Type": "application/json", "User-Agent": cmc.USER_AGENT, "Origin": "https://direct.yandex.ru",
             "Referer": f"https://direct.yandex.ru/dna/grid/campaigns?ulogin={login}"}
        if csrf["t"]:
            h["x-csrf-token"] = csrf["t"]
        r = sess.post(f"{_GRID_URL}?operationName={op}&ulogin={login}",
                      json={"operationName": op, "query": q, "variables": var}, headers=h, timeout=40)
        if r.status_code == 403:
            m = _re.search(r"_direct_csrf_token=([^;,\s]+)", r.headers.get("Set-Cookie", ""))
            t = r.cookies.get("_direct_csrf_token") or (m.group(1) if m else None)
            if t:
                csrf["t"] = t
                r = sess.post(f"{_GRID_URL}?operationName={op}&ulogin={login}",
                              json={"operationName": op, "query": q, "variables": var}, headers=h, timeout=40)
        return r

    try:
        _g("Callouts", "query Callouts($login:String!){callouts(input:{searchBy:{login:$login}"
           "filter:{deleted:false}}){id}}", {"login": login})
        camp_ids = [c["id"] for c in _grid_list_campaigns(login) if c.get("id")]
    except Exception:  # noqa: BLE001
        return {}
    A = ("query A($login:String!,$inp:GdAdsContainerInput!){client(searchBy:{login:$login}){"
         "ads(input:$inp){rowset{id ...on GdAdaptiveTextAd{images{imageHash name}}}}}}")
    out: dict = {}
    for i in range(0, len(camp_ids), 100):
        inp = {"filter": {"campaignIdIn": [str(x) for x in camp_ids[i:i + 100]]},
               "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [], "useCampaignGoalIds": True},
               "limitOffset": {"limit": 5000, "offset": 0}, "orderBy": [{"order": "ASC", "field": "ID"}]}
        try:
            d = _g("A", A, {"login": login, "inp": inp}).json()
        except Exception:  # noqa: BLE001
            continue
        if d.get("errors"):
            continue
        for ad in (((d.get("data") or {}).get("client") or {}).get("ads") or {}).get("rowset") or []:
            for im in (ad.get("images") or []):
                if im.get("name") and im.get("imageHash"):
                    out.setdefault(im["name"], im["imageHash"])
    return out


# ── Кэш account-map картинок {basename: imageHash} ПО ЛОГИНУ (процесс-глобальный) ──────────
# Зачем: `_grid_account_image_hashes` читает ВСЕ кампании + ВСЕ объявления аккаунта, а звался
# на КАЖДОЙ cookie-кампании набора (tp1 :_create_tp1_via_cookie, tp2/tp4 :_create_search_via_cookie)
# → стоимость росла квадратично по ходу набора (аккаунт пухнет с каждой созданной РК).
# Теперь чтение одно на логин/TTL, а результаты доливаются мёржем.
#
# Почему МЁРЖ, а не замена: `_grid_account_image_hashes` видит только картинки, ПРИВЯЗАННЫЕ К
# ОБЪЯВЛЕНИЯМ. После `delete_drafts` объявлений нет → чтение отдаёт ~0, хотя сами картинки
# остаются в БИБЛИОТЕКЕ логина и их imageHash по-прежнему валиден. Замена обнулила бы карту и
# заставила перезаливать весь набор (живой профиль: 829 файлов / 134 с на прогон). Мёрж это чинит.
#
# Инвалидация — TTL (по умолчанию 30 мин, env `DIRECT_ACC_IMG_MAP_TTL`) + явный `_account_image_map_drop`.
# ⚠️ Осознанный риск: imageHash, удалённый из кабинета ВРУЧНУЮ между чтением и использованием,
# останется в кэше до истечения TTL и даст отказ Директа на создании объявления. TTL ограничивает
# окно; `_account_image_map_drop(login)` — принудительный сброс. Автосброс по тексту ошибки Директа
# НЕ реализован: сигнатура отказа «неизвестный imageHash» не подтверждена фактом (гадать не стал).
_ACC_IMG_MAP: dict[str, dict] = {}
_ACC_IMG_MAP_TS: dict[str, float] = {}
_ACC_IMG_MAP_LOCK = threading.Lock()
_ACC_IMG_MAP_TTL = float(os.environ.get("DIRECT_ACC_IMG_MAP_TTL", "1800"))
_ACC_IMG_MAP_STATS = {"hit": 0, "read": 0}


def _account_image_map(login: str, force: bool = False) -> dict:
    """Кэшированная {basename: imageHash} по логину. Возвращает КОПИЮ (вызывающие мутируют карту).
    force=True — принудительно перечитать аккаунт (мёрж поверх накопленного)."""
    if not login:
        return {}
    if not force:
        _snap = _msg = None
        with _ACC_IMG_MAP_LOCK:
            _m = _ACC_IMG_MAP.get(login)
            if _m is not None and (time.time() - _ACC_IMG_MAP_TS.get(login, 0.0)) < _ACC_IMG_MAP_TTL:
                _ACC_IMG_MAP_STATS["hit"] += 1
                _hn = _ACC_IMG_MAP_STATS["hit"]
                _snap = dict(_m)
                # HIT частый — печатаем каждый 25-й (как [img-cache]): журнал не засоряем,
                # но видно, что кэш живой. print — ВНЕ лока (блокирующий write в забитый pipe
                # держал бы общий лок кэша).
                _msg = ((f"[img-accmap] HIT total={_hn} reads={_ACC_IMG_MAP_STATS['read']} "
                         f"{login} entries={len(_snap)}") if _hn % 25 == 0 else None)
        if _snap is not None:                  # промах/протухший TTL → падаем в чтение аккаунта ниже
            if _msg:
                try:
                    print(_msg, flush=True)
                except Exception:  # noqa: BLE001
                    pass
            return _snap
    try:
        fresh = _grid_account_image_hashes(login) or {}
    except Exception:  # noqa: BLE001 — мягкая деградация как у сырого читателя
        fresh = {}
    with _ACC_IMG_MAP_LOCK:
        base = dict(_ACC_IMG_MAP.get(login) or {})
        base.update(fresh)                     # мёрж: пережить обнуление после delete_drafts
        _ACC_IMG_MAP[login] = base
        _ACC_IMG_MAP_TS[login] = time.time()
        _ACC_IMG_MAP_STATS["read"] += 1
        _rn = _ACC_IMG_MAP_STATS["read"]
        _snap = dict(base)
    try:
        print(f"[img-accmap] READ total={_rn} hits={_ACC_IMG_MAP_STATS['hit']} {login} "
              f"account={len(fresh)} merged={len(_snap)}", flush=True)
    except Exception:  # noqa: BLE001
        pass
    return _snap


def _account_image_map_merge(login: str, mapping: dict | None) -> int:
    """Долить в кэш логина только что ЗАЛИТЫЕ картинки {basename: imageHash}, чтобы следующая
    кампания набора переиспользовала их без сетевой заливки. → сколько новых записей добавлено."""
    if not login or not mapping:
        return 0
    added = 0
    with _ACC_IMG_MAP_LOCK:
        base = _ACC_IMG_MAP.setdefault(login, {})
        for bn, h in (mapping or {}).items():
            if bn and h and base.get(bn) != h:
                base[bn] = h
                added += 1
        if login not in _ACC_IMG_MAP_TS:
            _ACC_IMG_MAP_TS[login] = time.time()
    return added


def _account_image_map_drop(login: str | None = None) -> None:
    """Сбросить кэш account-map: конкретного логина или весь. Звать, если Директ отказал по
    неизвестному imageHash или картинки чистили в кабинете вручную."""
    with _ACC_IMG_MAP_LOCK:
        if login:
            _ACC_IMG_MAP.pop(login, None)
            _ACC_IMG_MAP_TS.pop(login, None)
        else:
            _ACC_IMG_MAP.clear()
            _ACC_IMG_MAP_TS.clear()


def _preupload_tp1_images(login: str, items: list, site_type: str, slepok: str,
                          grid_cookie: str | None = None) -> dict:
    """Набор-level ПРЕД-ЗАЛИВКА картинок tp1 (РСЯ) в библиотеку аккаунта ОДИН раз, ДО цикла
    создания РК, чтобы создание каждой РК не блокировалось на аплоаде ассетов.

    Собирает уникальные пути картинок tp1-пунктов набора ТЕМ ЖЕ резолвером, что и цикл:
    если план уже содержит camp_names-routing ``tp1_only_cts`` — берём только реально выбранные
    ct; иначе используем безопасный segment-filter. На каждый ct берём максимум 5 картинок.
    Дедупит против уже залитых в аккаунт (_grid_account_image_hashes,
    0 баллов) и догружает недостающее параллельно через _parallel_upload_images (Grid/куки,
    без баллов). Побочный эффект — прогрев процесс-глобального _GRID_IMG_HASH_CACHE
    (ключ login+realpath): per-РК заливка в цикле (Фаза 3.4 _build_tp1_adgroups и куки-путь
    _create_tp1_via_cookie) переиспользует хэши БЕЗ повторной сети.

    ⚠ Только tp1_rsy: реальную Grid-заливку креативов делает лишь tp1 — tp5 картинки не грузит
    (они у ShoppingAd/ListingAd), tp2/tp4 картинки запрещены правилом, tp3 без креативов.

    Грациозность: любой сбой = частичный/пустой прогрев, а цикл сам зальёт недостающее per-РК
    (кэш-промах → штатная заливка, НЕ падаем). Идемпотентно и почти no-op на «созревшем»
    аккаунте (всё уже в account_map)."""
    out = {"cts": 0, "paths": 0, "resolved": 0, "explicit_cts": 0}
    try:
        tp1_items = [it for it in (items or [])
                     if isinstance(it, dict) and (it.get("type") or "") == "tp1_rsy"]
        if not tp1_items:
            return out
        key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
        explicit_cts: set[str] = set()
        for it in tp1_items:
            explicit_cts.update(str(x).strip() for x in (it.get("tp1_only_cts") or []) if str(x).strip())
        # Сегменты, реально запрошенные пунктами (None = без сегментного фильтра → все ct).
        segments = {it.get("tp1_segment") for it in tp1_items}
        want_all = None in segments
        seg_set = {s for s in segments if s}
        try:
            pack = kp.gather(key, site_type, "tp1") or {}     # тот же ssh-вызов к M3, что и цикл
        except Exception:  # noqa: BLE001
            pack = {}
        if not pack:
            return out
        seen_ct: set = set()
        all_paths: list = []
        for ct in pack.keys():
            if explicit_cts and ct not in explicit_cts:
                continue
            data = pack.get(ct) or {}
            if not data.get("positive"):
                continue                                      # цикл пропускает ct без ключей — тоже
            if not want_all and seg_set and _ct_segment(ct) not in seg_set:
                continue                                      # тот же сегментный фильтр, что в цикле
            if ct in seen_ct:
                continue
            seen_ct.add(ct)
            try:
                paths = _creative_images_for_ct(site_type, "tp1", ct, key) or []  # резолвинг как есть
            except Exception:  # noqa: BLE001
                paths = []
            all_paths.extend(paths[:5])                       # лимит 5 картинок/ct — как в цикле
        out["cts"] = len(seen_ct)
        out["paths"] = len(all_paths)
        out["explicit_cts"] = len(explicit_cts)
        if not all_paths:
            return out
        # Дедуп против уже залитых В АККАУНТ (0 баллов): _parallel_upload_images(account_map=…)
        # переиспользует их хэши без сетевой заливки, догружает только недостающее.
        # Прогрев набора — единственное место, где account-map читается ПРИНУДИТЕЛЬНО (force=True):
        # набор начинается, аккаунт мог измениться с прошлого прогона. Дальше по циклу создания все
        # берут кэш (`_account_image_map` без force) — раньше каждая cookie-кампания перечитывала
        # весь аккаунт заново.
        account_map = _account_image_map(login, force=True)
        gc_img = gf.get_grid_client(login, cookie=grid_cookie)    # per-thread клиент (как per-РК)
        resolved = _parallel_upload_images(gc_img, login, all_paths, account_map=account_map)
        out["resolved"] = len(resolved or {})
        # Свежезалитое — сразу в кэш логина: следующая кампания набора возьмёт хэш отсюда, а не по сети.
        _account_image_map_merge(login, resolved)
        try:
            print(f"[img-preupload] {login}: tp1 набор — ct={out['cts']} explicit_cts={out['explicit_cts']} paths={out['paths']} "
                  f"resolved={out['resolved']} (account_lib={len(account_map)}) — кэш прогрет", flush=True)
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001 — прогрев не смеет ронять/трогать джобу
        try:
            print(f"[img-preupload] {login}: прогрев картинок не удался (best-effort): {str(e)[:160]}",
                  flush=True)
        except Exception:  # noqa: BLE001
            pass
    return out


_STRUCT_CT_NAME_CACHE: dict = {}


def _struct_ct_names(slepok: str, site_type: str) -> dict:
    """{ctNNNN: имя темы из структуры слепка} — ТОЛЬКО для НЕ-авто слепков (dmp и будущих B2B).
    Имя группы у них берём из структуры (= выгрузка кабинета: «Идентификация», «Определение»…),
    а НЕ из авто-фида `feeds_ct_model` (он давал «Авто» всем ct — дубли имён групп, инцидент 2026-07-12).
    Для авто-слепков возвращает {} → caller сохраняет прежнее поведение (ct_model).

    ⚠️ У caller'ов эта карта имеет ПРИОРИТЕТ над `_ag_part1_map()` (справочник марок): авто-ct
    может совпасть с темой не-авто слепка (ct0084 = авто «Faw Bestune T77» и dmp «Конкуренты»), и тогда
    справочник подменил бы B2B-тему марка-брендом. Приоритет исправлен 2026-07-18."""
    import re as _ren
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    ck = (key, site_type or "")
    if ck in _STRUCT_CT_NAME_CACHE:
        return _STRUCT_CT_NAME_CACHE[ck]
    out: dict = {}
    try:
        from . import slepki_store as _ss   # структура из per-slepok файлов (assemble)
        d = _ss.assemble()
        dl = next((x for x in d.get("directologists", []) if x.get("key") == key), None)
        if dl and dl.get("auto", True) is False:          # имя из структуры — ТОЛЬКО не-авто
            st = next((s for s in dl.get("site_types", []) if s.get("name") == site_type), None)
            for tp in (st.get("tp", []) if st else []):
                blocks = tp.get("splits") or ([{"groups": tp.get("groups", [])}] if tp.get("groups") else [])
                for sp in blocks:
                    for g in sp.get("groups", []):
                        for it in (g.get("items") or []):
                            if not isinstance(it, dict):
                                continue
                            m = _ren.search(r"ct\d{4}", it.get("gc") or "")
                            nm = (it.get("t") or g.get("name") or "").strip()
                            if m and nm and m.group(0) not in out:
                                out[m.group(0)] = nm
    except Exception:  # noqa: BLE001
        out = {}
    _STRUCT_CT_NAME_CACHE[ck] = out
    return out


# Транслит-артефакты харвеста (латиница-имитация кириллицы: «Abto Py»=«Авто Ру» auto.ru,
# «Abto»=«Авто»). В режиме групп 1в1 (_multi) имя берётся из структурного `t` как есть → в
# кабинет уезжала ломаная кириллица. Нормализуем при чтении. (Fix 3а, зеркало text_builders)
_STRUCT_NAME_FIXES = {
    "abto py": "Авто Ру",
    "abto py.": "Авто Ру",
    "abto": "Авто",
}

def _norm_struct_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return s
    fixed = _STRUCT_NAME_FIXES.get(s.lower())
    if fixed:
        return fixed
    return re.sub(r"(?i)\bAbto\b", "Авто", s)

def _struct_items(slepok: str, site_type: str, tp_code: str) -> list:
    """per-adgroup 1в1: по одному элементу на структурный item (БЕЗ дедупа ct).
    → [{"ct":ctNNNN, "gk":<group-slug>, "name":<имя из структуры>}]. gk — авторитетное поле
    item (``gk``) ИЛИ выведенное из ``gc`` через kp._group_slug. Формат splits (dmp) → []."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    try:
        from . import slepki_store as _ss   # структура из per-slepok файлов (assemble)
        d = _ss.assemble()
    except Exception:  # noqa: BLE001
        return []
    dl = next((x for x in d.get("directologists", []) if x.get("key") == key), None)
    if not dl:
        return []
    st = next((s for s in dl.get("site_types", []) if s.get("name") == site_type), None)
    if not st:
        return []
    items = []
    for tp in st.get("tp", []):
        if tp.get("code") != tp_code:
            continue
        for grp in tp.get("groups", []):          # только формат groups (splits/dmp — не сюда)
            for it in grp.get("items", []):
                gc = it.get("gc", "")
                m = re.search(r"ct\d{4}", gc or "")
                ct = m.group(0) if m else ""
                if not ct or ct == "ct0000":
                    continue
                gk = (it.get("gk") or kp._group_slug(gc))
                items.append({"ct": ct, "gk": gk,
                              "name": _norm_struct_name(it.get("t") or grp.get("name") or "")})
    return items


def _struct_ct0000_units(slepok: str, site_type: str, tp_code: str, only_gks) -> list:
    """Структурные группы кампании, лежащие ЦЕЛИКОМ на ct0000 → [(ct0000, gk, имя)].

    Явный маркер «структурный узел на ct0000» для camp_names-маршрута: `_struct_items` ct0000
    пропускает НАМЕРЕННО (там перечисляются модель-ct), поэтому у такого узла и `only_cts`
    (`structure_to_campaigns.cts`) пуст. Без этого перечня билдер трактовал пустой ct-фильтр как
    «фильтра нет» и брал ВЕСЬ пак: узел «Агрегаторы» (1 группа) давал 27 групп кабинета.

    Строго по ``only_gks`` кампании и строго ct0000 (ct берётся из `gc` item'а). Пустой
    ``only_gks`` / нечитаемая структура → [] (вызывающий остаётся на прежнем поведении).
    """
    _og = {str(g).strip() for g in (only_gks or ()) if str(g).strip()}
    if not _og:
        return []
    try:
        from .create_set_structure import _load_struct as _ls, _slepok_key as _sk
        _d = _ls()
        _key = _sk(slepok)
        _dl = next((x for x in (_d.get("directologists") or []) if x.get("key") == _key), None)
        _st = next((s for s in ((_dl.get("site_types") or []) if _dl else [])
                    if s.get("name") == site_type), None)
    except Exception:  # noqa: BLE001 — чтение структуры не должно ронять создание
        return []
    out: list = []
    seen: set = set()
    for _tp in ((_st.get("tp") or []) if _st else []):
        if (_tp.get("code") or "") != tp_code:
            continue
        for _grp in (_tp.get("groups") or []):
            for _it in (_grp.get("items") or []):
                _gc = _it.get("gc") or ""
                _m = re.search(r"ct\d{4}", _gc)
                if not _m or _m.group(0) != "ct0000":
                    continue
                _gk = str(_it.get("gk") or kp._group_slug(_gc) or "").strip()
                if not _gk or _gk not in _og or _gk in seen:
                    continue
                seen.add(_gk)
                out.append(("ct0000", _gk,
                            _norm_struct_name(_it.get("t") or _grp.get("name") or "")))
    return out


def _tp1_pack_groups(login: str, slepok: str, site_type: str, r_code: str, href: str,
                     titles: list | None, texts: list,
                     segment: str | None = None, ai_title2: str = "", city: str = "",
                     with_shopping: bool = False, tp_code: str = "tp1",
                     image_map: dict | None = None, autotarget: bool = False,
                     keep_keywords: bool = False,
                     feed_url_by_model: dict | None = None,
                     only_cts: list[str] | None = None,
                     only_gks: set | None = None) -> list:
    """Бренд-группы tp1/tp5 из пака M3 — ЧИСТО данные (без API-вызовов, без баллов). Зеркало
    группо-сборки _build_tp1_from_pack (см. там), вынесено для куки-пути (grid_create.create_full).
    image_map (РСЯ tp1): {basename→imageHash} уже залитых картинок аккаунта — переиспользуем хэши
    (картинку при 0 баллов залить нельзя). Источник картинок — как в v5 (_build_tp1_from_pack:
    read_slepok_images ∥ read_images), basename матчим с image_map.
    → [{name, ct, brand, keywords, minus, titles, texts, href[, image_hashes]}]."""
    import os as _os
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    # #3 (решение Семёна): tp4 = те же кампании, что tp2 (отличие — только галочка «Динамика»). Пак
    # tp4 беднее (были группы с 1 ключом) → ИСТОЧНИК ГРУПП/КЛЮЧЕЙ для tp4 берём из tp2-пака. Алиас
    # касается ТОЛЬКО `kp.gather`; место показа (organic=True), нейминг/кодер, тип Поиск+Динамика,
    # корректировки и контент tp4 остаются tp4 (ниже `tp_code` не подменяется).
    _pack_tp = "tp2" if tp_code == "tp4" else tp_code
    pack = kp.gather(key, site_type, _pack_tp)
    if not pack:
        return []
    text0 = _trim_clean(texts[0] if texts else "", 81) or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии."
    ct_model = kp.feeds_ct_model()
    ct_name = _ag_part1_map()
    _sc_titles = _slepok_campaign_content(slepok, site_type).get("titles") or []  # пул слепка — 1 раз
    # Минус-марки/модели: вычисляем один раз на кампанию для O(1) проверки.
    # Марки: канонизируем в латиницу (_brand_canon). Модели: карта {mark_canon → set(model_lower)}.
    # Допущение: марки односложные (BAIC/Chery/MG/Lada) — split()[0] = бренд, split()[1:] = модель.
    _minus_m_set: set = {_brand_canon(str(m).strip().lower()) for m in (_enabled_minus_marks() or []) if str(m).strip()}
    _minus_mod_by_brand: dict = _build_minus_mod_by_brand(_enabled_minus_model_pairs() or [])
    # only_cts (split-driven слепки, напр. dmp/tp2): наполняем ТОЛЬКО ct-кодами этого split-блока
    # из плана (create_set_plan "tp2_split_cts"). Без него cookie-путь брал ВСЕ ct пака (34 у dmp)
    # в каждую split-кампанию. None → авто-слепки, поведение неизменно (весь пул × segment-фильтр).
    _only_cts = {c for c in (only_cts or []) if c} or None
    _only_gks = {g for g in (only_gks or []) if g} or None
    _struct_names = _struct_ct_names(slepok, site_type)   # не-авто: имя группы из структуры/выгрузки, не авто-фид
    _units: list[tuple[str, str, str]] = []
    if _only_gks is not None:
        try:
            from .create_set_structure import _load_struct as _ls_gk, _slepok_key as _sk_gk
            _sd_gk = _ls_gk()
            _dl_gk = next((x for x in (_sd_gk.get("directologists") or [])
                           if x.get("key") == _sk_gk(slepok)), None)
            _st_gk = next((s for s in ((_dl_gk.get("site_types") or []) if _dl_gk else [])
                           if s.get("name") == site_type), None)
            for _tp_gk in ((_st_gk.get("tp") or []) if _st_gk else []):
                if _tp_gk.get("code") != tp_code:
                    continue
                for _grp_gk in (_tp_gk.get("groups") or []):
                    for _it_gk in (_grp_gk.get("items") or []):
                        _igk = (_it_gk.get("gk") or kp._group_slug(_it_gk.get("gc") or "") or "").strip()
                        if _igk not in _only_gks:
                            continue
                        _m_ct = re.search(r"ct\d{4}", _it_gk.get("gc") or "")
                        _ct_gk = _m_ct.group(0) if _m_ct else ""
                        if not _ct_gk:
                            continue
                        if _only_cts is not None and _ct_gk not in _only_cts:
                            continue
                        _units.append((_ct_gk, _igk,
                                       _norm_struct_name(_it_gk.get("t") or _grp_gk.get("name") or "")))
                break
        except Exception:  # noqa: BLE001 — structure read failure falls back to ct-based route
            _units = []
    if not _units:
        _units = [(ct, "", "") for ct in sorted(pack.keys())
                  if (_only_cts is None or ct in _only_cts)]
    groups = []
    # Pre-pass: прогрев кэша link_check параллельно (6 потоков). Основной цикл ниже
    # вызывает _resolve_url(model_href) — теперь это будет cache-hit. (#LINK_CHECK_404_FALLBACK)
    _batch_hrefs: list[str] = []
    for _pct, _pgk, _puname in _units:
        _pgrp_pack = (pack.get(_pct, {}).get("_groups") or {}).get(_pgk) if _pgk else None
        _pdata = _pgrp_pack or pack.get(_pct) or {}
        if not _pdata.get("positive"):
            continue
        if segment and _ct_segment(_pct) != segment:
            continue
        _praw = ((_struct_names.get(_pct) or ct_name.get(_pct) or _pct) if _struct_names
                 else (_puname or ct_name.get(_pct) or ct_model.get(_pct) or _pct))
        _pbrand = _valid_pack_brand_name(_pct, _praw)
        # ФИКС-B pre-pass: per-model (_pgk + _puname) → формульный href (аналогично v5-пути).
        _batch_hrefs.append(_model_page_href(_site_root_href(href), site_type, _puname)
                            if (_pgk and _puname)
                            else _pack_group_href(_pct, _pbrand, feed_url_by_model, href, site_type))
    _resolve_urls_batch(_batch_hrefs)

    for ct, _gk, _uname in _units:
        _grp_pack = (pack.get(ct, {}).get("_groups") or {}).get(_gk) if _gk else None
        data = _grp_pack or pack.get(ct) or {}
        if not data.get("positive"):
            continue
        if segment and _ct_segment(ct) != segment:
            continue
        # не-авто (dmp): имя = структура t(выгрузка) → leadgen(описание кодера) → ct; НИКОГДА авто-фид «Авто».
        # Структура ПЕРВАЯ: _ag_part1_map мёржит авто-справочник gsheet_naming, и авто-ct может
        # совпасть с темой слепка (ct0084: авто «Faw Bestune T77» ↔ dmp «Конкуренты») → тема стала бы маркой.
        if _struct_names:
            raw_brand = _struct_names.get(ct) or _uname or ct_name.get(ct) or ct
        elif _gk:
            raw_brand = _uname or ct_name.get(ct) or ct_model.get(ct) or ct
        else:
            raw_brand = ct_name.get(ct) or ct_model.get(ct) or ct
        # Минус-фильтр групп: марка/модель отмечена в «Глобальных правилах» → группа не создаётся
        if _minus_m_set or _minus_mod_by_brand:
            _rb_tok = (raw_brand or "").strip().split()[0].lower()
            _rb_canon = _brand_canon(_rb_tok) if _rb_tok else ""
            _model_portion = " ".join((raw_brand or "").strip().split()[1:]).lower()
            if ((_rb_canon and _rb_canon in _minus_m_set) or
                    (_rb_canon and _model_portion and
                     _model_portion in _minus_mod_by_brand.get(_rb_canon, set()))):
                print(f"[minus-filter] group skipped: ct={ct} brand={raw_brand!r} tp={tp_code}", flush=True)
                continue
        brand = _valid_pack_brand_name(ct, raw_brand)
        group_label = _pack_group_display_name(ct, raw_brand, brand)
        # tp2/tp4 — поисковые группы: в кодере используем aoff, не сетевой tp1-формат.
        _is_search_tp = tp_code in ("tp2", "tp4")
        # dmp/не-авто (гейт _struct_names): имя = чистая тема (group_label), без gc-хвоста и «Авто».
        # Авто-слепки: прежний кодер-нейминг. (DMP_GROUP_NAMES_AVTO)
        group_name = (group_label
                      if (_struct_names and _is_search_tp)
                      else _text_group_name(ct, r_code, group_label)
                      if _is_search_tp
                      else _tp1_group_name(ct, r_code, group_label, with_shopping=with_shopping,
                                           autotarget=autotarget, tp_code=tp_code))
        # deep-link: фид → формульный слаг. ФИКС-A: Марки→/auto/{brand}, Модели→полный путь.
        # 404-фолбэк: кэш прогрет pre-pass (batch 6 потоков) → cache-hit. (#LINK_CHECK_404_FALLBACK)
        # ФИКС-B: per-model (_gk + _uname): аналогично v5-пути — формульный href минуя feed-lookup.
        model_href = (_resolve_url(_model_page_href(_site_root_href(href), site_type, _uname))
                      if (_gk and _uname)
                      else _resolve_url(_pack_group_href(ct, brand, feed_url_by_model, href, site_type)))
        is_brand_group = _ct_segment(ct) in ("Марки", "Модели")
        title = (_title_from_template(brand, city, slepok=slepok, site_type=site_type) if (is_brand_group and not ai_title2)
                 else (_GENERIC_AT_TITLES[0] if not is_brand_group else brand[:35]))
        ttl2 = (ai_title2[:30] if ai_title2 else _next_title2())
        # Cookie/Grid-путь не должен делать M3-вызов на каждую ct-группу: это и было источником
        # зависания боевого create_set после restart. ИИ остаётся на уровне item, а группа берёт
        # локально собранный набор в том же стиле.
        _gt = _rsya_titles(brand, city, site_type, ai_title2=ai_title2, slepok=slepok,
                           base=(list(titles or []) + [title, ttl2] if is_brand_group
                                 else list(titles or []) + list(_GENERIC_AT_TITLES)),
                           pool=_sc_titles, is_brand=is_brand_group)
        _gx = _rsya_texts([t for t in (list(texts or []) + ([text0] if text0 else [])) if t], site_type, city, brand)
        _gt, _gx, _sl_dummy, _pay_changed = _coherent_payments(_gt, _gx, [])
        _keywords = _filter_group_keywords(data.get("positive", []), _ct_segment(ct), brand, city, site_type,
                                           model=(_uname if (_gk and _uname) else brand))
        if _is_search_tp and ((not autotarget) or keep_keywords) and not _keywords:
            continue
        g = {
            "name": group_name, "ct": ct, "brand": brand, "seg": _ct_segment(ct),  # 'Марки' → цена=МИН по марке
            # БАГ-13: для «Марки» — убрать ключи «марка+модель»; model= защищает «Модели» от чужих моделей
            "keywords": _keywords,
            "minus": [],   # группа: минуса сняты — campaign-level через spec (tp1 куки) / _apply_campaign_direct_minus (tp1/tp5 v5)
            "titles": _gt or [t for t in ([title, brand] if brand else [title]) if t],
            "texts": _gx or ([text0] if text0 else []),
            "href": model_href,
        }
        # ПОИСКОВЫЕ tp2/tp4: объявления БЕЗ картинок (IMAGES_FORBIDDEN — правило Семёна).
        # Раньше куки-путь цеплял image_hashes и поисковым → 27 объявл./кампанию с картинками
        # (инцидент 03.07.2026, докрутка psm5h7q6) → аудит чистил постфактум. Гейт у источника.
        _want_images = tp_code not in ("tp2", "tp4")
        _all_imgs = _creative_images_for_ct(site_type, tp_code, ct, key) if _want_images else []
        if _all_imgs:
            g["image_paths"] = _all_imgs[:5]
        # РСЯ-картинки по куке (БЕЗ баллов): источник — пак M3 + Manual-добивка.
        # basename → hash из image_map (уже залитые в аккаунт, без баллов). Найденные → imageHashes.
        # Fallback по другим слепкам для того же ct (не менять ct — ct0000 ЗАПРЕЩЁН).
        if image_map and _want_images:
            _hh = [image_map.get(_os.path.basename(p)) for p in _all_imgs]
            _hh = [h for h in _hh if h]
            if _hh:
                g["image_hashes"] = _hh[:5]
        groups.append(g)
    return groups

def _pack_groups_with_retry(login: str, slepok: str, site_type: str, r_code: str, href: str,
                            titles, texts, *, retries: int = 2, **kw) -> tuple[list, bool]:
    """`_tp1_pack_groups` с КОРОТКИМИ ретраями (M3-пак мог быть ВРЕМЕННО недоступен — sshfs/relay).
    Пустой пак больше НЕ повод для мгновенного permanent-fail. Бюджет ОГРАНИЧЕН: это вызывается и на
    СИНХРОННОМ route /api/create_set — длинные sleep вешали бы запрос. Worst-case ~0.5с sleep + ~3с
    статус M3. → (groups, m3_alive); m3_alive=False → пак пуст И M3 лежит → caller отправит в deferred."""
    groups: list = []
    for _i in range(max(1, int(retries))):
        try:
            groups = _tp1_pack_groups(login, slepok, site_type, r_code, href, titles, texts, **kw)
        except Exception as _e:  # noqa: BLE001 — сбой чтения пака считаем как «пусто», ретраим
            groups = []
        if groups:
            return groups, True
        if _i < retries - 1:
            time.sleep(0.5)                               # короткий backoff (не вешать sync-route)
    # Пусто после ретраев — жив ли M3 (единый источник правды о статусе)? Логируем для диагностики.
    try:
        _m3 = _m3_content_status(timeout=3.0)
    except Exception:  # noqa: BLE001
        _m3 = {"ok": False, "detail": "статус M3 не прочитан"}
    _alive = bool(_m3.get("ok"))
    print(f"[pack-empty] slepok={slepok} site_type={site_type} tp_retry={retries} "
          f"M3_alive={_alive} detail={_m3.get('detail')}", flush=True)
    return [], _alive

def _create_tp1_via_cookie(
    login: str, name: str, counter_id: int, goal_id: int, cpc_cpa: int,
    region_ids: list, href: str, slepok: str, site_type: str, r_code: str,
    titles: list | None, texts: list, budget_rub: int = 0, segment: str | None = None,
    ai_title2: str = "", city: str = "", autotarget: bool = False, no_cpa: bool = False,
    keep_keywords: bool = False,
    token: str = "", corr: dict | None = None, ret_map: dict | None = None,
    callout_texts: list | None = None, sitelinks: list | None = None,
    callout_ids: list | None = None,
    feed_id: int = 0, with_shopping: bool = False, feed_models: dict | None = None,
    products_only: bool = False,
    job=None, only_gks: set | None = None, only_cts: set | None = None,
    all_feeds_list: list | None = None,
) -> dict:
    """tp1 РСЯ ПО КУКЕ (без баллов v5) — когда исчерпан лимит (152) и пользователь согласился через
    попап. Кампания+группы+комбинаторные объявления через grid_create.create_full.
    При наличии фида добиваем ShoppingAd+ListingAd через Grid, как и на token-path.
    → {"ok", "campaign_id", "campaigns":[...], "via":"cookie"} (форма как у _create_tp1_campaign)."""
    import datetime as _dt
    # РСЯ-картинки по куке: переиспользуем хэши уже залитых в аккаунт картинок (basename→hash).
    # При 0 баллов залить новую нельзя (adimages.add=152), но reuse — без баллов. Best-effort: {} → без картинок.
    _img_map = _account_image_map(login)          # кэш по логину (было: перечитывание аккаунта на КАЖДОЙ РК)
    # URL страниц моделей: account-level мёрж (все фиды, как цены) — покрывает марки без URL
    # в конкретном feed_id (#ФИКС-8).
    _feed_url_map = _account_offer_urls(login, _site_root_href(href))
    groups, _m3_alive = _pack_groups_with_retry(login, slepok, site_type, r_code, href, titles, texts,
                                                segment=segment, ai_title2=ai_title2, city=city, tp_code="tp1",
                                                image_map=_img_map, autotarget=autotarget,
                                                keep_keywords=keep_keywords,
                                                with_shopping=with_shopping,
                                                feed_url_by_model=_feed_url_map or None,
                                                only_cts=only_cts, only_gks=only_gks)
    if not groups:
        seg_note = f", segment={segment}" if segment else ""
        # Keyword pack пуст после ретраев → НЕ permanent-fail: помечаем defer (пункт уйдёт на
        # отложенную докрутку позже, когда локальное зеркало/M3-источник восстановится),
        # а не считаем окончательной ошибкой.
        return {"ok": False, "defer": True, "name": name,
                "error": (f"tp1(куки): keyword pack пуст/недоступен "
                          f"(local/M3 source alive={_m3_alive}) для "
                          f"slepok={slepok}, site_type={site_type}, tp=tp1{seg_note} → отложено на докрутку")}
    groups, _img_skip = _drop_tp1_groups_without_images(groups, "tp1", products_only)
    if _img_skip:
        _cts_s = ", ".join((_img_skip.get("image_no_pool_cts") or [])[:12])
        _more = len(_img_skip.get("image_no_pool_cts") or []) - 12
        _note = (f"tp1(куки): пропущено групп без картинок: {_img_skip['groups_skipped_no_images']} "
                 f"(ct: {_cts_s}{'; +' + str(_more) if _more > 0 else ''})")
        if not groups:
            return {"ok": True, "skipped": True, "name": name,
                    "image_no_pool": True, "tp1_build": _img_skip,
                    "error": _note}
    # РСЯ-картинки ПО КУКЕ до create_full: ЗАЛИТЬ картинки набора Grid-ом (upload_image — БЕЗ баллов,
    # допустимо даже при 0 units; НЕ путать с v5 adimages.add=152) и проставить imageHashes группам,
    # ЧТОБЫ create_full строил объявления СРАЗУ С картинками. Свежий ct (первая РК бренда в аккаунте)
    # не имеет basename в _grid_account_image_hashes → _img_map пуст → _tp1_pack_groups оставлял
    # image_hashes пустыми → NO_IMAGES_LIVE (систематичен на tp1). Пост-create Grid-repair ненадёжен,
    # поэтому создаём С картинками. Зеркалит token-path Фаза 3.4 (_parallel_upload_images, account_map).
    try:
        import os as _osu
        _all_paths = [p for g in groups for p in (g.get("image_paths") or [])]
        if _all_paths:
            _gc_img_pre = gf.get_grid_client(login)
            _uploaded_pre = _parallel_upload_images(_gc_img_pre, login, _all_paths, account_map=_img_map)
            _account_image_map_merge(login, _uploaded_pre)   # свежее — в кэш для следующих РК набора
            for g in groups:
                _hh = list(dict.fromkeys(g.get("image_hashes") or []))
                for p in (g.get("image_paths") or []):
                    if len(_hh) >= 5:
                        break
                    _h = _uploaded_pre.get(_osu.path.basename(p)) if p else None
                    if _h and _h not in _hh:
                        _hh.append(_h)
                if _hh:
                    g["image_hashes"] = _hh[:5]
    except Exception as _iue:  # noqa: BLE001 — картинки best-effort, не роняем создание кампании
        print(f"[tp1-cookie] pre-upload images failed login={login}: {str(_iue)[:120]}", flush=True)
    start_date = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=3))).strftime("%Y-%m-%d")  # МСК
    wkl = int(budget_rub) if budget_rub else int(cpc_cpa) * 10
    name_cpa = name.replace("tp1_cpc_site", "tp1_cpa_site", 1)
    variants = [(name, "network_cpa", False)]
    if not no_cpa:
        variants.append((name_cpa, "network_payconv", True))
    # ЦЕНА из фида в комбинаторное по куке (как v5 Фаза 3.5): adPrice по бренду группы. Без баллов.
    # Раньше куки-путь цены не ставил вовсе (price_map не прокидывался). Best-effort: {} → без цен.
    try:
        _price_map = _account_offer_prices(login, href)   # цены из предпочтительных фидов (чистые имена)
    except Exception:  # noqa: BLE001
        _price_map = {}
    # Ассеты кампании (уточнения/быстрые ссылки/промо) — чтобы кампания была ДОЗАПОЛНЕНА как на v5-пути.
    # Грузим один раз; v5-GET'ы и Grid-докрутка баллов НЕ стоят (units тратят только add/update РК/объяв).
    # БАГ-1 FIX: ассеты загружаем ВСЕГДА при наличии токена, не только при goal_id.
    # Grid принимает goalId="0" (проверено live 2026-06-24): кампания обновляется, callouts/sitelinks ставятся.
    # ФИКС B: Сайтлинки → href первой брендовой группы, а не базовый сайт. Cookie-путь создаёт
    # сайтлинки на уровне кампании (gc.create_full не поддерживает per-group). Берём первую
    # группу с не-базовым href как представителя. Для полноценных per-group сайтлинков нужен
    # рефакторинг gc.create_full (намеренно не трогается). (#ФИКС-B)
    _sl_href = next(
        (g["href"] for g in groups if g.get("href") and g["href"] != href.rstrip("/")),
        href
    )
    _assets = _resolve_campaign_assets(
        token, login, _sl_href, sitelinks=sitelinks,
        slepok=slepok, site_type=site_type, prefer_callout_texts=callout_texts,
        prefer_callout_ids=callout_ids)
    _slset = _assets.get("sitelink_set_id")
    _mp_disabled = _enabled_minus_places(slepok)              # #21 минус-площадки РСЯ (общий список, slepok игнорируется, 1 раз на аккаунт)
    out_campaigns = []
    for nm, _mode, pay_conv in variants:
        if job and job.get("cancel"):                        # отмена: стоп ПЕРЕД cpa-вариантом пары
            break                                             # (cpc уже создан/дозаполнен)
        spec = {"name": nm, "counter_id": counter_id or 0, "goal_id": goal_id or 0,
                "cpa": int(cpc_cpa), "weekly_budget": wkl, "start_date": start_date,
                "network": True, "search": False, "pay_for_conversion": pay_conv,
                "disabled_places": _mp_disabled,             # #21 → build_unified_campaign.disabledPlaces
                "minus_keywords": _enabled_minus_words()}    # глобальные минус-слова кампании (tp1 куки)
        try:
            rep = gc.create_full(login, campaign_spec=spec, groups=groups,
                                 region_ids=region_ids, href=href, goal_id=goal_id or 0,
                                 autotargeting=bool(autotarget),
                                 price_map=_price_map, brand_price_fn=_group_ad_price)
            if _img_skip:
                rep.update(_img_skip)
                rep.setdefault("warnings", []).append(_note)
            cid = rep.get("campaign_id")
            ok = bool(cid) and bool(rep.get("ads")) and not (rep.get("errors") and not rep.get("groups"))
            if cid and not rep.get("ads"):
                if not rep.get("errors"):                     # ДИАГНОСТИКА: add_ads вернул пусто БЕЗ исключения
                    rep.setdefault("errors", []).append(
                        f"объявления(куки): 0 TextAd (groups={rep.get('groups')}, "
                        f"adgroup_ids={rep.get('adgroup_ids')}) — add_ads вернул пусто без ошибки Grid")
                print(f"[tp1-cookie] {nm}: 0 ads groups={rep.get('groups')} feed={feed_id} errs={rep.get('errors')}", flush=True)
                try:
                    gc.GridCreateClient(login).delete_campaigns([cid])
                except Exception:  # noqa: BLE001
                    pass
                out_campaigns.append({
                    "ok": False, "name": nm, "campaign_id": cid, "launched": False,
                    "via": "cookie",
                    "tp1_build": {"groups": rep.get("groups"), "ads": rep.get("ads"),
                                  "errors": rep.get("errors", [])[:5]},
                    "url": (f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}" if cid else ""),
                    "error": "tp1(куки): partial-кампания удалена — объявления не созданы",
                    "partial_deleted": True,
                })
                continue
            _shop_ids: list[int] = []
            _listing_ids: list[int] = []
            if job and job.get("cancel"):           # отмена: кампания+объявления созданы, товарку/ассеты пропускаем
                print(f"[cancel] {nm}: прервано пользователем перед товаркой (cid={cid})", flush=True)
                out_campaigns.append({
                    "ok": ok, "name": nm, "campaign_id": cid, "launched": False,
                    "via": "cookie", "cancelled_mid": "shopping",
                    "tp1_build": {"groups": rep.get("groups"), "ads": rep.get("ads"),
                                  "shopping_ads": 0, "listing_ads": 0,
                                  "errors": rep.get("errors", [])[:5],
                                  "warnings": rep.get("warnings", [])[:5]},
                    "url": (f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}" if cid else ""),
                })
                break
            if ok and with_shopping and feed_id:
                _grid_shop_items = []
                # Bug A fix: резолвим имена полей бренда/модели для ЭТОГО фида (AUTO_RU: mark_id/folder_id;
                # YANDEX_MARKET: vendor/model). Без этого AUTO_RU фиды получали UNKNOWN_FIELD → стрип фильтра.
                _ck_brand_field = "vendor"
                _ck_model_field = "model"
                try:
                    from . import create_set_feeds as _csf_ck
                    _ck_brand_field = _csf_ck._resolve_feed_field(login, int(feed_id), "brand") or "vendor"
                    _ck_model_field = _csf_ck._resolve_feed_field(login, int(feed_id), "model") or "model"
                    if _ck_brand_field != "vendor" or _ck_model_field != "model":
                        print(f"[tp1-cookie] feed {feed_id}: brand_field={_ck_brand_field!r} model_field={_ck_model_field!r}",
                              flush=True)
                except Exception:  # noqa: BLE001
                    pass
                # ДВА фильтра по типу (решение Семёна, HAR36): Товары → brand_field [марка]; Страницы каталога
                # → name [марка|марка+модель]. ct0000 без марки → без фильтра.
                _shop_name_vals = []   # параллельно _grid_shop_items: name-значение листинга на группу
                for _grp, _agid in zip(groups, rep.get("adgroup_ids") or []):
                    if not _agid:
                        continue
                    _g_brand = (_grp.get("brand") or "").strip()
                    _g_seg = _ct_segment(_grp.get("ct") or "")
                    # Фильтр валиден ТОЛЬКО для брендовых групп («Марки»/«Модели»). «Общее» (тема в
                    # brand: «Автокредит»/«Trade-in»/«Авито») → без фильтра: товары по всему фиду, каталог — все стр.
                    _is_brand_seg = _g_seg in ("Марки", "Модели")
                    _vendor = _vendor_value(_g_brand) if (_g_brand and _is_brand_seg) else None     # товары: vendor [марка]
                    _name_val = _listing_name_value(_g_brand, _g_seg) if (_g_brand and _is_brand_seg) else None  # листинг: name
                    _model_vals = _model_field_values(_g_brand, _g_seg) if (_g_brand and _is_brand_seg) else []  # Модели → +model
                    _grid_shop_items.append({
                        "adgroup_id": int(_agid),
                        "feed_id": int(feed_id),
                        "vendor": _vendor,
                        "collection_id": None,
                        "model": _model_vals,
                        "name": _grp.get("name", "?"),
                        "brand_field": _ck_brand_field,
                        "model_field": _ck_model_field,
                        "apply_global_minus": _apply_global_feed_minus_for_site(site_type),
                    })
                    _shop_name_vals.append(_name_val)
                if _grid_shop_items:
                    try:
                        _gcl_shop = gf.get_grid_client(login)
                        _add_ids = _gcl_shop.add_shopping_ads(_grid_shop_items) or []
                        _shop_ids = [int(x) for x in _add_ids if x]
                        # карта shopping_ad_id → name_value (для name-фильтра листинга)
                        _name_by_shop = {}
                        for _ai, _raw in enumerate(_add_ids):
                            if _raw and _ai < len(_shop_name_vals) and _shop_name_vals[_ai]:
                                _name_by_shop[int(_raw)] = _shop_name_vals[_ai]
                        if _shop_ids:
                            _default_text = (_trim_clean(texts[0] if texts else "", 81)
                                             or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии.")
                            _shop_filters = {}
                            # #ФИКС-4: адресовать фильтры через enumerate(_add_ids) параллельно
                            # _grid_shop_items (тот же паттерн, что _name_by_shop 2023-2025).
                            # zip(_shop_ids_без_None, _grid_shop_items_полный) при частичном
                            # ответе давал смещение → фильтры чужой марки/модели на ShoppingAd.
                            for _ai, _raw in enumerate(_add_ids):
                                if not _raw or _ai >= len(_grid_shop_items):
                                    continue
                                _sid = int(_raw)
                                _src = _grid_shop_items[_ai]
                                _conds = []
                                _bf2 = _src.get("brand_field") or "vendor"
                                _mf2 = _src.get("model_field") or "model"
                                if _src.get("vendor"):
                                    _conds.append({"field": _bf2, "operator": "CONTAINS_ANY",
                                                   "stringValue": json.dumps(_vendor_filter_values(_src["vendor"]), ensure_ascii=False)})
                                if _src.get("model"):
                                    _mvals = _src["model"] if isinstance(_src["model"], list) else [str(_src["model"])]
                                    _mvals = [str(x) for x in _mvals if str(x).strip()]
                                    if _mvals:
                                        _conds.append({"field": _mf2, "operator": "CONTAINS_ANY",
                                                       "stringValue": json.dumps(_mvals, ensure_ascii=False)})
                                if _src.get("apply_global_minus", True) is not False:
                                    try:                 # глобальные минус-марки: используем тот же brand_field/model_field
                                        from . import create_set_feeds as _csf
                                        _conds.extend(_csf._minus_marks_grid_conditions(brand_field=_bf2, model_field=_mf2))
                                    except Exception:  # noqa: BLE001
                                        pass
                                if _conds:
                                    _shop_filters[int(_sid)] = {"tab": "CONDITION", "conditions": _conds}
                            # G review: set_default_text в СВОЁМ try — падение (Яндекс 500) НЕ блокирует
                            # создание листингов ниже (раньше оба в одном try → текст падал → 0 ListingAd).
                            try:
                                _gcl_shop.set_default_text(
                                    _shop_ids, int(feed_id), _default_text,
                                    filters_by_ad_id=_shop_filters,
                                )
                            except Exception as _dte:  # noqa: BLE001
                                rep.setdefault("warnings", []).append(f"shopping text(куки): {str(_dte)[:140]}")
                            # #ФИКС-1(v2): adGroupId→name_val НАПРЯМУЮ из параллельных массивов —
                            # БЕЗ _add_ids. При частичном создании (len(_add_ids)<len(items))
                            # старая индексная адресация через enumerate(_add_ids) давала смещение:
                            # _shop_name_vals[i] уходило на _grid_shop_items[i] чужой марки.
                            # adGroupId в items надёжен (группа создана ДО add_shopping_ads).
                            _agid_to_nv2 = {}
                            for _gsi2, _nv2 in zip(_grid_shop_items, _shop_name_vals):
                                if _nv2 and isinstance(_gsi2, dict):
                                    _gi2 = _gsi2.get("adgroup_id")
                                    if _gi2:
                                        _agid_to_nv2[str(_gi2)] = _nv2
                            _listing_rows = (_gcl_shop.add_listing_ads_by_shopping_ads(_shop_ids) or [])
                            _listing_ids = []
                            _lf_items = []
                            for _row in _listing_rows:
                                try:
                                    _lid = _row.get("id") if isinstance(_row, dict) else _row
                                    _said2 = int(_row.get("shoppingAdId") or 0) if isinstance(_row, dict) else 0
                                    if _lid:
                                        _listing_ids.append(int(_lid))
                                    _val = _name_by_shop.get(_said2) if _said2 else None
                                    if _lid and _val:
                                        _lf_items.append({"id": _lid, "feed_id": int(feed_id),
                                                          "value": _val, "bodies": [_default_text]})
                                except Exception:  # noqa: BLE001
                                    continue
                            # adGroupId-фолбэк убран (fix-3 08.07.2026): adGroupId нет в
                            # GdUpdateListingAdInput; shoppingAdId-матч надёжен (addedAds
                            # непуст при saveDraft:True — подтверждено 03.07 после фикса query).
                            # name-фильтр «Страницы каталога» (HAR36; by-shopping фильтр не наследует)
                            if _lf_items:
                                try:
                                    rep["listing_name_set"] = _gcl_shop.set_listing_name_filters(_lf_items)
                                except Exception as _lfe:  # noqa: BLE001
                                    rep["errors"].append(f"listing name-filter(куки): {str(_lfe)[:140]}")
                    except Exception as _shop_exc:  # noqa: BLE001
                        rep["errors"].append(f"shopping/listing(куки): {str(_shop_exc)[:160]}")
                if not _shop_ids:
                    # Bug2 graceful (как v5-путь / tp7 whole-feed fallback): фид без офферов
                    # (напр. лендинг-фид) → НЕ удаляем кампанию — в ней валидные TextAd-группы РСЯ.
                    # Диагностику пишем в WARNINGS (не errors!), чтобы выжившая ok=True кампания не
                    # показывала ложную «ошибку» в карточке — консистентно с v5-путём. (#1 review)
                    rep.setdefault("warnings", []).append(
                        "товарка(куки): 0 ShoppingAd — фид без офферов; оставлена РСЯ без товарных")
                    print(f"[tp1-cookie] {nm}: ShoppingAd=0 feed={feed_id} (graceful, РСЯ без товарки)", flush=True)
                elif not _listing_ids:
                    # Bug2 graceful: ShoppingAd есть, листинги пусты (фид-каталог без готовых офферов)
                    # → НЕ удаляем кампанию, оставляем товарку без листингов. Диагностика → warnings.
                    rep.setdefault("warnings", []).append(
                        f"листинги(куки): 0 ListingAd из {len(_shop_ids)} ShoppingAd (feed={feed_id}) — "
                        "0 ListingAd из by-shopping — оставлена товарка без листингов")
                    print(f"[tp1-cookie] {nm}: ListingAd=0 shop={len(_shop_ids)} feed={feed_id} (graceful)", flush=True)
            # ── Phase 4a куки: «Все фиды» — группа на каждый фид (shopping + listing) ──────────
            # Зеркало Phase 4a в _build_tp1_adgroups (token-путь). При all_feeds_list И ok кампании
            # создаём по одной группе Товарная галерея · {фид} на каждый фид без brand-фильтра.
            elif ok and cid and all_feeds_list:
                _af_ck_default = "Новые автомобили в наличии. Большой выбор. Скидки до 45% при покупке. Тест-драйв."
                try:
                    from .create_set_assets import SHOPPING_DEFAULT_TEXT as _af_ck_sdt  # noqa: PLC0415
                    _af_ck_default = _af_ck_sdt
                except Exception:  # noqa: BLE001
                    pass
                for _af_entry in all_feeds_list:
                    if not _af_entry:
                        continue
                    _af_fid = int(_af_entry[0]) if _af_entry[0] else 0
                    _af_fnm = str(_af_entry[1]) if len(_af_entry) > 1 and _af_entry[1] else ""
                    if not _af_fid:
                        continue
                    _af_gn = f"Товарная галерея · {(_af_fnm or str(_af_fid))}"[:255]
                    # Создаём группу через GridClient (куки-путь — только Grid)
                    _af_new_ag = None
                    try:
                        _af_gcl = gc.GridCreateClient(login)
                        _af_gitems = [gc.build_adgroup(
                            campaign_id=int(cid), name=_af_gn, region_ids=region_ids,
                            keywords=[], minus_keywords=[])]
                        _af_gids = _af_gcl.add_adgroups(_af_gitems)
                        _af_new_ag = _af_gids[0] if _af_gids else None
                    except Exception as _afe:  # noqa: BLE001
                        rep.setdefault("warnings", []).append(
                            f"all_feeds(куки) grp({_af_fid}): {str(_afe)[:120]}")
                    if not _af_new_ag:
                        continue
                    try:
                        _af_gcl2 = gc.GridCreateClient(login)
                        _af_shop_items = [{"adgroup_id": int(_af_new_ag), "feed_id": _af_fid,
                                           "vendor": None, "collection_id": None, "model": [],
                                           "name": _af_gn, "brand_field": "vendor", "model_field": "model",
                                           "apply_global_minus": _apply_global_feed_minus_for_site(site_type)}]
                        _af_sids = _af_gcl2.add_shopping_ads(_af_shop_items)
                        _af_sids = [int(x) for x in _af_sids if x]
                        if _af_sids:
                            try:
                                _af_gcl2.set_default_text(_af_sids, _af_fid, _af_ck_default)
                            except Exception:  # noqa: BLE001
                                pass
                            _af_b2: dict = {"listing_name_by_shop": {}}
                            _grid_add_listings_with_name_filters(
                                _af_gcl2, _af_sids, _af_b2, _af_fid, _af_ck_default,
                                apply_global_minus=_apply_global_feed_minus_for_site(site_type))
                            _shop_ids.extend(_af_sids)
                    except Exception as _afse:  # noqa: BLE001
                        rep.setdefault("warnings", []).append(
                            f"all_feeds(куки) shop({_af_fid}): {str(_afse)[:120]}")
            # Grid-докрутка РСЯ: уточнения/быстрые ссылки/промо на уровне кампании (без баллов).
            # БАГ-1 FIX: вызываем ВСЕГДА при ok+cid, не только при goal_id.
            # Grid принимает goalId="0" без ошибки (verified live 2026-06-24): ассеты ставятся корректно.
            _fin = None
            if ok and cid:
                try:
                    # Корректировки «Глобальных правил» через Grid (HAR21, без баллов) — campaignId
                    # ЭТОЙ кампании. v5 bidmodifiers.add тут недоступен (152), поэтому Grid.
                    _bm = _grid_bid_modifiers(cid, corr or {}, ret_map or {})
                    _finalize_rsya(
                        login, cid, name=nm, goal_id=goal_id or 0, cpa_rub=cpc_cpa, weekly_rub=wkl,
                        counter_ids=[counter_id] if counter_id else [],
                        pay_for_conversion=pay_conv,
                        callout_ids=_assets.get("callout_ids"), sitelink_set_id=_slset,
                        promo_id=(_assets["promos"][0] if _assets.get("promos") else None),
                        minus_set_ids=None, bid_modifiers=_bm, disabled_places=_mp_disabled)
                    _fin = {"callouts": len(_assets.get("callout_ids") or []),
                            "sitelink_set": _slset, "promo": bool(_assets.get("promos")),
                            "corrections": len((_bm.get("bidModifierRetargeting") or {}).get("adjustments") or [])}
                    if token:
                        _v5_mods, _v5_mod_err = _apply_corrections(token, login, cid, corr or {}, ret_map or {})
                        _fin["v5_corrections"] = _v5_mods
                        if _v5_mod_err:
                            _fin["v5_corrections_error"] = _v5_mod_err[:160]
                    # demographic (age/gender) теперь через Grid (bidModifierDemographics, HAR23/JS реверс).
                    # _grid_bid_modifiers уже включил их в _bm → _finalize_rsya применила Grid-ом.
                    _fin["demographic_corrections"] = len((_bm.get("bidModifierDemographics") or {}).get("adjustments") or [])
                except Exception as _fe:  # noqa: BLE001
                    _fin = {"error": str(_fe)[:160]}
                # ── Картинки для РСЯ-объявлений (новые аккаунты без истории) ─────────────
                # Если reuse image_hashes не сработал (новый аккаунт) — пробуем довесить картинки
                # ПО ГРУППАМ, чтобы не размазывать один и тот же хэш на все бренды кампании.
                _ad_ids = rep.get("ad_ids") or []
                if _ad_ids and not (job and job.get("cancel")):  # отмена: ассеты выставлены, картинки пропускаем
                    try:
                        import os as _os2
                        _gc_img = gf.get_grid_client(login)
                        # ── Параллельная заливка картинок ──────────────────────────────
                        # Собираем все пути по всем группам ПЕРЕД циклом, заливаем параллельно.
                        _all_img_paths2 = [_pth for _grp in groups
                                           for _pth in (_grp.get("image_paths") or [])]
                        # account_map=_img_map (basename→hash уже залитых) → warm-up/пост-рестарт
                        # переиспользует хэши «оттуда», не грузит повторно по сети.
                        _uploaded_by_name: dict[str, str] = _parallel_upload_images(
                            _gc_img, login, _all_img_paths2, account_map=_img_map)
                        _upd_items = []
                        # ad_ids теперь 1:1 с groups (None = объявление группы не создано) —
                        # zip больше не смещает пары (ревью 03.07 #5/#21)
                        for _aid, _grp in zip(_ad_ids, groups):
                            if not _aid:
                                continue
                            _gpaths = _grp.get("image_paths") or []
                            _hashes = list(dict.fromkeys(_grp.get("image_hashes") or []))
                            for _pth in _gpaths:
                                if len(_hashes) >= 5:
                                    break
                                if not _pth or not _os2.path.isfile(_pth):
                                    continue
                                _bn = _os2.path.basename(_pth)
                                _h = _uploaded_by_name.get(_bn)
                                if _h and _h not in _hashes:
                                    _hashes.append(_h)
                            _upd = {"id": _aid, "href": _grp.get("href") or href,
                                    "titles": _grp.get("titles") or [],
                                    "bodies": _grp.get("texts") or []}
                            if _hashes:
                                _upd["image_hashes"] = _hashes[:5]
                                _grp["image_hashes"] = _hashes[:5]  # фикс 3a: video-meta видит реальные хэши
                            _cur, _old = _group_ad_price(
                                _price_map, _grp.get("brand") or _grp.get("name") or "",
                                _grp.get("seg") or _ct_segment(_grp.get("ct") or "")
                            )
                            _ad_price = _grid_ad_price_payload(_cur, _old)
                            if _ad_price:
                                _upd["adPrice"] = _ad_price
                            _upd_items.append(_upd)
                        # Не используем suggest_images: Яндекс может предложить чужую/модельную картинку.
                        # Если своих картинок нет или они запрещены вкладкой «Контент», объявление остаётся без картинки.
                        if _upd_items:
                            _imgs_applied = _grid_update_adaptive_ads(
                                login, _upd_items, campaign_ids=[cid] if cid else None)
                            if _fin and isinstance(_fin, dict):
                                _fin["ads_repaired"] = _imgs_applied
                                _fin["image_groups"] = sum(1 for _it in _upd_items if _it.get("image_hashes"))
                    except Exception:  # noqa: BLE001 — картинки не критичны
                        pass
                # ── Видео РСЯ по куке (аналог _tp1_video_ads в v5 Фазе 3.6) ──────────────
                # created_ad_meta: adgroup_ids И ad_ids оба СТРОГО 1:1 с groups (None для
                # упавших) — прямой zip без счётчика. Старое последовательное потребление
                # ad_ids смещало видео на чужие объявления при частичных отказах Grid
                # (ревью 03.07 #5/#21). adPrice несём в мета (fix price-C): full-replace
                # без adPrice обнулил бы цену (verified live 2026-07-02).
                _ag_ids_v = rep.get("adgroup_ids") or []
                _ad_ids_v = rep.get("ad_ids") or []
                _cookie_ad_meta: list = []
                for _vgrp, _vagid, _vaid in zip(groups, _ag_ids_v, _ad_ids_v):
                    if not _vagid or not _vaid:
                        continue
                    _vcur, _vold = _group_ad_price(
                        _price_map, _vgrp.get("brand") or _vgrp.get("name") or "",
                        _vgrp.get("seg") or _ct_segment(_vgrp.get("ct") or "")
                    )
                    _cookie_ad_meta.append({
                        "id": _vaid,
                        "meta": {
                            "ct": _vgrp.get("ct") or "",
                            "site_type": site_type or _vgrp.get("site_type") or "",
                            "href": _vgrp.get("href") or href,
                            "titles": _vgrp.get("titles") or [],
                            "bodies": _vgrp.get("texts") or [],
                            "image_hashes": _vgrp.get("image_hashes") or [],
                            "ad_price_payload": _grid_ad_price_payload(_vcur, _vold),
                        }
                    })
                if _cookie_ad_meta and _VIDEO_AT_CREATE and not (job and job.get("cancel")):  # отмена: видео пропускаем
                    try:
                        _vr2 = _tp1_video_ads(login, _cookie_ad_meta, grid_cookie=None,
                                              campaign_id=cid)
                        if _fin and isinstance(_fin, dict):
                            _fin.update({k: _vr2[k] for k in
                                         ("videos_uploaded", "videos_attached", "video_groups")
                                         if k in _vr2})
                        if _vr2.get("warnings"):
                            rep.setdefault("warnings", []).extend(_vr2["warnings"][:3])
                    except Exception as _ve2:  # noqa: BLE001 — видео best-effort
                        rep.setdefault("warnings", []).append(f"видео(куки): {str(_ve2)[:100]}")
                elif _cookie_ad_meta:
                    rep["videos_deferred"] = True   # добьётся аудитом (VIDEO_MISSING)
            out_campaigns.append({
                "ok": ok, "name": nm, "campaign_id": cid, "launched": False,
                "via": "cookie", "rsya_finalized": _fin,
                "tp1_build": {"groups": rep.get("groups"), "ads": rep.get("ads"),
                              "shopping_ads": len(_shop_ids), "listing_ads": len(_listing_ids),
                              # groups_expected — сколько групп ушло в AddUnifiedAdGroups;
                              # расхождение ловит верификатор (GROUPS_CREATED_LESS_THAN_SENT).
                              "groups_expected": rep.get("groups_expected"),
                              "errors": rep.get("errors", [])[:5],
                              "warnings": rep.get("warnings", [])[:5]},
                "url": (f"https://direct.yandex.ru/dna/campaign/{cid}?ulogin={login}" if cid else ""),
                "error": ("; ".join(rep.get("errors") or [])[:240] if not ok else None),
            })
        except Exception as e:  # noqa: BLE001
            out_campaigns.append({"ok": False, "name": nm, "error": f"tp1(куки): {str(e)[:200]}"})
    ok = any(c.get("ok") for c in out_campaigns)
    first_id = next((c.get("campaign_id") for c in out_campaigns if c.get("campaign_id")), None)
    out = {"ok": ok, "name": name, "campaign_id": first_id, "launched": False,
           "via": "cookie", "campaigns": out_campaigns,
           "url": next((c.get("url") for c in out_campaigns if c.get("url")), "")}
    if not ok:
        _errs = [c.get("error") for c in out_campaigns if c and c.get("error")]
        out["error"] = ("; ".join(dict.fromkeys(_errs))[:240] or "tp1(куки): пара не создалась")
    return out
