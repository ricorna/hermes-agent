"""Connect uses HTTP, never an op session or CLI listing (synthetic credentials only)."""
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent.secret_scope import reset_secret_scope, set_secret_scope, set_multiplex_context, reset_multiplex_context
from agent.vault_backends.onepassword import OnePasswordLoginBackend

VAULT = "v" * 26
ITEM = "i" * 26
LOGIN = {"id": ITEM, "category": "LOGIN", "title": "Example",
         "urls": [{"href": "https://example.com/login"}, {"href": "https://other.example"}],
         "vault": {"id": VAULT}}


@contextmanager
def scope(values):
    token = set_secret_scope(values)
    multiplex = set_multiplex_context(True)
    try:
        yield
    finally:
        reset_multiplex_context(multiplex)
        reset_secret_scope(token)


@pytest.fixture
def connect_server():
    calls = []
    routes = {"/v1/vaults": (200, [{"id": VAULT}]),
              f"/v1/vaults/{VAULT}/items": (200, [LOGIN]),
              f"/v1/vaults/{VAULT}/items/{ITEM}": (200, {**LOGIN, "fields": [
                  {"id": "password", "purpose": "PASSWORD", "value": " test password\n"},
                  {"id": "username", "purpose": "USERNAME", "value": "test@example.com"}]})}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization")))
            status, body = routes.get(self.path, (404, {}))
            self.send_response(status)
            if status == 302:
                self.send_header("Location", body)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = f"http://127.0.0.1:{server.server_port}"
    try:
        with scope({"OP_CONNECT_HOST": host, "OP_CONNECT_TOKEN": "test-connect-token"}):
            yield host, routes, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_connect_lists_metadata_and_resolves_password_without_cli(connect_server, monkeypatch):
    _, _, calls = connect_server
    monkeypatch.setattr(OnePasswordLoginBackend, "_op", lambda self: pytest.fail("Connect must not invoke op"))
    backend = OnePasswordLoginBackend()
    assert backend.is_unlocked()
    metas = backend.list_items()
    assert [m.id for m in metas] == [f"op:{ITEM}"]
    assert metas[0].allowed_origins == ("https://example.com", "https://other.example")
    assert "test password" not in repr(metas)
    assert not any(path.endswith(ITEM) for path, _ in calls), "listing must not read full items"
    assert backend.get_meta(f"op:{ITEM}") == metas[0]
    assert backend.resolve_password(f"op:{ITEM}") == " test password\n"
    assert all(auth == "Bearer test-connect-token" for _, auth in calls)


def test_connect_otp_uses_existing_totp_parameters(connect_server, monkeypatch):
    _, routes, _ = connect_server
    routes[f"/v1/vaults/{VAULT}/items/{ITEM}"][1]["fields"].append({
        "type": "OTP", "value": "otpauth://totp/test?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ&digits=8"})
    monkeypatch.setattr("time.time", lambda: 59)
    assert OnePasswordLoginBackend().resolve_otp(f"op:{ITEM}") == "94287082"


def test_connect_scope_does_not_fall_back_to_launch_credentials(connect_server, monkeypatch):
    host, _, calls = connect_server
    backend = OnePasswordLoginBackend()
    backend.list_items()
    monkeypatch.setenv("OP_CONNECT_HOST", host)
    monkeypatch.setenv("OP_CONNECT_TOKEN", "launch-token-must-not-be-used")
    with scope({}):
        assert not OnePasswordLoginBackend().is_unlocked()
        assert not backend.is_unlocked()
    with scope({"OP_CONNECT_HOST": host}):
        with pytest.raises(RuntimeError, match="requires both"):
            backend.list_items()
    assert all(auth == "Bearer test-connect-token" for _, auth in calls)


@pytest.mark.parametrize("status", [302, 401, 403, 500])
def test_connect_errors_never_echo_bodies_or_follow_redirects(connect_server, status):
    host, routes, calls = connect_server
    routes["/v1/vaults"] = (status, host + "/sensitive-response-body")
    with pytest.raises(RuntimeError) as caught:
        OnePasswordLoginBackend().list_items()
    assert "sensitive-response-body" not in str(caught.value)
    assert "test-connect-token" not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize("category,trashed", [("SECURE_NOTE", False), ("LOGIN", True)])
def test_connect_refuses_nonlogin_and_trashed_items(connect_server, category, trashed):
    _, routes, calls = connect_server
    routes[f"/v1/vaults/{VAULT}/items"] = (200, [{**LOGIN, "category": category, "trashed": trashed}])
    with pytest.raises(RuntimeError, match="not found"):
        OnePasswordLoginBackend().resolve_password(f"op:{ITEM}")
    assert not any(path.endswith(ITEM) for path, _ in calls)


@pytest.mark.parametrize("host", ["file:///etc/passwd", "http://user:password@example.com", "https://example.com/?q=x", "https://example.com/#x"])
def test_connect_rejects_non_origin_endpoints(host):
    with scope({"OP_CONNECT_HOST": host, "OP_CONNECT_TOKEN": "test-only"}):
        with pytest.raises(RuntimeError, match="HTTP.S. origin"):
            OnePasswordLoginBackend().list_items()


def test_connect_fill_keeps_origin_guard_and_secret_channel(connect_server, monkeypatch):
    from tools import browser_vault_tool as tool
    backend = OnePasswordLoginBackend()
    monkeypatch.setattr("agent.vault_backends.base.enabled_backends", lambda: [backend])
    monkeypatch.setattr("agent.vault_backends.enabled_backends", lambda: [backend])
    current_origin = ["https://wrong.example"]
    monkeypatch.setattr(tool, "_current_page_origin", lambda *a, **kw: current_origin[0])
    monkeypatch.setattr(tool, "_eval_js", lambda *a, **kw: {"success": True, "result": json.dumps([
        {"tag": "input", "type": "password", "name": "password", "id": "pw",
         "autocomplete": "current-password", "visible": True}])})
    scripts = []

    def secret_eval(task, script):
        scripts.append(script)
        return {"success": True, "result": json.dumps({"filled": 1})}

    monkeypatch.setattr(tool, "_eval_js_secret", secret_eval)
    denied = json.loads(tool.browser_vault_fill(f"op:{ITEM}", task_id="test"))
    assert not denied["success"]
    assert not scripts
    current_origin[0] = "https://example.com"
    filled = json.loads(tool.browser_vault_fill(f"op:{ITEM}", task_id="test"))
    assert filled["success"]
    assert "test password" not in json.dumps(filled)
    assert "test password" in scripts[0]


def test_connect_ignores_ambient_proxy_and_uses_current_scope(connect_server, monkeypatch):
    host, _, calls = connect_server
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    backend = OnePasswordLoginBackend()
    backend.list_items()
    with scope({"OP_CONNECT_HOST": host, "OP_CONNECT_TOKEN": "second-test-token"}):
        backend.resolve_password(f"op:{ITEM}")
    assert calls[-1][1] == "Bearer second-test-token"


@pytest.mark.parametrize("value", [None, "", 42])
def test_connect_missing_password_is_a_safe_error(connect_server, value):
    _, routes, _ = connect_server
    routes[f"/v1/vaults/{VAULT}/items/{ITEM}"][1]["fields"][0]["value"] = value
    with pytest.raises(RuntimeError, match="no password"):
        OnePasswordLoginBackend().resolve_password(f"op:{ITEM}")


def test_connect_invalid_item_id_cannot_change_request_path(connect_server):
    _, routes, calls = connect_server
    routes[f"/v1/vaults/{VAULT}/items"] = (200, [{**LOGIN, "id": "../../other"}])
    with pytest.raises(RuntimeError, match="invalid item or vault ID"):
        OnePasswordLoginBackend().list_items()
    assert len(calls) == 2


def test_connect_absent_or_invalid_otp_returns_none(connect_server):
    _, routes, _ = connect_server
    backend = OnePasswordLoginBackend()
    assert backend.resolve_otp(f"op:{ITEM}") is None
    routes[f"/v1/vaults/{VAULT}/items/{ITEM}"][1]["fields"].append({"type": "OTP", "value": "not-a-seed!"})
    assert backend.resolve_otp(f"op:{ITEM}") is None


def test_connect_is_available_without_cli_and_respects_opt_out(connect_server, monkeypatch):
    from agent.vault_backends import base
    monkeypatch.setattr("agent.secret_sources.onepassword.find_op", lambda: None)
    monkeypatch.setattr(base, "_cfg", dict)
    assert base.is_installed("onepassword")
    assert base.is_enabled("onepassword")
    assert any(isinstance(b, OnePasswordLoginBackend) for b in base.enabled_backends())
    monkeypatch.setattr(base, "_cfg", lambda: {"onepassword": {"enabled": False}})
    assert not base.is_enabled("onepassword")
