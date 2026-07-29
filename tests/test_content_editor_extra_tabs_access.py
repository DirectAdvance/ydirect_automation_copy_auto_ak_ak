from flask import Blueprint, Flask, jsonify

from direct.content.content_dashboard_routes import register_content_dashboards
from direct.content.content_images_routes import register_image_routes
from direct.content.content_renames_routes import register_rename_routes


def _access(fn):
    return fn


class _FakeCursor:
    def __init__(self, calls, rows):
        self.calls = calls
        self.rows = rows

    def execute(self, sql, params):
        self.calls.append((sql, list(params)))

    def fetchall(self):
        return self.rows


class _FakeConn:
    def __init__(self, calls, rows):
        self.calls = calls
        self.rows = rows

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self.calls, self.rows)

    def close(self):
        return None


def test_copy_accounts_is_not_scoped_by_directologist():
    app = Flask(__name__)
    bp = Blueprint("direct", __name__, url_prefix="/direct")
    calls = []
    rows = [{"login_key": "porg-a"}]
    register_content_dashboards(
        bp,
        _access,
        victory_conn=lambda: _FakeConn(calls, rows),
        _allowed_directologists=lambda: ["dir-1"],
        _admin_allowed=lambda: False,
        _content_tools_allowed=lambda: True,
        default_status="Контекст активно",
        exclude_directologs=[],
    )
    app.register_blueprint(bp)
    client = app.test_client()

    scoped = client.get("/direct/api/content-editor/accounts?status=__all__")
    full = client.get("/direct/api/content-editor/copy_accounts?status=__all__")

    assert scoped.status_code == 200
    assert full.status_code == 200
    assert scoped.get_json()["rows"] == rows
    assert full.get_json()["rows"] == rows
    assert "directologist = ANY(%s)" in calls[0][0]
    assert calls[0][1] == [["dir-1"]]
    assert "directologist = ANY(%s)" not in calls[1][0]
    assert calls[1][1] == []


def test_copy_accounts_requires_extra_tab_access():
    app = Flask(__name__)
    bp = Blueprint("direct", __name__, url_prefix="/direct")
    register_content_dashboards(
        bp,
        _access,
        victory_conn=lambda: _FakeConn([], []),
        _allowed_directologists=lambda: ["dir-1"],
        _admin_allowed=lambda: False,
        _content_tools_allowed=lambda: False,
        default_status="Контекст активно",
        exclude_directologs=[],
    )
    app.register_blueprint(bp)

    response = app.test_client().get("/direct/api/content-editor/copy_accounts?status=__all__")

    assert response.status_code == 403
    assert response.get_json()["error"] == "Forbidden"


def test_rename_routes_require_admin_access():
    """Вкладка «Смена названий» — admin-only (поведение из HEAD)."""
    app = Flask(__name__)
    bp = Blueprint("direct", __name__, url_prefix="/direct")
    register_rename_routes(
        bp,
        _access,
        v5_call=lambda *args, **kwargs: {
            "result": {"Campaigns": [{"Id": 11, "Name": "Camp", "Status": "ON", "Type": "TEXT_CAMPAIGN"}]}
        },
        _login_allowed=lambda login: (login == "ok-login", "scope denied"),
        _admin_allowed=lambda: True,
        _token=lambda login: ("token", "agency", None),
        _enqueue_content_job=lambda *args, **kwargs: jsonify({"ok": True}),
    )
    app.register_blueprint(bp)
    client = app.test_client()

    allowed = client.get("/direct/api/content-editor/renames/campaigns?login=ok-login")
    denied_scope = client.get("/direct/api/content-editor/renames/campaigns?login=other-login")

    assert allowed.status_code == 200
    assert allowed.get_json()["campaigns"][0]["id"] == 11
    assert denied_scope.status_code == 403
    assert denied_scope.get_json()["error"] == "scope denied"


def test_rename_routes_deny_non_admin():
    """Вкладка «Смена названий» недоступна не-адмиину."""
    app = Flask(__name__)
    bp = Blueprint("direct", __name__, url_prefix="/direct")
    register_rename_routes(
        bp,
        _access,
        v5_call=lambda *args, **kwargs: {},
        _login_allowed=lambda login: (True, ""),
        _admin_allowed=lambda: False,
        _token=lambda login: ("token", "agency", None),
        _enqueue_content_job=lambda *args, **kwargs: jsonify({"ok": True}),
    )
    app.register_blueprint(bp)

    response = app.test_client().get("/direct/api/content-editor/renames/campaigns?login=any")
    assert response.status_code == 403
    assert response.get_json()["error"] == "Forbidden"


def test_image_routes_require_admin_access():
    """Вкладка «Смена изображений» — admin-only (поведение из HEAD)."""
    app = Flask(__name__)
    bp = Blueprint("direct", __name__, url_prefix="/direct")
    register_image_routes(
        bp,
        _access,
        v5_call=lambda *args, **kwargs: {},
        _login_allowed=lambda login: (login == "ok-login", "scope denied"),
        _admin_allowed=lambda: True,
        _token=lambda login: ("token", "agency", None),
        _enqueue_content_job=lambda *args, **kwargs: jsonify({"ok": True}),
    )
    app.register_blueprint(bp)
    client = app.test_client()

    allowed = client.post("/direct/api/content-editor/images/upload", data={"login": "ok-login"})
    denied_scope = client.post("/direct/api/content-editor/images/upload", data={"login": "other-login"})

    assert allowed.status_code == 400
    assert allowed.get_json()["error"] == "не передан ни один файл"
    assert denied_scope.status_code == 403
    assert denied_scope.get_json()["error"] == "scope denied"


def test_image_routes_deny_non_admin():
    """Вкладка «Смена изображений» недоступна не-адмиину."""
    app = Flask(__name__)
    bp = Blueprint("direct", __name__, url_prefix="/direct")
    register_image_routes(
        bp,
        _access,
        v5_call=lambda *args, **kwargs: {},
        _login_allowed=lambda login: (True, ""),
        _admin_allowed=lambda: False,
        _token=lambda login: ("token", "agency", None),
        _enqueue_content_job=lambda *args, **kwargs: jsonify({"ok": True}),
    )
    app.register_blueprint(bp)

    response = app.test_client().post("/direct/api/content-editor/images/upload",
                                      data={"login": "any"})
    assert response.status_code == 403
    assert response.get_json()["error"] == "Forbidden"
