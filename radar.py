import base64
import ctypes as ct
from ctypes import wintypes as wt
import json
from datetime import datetime, timezone
import math
from pathlib import Path
from queue import Empty, Queue
import threading
import time
import tkinter as tk
from urllib.parse import urlencode
import webbrowser

import cv2
from mss import MSS
from mss.exception import ScreenShotError
import numpy as np
from PIL import Image, ImageDraw, ImageTk
import pystray

from radar_visuals import (
    GLOW_RADIUS, NOTICE_SIZE, Layer, chest_beam, radar_frame,
    notice_card, preview_base,
    preview_frame,
)
from radar_settings import Settings, clean_preferences
from chest_cue import chest_cue_position, glowing_container
from chest_memory import ChestMemory, LIVE_REGION, MAX_MARKERS
from community import Community
from find_tracker import OpenTracker, TrackerLog, monitor_banners


HERE = Path(__file__).resolve().parent
CONFIG = HERE / "minimap.local.json"
PREFERENCES = HERE / "radar-ui.local.json"
OBSERVATIONS = HERE / "chest-observations.jsonl"
TRACKER_LOG = HERE / "chest-finds.jsonl"
MEMORY_FOLDER = HERE / "chest-memory.local"
REPORT_URL = "https://github.com/onetapggt-maker/fortnite-chest-radar/issues/new"
# v43.10 source map of 2048 pixels uses about .006825 pixels per world centimetre.
METERS_PER_PIXEL = 1 / .6825
USER = ct.WinDLL("user32")
for name in ("GetForegroundWindow",):
    getattr(USER, name).restype = wt.HWND
USER.GetWindowTextW.argtypes = (wt.HWND, wt.LPWSTR, ct.c_int)
USER.GetAsyncKeyState.argtypes = (ct.c_int,)
USER.GetAsyncKeyState.restype = ct.c_short
USER.GetClientRect.argtypes = (wt.HWND, ct.POINTER(wt.RECT))
USER.ClientToScreen.argtypes = (wt.HWND, ct.POINTER(wt.POINT))

BG = "#110d20"
FG = "#faf1ff"
CYAN = "#d49aff"
GOLD = "#ffcd72"
DOT = "#ffdf00"
HALO = "#87601a"
FONT = "Segoe UI"
ISSUES = {
    "lost": ("Мини-карта не распознана", "Трей → Настройки → Объекты"),
    "capture": ("Не удалось захватить экран", "Проверь режим «Оконный без рамки»"),
    "error": ("Ошибка радара", "Подробности в настройках"),
    "restored": ("Сигнал восстановлен", "Мини-карта снова распознана"),
    "near": ("Сундук рядом", "Точка появления в пределах примерно 50 м"),
    "close": ("Очень близко", "Точка появления в пределах примерно 20 м"),
}


def tray_image():
    icon = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((4, 4, 59, 59), radius=13, fill=BG)
    draw.ellipse((10, 10, 53, 53), outline="#493267", width=4)
    draw.arc((10, 10, 53, 53), start=220, end=345, fill=CYAN, width=4)
    draw.ellipse((21, 21, 42, 42), fill=HALO)
    draw.ellipse((25, 25, 38, 38), fill=DOT)
    return icon


def fortnite_window():
    window = USER.GetForegroundWindow()
    title = ct.create_unicode_buffer(256)
    USER.GetWindowTextW(window, title, len(title))
    if not title.value.lower().startswith("fortnite"):
        return None
    origin, rect = wt.POINT(), wt.RECT()
    if not USER.GetClientRect(window, ct.byref(rect)):
        return None
    if not USER.ClientToScreen(window, ct.byref(origin)):
        return None
    if rect.right < 320 or rect.bottom < 240:
        return None
    return origin.x, origin.y, rect.right, rect.bottom


def crop_rect(window, fractions):
    x, y, width, height = window
    left, top, right, bottom = fractions
    return {
        "left": x + round(width * left),
        "top": y + round(height * top),
        "width": round(width * right) - round(width * left),
        "height": round(height * bottom) - round(height * top),
    }


def defaults(window):
    _, _, width, height = window
    for size, padding in [(0.22, 0.018), (0.19, 0.013), (0.25, 0.025), (0.16, 0.013)]:
        yield (
            1 - (padding + size) * height / width, padding,
            1 - padding * height / width, padding + size,
        )


class Island:
    def __init__(self):
        data = json.loads((HERE / "chests.json").read_text(encoding="utf-8"))
        self.size = (data["width"], data["height"])
        image = cv2.imdecode(np.fromfile(HERE / "island.png", dtype=np.uint8), 0)
        self.map = cv2.resize(image, self.size, interpolation=cv2.INTER_AREA)
        self.points = np.array([
            (x, y) for kind, x, y, *_ in data["points"]
            if data["types"][kind] in {"chest", "rare_chest"}
        ], dtype=np.float32)
        self.sift = cv2.SIFT_create(contrastThreshold=0.018)
        # Features at several resolutions help when the HUD scale changes.
        positions, descriptors = [], []
        for scale in (1.0, 0.62, 0.38):
            layer = cv2.resize(self.map, None, fx=scale, fy=scale)
            kp, desc = self.sift.detectAndCompute(layer, None)
            positions.extend((p.pt[0] / scale, p.pt[1] / scale) for p in kp)
            descriptors.append(desc)
        self.positions = np.asarray(positions, dtype=np.float32)
        self.index = cv2.FlannBasedMatcher(dict(algorithm=1, trees=5), dict(checks=64))
        self.index.add([np.vstack(descriptors)])
        self.index.train()

    def locate(self, gray, drawn_dots=None):
        height, width = gray.shape
        mask = np.zeros_like(gray)
        mask[5:-5, 5:-5] = 255
        cv2.circle(mask, (width // 2, height // 2), max(7, width // 18), 0, -1)
        if drawn_dots is not None:
            mask[drawn_dots != 0] = 0
        keys, desc = self.sift.detectAndCompute(gray, mask)
        if desc is None or len(desc) < 9:
            return None
        screen, world = [], []
        for matches in self.index.knnMatch(desc, k=6):
            match = matches[0]
            target = self.positions[match.trainIdx]
            # A landmark can appear in several resolutions of the same map.
            rival = next((
                m for m in matches[1:]
                if np.linalg.norm(self.positions[m.trainIdx] - target) > 6
            ), None)
            if rival and match.distance < 0.77 * rival.distance:
                screen.append(keys[match.queryIdx].pt)
                world.append(target)
        if len(screen) < 9:
            return None
        screen = np.asarray(screen, dtype=np.float32)
        matrix, inliers = cv2.estimateAffinePartial2D(
            screen, np.asarray(world), ransacReprojThreshold=4.0
        )
        if matrix is None or inliers is None:
            return None
        good = screen[inliers.ravel().astype(bool)]
        scale = math.hypot(matrix[0, 0], matrix[1, 0])
        if len(good) < 9 or len(good) / len(screen) < 0.28 or not 0.12 < scale < 4:
            return None
        if np.any(np.ptp(good, axis=0) < np.array([width, height]) * 0.24):
            return None
        center = matrix @ np.array([width / 2, height / 2, 1])
        if np.any(center < 0) or np.any(center >= self.size):
            return None
        return matrix

    def visible_chests(self, matrix, width, height, opened=()):
        inverse = cv2.invertAffineTransform(matrix)
        dots = self.points @ inverse[:, :2].T + inverse[:, 2]
        visible = np.all((dots >= 5) & (dots < [width - 5, height - 5]), axis=1)
        visible[list(opened)] = False
        return {int(index): tuple(dots[index]) for index in np.flatnonzero(visible)}


def ignore_markers(bounds, points):
    if not points:
        return None
    mask = np.zeros((bounds["height"], bounds["width"]), dtype=np.uint8)
    for x, y in points:
        cv2.circle(mask, (x - bounds["left"], y - bounds["top"]), GLOW_RADIUS, 255, -1)
    return mask


def monitor_visible_chests(stop, options):
    memory = ChestMemory(MEMORY_FOLDER)
    if not (memory.references or memory.open_references):
        return
    with MSS() as capture:
        while not stop.is_set():
            window = None if options["paused"] else fortnite_window()
            if window is None:
                options["live_chest"] = None
                stop.wait(.08)
                continue
            captured = time.monotonic()
            bounds = crop_rect(window, LIVE_REGION)
            origin = (bounds["left"]-window[0], bounds["top"]-window[1])
            try:
                result = memory.detect_visible(np.asarray(capture.grab(bounds)), window[3], origin)
            except (ScreenShotError, cv2.error):
                result = None
            if result is not None:
                options["live_chest"] = (window, captured, *result)
            stop.wait(max(0, .075 - (time.monotonic()-captured)))


def monitor(stop, events, options):
    try:
        island = Island()
        memory = ChestMemory(MEMORY_FOLDER)
        last_region = None
        with MSS() as capture:
            events.put(("ready",))
            while not stop.is_set():
                region = options["region"]
                active = options["enabled"] and not options["paused"]
                window = fortnite_window() if active else None
                if window is None:
                    options["map_link"] = None
                    events.put(("idle",))
                    stop.wait(0.25)
                    continue
                candidates = list(defaults(window)) if region is None else [region]
                if region is None and last_region in candidates:
                    candidates.remove(last_region)
                    candidates.insert(0, last_region)
                result = None
                try:
                    captured = time.monotonic()
                    game_frame = np.asarray(capture.grab(crop_rect(window, (0, 0, 1, 1))))
                    cue = chest_cue_position(game_frame)
                    box = glowing_container(game_frame) if cue and options["boxes"] else None
                    for candidate in candidates:
                        bounds = crop_rect(window, candidate)
                        x = bounds["left"] - window[0]
                        y = bounds["top"] - window[1]
                        frame = game_frame[y:y+bounds["height"], x:x+bounds["width"]]
                        # The glowing pixels aren't landmarks from the island map.
                        drawn = ignore_markers(bounds, options["drawn"])
                        result = island.locate(cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY), drawn)
                        if result is not None:
                            last_region = candidate
                            break
                except ScreenShotError:
                    options["map_link"] = None
                    events.put(("capture", window))
                    stop.wait(0.5)
                    continue
                if result is None:
                    options["map_link"] = None
                    options["suppress_live"] = None
                    events.put(("lost", window, cue, box))
                else:
                    opened = options["opened"]
                    dots = island.visible_chests(result, bounds["width"], bounds["height"], opened)
                    inverse = cv2.invertAffineTransform(result)
                    north = math.degrees(math.atan2(-inverse[1, 1], -inverse[0, 1]))
                    center = result @ np.array([bounds["width"]/2, bounds["height"]/2, 1])
                    distances = np.linalg.norm(island.points - center, axis=1)
                    nearest = int(distances.argmin())
                    suppressed = nearest in opened and distances[nearest] * METERS_PER_PIXEL <= 11
                    options["suppress_live"] = (window, nearest) if suppressed else None
                    can_show_cue = cue and not suppressed
                    linked_point = unique_nearby_point(distances, opened)
                    options["map_link"] = (window, captured, linked_point)
                    point_id = linked_point if can_show_cue else None
                    distances[list(opened)] = np.inf
                    distance = distances.min()
                    saved_image = None
                    if options["memory"]:
                        live = options["live_chest"]
                        visibly_closed = (live and live[0] == window and live[2] == "closed"
                                          and abs(captured-live[1]) <= .35)
                        nearby = np.flatnonzero(distances * METERS_PER_PIXEL <= 26).tolist()
                        nearby.sort(key=distances.__getitem__)
                        saved_image = memory.update(
                            game_frame, linked_point, nearby, can_show_cue, options["memory_drawn"],
                            allow_live=bool(can_show_cue or (linked_point is not None and visibly_closed)),
                        )
                    events.put(("located", window, bounds, dots, north, distance * METERS_PER_PIXEL,
                                cue if can_show_cue else None,
                                tuple(round(float(value), 1) for value in center),
                                box if can_show_cue else None, point_id,
                                saved_image))
                stop.wait(0.18)
    except (OSError, ValueError, cv2.error, RuntimeError, ScreenShotError) as error:
        events.put(("error", str(error)))


def unique_nearby_point(distances, opened):
    first, second = np.argsort(distances)[:2]
    if first in opened or distances[first] * METERS_PER_PIXEL > 11:
        return None
    if (distances[second] - distances[first]) * METERS_PER_PIXEL < 4:
        return None
    return int(first)


def same_chest(chest, lid):
    margin = max(5, float(np.ptp(chest[:, 1])) * .35)
    center = lid.mean(axis=0)
    return np.all(center >= chest.min(axis=0) - margin) and np.all(center <= chest.max(axis=0) + margin)


class Radar:
    def __init__(self, root):
        self.root = root
        root.title("Радар сундуков Fortnite")
        root.resizable(False, False)
        root.configure(bg=BG)
        self.status = tk.StringVar(value="Загружаю карту v43.10…")
        self.stop = threading.Event()
        self.events = Queue()
        self.actions = Queue()
        self.options = {"region": None, "paused": False, "enabled": True, "drawn": (), "opened": ()}
        self.error = None
        self.issue = None
        self.issue_since = 0.0
        self.notice_key = None
        self.notice_motion = None
        self.notice_until = None
        self.lost_signal = False
        self.chest_band = None
        self.last_chest_alert = float("-inf")
        self.mark_bounds = None
        self.north = -90
        self.targets = {}
        self.closest_distance = float("inf")
        self.mark_key_down = False
        self.cue_hits = 0
        self.last_cue = 0.0
        self.cue_saved = False
        self.cue_uploaded = False
        self.cue_point = None
        self.cue_candidate = None
        self.cue_point_hits = 0
        self.recent_sightings = {}
        self.marked_chests = set()
        self.handled_visual = 0.0
        self.pending_open = None
        self.open_tracker = OpenTracker(same_chest)
        self.tracker = TrackerLog(TRACKER_LOG)
        self.tracker_events = Queue()
        self.tracker_status = "Запускаю распознавание баннеров…"
        self.tracker_window = None
        self.points = {}
        self.demo_index = 0
        self.demo_start = time.monotonic()
        self.demo_kind = "near"
        self.save_job = None
        try:
            self.prefs = clean_preferences(json.loads(PREFERENCES.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            self.prefs = clean_preferences(None)
        if CONFIG.exists():
            try:
                region = tuple(json.loads(CONFIG.read_text(encoding="utf-8")))
                left, top, right, bottom = region
                if 0 <= left < right <= 1 and 0 <= top < bottom <= 1:
                    self.options["region"] = region
            except (OSError, ValueError, TypeError):
                pass
        self.build_settings()
        self.options["boxes"] = self.prefs["boxes"]
        self.options["memory"] = self.prefs["memory"]
        self.options["memory_drawn"] = ()
        self.options["live_chest"] = None
        self.options["map_link"] = None
        self.options["suppress_live"] = None
        self.slow_marker = None
        self.drawn_marker = None
        self.community = Community(self.prefs["share"])
        self.overlay = Layer()
        self.notice = Layer()
        self.box_overlay = Layer()
        self.memory_layers = [Layer() for _ in range(MAX_MARKERS)]
        self.memory_until = 0.0
        self.last_box = None
        self.box_until = 0.0
        root.protocol("WM_DELETE_WINDOW", root.withdraw)
        root.withdraw()
        self.tray = pystray.Icon("FortniteChestRadar", tray_image(), "Радар сундуков Fortnite",
                                menu=pystray.Menu(
                                    pystray.MenuItem("Показывать сундуки",
                                                    lambda: self.actions.put(self.toggle),
                                                    checked=lambda _: self.options["enabled"]),
                                    pystray.MenuItem("Настройки", lambda: self.actions.put(self.settings),
                                                    default=True),
                                    pystray.MenuItem("Сбросить открытые сундуки",
                                                     lambda: self.actions.put(self.reset_opened)),
                                    pystray.MenuItem("Замеченные сундуки",
                                                     lambda: self.actions.put(self.show_observations)),
                                    pystray.MenuItem("Трекер находок",
                                                     lambda: self.actions.put(self.show_tracker)),
                                    pystray.MenuItem("Выход", lambda: self.actions.put(self.close)),
                                ))
        self.tray.run_detached()
        threading.Thread(target=monitor, args=(self.stop, self.events, self.options),
                         daemon=True).start()
        threading.Thread(target=monitor_visible_chests, args=(self.stop, self.options),
                         daemon=True).start()
        threading.Thread(target=monitor_banners,
                         args=(self.stop, self.tracker_events, self.tracker_game_window, crop_rect),
                         daemon=True).start()
        threading.Thread(target=self.community.run, args=(self.stop,), daemon=True).start()
        root.after(33, self.update)

    def build_settings(self):
        self.logo = ImageTk.PhotoImage(tray_image(), master=self.root)
        self.preview_base = preview_base(HERE / "island.png")
        self.preview = ImageTk.PhotoImage(self.preview_base, master=self.root)
        self.demo_card = notice_card(*ISSUES[self.demo_kind], self.demo_kind)
        self.preview_notice = ImageTk.PhotoImage(self.demo_card, master=self.root)
        self.controls = Settings(self, self.logo, self.preview, self.preview_notice)

    def cycle_preview(self):
        sequence = ("near", "close", "lost", "restored")
        self.demo_index = (self.demo_index + 1) % len(sequence)
        self.demo_kind = sequence[self.demo_index]
        self.demo_card = notice_card(*ISSUES[self.demo_kind], self.demo_kind)
        self.demo_start = time.monotonic()

    def set_preference(self, key, value):
        if self.prefs[key] == value:
            return
        self.prefs[key] = value
        if key == "boxes":
            self.options["boxes"] = value
            if not value:
                self.clear_box()
        if key == "memory":
            self.options["memory"] = value
            if not value:
                self.clear_memory()
        if key == "share":
            self.community.set_enabled(value)
        if not self.prefs["notices"]:
            self.clear_notice()
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(300, self.save_preferences)

    def save_preferences(self):
        self.save_job = None
        try:
            PREFERENCES.write_text(json.dumps(self.prefs, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            self.status.set("Не удалось сохранить настройки.")

    def settings(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def toggle(self):
        self.options["enabled"] = not self.options["enabled"]
        if self.options["enabled"]:
            self.status.set(self.error or "Переключись в матч Fortnite")
        else:
            self.hide()
            self.clear_notice()
            self.status.set("Метки выключены в трее")
        self.tray.update_menu()

    def close(self):
        self.stop.set()
        if self.save_job:
            self.root.after_cancel(self.save_job)
            self.save_preferences()
        self.tray.stop()
        self.overlay.close()
        self.notice.close()
        self.box_overlay.close()
        for layer in self.memory_layers:
            layer.close()
        self.root.destroy()

    def hide(self):
        self.overlay.hide()
        self.mark_bounds = None
        self.targets.clear()
        self.points.clear()
        self.options["drawn"] = ()

    def clear_notice(self, fade=False):
        if self.notice_key is not None:
            if fade:
                self.notice_motion = (*self.notice_motion[:3], time.monotonic(), False)
            else:
                self.notice.hide()
                self.notice_motion = None
            self.notice_key = None
        elif not fade:
            self.notice.hide()
            self.notice_motion = None
        self.issue = None
        self.notice_until = None

    def render(self, bounds, dots, north):
        if self.mark_bounds != bounds:
            self.points.clear()
        self.mark_bounds = bounds
        self.targets = dots
        self.north = north

    def clear_box(self):
        self.box_overlay.hide()
        self.last_box = None
        self.box_until = 0.0

    def clear_memory(self):
        if self.options["memory_drawn"]:
            for layer in self.memory_layers:
                layer.hide()
            self.options["memory_drawn"] = ()
        self.drawn_marker = None

    def show_memory(self, window, result):
        if not self.prefs["memory"] or result is None:
            self.clear_memory()
            return
        if result[0] == "saved":
            self.show_feedback(window, "Внешний вид сохранён", "Вырез сундука сохранён на этом компьютере")
        visible = [quad for point, quad in result[1].items() if point not in self.options["opened"]]
        drawn = []
        for index, layer in enumerate(self.memory_layers):
            if index >= len(visible):
                layer.hide()
                continue
            (x, y), marker = chest_beam(visible[index], *window[2:])
            layer.draw(marker, window[0]+x, window[1]+y)
            drawn.append((x, y, marker.width, marker.height))
        self.options["memory_drawn"] = tuple(drawn)
        self.memory_until = time.monotonic() + .58

    def show_latest_chest(self, window, now):
        if not self.prefs["memory"]:
            return False
        live = self.options["live_chest"]
        slow = self.slow_marker
        suppressed = self.options["suppress_live"]
        fresh = live is not None and live[0] == window and now-live[1] < .4
        use_live = (fresh and live[2] == "closed" and
                    not (suppressed and suppressed[0] == window and suppressed[1] in self.options["opened"]))
        if use_live:
            marker, result = live, ("live", {None: live[3]})
        elif slow is not None and slow[0] == window and now-slow[1] < .58:
            marker, result = slow, slow[2]
            if fresh and live[2] == "open":
                visible = {point: quad for point, quad in result[1].items() if not same_chest(quad, live[3])}
                marker, result = live, (result[0], visible)
        else:
            self.clear_memory()
            return bool(fresh and live[2] == "open")
        if marker is not self.drawn_marker:
            self.show_memory(window, result)
            self.drawn_marker = marker
        return use_live or bool(fresh and live[2] == "open") or result[0] in ("saved", "live")

    def show_detected_chest(self, window, cue, box):
        if not cue or box is None or not self.prefs["boxes"]:
            self.clear_box()
            return
        previous = self.last_box
        self.last_box = box
        x, y, width, height = box
        if previous is None or any(abs(value-old) > max(12, min(width, height)*.25)
                                   for value, old in zip(box, previous)):
            self.box_overlay.hide()
            return
        quad = np.float32([[x, y], [x+width, y], [x+width, y+height], [x, y+height]])
        (left, top), marker = chest_beam(quad, *window[2:])
        self.box_overlay.draw(marker, window[0]+left, window[1]+top)
        self.box_until = time.monotonic() + .55

    def animate(self, now):
        if self.options["memory_drawn"] and now >= self.memory_until:
            self.clear_memory()
        if self.box_overlay.visible and now >= self.box_until:
            self.clear_box()
        if self.mark_bounds:
            bounds = self.mark_bounds
            positions = {}
            for key, (x, y) in self.targets.items():
                old_x, old_y = self.points.get(key, (x, y))
                positions[key] = (old_x + (x - old_x) * 0.45, old_y + (y - old_y) * 0.45)
            self.points = positions
            left, top = bounds["left"], bounds["top"]
            self.options["drawn"] = tuple(
                (left + round(x), top + round(y)) for x, y in positions.values()
            )
            frame = radar_frame(bounds["width"], bounds["height"], positions, now,
                                self.prefs, self.north, reported=self.community.points,
                                observed=self.recent_sightings)
            self.overlay.draw(frame, left, top, round(255 * self.prefs["opacity"] / 100))
        if self.notice_until and now >= self.notice_until:
            self.clear_notice(fade=True)
        if self.notice_motion:
            card, left, top, started, entering = self.notice_motion
            fraction = min(1.0, (now - started) / (0.23 if entering else 0.15))
            if not entering and fraction >= 1:
                self.notice.hide()
                self.notice_motion = None
            elif entering and fraction >= 1:
                if not self.notice.visible or started != 0:
                    self.notice.draw(card, left, top)
                    self.notice_motion = (card, left, top, 0, True)
            else:
                easing = 1 - (1 - fraction) ** 3
                amount = easing if entering else 1 - easing
                self.notice.draw(card, left + round((1 - amount) * 8), top, round(255 * amount))
        if self.root.state() == "normal":
            self.preview.paste(preview_frame(self.preview_base, now, self.prefs))
            fraction = min(1, (now - self.demo_start) / .24)
            if fraction < 1:
                card = self.demo_card.copy()
                card.putalpha(card.getchannel("A").point(lambda value: round(value * fraction)))
                self.preview_notice.paste(card)
            else:
                self.preview_notice.paste(self.demo_card)

    def alert(self, state, window):
        if self.issue != state:
            self.issue = state
            self.issue_since = time.monotonic()
        if state == "lost" and time.monotonic() - self.issue_since < 1.25:
            return
        self.lost_signal = True
        self.chest_band = None
        if self.prefs["notices"] and self.prefs["errors"]:
            self.show_notice(state, window)

    def show_notice(self, state, window, timed=False):
        key = (state, window)
        if self.notice_key == key:
            return
        x, y, width, height = window
        notice_width, notice_height = NOTICE_SIZE
        region = self.options["region"] or next(defaults(window))
        bounds = crop_rect(window, region)
        left, top = bounds["left"] - notice_width - 14, max(y + 12, bounds["top"])
        if left < x + 8:
            left = max(x, x + width - notice_width - 8)
            top = bounds["top"] + bounds["height"] + 10
        if self.prefs["position"] == "Слева по центру":
            left, top = x + 22, y + (height - notice_height) // 2
        title, hint = ISSUES[state]
        now = time.monotonic()
        self.notice_motion = (notice_card(title, hint, state), left, top, now, True)
        self.notice_key = key
        self.notice_until = now + self.prefs["duration"] if timed else None

    def reset_opened(self):
        self.options["opened"] = ()
        self.marked_chests.clear()
        self.pending_open = None
        self.chest_band = None
        self.status.set("Открытые сундуки сброшены")

    def mark_opened(self, window):
        if not self.mark_bounds or not self.targets or self.closest_distance > 9:
            self.show_feedback(window, "Подойди ближе к сундуку", "F8 скрывает ближайшую точку в радиусе 9 м")
            return
        cx = self.mark_bounds["width"] / 2
        cy = self.mark_bounds["height"] / 2
        closest = min(self.targets, key=lambda key:
                      (self.targets[key][0]-cx)**2 + (self.targets[key][1]-cy)**2)
        self.hide_opened(window, closest)
        self.show_feedback(window, "Точка скрыта", "Сбросить отметки можно в меню радара в трее")

    def hide_opened(self, window, point):
        self.options["opened"] = (*self.options["opened"], point)
        self.options["suppress_live"] = (window, point)
        self.options["live_chest"] = None
        self.marked_chests.discard(point)
        self.slow_marker = None
        self.clear_memory()
        self.clear_box()
        self.closest_distance = float("inf")
        self.targets.pop(point, None)
        self.points.pop(point, None)
        self.clear_notice()

    def observe_visual(self, window, now):
        live = self.options["live_chest"]
        if not live or live[0] != window or live[1] <= self.handled_visual or now-live[1] > .5:
            return False
        captured, state, lid = live[1:]
        link = self.options["map_link"]
        point = link[2] if link and link[0] == window and abs(captured-link[1]) <= .65 else None
        slow = self.slow_marker
        if state == "open" and slow and slow[0] == window and abs(captured-slow[1]) <= .4:
            matches = [point for point, quad in slow[2][1].items()
                       if point is not None and same_chest(quad, lid)]
            if len(matches) > 1:
                return False
            if matches:
                point = matches[0]
                self.marked_chests.add(point)
        if point is None:
            return False
        self.handled_visual = captured
        if point in self.options["opened"]:
            return False
        if state == "closed":
            self.marked_chests.add(point)
            self.pending_open = None
            return False
        if point not in self.marked_chests:
            return False
        previous = self.pending_open
        self.pending_open = (point, captured)
        if previous and previous[0] == point and captured-previous[1] <= .7:
            self.pending_open = None
            self.hide_opened(window, point)
            return True
        return False

    def show_feedback(self, window, title, hint):
        if not self.prefs["notices"]:
            return
        x, y, width, height = window
        notice_width, notice_height = NOTICE_SIZE
        left = max(x, x + width - notice_width - 18)
        top = y + height // 2 - notice_height // 2
        now = time.monotonic()
        self.notice_motion = (notice_card(title, hint, "restored"), left, top, now, True)
        self.notice_key = ("mark", window)
        self.notice_until = now + 2.5

    def observe_chest(self, window, present, area=None, point_id=None):
        now = time.monotonic()
        gap = now - self.last_cue
        if gap > .85:
            self.cue_hits = 0
            self.cue_candidate = None
            self.cue_point_hits = 0
        if gap > 1.5:
            self.cue_saved = False
            self.cue_uploaded = False
            self.cue_point = None
        if not present:
            return
        self.last_cue = now
        self.cue_hits += 1
        if self.cue_hits < 2:
            self.cue_candidate = point_id
            self.cue_point_hits = 1 if point_id is not None else 0
            return
        if point_id != self.cue_candidate:
            self.cue_candidate = point_id
            self.cue_point_hits = 1 if point_id is not None else 0
        elif point_id is not None:
            self.cue_point_hits += 1
        if point_id is not None and self.cue_point_hits >= 2 and point_id != self.cue_point:
            if self.cue_point is not None:
                # The cue can stay on screen between two nearby chests.
                self.cue_saved = False
                self.cue_uploaded = False
            self.cue_point = point_id
            if now - self.recent_sightings.get(point_id, float("-inf")) < 12:
                self.cue_saved = True
                self.cue_uploaded = True
            elif self.cue_saved:
                self.recent_sightings[point_id] = now
        confirmed = self.cue_point if self.cue_point == point_id else None
        if not self.cue_saved and self.cue_point is not None and confirmed is None:
            return
        if not self.cue_saved:
            item = {"seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "map_version": "43.10", "player_area_on_map": area, "point_id": confirmed}
            try:
                with OBSERVATIONS.open("a", encoding="utf-8") as history:
                    history.write(json.dumps(item) + "\n")
            except OSError:
                self.show_feedback(window, "Не удалось сохранить", "Проверь доступ к папке радара")
                self.cue_saved = True
                return
            self.cue_saved = True
            if confirmed is not None:
                self.recent_sightings[confirmed] = now
            self.show_feedback(window, "Сундук замечен", "Наблюдение добавлено в список")
        if confirmed is not None and not self.cue_uploaded and self.community.enabled:
            self.community.submit(confirmed)
            self.cue_uploaded = True

    def show_observations(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Замеченные сундуки")
        dialog.configure(bg=BG)
        table = tk.Listbox(dialog, width=68, height=12, bg=BG, fg=FG,
                           font=(FONT, 11), borderwidth=0)
        table.pack(fill="both", expand=True, padx=15, pady=15)
        observation_areas = []
        share_status = tk.StringVar(value="Выбранное наблюдение откроется как публичный черновик на GitHub.")

        def share():
            selected = table.curselection()
            if not selected or selected[0] >= len(observation_areas):
                share_status.set("Выбери наблюдение из списка.")
                return
            try:
                x, y = map(round, observation_areas[selected[0]])
            except (ValueError, TypeError, OverflowError):
                share_status.set("У наблюдения нет координат района.")
                return
            if not (0 <= x < 2048 and 0 <= y < 2048):
                share_status.set("Координаты вне карты.")
                return
            query = urlencode({
                "title": "Наблюдение сундука",
                "body": f"Примерный район игрока при появлении значка сундука.\n\nRADAR-43.10: area {x},{y}",
            })
            if not webbrowser.open_new_tab(f"{REPORT_URL}?{query}"):
                share_status.set("Не удалось открыть GitHub в браузере.")

        tk.Label(dialog, textvariable=share_status, bg=BG, fg=FG, font=(FONT, 10)).pack(padx=15)
        tk.Button(dialog, text="Открыть черновик на GitHub", command=share, bg=CYAN, fg=BG,
                  font=(FONT, 10, "bold"), padx=10, pady=4).pack(pady=(8, 14))
        try:
            with OBSERVATIONS.open(encoding="utf-8") as history:
                from collections import deque
                rows = deque(history, maxlen=150)
        except FileNotFoundError:
            rows = ()
        except OSError:
            table.insert("end", "Не удалось открыть список наблюдений.")
            rows = None
        if rows is not None:
            for line in reversed(rows):
                try:
                    row = json.loads(line)
                    stamp = datetime.fromisoformat(row["seen_at"]).astimezone()
                    area = row["player_area_on_map"]
                    label = f"Район игрока на карте: {area[0]:.0f}, {area[1]:.0f}" if area else "Район не определён"
                    table.insert("end", f"{stamp:%d.%m %H:%M:%S}   {label}")
                    observation_areas.append(area)
                except (ValueError, TypeError, KeyError, IndexError):
                    continue
            if not table.size():
                table.insert("end", "Пока нет наблюдений.")
        dialog.lift()

    def tracker_game_window(self):
        return None if self.options["paused"] else fortnite_window()

    def show_tracker(self):
        if self.tracker_window is not None and self.tracker_window.winfo_exists():
            self.tracker_window.deiconify()
            self.tracker_window.lift()
            return
        dialog = self.tracker_window = tk.Toplevel(self.root)
        dialog.title("Трекер находок")
        dialog.configure(bg=BG, padx=16, pady=14)
        dialog.minsize(520, 290)
        stats = tk.Frame(dialog, bg=BG)
        stats.pack(fill="x", pady=(0, 12))
        self.tracker_counts = {}
        for column, (kind, heading, color) in enumerate(
            (("chest", "Открыто сундуков", CYAN), ("find", "Найдено находок", GOLD))
        ):
            stats.columnconfigure(column, weight=1)
            tile = tk.Frame(stats, bg="#211633", padx=14, pady=8)
            tile.grid(row=0, column=column, sticky="ew", padx=(0, 4) if column == 0 else (4, 0))
            self.tracker_counts[kind] = tk.StringVar(master=dialog)
            tk.Label(tile, textvariable=self.tracker_counts[kind], bg="#211633", fg=color,
                     font=(FONT, 24, "bold")).pack(anchor="w")
            tk.Label(tile, text=heading, bg="#211633", fg=FG, font=(FONT, 10)).pack(anchor="w")
        journal = tk.Frame(dialog, bg=BG)
        journal.pack(fill="both", expand=True)
        scroll = tk.Scrollbar(journal, orient="vertical")
        self.tracker_rows = tk.Listbox(journal, height=9, bg="#211633", fg=FG, relief="flat",
                                      highlightthickness=0, font=(FONT, 11), yscrollcommand=scroll.set)
        scroll.configure(command=self.tracker_rows.yview)
        scroll.pack(side="right", fill="y")
        self.tracker_rows.pack(side="left", fill="both", expand=True)
        self.tracker_hint = tk.StringVar(master=dialog)
        tk.Label(dialog, textvariable=self.tracker_hint, bg=BG, fg=CYAN, font=(FONT, 9),
                 anchor="w").pack(fill="x", pady=(8, 0))
        self.refresh_tracker()
        dialog.lift()

    def refresh_tracker(self):
        if self.tracker_window is None or not self.tracker_window.winfo_exists():
            return
        for kind, variable in self.tracker_counts.items():
            variable.set(str(self.tracker.totals[kind]))
        self.tracker_rows.delete(0, "end")
        for label, stamp in reversed(self.tracker.rows):
            self.tracker_rows.insert("end", f"  {stamp.astimezone():%d.%m %H:%M:%S}    {label}")
        if not self.tracker.rows:
            self.tracker_rows.insert("end", "  Пока нет распознанных находок.")
        self.tracker_hint.set(self.tracker.error or self.tracker_status)

    def update_tracker(self, window, now):
        updated = self.open_tracker.update(window, self.options["live_chest"], now)
        if updated:
            self.tracker.add("chest", "Сундук открыт")
        while True:
            try:
                kind, label = self.tracker_events.get_nowait()
            except Empty:
                break
            if kind == "find":
                self.tracker.add(kind, label)
            else:
                self.tracker_status = label
            updated = True
        if updated:
            self.refresh_tracker()

    def check_proximity(self, window, distance):
        previous = self.chest_band
        if distance <= 20 or (previous == "close" and distance <= 25):
            band = "close"
        elif distance <= 50 or (previous == "near" and distance <= 57):
            band = "near"
        else:
            band = None
        if band == "near" and previous == "close":
            self.chest_band = band
            return
        if band is None or not self.prefs["notices"] or not self.prefs[band]:
            self.chest_band = band
            return
        if band != previous and time.monotonic() - self.last_chest_alert >= 6:
            self.chest_band = band
            self.last_chest_alert = time.monotonic()
            self.show_notice(band, window, timed=True)

    def update(self):
        while True:
            try:
                action = self.actions.get_nowait()
            except Empty:
                break
            action()
            if self.stop.is_set():
                return
        latest = None
        while True:
            try:
                latest = self.events.get_nowait()
            except Empty:
                break
        if latest and latest[0] == "error":
            self.error = latest[1]
        active = self.options["enabled"] and not self.options["paused"]
        game_window = self.tracker_game_window()
        window = game_window if active else None
        box_data = None
        if window is None:
            self.cue_hits = 0
            self.pending_open = None
            self.hide()
            self.clear_box()
            self.clear_memory()
            self.slow_marker = None
            self.clear_notice()
            self.lost_signal = False
            self.chest_band = None
        if latest and active:
            state, *args = latest
            if state == "located" and args[0] == window:
                if self.lost_signal:
                    self.clear_notice(fade=True)
                    self.lost_signal = False
                    if self.prefs["notices"] and self.prefs["restored"]:
                        self.last_chest_alert = time.monotonic()
                        self.show_notice("restored", window, timed=True)
                else:
                    self.issue = None
                _, bounds, dots, north, distance, cue, area, box, point_id, saved_image = args
                self.status.set(f"Точек на мини-карте: {len(dots)}")
                self.closest_distance = distance
                self.render(bounds, {key: point for key, point in dots.items()
                                     if key not in self.options["opened"]}, north)
                self.check_proximity(window, distance)
                self.observe_chest(window, cue, area, point_id)
                if saved_image and saved_image[0] == "saved":
                    self.show_feedback(window, "Внешний вид сохранён",
                                       "Вырез сундука сохранён на этом компьютере")
                    saved_image = ("live", saved_image[1])
                self.slow_marker = (window, time.monotonic(), saved_image) if saved_image else None
                box_data = (cue, box)
            elif state in ("lost", "capture") and args[0] == window:
                self.hide()
                self.status.set(ISSUES[state][0])
                self.alert(state, window)
                self.slow_marker = None
                if state == "lost":
                    self.observe_chest(window, args[1])
                    box_data = (args[1], args[2])
                else:
                    self.clear_box()
            elif state in ("ready", "idle") and window is None:
                self.status.set("Переключись в матч Fortnite")
        if active and self.error:
            self.status.set(self.error)
            if window:
                self.alert("error", window)
        now = time.monotonic()
        self.update_tracker(game_window, now)
        pressed = bool(USER.GetAsyncKeyState(0x77) & 0x8000)  # F8
        if pressed and not self.mark_key_down and window:
            self.mark_opened(window)
        self.mark_key_down = pressed
        if window is not None and self.prefs["memory"] and self.observe_visual(window, now):
            box_data = None
        direct = window is not None and self.show_latest_chest(window, now)
        if direct:
            self.clear_box()
        elif box_data is not None:
            self.show_detected_chest(window, *box_data)
        self.animate(now)
        self.root.after(33, self.update)

    def reset(self):
        try:
            CONFIG.unlink(missing_ok=True)
        except OSError:
            self.status.set("Не удалось сбросить сохранённые границы мини-карты.")
            return
        self.options["region"] = None
        self.status.set("Автопоиск включён. Переключись в Fortnite.")

    def calibrate(self):
        if self.options["paused"]:
            return
        self.options["paused"] = True
        self.hide()
        self.clear_notice()
        self.status.set("Вернись в Fortnite. Снимок через 5 секунд.")
        self.root.after(5000, self.select_region)

    def select_region(self):
        window = fortnite_window()
        if window is None:
            self.finish_calibration("Fortnite не был на переднем плане.")
            return
        try:
            with MSS() as capture:
                frame = np.asarray(capture.grab(crop_rect(window, (0, 0, 1, 1))))
        except ScreenShotError:
            self.finish_calibration("Не удалось снять экран Fortnite.")
            return
        scale = min(1, (self.root.winfo_screenwidth() - 120) / frame.shape[1],
                    (self.root.winfo_screenheight() - 170) / frame.shape[0])
        preview = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR), None,
                             fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        _, png = cv2.imencode(".png", preview)
        picture = tk.PhotoImage(data=base64.b64encode(png))
        dialog = tk.Toplevel(self.root)
        dialog.title("Выделение мини-карты")
        dialog.attributes("-topmost", True)
        dialog.configure(bg=BG)
        tk.Label(dialog, text="Обведи мини-карту без счётчиков под ней",
                 bg=BG, fg=FG, font=(FONT, 12, "bold"), padx=16, pady=10).pack()
        canvas = tk.Canvas(dialog, width=picture.width(), height=picture.height(),
                           cursor="crosshair", highlightthickness=0)
        canvas.pack()
        canvas.create_image(0, 0, image=picture, anchor="nw")
        canvas.image = picture
        selection = canvas.create_rectangle(0, 0, 0, 0, outline=GOLD, width=3)
        start = [0, 0]

        def down(event):
            start[:] = [event.x, event.y]

        def drag(event):
            canvas.coords(selection, *start, event.x, event.y)

        def up(event):
            left, right = sorted([start[0], event.x])
            top, bottom = sorted([start[1], event.y])
            width, height = picture.width(), picture.height()
            region = (max(0, left) / width, max(0, top) / height,
                      min(width, right) / width, min(height, bottom) / height)
            if (region[2] - region[0]) * window[2] < 65:
                return
            if (region[3] - region[1]) * window[3] < 65:
                return
            try:
                CONFIG.write_text(json.dumps(region), encoding="utf-8")
            except OSError:
                dialog.destroy()
                self.finish_calibration("Не удалось сохранить границы мини-карты.")
                return
            self.options["region"] = region
            dialog.destroy()
            self.finish_calibration("Мини-карта сохранена. Вернись в Fortnite.")

        def cancel():
            dialog.destroy()
            self.finish_calibration("Выделение отменено.")

        canvas.bind("<Button-1>", down)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", up)
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Escape>", lambda event: cancel())

    def finish_calibration(self, message):
        self.status.set(message)
        self.options["paused"] = False


if __name__ == "__main__":
    USER.SetProcessDpiAwarenessContext(ct.c_void_p(-4))
    cv2.setNumThreads(2)
    root = tk.Tk()
    Radar(root)
    root.mainloop()
