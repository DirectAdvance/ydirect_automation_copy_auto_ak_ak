"""
Сенсоры автоправил — сбор данных из Direct API v5 и БД (фаза 2).

Каждый сенсор = отдельный модуль с единым интерфейсом:
    run(login: str, ctx: dict) -> {"found": int, "details": list, "error": str|None}

ctx содержит:
    token       — OAuth-токен агентства для этого логина
    agency      — логин агентства
    victory_conn    — callable → psycopg2 connection к Victory DB (для отчётных данных)
    metrika_goals_for — callable(login) → {counters: [...], goal_id: int|None}|None

Все сенсоры — ТОЛЬКО ЧТЕНИЕ (никаких write/update в кабинеты Директа).
"""
