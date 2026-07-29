"""Create-set tp2/tp4 text campaign builders extracted from blueprint.py."""

from __future__ import annotations

from . import create_set_audiences as _cs_aud
from ..text_norm import _trim_clean
from ..text_gen import _fill_title
from ..clients.grid_create import unique_keyword_ids as _unique_keyword_ids
from ..link_check import resolve_or_fallback_url as _resolve_url, resolve_urls_batch as _resolve_urls_batch
from ..model_urls import _is_degenerate_feed_url

import re
import time
from collections import Counter
from urllib.parse import urlsplit, urlunsplit

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by text builders."""
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


def _pack_group_href(ct: str, brand: str, real_brand: str, feed_urls: dict, href: str, site_type: str) -> str:
    """Raw model_href для группы пака (до link_check resolve). Вызывается в pre-pass и основном цикле.

    BUTTON_404_GENERIC_AVTO (2026-07-21): формульный deep-link зовём ТОЛЬКО с реальным брендом
    (real_brand, не brand), т.к. brand может быть литеральным фолбэком «Авто» →
    _slugify("Авто")="avto" → /auto/avto (404, страницы не существовало). Структурное имя группы
    (`t` из слепка) сюда НЕ подставляем: пустой real_brand означает, что _valid_pack_brand_name
    только что отверг имя ct как не-марку («Авито»/«Автокредит»/«Автосалон»), и структурное имя
    у таких групп — та же тема. Для них остаётся корень сайта (прежнее поведение тем «Общее»).

    AD_HREF_ROOT_INSTEAD_OF_MODEL: вырожденный URL из фида (квиз-оффер `/quiz?fid=…`, голый
    корень) игнорируем — иначе link_check.strip_quiz_url схлопнет его в главную. Фолбэк —
    формула по real_brand.
    """
    site_href = _site_root_href(href)
    raw_feed = _feed_url_for_model(feed_urls, brand, no_brand_fallback=(_ct_segment(ct) == "Модели"))
    if raw_feed and not _is_degenerate_feed_url(raw_feed):
        return _brand_level_url(raw_feed) if _ct_segment(ct) == "Марки" else _strip_url_query(raw_feed)
    if real_brand:
        return _model_page_href(site_href, site_type, real_brand)
    return site_href.rstrip("/")


def _build_tp2_adgroups(token: str, login: str, campaign_id: int,
                        region_ids: list, groups: list,
                        feed_id: int = 0, with_shopping: bool = False,
                        apply_group_minus: bool = True,
                        autotarget: bool = False,
                        keep_keywords: bool = False,
                        site_type: str = "",
                        campaign_is_new: bool = False,
                        search_channel: bool = True) -> dict:
    """Наполнить Поисковую (tp2) / tp5 группами БАТЧЕМ: adgroups.add → keywords.add → ads.add(TextAd).

    groups: [{name, keywords:[], minus:[], title, text, href, title2?, callout_ext_ids?}].
    feed_id + with_shopping (tp5 «Товарная галерея», как в слепках): дополнительно к TextAd в каждую
    группу — ListingAd (динамика) + ShoppingAd (товарное) по фиду → состав «Т+Л+ТОВ». Проверено live:
    v5 TEXT-кампания принимает ShoppingAd. Аддитивно: нет фида/флага → только TextAd (старое поведение).
    apply_group_minus: если False — групповые минусы НЕ вешаются (для campaign/shared_set слепков, где
    минусы уходят на уровень кампании, а не групп — как в реальных аккаунтах pavlov/kryuchkova/scherbakova).
    Анти-блок: операции идут пачками (см. _AC_CHUNK_*) с паузами, групп ≤ _AC_GROUP_CAP за проход.
    Кампания остаётся черновиком (State=OFF из оболочки). Лимиты Директа: ключей ≤200/группу,
    минус ≤4096 симв. без пробелов/группу (terehov), Title ≤56, Title2 ≤30, Text ≤81, уточнений ≤4/объявление.
    campaign_is_new: кампания создана вызывающим ШАГОМ ВЫШЕ и пуста → Фаза 1 не читает
    предмутационный снимок имён групп (лишний Grid-запрос, а при сбое чтения сверка потерянного
    ответа AddUnifiedAdGroups отключилась бы совсем). Дефолт False: для непустой кампании снимок
    обязателен (коллизии имён групп).
    search_channel: канал КАМПАНИИ (spec `search`/`network`, см. `create_set_audiences.is_search_channel`)
    — им выбирается поле аудиторий: поиск → `searchRetargetings`, сеть → `retargetings`. Этот билдер
    вызывается только из `_build_text_from_pack` (tp2/tp4/tp5 — все поисковые), поэтому дефолт True.
    → {adgroups, keywords, ads, errors, deferred}."""
    rep = {"adgroups": 0, "keywords": 0, "ads": 0, "images_uploaded": 0, "errors": [], "deferred": 0}
    rids = [int(r) for r in (region_ids or []) if str(r).lstrip("-").isdigit()] or [225]
    wants_keywords = (not autotarget) or bool(keep_keywords)
    if wants_keywords:
        _seen_kw_campaign: set[str] = set()
        _deduped_groups = []
        _dropped_groups = 0
        for _g in groups or []:
            _ng = dict(_g or {})
            _kws = []
            for _kw in _kw_clean(_ng.get("keywords") or [], 200):
                _key = " ".join(sorted(re.sub(r"(^|\s)\+", " ", str(_kw or "").lower()).split()))
                if not _key or _key in _seen_kw_campaign:
                    continue
                _seen_kw_campaign.add(_key)
                _kws.append(_kw)
            if _kws:
                _ng["keywords"] = _kws
                _deduped_groups.append(_ng)
            else:
                _dropped_groups += 1
        groups = _deduped_groups
        if _dropped_groups:
            rep["groups_skipped_duplicate_keywords"] = _dropped_groups
    if len(groups) > _AC_GROUP_CAP:                       # кап за проход (анти-блок)
        rep["deferred"] = len(groups) - _AC_GROUP_CAP
        groups = groups[:_AC_GROUP_CAP]

    # ── Фаза 0: картинки НЕ грузим через v501 заранее.
    # Живой баг 2026-06-28: массовый upload_image на token-path мог подвешивать создание кампании
    # до ads.add, в итоге кампания появлялась с adgroups, но без объявлений. Боевой fallback теперь
    # такой: ResponsiveAd создаём сразу, а image hashes добиваем post-create через Grid/куки
    # (_grid_update_adaptive_ads + GridClient.upload_image) по фактическим ad_id.

    # ── Фаза 1: adgroups — АТОМАРНОЕ создание через Grid AddUnifiedAdGroups с relevanceMatch
    # профиля search_tp2 (EXACT_V2_MARK + WITHOUT_BRAND). Это token-путь tp2/tp4 (DIRECT_API_FIRST).
    # v5 adgroups.add НЕ ставит relevanceMatchCategories → Яндекс дефолт (все 5 категорий + 3 бренда),
    # а пост-патч через groups_for_edit (edit-view) ловит ЛАГ реплики → свежие группы не видны → тихий
    # return 0 → WRONG_AUTOTARGET (журнал TP5_AUTOTARGET v2 «не помогло»; I/J — тот же корень edit-view).
    # Grid профиль ставится при СОЗДАНИИ — lag-проблемы нет (эталон v3: _build_tp1_adgroups tp5 и
    # cookie-путь create_full). Ключи — ТОЛЬКО через Фазу 2 (AddKeywords v5), keywords=[] в build_adgroup
    # (иначе Grid дублирует их для групп <~140 ключей). UTM (trackingParams) и групповой минус
    # (adGroupMinusKeywords) проставляет сам build_adgroup — как в куки-пути.
    _gcl2 = gc.GridCreateClient(login)
    _g2_items = []
    for g in groups:
        # Групповые минусы (group-режим): бюджет 4096 симв.; build_adgroup доп. кап 100 фраз (как
        # куки-путь create_full). Для campaign/shared_set (apply_group_minus=False) минус — на кампании.
        _gm = (_minus_char_budget(g.get("minus") or [], _MINUS_SHARED_SET_CHAR_BUDGET)
               if apply_group_minus else [])
        _g2_items.append(gc.build_adgroup(
            campaign_id=int(campaign_id),
            name=(g.get("name") or "группа")[:255],
            region_ids=rids,
            keywords=[],                          # ключи — ТОЛЬКО через Фазу 2, без дублей
            minus_keywords=_gm,
            autotargeting=bool(autotarget),
            autotargeting_profile=("search_tp2" if autotarget else ""),   # EXACT_V2_MARK + WITHOUT_BRAND атомарно
            # Аудитории структуры (g["audiences"] — id условий, резолвнутые под ЦЕЛЕВОЙ кабинет
            # в _build_text_from_pack / _tp1_pack_groups). Поле выбирается КАНАЛОМ кампании,
            # а не списком tp-кодов: поиск → searchRetargetings, сеть → retargetings.
            retargeting_ids=g.get("audiences"),
            retargeting_on_search=bool(search_channel),
        ))
    _aud_notes = _cs_aud.group_notes(groups)
    if _aud_notes:
        rep.setdefault("warnings", []).extend(_aud_notes)
    try:
        # campaign_is_new=True (shell TEXT_CAMPAIGN создан шагом выше и пуст) → снимок имён групп
        # не читаем: лишний Grid-запрос в горячем пути tp2/tp4, а его сбой отключил бы сверку.
        ag_ids = _gcl2.add_adgroups(_g2_items, campaign_is_new=bool(campaign_is_new))
        # Позиционный сдвиг: Grid пропускает упавшие группы (без null-заглушки) → список короче
        # входного → выравниваем строго по имени (аналог create_full:615 / _build_tp1_adgroups:238).
        if len(ag_ids) != len(groups):
            _n2id2 = _gcl2._read_adgroup_name_to_id(int(campaign_id))
            if _n2id2:
                ag_ids = [_n2id2.get(g.get("name") or "") for g in groups]
            else:
                ag_ids = list(ag_ids) + [None] * (len(groups) - len(ag_ids))
                rep["errors"].append("tp2/tp4 Grid: позиционный сдвиг групп — ключи могут быть смещены")
        else:
            time.sleep(0.6)
            _n2id2 = _gcl2._read_adgroup_name_to_id(int(campaign_id))
            if _n2id2:
                ag_ids = [_n2id2.get(g.get("name") or "") for g in groups]
        rep["adgroups"] = sum(1 for x in ag_ids if x)
        rep["relevance_match_set"] = rep["adgroups"]   # relevanceMatch атомарно при создании
    except gc.GridCreateError as _g2e:
        # Grid-группы не создались → rep без adgroups → вызывающий (_create_text_via_token) удалит
        # недоделанную РК и уйдёт в defer/фолбэк. ok:True без корректного автотаргета невозможен.
        rep["errors"].append(f"adgroups(Grid tp2/tp4): {str(_g2e)[:200]}")
        return rep

    # ── Фаза 2: ключи ТЕМ ЖЕ Grid-транспортом (AddKeywords на _gcl2 из Фазы 1), а НЕ v5 keywords.add.
    # Регресс job fcd1d01c0d93 (DMP_TP2_KEYWORDS_LOST_MIXED_TRANSPORT): группы создаются через Grid
    # AddUnifiedAdGroups (узкий автотаргет), а ключи лились v5 keywords.add на этих СВЕЖИХ Grid-группах
    # → лаг репликации Grid→v5 → v5 рапортовал Id, но LIVE=0 (ключи-фантомы, 68/68 групп пустые).
    # Grid.add_keywords видит свои же только что созданные группы без лага (единый транспорт, как
    # cookie-путь create_full:624-638, который потому и закреплял ключи). Без баллов, сам чанкует ≤1000
    # с паузами. autotarget=True → реальных ключей нет; таргетинг = relevanceMatch, УЖЕ активный
    # атомарно из Grid build_adgroup(search_tp2) (Фаза 1). ---autotargeting спецключ add_keywords
    # сам пропускает (живёт в relevanceMatch, не в keywords).
    kw_items = []
    for i, g in enumerate(groups):
        if not ag_ids[i]:
            continue
        if autotarget and not keep_keywords:
            continue
        # _kw_clean дедуплит (lowercase seen) и капит ≤200/группу → нет MUST_NOT_CONTAIN_DUPLICATED.
        for k in _kw_clean(g.get("keywords") or [], 200):
            kw_items.append({"adGroupId": str(ag_ids[i]), "keyword": k})
    if kw_items:
        try:
            _sent_kw_agids = {str(it.get("adGroupId") or "") for it in kw_items if it.get("adGroupId")}
            # Считаем УНИКАЛЬНЫЕ keywordId, а не отправленные строки: Директ схлопывает
            # дубли (порядок слов/регистр/пробелы/«+»/словоформа) и отдаёт id уже существующей
            # фразы → len(addedItems) давал ложный недобор в сверке build⇄кабинет.
            _added_kw_rows = _gcl2.add_keywords(kw_items)
            _added_kw_agids = {str(r.get("adGroupId") or "") for r in (_added_kw_rows or []) if r.get("adGroupId")}
            _missing_kw_agids = _sent_kw_agids - _added_kw_agids
            if _missing_kw_agids:
                time.sleep(0.6)
                _retry_items = [it for it in kw_items if str(it.get("adGroupId") or "") in _missing_kw_agids]
                _retry_rows = _gcl2.add_keywords(_retry_items) if _retry_items else []
                _added_kw_rows = list(_added_kw_rows or []) + list(_retry_rows or [])
                _added_kw_agids = {str(r.get("adGroupId") or "") for r in (_added_kw_rows or []) if r.get("adGroupId")}
                _missing_kw_agids = _sent_kw_agids - _added_kw_agids
            rep["keywords"] = _unique_keyword_ids(_added_kw_rows)
            if _missing_kw_agids:
                rep["errors"].append(
                    f"keywords(Grid AddKeywords): {len(_missing_kw_agids)} групп без подтверждённых ключей")
        except Exception as _kwe:  # noqa: BLE001 — ключи = ЕДИНСТВЕННЫЙ путь; сбой = группы без ключей
            rep["errors"].append(f"keywords(Grid AddKeywords): {str(_kwe)[:200]}")

    # ── Фаза 3: ads.add пачками — КОМБИНАТОРНОЕ объявление (ResponsiveAd) через v501.
    # Замена ТГО (TextAd, отключают с 30.06): несколько заголовков/текстов в одном объявлении.
    # Уточнения наследуются на уровне группы/кампании (AdExtensions у ResponsiveAd нет).
    ad_items = []
    ad_meta = []   # параллельно ad_items — для adPrice из фида (#2)
    _acc_url = ""
    for i, g in enumerate(groups):
        if not ag_ids[i]:
            continue
        href = g.get("href") or ""
        if href and not _acc_url:
            _acc_url = re.sub(r"(https?://[^/]+).*", r"\1", href)   # https://домен — для выбора фида цен
        img_paths = g.get("image_paths") or ([g.get("image_path")] if g.get("image_path") else [])
        ra = _responsive_ad(g.get("titles") or [g.get("title"), g.get("name")],
                            g.get("texts") or [g.get("text")], href,
                            image_hashes=None,
                            # site_type нужен финальной сборке: `_upgrade_credit_*` подставляет
                            # хардкод-варианты про «новые авто» уже ПОСЛЕ всех `_cf`.
                            site_type=site_type or g.get("site_type") or "")
        if not ra:
            rep["errors"].append(f"{g.get('name', '?')}: пропущено объявление (нет заголовков/текстов/href)")
            continue
        ad_items.append({"AdGroupId": int(ag_ids[i]), "ResponsiveAd": ra})
        ad_meta.append({"brand": g.get("brand") or g.get("name") or "", "href": href,
                        "seg": _ct_segment(g.get("ct") or ""),   # 'Марки' → цена = МИН по марке
                        "titles": ra.get("Titles") or [], "bodies": ra.get("Texts") or [],
                        "image_hashes": [],
                        "image_paths": img_paths[:5]})
    created_ad_meta = []
    _base = 0
    for chunk in _chunks(ad_items, _AC_CHUNK_AD):
        jd = _v501_svc("ads", "add", token, login, {"Ads": chunk})
        used_retry = ""
        if "error" in jd:
            for retry_name, retry_chunk in (
                ("без быстрых ссылок", _responsive_retry_items(chunk, drop_sitelinks=True)),
                ("без быстрых ссылок и картинок", _responsive_retry_items(chunk, drop_sitelinks=True, drop_images=True)),
            ):
                jd2 = _v501_svc("ads", "add", token, login, {"Ads": retry_chunk})
                if "error" not in jd2:
                    jd = jd2
                    used_retry = retry_name
                    rep.setdefault("warnings", []).append(f"ads.add(tp1 ResponsiveAd): retry {retry_name}")
                    break
        if "error" not in jd:
            for k, r in enumerate((jd.get("result") or {}).get("AddResults", [])):
                if r.get("Id"):
                    rep["ads"] += 1
                    gi = _base + k
                    if gi < len(ad_meta):
                        created_ad_meta.append((int(r["Id"]), ad_meta[gi]))
                for e in (r.get("Errors") or []):
                    rep["errors"].append(f"ResponsiveAd: {e.get('Message')} {e.get('Details','')}".strip())
        else:
            rep["errors"].append(f"ads.add(ResponsiveAd) {_v5_err(jd)}")
        _base += len(chunk)
        time.sleep(_AC_BATCH_SLEEP)

    # Фаза 3.4: post-create repair через Grid/куки для token-path.
    # Даже если v501 ads.add прошло, live payload в Директе может схлопнуться до 2 заголовков /
    # 1-2 картинок. Поэтому после создания всегда добиваем фактическое объявление через Grid:
    # titles + bodies + imageHashes. Это и есть fallback «если токены недогрузили — догружаем по куки».
    if created_ad_meta:
        try:
            import os as _os3
            _gc_img = gf.get_grid_client(login)
            # ── Параллельная заливка картинок ──────────────────────────────
            # Собираем все уникальные пути ПЕРЕД циклом, заливаем 8 потоками.
            _all_img_paths = [_pth for _ad_id2, _meta2 in created_ad_meta
                              for _pth in (_meta2.get("image_paths") or [])]
            _uploaded_by_name: dict[str, str] = _parallel_upload_images(_gc_img, login, _all_img_paths)
            _upd_items = []
            for ad_id, meta in created_ad_meta:
                _hashes = list(dict.fromkeys(meta.get("image_hashes") or []))
                for _pth in (meta.get("image_paths") or []):
                    if len(_hashes) >= 5:
                        break
                    if not _pth or not _os3.path.isfile(_pth):
                        continue
                    _bn = _os3.path.basename(_pth)
                    _h = _uploaded_by_name.get(_bn)
                    if _h and _h not in _hashes:
                        _hashes.append(_h)
                if _hashes:
                    meta["image_hashes"] = _hashes[:5]
                _upd = {"id": ad_id, "href": meta["href"], "titles": meta["titles"], "bodies": meta["bodies"]}
                if meta.get("image_hashes") is not None:
                    _upd["image_hashes"] = meta.get("image_hashes") or []
                _upd_items.append(_upd)
            if _upd_items:
                rep["ads_repaired"] = _grid_update_adaptive_ads(
                    login, _upd_items, campaign_ids=[campaign_id] if campaign_id else None)
                rep["image_groups"] = sum(1 for _it in _upd_items if _it.get("image_hashes"))
        except Exception as _e:  # noqa: BLE001
            rep.setdefault("warnings", []).append(f"tp2/tp4 repair: {str(_e)[:100]}")

    # Фаза 3.5: ЦЕНА из фида в комбинаторное (#2) — Grid по куке. adPrice по бренду/модели группы.
    # Цены берём из предпочтительных фидов (чистые имена офферов), а не из дефолтного (промо-префикс).
    try:
        _pmap = _account_offer_prices(login, _acc_url)
        if _pmap and created_ad_meta:
            _pitems = []
            for ad_id, meta in created_ad_meta:
                cur, old = _group_ad_price(_pmap, meta.get("brand", ""), meta.get("seg", ""))
                if cur:
                    _pitems.append({"id": ad_id, "href": meta["href"], "titles": meta["titles"],
                                    "bodies": meta["bodies"], "image_hashes": meta["image_hashes"],
                                    "current": cur, "old": old})
            rep["prices_set"] = _grid_set_ad_prices(login, _pitems)
            # ads_repaired_after_price УДАЛЁН (2026-07-02, code-review): repair_items без adPrice →
            # Grid full-replace ЗАТИРАЛ только что установленные цены (тот же баг чинили в tp1).
            # Контент цела: _grid_set_ad_prices сам шлёт titles/bodies/imageHashes полностью.
    except Exception as _e:  # noqa: BLE001
        rep.setdefault("warnings", []).append(f"adPrice: {str(_e)[:100]}")

    # ── Фаза 4 (tp5): товарные по фиду — ListingAd (динамика) + ShoppingAd (товарное) в каждую группу.
    # Состав «Т+Л+ТОВ» как в слепках. v501 add_listing_ad/add_shopping_ad (FeedId в объявлении).
    if feed_id and with_shopping:
        rep["listing_ads"], rep["shopping_ads"] = 0, 0
        v501c = cmc.DirectV501Client(token, login)
        v501c.sess.headers.update({"Authorization": f"Bearer {token}"})
        for i in range(len(groups)):
            if not ag_ids[i]:
                continue
            try:
                if v501c.add_listing_ad(int(ag_ids[i]), int(feed_id)):
                    rep["listing_ads"] += 1
            except Exception as e:  # noqa: BLE001 — товарные не критичны, TextAd уже создан
                rep["errors"].append(f"{groups[i].get('name','?')}: listing_ad {str(e)[:80]}")
            try:
                if v501c.add_shopping_ad(int(ag_ids[i]), int(feed_id)):
                    rep["shopping_ads"] += 1
            except Exception as e:  # noqa: BLE001
                rep["errors"].append(f"{groups[i].get('name','?')}: shopping_ad {str(e)[:80]}")
            time.sleep(_AC_BATCH_SLEEP)
    return rep

def _struct_cts(slepok: str, site_type: str, tp_code: str) -> list:
    """Список модель-ct для (слепок, тип сайта, tp_code) из структуры (формат groups+gc).
    Грубый формат (splits без gc) → []."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    struct = _json("slepki_structure.json").get("directologists", [])
    d = next((x for x in struct if x.get("key") == key), None)
    if not d:
        return []
    st = next((s for s in d.get("site_types", []) if s.get("name") == site_type), None)
    if not st:
        return []
    cts, seen = [], set()
    for tp in st.get("tp", []):
        if tp.get("code") != tp_code:
            continue
        for grp in tp.get("groups", []):          # формат terehov; у splits ключа groups нет
            for it in grp.get("items", []):
                ct = _gc_ct(it.get("gc", ""))
                if ct and ct != "ct0000" and ct not in seen:
                    seen.add(ct)
                    cts.append(ct)
    return cts

# Транслит-артефакты харвеста: живые имена групп иногда приходят латиницей, визуально
# имитирующей кириллицу («Abto Py» = «Авто Ру», auto.ru; «Abto» = «Авто»). В режиме групп 1в1
# (_multi) имя группы берётся из структурного `t` как есть → в кабинет уезжала ломаная кириллица.
# Нормализуем при чтении структурного имени. (Fix 3а — правка Семёна 2026-07-14)
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
    # пословный фолбэк: отдельный латинский токен «Abto» → «Авто» (регистронезависимо)
    return re.sub(r"(?i)\bAbto\b", "Авто", s)

def _struct_items(slepok: str, site_type: str, tp_code: str) -> list:
    """Как _struct_cts, но БЕЗ дедупа — по одному элементу на структурный item (per-adgroup 1в1).
    → [{"ct":ctNNNN, "gk":<group-slug>, "name":<имя из структуры>}]. gk — авторитетное поле item
    (``gk``) ИЛИ выведенное из ``gc`` правилом kp._group_slug. Грубый/split формат (без groups) → []."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    struct = _json("slepki_structure.json").get("directologists", [])
    d = next((x for x in struct if x.get("key") == key), None)
    if not d:
        return []
    st = next((s for s in d.get("site_types", []) if s.get("name") == site_type), None)
    if not st:
        return []
    items = []
    for tp in st.get("tp", []):
        if tp.get("code") != tp_code:
            continue
        for grp in tp.get("groups", []):          # формат terehov; у splits ключа groups нет
            for it in grp.get("items", []):
                gc = it.get("gc", "")
                ct = _gc_ct(gc)
                if not ct or ct == "ct0000":
                    continue
                gk = (it.get("gk") or kp._group_slug(gc))
                items.append({"ct": ct, "gk": gk,
                              "name": _norm_struct_name(it.get("t") or grp.get("name") or "")})
    return items

def _tp2_struct_cts(slepok: str, site_type: str) -> list:
    """Совместимость: модель-ct для tp2."""
    return _struct_cts(slepok, site_type, "tp2")

def _struct_has_tp(slepok: str, site_type: str, tp_code: str) -> bool:
    """tp ОБЪЯВЛЕН в структуре слепка — независимо от наличия модель-ct.
    _struct_cts для этого НЕ годится: у tp7/tp6 бывают только ct0000-группы («Товарка -
    Общая», «Автотаргетинг») → он даёт [] и tp ложно считался «не в слепке»
    (ложняки EXTRA_TP_NOT_IN_SLEPOK: scherbakova-tp7, pavlov-tp6, выверено 03.07.2026)."""
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    struct = _json("slepki_structure.json").get("directologists", [])
    d = next((x for x in struct if x.get("key") == key), None)
    st = next((s for s in (d or {}).get("site_types", []) if s.get("name") == site_type), None)
    return any(tp.get("code") == tp_code for tp in (st or {}).get("tp", []))

def _text_group_name(ct: str, r_code: str, model: str) -> str:
    """Кодер-имя группы текстовой кампании (tp2/tp4 Поиск) по текущему канону:
    {ct}_aon_n000_{r_code}_ct001_ag011_g00 — {model}.
    Если r_code пуст (нет контекста) — отдаём просто модель (старое поведение)."""
    if not r_code:
        return model or ct
    return f"{ct}_aon_n000_{r_code}_ct001_ag011_g00 — {model}"

def _build_text_from_pack(token: str, login: str, campaign_id: int, slepok: str,
                          site_type: str, tp_code: str, region_ids: list, href: str,
                          titles: list | None, texts: list,
                          feed_id: int = 0, with_shopping: bool = False,
                          r_code: str = "", segment: str | None = None,
                          ai_title2: str = "",
                          apply_group_minus: bool = True,
                          city: str = "", autotarget: bool = False,
                          keep_keywords: bool = False,
                          only_cts: list[str] | None = None,
                          only_gks: set | None = None,
                          campaign_is_new: bool = False,
                          campaign_mode: str = "search") -> dict:
    """Наполнить текстовую кампанию (tp1/tp2/tp5): структура→модель-ct→ключи/минус/уточнения
    из пака M3 (по tp_code)→группы+объявления+callouts. Тексты — из titles/texts. Всё черновиком.

    segment ('Марки'|'Модели'|None): фильтр ct-групп по сегменту (tp4 — марки/модели разными
    кампаниями, как боевые). None → все ct (поведение tp2/tp5 неизменно).

    ⚠️ tp5 «Поиск+Динамика+Товарная галерея»: тут строится ТОЛЬКО поисковый backbone
    (TEXT_AD + ключи). Фид-объявления (LISTING_AD «динамика» / SHOPPING_AD «товарная») —
    автогенерация Яндекса из фида, НЕ через v5 ads.add; добавятся отдельным шагом."""
    cts = _struct_cts(slepok, site_type, tp_code)
    # only_cts (split-driven слепки, напр. dmp/tp2): EXPLICIT override — наполняем ТОЛЬКО ct-кодами
    # этого split-блока (create_set_plan "tp2_split_cts"). Приоритетнее segment-фильтра. Для splits-
    # формата _struct_cts даёт [] (читает лишь top-level tp.groups) → доверяем ct-кодам плана
    # (порядок — как в плане/split). Если структура «модельная» (terehov) — пересекаем со _struct_cts.
    if only_cts:
        _oc = [c for c in only_cts if c]
        cts = ([ct for ct in cts if ct in set(_oc)] if cts else list(_oc))
        if not cts:
            return {"skipped": "only_cts не пересёкся со структурой слепка"}
    elif segment:
        cts = [ct for ct in cts if _ct_segment(ct) == segment]
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    gather_key = key
    donor_note = ""
    # Фолбэк донора: сегмент задан, но у слепка нет своих ct этого сегмента (напр. Терехов tp4
    # без «Моделей») → берём ct И контент сегмента у слепка-донора («по примеру других слепков»,
    # структура слепка не теряется — его «Марки» строятся отдельной кампанией своим контентом).
    if segment and not cts:
        donor = _segment_donor(segment, tp_code, site_type, exclude=key)
        if donor:
            cts = [ct for ct in _struct_cts(donor, site_type, tp_code) if _ct_segment(ct) == segment]
            gather_key = _SLEPOK_KEY.get(donor, donor)
            donor_note = f"сегмент «{segment}» взят у донора «{donor}» (у «{slepok}» своих нет)"
    if not cts:
        return {"skipped": f"нет модель-ct в структуре для {tp_code} (грубый формат)"}
    pack = kp.gather(gather_key, site_type, tp_code)   # один ssh-вызов к M3
    if not pack:
        return {"skipped": "пак недоступен (мост M3?)"}
    text0 = _trim_clean(texts[0] if texts else "", 81)
    ct_name = _ag_part1_map()                   # ct→имя из gsheet_naming (полное покрытие 318) — кодер
    ct_model = kp.feeds_ct_model()              # фид-индекс (модельные ct) — фолбэк
    # Не-авто (dmp): {ctNNNN: тема из структуры/выгрузки} — непустой ТОЛЬКО у слепков auto:false.
    # Гейт-разделитель: непустой → dmp (имя группы = тема, без gc-хвоста и без «Авто», авто-фид НЕ
    # используем); пустой → авто-слепки, прежнее поведение (ct_model + фолбэк «Авто»). (DMP_GROUP_NAMES_AVTO)
    _struct_names = _struct_ct_names(slepok, site_type)
    _sc_titles = _slepok_campaign_content(slepok, site_type).get("titles") or []  # пул слепка — 1 раз
    # URL страниц моделей: account-level мёрж (все фиды, как цены) → покрывает марки без URL
    # в конкретном feed_id (был Баг-8: formular _model_page_href на 404). (#ФИКС-8)
    _feed_urls = _account_offer_urls(login, _site_root_href(href))
    # Группы 1в1 (per-adgroup). Гейт: если в структуре у какого-то ct >1 группа (реальная
    # ct-коллизия) И это НЕ split/dmp (only_cts / _struct_names) — строим по СТРУКТУРНЫМ items
    # (не по дедуп-ct), беря per-group пак-данные pack[ct]["_groups"][gk] с фолбэком на общий
    # pack[ct]. Иначе — прежний per-ct цикл (обратная совместимость: 1 группа на ct).
    # camp_names-маршрутизация (задача 7): only_gks задан → строим per-adgroup по gk кампании
    # (пересечение со cts кампании), приоритетнее ветки only_cts/_struct_names (per-ct).
    _og = {g for g in (only_gks or ()) if g} or None
    if _og is not None:
        _items = [it for it in _struct_items(slepok, site_type, tp_code)
                  if it.get("ct") in set(cts) and (it.get("gk") or "") in _og]
    else:
        _items = ([] if (only_cts or _struct_names)
                  else [it for it in _struct_items(slepok, site_type, tp_code)
                        if it.get("ct") in set(cts)])
    _multi = bool(_items) and any(v > 1 for v in Counter(it["ct"] for it in _items).values())
    if _multi:
        _units = [(it["ct"], it.get("gk") or "", it.get("name") or "") for it in _items]
    else:
        _units = [(ct, "", "") for ct in cts]
    groups = []
    # Pre-pass: прогрев кэша link_check параллельно (6 потоков). Основной цикл ниже
    # вызывает _resolve_url(model_href) — теперь это будет cache-hit. (#LINK_CHECK_404_FALLBACK)
    _batch_hrefs: list[str] = []
    for _pct, _pgk, _puname in _units:
        _pgrp_pack = (pack.get(_pct, {}).get("_groups") or {}).get(_pgk) if _pgk else None
        _pdata = _pgrp_pack or pack.get(_pct) or {}
        if not _pdata.get("positive"):
            continue
        _praw = ((_struct_names.get(_pct) or ct_name.get(_pct) or _pct) if _struct_names
                 else (ct_name.get(_pct) or ct_model.get(_pct) or _pct))
        _preal = _valid_pack_brand_name(_pct, _praw)
        _pbrand = _preal if _struct_names else (_preal or "Авто")
        _batch_hrefs.append(_pack_group_href(_pct, _pbrand, _preal, _feed_urls, href, site_type))
    _resolve_urls_batch(_batch_hrefs)

    # Аудитории структуры {gk → [(id условия, имя)]} — читается один раз на кампанию.
    # Зеркало token-пути tp1/tp5 (_build_tp1_from_pack:1063). Без этого на token/DIRECT_API_FIRST
    # пути tp2/tp4 группы уходили в Grid с g["audiences"]=None, и аудитории доезжали только
    # cookie-фолбэком (_tp1_pack_groups:2224).
    _aud_by_gk = _cs_aud.struct_audiences_by_gk(slepok, site_type, tp_code)

    for ct, _gk, _uname in _units:
        _grp_pack = (pack.get(ct, {}).get("_groups") or {}).get(_gk) if _gk else None
        data = _grp_pack or pack.get(ct) or {}
        if not data.get("positive"):
            continue                            # нет ключей в паке — пропускаем модель
        # Не-авто (dmp): имя из кодера→структуры, БЕЗ авто-фида и БЕЗ фолбэка «Авто»; brand для
        # текстов/тайтлов остаётся пустым у тем (не марок) → «Авто» не течёт в заголовки dmp.
        # Авто-слепки: прежнее поведение (фид-фолбэк ct_model + «Авто»).
        if _struct_names:
            # Приоритет СТРУКТУРЫ (см. _struct_ct_names): у не-авто слепка справочник марок
            # (_ag_part1_map = gsheet_naming) НЕ авторитет — авто-ct может совпасть с темой
            # слепка (ct0084: авто «Faw Bestune T77» ↔ dmp «Конкуренты») и подменить тему марка-брендом.
            raw_name = _struct_names.get(ct) or ct_name.get(ct) or ct
            _real_brand = _valid_pack_brand_name(ct, raw_name)    # тема → "" → не авто-лексика
            brand = _real_brand
        else:
            raw_name = ct_name.get(ct) or ct_model.get(ct) or ct
            _real_brand = _valid_pack_brand_name(ct, raw_name)
            brand = _real_brand or "Авто"
        display = _pack_group_display_name(ct, raw_name, brand)   # человекочитаемая тема для ИМЕНИ группы
        # deep-link: фид → формульный слаг. ФИКС-A: Марки→/auto/{brand}, Модели→полный путь.
        # BUTTON_404_GENERIC_AVTO: helper принимает _real_brand (не brand) для формульного deep-link.
        # 404-фолбэк: кэш прогрет pre-pass (batch 6 потоков) → cache-hit. (#LINK_CHECK_404_FALLBACK)
        model_href = _resolve_url(_pack_group_href(ct, brand, _real_brand, _feed_urls, href, site_type))
        # Title: шаблон «Новые {brand} в {город}. {акция}» (≤35 симв.) — фолбэк brand[:56]
        # с добивкой до ≥54 через _fill_title (иначе «BAIC» 4 симв. отбрасывается gate-ом <48).
        # dmp (гейт _struct_names): авто-шаблоны неприменимы — title-seed пуст, _rsya_titles ведёт
        # B2B-корпусом; так «Авто»/«Новые Авто…» не попадают в заголовки dmp.
        if _struct_names:
            title = ""
        else:
            title = (_title_from_template(brand, city, slepok=slepok, site_type=site_type)
                     if (not ai_title2 and brand) else _fill_title(brand[:56]))
        ttl2 = (ai_title2[:30] if ai_title2 else _next_title2())   # ИИ-title2 или round-robin из пула
        # В боевом create_set контент генерим ОДИН РАЗ на кампанию/item. Делать M3-вызов на
        # КАЖДУЮ ct-группу нельзя: tp1/tp5 содержат десятки групп, и создание зависает на минуты
        # ещё до первой кампании. Внутри группы используем уже готовый кампанийный набор +
        # локальную rsya-добивку/дедупликацию.
        g_titles = _rsya_titles(brand, city, site_type, ai_title2=ai_title2,
                                base=list(titles or []) + [title, ttl2], pool=_sc_titles,
                                is_brand=(_ct_segment(ct) in ("Марки", "Модели")),
                                slepok=slepok)
        g_texts = _rsya_texts(list(texts or []) + ([text0] if text0 else []), site_type, city, brand)
        # Картинки: общие ct0000-ct0014 → общий пул ct0000; кузова ct0015-ct0018 → свой ct;
        # модели/марки → свой ct.
        tp2_all_images = _creative_images_for_ct(site_type, tp_code, ct, key)
        _keywords = _filter_group_keywords(data.get("positive", []), _ct_segment(ct), brand, city, site_type,
                                           model=(_uname if (_multi and _uname) else brand))
        if ((not autotarget) or keep_keywords) and not _keywords:
            continue
        _g_new = {
            # per-adgroup (multi): кодер-имя строим через _text_group_name — дисплейное имя ГРУППЫ
            # (структурный _uname) уходит в "тему" кодера, а не вместо кодера целиком.
            # dmp (_struct_names): чистая тема без кодера (как прежде).
            "name": (display if _struct_names
                     else _text_group_name(ct, r_code, _uname if (_multi and _uname) else display)),
            # БАГ-13: для «Марки» — убрать ключи «марка+модель» (напр. «Chery Tiggo 8 Pro»)
            # model=: в per-adgroup режиме (_multi) фильтр чужих моделей строим по модели САМОЙ
            # ГРУППЫ (структурный `t`, напр. «Lada Granta Liftback»), а не по ct-уровневому brand
            # («Lada Granta») — иначе под-модели одного ct дискриминируют друг друга и группа
            # уезжает с 0 ключей (лифтбек/седан/универсал попадали в «чужие»).
            "keywords": _keywords,
            "minus": data.get("minus") or [],  # пак-специфичные минус-слова ct/группы; глобальные — только на кампании
            "ct": ct,                            # баг #5: нужен для _ct_segment→seg→adPrice по Марке
            "brand": brand,                      # модель/бренд группы — для adPrice из фида (#2)
            "titles": g_titles,                  # ← Комбинаторное: список заголовков
            "texts": g_texts,                    # ← Комбинаторное: список текстов
            "title": title, "title2": ttl2,      # совместимость (в ResponsiveAd не используются)
            "text": text0 or "Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии.",
            "href": model_href,                  # deep-link страницы модели (если возможен)
            # tp2/tp4 (поиск) — картинки запрещены правилом Семёна; tp5/tp1 — разрешены.
            "image_path": (tp2_all_images[0] if tp2_all_images else None) if tp_code not in ("tp2", "tp4") else None,
            "image_paths": tp2_all_images[:5] if tp_code not in ("tp2", "tp4") else [],
            "callouts": data.get("callouts", []),   # уточнения слепка по модели (из пака)
        }
        # Аудитории структуры (tp2/tp4): только по gk группы, резолв под ЦЕЛЕВОЙ кабинет.
        _cs_aud.attach_to_group(_g_new, login, _aud_by_gk.get(_gk) if _gk else None)
        groups.append(_g_new)
    if not groups:
        return {"skipped": f"пак пуст по {len(cts)} модель-ct"}
    # «Уточнения» (callouts) из пака: создаём общий пул AdExtensions один раз (дедуп) →
    # привязываем ≤4 на объявление каждой группы. Падение callouts не валит сборку.
    co_pool = {}
    try:
        all_co = [c for g in groups for c in (g.get("callouts") or [])]
        co_pool = _ensure_callout_exts(token, login, all_co) if all_co else {}
        for g in groups:
            ids = [co_pool[c] for c in (g.get("callouts") or []) if c in co_pool]
            if ids:
                g["callout_ext_ids"] = ids[:4]
    except Exception:  # noqa: BLE001
        co_pool = {}
    rep = _build_tp2_adgroups(token, login, campaign_id, region_ids, groups,
                              feed_id=feed_id, with_shopping=with_shopping,
                              apply_group_minus=apply_group_minus, autotarget=autotarget,
                              keep_keywords=keep_keywords,
                              site_type=site_type,
                              campaign_is_new=bool(campaign_is_new),
                              search_channel=_cs_aud.is_search_channel(
                                  mode=campaign_mode, tp_code=tp_code))
    rep["cts"] = len(cts)
    rep["groups_built"] = len(groups)
    rep["callouts_pool"] = len(co_pool)
    if donor_note:
        rep["donor"] = donor_note
    return rep

def _build_tp2_from_pack(token: str, login: str, campaign_id: int, slepok: str,
                         site_type: str, region_ids: list, href: str,
                         titles: list | None, texts: list) -> dict:
    """Совместимость: наполнение Поисковой (tp2)."""
    return _build_text_from_pack(token, login, campaign_id, slepok, site_type, "tp2",
                                 region_ids, href, titles, texts)
