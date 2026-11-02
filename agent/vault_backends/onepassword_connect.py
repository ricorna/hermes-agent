"""Read-only Connect transport for the browser vault, independent of the op CLI.

The endpoint is operator-configured in the active profile, not supplied by a page
or item. Private HTTP deployments are supported. Redirects and ambient proxies
are disabled so neither responses nor launch-profile settings reroute the token.
"""
from __future__ import annotations

import json
import re
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_MAX_RESPONSE = 8 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _id(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9]{26}", value):
        raise RuntimeError("1Password Connect returned an invalid item or vault ID")
    return value


class ConnectClient:
    def __init__(self, host: str, token: str):
        try:
            parts = urlsplit(host)
            valid = (parts.scheme in ("http", "https") and parts.hostname
                     and not parts.username and not parts.password
                     and parts.path in ("", "/") and not parts.query and not parts.fragment
                     and not any(c.isspace() or ord(c) < 32 for c in host))
            parts.port  # validate a supplied port before any network request
        except ValueError:
            valid = False
        if not valid:
            raise RuntimeError("1Password Connect host must be an HTTP(S) origin")
        self._host = host.rstrip("/")
        self._token = token

    def _get(self, path: str):
        request = Request(self._host + path, headers={
            "Authorization": "Bearer " + self._token, "Accept": "application/json"})
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=30) as response:
                body = response.read(_MAX_RESPONSE + 1)
            if len(body) > _MAX_RESPONSE:
                raise RuntimeError("1Password Connect response is too large")
            return json.loads(body)
        except HTTPError as exc:
            # Response bodies, URLs and exception text can contain credentials.
            code = exc.code
            exc.close()
            raise RuntimeError(f"1Password Connect request failed (HTTP {code})") from None
        except (URLError, OSError, ValueError, HTTPException):
            raise RuntimeError("1Password Connect request failed or returned invalid JSON") from None

    def list_items(self) -> list[dict]:
        vaults = self._get("/v1/vaults")
        if not isinstance(vaults, list):
            raise RuntimeError("1Password Connect returned an invalid vault list")
        out = []
        seen = set()
        for vault in vaults:
            vault_id = _id(vault.get("id") if isinstance(vault, dict) else None)
            items = self._get(f"/v1/vaults/{vault_id}/items")
            if not isinstance(items, list):
                raise RuntimeError("1Password Connect returned an invalid item list")
            for item in items:
                if not isinstance(item, dict) or item.get("category") != "LOGIN":
                    continue
                if item.get("trashed"):
                    continue
                item_id = _id(item.get("id"))
                if item_id in seen:
                    raise RuntimeError("1Password Connect returned ambiguous login IDs")
                seen.add(item_id)
                # Only project metadata. Never fetch fields just to populate a list.
                out.append({"id": item_id, "vault": {"id": vault_id},
                            "title": item.get("title"), "urls": item.get("urls"),
                            "created_at": item.get("createdAt")})
        return out

    def password(self, handle: str) -> str:
        item = self._login(handle)
        for field in item.get("fields") or []:
            if isinstance(field, dict) and field.get("purpose") == "PASSWORD":
                value = field.get("value")
                if isinstance(value, str) and value:
                    return value
        raise RuntimeError("1Password Connect login has no password")

    def otp(self, handle: str) -> str | None:
        from agent.vault_store import normalize_otp_secret, totp_now
        item = self._login(handle)
        for field in item.get("fields") or []:
            if isinstance(field, dict) and field.get("type") == "OTP":
                value = field.get("value")
                if isinstance(value, str) and value:
                    try:
                        return totp_now(normalize_otp_secret(value))
                    except (ValueError, RuntimeError):
                        return None
        return None

    def _login(self, handle: str) -> dict:
        # Re-list under this operation's profile credentials; no cross-profile
        # item/vault cache and no direct reads of unlisted/non-Login handles.
        matches = [i for i in self.list_items() if handle == "op:" + i["id"]]
        if len(matches) != 1:
            raise RuntimeError("1Password login not found; list logins again")
        summary = matches[0]
        vault_id, item_id = summary["vault"]["id"], summary["id"]
        item = self._get(f"/v1/vaults/{vault_id}/items/{item_id}")
        if (not isinstance(item, dict) or item.get("id") != item_id
                or item.get("category") != "LOGIN" or item.get("trashed")
                or item.get("vault", {}).get("id") != vault_id):
            raise RuntimeError("1Password Connect login changed; list logins again")
        return item


def current_client() -> ConnectClient | None:
    from agent.secret_scope import get_secret
    host = get_secret("OP_CONNECT_HOST", "") or ""
    token = get_secret("OP_CONNECT_TOKEN", "") or ""
    if not host and not token:
        return None
    if not host or not token:
        raise RuntimeError("1Password Connect requires both OP_CONNECT_HOST and OP_CONNECT_TOKEN in this profile")
    return ConnectClient(host, token)
