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
            patch = band[y:y+th, x:x+tw].astype(np.int16)
            light = gray[y:y+th, x:x+tw]
            inside = glyph > 195
            outside = glyph < 20
            blue, green, red = np.moveaxis(patch, -1, 0)
            warm = (red - blue > 37) & (green - blue > 17) & (red > 84)
            if (light[inside].mean() > 142 and
                    light[inside].mean() - light[outside].mean() > 33 and
                    warm[outside].mean() > .27):
                return True
            scores[max(0,y-5):y+6, max(0,x-5):x+6] = -1
    return False
