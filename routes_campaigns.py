"""Campaign action routes for Direct automation."""

from __future__ import annotations

from typing import Callable


def register_campaign_routes(
    bp,
    access,
    danger_access,
    *,
    campaigns_response: Callable,
    stop_all_response: Callable,
    delete_drafts_response: Callable,
    delete_drafts_async_response: Callable,
    check_blocks_response: Callable,
) -> None:
    @bp.route("/api/campaigns")
    @access
    def api_campaigns():
        """Кампании аккаунта."""
        return campaigns_response()

    @bp.route("/api/campaigns/stop_all", methods=["POST"])
    @danger_access
    def api_stop_all():
        """Остановить все активные кампании аккаунта."""
        return stop_all_response()

    @bp.route("/api/campaigns/delete_drafts", methods=["POST"])
    @danger_access
    def api_delete_drafts():
        """Синхронное удаление черновиков."""
        return delete_drafts_response()

    @bp.route("/api/campaigns/delete_drafts_async", methods=["POST"])
    @danger_access
    def api_delete_drafts_async():
        """Удаление черновиков фоновой задачей."""
        return delete_drafts_async_response()

    @bp.route("/api/check_blocks", methods=["POST"])
    @access
    def api_check_blocks():
        """Блокировки аккаунтов."""
        return check_blocks_response()
