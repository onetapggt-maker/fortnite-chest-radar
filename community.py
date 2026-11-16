"""Read and submit community sightings without account credentials."""

import json
from queue import Empty, Full, Queue
import time
from urllib.request import Request, urlopen


URL = "https://fortnite-chest-radar-api-onetapggt.yummydot9.chatgpt.site/observations"
VERSION = "43.10"
POINT_COUNT = 965


class Community:
    def __init__(self, enabled):
        self.enabled = enabled
        self.pending = Queue(maxsize=32)
        self.points = frozenset()

    def set_enabled(self, enabled):
        self.enabled = enabled
        if not enabled:
            while True:
                try:
                    self.pending.get_nowait()
                except Empty:
                    break

    def submit(self, point):
        if not self.enabled or point is None:
            return
        try:
            self.pending.put_nowait((point, time.monotonic() + 300))
        except Full:
            pass

    def run(self, stop):
        next_read = 0.0
        next_send = 0.0
        current = None
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_read:
                next_read = now + 90
                try:
                    data = self.request()
                    if data["version"] == VERSION:
                        self.points = frozenset(row["point_id"] for row in data["points"]
                                                if type(row["point_id"]) is int
                                                and 0 <= row["point_id"] < POINT_COUNT
                                                and row["observations"] > 0)
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            if not self.enabled or (current and current[1] <= now):
                current = None
            if self.enabled and current is None:
                try:
                    current = self.pending.get_nowait()
                except Empty:
                    pass
            if self.enabled and current and current[1] > now and now >= next_send:
                try:
                    self.request({"version": VERSION, "point_id": current[0]})
                except (OSError, ValueError):
                    next_send = now + 15
                else:
                    current = None
                    next_read = min(next_read, now + 10)
            stop.wait(.4)

    @staticmethod
    def request(body=None):
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "User-Agent": "FortniteChestRadar/1"}
        if payload:
            headers["Content-Type"] = "application/json"
        with urlopen(Request(URL, data=payload, headers=headers), timeout=5) as response:
            return json.loads(response.read(128_000))
