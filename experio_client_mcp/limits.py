"""What stops abuse: wrong credentials are counted and then paused (so this server is no way to guess an Experio
password, nor to lock a client's account), and each credential has a rate and a number of calls at once. All in memory,
for one server process: a second process would need the same limits at the proxy."""

import threading
import time
from collections import defaultdict, deque

from experio_client_mcp import config


class Failures:
    """Wrong credentials per key (a login in a cabinet, an address): after FAILED_MAX in FAILED_WINDOW seconds the key
    is refused for PAUSE seconds, even with the right credential."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.Lock()
        self.seen, self.until = defaultdict(deque), {}

    def paused(self, key):
        with self.lock:
            return self.until.get(key, 0) > self.clock()

    def failed(self, key):
        now = self.clock()
        with self.lock:
            seen = self.seen[key]
            seen.append(now)
            while seen and seen[0] < now - config.FAILED_WINDOW:
                seen.popleft()
            if len(seen) >= config.FAILED_MAX:
                self.until[key] = now + config.PAUSE
                seen.clear()

    def cleared(self, key):
        with self.lock:
            self.seen.pop(key, None)


class Rates:
    """A credential's calls: at most RATE_PER_MINUTE a minute and CONCURRENT at once."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.Lock()
        self.calls, self.running = defaultdict(deque), defaultdict(int)

    def start(self, key):
        """Return "" and count a call, or the reason it is refused: "rate" or "busy"."""
        now = self.clock()
        with self.lock:
            calls = self.calls[key]
            while calls and calls[0] < now - 60:
                calls.popleft()
            if len(calls) >= config.RATE_PER_MINUTE:
                return "rate"
            if self.running[key] >= config.CONCURRENT:
                return "busy"
            calls.append(now)
            self.running[key] += 1
        return ""

    def end(self, key):
        with self.lock:
            self.running[key] = max(0, self.running[key] - 1)
