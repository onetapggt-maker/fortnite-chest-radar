"""Detect Fortnite's visible chest sound cue in a captured game frame."""

from functools import lru_cache

import cv2
import numpy as np


@lru_cache(maxsize=1)
def cue_templates():
    shape = np.zeros((22, 26), np.uint8)
    cv2.rectangle(shape, (4, 4), (21, 13), 255, -1)
    shape[4, [4, 21]] = 0
    shape[5:10, 12:14] = 0
    cv2.rectangle(shape, (5, 15), (20, 18), 255, -1)
    shape = cv2.GaussianBlur(shape, (3, 3), .65)
    return tuple(cv2.resize(shape, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
                 for scale in (.70, .82, .94, 1.06, 1.18, 1.31))


def cue_lighting(patch, light, glyph, score):
    inside = glyph > 195
    outside = glyph < 20
    brightness = light[inside].mean()
    contrast = brightness - light[outside].mean()
    blue, green, red = np.moveaxis(patch.astype(np.int16), -1, 0)
    red_blue = red - blue
    green_blue = green - blue
    warm = (red_blue > 37) & (green_blue > 17) & (red > 84)
    if brightness > 142 and contrast > 33 and warm[outside].mean() > .27:
        return True

    # The icon's light fill is a local white reference. Subtracting its
    # color cast keeps the gold test usable under a purple screen tint.
    blue_cast = -float(np.median(green_blue[inside]))
    if score < .855 or blue_cast < 12 or brightness < 93 or contrast < 17:
        return False
    relative_gold = ((red_blue - np.median(red_blue[inside]) > 19) &
                     (green_blue + blue_cast > 9))
    return relative_gold[outside].mean() > .24


def chest_cue(frame):
    height, width = frame.shape[:2]
    if height < 100 or width < 100:
        return False
    factor = 720 / height
    resized = cv2.resize(frame, (round(width * factor), 720), interpolation=cv2.INTER_AREA)
    center = resized.shape[1] // 2
    left = max(0, center-250)
    band = resized[125:590, left:center+250, :3]
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    if gray.shape[1] < 36:
        return False
    for glyph in cue_templates():
        scores = cv2.matchTemplate(gray, glyph, cv2.TM_CCOEFF_NORMED)
        tw, th = glyph.shape[1], glyph.shape[0]
        yy, xx = np.ogrid[:scores.shape[0], :scores.shape[1]]
        radius = (xx + tw/2 + left - center)**2 + (yy + th/2 + 125 - 360)**2
        scores[(radius < 147**2) | (radius > 183**2)] = -1
        for _ in range(8):
            _, score, _, (x, y) = cv2.minMaxLoc(scores)
            if score < .84:
                break
            patch = band[y:y+th, x:x+tw]
            light = gray[y:y+th, x:x+tw]
            if cue_lighting(patch, light, glyph, score):
                return True
            scores[max(0,y-5):y+6, max(0,x-5):x+6] = -1
    return False


def glowing_container(frame):
    """Find a box-shaped object with a localized golden glow near the reticle."""
    height, width = frame.shape[:2]
    factor = min(1, 540 / height)
    x0, y0 = round(width * .24), round(height * .25)
    region = frame[y0:round(height * .77), x0:round(width * .76), :3]
    region = cv2.resize(region, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(hsv, (18, 95, 175), (37, 255, 255)) > 0
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    blue, green, red = np.moveaxis(region.astype(np.int16), -1, 0)
    red_blue = red - blue
    green_blue = green - blue
    storm = np.mean(green_blue < -14) > .25
    if not gold.any() and not storm:
        return None
    edges = cv2.Canny(gray, 30, 73) if storm else cv2.Canny(gray, 45, 110)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    best_score = 0
    rh, rw = gray.shape
    aim = (width / 2 - x0) * factor, (height / 2 - y0) * factor
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if not (25 <= w <= 170 and 21 <= h <= 135 and .55 <= w / h <= 2.5):
            continue
        if cv2.contourArea(contour) < .56 * w * h:
            continue
        polygon = cv2.approxPolyDP(contour, .035 * cv2.arcLength(contour, True), True)
        if len(polygon) not in (4, 5) or not cv2.isContourConvex(polygon):
            continue
        pad = max(4, round(min(w, h) * .22))
        if x < pad or y < pad or x+w+pad > rw or y+h+pad > rh:
            continue
        rim = gold[y-pad:y+h+pad, x-pad:x+w+pad]
        inside = gold[y:y+h, x:x+w].mean()
        band = ((rim.sum() - gold[y:y+h, x:x+w].sum()) / (rim.size - w*h))
        if storm and band < .10:
            # Compare the perimeter to the center of the crate. A screen-wide
            # tint lifts both equally, while a glow still lifts green and red.
            border = np.ones(rim.shape, dtype=bool)
            border[pad:pad+h, pad:pad+w] = False
            outer = np.s_[y-pad:y+h+pad, x-pad:x+w+pad]
            core = np.s_[y+h//4:y+3*h//4, x+w//4:x+3*w//4]
            warm_halo = ((red_blue[outer] - np.median(red_blue[core]) > 18) &
                         (green_blue[outer] - np.median(green_blue[core]) > 13) &
                         (gray[outer] - np.median(gray[core]) > 18))
            band = warm_halo[border].mean()
        # Wood may be warm throughout. Look for brighter gold around its outline.
        if band < .10 or band < inside * .7:
            continue
        distance = np.hypot((x+w/2)-aim[0], (y+h/2)-aim[1])
        score = min(band, .65) / (1 + distance / 75)
        if score > best_score:
            best_score = score
            best = (round(x / factor) + x0, round(y / factor) + y0,
                    round(w / factor), round(h / factor))
    return best
