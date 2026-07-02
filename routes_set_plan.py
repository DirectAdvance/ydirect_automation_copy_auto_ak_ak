"""Set-plan route wrapper for Direct automation."""

from __future__ import annotations

from typing import Callable


def register_set_plan_routes(bp, access, *, set_plan_response: Callable) -> None:
    @bp.route("/api/set_plan", methods=["POST"])
    @access
    def api_set_plan():
        """Предпросмотр набора кампаний."""
        return set_plan_response()
