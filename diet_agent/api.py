from __future__ import annotations

import json
import os
import re
from enum import Enum
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from google import genai as google_genai
from google.genai import types
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator

from diet_agent.config import load_project_dotenv


load_project_dotenv()


class ChatType(str, Enum):
    COACHING = "COACHING"
    NUTRITION = "NUTRITION"


class Role(str, Enum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


class Gender(str, Enum):
    MALE = "MALE"
    FEMALE = "FEMALE"


class PreviousWorkout(str, Enum):
    UPPER_BODY = "UPPER_BODY"
    LOWER_BODY = "LOWER_BODY"


class ChatSettings(BaseModel):
    upperBody: str | None = None
    lowerBody: str | None = None
    durationMinutes: int | None = Field(default=None, gt=0)


class UserProfile(BaseModel):
    name: str
    gender: Gender | None = None
    heightCm: float | None = None
    targetGainKg: float | None = None
    previousWorkout: PreviousWorkout | None = None


class InbodySnapshot(BaseModel):
    measuredAt: str
    weightKg: float | None = None
    skeletalMuscleMassKg: float | None = None
    bodyFatMassKg: float | None = None
    bmrKcal: int | None = None


class HistoryMessage(BaseModel):
    role: Role
    content: str


class GenerateRequest(BaseModel):
    type: ChatType
    message: str | None = None
    settings: ChatSettings | None = None
    profile: UserProfile
    inbody: InbodySnapshot | None = None
    history: list[HistoryMessage] = Field(default_factory=list)

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        return value


class GenerateResponse(BaseModel):
    reply: str
    result: dict[str, Any] | None = None


app = FastAPI(title="microstone AI server", version="0.1.0")


def _get_gemini_api_key() -> str:
    gemini_api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        raise HTTPException(
            status_code=500,
            detail="OPENAI_API_KEY, GOOGLE_API_KEY, GEMINI_API_KEY 중 하나가 필요합니다.",
        )
    return gemini_api_key


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


def _build_prompt(request: GenerateRequest) -> str:
    mode = "운동 코칭" if request.type == ChatType.COACHING else "영양 코칭"
    result_schema = _result_schema_instruction(request.type, request.message is None)

    payload = request.model_dump(mode="json")
    return f"""
당신은 헬스케어 앱의 {mode} AI입니다.
백엔드 서버가 아래 JSON 입력을 보냈습니다.
이 AI 서버는 DB에 직접 접근하지 않는 stateless 서버입니다. 백엔드가 전달한 profile, inbody,
settings, history만 근거로 답하세요.

반드시 JSON만 반환하세요. 마크다운, 설명 문장, 코드블록은 금지입니다.
최상위 응답 스키마:
{{
  "reply": "사용자에게 보여줄 자연스러운 한국어 말풍선 텍스트",
  "result": null 또는 아래 타입별 구조화 결과
}}

공통 규칙:
- reply는 한국어 존댓말로 작성합니다.
- message가 null이면 첫 인사말 요청입니다. 이름을 부르고, 인바디/이전 운동 정보가 있으면 오늘 추천 방향을 짧게 말하세요. 이 경우 result는 null입니다.
- 확실한 근거가 없는 수치나 의학적 진단은 만들지 마세요.
- 사용자가 질문만 한 경우에는 자연어로 답하고 result는 null로 둡니다.
- 사용자가 루틴/식단표처럼 구조화 결과를 명확히 요청한 경우에만 result를 만들고, 백엔드 명세의 필드명을 정확히 사용하세요.

{result_schema}

입력 JSON:
{json.dumps(payload, ensure_ascii=False)}
""".strip()


def _result_schema_instruction(chat_type: ChatType, is_greeting: bool) -> str:
    if is_greeting:
        return "첫 인사말 요청이므로 result는 반드시 null로 반환하세요."

    if chat_type == ChatType.COACHING:
        return """
COACHING 규칙:
- profile.previousWorkout이 UPPER_BODY이면 최근 상체 운동을 한 것으로 보고, 같은 부위 고강도 반복을 피하세요.
- profile.previousWorkout이 LOWER_BODY이면 최근 하체 운동을 한 것으로 보고, 같은 부위 고강도 반복을 피하세요.
- settings.upperBody, settings.lowerBody, settings.durationMinutes가 있으면 사용자의 선택 조건으로 반영하세요.
- inbody가 있으면 체중, 골격근량, 체지방량, bmrKcal을 강도 조절의 참고 정보로만 사용하세요.
- history에 최근 운동 관련 대화가 있으면 중복 부위를 피하고, 사용자의 선호를 유지하세요.
- 통증, 부상, 질환이 언급되면 무리한 동작을 피하게 하고 전문가 상담을 권하세요.

사용자가 운동 루틴 추천을 명확히 요청한 경우에만 아래 result 스키마를 사용하세요.
단순 질문, 상담, 설명 요청이면 result는 null입니다.

COACHING result 스키마:
{
  "routine": {
    "title": "COACHING AI 운동루틴",
    "exercises": [
      {
        "order": 1,
        "name": "운동명",
        "sets": "3~4세트",
        "reps": "8~12회",
        "description": "자세와 수행 방법",
        "imageUrl": "https://..."
      }
    ]
  }
}
운동은 3~5개를 추천하세요. imageUrl을 확실히 모르면 null을 사용하세요.
sets/reps는 "3~4세트", "8~12회"처럼 백엔드 명세와 같은 문자열로 작성하세요.
""".strip()

    return """
NUTRITION 규칙:
- profile, inbody, history를 참고하되 확실하지 않은 수치는 만들지 마세요.
- 사용자가 식단표를 명확히 요청한 경우에만 result를 만들고, 일반 식단 질문이면 result는 null입니다.

NUTRITION result 스키마:
{
  "mealPlan": {
    "title": "NUTRITION AI 식단표",
    "days": [
      {
        "dayOfWeek": "MON",
        "meals": [
          {
            "slot": "BREAKFAST",
            "menu": "메뉴",
            "calories": 500,
            "carbsG": 60,
            "proteinG": 30,
            "fatG": 15
          }
        ]
      }
    ]
  }
}
MON부터 SUN까지 7일을 모두 포함하고, 각 day에는 BREAKFAST, LUNCH, DINNER를 포함하세요.
""".strip()


def _parse_generate_response(raw_text: str) -> GenerateResponse:
    try:
        data = json.loads(_strip_json_fence(raw_text))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="AI 응답 JSON 파싱에 실패했습니다.") from exc

    try:
        return GenerateResponse.model_validate(data)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="AI 응답 스키마가 올바르지 않습니다.") from exc


def _generate_with_openai(request: GenerateRequest) -> GenerateResponse:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL") or os.getenv("AI_MODEL", "gpt-4o-mini"),
        messages=[
            {
                "role": "user",
                "content": _build_prompt(request),
            }
        ],
        response_format={"type": "json_object"},
        temperature=0.7,
    )
    raw_text = response.choices[0].message.content or ""
    return _parse_generate_response(raw_text)


def _generate_with_gemini(request: GenerateRequest) -> GenerateResponse:
    client = google_genai.Client(api_key=_get_gemini_api_key())
    response = client.models.generate_content(
        model=os.getenv("AI_MODEL", "gemini-2.5-flash"),
        contents=_build_prompt(request),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.7,
            top_p=0.9,
        ),
    )

    return _parse_generate_response(response.text or "")

def _generate_ai_response(request: GenerateRequest) -> GenerateResponse:
    if os.getenv("OPENAI_API_KEY"):
        return _generate_with_openai(request)
    return _generate_with_gemini(request)


async def _parse_generate_request(raw_request: Request) -> GenerateRequest:
    body = await raw_request.body()

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid UTF-8 JSON.") from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON.") from exc

    try:
        return GenerateRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


@app.get("/health")
def health() -> dict[str, Literal["ok"]]:
    return {"status": "ok"}


@app.post("/generate", response_model=GenerateResponse)
async def generate(raw_request: Request) -> GenerateResponse:
    request = await _parse_generate_request(raw_request)
    return _generate_ai_response(request)
