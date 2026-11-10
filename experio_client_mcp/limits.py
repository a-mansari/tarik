"""What stops abuse: wrong credentials are counted and then paused (so this server is no way to guess an Experio
password, nor to lock a client's account), each user has a rate and a number of calls at once, and the threads that
talk to Experio are capped in all and per cabinet (one slow cabinet cannot starve the others). All in memory, for one
server process: a second process would need the same limits at the proxy."""

import threading
import time
from collections import defaultdict, deque

import anyio

from experio_client_mcp import config
from experio_client_mcp.experio import Refusal

MAX_KEYS = 5000  # keys kept by a counter before the idle ones are swept


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
        """Count a wrong credential of ``key``; return True when it starts a pause."""
        now = self.clock()
        with self.lock:
            if len(self.seen) + len(self.until) > MAX_KEYS:
                self.sweep(now)
            seen = self.seen[key]
            seen.append(now)
            while seen and seen[0] < now - config.FAILED_WINDOW:
                seen.popleft()
            if len(seen) >= config.FAILED_MAX:
                self.until[key] = now + config.PAUSE
                seen.clear()
                return True
        return False

    def cleared(self, key):
        with self.lock:
            self.seen.pop(key, None)

    def sweep(self, now):
        """Forget the keys whose failures are out of the window and whose pause is over (a guesser picks the keys:
        nothing else bounds them); if that is not enough, the oldest."""
        self.until = {k: t for k, t in self.until.items() if t > now}
        self.seen = defaultdict(
            deque, {k: s for k, s in self.seen.items() if s and s[-1] >= now - config.FAILED_WINDOW}
        )
        while len(self.seen) + len(self.until) > MAX_KEYS:
            (self.seen if self.seen else self.until).pop(next(iter(self.seen or self.until)))


class Rates:
    """A user's calls: at most RATE_PER_MINUTE a minute and CONCURRENT at once. The key is the user (a cabinet and a
    login), not the credential: a second API key brings no second allowance."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.Lock()
        self.calls, self.running = defaultdict(deque), defaultdict(int)

    def start(self, key):
        """Return "" and count a call, or the reason it is refused: "rate" or "busy"."""
        now = self.clock()
        with self.lock:
            if len(self.calls) > MAX_KEYS:
                self.sweep(now)
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

    def sweep(self, now):
        """Forget the users with nothing running and no call in the last minute."""
        idle = [k for k, c in self.calls.items() if not self.running[k] and (not c or c[-1] < now - 60)]
        for key in idle:
            del self.calls[key]
            self.running.pop(key, None)


class Capacity:
    """The worker threads: THREADS at once in all, CABINET_THREADS per cabinet. A call whose cabinet is full is
    refused at once, not queued behind a slow Experio."""

    def __init__(self):
        self.total, self.cabinets = None, {}  # made inside the event loop

    async def run(self, cabinet, function):
        """Run ``function`` in a worker thread for ``cabinet``; raise Refusal when that cabinet's threads are all
        busy."""
        self.total = self.total or anyio.CapacityLimiter(config.THREADS)
        each = self.cabinets.setdefault(cabinet, anyio.CapacityLimiter(config.CABINET_THREADS))
        borrower = object()  # one token per call, not per task
        try:
            each.acquire_on_behalf_of_nowait(borrower)
        except anyio.WouldBlock:
            raise Refusal("Experio est très sollicité pour ce cabinet : réessayez dans un instant.") from None
        try:
            return await anyio.to_thread.run_sync(function, limiter=self.total)
        finally:
            each.release_on_behalf_of(borrower)
