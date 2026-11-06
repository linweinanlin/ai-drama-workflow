"""口型检测：测出每镜「嘴在动」的时间窗，用于把配音放到正确位置。

优先用 MediaPipe FaceMesh（近似侧脸也能测），没有就退回 OpenCV Haar + 嘴部 ROI 差分。
结果缓存到 07_qc/lipsync/<集数>/<镜号>.json，避免每次重算。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_FPS = 10.0
MIN_WINDOW = 0.35

try:
    import cv2
except Exception:  # noqa: BLE001
    cv2 = None

try:
    import mediapipe as mp
except Exception:  # noqa: BLE001
    mp = None


def _mesh_mar(frame, face_mesh) -> float | None:
    """用 FaceMesh 的关键点算嘴部开合比（内唇上下 / 嘴宽）。"""
    import cv2 as _cv2

    rgb = _cv2.cvtColor(frame, _cv2.COLOR_BGR2RGB)
    result = face_mesh.process(rgb)
    if not result.multi_face_landmarks:
        return None
    landmarks = result.multi_face_landmarks[0].landmark
    height, width = frame.shape[:2]

    def point(index: int) -> tuple[float, float]:
        item = landmarks[index]
        return item.x * width, item.y * height

    top = point(13)
    bottom = point(14)
    left = point(78)
    right = point(308)
    mouth_height = abs(bottom[1] - top[1])
    mouth_width = max(1.0, abs(right[0] - left[0]))
    return float(mouth_height / mouth_width)


def _haar_motion(frames: list[Any]) -> list[float]:
    """兜底：在检测到的嘴部小区域里做帧差，衡量“嘴在动”。"""
    import cv2 as _cv2

    cascade = _cv2.CascadeClassifier(_cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    values: list[float] = []
    previous = None
    for frame in frames:
        gray = _cv2.cvtColor(frame, _cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.15, 5, minSize=(80, 80))
        if len(faces) == 0:
            values.append(0.0)
            previous = None
            continue
        x, y, w, h = max(faces, key=lambda box: box[2] * box[3])
        mouth = gray[y + int(h * 0.62): y + int(h * 0.95), x + int(w * 0.22): x + int(w * 0.78)]
        mouth = _cv2.resize(mouth, (96, 48))
        if previous is None:
            values.append(0.0)
        else:
            values.append(float(np.mean(_cv2.absdiff(mouth, previous))))
        previous = mouth
    return values


def activity(video: Path) -> dict[str, Any]:
    """返回嘴部活动曲线与「在说话」的时间窗。"""
    if cv2 is None:
        return {"ok": False, "reason": "缺少 opencv", "samples": [], "window": None}
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        return {"ok": False, "reason": "无法打开视频", "samples": [], "window": None}
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(fps / SAMPLE_FPS)))
    frames: list[Any] = []
    times: list[float] = []
    index = 0
    series: list[float] = []
    mesh = None
    if mp is not None:
        mesh = mp.solutions.face_mesh.FaceMesh(
            static_image_mode=False, max_num_faces=2, refine_landmarks=True,
            min_detection_confidence=0.4, min_tracking_confidence=0.4)
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if index % step == 0:
                times.append(round(index / fps, 3))
                if mesh is not None:
                    value = _mesh_mar(frame, mesh)
                    series.append(value if value is not None else -1.0)
                else:
                    frames.append(frame)
            index += 1
    finally:
        capture.release()
        if mesh is not None:
            mesh.close()

    method = "facemesh"
    if mesh is None:
        series = _haar_motion(frames)
        method = "haar"

    valid = [value for value in series if value >= 0]
    if len(valid) < 4:
        return {"ok": False, "reason": "没检测到脸（背身/遮挡/远景）", "method": method,
                "samples": [], "window": None}

    baseline = float(np.percentile(valid, 25))
    peak = float(np.percentile(valid, 95))
    span = max(1e-4, peak - baseline)
    threshold = baseline + max(0.012, span * 0.35)
    if method == "haar":
        threshold = baseline + max(0.6, span * 0.45)

    flags = [(time, value >= threshold) for time, value in zip(times, series) if value >= 0]
    runs: list[list[float]] = []
    start = None
    last = None
    for time, active in flags:
        if active and start is None:
            start = time
        if active:
            last = time
        elif start is not None and last is not None and time - last > 0.3:
            runs.append([start, last])
            start = None
    if start is not None and last is not None:
        runs.append([start, last])

    window = None
    if runs:
        best = max(runs, key=lambda item: item[1] - item[0])
        if best[1] - best[0] >= MIN_WINDOW:
            window = [round(best[0], 3), round(best[1], 3)]
    return {
        "ok": window is not None,
        "method": method,
        "baseline": round(baseline, 4),
        "threshold": round(threshold, 4),
        "samples": [[round(time, 3), round(value, 4)] for time, value in zip(times, series) if value >= 0],
        "window": window,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


def cache_path(project: Path, episode: str, shot_id: str) -> Path:
    return Path(project) / "07_qc" / "lipsync" / episode / f"{shot_id}.json"


def detect(project: Path, episode: str, shot_id: str, force: bool = False) -> dict[str, Any]:
    video = Path(project) / "05_generation" / "videos" / episode / f"{shot_id}.mp4"
    path = cache_path(project, episode, shot_id)
    if not force and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data and data.get("cached_from") == video.name:
                return data
        except (OSError, json.JSONDecodeError):
            pass
    if not video.exists():
        return {"ok": False, "reason": "视频不存在", "window": None, "samples": []}
    result = activity(video)
    result["cached_from"] = video.name
    result["shot_id"] = shot_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
