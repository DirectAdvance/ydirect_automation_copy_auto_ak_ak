"""Page routes for Direct automation."""

from __future__ import annotations

from typing import Callable

from flask import redirect, render_template


def register_page_routes(bp, access, minusphrase_access, *, render_page: Callable) -> None:
    @bp.route("/")
    @access
    def index():
        return redirect("/direct/automation")

    @bp.route("/automation")
    @access
    def automation():
        """Канонический маршрут страницы «Автоматизация Директа»."""
        return render_page()

    @bp.route("/minusphrase")
    @minusphrase_access
    def minusphrase():
        """Инструмент подбора минус-фраз для ключевых слов."""
        return render_template(
            "direct/minusphrase.html",
            active_section="work",
            active_page="direct_minusphrase",
        )
