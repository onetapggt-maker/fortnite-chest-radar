"""Remember a visible chest and reproject its picture using nearby scene features."""

from pathlib import Path

import cv2
import numpy as np


class ChestMemory:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.references = []
        self.entries = {}
        self.sift = cv2.SIFT_create(nfeatures=1250, contrastThreshold=.018)
        self.matcher = cv2.BFMatcher(cv2.NORM_L2)
        for path in sorted(self.folder.glob("reference*.png")):
            try:
                features = self.features(cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_GRAYSCALE))
                if features is not None:
                    self.references.append(features)
            except (OSError, cv2.error):
                continue

    def features(self, gray, mask=None):
        if gray is None:
            return None
        keypoints, descriptors = self.sift.detectAndCompute(gray, mask)
        if descriptors is None:
            return None
        return np.float32([point.pt for point in keypoints]), descriptors, gray.shape

    def project(self, reference, current, rectangle, minimum, anchored=False):
        source, source_descriptors, _ = reference
        target, target_descriptors, shape = current
        if len(target_descriptors) < 2:
            return None
        pairs = [m for pair in self.matcher.knnMatch(source_descriptors, target_descriptors, k=2)
                 if len(pair) == 2 for m, n in [pair] if m.distance < .72 * n.distance]
        if len(pairs) < minimum:
            return None
        start = np.float32([source[p.queryIdx] for p in pairs])
        end = np.float32([target[p.trainIdx] for p in pairs])
        transform, inliers = cv2.findHomography(start, end, cv2.RANSAC, 3)
        if transform is None or inliers is None or inliers.sum() < minimum:
            return None
        x, y, width, height = rectangle
        accepted = inliers.ravel().astype(bool)
        if anchored:
            center = (float(x+width/2), float(y+height/2))
            if cv2.pointPolygonTest(cv2.convexHull(start[accepted]), center, True) < 4:
                return None
        corners = np.float32([[[x, y], [x+width, y], [x+width, y+height], [x, y+height]]])
        quad = cv2.perspectiveTransform(corners, transform)[0]
        if not np.isfinite(quad).all() or not cv2.isContourConvex(quad):
            return None
        area = cv2.contourArea(quad)
        if area < 320 or area > shape[0]*shape[1]*.16:
            return None
        left, top = quad.min(axis=0)
        right, bottom = quad.max(axis=0)
        if left < 1 or top < 1 or right >= shape[1] or bottom >= shape[0]:
            return None
        if area < (right-left) * (bottom-top) * .61:
            return None
        # Require support both horizontally and vertically near the projection.
        matched = end[accepted]
        pad = max(right-left, bottom-top) * 1.75
        local = matched[np.all((matched >= [left-pad, top-pad]) &
                               (matched <= [right+pad, bottom+pad]), axis=1)]
        if len(local) < minimum or np.any(np.ptp(local, axis=0) < [13, 13]):
            return None
        return quad

    def project_scene(self, scene, current, rectangle):
        source, before, _ = scene
        # Check scene support on every side of the saved chest. A homography
        # fitted only to the roof or a side wall drifts off the chest.
        x, y, width, height = rectangle
        pad = max(width, height)
        local = np.all((source >= [x-pad, y-pad]) &
                       (source <= [x+width+pad, y+height+pad]), axis=1)
        if local.sum() < 15:
            return None
        return self.project((source[local], before[local], scene[2]), current, rectangle, 15, anchored=True)

    def get(self, point):
        if point not in self.entries:
            self.entries[point] = []
            for suffix in ("", "-2", "-3"):
                try:
                    with np.load(self.folder / f"{point}{suffix}.npz", allow_pickle=False) as data:
                        self.entries[point].append((
                            (data["points"], data["features"], data["shape"]),
                            data["rect"], data["crop"],
                        ))
                except (OSError, ValueError, KeyError):
                    continue
        return self.entries[point]

    def remember(self, point, frame, gray, quad):
        x, y, width, height = cv2.boundingRect(quad)
        if width < 22 or height < 20:
            return False
        mask = np.zeros_like(gray)
        pad = max(width, height) * 2
        mask[max(0, y-pad):y+height+pad, max(0, x-pad):x+width+pad] = 255
        mask[max(0, y-8):y+height+8, max(0, x-8):x+width+8] = 0
        reference = self.features(gray, mask)
        if reference is None or len(reference[0]) < 15:
            return False
        crop = frame[y:y+height, x:x+width, :3].copy()
        rectangle = np.array([x, y, width, height])
        points, descriptors, shape = reference
        views = self.get(point)
        if len(views) >= 3:
            return False
        suffix = f"-{len(views)+1}" if views else ""
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            temporary = self.folder / f"{point}{suffix}.tmp.npz"
            np.savez_compressed(temporary, points=points, features=descriptors, shape=shape,
                                rect=rectangle, crop=crop)
            temporary.replace(self.folder / f"{point}{suffix}.npz")
        except OSError:
            return False
        views.append((reference, rectangle, crop))
        return True

    def update(self, frame, point, nearby, cue, overlay=None):
        known = [(key, view) for key in nearby for view in self.get(key)]
        can_learn = cue and point is not None and self.references and len(self.get(point)) < 3
        if not known and not can_learn:
            return None
        height, width = frame.shape[:2]
        ratio = 540 / height
        resized = cv2.resize(frame, (round(width*ratio), 540), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(resized[:, :, :3], cv2.COLOR_BGR2GRAY)
        mask = np.zeros_like(gray)
        mask[112:465, round(gray.shape[1]*.18):round(gray.shape[1]*.82)] = 255
        if overlay:
            x, y, w, h = (round(value*ratio) for value in overlay)
            mask[max(0,y):max(0,y+h), max(0,x):max(0,x+w)] = 0
        current = self.features(gray, mask)
        if current is None:
            return None
        # The global cue says a chest is nearby, but does not prove it is visible.
        for key, (scene, rectangle, crop) in known:
            quad = self.project_scene(scene, current, rectangle)
            if quad is None:
                continue
            return ("image", key, quad / ratio, crop)
        if can_learn:
            for reference in self.references:
                rh, rw = reference[2]
                visible = self.project(reference, current, (0, 0, rw, rh), 9)
                if visible is not None and self.remember(point, resized, gray, visible):
                    return ("saved", point)
        return None


def warp_snapshot(crop, quad):
    x, y, width, height = cv2.boundingRect(quad)
    ch, cw = crop.shape[:2]
    source = np.float32([[0, 0], [cw-1, 0], [cw-1, ch-1], [0, ch-1]])
    transform = cv2.getPerspectiveTransform(source, quad - np.float32([x, y]))
    rgba = cv2.cvtColor(crop, cv2.COLOR_BGR2RGBA)
    fade = np.zeros((ch, cw), np.uint8)
    fade[6:-6, 6:-6] = 178
    rgba[:, :, 3] = cv2.GaussianBlur(fade, (11, 11), 0)
    projected = cv2.warpPerspective(rgba, transform, (width, height))
    return (x, y, width, height), projected
