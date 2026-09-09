"""C6 · 查询限流与并发闸：令牌桶（用户 QPS）+ 并发闸（全局）+ 排队超时。

- 令牌桶：单用户 rate=qps、burst 上限，平滑突发
- 并发闸：全局最大并发查询（max_concurrent），排队等待 queue_timeout_s
- 任一超时抛出 RateLimitExceeded，调用方转为用户可见错误（不做隐性排队成功）
"""
import logging
import threading
import time

from ..config_override import get_effective as get_settings

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """限流/排队超时。"""


class TokenBucket:
    """线程安全令牌桶（每用户实例）。"""

    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def try_acquire(self, timeout_s: float) -> bool:
        """等待令牌（≤ timeout_s）。返回是否获得。"""
        deadline = time.monotonic() + timeout_s
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return True
                wait = (1 - self._tokens) / self.rate if self.rate > 0 else timeout_s
            if time.monotonic() + wait > deadline:
                time.sleep(min(wait, max(0.05, deadline - time.monotonic())))
                # 最后一搏
                with self._lock:
                    self._refill()
                    if self._tokens >= 1:
                        self._tokens -= 1
                        return True
                return False
            time.sleep(min(wait, 0.1))


class ConcurrencyGate:
    """全局并发闸：并发查询数上限 + 排队超时。"""

    def __init__(self, max_concurrent: int):
        self.max = max_concurrent
        self._sem = threading.Semaphore(max_concurrent)

    def acquire(self, timeout_s: float) -> bool:
        return self._sem.acquire(timeout=timeout_s)

    def release(self) -> None:
        self._sem.release()


class RateLimiter:
    """组合：每用户令牌桶 + 全局并发闸。"""

    def __init__(self, qps: float, burst: int, max_concurrent: int,
                 queue_timeout_s: float):
        self.qps = qps
        self.burst = burst
        self.queue_timeout_s = queue_timeout_s
        self.gate = ConcurrencyGate(max_concurrent)
        self._buckets: dict[int, TokenBucket] = {}
        self._lock = threading.Lock()

    def _bucket(self, user_id: int) -> TokenBucket:
        with self._lock:
            b = self._buckets.get(user_id)
            if b is None:
                b = TokenBucket(self.qps, self.burst)
                self._buckets[user_id] = b
            return b

    def acquire(self, user_id: int) -> None:
        """进入查询：令牌 + 并发闸，任一步超时抛 RateLimitExceeded。"""
        b = self._bucket(user_id)
        if not b.try_acquire(self.queue_timeout_s):
            raise RateLimitExceeded("请求过于频繁，请稍后再试")
        if not self.gate.acquire(self.queue_timeout_s):
            raise RateLimitExceeded("系统繁忙，排队超时，请稍后再试")

    def release(self) -> None:
        self.gate.release()


def get_limiter() -> RateLimiter:
    """按配置构造限流器（幂等单例）。"""
    s = get_settings()
    if not getattr(_state, "limiter", None):
        _state.limiter = RateLimiter(
            qps=s.rate_limit_qps, burst=s.rate_limit_burst,
            max_concurrent=s.rate_limit_max_concurrent,
            queue_timeout_s=s.rate_limit_queue_timeout_s)
    return _state.limiter


class _State:
    limiter: RateLimiter | None = None


_state = _State()
