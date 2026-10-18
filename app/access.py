"""Коды доступа к подробной карте.

Публичная карта показывает только тревоги по областям и районам. Подробный слой (значки дронов,
маршруты, ПВО, пуски, тепловая карта) — только тем, кому владелец выдал код (ACCESS_CODES).
В cookie лежит не сам код, а его подпись: убрали код из ACCESS_CODES — доступ по нему пропал.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

COOKIE = "dronchik_access"
COOKIE_MAX_AGE = 90 * 86400
ATTEMPTS_PER_WINDOW = 8          # перебор кодов: не больше 8 попыток с адреса за 10 минут
ATTEMPT_WINDOW_S = 600


class Access:
    def __init__(self, codes: list[str], secret: str = "", public_full: bool = False):
        self.public_full = public_full
        # без ACCESS_SECRET подписи меняются при перезапуске — придётся ввести код заново
        self.secret = (secret or secrets.token_hex(32)).encode()
        self.codes = list(codes)
        self._attempts: dict[str, deque] = defaultdict(deque)

    def token(self, code: str) -> str:
        return hmac.new(self.secret, code.encode(), hashlib.sha256).hexdigest()

    def check_code(self, code: str) -> str | None:
        """Подпись для cookie, если код верный."""
        for c in self.codes:
            if hmac.compare_digest(c.encode(), code.strip().encode()):
                return self.token(c)
        return None

    def is_full(self, cookie: str | None) -> bool:
        if self.public_full:
            return True
        return bool(cookie) and any(hmac.compare_digest(self.token(c), cookie) for c in self.codes)

    def allow_attempt(self, ip: str, now: float | None = None) -> bool:
        now = now or time.time()
        q = self._attempts[ip]
        while q and now - q[0] > ATTEMPT_WINDOW_S:
            q.popleft()
        if len(q) >= ATTEMPTS_PER_WINDOW:
            return False
        q.append(now)
        return True
