"""Фиды копирования: preview, target feed id, валидация feed-map.

Вынесено из copy_engine.py (чистый code-motion, логика не изменена).
DI инъектится copy_engine.configure() фан-аутом; sibling-модули copy_* — прямой импорт (DAG, цикла нет).
"""
from __future__ import annotations

from pathlib import Path

from .copy_snapshot import _copy_read_json

# ── DI (инъектится copy_engine.configure фан-аутом; None до инъекции) ──
_direct_tokens = _feed_key = _filter_allowed_feed_rows = _grid_feeds = _resolve_agency_hint = _token_for_login = _v5_call = None


def configure(deps: dict) -> None:
    """Инъекция DI из blueprint (фан-аут из copy_engine.configure)."""
    globals().update(deps)


_COPY_DEFAULT_FEED_PATH = "/dostup-k-rasprodazhe-live-01-b.xml"


def _copy_listing_fallback_feed_id(rows: list[dict]) -> int | None:
    """Pick an existing ecom/listings target feed when create allow-list has no match."""
    candidates = [f for f in (rows or []) if f.get("listings") or f.get("Listings")]
    if not candidates:
        return None

    def _score(row: dict) -> tuple[int, int, int, int]:
        raw = " ".join(str(row.get(k) or "") for k in ("name", "url", "href", "source", "SourceUrl")).lower()
        return (
            1 if "yandex" in raw else 0,
            1 if "used" in raw or "пробег" in raw else 0,
            1 if "auto" in raw else 0,
            len(row.get("listings") or row.get("Listings") or []),
        )

    for row in sorted(candidates, key=_score, reverse=True):
        try:
            fid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if fid > 0:
            return fid
    return None


def _copy_grid_validate_feed_map(target_login: str, target_agency: str, body: dict,
                                 *, log=lambda m: None) -> dict:
    """Разобрать и провалидировать body.feed_map для ЕПК-ветки (та же логика, что _copy_run_job).

    Возвращает {src_feed_id: tgt_feed_id} только с ЦЕЛЕВЫМИ фидами, ПРИНАДЛЕЖАЩИМИ target-аккаунту.
    Grid недоступен/пустой список фидов → доверяем вводу без валидации (как в _copy_run_job).
    feed_map пуст/битый → {}."""
    raw: dict[str, int] = {}
    fm = body.get("feed_map")
    if not isinstance(fm, dict):
        return {}
    for k, v in fm.items():
        if str(k).strip().isdigit() and str(v).strip().isdigit() and int(v) > 0:
            raw[str(int(k))] = int(v)
    if not raw:
        return {}
    try:
        tgt_ids = {int(f.get("id")) for f in _grid_feeds(target_login, target_agency or _resolve_agency_hint(target_login, ""))
                   if str(f.get("id") or "").strip().isdigit()}
    except Exception:  # noqa: BLE001
        tgt_ids = set()
    if not tgt_ids:
        log("feed_map: фиды target недоступны (grid пуст/ошибка) — feed_map применён без валидации")
        return raw
    valid: dict[str, int] = {}
    for sid, tid in raw.items():
        if tid in tgt_ids:
            valid[sid] = tid
        else:
            log(f"feed_map: целевой фид {tid} не принадлежит {target_login} — пропуск (source {sid})")
    return valid


def _copy_target_feed_id(target_login: str, target_agency: str, workdir: Path,
                         target_domain: str = "") -> int | None:
    maps = _copy_read_json(workdir / "id_maps.json") if (workdir / "id_maps.json").exists() else {}
    for raw in (maps.get("feeds") or {}).values():
        try:
            fid = int(raw)
        except (TypeError, ValueError):
            continue
        if fid > 0:
            return fid
    try:
        all_rows = _grid_feeds(target_login, target_agency)
        rows = _filter_allowed_feed_rows(all_rows)
        wanted_key = _feed_key(_COPY_DEFAULT_FEED_PATH)
        wanted_domain = (target_domain or "").strip().lower()

        def _score(row: dict) -> tuple[int, int, int]:
            raw = " ".join(str(row.get(k) or "") for k in ("name", "url", "href", "source", "SourceUrl"))
            key = _feed_key(raw)
            low = raw.lower()
            return (
                1 if key == wanted_key else 0,
                1 if wanted_domain and wanted_domain in low else 0,
                1 if row.get("listings") else 0,
            )

        for row in sorted(rows, key=_score, reverse=True):
            try:
                fid = int(row.get("id") or 0)
            except (TypeError, ValueError):
                fid = 0
            if fid > 0:
                return fid
        return _copy_listing_fallback_feed_id(all_rows)
    except Exception:  # noqa: BLE001
        pass
    return None


def _copy_feeds_preview(source_login: str, target_login: str, selected_ids: set[int]) -> dict:
    """Данные для секции «Замена фидов»: фиды исходного аккаунта с кол-вом кампаний/групп
    из выбранных (selected_ids), фиды целевого аккаунта. Grid-фиды без балловой стоимости."""
    def _feeds_for(login: str) -> list[dict]:
        agency = _resolve_agency_hint(login, "")
        rows = _grid_feeds(login, agency) or []
        out = []
        for f in rows:
            fid = f.get("id")
            if not str(fid or "").strip().isdigit():
                continue
            out.append({
                "id": int(fid),
                "name": (f.get("name") or "").strip() or f"feed {fid}",
            })
        out.sort(key=lambda r: r["name"].lower())
        return out

    # Task 2: подсчёт выбранных кампаний/групп, использующих каждый исходный фид (v5 adgroups.get)
    feed_camps: dict[int, set] = {}   # feed_id → set of campaign_ids
    feed_groups: dict[int, int] = {}  # feed_id → count of adgroups
    if selected_ids:
        try:
            src_agency = _resolve_agency_hint(source_login, "")
            src_token, _ = _token_for_login(source_login, src_agency, _direct_tokens())
            if src_token:
                params = {
                    "SelectionCriteria": {"CampaignIds": list(selected_ids)},
                    "FieldNames": ["Id", "CampaignId"],
                    "TextAdGroupFeedParamFieldNames": ["FeedId"],
                }
                data = _v5_call("adgroups", "get", src_token, source_login, params)
                for ag in ((data.get("result") or {}).get("AdGroups") or []):
                    fp = ag.get("TextAdGroupFeedParams") or {}
                    fid_raw = fp.get("FeedId")
                    if not fid_raw:
                        continue
                    try:
                        fid = int(fid_raw)
                        cid = int(ag.get("CampaignId") or 0)
                    except (TypeError, ValueError):
                        continue
                    feed_camps.setdefault(fid, set()).add(cid)
                    feed_groups[fid] = feed_groups.get(fid, 0) + 1
        except Exception:  # noqa: BLE001 — best-effort, не ломаем превью
            pass

    source_feeds = []
    for f in _feeds_for(source_login):
        fid = f["id"]
        f["campaigns"] = len(feed_camps.get(fid) or set())
        f["groups"] = feed_groups.get(fid) or 0
        source_feeds.append(f)

    return {"source_feeds": source_feeds, "target_feeds": _feeds_for(target_login)}
