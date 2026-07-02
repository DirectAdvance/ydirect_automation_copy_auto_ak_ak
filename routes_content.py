"""Content-pack routes for Direct automation."""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Callable

from flask import jsonify, request, send_file


def register_content_routes(
    bp,
    access,
    *,
    kp,
    m3_content_status: Callable,
    content_rules_map: Callable[[], dict],
    content_rule_key: Callable[[str, str, str, str, str], str],
    dedupe_content_assets_for_ui: Callable,
    content_rules_ensure: Callable,
    gc_ct: Callable[[str], str],
    victory_conn_rw: Callable,
    content_rules_cache: dict,
) -> None:
    @bp.route("/api/content-tree")
    @access
    def api_content_tree():
        try:
            force = (request.args.get("refresh") or "").strip() in {"1", "true", "yes"}
            return jsonify({"ok": True, "tree": kp.content_tree(force_refresh=force), "status": m3_content_status()})
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)[:300]}), 500

    @bp.route("/api/content-assets")
    @access
    def api_content_assets():
        segment = (request.args.get("segment") or "").strip()
        tp = (request.args.get("tp") or "").strip()
        ct = (request.args.get("ct") or "").strip()
        slepok = (request.args.get("slepok") or "").strip().lower()
        try:
            limit = int(request.args.get("limit") or 24)
        except (TypeError, ValueError):
            limit = 24
        try:
            offset = int(request.args.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        limit = max(1, min(limit, 120))
        offset = max(0, offset)
        if not segment or not tp or not ct:
            return jsonify({"ok": False, "error": "segment, tp, ct обязательны"}), 400

        assets = kp.content_assets(segment, tp, ct, slepok=slepok)
        try:
            rules = content_rules_map()
        except Exception:  # noqa: BLE001
            rules = {}
        for a in assets:
            original_key = a.get("asset_key") or ""
            source_slepok = (a.get("slepok") or "").strip().lower()
            scoped_key = content_rule_key(segment, tp, ct, original_key, source_slepok)
            a["original_asset_key"] = original_key
            a["asset_key"] = scoped_key
            a["source_slepok"] = source_slepok
            r = rules.get(scoped_key)
            if r:
                a["enabled"] = bool(r.get("enabled"))
                a["allowed_for"] = r.get("allowed_for") or []
                a["allowed_slepki"] = r.get("allowed_slepki") or []
            else:
                a["enabled"] = True
                a["allowed_for"] = []
                a["allowed_slepki"] = []

        raw_total = len(assets)
        scan_limit = min(raw_total, offset + max(limit * 3, limit))
        window = assets[offset:scan_limit]
        page_assets, hidden_duplicates = dedupe_content_assets_for_ui(window)
        page_assets = page_assets[:limit]
        next_offset = scan_limit
        thumb_paths = [
            a.get("remote") for a in page_assets
            if "image" in str(a.get("asset_type") or "") and a.get("remote")
        ]
        if thumb_paths:
            if len(thumb_paths) <= 8:
                kp.prefetch_remote_thumbnails(thumb_paths, size=360, max_workers=4)
            else:
                threading.Thread(
                    target=kp.prefetch_remote_thumbnails,
                    args=(thumb_paths,),
                    kwargs={"size": 360, "max_workers": 4},
                    daemon=True,
                ).start()

        return jsonify({
            "ok": True,
            "segment": segment,
            "tp": tp,
            "ct": ct,
            "slepok": slepok,
            "assets": page_assets,
            "total_assets": raw_total,
            "raw_total_assets": raw_total,
            "hidden_duplicates": hidden_duplicates,
            "limit": limit,
            "offset": offset,
            "next_offset": next_offset,
            "has_more": next_offset < raw_total,
        })

    @bp.route("/api/content-preview/<token>")
    @access
    def api_content_preview(token):
        remote = kp.decode_remote_asset(token)
        if not remote:
            return ("bad token", 400)
        roots = [
            getattr(kp, "M3_AGENCY_ROOT", kp.M3_PACK_ROOT),
            getattr(kp, "M3_MANUAL_ROOT", ""),
        ]
        if not any(r and remote.startswith(r + "/") for r in roots):
            return ("forbidden", 403)
        local = kp.fetch_remote_asset(remote)
        if not local or not os.path.isfile(local):
            return ("not found", 404)
        return send_file(local, conditional=True, max_age=86400)

    @bp.route("/api/content-thumb/<token>")
    @access
    def api_content_thumb(token):
        remote = kp.decode_remote_asset(token)
        if not remote:
            return ("bad token", 400)
        roots = [
            getattr(kp, "M3_AGENCY_ROOT", kp.M3_PACK_ROOT),
            getattr(kp, "M3_MANUAL_ROOT", ""),
        ]
        if not any(r and remote.startswith(r + "/") for r in roots):
            return ("forbidden", 403)
        local = kp.fetch_remote_thumbnail(remote, size=360)
        if not local or not os.path.isfile(local):
            local = kp.fetch_remote_asset(remote)
        if not local or not os.path.isfile(local):
            return ("not found", 404)
        return send_file(local, conditional=True, max_age=86400)

    @bp.route("/api/content-rules", methods=["POST"])
    @access
    def api_content_rules_post():
        body = request.json or {}
        assets = body.get("assets") or []
        if not isinstance(assets, list):
            return jsonify({"ok": False, "error": "assets должен быть массивом"}), 400
        conn = victory_conn_rw()
        saved = 0
        try:
            cur = conn.cursor()
            content_rules_ensure(cur)
            for a in assets:
                src_segment = (a.get("segment") or a.get("source_segment") or "").strip()
                src_tp = (a.get("tp") or a.get("source_tp") or "").strip()
                src_ct = (gc_ct(a.get("ct") or a.get("source_ct") or "") or "ct0000")
                src_slepok = (a.get("slepok") or a.get("source_slepok") or "").strip().lower()
                original_key = (a.get("original_asset_key") or "").strip()
                key = (a.get("asset_key") or "").strip()
                if original_key:
                    key = content_rule_key(src_segment, src_tp, src_ct, original_key, src_slepok)
                remote = (a.get("remote") or a.get("asset_path") or "").strip()
                if not key or not remote:
                    continue
                allowed = a.get("allowed_for") or []
                if isinstance(allowed, str):
                    allowed = [x.strip().lower() for x in re.split(r"[,;\s]+", allowed) if x.strip()]
                else:
                    allowed = [str(x).strip().lower() for x in allowed if str(x).strip()]
                allowed_slepki = a.get("allowed_slepki") or []
                if isinstance(allowed_slepki, str):
                    allowed_slepki = [x.strip().lower() for x in re.split(r"[,;\s]+", allowed_slepki) if x.strip()]
                else:
                    allowed_slepki = [str(x).strip().lower() for x in allowed_slepki if str(x).strip()]
                cur.execute(
                    "INSERT INTO public.direct_content_asset_rules("
                    "asset_key, asset_type, source_segment, source_tp, source_ct, asset_path, "
                    "name, enabled, allowed_for, source_slepok, allowed_slepki, updated_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,now()) "
                    "ON CONFLICT(asset_key) DO UPDATE SET asset_type=EXCLUDED.asset_type, "
                    "source_segment=EXCLUDED.source_segment, source_tp=EXCLUDED.source_tp, "
                    "source_ct=EXCLUDED.source_ct, asset_path=EXCLUDED.asset_path, name=EXCLUDED.name, "
                    "enabled=EXCLUDED.enabled, allowed_for=EXCLUDED.allowed_for, "
                    "source_slepok=EXCLUDED.source_slepok, allowed_slepki=EXCLUDED.allowed_slepki, updated_at=now()",
                    (
                        key,
                        (a.get("asset_type") or "image").strip(),
                        src_segment,
                        src_tp,
                        src_ct,
                        remote,
                        (a.get("name") or os.path.basename(remote)).strip(),
                        bool(a.get("enabled")),
                        json.dumps(allowed, ensure_ascii=False),
                        src_slepok,
                        json.dumps(allowed_slepki, ensure_ascii=False),
                    ),
                )
                saved += 1
            conn.commit()
            content_rules_cache.update({"ts": 0.0, "rows": {}})
        except Exception:  # noqa: BLE001
            conn.rollback()
            raise
        finally:
            conn.close()
        return jsonify({"ok": True, "saved": saved})
