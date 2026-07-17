# Slepki audit (2026-07-12)

Scope: all 13 `slepki_structure.json` directologists, targeting profile, referenced source
manifests and live M3 segment-count endpoint on LXC 101. No campaigns or drafts were created.

## Hard checks

- 13 directologists parsed;
- cross-slepok identical-section collisions: 0;
- empty tp/groups/splits: 0;
- invalid `gc`: 0;
- empty tp enabled by targeting profile: 0;
- source-manifest errors: 0.

## Exact item repeats (review, not auto-fixed)

The same `(c, t, gc)` occurs twice in these sections:

- `terehov / Мультибренд`: 6 extra rows;
- `gordeeva / Мультибренд`: 6;
- `gordeeva / Квиз`: 6;
- `zubakin / Мультибренд`: 6;
- `kuderko / Мультибренд`: 3;
- `dmp / dmp`: 14.

The automotive repeats are the same three models (`Changan CS95`, `Geely Monjaro`,
`Haval Jolion`) in tp1 and/or tp2 category groups. They may be an extraction overlap and
need source-account evidence before deletion. The 14 dmp rows are repeated across its two
split/payment blocks and are intentional in the current split representation.

## Structure versus live M3 pack

Many sections differ materially. This is a warning, not proof that structure is wrong:
the structure is campaign selection, whereas the M3 pack can be a content superset, and
`ct0000` collapses multiple human labels into one pack key. The largest gaps worth a focused
source audit are:

- `kuderko / Мультибренд`: tp1 `111 → 1`, tp5 `111 → 1`;
- `karavaev / Мультибренд`: tp5 `72 → 9`;
- `chepelev`: tp5 `184 → 1` for both multi/mono;
- `salamahin`: tp2/tp5 are much smaller in mono/used/quiz packs;
- `gordeeva`, `zubakin`, `terehov`: pack counts are often larger than structure and exact
  repeats also exist;
- several site/tp combinations report a pack unavailable rather than zero coverage.

Do not bulk-copy M3 counts back into structure. Review one slepok against its real campaigns,
then decide whether the pack is stale, a valid superset, or the structure is incomplete.
The UI now keeps the structure count stable and shows M3 as a separate warning count.

## gen_ses

- Existing coder foundation retained: tp1/tp2, 59 labels each, 49 unique pack-addressable
  ct values after `ct0000` collapse.
- Live M3 also reports 49 addressable groups per tp (32 Marks, 1 Model, 16 General), but this
  generic split is not the source campaign structure.
- The authoritative archive layer contains exactly 6 campaigns with group counts
  `55, 55, 55, 55, 4, 55`.
- Both archive sources contain byte-identical XLSX payloads; `Архив 4` additionally contains
  the screenshots used for campaign-level settings.
- `Москвич` is explicitly mapped to existing `Moskvich / ct0252`.
- Draft blockers remain: group-level interest IDs and retargeting-condition IDs are absent,
  and the source-aware builder is intentionally not enabled in this pre-draft pass.

## Architecture model

The four-layer model underlying the gaps found above (structure → profile → content pack →
service executor) and the full DoD checklist are documented as of 2026-07-13:

- Conceptual model and tp-type table: `ARCHITECTURE.md` section
  "Слепок — четырёхслойная модель и цепочка использования".
- Ten-point readiness checklist: `DOD.md` §5.c.

