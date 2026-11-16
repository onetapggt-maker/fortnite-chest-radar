import ctypes as ct
from ctypes import wintypes as wt
from functools import lru_cache
import math
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


USER = ct.WinDLL("user32", use_last_error=True)
GDI = ct.WinDLL("gdi32", use_last_error=True)
ALPHA = 2
NOTICE_SIZE = (384, 90)
GLOW_RADIUS = 9
THEMES = {
    "Неон": ("#e56bff", "#615dff", "#f4deff"),
    "Кибер": ("#35e4ff", "#167fbd", "#d2fbff"),
    "Огонь": ("#ffad36", "#dc4d27", "#fff0cf"),
    "Минимал": ("#dce3f5", "#69778e", "#ffffff"),
}
NOTICE_COLORS = {"near": "#c079ff", "close": "#ffcb55", "lost": "#ff587c",
                 "capture": "#ff587c", "error": "#ff587c", "restored": "#56f5b0"}


class BitmapHeader(ct.Structure):
    _fields_ = [
        ("size", wt.DWORD), ("width", wt.LONG), ("height", wt.LONG),
        ("planes", wt.WORD), ("bits", wt.WORD), ("compression", wt.DWORD),
        ("image_size", wt.DWORD), ("x_resolution", wt.LONG), ("y_resolution", wt.LONG),
        ("colors_used", wt.DWORD), ("colors_important", wt.DWORD),
    ]


class Blend(ct.Structure):
    _fields_ = [("op", wt.BYTE), ("flags", wt.BYTE), ("opacity", wt.BYTE),
                ("alpha_format", wt.BYTE)]


USER.CreateWindowExW.argtypes = (
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ct.c_int, ct.c_int, ct.c_int, ct.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
)
USER.CreateWindowExW.restype = wt.HWND
USER.ShowWindow.argtypes = (wt.HWND, ct.c_int)
USER.SetWindowPos.argtypes = (
    wt.HWND, wt.HWND, ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.UINT,
)
USER.DestroyWindow.argtypes = (wt.HWND,)
USER.UpdateLayeredWindow.argtypes = (
    wt.HWND, wt.HDC, ct.POINTER(wt.POINT), ct.POINTER(wt.SIZE), wt.HDC,
    ct.POINTER(wt.POINT), wt.DWORD, ct.POINTER(Blend), wt.DWORD,
)
GDI.CreateCompatibleDC.argtypes = (wt.HDC,)
GDI.CreateCompatibleDC.restype = wt.HDC
GDI.CreateDIBSection.argtypes = (
    wt.HDC, ct.POINTER(BitmapHeader), wt.UINT, ct.POINTER(ct.c_void_p), wt.HANDLE, wt.DWORD,
)
GDI.CreateDIBSection.restype = wt.HBITMAP
GDI.SelectObject.argtypes = (wt.HDC, wt.HANDLE)
GDI.SelectObject.restype = wt.HANDLE
GDI.DeleteObject.argtypes = (wt.HANDLE,)
GDI.DeleteDC.argtypes = (wt.HDC,)


class Layer:
    """A per-pixel transparent Windows overlay that never takes game input."""

    def __init__(self):
        # No activate, layered, tool window, click-through, always on top.
        self.hwnd = USER.CreateWindowExW(
            0x080800A8, "STATIC", None, 0x80000000, 0, 0, 0, 0,
            None, None, None, None,
        )
        if not self.hwnd:
            raise ct.WinError(ct.get_last_error())
        self.visible = False

    def draw(self, image, x, y, opacity=255):
        width, height = image.size
        header = BitmapHeader(ct.sizeof(BitmapHeader), width, -height, 1, 32)
        bits = ct.c_void_p()
        dc = GDI.CreateCompatibleDC(None)
        if not dc:
            raise ct.WinError(ct.get_last_error())
        bitmap = GDI.CreateDIBSection(dc, ct.byref(header), 0, ct.byref(bits), None, 0)
        if not bitmap:
            GDI.DeleteDC(dc)
            raise ct.WinError(ct.get_last_error())
        previous = GDI.SelectObject(dc, bitmap)
        try:
            # Windows expects premultiplied BGRA in the bitmap.
            pixels = np.asarray(image.convert("RGBa"))[:, :, [2, 1, 0, 3]].tobytes()
            ct.memmove(bits, pixels, len(pixels))
            position, size, origin = wt.POINT(x, y), wt.SIZE(width, height), wt.POINT()
            blend = Blend(0, 0, opacity, 1)
            if not USER.UpdateLayeredWindow(
                self.hwnd, None, ct.byref(position), ct.byref(size), dc, ct.byref(origin),
                0, ct.byref(blend), ALPHA,
            ):
                raise ct.WinError(ct.get_last_error())
        finally:
            GDI.SelectObject(dc, previous)
            GDI.DeleteObject(bitmap)
            GDI.DeleteDC(dc)
        if not self.visible:
            # Put the overlay above a borderless game's topmost window without taking focus.
            USER.SetWindowPos(self.hwnd, wt.HWND(-1), 0, 0, 0, 0, 0x0053)
            self.visible = True

    def hide(self):
        if self.visible:
            USER.ShowWindow(self.hwnd, 0)
            self.visible = False

    def close(self):
        USER.DestroyWindow(self.hwnd)


def chest_symbol(draw, x, y, size, color, line):
    draw.rounded_rectangle((x-size, y-size*.45, x+size, y+size*.75),
                           radius=size*.22, outline=color, width=line)
    draw.line((x-size, y, x+size, y), fill=color, width=line)
    draw.rounded_rectangle((x-size*.19, y-size*.12, x+size*.19, y+size*.33),
                           radius=line, fill=color)
    if size >= 28:
        for side in (-1, 1):
            rail = x + size*.62*side
            draw.line((rail, y-size*.35, rail, y+size*.65), fill=color, width=line)


@lru_cache(maxsize=32)
def container_marker(width, height):
    pad = 12
    label = "Возможный сундук"
    face = font(12, True)
    label_width = round(face.getlength(label)) + 34
    image = Image.new("RGBA", (max(width, label_width) + 2*pad, height + 50))
    draw = ImageDraw.Draw(image)
    length = max(6, min(20, width//5, height//5))
    for x, dx in ((pad, 1), (pad+width, -1)):
        for y, dy in ((pad, 1), (pad+height, -1)):
            draw.line((x, y+dy*length, x, y, x+dx*length, y), fill="#b3f5ff", width=2)
    top = height + pad + 7
    draw.rounded_rectangle((pad, top, pad+label_width, top+24), radius=7,
                           fill="#20132eee", outline="#b3f5ff")
    chest_symbol(draw, pad+13, top+11, 6, "#ffd476", 1)
    draw.text((pad+24, top+12), label, font=face, fill="#f8eeff", anchor="lm")
    return image


@lru_cache(maxsize=80)
def marker_sprite(phase, size=5, icon=True):
    size = GLOW_RADIUS * 2 + 1
    scale = 4
    center = (GLOW_RADIUS + 0.5) * scale
    halo = Image.new("RGBA", (size * scale, size * scale), (255, 167, 41, 0))
    light = Image.new("L", halo.size)
    radius = (size * .74 + 1.05 * phase / 23) * scale
    if phase >= 0:
        ImageDraw.Draw(light).ellipse(
            (center-radius, center-radius, center+radius, center+radius),
            fill=145 + phase * 3,
        )
    halo.putalpha(light.filter(ImageFilter.GaussianBlur(1.55 * scale)))
    draw = ImageDraw.Draw(halo)
    if icon:
        chest_symbol(draw, center, center, size * scale, "#ffe17e", max(4, scale))
    else:
        draw.ellipse((center-11, center-11, center+11, center+11),
                     fill="#ffe69b", outline="#804918", width=3)
    return halo.resize((size, size), Image.Resampling.LANCZOS)


def glowing_markers(width, height, points, t, size=5, glow=True, icon=True):
    image = Image.new("RGBA", (width, height))
    groups = []
    for key, (x, y) in points.items():
        for group in groups:
            if (x-group[1])**2 + (y-group[2])**2 < 12**2:
                group[3] += 1
                break
        else:
            groups.append([key, x, y, 1])
    for key, x, y, count in groups:
        pulse = (1 + math.sin(t * 2.35 + key * 0.43)) / 2
        phase = round(23 * pulse) if glow else -1
        image.alpha_composite(marker_sprite(phase, size, icon),
                              (round(x) - GLOW_RADIUS, round(y) - GLOW_RADIUS))
        if count > 1:
            draw = ImageDraw.Draw(image)
            label = str(min(count, 99))
            half = 4 if count < 10 else 6
            bx, by = min(width-half-1, round(x)+6), max(5, round(y)-5)
            draw.rounded_rectangle((bx-half, by-4, bx+half, by+4), radius=3,
                                   fill="#362137", outline="#c88942")
            draw.text((bx, by), label, font=font(7, True), fill="#fff0bc", anchor="mm")
    return image


@lru_cache
def font(size, bold=False):
    path = Path(os.environ["WINDIR"]) / "Fonts" / ("segoeuib.ttf" if bold else "segoeui.ttf")
    return ImageFont.truetype(str(path), size)


def notice_card(title, hint, kind="lost"):
    width, height = NOTICE_SIZE
    scale = 3
    image = Image.new("RGBA", (width * scale, height * scale))
    draw = ImageDraw.Draw(image)
    accent = NOTICE_COLORS[kind]
    draw.rounded_rectangle((8*scale, 9*scale, (width-9)*scale, (height-13)*scale),
                           radius=14*scale, outline=accent, width=4*scale)
    image = image.filter(ImageFilter.GaussianBlur(5 * scale))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((6*scale, 6*scale, (width-7)*scale, (height-10)*scale),
                           radius=13*scale, fill="#151127", outline=accent, width=scale)
    draw.rounded_rectangle((17*scale, 17*scale, 61*scale, 66*scale),
                           radius=11*scale, fill="#241b3b")
    if kind in ("near", "close"):
        chest_symbol(draw, 39*scale, 41*scale, 13*scale, "#ffd16c", 2*scale)
    else:
        draw.ellipse((26*scale, 28*scale, 52*scale, 54*scale), outline=accent, width=2*scale)
        if kind == "restored":
            draw.line((32*scale, 41*scale, 37*scale, 46*scale, 46*scale, 36*scale),
                      fill=accent, width=2*scale, joint="curve")
        else:
            draw.text((39*scale, 40*scale), "!", font=font(17*scale, True), fill=accent, anchor="mm")
    draw.text((74*scale, 29*scale), title, font=font(17*scale, True), fill="#f9efff", anchor="lm")
    draw.text((74*scale, 51*scale), hint, font=font(12*scale), fill="#c9b9de", anchor="lm")
    return image.resize(NOTICE_SIZE, Image.Resampling.LANCZOS)


@lru_cache(maxsize=20)
def compass_frame(width, height, theme, north, size):
    scale = 2
    cx, cy = width/2, height/2
    edge = min(width, height)/2
    radius = min(edge-8, max(12, edge-10) * size)
    accent, secondary, bright = THEMES[theme]
    bloom = Image.new("L", (width*scale, height*scale))
    glow_color = tuple(bytes.fromhex(accent[1:]))
    ring = Image.new("RGBA", bloom.size, (*glow_color, 0))
    box = tuple(v*scale for v in (cx-radius, cy-radius, cx+radius, cy+radius))
    light = ImageDraw.Draw(bloom)
    light.ellipse(box, outline=142, width=3*scale)
    for arc in range(4):
        start = arc*90 + 12
        light.arc(box, start, start+54, fill=255, width=5*scale)
    if theme != "Минимал":
        ring.putalpha(bloom.filter(ImageFilter.GaussianBlur(3*scale)))
    draw = ImageDraw.Draw(ring)
    draw.ellipse(box, outline=secondary, width=2*scale)
    for arc in range(4):
        start = arc*90 + 12
        draw.arc(box, start, start+54, fill=accent, width=2*scale)
        draw.arc(box, start+7, start+27, fill=bright, width=scale)
    for tick in range(36):
        angle = math.radians(tick*10 + north)
        inner = radius - (7 if tick % 3 == 0 else 4)
        x, y = math.cos(angle), math.sin(angle)
        draw.line(((cx+inner*x)*scale, (cy+inner*y)*scale,
                   (cx+(radius-2)*x)*scale, (cy+(radius-2)*y)*scale),
                  fill=accent, width=scale)
    for index, text in enumerate("NESW"):
        angle = math.radians(north + index*90)
        x = (cx + radius*math.cos(angle))*scale
        y = (cy + radius*math.sin(angle))*scale
        draw.rounded_rectangle((x-7*scale, y-7*scale, x+7*scale, y+7*scale),
                               radius=4*scale, fill="#201633")
        draw.text((x, y-.5*scale), text, font=font(9*scale, True), fill=bright, anchor="mm")
    return ring.resize((width, height), Image.Resampling.LANCZOS)


def radar_frame(width, height, points, t, options, north=-90, preview=False):
    image = Image.new("RGBA", (width, height))
    theme = options["theme"]
    accent = THEMES[theme][0]
    color = tuple(bytes.fromhex(accent[1:]))
    speed = options["speed"] / 100
    angle = (t * 85 * speed) % 360
    edge = min(width, height)/2
    radius = min(edge-8, max(12, edge-10) * options["size"] / 100)
    cx, cy = width/2, height/2
    if options["scan"]:
        draw = ImageDraw.Draw(image)
        box = (cx-radius, cy-radius, cx+radius, cy+radius)
        for segment in range(16):
            end = angle - segment*3
            alpha = (56 if preview else 26) * (16-segment) // 16
            draw.pieslice(box, end-3, end, fill=(*color, alpha))
        end = math.radians(angle)
        draw.line((cx, cy, cx+radius*math.cos(end), cy+radius*math.sin(end)),
                  fill=(*color, 190 if preview else 65))
    ring = compass_frame(width, height, theme, round(north / 5) * 5, options["size"] / 100)
    if options["pulse"]:
        amount = .83 + .17 * math.sin(t * 2.1 * speed)
        ring = ring.copy()
        ring.putalpha(ring.getchannel("A").point(lambda value: round(value*amount)))
    image.alpha_composite(ring)
    image.alpha_composite(glowing_markers(width, height, points, t * speed,
                                          7 if preview else 5, options["glow"], options["icons"]))
    if preview:
        ImageDraw.Draw(image).polygon((cx, cy-9, cx-7, cy+7, cx, cy+4, cx+7, cy+7), fill="#fff5ff")
    return image


def preview_base(path):
    width, height = 414, 286
    y, x = np.mgrid[:height, :width]
    glow = np.exp(-(((x - width/2) / 155) ** 2 + ((y - height/2) / 125) ** 2))
    tint = np.array([17, 13, 31]) + glow[:, :, None] * np.array([34, 20, 61])
    image = Image.fromarray(tint.astype(np.uint8)).convert("RGBA")
    draw = ImageDraw.Draw(image)
    for step in range(12, width, 18):
        draw.line((step, 0, step, height), fill="#292044")
    for step in range(11, height, 18):
        draw.line((0, step, width, step), fill="#292044")
    draw.rectangle((0, 0, width-1, height-1), outline="#483168")
    try:
        with Image.open(path) as island:
            # Use an actual crop of the same island that the overlay recognizes.
            iw, ih = island.size
            tile = island.crop((int(iw*.36), int(ih*.32), int(iw*.53), int(ih*.49)))
            tile = ImageOps.fit(tile.convert("RGB"), (246, 246))
    except OSError:
        return image
    tile = ImageEnhance.Brightness(tile).enhance(.53)
    mask = Image.new("L", tile.size)
    ImageDraw.Draw(mask).ellipse((2, 2, 243, 243), fill=255)
    image.paste(tile, ((width-246)//2, (height-246)//2), mask)
    return image


def preview_frame(base, t, options):
    image = base.copy()
    style = {**options, "size": min(options["size"], 105)}
    points = {0: (84, 89), 1: (185, 128), 2: (154, 181), 3: (79, 184)}
    layer = radar_frame(264, 264, points, t, style, preview=True)
    if options["opacity"] < 100:
        layer.putalpha(layer.getchannel("A").point(lambda value: value * options["opacity"] // 100))
    image.alpha_composite(layer, ((image.width-264)//2, (image.height-264)//2))
    return image
