"""Единый источник WHERE-условий для выборки Авто-аккаунтов из local_gsheet_sites.

Раньше один и тот же набор условий дублировался в трёх местах (routes_accounts.api_accounts,
routes_content_editor.ce_accounts, price_check.logins_for) и начал расходиться. Здесь — общая
база; каждый потребитель добавляет к ней свои специфичные условия (статус/поиск/директолог).

Отфильтровываем мусорные login_key: пустые, плейсхолдеры «нет»/«авито», строки из одних дефисов.
Условие `login_key ~ '[a-zA-Z]'` НЕ включаем — реальные Yandex-логины и так проходят дефис-фильтр,
а лишняя строгость раньше стояла только в logins_for и расходилась с остальными двумя местами.
"""

from __future__ import annotations


def base_account_where() -> list[str]:
    """Общие WHERE-условия (без параметров) для Авто-аккаунтов. Возвращает список строк для ' AND '.join."""
    return [
        "direction='Авто'",
        "login_key IS NOT NULL",
        "btrim(login_key)<>''",
        "lower(btrim(login_key)) NOT IN ('нет', 'авито')",
        "btrim(login_key) !~ '^-+$'",
    ]
