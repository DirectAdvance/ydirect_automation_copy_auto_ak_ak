#!/usr/bin/env python3
"""Разовый засев БД-библиотеки контента слепков (public.direct_slepok_content).

На каждый (слепок × тип сайта из site_fit) ПРОБУЕТ сгенерировать через M3, при неудаче
берёт контент из корпуса слепка (ai_agents). Заполняет kind='promo' (список вариантов) и
kind='campaign' ({titles,texts,sitelinks}). Идемпотентно (UPSERT).

Запускать на LXC101 (там доступны M3-туннель и Victory), из папки seoadvanced:
    python3 -m direct.seed_slepok_content            # только отсутствующие
    python3 -m direct.seed_slepok_content --all      # пересоздать всё
M3-таймаут на попытку (быстрый фейл-фаст) можно задать: --timeout 45
"""
import json
import sys

from direct.create import blueprint as B


def main() -> int:
    only_missing = "--all" not in sys.argv
    timeout = 45.0
    if "--timeout" in sys.argv:
        try:
            timeout = float(sys.argv[sys.argv.index("--timeout") + 1])
        except (ValueError, IndexError):
            pass
    rep = B._seed_slepok_content(only_missing=only_missing, m3_timeout=timeout)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
