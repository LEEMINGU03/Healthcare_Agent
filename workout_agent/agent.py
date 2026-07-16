from __future__ import annotations

import os

import requests
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools import ToolContext
from google.genai import types

from diet_agent.config import load_project_dotenv


load_project_dotenv()


BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://localhost:8080")
DEFAULT_USER_ID = os.getenv("DEFAULT_USER_ID")


retry_config = types.GenerateContentConfig(
    http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(initial_delay=2, attempts=4),
    ),
    temperature=0.7,
    top_p=0.9,
)


def _get_agent_model():
    if os.getenv("OPENAI_API_KEY"):
        return LiteLlm(model=os.getenv("OPENAI_MODEL") or os.getenv("AI_MODEL", "gpt-4o-mini"))
    return os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def _backend_get(path: str, params: dict | None = None) -> dict:
    try:
        response = requests.get(f"{BACKEND_BASE_URL}{path}", params=params, timeout=5)
        if response.status_code == 404:
            return {"status": "not_found"}
        if response.status_code == 400:
            return {"status": "error", "message": response.text, "detail": _safe_json(response)}
        response.raise_for_status()
        return {"status": "success", "data": response.json()}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _safe_json(response: requests.Response) -> dict | None:
    try:
        return response.json()
    except ValueError:
        return None


def _get_user_id(tool_context: ToolContext) -> int | None:
    user_id = tool_context.state.get("user_id")
    if user_id:
        return int(user_id)
    if DEFAULT_USER_ID:
        tool_context.state["user_id"] = int(DEFAULT_USER_ID)
        return int(DEFAULT_USER_ID)
    return None


def set_workout_context(
    goal: str,
    level: str,
    available_minutes: int,
    available_equipment: list[str],
    pain_or_limitations: list[str],
    tool_context: ToolContext,
) -> dict:
    """운동 상담에 필요한 세션 정보를 저장합니다.

    Args:
        goal: 운동 목표. 예: 근비대, 체지방 감량, 체력 향상. 모르면 빈 문자열.
        level: 운동 수준. 예: 초급, 중급, 고급. 모르면 빈 문자열.
        available_minutes: 오늘 운동 가능 시간(분). 모르면 0.
        available_equipment: 사용 가능한 장비 목록. 없거나 모르면 빈 리스트.
        pain_or_limitations: 통증, 부상, 제한 사항 목록. 없으면 빈 리스트.
        tool_context: ADK가 주입하는 컨텍스트.

    Returns:
        저장된 운동 상담 컨텍스트.
    """
    context = dict(tool_context.state.get("workout_context") or {})

    if goal:
        context["goal"] = goal
    if level:
        context["level"] = level
    if available_minutes > 0:
        context["available_minutes"] = available_minutes
    if available_equipment:
        context["available_equipment"] = sorted(set(available_equipment))
    if pain_or_limitations:
        context["pain_or_limitations"] = sorted(set(pain_or_limitations))

    tool_context.state["workout_context"] = context
    return {"status": "success", "context": context}


def get_workout_context(tool_context: ToolContext) -> dict:
    """세션에 저장된 운동 목표, 수준, 장비, 제한 사항을 조회합니다."""
    return {"context": tool_context.state.get("workout_context") or "미확정"}


def get_user_body_context(tool_context: ToolContext) -> dict:
    """백엔드에서 최신 인바디와 3개월 인바디 추이를 함께 조회합니다.

    체중, 체지방률/체지방량, 골격근량, 기초대사량을 운동 강도와 볼륨 조절의
    참고 정보로만 사용하세요. 의료적 진단처럼 단정하면 안 됩니다.
    """
    user_id = _get_user_id(tool_context)
    if not user_id:
        return {"status": "skipped", "reason": "userId 없음"}

    latest = _backend_get("/api/inbody/records/latest", {"userId": user_id})
    trend = _backend_get("/api/inbody/records/trend", {"userId": user_id})
    return {
        "status": "success",
        "latest_inbody": latest,
        "inbody_trend": trend,
    }


def get_recent_workouts(tool_context: ToolContext) -> dict:
    """백엔드에서 최근 운동 기록을 조회합니다.

    백엔드 스펙 기준 `GET /api/workouts?userId={userId}`를 호출합니다.
    실패하면 현재 ADK 세션에 저장된 `workout_history`만 반환합니다.
    """
    user_id = _get_user_id(tool_context)
    local_history = tool_context.state.get("workout_history", [])
    if not user_id:
        return {"status": "local_only", "recent_workouts": local_history}

    result = _backend_get("/api/workouts", {"userId": user_id})
    if result["status"] == "success":
        return {"status": "success", "recent_workouts": result["data"]}
    return {
        "status": "fallback",
        "recent_workouts": local_history,
        "backend_result": result,
    }


def get_coach_chat_history(tool_context: ToolContext) -> dict:
    """백엔드에 저장된 코칭 agent 대화 이력을 조회합니다."""
    user_id = _get_user_id(tool_context)
    if not user_id:
        return {"history": []}

    result = _backend_get("/api/chat", {"userId": user_id, "agentType": "COACH"})
    if result["status"] == "success":
        return {"history": result["data"]}
    return {"history": [], "error": result}


root_agent = Agent(
    name="workout_advice_agent",
    model=_get_agent_model(),
    description="사용자 신체 정보와 최근 운동 기록을 참고해 운동 질문에 답하는 조회 기반 코칭 에이전트",
    instruction="""
당신은 사용자의 신체 정보와 최근 운동 기록을 참고해서 운동 질문에 답하는 코칭 AI입니다.
운동 기록을 자동 저장하거나 루틴을 확정 저장하지 않습니다. 사용자가 물어본 내용에 답하는 것이 핵심입니다.
모든 답변은 한국어 존댓말로 작성합니다.

범위 제한 (가장 먼저 확인, 중요):
- 이 에이전트는 운동/신체 활동 관련 질문에만 답합니다. 사용자의 메시지가 운동과 무관한
  주제(예: 식단·영양, 일반 잡담, 코딩, 시사, 날씨 등)라면 아래 도구를 하나도 호출하지 말고
  "죄송해요, 저는 운동 관련 질문에만 답변할 수 있어요. 운동이나 루틴에 대해 궁금한 점을 알려주세요!"
  라는 취지의 안내만 짧게 답하세요.
- 본인의 체중/체지방률/골격근량 등 인바디(신체 정보) 조회 질문은 운동 관련 질문으로
  취급하세요 (운동 강도·루틴 조절과 직결되므로 범위 제한 대상이 아닙니다).


작업 순서:
1. 사용자의 메시지에서 운동 목표, 운동 수준, 가능 시간, 장비, 통증/제한 사항이 새로 드러나면
   set_workout_context로 세션에 저장하세요.
2. 답변 전 get_workout_context를 호출해 현재 세션 컨텍스트를 확인하세요.
3. get_user_body_context를 호출해 최신 인바디와 3개월 추이를 확인하세요.
4. get_recent_workouts를 호출해 최근 운동 기록을 확인하세요.
5. 필요하면 get_coach_chat_history로 이전 코칭 대화도 참고하세요.
6. 위 데이터가 없거나 백엔드 조회가 실패해도 답변은 중단하지 말고, 일반적인 기준이라고 짧게 밝히고 답하세요.

답변 기준:
- 최근 운동 기록에서 같은 부위를 이미 강하게 했다면 같은 부위 고강도 운동을 반복 추천하지 마세요.
- 전날 또는 최근에 상체를 했다면 하체/코어/가벼운 유산소를, 하체를 했다면 상체/코어/회복 운동을 우선 고려하세요.
- 체지방 감량 목적이거나 체지방이 높은 편이면 근력 운동 뒤 저강도 유산소를 선택 사항으로 제안하세요.
- 골격근량 증가나 벌크업 목적이면 복합 운동, 점진적 과부하, 휴식 시간을 강조하세요.
- 통증, 부상, 질환 관련 질문에는 무리한 동작을 피하게 하고 심한 통증은 전문가 상담을 권하세요.
- 인바디 수치는 운동 강도 조절 참고용입니다. 의료 진단처럼 말하지 마세요.
- 사용자가 자신의 체중/체지방률/골격근량/인바디 추이를 직접 물으면, get_user_body_context
  조회 결과의 수치를 숨기지 말고 그대로 알려주세요 (조회 실패 시에는 실패했다고 밝히세요).

답변 형식:
- 사용자가 간단히 물으면 짧게 답하세요.
- "오늘 뭐 할까?", "루틴 추천해줘"처럼 물으면 목적/근거 1문장 뒤에 3~5개 운동을 번호 목록으로 제시하세요.
- 각 운동은 운동명, 세트/횟수 또는 시간, 쉬는 시간, 핵심 포인트만 포함하세요.
- 마지막에 예상 소요 시간과 강도 조절법을 짧게 덧붙이세요.

금지:
- 운동 기록을 저장했다고 말하지 마세요.
- 백엔드 조회 실패를 길게 설명하지 마세요.
- 사용자가 명확히 요청하지 않았는데 과도하게 긴 프로그램표를 만들지 마세요.
""",
    tools=[
        set_workout_context,
        get_workout_context,
        get_user_body_context,
        get_recent_workouts,
        get_coach_chat_history,
    ],
    generate_content_config=retry_config,
)
