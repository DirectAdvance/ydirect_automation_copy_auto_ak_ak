"""Account/template/region preparation for Direct create_set."""
from __future__ import annotations

from typing import Any, Callable, Optional


AccountGetter = Callable[[str], Optional[dict[str, Any]]]
TemplatesGetter = Callable[[str], tuple[list[str], list[str], list[dict[str, Any]]]]
RegionParser = Callable[[Any], list[int]]


def prepare_create_set_account(*, login: str, body: dict[str, Any],
                               account_ctx: AccountGetter,
                               templates_for: TemplatesGetter,
                               parse_region_ids: RegionParser) -> dict[str, Any]:
    """Load account context and derive site/templates/href/region ids."""
    ctx = account_ctx(login)
    if not ctx:
        # Мягкая деградация: аккаунт не найден в local_gsheet_sites (новый, ещё не занесён).
        # Собираем минимальный ctx из тела запроса (из формы).
        domain = (body.get("domain") or "").strip()
        if not domain:
            return {"ok": False, "status": 400,
                    "error": f"аккаунт {login} не найден в БД — укажите домен вручную в форме"}
        ctx = {
            "domain": domain,
            "site_type": (body.get("site_type") or "").strip(),
            "agency": (body.get("agency") or "").strip(),
            "geoid": 225,
            "oblast": None,
            "city": (body.get("city") or "").strip(),
            "directologist": "",
        }
    if not ctx.get("domain"):
        return {"ok": False, "status": 400, "error": "у аккаунта нет домена в БД"}

    site_type = (body.get("site_type") or "").strip() or ctx.get("site_type")
    tpl_titles, tpl_texts, tpl_sitelinks = templates_for(site_type)
    body_region_ids = parse_region_ids(body.get("region_ids"))
    # #ФИКС-5: мультигород → union geoid (ctx["geoids"]); одногород — тот же список из одного id.
    region_ids = body_region_ids if body_region_ids else (list(ctx.get("geoids") or [])
                                                          or [ctx.get("geoid")])
    return {
        "ok": True,
        "ctx": ctx,
        "site_type": site_type,
        "tpl_titles": tpl_titles,
        "tpl_texts": tpl_texts,
        "tpl_sitelinks": tpl_sitelinks,
        "href": "https://" + ctx["domain"],
        "region_ids": region_ids,
    }


def validate_create_set_content(*, items: list[dict[str, Any]],
                                tpl_titles: list[str],
                                tpl_texts: list[str],
                                site_type: str) -> dict[str, Any]:
    """Ensure there is either item content or fallback templates."""
    any_item_content = any((item.get("titles") or item.get("texts")) for item in items or [])
    if (not tpl_titles or not tpl_texts) and not any_item_content:
        return {"ok": False, "status": 400,
                "error": f"нет шаблонных текстов для типа сайта «{site_type}» — создание невозможно"}
    return {"ok": True}
