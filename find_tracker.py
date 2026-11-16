"""Count chest openings and read the game's yellow find announcements."""

from collections import Counter, deque
from datetime import datetime, timezone
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import time

import cv2
from mss import MSS
from mss.exception import ScreenShotError
import numpy as np


BANNER_REGION = (.19, .06, .81, .60)
GENERIC_FIND = "Находка"
BANNER_TITLE = re.compile(
    r"^(?:(?:НАЙДЕН[АЫО]?|ОБНАРУЖЕН[АЫО]?|ПОЛУЧЕН[АЫО]?)\s+НАХОДК[АИ]"
    r"|НАХОДК[АИ](?:\s+НАЙДЕН[АЫО]?)?)(?=$|[\s:!—–-])\s*[:!—–-]?\s*(.*)$"
)


def banner_crop(frame):
    height, width = frame.shape[:2]
    scale = min(1.0, 1150 / width)
    if scale < 1:
        frame = cv2.resize(frame, (round(width*scale), round(height*scale)), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2HSV)
    yellow = cv2.inRange(hsv, (13, 95, 155), (39, 255, 255))
    inset = yellow.shape[1] // 5
    if cv2.countNonZero(yellow[:, inset:-inset]) < 22:
        return None
    return cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2GRAY), yellow


def find_name(lines, yellow):
    fallback = None
    for record in lines:
        line = " ".join(record["text"].upper().replace("Ё", "Е").split()).strip("«»! ")
        match = BANNER_TITLE.fullmatch(line)
        if not match and line != "ПИРАТСКИЙ ЖЕТОН":
            continue
        x, y, right, bottom = (max(0, round(value)) for value in record["box"])
        region = yellow[y:bottom, x:right]
        if not region.size or cv2.countNonZero(region) < max(12, region.size * .045):
            continue
        if not match:
            return "Пиратский жетон"
        name = match[1].strip("«»! .")
        if name and any(letter.isalpha() for letter in name):
            return name.capitalize()
        fallback = GENERIC_FIND
    return fallback


class BannerTracker:
    def __init__(self):
        self.candidate = None
        self.first_seen = None
        self.last_seen = None
        self.current = None

    def update(self, name, now):
        if self.last_seen is not None and now - self.last_seen > 1.2:
            self.candidate = self.current = None
        if not name:
            return None
        self.last_seen = now
        if name != self.candidate:
            self.candidate = name
            self.first_seen = now
            return None
        if name == self.current or now - self.first_seen < .09:
            return None
        if self.current is not None and name == GENERIC_FIND:
            return None
        previous = self.current
        self.current = name
        if previous == GENERIC_FIND:
            return None
        return name


class OpenTracker:
    def __init__(self, same_chest):
        self.same_chest = same_chest
        self.closed = None
        self.opened_once = False
        self.last_sample = 0

    def update(self, window, sample, now):
        if window is None:
            self.closed = None
            self.opened_once = False
            return False
        if not sample or sample[0] != window or sample[1] <= self.last_sample or now - sample[1] > .5:
            return False
        _, captured, state, quad = sample
        self.last_sample = captured
        if state == "closed":
            self.closed = (captured, quad)
            self.opened_once = False
            return False
        if self.closed is None or captured - self.closed[0] > 1.5:
            return False
        if not self.same_chest(self.closed[1], quad):
            return False
        if self.opened_once:
            self.closed = None
            self.opened_once = False
            return True
        self.opened_once = True
        return False


class TrackerLog:
    def __init__(self, path):
        self.path = path
        self.totals = Counter()
        self.rows = deque(maxlen=200)
        self.error = None
        try:
            with path.open(encoding="utf-8") as log:
                for line in log:
                    try:
                        row = json.loads(line)
                        stamp = datetime.fromisoformat(row["time"])
                        kind, label = row["kind"], row["label"]
                        if kind in ("chest", "find") and isinstance(label, str):
                            self.remember(kind, label, stamp)
                    except (ValueError, KeyError, TypeError):
                        continue
        except FileNotFoundError:
            pass
        except OSError:
            self.error = "Не удалось открыть журнал."

    def remember(self, kind, label, stamp):
        self.totals[kind] += 1
        if kind == "find":
            self.rows.append((label, stamp))

    def add(self, kind, label):
        stamp = datetime.now(timezone.utc)
        try:
            with self.path.open("a", encoding="utf-8") as log:
                log.write(json.dumps({"time": stamp.isoformat(), "kind": kind, "label": label},
                                     ensure_ascii=False) + "\n")
        except OSError:
            self.error = "Не удалось сохранить запись."
        else:
            self.error = None
        self.remember(kind, label, stamp)


def monitor_banners(stop, events, active_window, crop_rect):
    script = Path(__file__).with_name("find_ocr.ps1")
    powershell = Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    banner = BannerTracker()
    process = None
    try:
        process = subprocess.Popen(
            [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
        )
        ready = process.stdout.readline().strip()
        if ready != "ready":
            message = ("Установи распознавание текста для русского языка в настройках Windows."
                       if ready == "no-russian-ocr" else "Не удалось включить распознавание баннеров.")
            events.put(("status", message))
            return
        events.put(("status", "Распознавание жёлтых баннеров включено."))
        with MSS() as capture:
            while not stop.is_set():
                start = time.monotonic()
                window = active_window()
                name = None
                if window is not None:
                    try:
                        frame = banner_crop(np.asarray(capture.grab(crop_rect(window, BANNER_REGION))))
                    except (ScreenShotError, cv2.error):
                        frame = None
                    if frame is not None:
                        gray, yellow = frame
                        ok, encoded = cv2.imencode(".png", gray, [cv2.IMWRITE_PNG_COMPRESSION, 1])
                        if ok:
                            process.stdin.write(base64.b64encode(encoded).decode("ascii") + "\n")
                            process.stdin.flush()
                            reply = process.stdout.readline()
                            if not reply or reply.strip() == "ocr-error":
                                raise OSError("Windows OCR failed")
                            name = find_name(json.loads(reply), yellow)
                label = banner.update(name, start)
                if label:
                    events.put(("find", label))
                stop.wait(max(0, .17 - (time.monotonic()-start)))
    except (OSError, ValueError, ScreenShotError):
        events.put(("status", "Распознавание баннеров недоступно."))
    finally:
        if process is not None:
            process.terminate()
            process.communicate()
