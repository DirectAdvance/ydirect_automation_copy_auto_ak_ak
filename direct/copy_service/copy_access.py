"""Access helpers for Direct copy account scope."""
from __future__ import annotations

from typing import Callable


def login_allowed_for_directologists(
    victory_conn: Callable,
    login: str,
    allowed_directologists: list[str] | None,
) -> tuple[bool, str]:
    """Check whether a login belongs to one of the allowed directologists.

    ``allowed_directologists`` follows the content-editor convention:
    None means full access, an empty list means no scoped access.
    """
    login = (login or "").strip()
    if not login:
        return False, "login обязателен"
    if allowed_directologists is None:
        return True, ""
    if not allowed_directologists:
        return False, "нет доступных директологов"

    try:
        import psycopg2.extras

        conn = victory_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT directologist FROM public.local_gsheet_sites "
                "WHERE direction='Авто' AND login_key=%s "
                "ORDER BY status='Активен' DESC NULLS LAST LIMIT 1",
                (login,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return False, f"не удалось проверить доступ к аккаунту {login}: {str(exc)[:160]}"

    directologist = str((row or {}).get("directologist") or "").strip()
    if not directologist:
        return False, f"аккаунт {login} не найден в local_gsheet_sites"
    if directologist in allowed_directologists:
        return True, ""
    return False, f"аккаунт {login} вне вашего доступа"
