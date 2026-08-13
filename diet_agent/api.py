from __future__ import annotations

import json
import os
import re
from enum import Enum
from typing import Any, Literal, cast

from fastapi import FastAPI, HTTPException, Request
from google import genai as google_genai
from google.genai import types
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator

from diet_agent.config import load_project_dotenv
from diet_agent.nutrition_tools import (
    ACTIVITY_FACTOR_ACTIVE,
    ACTIVITY_FACTOR_REST,
    FAT_RATIO,
    PROTEIN_RATIO_DEFAULT,
    PROTEIN_RATIO_DIET,
    SODIUM_DAILY_LIMIT_MG,
    find_recipes_by_ingredients,
    get_meal_variety_options,
    search_food_nutrition_batch,
)


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


def _compute_nutrition_targets(request: GenerateRequest) -> dict[str, Any]:
    """agent.py의 _compute_daily_targets와 같은 계산(Mifflin-St Jeor 기반 TDEE +
    목표 보정, 없으면 체중 기반 배수 폴백)을 이 서버의 요청 스키마에 맞춰 다시
    구현한다. LLM이 TDEE/단백질/지방 목표를 프롬프트 안에서 암산하다 틀리는 걸
    막기 위해, 서버가 미리 계산한 숫자를 프롬프트에 박아 넣고 그대로 쓰게 한다.

    agent.py와 달리 이 서버의 UserProfile에는 나이(age) 필드가 없어 Mifflin-St
    Jeor 공식을 직접 쓸 수 없다. 대신 InbodySnapshot.bmrKcal(인바디 기기 실측값)이
    있으면 그걸 BMR로 쓰고, 없으면 체중 기반 단백질/지방 배수로만 계산한다
    (agent.py에서 나이 미확인일 때 쓰는 폴백과 동일한 방식). "오늘 이미 섭취한
    양"은 이 서버가 받는 값이 아니라서(meal-log-proposal.md 참고, 아직 백엔드
    미구현) 목표치만 계산하고 부족/과다분은 계산하지 않는다.
    """
    weight = request.inbody.weightKg if request.inbody else None
    if weight is None:
        return {"note": "체중 정보가 없어 목표치를 계산하지 못했습니다. 일반적인 성인 기준(단백질 100~120g)으로 추정치임을 밝히고 안내하세요."}

    bmr = request.inbody.bmrKcal if request.inbody else None
    has_recent_workout = request.profile.previousWorkout is not None
    target_gain_kg = request.profile.targetGainKg or 0

    calorie_target: float | None = None
    protein_target: float
    fat_target: float

    if bmr:
        activity_factor = ACTIVITY_FACTOR_ACTIVE if has_recent_workout else ACTIVITY_FACTOR_REST
        tdee = bmr * activity_factor
        goal_adjust = 300 if target_gain_kg > 0 else (-400 if target_gain_kg < 0 else 0)
        calorie_target = tdee + goal_adjust

        protein_ratio = PROTEIN_RATIO_DIET if target_gain_kg < 0 else PROTEIN_RATIO_DEFAULT
        protein_target = calorie_target * protein_ratio / 4
        fat_target = calorie_target * FAT_RATIO / 9
        note = "inbody.bmrKcal(실측 BMR) 기준으로 계산한 목표치이니 그대로 사용하세요."
    else:
        multiplier = 1.8 if has_recent_workout else 1.3
        protein_target = weight * multiplier
        fat_target = weight * 0.9
        note = "BMR 실측값이 없어 체중 기반 배수로 추정한 단백질/지방 목표입니다. 총 칼로리는 대략적인 추정치라고 밝히세요."

    return {
        "calorie_target_kcal": round(calorie_target) if calorie_target else None,
        "protein_target_g": round(protein_target),
        "fat_target_g": round(fat_target),
        "sodium_limit_mg": SODIUM_DAILY_LIMIT_MG,
        "note": note,
    }


def _build_prompt(request: GenerateRequest) -> str:
    mode = "운동 코칭" if request.type == ChatType.COACHING else "영양 코칭"
    result_schema = _result_schema_instruction(request.type, request.message is None)

    nutrition_context = ""
    if request.type == ChatType.NUTRITION:
        targets = _compute_nutrition_targets(request)
        nutrition_context += f"""
서버가 미리 계산한 목표치 (참고용 — 값이 있는 항목은 반드시 이 숫자를 그대로
쓰고, 스스로 다시 계산하지 마세요):
{json.dumps(targets, ensure_ascii=False)}
"""
        if request.message is not None:
            recent_text = " ".join(h.content for h in request.history if h.content)
            variety = get_meal_variety_options(recent_text)
            nutrition_context += f"""
다양성 후보 (참고용 — 최근 대화에 나온 재료/메뉴는 이미 제외되어 있습니다.
반드시 이 안에서만 골라야 하는 건 아니지만, 매번 같은 조합(닭가슴살+현미밥
반복 등)을 피하는 데 활용하세요):
{json.dumps(variety, ensure_ascii=False)}
"""

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
{nutrition_context}
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
- 하루 칼로리/단백질/지방 목표와 나트륨 상한은 프롬프트 하단의 "서버가 미리 계산한 목표치"를 그대로
  쓰세요. 이 값들은 인바디 실측 BMR(있으면)이나 체중 기반 공식으로 서버가 미리 계산한 것이라, 직접
  암산하는 것보다 정확합니다. note 필드에 계산 방식/한계가 적혀 있으면 reply에 자연스럽게 반영하세요
  (예: BMR 실측값이 없어 추정치라는 점).
- 메뉴를 새로 구성할 때는 프롬프트 하단의 "다양성 후보"를 우선 참고해서, 최근 대화에 나온 메뉴나
  같은 단백질원/조리법을 반복하지 마세요. 그 목록 안에서만 골라야 하는 건 아닙니다.
- 사용자가 "집에 있는 재료로 뭐 해먹을지" 물으면 find_recipes_by_ingredients 도구로 실제 레시피를
  찾아서 추천하세요.
- 식단표(result.mealPlan)를 만들기 전에 반드시 history와 이번 message를 먼저 읽고, 오늘 이미
  언급된 끼니가 있는지 확인하세요. 이때 두 종류를 구분하세요:
  - **실제로 먹었다고 한 것** (과거형: "먹었어", "먹었다" 등): 이미 벌어진 사실이므로 절대 못 바꿉니다.
    어떤 새 방향/선호가 나중에 나와도 이 메뉴는 그대로 유지하세요.
  - **AI가 추천만 했을 뿐 실제로 먹었다는 확인은 없는 것** ("~로 구성했습니다" 같은 이전 AI 응답):
    아직 확정된 게 아니므로, 사용자가 나중에 새로운 방향/선호를 말하면 이 부분도 그 방향에 맞게
    바뀔 수 있습니다.
  - 오늘 날짜에 해당하는 day(가장 첫 번째 day를 "오늘"로 취급)에서, "실제로 먹었다고 한" 슬롯은
    그 메뉴 그대로 채우고 다른 걸로 바꾸지 마세요. 그 외 슬롯(아직 안 먹은 것)은 자유롭게 채우세요.
  - history에 최근(오늘 포함 최근 며칠) 등장한 메뉴명이 있으면, 남은 끼니를 짤 때 그 메뉴와 겹치지
    않게 구성하세요 (매번 닭가슴살+현미밥만 반복하지 말 것).
  - 단, 이건 이번 대화(history)에 실제로 남아있는 내용까지만입니다. 세션이 새로 시작되어 history가
    비어있으면 오늘 아무것도 안 먹은 것으로 간주하고 평소대로 3끼를 모두 채우세요.
- 사용자가 식단 방향/선호를 말로 요청하면(예: "가볍게 해줘", "탄수화물 좀 줄여줘", "매운 거 빼줘",
  "저염으로", "단백질 위주로", "예산 낮게") 그 방향을 이번에 새로 만드는 식단표(및 이후 같은 대화에서
  다시 만드는 식단표)에 반드시 반영하세요.
  - 이건 이전 식단표를 그대로 두고 일부만 콕 집어 고치는 게 아니라, 그 방향에 맞게 전체를 다시
    구성하는 겁니다 (예: "가볍게 해줘" -> 전반적으로 칼로리/지방이 낮은 메뉴로 다시 짜기).
  - **단, 위에서 "실제로 먹었다고 한" 끼니는 이 새 방향으로도 절대 바꾸지 마세요** — 이미 먹은 건
    되돌릴 수 없으니 그대로 두고, 그 외 나머지(아직 안 먹은 끼니 전부)만 새 방향에 맞게 다시 짜세요.
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
            "식약처 식품영양성분DB(한식 위주)에서 조회한다. "
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
    """모델이 보낸 도구 인자가 스키마와 다르게 와도(잘못된 타입 등) 예외가 그대로
    새어나가 요청 전체가 500으로 죽는 일이 없도록, 여기서 한 번 더 방어한다."""
    try:
        if name == "search_food_nutrition":
            food_names = args.get("food_names") or []
            if not isinstance(food_names, list):
                return {"status": "error", "message": "food_names는 배열이어야 합니다."}
            food_names = [n for n in food_names if isinstance(n, str)]
            return search_food_nutrition_batch(food_names)
        if name == "find_recipes_by_ingredients":
            ingredients = args.get("ingredients") or []
            if not isinstance(ingredients, list):
                return {"status": "error", "message": "ingredients는 배열이어야 합니다."}
            ingredients = [i for i in ingredients if isinstance(i, str)]
            return find_recipes_by_ingredients(
                ingredients=ingredients,
                max_results=int(args.get("max_results") or 3),
            )
        return {"status": "error", "message": f"알 수 없는 도구: {name}"}
    except Exception as e:
        return {"status": "error", "message": f"도구 실행 중 오류: {e}"}


def _wants_nutrition_tools(request: GenerateRequest) -> bool:
    # 인사말 요청(message=None)은 조회할 음식이 없으니 도구를 붙이지 않는다 —
    # 그래야 Gemini 경로에서 JSON 강제 모드를 계속 쓸 수 있다 (도구가 있으면 강제 모드를 못 씀).
    return request.type == ChatType.NUTRITION and request.message is not None


def _generate_with_openai(request: GenerateRequest) -> GenerateResponse:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    model = os.getenv("OPENAI_MODEL") or os.getenv("AI_MODEL", "gpt-4o-mini")
    tools: list[dict[str, Any]] | None = None
    if _wants_nutrition_tools(request):
        tools = [{"type": "function", "function": spec} for spec in _NUTRITION_TOOL_SPECS]

    messages: list[dict[str, Any]] = [{"role": "user", "content": _build_prompt(request)}]

    for _ in range(_MAX_TOOL_ITERATIONS):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=cast(Any, messages),
                tools=cast(Any, tools),
                temperature=0.7,
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"AI 서버(OpenAI) 호출에 실패했습니다: {e}") from e
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
            if tool_call.type != "function":
                continue
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

    base_config_kwargs: dict[str, Any] = {
        "temperature": 0.7,
        "top_p": 0.9,
        # Gemini가 일시적으로 과부하(503 UNAVAILABLE)일 때 바로 502를 띄우지 않고
        # SDK 레벨에서 자동 재시도한다 (agent.py의 retry_config와 동일한 설정).
        "http_options": types.HttpOptions(
            retry_options=types.HttpRetryOptions(initial_delay=2, attempts=4)
        ),
        # thinking_budget 기본값(automatic)은 응답당 최대 5000토큰 넘게 내부 추론에
        # 써서 응답이 크게 느려진다. BMR/TDEE 같은 계산은 이제 _compute_nutrition_targets
        # 가 서버 코드로 미리 계산해서 프롬프트에 숫자로 박아주기 때문에(2026-08-13),
        # 모델이 그 계산을 위해 따로 추론할 필요가 줄었다 — 그래서 256으로도 충분히
        # 안정적이다. 7일 식단표 생성 기준 3회 반복 측정: 1024=평균 20.2초(13.7~33.2초),
        # 256=평균 13.3초(9.3~20.4초), 둘 다 3/3 성공. 0으로 완전히 끄면 더 빠를 때도
        # 있지만(평균 16.9초) 3번 중 2번은 JSON이 깨져 502로 실패해서 쓰지 않는다.
        "thinking_config": types.ThinkingConfig(thinking_budget=256),
    }

    wants_tools = _wants_nutrition_tools(request)
    tools = None
    if wants_tools:
        function_declarations = [
            types.FunctionDeclaration(
                name=spec["name"], description=spec["description"], parameters=spec["parameters"]
            )
            for spec in _NUTRITION_TOOL_SPECS
        ]
        tools = [types.Tool(function_declarations=function_declarations)]

    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part(text=_build_prompt(request))])
    ]

    # 도구 호출 라운드가 이미 한 번 끝났으면(tools_exhausted) 더 이상 도구를 주지 않는다.
    # Gemini는 tools와 response_mime_type(JSON 강제)을 동시에 못 쓰므로, 영양 조회가
    # 끝난 다음 턴부터는 tools를 빼고 JSON 강제 모드로 전환해서 마지막 대용량 응답
    # (7일 식단표 등)을 자유 텍스트가 아닌 구조화 디코딩으로 더 빠르고 안정적으로 받는다.
    tools_exhausted = False
    for _ in range(_MAX_TOOL_ITERATIONS):
        config_kwargs = dict(base_config_kwargs)
        if tools is not None and not tools_exhausted:
            config_kwargs["tools"] = tools
        else:
            config_kwargs["response_mime_type"] = "application/json"

        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(**config_kwargs),
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"AI 서버(Gemini) 호출에 실패했습니다: {e}") from e
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

        tools_exhausted = True
        contents.append(candidate.content)
        response_parts = [
            types.Part.from_function_response(
                name=fc.name or "", response={"result": _dispatch_nutrition_tool(fc.name or "", dict(fc.args or {}))}
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
