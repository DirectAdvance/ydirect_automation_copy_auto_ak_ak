"""Пакет копирования кабинетов Яндекс.Директа 1:1 (сервис direct-copy)."""

from __future__ import annotations

from ..clients import grid_finalize as _gf


def _install_copy_grid_reauth_patch() -> None:
    """Copy-service needs explicit-cookie Grid clients to refresh stale cookies.

    ``GridClient`` normally treats an explicit cookie as caller-owned and only resets
    CSRF on reauth. The copy service builds target/source Grid clients from
    ``build_client(...).sess.headers['Cookie']``; after a stale-cookie 403 that path
    can only recover by asking the common cookie picker for a fresh agency cookie.
    """
    cls = _gf.GridClient
    if getattr(cls, "_copy_force_refresh_reauth", False):
        return

    def _copy_reauth(self) -> None:
        if getattr(self, "_reauth_depth", 0):
            raise RuntimeError(f"Grid reauth уже выполняется для ulogin={self.login}")
        self._reauth_depth = int(getattr(self, "_reauth_depth", 0)) + 1
        self.csrf = None
        try:
            if not getattr(self, "_explicit_cookie", False) or getattr(self, "_refresh_explicit_cookie", False):
                self.cookie = _gf.cmc.pick_working_cookie(self.login, force_refresh=True)
            self._bootstrap_csrf()
            if not self.csrf:
                raise RuntimeError(f"Grid reauth не получил CSRF для ulogin={self.login}")
        finally:
            self._reauth_depth = max(0, int(getattr(self, "_reauth_depth", 1)) - 1)

    cls._reauth = _copy_reauth
    cls._copy_force_refresh_reauth = True


_install_copy_grid_reauth_patch()
