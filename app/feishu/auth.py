"""飞书 OAuth 授权与 token 管理。

- 授权页:  https://accounts.feishu.cn/open-apis/authen/v1/authorize
- 换 token: https://accounts.feishu.cn/oauth/v3/token（失败回退 v2）
- token 落盘 data_dir/tokens.json（chmod 600），带 refresh_token 自动续期
"""
import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path

import httpx

AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL_V3 = "https://accounts.feishu.cn/oauth/v3/token"
TOKEN_URL_V2 = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"


class AuthError(Exception):
    pass


class FeishuAuth:
    def __init__(self, app_id: str, app_secret: str, scopes: str,
                 redirect_uri: str, data_dir: Path):
        self.app_id = app_id
        self.app_secret = app_secret
        self.scopes = scopes
        self.redirect_uri = redirect_uri
        self.token_file = data_dir / "tokens.json"
        self._tokens: dict = {}
        self._state = ""
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        if self.token_file.exists():
            try:
                self._tokens = json.loads(self.token_file.read_text())
            except Exception:
                self._tokens = {}

    def _save(self):
        self.token_file.write_text(json.dumps(self._tokens, ensure_ascii=False))
        os.chmod(self.token_file, 0o600)

    # ---------- 授权流程 ----------
    def authorize_url(self) -> str:
        self._state = secrets.token_urlsafe(16)
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "client_id": self.app_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": self._state,
        })

    def handle_callback(self, code: str, state: str):
        if state != self._state:
            raise AuthError("state 不匹配，请重新发起授权")
        self._grant({
            "grant_type": "authorization_code",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
        })

    def _grant(self, body: dict):
        r = self._post_token(TOKEN_URL_V3, body)
        if not r.get("access_token"):
            r = self._post_token(TOKEN_URL_V2, body)
        if not r.get("access_token"):
            raise AuthError(
                f"获取 token 失败 code={r.get('code')}: "
                f"{r.get('error_description') or r.get('msg')}"
            )
        now = time.time()
        self._tokens = {
            "access_token": r["access_token"],
            "expires_at": now + r.get("expires_in", 7200) - 120,
            "refresh_token": r.get("refresh_token", self._tokens.get("refresh_token")),
            "refresh_expires_at": (now + r["refresh_token_expires_in"] - 120)
            if r.get("refresh_token_expires_in") else self._tokens.get("refresh_expires_at"),
            "scope": r.get("scope", ""),
        }
        self._save()

    @staticmethod
    def _post_token(url: str, body: dict) -> dict:
        try:
            resp = httpx.post(url, json=body, timeout=30)
            return resp.json()
        except Exception as e:
            return {"code": -1, "msg": str(e)}

    # ---------- 使用 ----------
    @property
    def authorized(self) -> bool:
        return bool(self._tokens.get("access_token"))

    @property
    def granted_scope(self) -> str:
        return self._tokens.get("scope", "")

    def access_token(self) -> str:
        if not self.authorized:
            raise AuthError("尚未授权，请先在网页上点击「连接飞书」")
        if time.time() < self._tokens.get("expires_at", 0):
            return self._tokens["access_token"]
        # 过期 → 刷新
        rt = self._tokens.get("refresh_token")
        if not rt or time.time() > (self._tokens.get("refresh_expires_at") or 0):
            self._tokens = {}
            self._save()
            raise AuthError("登录已过期，请重新授权（refresh_token 缺失或过期）")
        self._grant({
            "grant_type": "refresh_token",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "refresh_token": rt,
        })
        return self._tokens["access_token"]

    def logout(self):
        self._tokens = {}
        self._save()
