"""Convert the frozen human Markdown annotation into retrieval-eval JSON."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


VIDEO_HEADING = re.compile(r"^##\s+视频[^：]*：([a-zA-Z0-9-]+)\s*$")
CASE_HEADING = re.compile(r"^###\s+([a-zA-Z0-9-]+)\s+-\s+(.+?)\s*$")
TIME_RANGE = re.compile(
    r"^(ASR|OCR)\s*[，,]\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*"
    r"(\d{1,2}:\d{2}(?::\d{2})?)",
    re.IGNORECASE,
)

TYPE_MAP = {
    "ASR 事实或改写": "asr_fact_or_paraphrase",
    "OCR 画面文字": "ocr",
    "ASR+OCR 联合": "asr_ocr",
    "时间指代": "temporal_reference",
    "ASR多证据推理": "multi_evidence",
    "多证据": "multi_evidence",
    "比较多证据": "multi_evidence",
    "枚举多证据": "multi_evidence",
    "不可回答": "unanswerable",
}


def unquote(value: str) -> str:
    normalized = value.strip()
    if len(normalized) >= 2 and normalized.startswith("`") and normalized.endswith("`"):
        return normalized[1:-1].strip()
    return normalized


def timestamp_ms(value: str) -> int:
    parts = [int(item) for item in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        hours = 0
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError(f"无效时间戳：{value}")
    if minutes < 0 or seconds < 0 or seconds >= 60:
        raise ValueError(f"无效时间戳：{value}")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000


def parse_time_range(value: str) -> dict[str, Any] | None:
    match = TIME_RANGE.match(unquote(value))
    if not match:
        return None
    start_ms = timestamp_ms(match.group(2))
    end_ms = timestamp_ms(match.group(3))
    if end_ms < start_ms:
        raise ValueError(f"证据结束时间早于开始时间：{value}")
    return {
        "source": match.group(1).upper(),
        "startMs": start_ms,
        "endMs": end_ms,
    }


def parse_duration_ms(value: str) -> int:
    normalized = unquote(value)
    hours = re.search(r"(\d+)\s*时", normalized)
    minutes = re.search(r"(\d+)\s*分", normalized)
    seconds = re.search(r"(\d+)\s*秒", normalized)
    if not any((hours, minutes, seconds)):
        raise ValueError(f"无法解析视频时长：{value}")
    return (
        int(hours.group(1)) * 3600 if hours else 0
    ) * 1000 + (
        int(minutes.group(1)) * 60 if minutes else 0
    ) * 1000 + (
        int(seconds.group(1)) if seconds else 0
    ) * 1000


def parse_heldout_markdown(
    text: str,
    source_annotation: str = "",
    *,
    dataset_id: str = "seeit-heldout-retrieval-v1",
    status: str = "FROZEN_HELD_OUT",
    description: str = "SeeIt AI 独立 held-out 视频检索标注；参考答案不参与检索评分。",
) -> dict[str, Any]:
    videos: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    current_video: dict[str, Any] | None = None
    current_case: dict[str, Any] | None = None
    reading_ranges = False

    def finish_case() -> None:
        nonlocal current_case
        if current_case is None:
            return
        case_id = str(current_case["caseId"])
        annotation_type = str(current_case["annotationType"])
        if annotation_type not in TYPE_MAP:
            raise ValueError(f"{case_id} 使用未知题型：{annotation_type}")
        missing = [key for key in ("question", "answerable", "sourceLabel") if key not in current_case]
        if missing:
            raise ValueError(f"{case_id} 缺少字段：{', '.join(missing)}")
        evidence_ranges = current_case.pop("_evidenceRanges", [])
        answerable = bool(current_case["answerable"])
        if answerable and not evidence_ranges:
            raise ValueError(f"{case_id} 可回答但没有证据时间区间")
        if not answerable and evidence_ranges:
            raise ValueError(f"{case_id} 不可回答但填写了证据时间区间")
        current_case["type"] = TYPE_MAP[annotation_type]
        current_case["goldEvidence"] = [
            {
                "evidenceId": f"{case_id}-e{index}",
                "startMs": item["startMs"],
                "endMs": item["endMs"],
                "sources": [item["source"]],
            }
            for index, item in enumerate(evidence_ranges, start=1)
        ]
        cases.append(current_case)
        current_case = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        video_match = VIDEO_HEADING.match(line)
        if video_match:
            finish_case()
            current_video = {"videoId": video_match.group(1)}
            videos.append(current_video)
            reading_ranges = False
            continue
        case_match = CASE_HEADING.match(line)
        if case_match:
            finish_case()
            if current_video is None:
                raise ValueError(f"题目 {case_match.group(1)} 前缺少视频定义")
            current_case = {
                "caseId": case_match.group(1),
                "videoId": current_video["videoId"],
                "annotationType": case_match.group(2).strip(),
                "_evidenceRanges": [],
            }
            reading_ranges = False
            continue

        stripped = line.strip()
        if current_case is not None:
            if stripped.startswith("- 问题："):
                current_case["question"] = unquote(stripped.split("：", 1)[1])
                reading_ranges = False
            elif stripped.startswith("- 可回答性："):
                value = unquote(stripped.split("：", 1)[1])
                if value not in {"可回答", "不可回答"}:
                    raise ValueError(f"{current_case['caseId']} 可回答性无效：{value}")
                current_case["answerable"] = value == "可回答"
                reading_ranges = False
            elif stripped.startswith("- 证据来源："):
                current_case["sourceLabel"] = unquote(stripped.split("：", 1)[1])
                reading_ranges = False
            elif stripped.startswith("- 大致时间区间："):
                value = stripped.split("：", 1)[1].strip()
                parsed = parse_time_range(value)
                if parsed:
                    current_case["_evidenceRanges"].append(parsed)
                elif unquote(value) not in {"", "无"}:
                    raise ValueError(f"{current_case['caseId']} 时间区间无法解析：{value}")
                reading_ranges = not value
            elif re.match(r"^- 参考答案(?:（[^）]*）)?：", stripped):
                current_case["referenceAnswer"] = unquote(stripped.split("：", 1)[1])
                reading_ranges = False
            elif reading_ranges and stripped.startswith("- "):
                parsed = parse_time_range(stripped[2:].strip())
                if parsed:
                    current_case["_evidenceRanges"].append(parsed)
                else:
                    raise ValueError(f"{current_case['caseId']} 时间区间无法解析：{stripped}")
            continue

        if current_video is not None:
            if stripped.startswith("- 视频："):
                current_video["bvid"] = unquote(stripped.split("：", 1)[1])
            elif stripped.startswith("- 时长："):
                current_video["durationMs"] = parse_duration_ms(stripped.split("：", 1)[1])
            elif stripped.startswith((
                "- 是否未参与开发调参：",
                "- 未参与此前开发、调参与方案选择：",
            )):
                current_video["heldOut"] = unquote(stripped.split("：", 1)[1]) == "是"
            elif stripped.startswith("- 备注（可选）："):
                current_video["notes"] = unquote(stripped.split("：", 1)[1])

    finish_case()
    case_ids = [str(item["caseId"]) for item in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("caseId 存在重复")
    known_videos = {str(item["videoId"]) for item in videos}
    if {str(item["videoId"]) for item in cases} - known_videos:
        raise ValueError("存在引用未知视频的 case")
    for video in videos:
        missing = [key for key in ("bvid", "durationMs", "heldOut") if key not in video]
        if missing:
            raise ValueError(f"{video['videoId']} 缺少字段：{', '.join(missing)}")
        if not video["heldOut"]:
            raise ValueError(f"{video['videoId']} 未声明为独立留出视频")
    return {
        "schemaVersion": "1.0",
        "datasetId": dataset_id,
        "status": status,
        "sourceAnnotation": source_annotation,
        "description": description,
        "topK": [1, 3, 8],
        "videos": videos,
        "cases": cases,
    }


def write_dataset_json(path: Path, dataset: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(dataset, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="将 SeeIt AI held-out Markdown 标注转换为 JSON")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset-id", default="seeit-heldout-retrieval-v1")
    parser.add_argument("--status", default="FROZEN_HELD_OUT")
    parser.add_argument("--source-annotation", help="写入 JSON 的人类标注来源标签")
    parser.add_argument(
        "--description",
        default="SeeIt AI 独立 held-out 视频检索标注；参考答案不参与检索评分。",
    )
    args = parser.parse_args()
    dataset = parse_heldout_markdown(
        args.input.read_text(encoding="utf-8"),
        args.source_annotation or str(args.input),
        dataset_id=args.dataset_id,
        status=args.status,
        description=args.description,
    )
    write_dataset_json(args.output, dataset)
    print(json.dumps({
        "datasetId": dataset["datasetId"],
        "videoCount": len(dataset["videos"]),
        "caseCount": len(dataset["cases"]),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
