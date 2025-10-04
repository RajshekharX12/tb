import time

def parse_size_mb(v: str) -> int:
    try:
        return int(float(v))
    except Exception:
        return 1900

class RateLimiter:
    """Simple in-memory rate limiter per user (N actions / window)."""
    def __init__(self, limit: int, window_sec: int):
        self.limit = limit
        self.window = window_sec
        self.events = {}  # user_id -> list[timestamps]

    def check(self, user_id: int):
        now = time.time()
        arr = self.events.setdefault(user_id, [])
        # drop old
        self.events[user_id] = [t for t in arr if now - t < self.window]
        if len(self.events[user_id]) >= self.limit:
            wait = self.window - (now - self.events[user_id][0])
            return False, max(0, wait)
        self.events[user_id].append(now)
        return True, 0
