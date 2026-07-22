from __future__ import annotations

import json
from pathlib import Path

from scripts.convert_heldout_retrieval_markdown import parse_heldout_markdown, write_dataset_json
from scripts.evaluate_real_evidence import snapshot_readiness


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = BACKEND_ROOT / "evals" / "heldout_retrieval_eval.json"
FINAL_DATASET_PATH = BACKEND_ROOT / "evals" / "final_heldout_retrieval_eval.json"
COVERAGE_V2_FINAL_DATASET_PATH = (
    BACKEND_ROOT / "evals" / "coverage_aware_v2_final_heldout_retrieval_eval.json"
)


def test_markdown_converter_preserves_evidence_ranges_and_unanswerable() -> None:
    markdown = """\
## 视频一：heldout-video-001
- 视频：`BV1234567890`
- 时长：`约 04 分 29 秒`
- 是否未参与开发调参：`是`

### heldout-video-001-q01 - ASR+OCR 联合
- 问题：联合证据是什么？
- 可回答性：`可回答`
- 证据来源：`ASR + OCR`
- 大致时间区间：
  - `OCR，00:10-00:20：画面证据`
  - `ASR，00:15-00:25：声音证据`
- 参考答案（可选）：联合答案

### heldout-video-001-q02 - 不可回答
- 问题：视频没说什么？
- 可回答性：`不可回答`
- 证据来源：`无`
- 大致时间区间：`无`
- 参考答案（可选）：`视频中没有说明`
"""

    dataset = parse_heldout_markdown(markdown, "fixture.md")

    assert dataset["videos"][0]["durationMs"] == 269000
    assert dataset["cases"][0]["type"] == "asr_ocr"
    assert dataset["cases"][0]["referenceAnswer"] == "联合答案"
    assert dataset["cases"][0]["goldEvidence"] == [
        {
            "evidenceId": "heldout-video-001-q01-e1",
            "startMs": 10000,
            "endMs": 20000,
            "sources": ["OCR"],
        },
        {
            "evidenceId": "heldout-video-001-q01-e2",
            "startMs": 15000,
            "endMs": 25000,
            "sources": ["ASR"],
        },
    ]
    assert dataset["cases"][1]["answerable"] is False
    assert dataset["cases"][1]["goldEvidence"] == []


def test_markdown_converter_normalizes_coverage_aware_multi_evidence_types() -> None:
    markdown = """\
## 视频一：coverage-final-video-001
- 视频：`BV1234567890`
- 时长：`约 01 分 00 秒`
- 未参与此前开发、调参与方案选择：`是`

### coverage-final-video-001-q01 - 比较多证据
- 问题：两部分有什么区别？
- 可回答性：`可回答`
- 证据来源：`多证据 ASR`
- 大致时间区间：`ASR，00:10-00:20`

### coverage-final-video-001-q02 - 枚举多证据
- 问题：有哪些来源？
- 可回答性：`可回答`
- 证据来源：`多证据 ASR+OCR`
- 大致时间区间：
  - `ASR，00:20-00:30：第一项`
  - `OCR，00:30-00:40：第二项`
"""

    dataset = parse_heldout_markdown(markdown, "coverage-v2.md")

    assert [item["type"] for item in dataset["cases"]] == [
        "multi_evidence",
        "multi_evidence",
    ]
    assert len(dataset["cases"][1]["goldEvidence"]) == 2


def test_frozen_heldout_dataset_has_expected_shape() -> None:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    case_ids = [item["caseId"] for item in dataset["cases"]]
    by_id = {item["caseId"]: item for item in dataset["cases"]}

    assert dataset["status"] == "FROZEN_HELD_OUT"
    assert len(dataset["videos"]) == 2
    assert len(case_ids) == 12
    assert len(set(case_ids)) == 12
    assert len(by_id["heldout-video-001-q05"]["goldEvidence"]) == 2
    assert len(by_id["heldout-video-002-q05"]["goldEvidence"]) == 4
    assert by_id["heldout-video-001-q06"]["goldEvidence"] == []
    assert by_id["heldout-video-002-q06"]["goldEvidence"] == []
    assert all("referenceAnswer" in item for item in dataset["cases"])


def test_snapshot_readiness_requires_both_asr_and_ocr_for_every_video() -> None:
    dataset = {
        "videos": [
            {"videoId": "video-1"},
            {"videoId": "video-2"},
        ]
    }
    snapshots = {
        "video-1": [{"source": "ASR"}, {"source": "OCR"}],
        "video-2": [{"source": "ASR"}],
    }

    readiness = snapshot_readiness(dataset, snapshots)

    assert readiness["ready"] is False
    assert readiness["videos"]["video-1"]["ready"] is True
    assert readiness["videos"]["video-2"]["missingConditions"] == ["OCR_MISSING"]


def test_final_frozen_heldout_dataset_has_expected_shape_and_reference_answers() -> None:
    dataset = json.loads(FINAL_DATASET_PATH.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    by_id = {item["caseId"]: item for item in cases}

    assert dataset["datasetId"] == "seeit-final-heldout-retrieval-v1"
    assert dataset["status"] == "FROZEN_FINAL_HELD_OUT"
    assert len(dataset["videos"]) == 2
    assert all(item["heldOut"] is True for item in dataset["videos"])
    assert len(cases) == 14
    assert len(by_id) == 14
    assert sum(bool(item["answerable"]) for item in cases) == 9
    assert sum(not bool(item["answerable"]) for item in cases) == 5
    assert len(by_id["final-video-001-q05"]["goldEvidence"]) == 2
    assert len(by_id["final-video-002-q04"]["goldEvidence"]) == 2
    assert by_id["final-video-001-q05"]["type"] == "multi_evidence"
    assert by_id["final-video-002-q07"]["goldEvidence"] == []
    assert all("referenceAnswer" in item for item in cases)


def test_coverage_v2_final_dataset_preserves_all_frozen_cases_and_evidence() -> None:
    dataset = json.loads(COVERAGE_V2_FINAL_DATASET_PATH.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    by_id = {item["caseId"]: item for item in cases}

    assert dataset["datasetId"] == "seeit-coverage-aware-v2-final-heldout-retrieval-v1"
    assert dataset["status"] == "FROZEN_FINAL_HELD_OUT"
    assert [item["bvid"] for item in dataset["videos"]] == [
        "BV1RYKp66Ext",
        "BV1d3f2BqEMx",
    ]
    assert len(cases) == 16
    assert len(by_id) == 16
    assert sum(bool(item["answerable"]) for item in cases) == 11
    assert sum(not bool(item["answerable"]) for item in cases) == 5
    assert len(by_id["coverage-final-video-001-q04"]["goldEvidence"]) == 4
    assert len(by_id["coverage-final-video-001-q05"]["goldEvidence"]) == 8
    assert len(by_id["coverage-final-video-002-q04"]["goldEvidence"]) == 2
    assert len(by_id["coverage-final-video-002-q05"]["goldEvidence"]) == 2
    assert by_id["coverage-final-video-001-q04"]["type"] == "multi_evidence"
    assert by_id["coverage-final-video-001-q05"]["type"] == "multi_evidence"
    assert by_id["coverage-final-video-001-q07"]["goldEvidence"] == []
    assert by_id["coverage-final-video-002-q08"]["goldEvidence"] == []
    assert all("referenceAnswer" in item for item in cases)


def test_dataset_writer_uses_stable_utf8_lf(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "dataset.json"

    write_dataset_json(output, {"description": "中文", "cases": []})

    raw = output.read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode("utf-8") == '{\n  "description": "中文",\n  "cases": []\n}'
