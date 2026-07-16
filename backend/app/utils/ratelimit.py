"""Rate-limit helpers (audit M)。

1) client_ip_key — slowapi key_func：反向代理後取真實 client IP。
   舊版直接用 request.client.host，部署在 proxy 後所有人共用同一 IP 桶：
   (a) 全體使用者互相 DoS 對方的 login 配額；(b) 換 IP 即繞過。
   安全策略：只有「直接對端是 loopback（受信任的本機代理，如 vite dev proxy / 本機 nginx）」
   時才採信 X-Forwarded-For 的第一個 IP，否則一律用 socket 對端 IP —— 避免外部偽造 XFF。

2) check_quota — 重運算端點（KG 抽取 / VL 分析 / OCR / 上傳）的每使用者配額。
   in-memory 滑動視窗（單進程部署，與 vector_store/kg_queue 同一假設），超額回 429。

3) login 帳號層級節流 — 以 username 為 key 的指數退避鎖定，
   補足「IP 節流在 proxy 後失效 / 攻擊者換 IP」的缺口。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

_LOOPBACKS = {"127.0.0.1", "::1", "localhost"}


def client_ip_key(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer in _LOOPBACKS:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
    return peer


# ---------------------------------------------------------------- quotas
_quota_lock = threading.Lock()
_quota_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def check_quota(user_id: str, action: str, limit: int, per_seconds: int = 3600) -> None:
    """滑動視窗配額；超額丟 429。單進程 in-memory（重啟歸零，可接受）。"""
    now = time.monotonic()
    key = (user_id, action)
    with _quota_lock:
        dq = _quota_hits[key]
        cutoff = now - per_seconds
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            retry = int(dq[0] + per_seconds - now) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"操作過於頻繁（{action} 每 {per_seconds//60} 分鐘上限 {limit} 次），請 {retry} 秒後再試",
                headers={"Retry-After": str(retry)},
            )
        dq.append(now)


# ---------------------------------------------------------- login backoff
_login_lock = threading.Lock()
_login_fails: dict[str, tuple[int, float]] = {}  # username -> (fail_count, locked_until)

_LOCK_THRESHOLD = 5      # 連續失敗次數達此值開始鎖定
_BASE_LOCK_SECONDS = 30  # 首次鎖定秒數，之後每多一次失敗翻倍
_MAX_LOCK_SECONDS = 900  # 上限 15 分鐘


def check_login_allowed(username: str) -> None:
    """帳號層級節流：鎖定期間直接 429（不洩漏帳號是否存在）。"""
    with _login_lock:
        entry = _login_fails.get(username)
        if not entry:
            return
        _count, locked_until = entry
        remain = locked_until - time.monotonic()
        if remain > 0:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"嘗試次數過多，請 {int(remain)+1} 秒後再試",
                headers={"Retry-After": str(int(remain) + 1)},
            )


def record_login_failure(username: str) -> None:
    with _login_lock:
        count, _ = _login_fails.get(username, (0, 0.0))
        count += 1
        locked_until = 0.0
        if count >= _LOCK_THRESHOLD:
            lock_s = min(_BASE_LOCK_SECONDS * (2 ** (count - _LOCK_THRESHOLD)), _MAX_LOCK_SECONDS)
            locked_until = time.monotonic() + lock_s
        _login_fails[username] = (count, locked_until)


def reset_login_failures(username: str) -> None:
    with _login_lock:
        _login_fails.pop(username, None)
