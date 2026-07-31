"""Копирование кампаний Яндекс.Директа 1:1 (обёртки-оркестрация поверх внешнего движка
work/slepki_direktologov/scripts/direct_copy.py) — вынесено из blueprint.py.

Инвариант wiring-hub: НЕ импортирует blueprint. Direct API/токены/Grid-обёртки/очередь-хелперы
инъектятся через configure(deps). Sibling-модули (campaign, grid_create, grid_finalize,
llm_providers) — прямой импорт (цикла нет). copy_geo_morph/copy_steps — ленивые внутри функций.
Прогресс копирования зеркалится в ОБЩУЮ create-очередь (_CREATE_JOBS, инъектится тем же объектом).
"""
from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import threading
import time
from pathlib import Path

from ..core import campaign as cmc
from ..clients import grid_create as gc
from ..clients import grid_finalize as gf
from ..repair import repair_auto as rauto
from ..repair import repair_gate as rgate
from .copy_request import parse_feed_map, parse_image_hashes
from ..llm_providers import _m3_complete, _m3_llm_probe

_HERE = Path(__file__).resolve().parent

# ── DI из blueprint (инъектятся configure; None до инъекции — заглушки для статики) ──
_v5_call = _v501_svc = _v5_err = _token_for_login = _direct_tokens = None
_resolve_agency_hint = _victory_conn_rw = None
_resolve_region = None   # город → (r_code, oblast); ремап r-сегмента кодера при копировании
_grid_list_campaigns = _grid_feeds = _grid_feed_offer_prices = _group_ad_price = None
_grid_set_ad_prices = _grid_update_adaptive_ads = _account_offer_prices = _account_ctx = None
_geo_id = _geo_name_by_id = _geo_type_by_id = _enabled_minus_places = _filter_allowed_feed_rows = _feed_key = None
_enabled_global_minus_places = None   # copy = клон 1:1 без слепка → глобальная таблица минус-площадок (legacy)
_enabled_baseline_minus_places = None   # legacy DI; copy disabledPlaces теперь копируются 1в1 из источника
_create_set_live_verification = _attach_post_repair_verification = _repair_deps = None
_CREATE_JOBS = _CREATE_JOBS_LOCK = _JOB_TERMINAL = None   # общие ОБЪЕКТЫ (mirror в create-карточку)
_job_touch = _job_db_save = _CALLOUT_PER_CAMPAIGN_CAP = None


def configure(deps: dict) -> None:
    """Инъекция зависимостей из blueprint (Direct API/токены/Grid/очередь).

    Фан-аут: те же deps раздаются извлечённым суб-модулям распила (у каждого свой
    globals().update — берёт нужные ключи). Модули импортируются ниже (ре-экспорт распила),
    к моменту вызова configure() (runtime, после load) имена уже связаны.
    """
    globals().update(deps)
    for _sub in (copy_jobs, copy_geo, copy_snapshot, copy_images, copy_metrika,
                 copy_feeds, copy_grid_read, copy_uac, copy_cleanup, copy_grid_steps,
                 copy_grid_unified, copy_postprocess):
        try:
            _sub.configure(deps)
        except Exception:  # noqa: BLE001 — фан-аут best-effort, не валит основную инъекцию
            pass


_DIRECT_COPY_MOD = None


def _direct_copy_module():
    """Ленивая загрузка work/slepki_direktologov/scripts/direct_copy.py как модуля."""
    global _DIRECT_COPY_MOD
    if _DIRECT_COPY_MOD is not None:
        return _DIRECT_COPY_MOD
    mod_path = _HERE.parents[3] / "work" / "slepki_direktologov" / "scripts" / "direct_copy.py"
    spec = importlib.util.spec_from_file_location("seoadvanced_direct_copy", mod_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"не удалось загрузить direct_copy.py: {mod_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _DIRECT_COPY_MOD = mod
    return mod


def _copy_target_campaign_types(target_login: str, target_token: str | None) -> set[str] | None:
    """Return target AvailableCampaignTypes from Direct clients.get.

    None means the check is unavailable, so callers must keep the older path.
    """
    if not target_login or not target_token or not callable(_v5_call):
        return None
    try:
        res = _v5_call(
            "clients",
            "get",
            target_token,
            target_login,
            {"FieldNames": ["AvailableCampaignTypes"]},
        )
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(res, dict) or res.get("error"):
        return None
    clients = (res.get("result") or {}).get("Clients") or []
    if not clients:
        return None
    raw = clients[0].get("AvailableCampaignTypes")
    if not isinstance(raw, list):
        return None
    return {str(x).strip() for x in raw if str(x).strip()}


def _copy_target_add_preflight_error(target_login: str, target_token: str | None) -> str:
    """Return a blocking error when target cannot create campaigns.

    The probe uses campaigns.add with an intentionally invalid past StartDate. On a writable
    account Yandex returns validation 5005 and creates nothing; when the token lacks write access,
    Yandex returns 54 before validation, which is the failure we want to catch before copy work.
    """
    if not target_login or not target_token or not callable(_v5_call):
        return ""
    probe = {
        "Campaigns": [{
            "Name": "__copy_preflight_no_create__",
            "StartDate": "2000-01-01",
            "TextCampaign": {
                "BiddingStrategy": {
                    "Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
                    "Network": {"BiddingStrategyType": "SERVING_OFF"},
                },
            },
        }],
    }
    try:
        res = _v5_call("campaigns", "add", target_token, target_login, probe)
    except Exception as exc:  # noqa: BLE001
        return f"preflight campaigns.add недоступен: {str(exc)[:180]}"

    def _err_is_denied(err: dict) -> bool:
        code = str(err.get("error_code") or err.get("Code") or "").strip()
        text = " ".join(str(err.get(k) or "") for k in (
            "error_string", "error_detail", "Message", "Details",
        ))
        return code == "54" or "нет прав" in text.lower()

    top_error = res.get("error") if isinstance(res, dict) else None
    if isinstance(top_error, dict):
        if _err_is_denied(top_error):
            return (
                f"нет прав на создание кампаний в target-аккаунте {target_login}: "
                "preflight campaigns.add вернул 54 «Нет прав на объект»"
            )
        return ""

    add_results = ((res.get("result") or {}).get("AddResults") or []) if isinstance(res, dict) else []
    for item in add_results:
        for err in (item.get("Errors") or []):
            if isinstance(err, dict) and _err_is_denied(err):
                return (
                    f"нет прав на создание кампаний в target-аккаунте {target_login}: "
                    "preflight campaigns.add вернул 54 «Нет прав на объект»"
                )
    return ""


def _copy_enrich_body_context(body: dict, source_login: str, target_login: str) -> None:
    """Best-effort copy context for post-create repairs/verifiers.

    Copy UI does not historically send create-service fields like agent/site_type, but shared
    repair executors need them to choose the correct content/image pool. Preserve explicit
    client values and fill only the missing ones from account metadata.
    """
    if body.get("agent") and body.get("site_type"):
        return
    try:
        src_ctx = _account_ctx(source_login) or {}
    except Exception:  # noqa: BLE001
        src_ctx = {}
    try:
        tgt_ctx = _account_ctx(target_login) or {}
    except Exception:  # noqa: BLE001
        tgt_ctx = {}
    if not body.get("agent"):
        try:
            from ..pack_resolver import _slepok_key_from_text  # noqa: PLC0415
            body["agent"] = _slepok_key_from_text(src_ctx.get("directologist") or "")
        except Exception:  # noqa: BLE001
            body["agent"] = ""
    if not body.get("site_type"):
        body["site_type"] = (
            (tgt_ctx.get("site_type") or "").strip()
            or (src_ctx.get("site_type") or "").strip()
        )




























# _REGION_ALIASES / _REGION_ALIASES_NORM / _norm_region_alias_key перенесены в copy_geo.py
# (распил): их использует _copy_geo_replacements там же. Ре-экспорт — ниже в блоке copy_geo.


def _copy_geo_filter_negatives(minus_list: list, replacements: list) -> list:
    from .copy_grid_unified import _copy_geo_filter_negatives as _impl
    return _impl(minus_list, replacements)










def _copy_rcode_to_region(r_code: str) -> str:
    from .copy_grid_unified import _copy_rcode_to_region as _impl
    return _impl(r_code)






























def _copy_grid_unified_campaigns(job_id: str, body: dict, selected_grid_rows: list[dict],
                                 workdir: Path) -> dict:
    from .copy_grid_unified import _copy_grid_unified_campaigns as _impl
    return _impl(job_id, body, selected_grid_rows, workdir)


























def _copy_timed(job_id: str, label: str, fn):
    from .copy_postprocess import _copy_timed as _impl
    return _impl(job_id, label, fn)


def _copy_terminal_status_from_results(rows: list[dict]) -> tuple[str, str | None]:
    """Return terminal copy-job status from per-campaign result rows."""
    failed = [r for r in (rows or []) if isinstance(r, dict) and r.get("ok") is False]
    if not failed:
        return "done", None
    samples = []
    for r in failed[:3]:
        name = str(r.get("name") or r.get("source_id") or "campaign").strip()
        err = str(r.get("error") or "не создана").strip()
        samples.append(f"{name}: {err}" if name else err)
    tail = f"; ещё {len(failed) - len(samples)}" if len(failed) > len(samples) else ""
    return "error", ("; ".join(samples) + tail)[:500]


def _copy_terminal_status_from_postprocess(rows: list[dict], cookie_post: dict | None) -> tuple[str, str | None]:
    """Return terminal status including postprocess verification gates."""
    status, error = _copy_terminal_status_from_results(rows)
    post_errors = []
    if isinstance(cookie_post, dict):
        post_errors = [str(e).strip() for e in (cookie_post.get("errors") or []) if str(e).strip()]
    if status == "done" and post_errors:
        return "error", "; ".join(post_errors[:3])[:500]
    return status, error


def _copy_cookie_postprocess(job_id: str, target_login: str, target_agency: str,
                             src_dir: Path, workdir: Path, body: dict) -> dict:
    from .copy_postprocess import _copy_cookie_postprocess as _impl
    return _impl(job_id, target_login, target_agency, src_dir, workdir, body)


















# verify-after-settle: in-job copy_verify бежит ДО статуса done, а привязки (sitelinks/промо/
# картинки) доливаются/индексируются 5-10+ мин ПОСЛЕ done (dcr-демон direct-create-worker +
# async-индексация Яндекса). Доказано: settle-wait 150/240с в джобе и re-verify +300с → 0
# sitelinks, но спот-проверка позже = 9/46 на цели. Поэтому re-verify АДАПТИВНЫЙ: поллит цель
# до появления sitelinks (или таймаут), затем гонит полную сверку и перезаписывает copy_verify.
_COPY_REVERIFY_FIRST_SEC = 240          # первая проба (dcr стартует ~180с после done)
_COPY_REVERIFY_POLL_SEC = 90            # шаг опроса оседания
_COPY_REVERIFY_MAX_SEC = 900            # общий бюджет ожидания оседания (15 мин)
# Ре-лечение ключей после окна ВЫРЕЗАНИЯ: ключи, залитые в момент создания кампании, Яндекс
# частично вырезает через ~10-20 мин (наблюдалось на крупных tp2/Поиск). Ре-add в ОСЕВШУЮ
# кампанию держится (проверено монитором 20 мин). Цикл repair→пауза→re-verify сходится.
_COPY_HEAL_ROUNDS = 8                    # макс. кругов ре-лечения (страховка от зацикливания)
_COPY_HEAL_WAIT_SEC = 200               # пауза между кругами (даём вырезанию проявиться / осесть)
_COPY_HEAL_MIN_SEC = 1200               # НЕ выходить раньше 20 мин: окно вырезания может быть поздним,
#                                         иначе «два чистых круга» ложно сойдутся ДО вырезания


def _copy_target_sitelinks_ready(target_login: str, target_agency: str,
                                 workdir: Path) -> bool:
    """Быстрая проба: появились ли sitelinks на объявлениях цели (индикатор оседания привязок).

    Читает id_maps.json (созданные кампании) + v5 ads.get SitelinkSetId по первым
    кампаниям. Быстрые ссылки могут быть и в TextAd, и в DynamicTextAd; True как только хоть
    одно объявление имеет sitelink. Best-effort → False при сбое.
    """
    try:
        maps = _copy_read_json(workdir / "id_maps.json")
        cids = [int(v) for v in (maps.get("campaigns") or {}).values() if str(v).isdigit()][:6]
        if not cids:
            return False
        tr = _token_for_login(target_login, target_agency or "", _direct_tokens())
        tok = tr[0] if isinstance(tr, (tuple, list)) else tr
        if not tok:
            return False
        r = _v5_call("ads", "get", tok, target_login, {
            "SelectionCriteria": {"CampaignIds": cids},
            "FieldNames": ["Id"],
            "TextAdFieldNames": ["SitelinkSetId"],
            "DynamicTextAdFieldNames": ["SitelinkSetId"],
            "Page": {"Limit": 500}})
        for a in ((r.get("result") or {}).get("Ads") or []):
            if ((a.get("TextAd") or {}).get("SitelinkSetId") or
                    (a.get("DynamicTextAd") or {}).get("SitelinkSetId")):
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _copy_mark_reverify_blockers(job_id: str, verify_result: dict) -> None:
    """Turn delayed copy_verify blockers into a terminal job error."""
    try:
        from .copy_postprocess import _copy_verify_blockers
        blockers = _copy_verify_blockers(verify_result)
    except Exception as exc:  # noqa: BLE001
        blockers = [{"kind": "copy_verify", "dimension": "BLOCKER_CHECK_ERROR",
                     "status": "error", "error": str(exc)[:180]}]
    if not blockers:
        return
    sample = []
    for b in blockers[:6]:
        sample.append(f"{b.get('dimension')}={b.get('status')} {b.get('scope')}")
    msg = f"copy_verify settled gate: {len(blockers)} незакрытых дефектов ({'; '.join(sample)})"
    with _COPY_JOBS_LOCK:
        j = _COPY_JOBS.get(job_id)
        current_status = str((j or {}).get("status") or "")
        res = dict(j["result"]) if (j and isinstance(j.get("result"), dict)) else {}
    if current_status and current_status not in ("done", "settling"):
        return
    res["copy_verify_blockers"] = blockers[:80]
    _copy_job_upsert(job_id, status="error", error=msg[:500], result=res)
    _copy_job_log(job_id, f"⚠️ {msg}")


def _copy_delayed_reverify(job_id: str, src_dir: Path, workdir: Path,
                           target_login: str, target_agency: str,
                           source_login: str = "",
                           geo_pairs: list | None = None) -> None:
    """Отложенная адаптивная пере-сверка source↔target ПОСЛЕ оседания привязок.

    Ждёт первую пробу, затем поллит появление sitelinks до таймаута; как только осело (или
    бюджет исчерпан) — гонит полную copy_verify и перезаписывает результат job'а (UI читает его).
    source_grid ПЕРЕСОБИРАЕТСЯ (иначе build_source_profile уходит в fallback без Grid и недочитывает
    адаптивы titles/bodies/images источника → ложный mismatch adaptive/images).
    Best-effort: ошибки/рестарт сервиса не критичны — in-job copy_verify остаётся как есть.
    """
    try:
        time.sleep(_COPY_REVERIFY_FIRST_SEC)
        _waited = _COPY_REVERIFY_FIRST_SEC
        while _waited < _COPY_REVERIFY_MAX_SEC:
            if _copy_target_sitelinks_ready(target_login, target_agency, workdir):
                break
            _copy_job_log(job_id, f"copy_verify: жду оседания привязок ({_waited}/{_COPY_REVERIFY_MAX_SEC}s)")
            time.sleep(_COPY_REVERIFY_POLL_SEC)
            _waited += _COPY_REVERIFY_POLL_SEC
        _src_grid_rv = None
        if source_login:
            try:
                _src_ag_rv = _resolve_agency_hint(source_login, "")
                _src_cli_rv = cmc.build_client(source_login, account=(_src_ag_rv or None))
                _src_grid_rv = gf.GridClient(source_login, cookie=(_src_cli_rv.sess.headers.get("Cookie") or ""))
            except Exception as _sge:  # noqa: BLE001 — без source_grid профиль источника уйдёт в fallback
                _copy_job_log(job_id, f"copy_verify (осевший): source_grid не пересобран ({str(_sge)[:120]})")
        from . import copy_verify as cv
        _geo_pairs = list(geo_pairs or [])
        vr = cv.run_copy_verification(
            src_dir=src_dir, workdir=workdir,
            target_login=target_login, target_agency=target_agency or "",
            geo_pairs=_geo_pairs, grid=None, source_grid=_src_grid_rv,
            log=(lambda m: _copy_job_log(job_id, m)))
        _s = vr.get("summary") or {}
        _copy_job_log(job_id, f"copy_verify (осевший, +{_waited}s): "
                              f"ok={_s.get('ok')}, mismatch={_s.get('mismatch')}, "
                              f"unreadable={_s.get('unreadable')}")
        with _COPY_JOBS_LOCK:
            j = _COPY_JOBS.get(job_id)
            _res = dict(j["result"]) if (j and isinstance(j.get("result"), dict)) else None
        if _res is not None:
            _res["copy_verify_settled"] = vr
            _cp = _res.get("cookie_postprocess")
            if isinstance(_cp, dict):
                _cp = dict(_cp)
                _cp["copy_verify"] = vr        # UI (_cvAggregate) читает отсюда → покажет осевшее
                _res["cookie_postprocess"] = _cp
            _copy_job_upsert(job_id, result=_res)

        # ── Ре-лечение вырезанных ключей/ссылок (после окна вырезания) ───────────
        # Осевшая сверка выше могла пройти ДО вырезания (ключи ещё на месте → mismatch=0), а Яндекс
        # вырезает часть ключей свежесозданных кампаний через ~10-20 мин. Ре-add в ОСЕВШУЮ кампанию
        # держится (проверено монитором 20 мин). Сходимость — по ДВУМ чистым кругам ПОДРЯД: один
        # чистый круг может быть «ещё до вырезания», поэтому ждём паузу и перепроверяем; два подряд
        # без правок = вырезание прошло и ре-add устойчив. На каждом круге: repair → пауза → свежая
        # сверка (видит текущее живое состояние, в т.ч. отложенное вырезание) → сохранить.
        _clean_streak = 0
        _heal_t0 = time.monotonic()
        for _hround in range(_COPY_HEAL_ROUNDS):
            try:
                _rep = cv.run_copy_repair(
                    vr, src_dir=src_dir, workdir=workdir,
                    target_login=target_login, target_agency=target_agency or "",
                    grid=None, geo_pairs=_geo_pairs,
                    log=(lambda m: _copy_job_log(job_id, m)))
            except Exception as _he:  # noqa: BLE001
                _copy_job_log(job_id, f"copy_repair (осевший, круг {_hround + 1}) error: {str(_he)[:150]}")
                break
            if _rep.get("repairs"):
                _clean_streak = 0
                _copy_job_log(job_id, f"ре-лечение круг {_hround + 1}: правок {len(_rep.get('repairs') or [])}")
            else:
                _clean_streak += 1
                # выходим только если ДВА чистых круга подряд И прошло окно вырезания (иначе
                # ложная сходимость до отложенного вырезания)
                if _clean_streak >= 2 and (time.monotonic() - _heal_t0) >= _COPY_HEAL_MIN_SEC:
                    break
            time.sleep(_COPY_HEAL_WAIT_SEC)          # даём отложенному вырезанию проявиться / ре-add осесть
            vr = cv.run_copy_verification(
                src_dir=src_dir, workdir=workdir,
                target_login=target_login, target_agency=target_agency or "",
                geo_pairs=_geo_pairs, grid=None, source_grid=_src_grid_rv,
                log=(lambda m: _copy_job_log(job_id, m)))
            _s2 = vr.get("summary") or {}
            _copy_job_log(job_id, f"copy_verify (ре-лечение, круг {_hround + 1}): "
                                  f"ok={_s2.get('ok')}, mismatch={_s2.get('mismatch')}")
            with _COPY_JOBS_LOCK:
                j2 = _COPY_JOBS.get(job_id)
                _res2 = dict(j2["result"]) if (j2 and isinstance(j2.get("result"), dict)) else None
            if _res2 is not None:
                _res2["copy_verify_settled"] = vr
                _cp2 = _res2.get("cookie_postprocess")
                if isinstance(_cp2, dict):
                    _cp2 = dict(_cp2)
                    _cp2["copy_verify"] = vr
                    _res2["cookie_postprocess"] = _cp2
                _copy_job_upsert(job_id, result=_res2)
        _copy_mark_reverify_blockers(job_id, vr)
    except Exception as e:  # noqa: BLE001
        _copy_job_log(job_id, f"copy_verify (осевший) error: {str(e)[:200]}")
    finally:
        # Добивка завершена (успех, ошибка или рестарт треда) — сигнализируем фронту.
        _copy_job_upsert(job_id, settling=False)


def _copy_expected_snapshot_count(selected_ids: set[int], selected_uac_rows: list[dict],
                                  v5_campaigns: list[dict] | None = None,
                                  skip_names: set[str] | None = None) -> tuple[int, list[dict]]:
    """Return expected v5 snapshot size and selected v5 campaigns skipped by phase_pull."""
    uac_ids = {int(r.get("id") or 0) for r in (selected_uac_rows or []) if str(r.get("id") or "").isdigit()}
    if v5_campaigns is None:
        return max(0, len(selected_ids) - len(uac_ids)), []

    skip_names = skip_names or set()
    v5_ids: set[int] = set()
    expected_ids: set[int] = set()
    skipped: list[dict] = []
    for c in v5_campaigns or []:
        try:
            cid = int(c.get("Id") or 0)
        except (TypeError, ValueError):
            continue
        if cid not in selected_ids:
            continue
        v5_ids.add(cid)
        name = str(c.get("Name") or "")
        state = str(c.get("State") or "")
        if state == "ARCHIVED" or name in skip_names:
            skipped.append({
                "Id": cid,
                "Name": name,
                "Type": c.get("Type"),
                "State": state,
                "Status": c.get("Status"),
                "reason": "archived" if state == "ARCHIVED" else "skip_name",
            })
        else:
            expected_ids.add(cid)

    # Unknown selected IDs must remain fail-closed: phase_pull will not return them either.
    unknown_non_uac = set(selected_ids) - uac_ids - v5_ids
    return len(expected_ids) + len(unknown_non_uac), skipped


def _copy_grid_campaign_is_archived(row: dict) -> bool:
    status = row.get("status")
    if isinstance(status, dict):
        primary = str(status.get("primaryStatus") or status.get("status") or "").upper()
        archived = bool(status.get("archived"))
    else:
        primary = str(status or "").upper()
        archived = bool(row.get("archived"))
    state = str(row.get("state") or row.get("State") or "").upper()
    return archived or primary == "ARCHIVED" or state == "ARCHIVED"


def _copy_grid_archived_skip_row(row: dict) -> dict:
    return {
        "Id": int(row.get("id") or row.get("Id") or 0),
        "Name": row.get("name") or row.get("Name"),
        "Type": row.get("typename") or row.get("type"),
        "State": "ARCHIVED",
        "Status": row.get("status"),
        "reason": "archived",
        "source": "grid",
    }


def _copy_selected_skip_error(selected_ids: set[int], selected_uac_rows: list[dict],
                              skipped_v5_snapshot: list[dict],
                              skipped_grid_snapshot: list[dict] | None = None) -> str:
    """Human-readable fail-fast error when all requested campaigns are intentionally skipped."""
    skipped = list(skipped_v5_snapshot or []) + list(skipped_grid_snapshot or [])
    if not selected_ids or selected_uac_rows or len(skipped) < len(selected_ids):
        return ""
    reasons = {str(x.get("reason") or "") for x in skipped}
    if reasons == {"archived"}:
        return (
            f"все выбранные кампании архивные ({len(selected_ids)}) — "
            "ARCHIVED не копируем; выберите активные/остановленные/черновики"
        )
    return (
        f"все выбранные кампании пропущены ({len(selected_ids)}): "
        + ", ".join(sorted(r for r in reasons if r)[:4])
    )


def _copy_upload_terminal_error(workdir: Path, expected_campaigns: int) -> tuple[str, list[str]]:
    """Classify full upload failure before cookie postprocess hides the real cause."""
    maps = _copy_read_json(workdir / "id_maps.json") if (workdir / "id_maps.json").exists() else {}
    if (maps.get("campaigns") or {}) or expected_campaigns <= 0:
        return "", []
    log_rows = _copy_read_json(workdir / "_upload_log.json") if (workdir / "_upload_log.json").exists() else []
    campaign_errors = [
        str(row) for row in (log_rows or [])
        if str(row).startswith("кампания ") and " FAIL:" in str(row)
    ]
    if not campaign_errors:
        return "", []
    if all("[campaigns.add] 54" in row or "Нет прав" in row for row in campaign_errors):
        return (
            f"нет прав на создание кампаний в target-аккаунте: campaigns.add вернул 54 "
            f"для {len(campaign_errors)}/{expected_campaigns}; проверьте управляющее агентство/токен target",
            campaign_errors,
        )
    return (
        "campaigns.add не создал ни одной кампании: " + "; ".join(campaign_errors[:3])[:420],
        campaign_errors,
    )


def _copy_run_job(job_id: str, body: dict) -> None:
    source_login = (body.get("source_login") or "").strip()
    target_login = (body.get("target_login") or "").strip()
    selected_ids = {int(x) for x in (body.get("campaign_ids") or []) if str(x).isdigit()}
    counter_id = int(body.get("counter_id") or 0)
    goal_id = int(body.get("goal_id") or 0)
    target_domain = (body.get("target_domain") or "").strip()
    target_city = (body.get("target_city") or "").strip()
    target_region = (body.get("target_region") or "").strip()
    target_feed_url = (body.get("target_feed_url") or _COPY_DEFAULT_FEED_PATH).strip()
    mode = (body.get("mode") or "auto").strip()
    geo_mode = (body.get("geo_mode") or "replace").strip()
    target_agency_hint = (body.get("agency") or "").strip()
    provided_image_hashes = parse_image_hashes(body)
    _copy_enrich_body_context(body, source_login, target_login)
    # Пофидовая замена (source_feed_id → target_feed_id, только существующие фиды target-аккаунта).
    # Пусто → поведение как раньше (единый target_feed_url / авто-пересоздание URL-фидов).
    # mode="other": явный feed_map клиента имеет приоритет; если его нет, строим auto-match.
    feed_map_raw: dict[str, int] = parse_feed_map(body)
    if mode == "other":
        if feed_map_raw:
            _copy_job_log(job_id, f"feed_map: mode=other использует явную карту клиента ({len(feed_map_raw)})")
        else:
            # Авто-подбор фидов — та же эвристика, что JS _feedMatchTarget (full path → filename match)
            try:
                feed_map_raw = _copy_auto_feed_map(
                    source_login,
                    target_login,
                    target_agency_hint=target_agency_hint,
                )
            except Exception:  # noqa: BLE001 — best-effort
                feed_map_raw = {}
    elif not feed_map_raw:
        # Auto-вкладка тоже копирует tp7/UAC product-кампании. Для них v5 snapshot пустой,
        # а единый fallback-фид может не найтись из-за create allow-list. Сопоставление
        # source feed → target feed по basename/path даёт точный used-* фид, если он есть.
        try:
            feed_map_raw = _copy_auto_feed_map(
                source_login,
                target_login,
                target_agency_hint=target_agency_hint,
            )
            if feed_map_raw:
                _copy_job_log(job_id, f"feed_map: auto-match source→target ({len(feed_map_raw)})")
        except Exception:  # noqa: BLE001 — best-effort
            feed_map_raw = {}
    use_feed_map = bool(feed_map_raw)
    # target_cleanup: 'none' | 'delete_drafts' | 'archive' — очистка цели ДО копирования.
    # Инициализируем вне try, чтобы cleanup_result не потерялся при ошибке.
    target_cleanup = (body.get("target_cleanup") or "none").strip()
    if target_cleanup not in ("none", "delete_drafts", "archive"):
        target_cleanup = "none"
    cleanup_target_ids = [
        int(x) for x in (body.get("_copy_retry_cleanup_target_ids") or [])
        if str(x).isdigit() and int(x) > 0
    ]
    cleanup_result: dict | None = None
    target_token = ""
    target_token_agency = ""

    def _run_target_cleanup(progress: int = 45) -> None:
        nonlocal cleanup_result
        if target_cleanup == "none" or cleanup_result is not None:
            return
        _copy_job_upsert(job_id, progress=progress)
        _copy_job_log(job_id, f"cleanup: начало ({target_cleanup}) на {target_login}")
        target_ag_cleanup = target_token_agency or body.get("agency") or _resolve_agency_hint(target_login, "")
        cleanup_result = _copy_target_cleanup(
            job_id, target_login, target_ag_cleanup, target_cleanup,
            campaign_ids=cleanup_target_ids or None,
        )
        for err in (cleanup_result.get("errors") or [])[:5]:
            _copy_job_log(job_id, f"cleanup предупреждение: {err}")
        _copy_job_upsert(job_id, result={"cleanup": cleanup_result})

    def _copy_validated_feed_map(raw_map: dict[str, int], target_agency_hint: str) -> dict[str, int]:
        if not raw_map:
            return {}
        _tgt_feed_ids_ok = True
        try:
            _tgt_feed_ids = {
                int(f.get("id")) for f in _grid_feeds(target_login, target_agency_hint)
                if str(f.get("id") or "").strip().isdigit()
            }
        except Exception:  # noqa: BLE001
            _tgt_feed_ids = set()
            _tgt_feed_ids_ok = False
        if not _tgt_feed_ids_ok or not _tgt_feed_ids:
            # Grid недоступен или вернул пустой список — не можем проверить, доверяем вводу
            _copy_job_log(job_id, "feed_map: не удалось получить фиды target (grid недоступен или список пуст) — feed_map применён без валидации")
            return dict(raw_map)

        out: dict[str, int] = {}
        for _sid, _tid in raw_map.items():
            if _tid in _tgt_feed_ids:
                out[_sid] = _tid
            else:
                _copy_job_log(job_id, f"feed_map: целевой фид {_tid} не принадлежит {target_login} — пропуск (source {_sid})")
        return out

    try:
        tmp_root = Path(tempfile.gettempdir())
        tmp_root.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix=f"direct-copy-{job_id[:8]}-", dir=str(tmp_root)))
        src_dir = workdir / "source"
        _copy_job_upsert(job_id, status="running", progress=5, workdir=str(workdir))
        dc = _direct_copy_module()
        selected_grid_rows = _copy_selected_grid_campaigns(source_login, selected_ids)
        # Кросс-чек с v5 (авторитетный, стабильный источник типа). Grid-typename флейкует:
        # наблюдалось «13 GdUnifiedCampaign» на кампаниях, которые v5 стабильно отдаёт как
        # TEXT_CAMPAIGN → неверный grid-cookie путь → битый CopyCamp-снапшот (EOF@305) и падение
        # ПОСЛЕ delete_drafts. Кампанию, которую v5 видит как НЕ-ЕПК (текст/динамика/приложение),
        # НИКОГДА не гоним grid-unified путём — только v5-pull (как в рабочем прогоне). Настоящие
        # UAC/ЕПК-черновики v5 не отдаёт → в _v5_native их нет → grid-путь для них сохраняется.
        _v5_native: set[int] = set()
        _v5_campaigns_for_expected: list[dict] | None = []
        try:
            _st_x, _sa_x = _token_for_login(source_login, _resolve_agency_hint(source_login, ""), _direct_tokens())
            _vr_x = _v5_call("campaigns", "get", _st_x, source_login,
                             {"SelectionCriteria": {"Ids": list(selected_ids)},
                              "FieldNames": ["Id", "Name", "Type", "State", "Status"]})
            for _cx in ((_vr_x.get("result") or {}).get("Campaigns") or []):
                _v5_campaigns_for_expected.append(dict(_cx))
                if str(_cx.get("Type") or "") in (
                    "TEXT_CAMPAIGN", "DYNAMIC_TEXT_CAMPAIGN", "MOBILE_APP_CAMPAIGN", "SMART_CAMPAIGN",
                ):
                    _v5_native.add(int(_cx.get("Id") or 0))
        except Exception as _ex:  # noqa: BLE001 — кросс-чек best-effort; при сбое остаётся прежняя grid-логика
            _v5_campaigns_for_expected = None
            _copy_job_log(job_id, f"v5-кросс-чек типов недоступен ({str(_ex)[:80]}) — grid-классификация как есть")
        skipped_grid_snapshot: list[dict] = []
        for _gr in selected_grid_rows:
            try:
                _gid = int(_gr.get("id") or _gr.get("Id") or 0)
            except (TypeError, ValueError):
                continue
            if _gid not in selected_ids or _gid in _v5_native:
                continue
            if _copy_grid_campaign_is_archived(_gr):
                skipped_grid_snapshot.append(_copy_grid_archived_skip_row(_gr))
        skipped_grid_ids = {int(x.get("Id") or 0) for x in skipped_grid_snapshot}
        if skipped_grid_ids:
            _copy_job_log(
                job_id,
                "grid-cookie: пропущено ARCHIVED Master/Product кампаний: "
                + ", ".join(str(x) for x in sorted(skipped_grid_ids)[:8])
            )
            selected_grid_rows = [
                r for r in selected_grid_rows
                if str(r.get("id") or r.get("Id") or "").isdigit()
                and int(r.get("id") or r.get("Id") or 0) not in skipped_grid_ids
            ]
        active_selected_ids = set(selected_ids) - skipped_grid_ids
        selected_uac_rows = [r for r in selected_grid_rows if _copy_is_uac_grid_row(r)]
        if selected_uac_rows:
            _copy_job_log(job_id, f"uac selected: {len(selected_uac_rows)} кампаний через Grid/UAC")
        grid_only_skip_error = _copy_selected_skip_error(selected_ids, selected_uac_rows, [], skipped_grid_snapshot)
        if grid_only_skip_error:
            _copy_job_upsert(
                job_id,
                total=0,
                result={
                    "source_login": source_login,
                    "target_login": target_login,
                    "selected": len(selected_ids),
                    "snapshot": {"campaigns": 0},
                    "skipped_campaigns": skipped_grid_snapshot,
                },
            )
            raise RuntimeError(grid_only_skip_error)
        target_token, target_token_agency = _token_for_login(
            target_login,
            target_agency_hint or _resolve_agency_hint(target_login, ""),
            _direct_tokens(),
        )
        target_add_error = _copy_target_add_preflight_error(target_login, target_token)
        if target_add_error:
            _copy_job_log(job_id, target_add_error)
            _copy_job_upsert(
                job_id,
                status="error",
                progress=100,
                total=0,
                error=target_add_error,
                result={
                    "source_login": source_login,
                    "target_login": target_login,
                    "selected": len(selected_ids),
                    "target_write_denied": "54" in target_add_error or "Нет прав" in target_add_error,
                    "preflight": {"target_campaigns_add": "denied"},
                },
            )
            raise RuntimeError(target_add_error)
        target_types = _copy_target_campaign_types(target_login, target_token)
        grid_convert_rows: list[dict] = []
        if (
            target_types is not None
            and "TEXT_CAMPAIGN" not in target_types
            and "UNIFIED_CAMPAIGN" in target_types
        ):
            grid_convert_rows = [
                r for r in selected_grid_rows
                if str(r.get("typename") or r.get("type") or "") in ("GdTextCampaign", "GdUnifiedCampaign")
            ]
            if grid_convert_rows and len(grid_convert_rows) == len(active_selected_ids):
                _copy_job_log(
                    job_id,
                    f"grid-cookie copy: target не поддерживает TEXT_CAMPAIGN, "
                    f"конвертирую {len(grid_convert_rows)} Text/Unified campaigns в ЕПК",
                )
        early_expected_snapshot: int | None = None
        early_skipped_v5_snapshot: list[dict] = []
        if _v5_campaigns_for_expected is not None:
            early_expected_snapshot, early_skipped_v5_snapshot = _copy_expected_snapshot_count(
                active_selected_ids,
                selected_uac_rows,
                _v5_campaigns_for_expected,
                set(getattr(dc, "SKIP_CAMPAIGN_NAMES", set()) or set()),
            )
            early_skip_error = _copy_selected_skip_error(
                selected_ids, selected_uac_rows, early_skipped_v5_snapshot, skipped_grid_snapshot
            )
            if early_skip_error and not (grid_convert_rows and len(grid_convert_rows) == len(active_selected_ids)):
                _copy_job_log(
                    job_id,
                    "выбранные кампании не копируются: "
                    + ", ".join(
                        f"{x.get('Id')}:{x.get('reason')}"
                        for x in (list(early_skipped_v5_snapshot) + list(skipped_grid_snapshot))[:8]
                    )
                )
                _copy_job_upsert(
                    job_id,
                    total=0,
                    result={
                        "source_login": source_login,
                        "target_login": target_login,
                        "selected": len(selected_ids),
                        "snapshot": {"campaigns": 0},
                        "skipped_campaigns": list(early_skipped_v5_snapshot) + list(skipped_grid_snapshot),
                    },
                )
                raise RuntimeError(early_skip_error)
        selected_unified_rows = [
            r for r in selected_grid_rows
            if str(r.get("typename") or r.get("type") or "") == "GdUnifiedCampaign"
            and int(r.get("id") or 0) not in _v5_native
        ]
        grid_only_rows = selected_unified_rows
        grid_only_reason = "Unified campaigns без Direct API баллов"
        if grid_convert_rows and len(grid_convert_rows) == len(active_selected_ids):
            grid_only_rows = grid_convert_rows
            grid_only_reason = "Text/Unified campaigns → ЕПК (target без TEXT_CAMPAIGN)"

        if grid_only_rows and len(grid_only_rows) == len(active_selected_ids):
            _copy_job_log(job_id, f"grid-cookie copy: {len(grid_only_rows)} {grid_only_reason}")
            _run_target_cleanup(progress=35)
            grid_res = _copy_grid_unified_campaigns(job_id, body, grid_only_rows, workdir)
            if cleanup_result is not None:
                grid_res["cleanup"] = cleanup_result
            status = "done" if not grid_res.get("errors") else "error"
            _copy_job_upsert(
                job_id,
                status=status,
                progress=100,
                result=grid_res,
                error=("; ".join(str(e.get("error") or e) for e in (grid_res.get("errors") or [])[:3])[:500]
                       if grid_res.get("errors") else None),
            )
            return
        _copy_job_log(job_id, f"pull источника {source_login}")
        source_token, source_agency = _token_for_login(
            source_login, _resolve_agency_hint(source_login, ""), _direct_tokens()
        )
        source_cookie_account = source_agency or _resolve_agency_hint(source_login, "") or source_login
        src_auth = dc.find_working_auth(source_login, cookie_account=source_cookie_account)
        _copy_direct_last_touch = {"ts": 0.0, "progress": 5}

        def _copy_direct_heartbeat(label: str) -> None:
            now = time.time()
            if now - _copy_direct_last_touch["ts"] < 20.0:
                return
            _copy_direct_last_touch["ts"] = now
            _copy_job_upsert(job_id, progress=int(_copy_direct_last_touch.get("progress") or 5))

        try:
            src_auth.heartbeat = _copy_direct_heartbeat
        except Exception:  # noqa: BLE001
            pass
        # ФАЗА 1 (П.11/П.10): зафиксировать исходную связь campaign→callouts/promo/sitelinks с ИСТОЧНИКА (Grid).
        # ВАЖНО: вызывается ДО _copy_filter_snapshot — campaign_callouts.json должен существовать
        # во время фильтрации снимка, чтобы campaign-level уточнения (inheritableCallouts) не
        # отфильтровались из callouts.json (ad-level callout_ids=0 → без этого callout_texts=[]).
        # Использует active_selected_ids — архивные Grid-only кампании уже отсеяны выше.
        # Best-effort: недоступность Grid не валит копирование — постпроцесс откатится на union/единичное.
        _asset_pull: dict = {}

        def _pull_source_assets_bg() -> None:
            try:
                from . import copy_steps as _csteps
                _src_cli = cmc.build_client(source_login, account=(source_agency or None))
                _src_grid = gf.GridClient(source_login, cookie=(_src_cli.sess.headers.get("Cookie") or ""))
                _asset_pull["report"] = _csteps.pull_source_campaign_assets(
                    _src_grid, list(active_selected_ids), src_dir, log=(lambda m: _copy_job_log(job_id, m)))
            except Exception as e:  # noqa: BLE001
                _asset_pull["error"] = str(e)[:180]

        _asset_thread = threading.Thread(
            target=_pull_source_assets_bg,
            daemon=True,
            name=f"copy-assets-{job_id[:8]}",
        )
        _asset_thread.start()
        dc.phase_pull(src_dir, src_auth, source_login, selected_campaign_ids=sorted(active_selected_ids))
        _asset_thread.join()
        _pa = _asset_pull.get("report") or {}
        if _pa.get("errors"):
            _copy_job_log(job_id, f"pull source assets warnings: {'; '.join(_pa['errors'][:3])[:220]}")
        if _asset_pull.get("error"):
            _copy_job_log(job_id, f"pull source assets: пропуск ({_asset_pull['error']})")
        meta = _copy_filter_snapshot(src_dir, active_selected_ids)
        if meta.get("dropped_empty_adgroups"):
            _copy_job_log(job_id, f"snapshot: пропущено {meta['dropped_empty_adgroups']} групп без копируемых объявлений (только архивные)")
        # total = ВСЕ выбранные кампании (v5 + UAC), чтобы счётчик «создано N/M» был честным.
        # meta.get("campaigns") = только v5-снапшот (UAC вычтены в expected_snapshot ниже) — НЕ использовать как total.
        _copy_job_upsert(job_id, progress=28, total=max(0, len(selected_ids) - len(skipped_grid_snapshot)))
        _copy_job_log(job_id, f"snapshot отфильтрован: {meta.get('campaigns')} кампаний")
        if early_expected_snapshot is None:
            expected_snapshot, skipped_v5_snapshot = _copy_expected_snapshot_count(
                active_selected_ids,
                selected_uac_rows,
                _v5_campaigns_for_expected,
                set(getattr(dc, "SKIP_CAMPAIGN_NAMES", set()) or set()),
            )
        else:
            expected_snapshot, skipped_v5_snapshot = early_expected_snapshot, early_skipped_v5_snapshot
        if skipped_v5_snapshot:
            _copy_job_log(
                job_id,
                "snapshot: пропущено v5-кампаний не в скоупе pull: "
                + ", ".join(f"{x.get('Id')}:{x.get('reason')}" for x in skipped_v5_snapshot[:8])
            )
            _copy_job_upsert(
                job_id,
                total=max(0, len(selected_ids) - len(skipped_v5_snapshot) - len(skipped_grid_snapshot)),
            )
        skip_error = _copy_selected_skip_error(
            selected_ids, selected_uac_rows, skipped_v5_snapshot, skipped_grid_snapshot
        )
        if skip_error:
            raise RuntimeError(skip_error)
        if int(meta.get("campaigns") or 0) != expected_snapshot:
            raise RuntimeError(
                f"snapshot неполный: выбрано {len(selected_ids)}, UAC/tp6/tp7 {len(selected_uac_rows)}, "
                f"в v5 snapshot {meta.get('campaigns')} вместо {expected_snapshot}"
            )
        target_cookie_account = (
            target_token_agency or target_agency_hint or _resolve_agency_hint(target_login, "") or target_login
        )
        tgt_auth = dc.find_working_auth(target_login, cookie_account=target_cookie_account)
        try:
            tgt_auth.heartbeat = _copy_direct_heartbeat
        except Exception:  # noqa: BLE001
            pass

        if not feed_map_raw:
            try:
                feed_map_raw = _copy_auto_feed_map_from_snapshot(
                    src_dir,
                    target_login,
                    target_agency_hint=target_token_agency or target_agency_hint,
                )
                if feed_map_raw:
                    _copy_job_log(job_id, f"feed_map: snapshot auto-match source→target ({len(feed_map_raw)})")
            except Exception:  # noqa: BLE001 — best-effort
                feed_map_raw = {}

        # Пофидовая замена: валидируем целевые фиды по аккаунту до skip/preflight.
        # Так кампания не пройдет дальше по невалидной карте и не уйдет на default/source feed.
        feed_map_valid = _copy_validated_feed_map(
            feed_map_raw,
            target_token_agency or target_agency_hint or _resolve_agency_hint(target_login, ""),
        )
        use_feed_map = bool(feed_map_valid)

        # Task 4: пропустить кампании с фидами без замены (только если feed_map задан)
        if feed_map_valid:
            skipped_cids = _copy_skip_unmapped_feed_campaigns(
                src_dir, feed_map_valid, log=lambda m: _copy_job_log(job_id, m))
            if skipped_cids:
                # total = активный selected минус пропущенные по feed-map; UAC в remaining остаются
                remaining = len(active_selected_ids) - len(skipped_cids)
                _copy_job_upsert(job_id, total=max(0, remaining))
        target_feed_abs = dc.build_url_feed_url(target_domain, target_feed_url) if target_feed_url else ""
        audit = _copy_snapshot_preflight(
            src_dir,
            # feed_map покрывает фиды пофидово → сентинел удовлетворяет проверку «целевой фид задан».
            target_feed_url=(target_feed_abs or ("__feed_map__" if use_feed_map else "")),
            target_city=target_city,
            target_region=target_region,
            geo_mode=geo_mode,
        )
        _copy_job_upsert(job_id, preflight=audit)
        for msg in audit.get("warnings") or []:
            _copy_job_log(job_id, f"preflight warning: {msg}")
        if audit.get("critical"):
            for msg in audit["critical"]:
                _copy_job_log(job_id, f"preflight error: {msg}")
            raise RuntimeError("preflight остановил копирование: " + "; ".join(audit["critical"][:3]))

        # ── Ветка (б): гео-морфология ─────────────────────────────────────────────────
        if mode == "other" and geo_mode == "keep":
            # Пропускаем гео-замену целиком: M3 не вызывается, snapshot не трогаем.
            rewrite_meta = {"files": 0, "replacements": 0, "pairs": [], "m3_used": False, "residual_geo": []}
            _copy_job_upsert(job_id, context_rewrite=rewrite_meta)
            _copy_job_log(job_id, "гео: режим 'keep' — морфологическая замена пропущена")
        else:
            source_ctx = _copy_ctx(source_login)
            if mode == "other" and geo_mode == "change":
                # mode="other": гео-замена по выбранному региону (если правило морфологии выполнено).
                # Морфология: только 1 плюс-регион, 0 минусов, тип НЕ World/Country.
                _gids_raw_v5 = body.get("geo_region_ids") or []
                if not _gids_raw_v5:
                    _s = int(body.get("geo_region_id") or 0)
                    _gids_raw_v5 = [_s] if _s else []
                _pos_v5 = [int(x) for x in _gids_raw_v5 if str(x).lstrip("-").isdigit() and int(x) > 0]
                _neg_v5 = [int(x) for x in _gids_raw_v5 if str(x).lstrip("-").isdigit() and int(x) < 0]
                _mtype_v5 = (_geo_type_by_id(_pos_v5[0]) if _geo_type_by_id and _pos_v5 else None) or ""
                _do_morph_v5 = (len(_pos_v5) == 1 and len(_neg_v5) == 0
                                and _mtype_v5 not in ("World", "Country"))
                if _do_morph_v5 and _pos_v5:
                    geo_rname = (_geo_name_by_id(_pos_v5[0]) if _geo_name_by_id else "") or ""
                    if not geo_rname:
                        raise RuntimeError(
                            f"geo_region_id={_pos_v5[0]}: имя региона не найдено в справочнике GeoRegions"
                        )
                    target_ctx = {"city": "", "region": geo_rname}
                    _copy_job_log(job_id, f"гео: 1 регион, меняем тексты: region_id={_pos_v5[0]} name={geo_rname!r}")
                else:
                    # Несколько регионов / есть минусы / страна → тексты не трогаем
                    rewrite_meta = {"files": 0, "replacements": 0, "pairs": [], "m3_used": False, "residual_geo": []}
                    _copy_job_upsert(job_id, context_rewrite=rewrite_meta)
                    _copy_job_log(job_id,
                                  f"гео: {len(_pos_v5)} регион(ов), {len(_neg_v5)} исключений"
                                  f" → тексты не меняем (RegionIds ставим)")
                    target_ctx = None  # сигнал: морфологию пропустить
            else:
                target_ctx = _copy_ctx(target_login)
                target_ctx["city"] = target_city or target_ctx.get("city") or ""
                target_ctx["region"] = target_region or target_ctx.get("region") or ""
            if target_ctx is not None:
                rewrite_meta = _copy_rewrite_snapshot_context(
                    src_dir, source_ctx, target_ctx, log=(lambda m: _copy_job_log(job_id, m))
                )
                _copy_job_upsert(job_id, context_rewrite=rewrite_meta)
                if rewrite_meta.get("m3_used"):
                    _copy_job_log(job_id, "гео-склонения: M3 парадигма падежей применена")
                else:
                    _copy_job_log(job_id, "гео-склонения: M3 недоступен, замена только по границам слов (именительный)")
                if rewrite_meta.get("m3_failed"):
                    _copy_job_log(job_id, f"гео-склонения: фолбэк для {', '.join(rewrite_meta['m3_failed'][:4])}")
                if rewrite_meta.get("replacements"):
                    _copy_job_log(job_id, f"гео в snapshot заменено: {rewrite_meta['replacements']} в {rewrite_meta['files']} файлах")
                if mode != "other" and rewrite_meta.get("residual_geo"):
                    # Для mode="other" residual-проверку пропускаем: источник может быть вне Краснодара/etc.
                    sample = ", ".join(rewrite_meta["residual_geo"][:5])
                    raise RuntimeError(f"после гео-замены в snapshot осталось старое гео: {sample}")

        src_domain = dc.infer_source_domain(src_dir)
        tgt_region_id = None
        geo_source = ""
        # ── Ветка (а): RegionIds ──────────────────────────────────────────────────────
        if mode == "other" and geo_mode == "keep":
            # GeoRegionId копируется из снимка как есть (phase_upload c tgt_region_id=None).
            _copy_job_log(job_id, "гео: режим 'keep' — RegionIds из источника без изменений")
        elif mode == "other" and geo_mode == "change":
            # tgt_region_id = список int (положительные + отрицательные); backward compat со скаляром.
            _gids_v5_b = body.get("geo_region_ids") or []
            if not _gids_v5_b:
                _s_b = int(body.get("geo_region_id") or 0)
                _gids_v5_b = [_s_b] if _s_b else []
            _gids_v5 = [int(x) for x in _gids_v5_b if str(x).lstrip("-").isdigit() and int(x) != 0]
            if not _gids_v5:
                raise RuntimeError("mode='other', geo_mode='change': geo_region_ids не задан в запросе")
            tgt_region_id = _gids_v5   # phase_upload получает список
            geo_source = f"other:region_ids={_gids_v5[:5]!r}"
            _copy_job_log(job_id, f"гео: режим 'change', region_ids={_gids_v5[:10]!r}")
        else:
            target_region = _copy_canonical_region_name(target_region)
            if target_city or target_region:
                local_gid, local_geo_name = _copy_geo_id_for_target(target_city, target_region)
                if local_gid:
                    tgt_region_id = int(local_gid)
                    geo_source = f"dict:{local_geo_name}"
                else:
                    tgt_region_id = dc.lookup_geo_region_id(target_city, target_region, tgt_auth, target_login)
                    geo_source = "direct_copy"
                if not tgt_region_id:
                    raise RuntimeError(f"не найден GeoRegionId для целевого гео: city={target_city!r}, region={target_region!r}")
        body["_copy_source_domain"] = src_domain
        # Ремап r-сегмента кодера в именах групп/кампаний снимка ДО phase_upload.
        # Гео-переписывание снимка меняет только СЛОВОФОРМЫ, а регион в кодере зашит КОДОМ
        # (`ag_part4`: r0088=Краснодарский край) — словами его не задеть. В Grid/ЕПК-ветке ремап
        # есть (copy_grid_unified.py:326), в v5-ветке имя группы уезжало как есть
        # (direct_copy.py:1453 `"Name": g["Name"]`) → на цели оставался r источника
        # (живой баг porg-mjyh6hjv→porg-ln7tz7xh, 2026-07-27: 102 группы с r0088 вместо r0066).
        # mode='other' — r не ремапим (как в ЕПК-ветке: чужая сфера, кодера может не быть).
        if mode != "other":
            _v5_r_code = _copy_target_region_code(target_city, target_region)
            if _v5_r_code:
                _r_renamed = _copy_remap_snapshot_region_code(src_dir, _v5_r_code)
                _copy_job_log(job_id, f"кодер: r-сегмент региона → {_v5_r_code} "
                                      f"(групп: {_r_renamed.get('adgroups', 0)}, "
                                      f"кампаний: {_r_renamed.get('campaigns', 0)})")
        # Пофидовая замена: валидируем целевые фиды по аккаунту (только СВОИ фиды) и предзаписываем
        # id_maps.json — phase_upload загрузит его и подставит целевые фиды вместо единого forced-фида.
        if use_feed_map:
            _copy_preseed_feed_maps(workdir, feed_map_valid)
            _copy_job_log(job_id, f"пофидовая замена активна: {feed_map_valid}")
        _copy_job_upsert(job_id, progress=42, feed_map=feed_map_valid)

        # Очистка цели должна идти после source pull/preflight/geo/feed validation, но до upload.
        # Иначе внешний API мог удалить/архивировать кампании цели, а затем упасть на неполном
        # snapshot, неизвестном GeoRegionId или битой карте фидов.
        _run_target_cleanup(progress=45)

        _copy_job_log(job_id, f"upload в {target_login} (домен={target_domain or '—'}, geo={target_city or target_region or '—'} #{tgt_region_id or '—'} {geo_source}, feed={'по карте' if use_feed_map else (target_feed_abs or '—')})")
        _copy_direct_last_touch["progress"] = 45

        def _copy_upload_progress(stage: str, done: int, total: int) -> None:
            total_safe = max(1, int(total or 0))
            progress = min(81, 45 + int(36 * max(0, int(done or 0)) / total_safe))
            _copy_direct_last_touch["progress"] = progress
            _copy_job_upsert(job_id, progress=progress)
            key = f"{stage}:{done}/{total}"
            if _copy_direct_last_touch.get("last_progress_log") != key:
                _copy_direct_last_touch["last_progress_log"] = key
                _copy_job_log(job_id, f"upload {stage}: {done}/{total}")

        dc.phase_upload(
            src_dir, workdir, tgt_auth, source_login, target_login,
            src_domain, target_domain, tgt_region_id,
            force_feed_url=("" if use_feed_map else target_feed_abs),
            force_feed_name=(None if use_feed_map else (target_feed_abs.rsplit("/", 1)[-1] if target_feed_abs else None)),
            skip_keywords=True,   # ФАЗА 3c п.2: ключи — Grid-first в постпроцессе (0 v5-баллов)
            # mode="other": картинки сайта, загруженные пользователем в целевой аккаунт. Без этого
            # v5-ветка перезаливала картинку ИСТОЧНИКА (тот же контент → тот же хэш) и на объявлениях
            # оставался чужой сайт, а загруженные не использовались (прогон 2026-07-17).
            image_hashes=(provided_image_hashes or None),
            progress_callback=_copy_upload_progress,
        )
        upload_error, upload_errors = _copy_upload_terminal_error(workdir, expected_snapshot)
        if upload_error:
            _copy_job_upsert(
                job_id,
                result={
                    "source_login": source_login,
                    "target_login": target_login,
                    "selected": len(selected_ids),
                    "snapshot": meta,
                    "upload_errors": upload_errors[:20],
                    "skipped_campaigns": list(skipped_grid_snapshot) + list(skipped_v5_snapshot or []),
                    "workdir": str(workdir),
                    "cleanup": cleanup_result,
                },
            )
            raise RuntimeError(upload_error)
        _copy_job_upsert(job_id, progress=82)
        token, _ag = target_token, target_token_agency
        metrika_res = {"updated": 0, "warned": 0}
        if token:
            _copy_job_log(job_id, f"докрутка Метрики: counter={counter_id}, goal={goal_id}")
            metrika_res = _copy_apply_metrika(
                target_login, token, src_dir, workdir, counter_id, goal_id, active_selected_ids, job_id
            )
        target_agency = _ag or target_agency_hint or _resolve_agency_hint(target_login, "")
        uac_copy = {"created": 0, "results": [], "errors": [], "uses_direct_units": False}
        if selected_uac_rows:
            target_feed_id = _copy_target_feed_id(target_login, target_agency or "", workdir, target_domain)
            # tgt_region_id теперь может быть списком (geo_region_ids), скаляром или None
            if isinstance(tgt_region_id, list):
                region_id_list = tgt_region_id if tgt_region_id else [225]
            else:
                region_id_list = [int(tgt_region_id)] if tgt_region_id else [225]
            target_href = _copy_target_href(None, "", target_domain)
            uac_target_r_code = _copy_target_region_code(target_city, target_region) if mode != "other" else ""
            _copy_job_log(job_id, f"uac copy: {len(selected_uac_rows)} → {target_login} (feed={target_feed_id or '—'})")
            uac_copy = _copy_uac_campaigns(
                source_login, target_login, target_agency or "", selected_uac_rows, body,
                target_href=target_href, region_ids=region_id_list, counter_id=counter_id,
                goal_id=goal_id, target_feed_id=target_feed_id, feed_map=feed_map_valid,
                geo_pairs=rewrite_meta.get("pairs") or [], target_r_code=uac_target_r_code,
            )
            body["_copy_uac_results"] = uac_copy.get("results") or []
            if uac_copy.get("errors"):
                for err in uac_copy["errors"][:8]:
                    _copy_job_log(job_id, f"uac copy warning: {err}")
            _copy_job_log(job_id, f"uac copy done: {uac_copy.get('created') or 0} created")
            _copy_job_upsert(job_id, progress=88)   # UAC завершён, ещё не done — обновляем оценку
        _copy_job_log(job_id, "cookie postprocess: уточнения / ShoppingAd / ListingAd / live-check / auto-repair")
        cookie_post = _copy_cookie_postprocess(job_id, target_login, target_agency or "", src_dir, workdir, body)
        if cookie_post.get("errors"):
            for err in cookie_post["errors"][:8]:
                _copy_job_log(job_id, f"cookie postprocess warning: {err}")
        live_status = ((cookie_post.get("live_verification") or {}).get("status") or "")
        if live_status:
            _copy_job_log(job_id, f"live verification: {live_status}")
        skipped_camps = _copy_read_json(src_dir / "campaigns_skipped.json")
        if skipped_v5_snapshot:
            skipped_camps = list(skipped_camps or []) + skipped_v5_snapshot
        if skipped_grid_snapshot:
            skipped_camps = list(skipped_camps or []) + skipped_grid_snapshot
        final_results = cookie_post.get("results") or _copy_build_results(src_dir, workdir)
        final_status, final_error = _copy_terminal_status_from_postprocess(final_results, cookie_post)
        _copy_job_upsert(
            job_id, status=final_status, progress=100, error=final_error,
            result={
                "source_login": source_login,
                "target_login": target_login,
                "selected": len(selected_ids),
                "snapshot": meta,
                "metrika": metrika_res,
                "uac_copy": uac_copy,
                "cookie_postprocess": cookie_post,
                "results": final_results,
                "skipped_campaigns": skipped_camps,
                "live_verification": cookie_post.get("live_verification"),
                "repair_gate": cookie_post.get("repair_gate"),
                "auto_repair": cookie_post.get("auto_repair"),
                "preflight": audit,
                "context_rewrite": rewrite_meta,
                "target_feed_url": target_feed_abs,
                "workdir": str(workdir),
                "cleanup": cleanup_result,
            })
        # verify-after-settle: отложенная пере-сверка после оседания dcr-привязок (см. выше).
        # settling=True → фронт видит «идёт добивка» вместо финала и не останавливает таймер.
        # _copy_delayed_reverify сбрасывает settling=False в finally при любом исходе.
        if body.get("_copy_skip_delayed_reverify"):
            _copy_job_upsert(job_id, settling=False)
            _copy_job_log(job_id, "copy_verify: delayed reverify пропущен по внутреннему флагу")
            return
        try:
            _copy_job_upsert(job_id, settling=True)
            threading.Thread(
                target=_copy_delayed_reverify,
                args=(job_id, src_dir, workdir, target_login, target_agency or "", source_login, rewrite_meta.get("pairs") or []),
                daemon=True, name=f"copy-reverify-{job_id[:8]}").start()
            _copy_job_log(job_id, f"copy_verify: осевшая пере-сверка запланирована (до {_COPY_REVERIFY_MAX_SEC}s ожидания оседания)")
        except Exception as _te:  # noqa: BLE001
            # Тред не стартовал — сбрасываем settling чтобы не замереть навсегда
            _copy_job_upsert(job_id, settling=False)
            _copy_job_log(job_id, f"copy_verify reverify schedule error: {str(_te)[:150]}")
    except BaseException as e:  # noqa: BLE001
        # cleanup_result инициализирован вне try → всегда доступен здесь.
        # Если очистка отработала до падения job — явно включаем её в result,
        # чтобы пользователь видел факт удаления/архивации в карточке статуса.
        _err_result = {"cleanup": cleanup_result} if cleanup_result is not None else None
        _copy_job_upsert(job_id, status="error", error=str(e)[:500], progress=100,
                         **({} if _err_result is None else {"result": _err_result}))
        _copy_job_log(job_id, f"ошибка: {str(e)[:300]}")
    finally:
        # Артефакты оставляем во временной папке до ручной очистки — полезно для отладки id_maps/upload_log.
        pass






# Модульные имена для DI-фан-аута в configure() (см. выше).
from . import copy_jobs, copy_geo, copy_snapshot, copy_images, copy_metrika  # noqa: E402,F401
from . import copy_feeds, copy_grid_read, copy_uac, copy_cleanup, copy_grid_steps  # noqa: E402,F401
from . import copy_grid_unified, copy_postprocess  # noqa: E402,F401

from .copy_jobs import (  # noqa: E402,F401  (ре-экспорт распила)
    _COPY_JOBS, _COPY_JOBS_LOCK, _copy_job_upsert, _copy_mirror_create_job, _copy_job_log, _copy_jobs_recover,
)

from .copy_geo import (  # noqa: E402,F401  (ре-экспорт распила)
    _COPY_R_CODE_RE, _copy_canonical_region_name, _copy_geo_id_for_target, _copy_ctx, _copy_m3_decliner, _copy_build_geo, _copy_geo_replacements, _copy_apply_geo_replacements, _copy_target_region_code, _copy_remap_region_code, _copy_remap_snapshot_region_code, _copy_normalize_campaign_name, _copy_domain_from_href, _copy_target_href,
    _REGION_ALIASES, _REGION_ALIASES_NORM, _norm_region_alias_key, _REGION_ALIAS_DASH_RE,
)

from .copy_snapshot import (  # noqa: E402,F401  (ре-экспорт распила)
    _COPY_JSON_PAYLOADS, _COPY_SUPPORTED_V5_TYPES, _copy_read_json, _copy_write_json, _copy_filter_snapshot, _copy_walk_strings, _copy_scan_payload_terms, _copy_rewrite_snapshot_context, _copy_snapshot_preflight, _copy_build_results, _copy_preseed_feed_maps, _copy_skip_unmapped_feed_campaigns,
)

from .copy_images import (  # noqa: E402,F401  (ре-экспорт распила)
    _copy_grid_ad_image_hashes, _copy_v501_ad_image_hashes, _copy_image_remapper,
)

from .copy_metrika import (  # noqa: E402,F401  (ре-экспорт распила)
    _copy_rewrite_strategy_goal, _copy_apply_metrika,
)

from .copy_feeds import (  # noqa: E402,F401  (ре-экспорт распила)
    _COPY_DEFAULT_FEED_PATH, _copy_target_feed_id, _copy_feeds_preview, _copy_grid_validate_feed_map,
)

from .copy_grid_read import (  # noqa: E402,F401  (ре-экспорт распила)
    _copy_selected_grid_campaigns, _copy_grid_read_selected, _copy_grid_campaign_spec,
)

from .copy_uac import (  # noqa: E402,F401  (ре-экспорт распила)
    _copy_is_uac_grid_row, _copy_uac_value, _copy_uac_strings, _copy_uac_sitelinks, _copy_uac_media_urls, _copy_uac_filter_list, _copy_uac_campaigns,
)

from .copy_cleanup import (  # noqa: E402,F401  (ре-экспорт распила)
    _copy_target_campaigns_info, _copy_cleanup_uac_drafts, _copy_target_cleanup,
)

from .copy_grid_steps import (  # noqa: E402,F401  (ре-экспорт распила)
    _copy_grid_bridge_callouts, _copy_grid_unified_steps, _copy_make_video_resolver,
)

# ── copy_other: ре-экспорт функций вкладки «Прочие сферы» ───────────────────
# copy_other не импортирует copy_engine на уровне модуля → цикла нет.
# DI (_grid_feeds, _resolve_agency_hint, _copy_feeds_preview) берётся из
# copy_engine._xxx в рантайме (ленивый import внутри тел функций copy_other).
from .copy_other import (                                    # noqa: E402
    _ARCHIVE_MAX_FILES, _ARCHIVE_MAX_BYTES, _IMAGE_EXTS,
    _feed_url_path, _feed_url_file, _feed_auto_match_one,
    _copy_auto_feed_map, _copy_auto_feed_map_from_snapshot, _copy_feeds_check,
    _extract_archive_images, _copy_images_upload,
)
