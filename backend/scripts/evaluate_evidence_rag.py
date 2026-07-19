from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from seeit.retrieval import evaluate_retrieval_cases  # noqa: E402


DEFAULT_DATASET = BACKEND_ROOT / "evals" / "evidence_rag_eval.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="评估 SeeIt AI 证据 RAG 检索基线")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--top-k", type=int, default=None)
    args = parser.parse_args()

    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    result = evaluate_retrieval_cases(
        payload["segments"],
        payload["cases"],
        top_k=args.top_k or int(payload.get("topK", 5)),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
