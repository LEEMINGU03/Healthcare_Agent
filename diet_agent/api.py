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
from diet_agent.nutrition_tools import find_recipes_by_ingredients, search_food_nutrition_batch


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
- settings.upperBody, settings.lowerBody, durationMinutes 중 하나라도 있으면 사용자가 오늘 부위를 직접 선택한 것입니다. 이 경우 profile.previousWorkout과 상관없이 사용자가 요청한 부위로 루틴을 만드세요. 강도만 previousWorkout을 참고해 조절하세요.
- settings에 부위 선택이 전혀 없을 때만 profile.previousWorkout을 보고 같은 부위 고강도 반복을 피하세요 (UPPER_BODY였으면 상체, LOWER_BODY였으면 하체 반복 회피).
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
- 메뉴의 칼로리/탄수화물/단백질/지방 수치는 절대 임의로 지어내지 마세요. 반드시 search_food_nutrition
  도구로 각 메뉴(또는 사용자가 방금 언급한 음식)를 조회하고, 그 조회 결과의 수치를 사용하세요.
- search_food_nutrition은 대화당 정확히 한 번만 호출하세요. 필요한 모든 메뉴 이름을 먼저 다 정하고,
  그 목록 전체를 food_names 배열 하나에 담아 한 번에 보내세요. 응답이 오면 그걸로 끝입니다 —
  일부 항목이 not_found/error여도 다른 이름으로 바꿔서 다시 호출하지 마세요 (호출할 때마다 응답 시간이
  크게 늘어나 타임아웃 위험이 있습니다). not_found/error인 항목은 그 메뉴를 유지한 채 합리적인 대략치를
  쓰고, reply에 "일부 메뉴는 정확한 조회값이 없어 대략치를 사용했다"고 짧게 밝히세요.
- 사용자가 "집에 있는 재료로 뭐 해먹을지" 물으면 find_recipes_by_ingredients 도구로 실제 레시피를
  찾아서 추천하세요.
- 식단표(result.mealPlan)를 만들기 전에 반드시 history와 이번 message를 먼저 읽고, 오늘 이미
  언급된(먹었다고 했거나 이미 추천해서 확정한) 끼니가 있는지 확인하세요.
  - 오늘 날짜에 해당하는 day(가장 첫 번째 day를 "오늘"로 취급)에서, 이미 언급된 슬롯(아침/점심/저녁)은
    그 실제로 언급된 메뉴 그대로 채우고, 새로 추천하는 것처럼 다른 메뉴로 바꾸지 마세요.
  - history에 최근(오늘 포함 최근 며칠) 등장한 메뉴명이 있으면, 남은 끼니를 짤 때 그 메뉴와 겹치지
    않게 구성하세요 (매번 닭가슴살+현미밥만 반복하지 말 것).
  - 단, 이건 이번 대화(history)에 실제로 남아있는 내용까지만입니다. 세션이 새로 시작되어 history가
    비어있으면 오늘 아무것도 안 먹은 것으로 간주하고 평소대로 3끼를 모두 채우세요.
- 사용자가 식단 방향/선호를 말로 요청하면(예: "가볍게 해줘", "탄수화물 좀 줄여줘", "매운 거 빼줘",
  "저염으로", "단백질 위주로", "예산 낮게") 그 방향을 이번에 새로 만드는 식단표(및 이후 같은 대화에서
  다시 만드는 식단표)에 반드시 반영하세요.
  - 이건 이전 식단표를 그대로 두고 일부만 콕 집어 고치는 게 아니라, 그 방향에 맞게 전체를 다시
    구성하는 겁니다 (예: "가볍게 해줘" -> 전반적으로 칼로리/지방이 낮은 메뉴로 다시 짜기).
  - history를 보고 이전에 이미 말한 방향/선호가 있으면(예: 두 턴 전에 "매운 거 빼줘"라고 했음),
    이번 요청에서 다시 언급하지 않았어도 계속 유지해서 반영하세요 — 한 번 말한 선호는 대화가
    끝날 때까지 계속 적용된다고 가정하세요.
  - reply에 어떤 방향을 반영했는지 한 줄로 짧게 언급하세요 (예: "말씀하신 대로 저녁 위주로 가볍게
    구성했습니다").

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


# NUTRITION 요청에서만 켜는 실제 영양 조회 도구. COACHING에는 필요 없다.
# agent.py의 search_food_nutrition/find_recipes_by_ingredients를 그대로 가져다 쓴다 —
# 이 두 함수는 세션 상태나 백엔드 호출 없이 순수하게 외부 영양 DB만 조회하므로
# /generate 처럼 세션 없는 stateless 호출에서도 안전하게 쓸 수 있다.
_NUTRITION_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "search_food_nutrition",
        "description": (
            "음식명 목록으로 칼로리 및 영양성분(탄수화물/단백질/지방/나트륨)을 한 번에 조회한다. "
            "한글 음식명은 식약처 식품영양성분DB를 우선 조회하고 없으면 FatSecret으로 조회한다. "
            "여러 메뉴가 필요하면(예: 7일 식단표) 한 번의 호출에 모두 담아서 보내라 — 메뉴 하나마다 "
            "따로 호출하지 마라. 식단표에 넣을 메뉴의 칼로리/영양성분은 반드시 이 도구로 조회한 값을 사용해야 한다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "food_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "조회할 음식명 목록 (한글 또는 영어). 중복 제거해서 보내라.",
                },
            },
            "required": ["food_names"],
        },
    },
    {
        "name": "find_recipes_by_ingredients",
        "description": "사용자가 가진 재료 목록으로 만들 수 있는 실제 레시피를 식약처 조리식품 레시피 DB에서 찾는다.",
        "parameters": {
            "type": "object",
            "properties": {
                "ingredients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "가진 재료 이름 목록",
                },
                "max_results": {"type": "integer", "description": "반환할 최대 레시피 수 (기본 3)"},
            },
            "required": ["ingredients"],
        },
    },
]

_MAX_TOOL_ITERATIONS = 8


def _dispatch_nutrition_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "search_food_nutrition":
        return search_food_nutrition_batch(args.get("food_names") or [])
    if name == "find_recipes_by_ingredients":
        return find_recipes_by_ingredients(
            ingredients=args.get("ingredients") or [],
            max_results=int(args.get("max_results") or 3),
        )
    return {"status": "error", "message": f"알 수 없는 도구: {name}"}


def _generate_with_openai(request: GenerateRequest) -> GenerateResponse:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    model = os.getenv("OPENAI_MODEL") or os.getenv("AI_MODEL", "gpt-4o-mini")
    tools = None
    if request.type == ChatType.NUTRITION:
        tools = [{"type": "function", "function": spec} for spec in _NUTRITION_TOOL_SPECS]

    messages: list[dict[str, Any]] = [{"role": "user", "content": _build_prompt(request)}]

    for _ in range(_MAX_TOOL_ITERATIONS):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            temperature=0.7,
        )
        message = response.choices[0].message
        if not message.tool_calls:
            return _parse_generate_response(message.content or "")

        messages.append(
            {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [tc.model_dump() for tc in message.tool_calls],
            }
        )
        for tool_call in message.tool_calls:
            args = json.loads(tool_call.function.arguments or "{}")
            result = _dispatch_nutrition_tool(tool_call.function.name, args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    raise HTTPException(status_code=502, detail="AI가 도구 호출을 마치지 못했습니다.")


def _generate_with_gemini(request: GenerateRequest) -> GenerateResponse:
    client = google_genai.Client(api_key=_get_gemini_api_key())
    model = os.getenv("AI_MODEL", "gemini-2.5-flash")

    config_kwargs: dict[str, Any] = {"temperature": 0.7, "top_p": 0.9}
    if request.type == ChatType.NUTRITION:
        function_declarations = [
            types.FunctionDeclaration(
                name=spec["name"], description=spec["description"], parameters=spec["parameters"]
            )
            for spec in _NUTRITION_TOOL_SPECS
        ]
        config_kwargs["tools"] = [types.Tool(function_declarations=function_declarations)]
    else:
        # 도구가 없을 때만 JSON 강제 모드 사용 — 도구 호출 턴에서는 모델이 함수 호출을
        # 내야 하므로 응답을 JSON으로 강제하면 안 된다.
        config_kwargs["response_mime_type"] = "application/json"

    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part(text=_build_prompt(request))])
    ]

    for _ in range(_MAX_TOOL_ITERATIONS):
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(**config_kwargs),
        )
        if not response.candidates:
            raise HTTPException(status_code=502, detail="AI 응답에 candidate가 없습니다 (안전 필터 등으로 차단됐을 수 있음).")
        candidate = response.candidates[0]
        if candidate.content is None or not candidate.content.parts:
            raise HTTPException(
                status_code=502,
                detail=f"AI 응답이 비어 있습니다 (finish_reason={candidate.finish_reason}).",
            )
        function_calls = [part.function_call for part in candidate.content.parts if part.function_call]
        if not function_calls:
            return _parse_generate_response(response.text or "")

        contents.append(candidate.content)
        response_parts = [
            types.Part.from_function_response(
                name=fc.name, response={"result": _dispatch_nutrition_tool(fc.name, dict(fc.args or {}))}
            )
            for fc in function_calls
        ]
        contents.append(types.Content(role="user", parts=response_parts))

    raise HTTPException(status_code=502, detail="AI가 도구 호출을 마치지 못했습니다.")


def _generate_ai_response(request: GenerateRequest) -> GenerateResponse:
    if os.getenv("OPENAI_API_KEY"):
        return _generate_with_openai(request)
    return _generate_with_gemini(request)


def _decode_request_body(body: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue

    return body.decode("latin-1")


def _repair_text_encoding(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _repair_text_encoding(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_repair_text_encoding(item) for item in value]
    if not isinstance(value, str):
        return value

    for encoding in ("utf-8", "cp949", "euc-kr"):
        try:
            return value.encode("latin-1").decode(encoding)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue

    return value


async def _parse_generate_request(raw_request: Request) -> GenerateRequest:
    body = await raw_request.body()
    text = _decode_request_body(body)

    try:
        payload = _repair_text_encoding(json.loads(text))
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
