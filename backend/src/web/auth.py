"""轻量账号与令牌（演示级，标准库实现）。

账号文件为 JSON：{用户名: {"password": 明文, "role": 角色, "name": 显示名}}。
令牌为 HMAC 签名的「用户名|过期时间」，存内存表，支持登出失效。
"""

import hashlib
import hmac
import secrets
import threading
import time

TOKEN_TTL = 8 * 3600


class Authenticator:
    def __init__(self, accounts: dict, secret: str):
        self.accounts = accounts
        self.secret = secret.encode('utf-8')
        self._revoked = set()
        self._lock = threading.Lock()

    def login(self, username, password):
        account = self.accounts.get(username)
        if account is None or not hmac.compare_digest(account['password'], password or ''):
            raise PermissionError('账号或密码不正确')
        token = self._sign(username, int(time.time()) + TOKEN_TTL)
        return token, account

    def _sign(self, username, expires):
        payload = f'{username}|{expires}'
        mac = hmac.new(self.secret, payload.encode('utf-8'), hashlib.sha256).hexdigest()
        return f'{payload}|{mac}'

    def logout(self, token):
        with self._lock:
            self._revoked.add(token)

    def verify(self, token):
        if not token or token in self._revoked:
            return None
        try:
            username, expires, mac = token.rsplit('|', 2)
            expected = hmac.new(
                self.secret, f'{username}|{expires}'.encode('utf-8'),
                hashlib.sha256).hexdigest()
            if not hmac.compare_digest(mac, expected):
                return None
            if int(expires) < time.time():
                return None
        except (ValueError, TypeError):
            return None
        account = self.accounts.get(username)
        if account is None:
            return None
        return {'username': username, **account}

    @staticmethod
    def random_secret():
        return secrets.token_hex(24)
