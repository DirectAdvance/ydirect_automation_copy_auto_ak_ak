"""Роуты вкладки «Смена названий» редактора контента Директа.

Отдельный модуль по образцу ``content_images_routes`` — ``routes_content_editor``
уже большой, раздувать его дальше нет смысла.

Что делает вкладка: позволяет массово переименовать кампании и группы объявлений
в выбранном аккаунте без изменения каких-либо других настроек. Обязателен экран
предпросмотра «было → станет» перед apply (review-first).

Кампании переименовываются через официальный v5 campaigns.update(Name), если worker
получил OAuth-токен. GridClient.set_campaign_names остаётся fallback для путей без
токена. Группы — через GridClient.set_adgroup_names (full-object RMW: читаем объект
группы → заменяем только adGroupName → пишем обратно через UpdateUnifiedAdGroups;
таргетинг/ключи/регионы/минус-слова не трогаются).
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from flask import jsonify, request

log = logging.getLogger(__name__)


# ─────────────────────── вспомогательная функция воркера ────────────────────


def run_rename(login: str, payload: dict, *,
               grid_client_factory: Callable | None = None) -> dict:
    """Применить переименование кампаний и/или групп.

    Вызывается из _do_replace (routes_content_editor.py) для задания типа
    ``campaign_rename``. Возвращает dict формата ``{"replaced": N, "errors": [...]}``.

    Намеренно НЕ импортирует из routes_content_editor (циклическая зависимость):
    используем grid_client_factory или строим GridClient напрямую.
    """
    from . import grid_finalize as gf

    # ── Разбор payload ────────────────────────────────────────────────────────
    campaign_renames: dict[int, str] = {}
    for k, v in (payload.get("campaign_renames") or {}).items():
        try:
            cid = int(k)
        except (TypeError, ValueError):
            continue
        name = str(v or "").strip()
        if cid > 0 and name:
            campaign_renames[cid] = name

    adgroup_renames: dict[int, str] = {}
    for k, v in (payload.get("adgroup_renames") or {}).items():
        try:
            gid = int(k)
        except (TypeError, ValueError):
            continue
        name = str(v or "").strip()
        if gid > 0 and name:
            adgroup_renames[gid] = name

    adgroup_campaign_ids: list[int] = []
    for raw in (payload.get("adgroup_campaign_ids") or []):
        try:
            cid = int(raw)
            if cid > 0:
                adgroup_campaign_ids.append(cid)
        except (TypeError, ValueError):
            pass

    def _get_grid(lg: str):
        if grid_client_factory:
            return grid_client_factory(lg)
        return gf.GridClient(lg)

    replaced = 0
    errors: list[str] = []

    # ── Переименование кампаний ───────────────────────────────────────────────
    if campaign_renames:
        grid = None
        try:
            grid = _get_grid(login)
            updated = grid.set_campaign_names(campaign_renames)
            replaced += len(updated)
        except gf.GridFinalizeError as e:
            if len(campaign_renames) <= 1:
                cid, name = next(iter(campaign_renames.items()))
                try:
                    updated = (grid or _get_grid(login)).set_campaign_names({cid: name})
                    replaced += len(updated)
                except Exception as one_e:  # noqa: BLE001
                    errors.append(f"переименование кампании {cid}: {str(one_e)[:240]}")
            else:
                # Grid иногда стабильно отдаёт internal server error на bulk UpdateCampaigns,
                # хотя одиночные id из той же пачки проходят. Переименование идемпотентно,
                # поэтому безопасно добиваем по одной кампании.
                for cid, name in campaign_renames.items():
                    try:
                        updated = (grid or _get_grid(login)).set_campaign_names({cid: name})
                        replaced += len(updated)
                    except Exception as one_e:  # noqa: BLE001
                        errors.append(f"переименование кампании {cid}: {str(one_e)[:240]}")
        except Exception as e:  # noqa: BLE001
            if len(campaign_renames) <= 1:
                cid, name = next(iter(campaign_renames.items()))
                try:
                    updated = (grid or _get_grid(login)).set_campaign_names({cid: name})
                    replaced += len(updated)
                except Exception as one_e:  # noqa: BLE001
                    errors.append(f"переименование кампании {cid}: {str(one_e)[:240]}")
            else:
                for cid, name in campaign_renames.items():
                    try:
                        updated = (grid or _get_grid(login)).set_campaign_names({cid: name})
                        replaced += len(updated)
                    except Exception as one_e:  # noqa: BLE001
                        errors.append(f"переименование кампании {cid}: {str(one_e)[:240]}")

    # ── Переименование групп ──────────────────────────────────────────────────
    if adgroup_renames:
        if not adgroup_campaign_ids:
            errors.append("переименование групп: adgroup_campaign_ids не указаны в payload")
        else:
            try:
                grid = _get_grid(login)
                result = grid.set_adgroup_names(adgroup_renames, adgroup_campaign_ids)
                replaced += len(result.get("updated") or [])
                for sk in (result.get("skipped") or []):
                    log.warning("[rename] skip adgroup %s: %s",
                                sk.get("adgroup_id"), sk.get("reason"))
                errors.extend(result.get("errors") or [])
            except gf.GridFinalizeError as e:
                errors.append(f"переименование групп: {str(e)[:300]}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"переименование групп: {str(e)[:300]}")

    return {"replaced": replaced, "errors": errors}


# ────────────────────────── регистрация роутов ───────────────────────────────


def register_rename_routes(
    bp,
    access,
    v5_call: Callable,
    _login_allowed: Callable,
    _admin_allowed: Callable,
    _token: Callable,
    _enqueue_content_job: Callable,
) -> None:
    """Регистрирует ручки вкладки «Смена названий» на blueprint ``bp``."""

    def _paginate(v5_params: dict, token: str, login: str, collection: str) -> tuple[list, str | None]:
        rows: list = []
        offset = 0
        for _ in range(200):
            params = dict(v5_params or {})
            page = dict(params.get("Page") or {})
            page.setdefault("Limit", 10000)
            if offset:
                page["Offset"] = offset
            params["Page"] = page
            data = v5_call("adgroups", "get", token, login, params)
            if not isinstance(data, dict):
                return rows, "v5 adgroups.get вернул неожиданный тип"
            if data.get("error"):
                return rows, str(data["error"])[:240]
            result = data.get("result") or {}
            rows.extend(result.get(collection) or [])
            limited_by = result.get("LimitedBy")
            if not limited_by:
                break
            try:
                offset = int(limited_by)
            except (TypeError, ValueError):
                break
        return rows, None

    def _paginate_adgroups_by_campaigns(
        token: str,
        login: str,
        campaign_ids: list[int],
        *,
        batch_size: int = 10,
    ) -> tuple[list, str | None]:
        rows: list = []
        for i in range(0, len(campaign_ids), batch_size):
            chunk = campaign_ids[i:i + batch_size]
            batch_rows, err = _paginate(
                {
                    "SelectionCriteria": {"CampaignIds": chunk},
                    "FieldNames": ["Id", "Name", "CampaignId", "Status"],
                },
                token,
                login,
                "AdGroups",
            )
            rows.extend(batch_rows)
            if err:
                return rows, err
        return rows, None

    def _deny():
        """Вкладка админская: единый гейт для всех ручек."""
        if not _admin_allowed():
            return jsonify({"error": "Forbidden"}), 403
        return None

    # ── GET список кампаний с текущими именами ────────────────────────────────
    @bp.route("/api/content-editor/renames/campaigns")
    @access
    def ce_renames_campaigns():
        deny = _deny()
        if deny is not None:
            return deny
        login = (request.args.get("login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        token, agency, terr = _token(login)
        if terr:
            return jsonify({"error": terr}), 404
        try:
            # v5_call signature: v5_call(svc, method, token, login, params) → dict
            data = v5_call(
                "campaigns", "get", token, login,
                {
                    "SelectionCriteria": {},
                    "FieldNames": ["Id", "Name", "Status", "Type"],
                    "Page": {"Limit": 10000},
                },
            )
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"v5 campaigns.get: {str(e)[:200]}"}), 502
        if not isinstance(data, dict):
            return jsonify({"error": "v5 campaigns.get вернул неожиданный тип"}), 502
        if data.get("error"):
            return jsonify({"error": str(data["error"])[:200]}), 502
        result = data.get("result") or {}
        campaigns_raw = result.get("Campaigns") or []
        campaigns = [
            {
                "id": int(c["Id"]),
                "name": str(c.get("Name") or ""),
                "status": str(c.get("Status") or ""),
                "type": str(c.get("Type") or ""),
            }
            for c in campaigns_raw
            if c.get("Id") and c.get("Name")
        ]
        return jsonify({"campaigns": campaigns, "total": len(campaigns)})

    @bp.route("/api/content-editor/renames/adgroups")
    @access
    def ce_renames_adgroups():
        deny = _deny()
        if deny is not None:
            return deny
        login = (request.args.get("login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403
        raw_ids = (request.args.get("campaign_ids") or "").strip()
        if not raw_ids:
            return jsonify({"error": "campaign_ids обязателен"}), 400
        campaign_ids: list[int] = []
        for raw in raw_ids.split(","):
            try:
                cid = int((raw or "").strip())
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in campaign_ids:
                campaign_ids.append(cid)
        if not campaign_ids:
            return jsonify({"error": "campaign_ids пуст или невалиден"}), 400
        token, agency, terr = _token(login)
        if terr:
            return jsonify({"error": terr}), 404
        try:
            adgroups_raw, err = _paginate_adgroups_by_campaigns(token, login, campaign_ids)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"v5 adgroups.get: {str(e)[:200]}"}), 502
        if err:
            return jsonify({"error": err}), 502
        adgroups = [
            {
                "id": int(g["Id"]),
                "name": str(g.get("Name") or ""),
                "campaign_id": int(g.get("CampaignId") or 0),
                "status": str(g.get("Status") or ""),
            }
            for g in adgroups_raw
            if g.get("Id") and g.get("Name") and g.get("CampaignId")
        ]
        return jsonify({"adgroups": adgroups, "total": len(adgroups), "campaigns": len(campaign_ids)})

    # ── POST поставить задание переименования в очередь ───────────────────────
    @bp.route("/api/content-editor/renames/apply_async", methods=["POST"])
    @access
    def ce_renames_apply_async():
        deny = _deny()
        if deny is not None:
            return deny
        body = request.json or {}
        login = (body.get("login") or "").strip()
        if not login:
            return jsonify({"error": "login обязателен"}), 400
        ok, scope_err = _login_allowed(login)
        if not ok:
            return jsonify({"error": scope_err}), 403

        campaign_renames: dict = body.get("campaign_renames") or {}
        adgroup_renames: dict = body.get("adgroup_renames") or {}
        adgroup_campaign_ids: list = body.get("adgroup_campaign_ids") or []

        if not campaign_renames and not adgroup_renames:
            return jsonify({"error": "нет объектов для переименования"}), 400

        # Минимальная валидация: значения должны быть непустыми строками
        for k, v in {**campaign_renames, **adgroup_renames}.items():
            if not str(v or "").strip():
                return jsonify({"error": f"новое имя для объекта {k} пустое"}), 400

        _, agency, terr = _token(login)
        if terr:
            return jsonify({"error": terr}), 404

        campaign_count = len(campaign_renames) + len(adgroup_renames)
        payload = json.dumps(
            {
                "campaign_renames": campaign_renames,
                "adgroup_renames": adgroup_renames,
                "adgroup_campaign_ids": adgroup_campaign_ids,
            },
            ensure_ascii=False,
        )
        return _enqueue_content_job(
            login, agency,
            "campaign_rename", payload, payload, "rename",
            campaign_count,
            resp_type="campaign_rename",
        )
