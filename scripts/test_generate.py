"""diet_agent의 /generate 엔드포인트를 서버 없이(TestClient로 직접) 테스트하는 CLI.

실제 서버(uvicorn)를 띄우지 않고 diet_agent/api.py의 FastAPI app을 프로세스 안에서
그대로 호출하므로, 프로덕션 코드와 100% 동일한 경로를 검증한다.

사용 예:
    python scripts/test_generate.py "일주일치 식단표 만들어줘"
    python scripts/test_generate.py "탄수화물 줄여줘" --history history.json
    python scripts/test_generate.py --type COACHING "상체 루틴 짜줘"
    python scripts/test_generate.py  # message 생략 시 첫 인사말 요청
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path
from typing import cast

cast(io.TextIOWrapper, sys.stdout).reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from diet_agent.api import app  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", nargs="?", default=None, help="사용자 메시지 (생략하면 첫 인사말 요청)")
    parser.add_argument("--type", choices=["NUTRITION", "COACHING"], default="NUTRITION")
    parser.add_argument("--name", default="테스트유저", help="profile.name")
    parser.add_argument(
        "--history",
        help="history 배열(HistoryMessage 리스트)이 담긴 JSON 파일 경로. "
        "같은 대화를 이어서 테스트하고 싶을 때 사용",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Gemini/OpenAI 일시 과부하(502)로 실패할 때 재시도 횟수 (기본 3)",
    )
    args = parser.parse_args()

    payload: dict = {
        "type": args.type,
        "message": args.message,
        "profile": {"name": args.name},
    }
    if args.history:
        with open(args.history, encoding="utf-8") as f:
            payload["history"] = json.load(f)

    client = TestClient(app)

    response = None
    for attempt in range(1, args.retries + 1):
        response = client.post("/generate", json=payload)
        if response.status_code == 200:
            break
        print(f"[재시도 {attempt}/{args.retries}] status={response.status_code}: {response.text[:300]}", file=sys.stderr)
        if attempt < args.retries:
            time.sleep(3)

    if response is None:
        print("--retries가 1 이상이어야 요청이 실행됩니다.", file=sys.stderr)
        sys.exit(1)

    print(f"status: {response.status_code}")
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
