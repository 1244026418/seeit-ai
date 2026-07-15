from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def paddle_ocr_content(result: Any, confidence_threshold: float, min_length: int = 2) -> str:
    payload = result.json if hasattr(result, "json") else result
    if isinstance(payload, dict) and isinstance(payload.get("res"), dict):
        payload = payload["res"]
    if not isinstance(payload, dict):
        return ""

    texts = list(payload.get("rec_texts") or [])
    scores = list(payload.get("rec_scores") or [])
    accepted: list[str] = []
    for index, raw_text in enumerate(texts):
        text = re.sub(r"\s+", " ", str(raw_text)).strip()
        try:
            score = float(scores[index]) if index < len(scores) else 1.0
        except (TypeError, ValueError):
            score = 0.0
        if score >= confidence_threshold and len(text) >= min_length:
            accepted.append(text)
    return " ".join(accepted)[:2000]


def run(input_dir: Path) -> dict[str, Any]:
    model_root = Path(env("PADDLEOCR_MODEL_ROOT", "/data/models/paddlex")).resolve()
    model_root.mkdir(parents=True, exist_ok=True)
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(model_root)
    os.environ["PADDLE_PDX_MODEL_SOURCE"] = env("PADDLEOCR_MODEL_SOURCE", "bos").strip().lower() or "bos"
    from paddleocr import PaddleOCR

    confidence = min(1.0, max(0.0, float(env("PADDLEOCR_CONFIDENCE_THRESHOLD", "0.65"))))
    min_length = max(1, int(env("PADDLEOCR_MIN_TEXT_LENGTH", "2")))
    started = time.perf_counter()
    model_started = time.perf_counter()
    model = PaddleOCR(
        text_detection_model_name=env("PADDLEOCR_DETECTION_MODEL", "PP-OCRv5_mobile_det"),
        text_recognition_model_name=env("PADDLEOCR_RECOGNITION_MODEL", "PP-OCRv5_mobile_rec"),
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        text_rec_score_thresh=confidence,
        device=env("PADDLEOCR_DEVICE", "cpu"),
        cpu_threads=max(1, int(env("PADDLEOCR_CPU_THREADS", "4"))),
        enable_mkldnn=env("PADDLEOCR_ENABLE_MKLDNN", "true").lower() in {"1", "true", "yes"},
    )
    model_load_ms = int((time.perf_counter() - model_started) * 1000)

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    frames = sorted(
        frame
        for pattern in ("frame-*.png", "frame-*.jpg", "frame-*.jpeg")
        for frame in input_dir.glob(pattern)
    )
    for index, frame in enumerate(frames):
        try:
            predictions = model.predict(
                str(frame),
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                text_rec_score_thresh=confidence,
            )
            content = " ".join(
                item
                for item in (
                    paddle_ocr_content(prediction, confidence, min_length)
                    for prediction in predictions
                )
                if item
            )[:2000]
            results.append({"index": index, "frame": frame.name, "content": content})
        except Exception as exc:
            errors.append({"frame": frame.name, "error": f"{type(exc).__name__}: {exc}"})

    return {
        "frameCount": len(frames),
        "results": results,
        "errors": errors,
        "modelLoadMs": model_load_ms,
        "elapsedMs": int((time.perf_counter() - started) * 1000),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    try:
        payload = run(Path(args.input_dir))
        status = 0
    except Exception as exc:
        payload = {"frameCount": 0, "results": [], "errors": [], "fatalError": f"{type(exc).__name__}: {exc}"}
        status = 1
    output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
