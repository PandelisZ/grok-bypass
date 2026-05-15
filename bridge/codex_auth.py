"""Codex ChatGPT OAuth auth loading and refresh helpers."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_REFRESH_URL = os.environ.get(
    "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
    "https://auth.openai.com/oauth/token",
)
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_REFRESH_SKEW_SECONDS = 90


class BridgeError(RuntimeError):
    def __init__(self, message: str, status: int = 500) -> None:
        super().__init__(message)
        self.status = status


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def decode_jwt_payload(jwt: str) -> dict[str, Any]:
    parts = jwt.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def jwt_expired(jwt: str, skew_seconds: int = TOKEN_REFRESH_SKEW_SECONDS) -> bool:
    exp = decode_jwt_payload(jwt).get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return exp <= time.time() + skew_seconds


class CodexAuthStore:
    def __init__(self, codex_home: Path) -> None:
        self.codex_home = codex_home
        self.auth_file = codex_home / "auth.json"
        self.lock = threading.Lock()

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            with self.auth_file.open("r", encoding="utf-8") as file:
                value = json.load(file)
        except FileNotFoundError as exc:
            raise BridgeError(f"Codex auth file not found: {self.auth_file}", 401) from exc
        if not isinstance(value, dict):
            raise BridgeError("Codex auth file is not a JSON object", 401)
        return value

    def _save_unlocked(self, value: dict[str, Any]) -> None:
        self.codex_home.mkdir(parents=True, exist_ok=True)
        tmp = self.auth_file.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as file:
            json.dump(value, file, indent=2)
            file.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.auth_file)

    def auth_headers(self) -> dict[str, str]:
        with self.lock:
            auth = self._load_unlocked()
            tokens = self._tokens(auth)
            access_token = self._access_token(tokens)
            if jwt_expired(access_token):
                auth = self._refresh_unlocked(auth)
                tokens = self._tokens(auth)
                access_token = self._access_token(tokens)
            return self._headers_for(tokens, access_token)

    def refresh_after_unauthorized(self) -> dict[str, str]:
        with self.lock:
            auth = self._refresh_unlocked(self._load_unlocked())
            tokens = self._tokens(auth)
            return self._headers_for(tokens, self._access_token(tokens))

    def _tokens(self, auth: dict[str, Any]) -> dict[str, Any]:
        mode = auth.get("auth_mode") or "chatgpt"
        if mode != "chatgpt":
            raise BridgeError(f"Codex auth mode {mode!r} is not ChatGPT OAuth auth", 401)
        tokens = auth.get("tokens")
        if not isinstance(tokens, dict):
            raise BridgeError("Codex ChatGPT auth has no token payload", 401)
        return tokens

    def _access_token(self, tokens: dict[str, Any]) -> str:
        access_token = tokens.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise BridgeError("Codex ChatGPT auth has no access token", 401)
        return access_token

    def _headers_for(self, tokens: dict[str, Any], access_token: str) -> dict[str, str]:
        id_token_auth = self._id_token_auth(tokens)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "codex-cli",
        }
        account_id = tokens.get("account_id") or id_token_auth.get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id:
            headers["ChatGPT-Account-ID"] = account_id
        if id_token_auth.get("chatgpt_account_is_fedramp") is True:
            headers["X-OpenAI-Fedramp"] = "true"
        return headers

    def _id_token_auth(self, tokens: dict[str, Any]) -> dict[str, Any]:
        id_token = tokens.get("id_token")
        if isinstance(id_token, str):
            claims = decode_jwt_payload(id_token)
            auth_claims = claims.get("https://api.openai.com/auth")
            return auth_claims if isinstance(auth_claims, dict) else {}
        return {}

    def _refresh_unlocked(self, auth: dict[str, Any]) -> dict[str, Any]:
        tokens = self._tokens(auth)
        refresh_token = tokens.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise BridgeError("Codex ChatGPT auth has no refresh token", 401)

        request = urllib.request.Request(
            DEFAULT_REFRESH_URL,
            data=json_bytes(
                {
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                }
            ),
            headers={"Content-Type": "application/json", "User-Agent": "codex-cli"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise BridgeError(f"Codex token refresh failed with HTTP {exc.code}", 401) from exc
        except Exception as exc:
            raise BridgeError(f"Codex token refresh failed: {exc}", 502) from exc

        if not isinstance(payload, dict):
            raise BridgeError("Codex token refresh returned a non-object payload", 502)
        if isinstance(payload.get("access_token"), str):
            tokens["access_token"] = payload["access_token"]
        if isinstance(payload.get("refresh_token"), str):
            tokens["refresh_token"] = payload["refresh_token"]
        if isinstance(payload.get("id_token"), str):
            tokens["id_token"] = payload["id_token"]
        auth["last_refresh"] = utc_now().isoformat()
        self._save_unlocked(auth)
        return auth
