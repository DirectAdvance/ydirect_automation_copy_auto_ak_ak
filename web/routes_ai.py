"""AI and promo routes for Direct automation."""

from __future__ import annotations

import random
from typing import Callable

from flask import jsonify, request


def register_ai_routes(
    bp,
    access,
    *,
    ai_agents,
    campaign_module,
    promo_client_cls,
    m3_llm_url: str,
    m3_llm_timeout: float,
    m3_complete: Callable,
    promo_ctx: Callable,
    promo_extract_json: Callable,
    promo_from_slepok: Callable,
    promo_preview: Callable,
    promo_validate: Callable,
    promo_amount_steps: Callable,
    gen_campaign_content: Callable,
    seed_slepok_content: Callable,
    victory_conn: Callable,
    direct_tokens: Callable,
    token_for_login: Callable,
    pull_begin: Callable,
    pull_end: Callable,
    busy_response: Callable,
    v5_get: Callable,
) -> None:
    @bp.route("/api/ai/status")
    @access
    def api_ai_status():
        """Жив ли локальный mlx-сервер на M3 + какая модель загружена."""
        import requests as _rqs

        try:
            r = _rqs.get(f"{m3_llm_url}/v1/models", timeout=8)
            if r.status_code != 200:
                return jsonify({"online": False, "url": m3_llm_url, "error": f"HTTP {r.status_code}"}), 200
            data = r.json().get("data") or []
            models = [(m.get("id") or "").rstrip("/").split("/")[-1] for m in data if m.get("id")]
            return jsonify({"online": True, "url": m3_llm_url, "models": models})
        except Exception as e:  # noqa: BLE001
            return jsonify({"online": False, "url": m3_llm_url, "error": str(e)[:200]}), 200

    @bp.route("/api/ai/chat", methods=["POST"])
    @access
    def api_ai_chat():
        """Прокси к локальной ИИ (OpenAI /v1/chat/completions)."""
        import requests as _rqs

        d = request.json or {}
        messages = []
        raw = d.get("messages")
        if isinstance(raw, list) and raw:
            for m in raw[-40:]:
                role = m.get("role")
                content = (m.get("content") or "").strip()
                if role in ("system", "user", "assistant") and content:
                    messages.append({"role": role, "content": content[:6000]})
        if not messages:
            prompt = (d.get("prompt") or "").strip()
            if not prompt:
                return jsonify({"ok": False, "error": "prompt или messages обязательны"}), 400
            system = (d.get("system") or "").strip()
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
        payload = {
            "messages": messages,
            "max_tokens": int(d.get("max_tokens") or 512),
            "temperature": float(d.get("temperature") if d.get("temperature") is not None else 0.7),
        }
        if d.get("model"):
            payload["model"] = d["model"]
        try:
            r = _rqs.post(f"{m3_llm_url}/v1/chat/completions", json=payload, timeout=m3_llm_timeout)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"M3 недоступна: {str(e)[:200]}"}), 502
        if r.status_code != 200:
            return jsonify({"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}), 502
        j = r.json()
        try:
            text = j["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return jsonify({"ok": False, "error": "пустой ответ модели"}), 502
        return jsonify({"ok": True, "text": text, "usage": j.get("usage")})

    @bp.route("/api/ai/agents")
    @access
    def api_ai_agents():
        """Список ИИ-агентов (слепков директологов) для дропдауна."""
        return jsonify({"agents": ai_agents.agent_list()})

    @bp.route("/api/ai/promo/generate", methods=["POST"])
    @access
    def api_ai_promo_generate():
        """Сгенерировать промоакцию в стиле выбранного агента."""
        d = request.json or {}
        login = (d.get("login") or "").strip()
        agent = ai_agents.get_agent(d.get("agent"))
        if not login:
            return jsonify({"ok": False, "error": "login обязателен"}), 400
        if not agent:
            return jsonify({"ok": False, "error": "неизвестный агент"}), 400
        ctx = promo_ctx(login)
        if not ctx:
            return jsonify({"ok": False, "error": f"аккаунт {login} не найден в БД (Авто)"}), 404

        avoid = d.get("avoid") if isinstance(d.get("avoid"), list) else []
        avoid = [str(a)[:60] for a in avoid if a][-6:]
        avoid_types = [str(t).upper() for t in (d.get("avoid_types") or []) if t][-6:]
        force_type = ai_agents.next_promo_type(avoid_types, agent["promo"]["type"]) if avoid else None
        messages = ai_agents.build_promo_messages(agent, ctx, avoid=avoid, force_type=force_type)
        temp = 1.05 if avoid else 0.85
        text, err = m3_complete(messages, max_tokens=400, temperature=temp)
        raw = promo_extract_json(text) if not err else {}
        domain = (ctx.get("domain") or "").strip()
        if err or not raw:
            promo, warns = promo_from_slepok(
                agent, ctx, force_type=force_type, avoid=avoid,
                avoid_amounts=d.get("avoid_amounts"), slepok_key=(d.get("agent") or ""),
            )
            reason = err or "модель вернула не-JSON"
            warns = [f"⚠ M3 недоступна ({reason}) — промо собрано из слепка «{agent['name']}»"] + warns
            return jsonify({"ok": True, "agent": agent["name"], "login": login, "fallback": True,
                            "domain": domain, "href": ("https://" + domain) if domain else "",
                            "promo": promo, "preview": promo_preview(promo), "warnings": warns})

        promo, warns = promo_validate(raw, agent, site_type=(ctx.get("site_type") or ""))
        if force_type:
            promo["type"] = force_type
            if force_type == "GIFT":
                promo["unit"] = "RUB"
        if avoid:
            excl = {int(a) for a in (d.get("avoid_amounts") or []) if str(a).strip().isdigit()}
            steps = [x for x in promo_amount_steps(agent["promo"], promo["unit"], promo["type"]) if x not in excl]
            if steps:
                promo["amount"] = random.choice(steps)
        return jsonify({"ok": True, "agent": agent["name"], "login": login,
                        "domain": domain, "href": ("https://" + domain) if domain else "",
                        "promo": promo, "preview": promo_preview(promo), "warnings": warns})

    @bp.route("/api/ai/campaign/generate", methods=["POST"])
    @access
    def api_ai_campaign_generate():
        """Сгенерировать контент ОДНОЙ РК."""
        d = request.json or {}
        login = (d.get("login") or "").strip()
        ctx = d.get("ctx") if isinstance(d.get("ctx"), dict) else None
        agent = ai_agents.get_agent(d.get("agent"))
        item = d.get("item") if isinstance(d.get("item"), dict) else {}
        if not login and not ctx:
            return jsonify({"ok": False, "error": "нужен login или ctx"}), 400
        if not agent:
            return jsonify({"ok": False, "error": "неизвестный агент"}), 400
        avoid0 = d.get("avoid") if isinstance(d.get("avoid"), list) else []
        res = gen_campaign_content(login, agent, (d.get("agent") or "").strip().lower(),
                                   item, avoid=avoid0, ctx_override=ctx)
        if not res.get("ok"):
            return jsonify(res), (404 if "не найден" in (res.get("error") or "") else 400)
        return jsonify(res)

    @bp.route("/api/ai/slepok_content/seed", methods=["POST"])
    @access
    def api_ai_slepok_content_seed():
        """Засев/обновление БД-библиотеки контента слепков."""
        d = request.json or {}
        rep = seed_slepok_content(
            only_missing=bool(d.get("only_missing", True)),
            m3_timeout=float(d.get("m3_timeout") or 45),
        )
        return jsonify({"ok": True, "report": rep})

    @bp.route("/api/ai/slepok_content")
    @access
    def api_ai_slepok_content():
        """Просмотр БД-библиотеки слепков."""
        import psycopg2.extras

        try:
            conn = victory_conn()
        except Exception as e:  # noqa: BLE001
            return jsonify({"rows": [], "error": str(e)[:200]})
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT slepok, site_type, kind, source, "
                "       jsonb_array_length(CASE WHEN jsonb_typeof(content)='array' THEN content ELSE '[]'::jsonb END) AS n, "
                "       updated_at FROM public.direct_slepok_content ORDER BY slepok, site_type, kind"
            )
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["updated_at"] = r["updated_at"].isoformat() if r.get("updated_at") else None
            return jsonify({"rows": rows, "total": len(rows)})
        finally:
            conn.close()

    @bp.route("/api/ai/promo/publish", methods=["POST"])
    @access
    def api_ai_promo_publish():
        """Опубликовать промо в кабинет клиента через grid/api."""
        body = request.json or {}
        login = (body.get("login") or "").strip()
        raw = body.get("promo") or {}
        if not login or not raw:
            return jsonify({"ok": False, "error": "login и promo обязательны"}), 400

        ctx = promo_ctx(login)
        if not ctx:
            return jsonify({"ok": False, "error": f"аккаунт {login} не найден в БД"}), 404
        domain = (ctx.get("domain") or "").strip()
        href = (body.get("href") or (("https://" + domain) if domain else "")).strip()
        if not href:
            return jsonify({"ok": False, "error": "нет домена аккаунта для ссылки промо"}), 400

        agent = ai_agents.get_agent(body.get("agent")) or {
            "promo": {"type": "DISCOUNT", "unit": "PCT", "prefix": "TO",
                      "amount_min": 30, "amount_max": 50, "examples": ["Спецпредложение"]}
        }
        promo, _ = promo_validate(raw, agent)

        ok, reason, wait = pull_begin(f"promo:{login}", 8.0)
        if not ok:
            return busy_response(reason, wait)
        try:
            pr_token, working_agency = token_for_login(
                login, body.get("agency") or ctx.get("agency_account") or "", direct_tokens()
            )
            try:
                client = campaign_module.build_client(login, account=(working_agency or None))
                client.link_info(href)
            except Exception as e:  # noqa: BLE001
                return jsonify({"ok": False, "error": f"не удалось подобрать рабочую куку: {str(e)[:160]}"}), 502

            pid, perr = promo_client_cls(client, login).add(
                type=promo["type"], description=promo["description"], href=href,
                amount=promo["amount"], unit=promo["unit"], prefix=promo["prefix"],
                promocode=promo["promocode"], finish=promo["finishDate"],
            )
            if not pid:
                return jsonify({"ok": False, "error": f"grid отклонил промо: {perr}"}), 502

            verified = None
            if pr_token:
                jp = v5_get("promotions", pr_token, login,
                            ["Id", "Type", "Name", "Description", "Amount", "AmountUnit"], criteria={})
                for it in (jp.get("result") or {}).get("Promotions", []):
                    if str(it.get("Id")) == str(pid):
                        verified = {"id": it.get("Id"), "name": it.get("Name"),
                                    "type": it.get("Type"), "amount": it.get("Amount")}
                        break
            return jsonify({"ok": True, "id": pid, "preview": promo_preview(promo), "verified": verified})
        finally:
            pull_end(f"promo:{login}")
