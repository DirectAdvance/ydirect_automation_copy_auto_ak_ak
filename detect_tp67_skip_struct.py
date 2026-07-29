"""Фикстурный детект прохода сверки по УЖЕ СУЩЕСТВУЮЩИМ tp6/tp7 (RESUME-SKIP) + предохранителя.

Держит два инварианта класса `UAC_STRUCT_*` (Д7, 2026-07-19):
  1. id пропущенной (`already_in_direct`) кампании РЕЗОЛВИТСЯ по имени из Grid-снимка, иначе
     деталей по ней не запрашивается и сверка инертна ПО ПОСТРОЕНИЮ (`_created_ids` отбрасывает
     `skipped`, см. `campaign_result.py:49`);
  2. этот проход НИКОГДА не порождает удаление: наружу выходят только `UAC_STRUCT_*`, repair-
     кандидатов нет, `UAC_NOT_DRAFT` права на delete не даёт. SKIP значит «не трогать существующее»;
  3. фан-аут по фидам: позиция, развёрнутая в НЕСКОЛЬКО РК, проверяется по ВСЕМ sibling'ам,
     а не по первому совпадению имени (иначе потери остальных РК не видны молча).

Фикстурный, без сети: живой аккаунт не нужен, гоняется в любой момент как регресс-тест.

Запуск (прод-венв 3.11 на LXC101; локальный 3.9 не тянет синтаксис проекта):

    ssh proxmox-ts "pct exec 101 -- bash -lc 'cd /opt/scripts/home/seoadvanced && \\
        DIRECT_ROLE=web /root/venv/bin/python -m direct.detect_tp67_skip_struct'"

Эталон прогона 2026-07-19: ВСЕ 5 КРИТЕРИЕВ ЗЕЛЁНЫЕ (exit 0).
"""
import sys

# ── фикстура: одна СОЗДАННАЯ tp6 + одна ПРОПУЩЕННАЯ (already_in_direct) tp7 ──────────────
NAME_SKIP = "tp7_cpc_site_aon_n000_r0000_ct0010_ag001_g00 — Краснодар — КС"
NAME_MADE = "tp6_cpc_site_aon_n000_r0000_ct0001_ag011_g00 — Краснодар — автотаргетинг"

RESULTS = [
    {"ok": True, "name": NAME_MADE, "id": 111,
     "struct": {"keywords": 10, "audiences": 0}},
    # RESUME-SKIP: кампания УЖЕ есть в кабинете, мы её НЕ создавали
    {"ok": True, "skipped": True, "skip_reason": "already_in_direct", "name": NAME_SKIP,
     "struct": {"keywords": 416, "audiences": 9}},
]
# В Grid она лежит под именем с фид-суффиксом → проверяем и резолв по префиксу
GRID = [{"id": 111, "name": NAME_MADE, "status": "DRAFT"},
        {"id": 999, "name": NAME_SKIP + " — site.ru — yandex", "status": "ACCEPTED"}]

# ФАН-АУТ: та же позиция развёрнута по ДВУМ фидам → в кабинете ДВЕ РК (критерий 5)
GRID_FANOUT = [{"id": 111, "name": NAME_MADE, "status": "DRAFT"},
               {"id": 999, "name": NAME_SKIP + " — site.ru — feedA", "status": "ACCEPTED"},
               {"id": 998, "name": NAME_SKIP + " — site.ru — feedB", "status": "ACCEPTED"}]

# Живая, ЗАПУЩЕННАЯ кампания с обнулёнными ключами/аудиториями и пустым контентом:
DETAIL_SKIP_RUNNING = {
    "status": "accepted",         # ← НЕ draft
    "keywords": 0, "audiences": 0,  # ← обнулены: UAC_STRUCT_* обязан сработать
    "titles": 0, "texts": 0, "sitelinks": 0, "content": 0,
    "counters": 0, "goals": 0, "regions": 0, "has_feed": False,
}
DETAIL_MADE = {"status": "draft", "keywords": 10, "audiences": 0, "titles": 5, "texts": 3,
               "sitelinks": 8, "content": 3, "counters": 1, "goals": 1, "regions": 1,
               "has_tracking_params": True, "has_feed": True, "pricing": "PER_CLICK"}


def main():
    from direct import live_verifier as lv
    from direct.repair import repair_gate as rgate
    from direct import verification_service as vs

    ok = True

    # ── Критерий 1: id для SKIP-строки резолвится ⇒ детали ЗАПРАШИВАЮТСЯ ──────────────
    ids = vs._skipped_uac_ids(RESULTS, GRID)
    print(f"[1] _skipped_uac_ids -> {ids}   (ожидалось [999], резолв по фид-префиксу)")
    ok &= (ids == [999])

    # Сверка "до фикса": через created_campaigns skip-строка отсутствует
    created_uac = vs._created_ids(RESULTS, kind="uac")
    print(f"    _created_ids(kind=uac) -> {created_uac}  (999 отсутствует — это и была инертность)")
    ok &= (999 not in created_uac)

    # ── Критерий 3: UAC_STRUCT_* реально поднимается (не пустышка) ────────────────────
    rep = lv.verify_live_create_set(
        login="test", results=RESULTS, grid_campaigns=GRID,
        uac_details={111: DETAIL_MADE, 999: DETAIL_SKIP_RUNNING},
        grid_content_counts={}, phase="in_job")
    skip_issues = [i for i in rep["issues"] if i.get("id") == 999]
    codes = sorted({i["code"] for i in skip_issues})
    print(f"[3] коды от SKIP-кампании 999: {codes}")
    ok &= ("UAC_STRUCT_KEYWORDS_MISSING" in codes and "UAC_STRUCT_AUDIENCES_MISSING" in codes)
    for i in skip_issues:
        print(f"    {i['code']}: expected={i.get('expected')} actual={i.get('actual')} "
              f"source={i.get('source')}")

    # ── Критерий 2: UAC_NOT_DRAFT из SKIP-прохода НЕ доходит до delete_uac ────────────
    print(f"[2] UAC_NOT_DRAFT в issues по 999? {'UAC_NOT_DRAFT' in codes}  (ожидалось False)")
    ok &= ("UAC_NOT_DRAFT" not in codes)
    non_struct = [c for c in codes if not c.startswith("UAC_STRUCT_")]
    print(f"    непрофильные UAC_*-коды по 999: {non_struct}  (ожидался пустой список)")
    ok &= (non_struct == [])
    repl = rgate.executable_uac_replace_campaigns(rep["repair_plan"])
    print(f"    executable_uac_replace_campaigns -> {repl}  (ожидался []: удалять нечего)")
    ok &= (repl == [])
    cand999 = [c for c in rep["repair_candidates"] if c.get("id") == 999]
    print(f"    repair_candidates по 999: {cand999}  (ожидался []: проход report-only)")
    ok &= (cand999 == [])

    # ── Критерий 4: draft-гард — ЗАПУЩЕННАЯ кампания не попадает в исполняемые удаления ─
    plan_not_draft = {"actions": [{
        "action": "resume_or_recreate_campaign", "campaign_id": 999,
        "name": NAME_SKIP, "issue_code": "UAC_NOT_DRAFT"}]}
    got = rgate.executable_uac_replace_campaigns(plan_not_draft)
    print(f"[4] план с issue_code=UAC_NOT_DRAFT -> {got}  (ожидался []: код изъят из _UAC_REPLACE_CODES)")
    ok &= (got == [])
    plan_tagged = {"actions": [{
        "action": "resume_or_recreate_campaign", "campaign_id": 999, "name": NAME_SKIP,
        "issue_code": "UAC_TITLES_MISSING", "source": "resume_skip"}]}
    got2 = rgate.executable_uac_replace_campaigns(plan_tagged)
    print(f"    план с source=resume_skip -> {got2}  (ожидался []: второй рубеж)")
    ok &= (got2 == [])
    plan_normal = {"actions": [{
        "action": "resume_or_recreate_campaign", "campaign_id": 111, "name": NAME_MADE,
        "issue_code": "UAC_TITLES_MISSING"}]}
    got3 = rgate.executable_uac_replace_campaigns(plan_normal)
    print(f"    КОНТРОЛЬ (наш свежий черновик, без тега) -> {got3}  (ожидалась 1 запись — не сломали штатный путь)")
    ok &= (len(got3) == 1 and got3[0]["campaign_id"] == 111)

    # ── Критерий 5: ФАН-АУТ по фидам — проверяются ВСЕ sibling'и позиции, не первый ────
    # Позиция разворачивается в несколько РК («— feedA», «— feedB»). Если резолв вернёт одно
    # совпадение (прежний `_grid_by_prefix`), потерянные ключи остальных РК не видны молча.
    ids_fan = vs._skipped_uac_ids(RESULTS, GRID_FANOUT)
    print(f"[5] _skipped_uac_ids (фан-аут) -> {ids_fan}  (ожидались ОБА: 998 и 999)")
    ok &= (sorted(ids_fan) == [998, 999])

    rep_fan = lv.verify_live_create_set(
        login="test", results=RESULTS, grid_campaigns=GRID_FANOUT,
        uac_details={111: DETAIL_MADE, 999: DETAIL_SKIP_RUNNING, 998: DETAIL_SKIP_RUNNING},
        grid_content_counts={}, phase="in_job")
    struct_by_id: dict[int, set] = {}
    for i in rep_fan["issues"]:
        if str(i.get("code", "")).startswith("UAC_STRUCT_"):
            struct_by_id.setdefault(i.get("id"), set()).add(i["code"])
    print(f"    UAC_STRUCT_* по sibling'ам: "
          f"{ {k: sorted(v) for k, v in sorted(struct_by_id.items(), key=lambda x: x[0] or 0)} }")
    for _sib in (998, 999):
        got_codes = struct_by_id.get(_sib, set())
        hit = ("UAC_STRUCT_KEYWORDS_MISSING" in got_codes
               and "UAC_STRUCT_AUDIENCES_MISSING" in got_codes)
        print(f"    sibling {_sib}: KEYWORDS+AUDIENCES подняты? {hit}  (ожидалось True)")
        ok &= hit

    print("\nИТОГ:", "ВСЕ 5 КРИТЕРИЕВ ЗЕЛЁНЫЕ" if ok else "ЕСТЬ ПРОВАЛ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
