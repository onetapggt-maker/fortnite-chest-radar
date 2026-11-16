"""Count recognised chests and read the game's yellow find announcements."""

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
    purple = cv2.inRange(hsv, (124, 38, 155), (169, 255, 255))
    inset = yellow.shape[1] // 5
    if cv2.countNonZero(yellow[:, inset:-inset]) < 22 and cv2.countNonZero(purple) < 22:
        return None
    return cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2GRAY), yellow, purple


def colored_line(record, mask, minimum):
    x, y, right, bottom = (max(0, round(value)) for value in record["box"])
    region = mask[y:bottom, x:right]
    return bool(region.size and cv2.countNonZero(region) >= max(12, region.size * minimum))


def chest_award(lines, purple):
    # The XP toast uses a short, purple «СУНДУК» line. Ordinary objective
    # descriptions and the on-screen search prompt have different text.
    return any(row["text"].upper().strip(' «»“”"!.') == "СУНДУК"
               and colored_line(row, purple, .09) for row in lines)


def find_name(lines, yellow):
    fallback = None
    for record in lines:
        line = " ".join(record["text"].upper().replace("Ё", "Е").split()).strip("«»! ")
        match = BANNER_TITLE.fullmatch(line)
        if not match and line != "ПИРАТСКИЙ ЖЕТОН":
            continue
        if not colored_line(record, yellow, .045):
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


class ChestTracker:
    def __init__(self, same_chest):
        self.same_chest = same_chest
        self.last_sample = 0
        self.recent = None
        self.counted_points = set()
        self.last_cue = None

    def reset_points(self):
        self.recent = None
        self.last_cue = None
        self.counted_points.clear()

    def update(self, window, sample, point, now):
        if window is None or not sample or sample[0] != window:
            return False
        if sample[1] <= self.last_sample or now - sample[1] > .5:
            return False
        _, captured, state, quad = sample
        self.last_sample = captured
        if state != "closed":
            return False
        return self.sighting(window, captured, quad, point)

    def cue(self, window, sample, now):
        if not sample or sample[0] != window or now - sample[1] > .9:
            return False
        previous = self.last_cue
        if previous == sample or (previous and sample[1] < previous[1]):
            return False
        self.last_cue = sample
        if not previous or previous[0] != window or previous[2] != sample[2] or sample[1]-previous[1] > 1:
            return False
        return self.sighting(window, sample[1], None, sample[2])

    def award(self, window, captured, point):
        prior = self.recent
        if prior and prior[0] == window and abs(captured-prior[1]) <= 2.5:
            if point is None or prior[3] in (None, point):
                if point is not None:
                    self.counted_points.add(point)
                return False
        return self.sighting(window, captured, None, point)

    def sighting(self, window, captured, quad, point):
        prior = self.recent
        if prior and prior[0] == window and captured < prior[1]:
            return False
        same_track = False
        if prior and prior[0] == window and not (point is not None and prior[3] not in (None, point)):
            elapsed = captured - prior[1]
            if quad is None or prior[2] is None:
                same_track = elapsed <= 2.5
            else:
                same_track = elapsed <= .6 or (elapsed <= 4 and self.same_chest(prior[2], quad))
        if same_track:
            point = point if point is not None else prior[3]
            quad = quad if quad is not None else prior[2]
        self.recent = (window, captured, quad, point)
        counted = point in self.counted_points
        if point is not None:
            self.counted_points.add(point)
        return not same_track and not counted


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
    last_award = float("-inf")
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
        events.put(("status", "Распознавание находок и уведомлений о сундуках включено."))
        with MSS() as capture:
            while not stop.is_set():
                start = time.monotonic()
                window = active_window()
                name = None
                award = False
                if window is not None:
                    try:
                        frame = banner_crop(np.asarray(capture.grab(crop_rect(window, BANNER_REGION))))
                    except (ScreenShotError, cv2.error):
                        frame = None
                    if frame is not None:
                        gray, yellow, purple = frame
                        ok, encoded = cv2.imencode(".png", gray, [cv2.IMWRITE_PNG_COMPRESSION, 1])
                        if ok:
                            process.stdin.write(base64.b64encode(encoded).decode("ascii") + "\n")
                            process.stdin.flush()
                            reply = process.stdout.readline()
                            if not reply:
                                raise OSError("Windows OCR failed")
                            if reply.strip() != "ocr-error":
                                lines = json.loads(reply)
                                name = find_name(lines, yellow)
                                award = chest_award(lines, purple)
                label = banner.update(name, start)
                if label:
                    events.put(("find", label))
                if award:
                    if start - last_award > 1.2:
                        events.put(("chest_award", (window, start)))
                    last_award = start
                stop.wait(max(0, .17 - (time.monotonic()-start)))
    except (OSError, ValueError, ScreenShotError):
        events.put(("status", "Распознавание баннеров недоступно."))
    finally:
        if process is not None:
            process.terminate()
            process.communicate()
