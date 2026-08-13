from google.adk.agents import Agent
from google.adk.planners import BuiltInPlanner
from google.adk.tools import ToolContext
from datetime import datetime
from dotenv import load_dotenv
import requests
import os
from google.genai import types  # 서버 과부하시 자동 재시도

from diet_agent.nutrition_tools import (
    ACTIVITY_FACTOR_ACTIVE,
    ACTIVITY_FACTOR_REST,
    FAT_RATIO,
    PROTEIN_RATIO_DEFAULT,
    PROTEIN_RATIO_DIET,
    SODIUM_DAILY_LIMIT_MG,
    find_recipes_by_ingredients as _shared_find_recipes_by_ingredients,
    get_meal_variety_options as _shared_get_meal_variety_options,
    search_food_nutrition,
    search_food_nutrition_batch,
)

# ADK가 자체적으로 쓰는 값(GOOGLE_API_KEY 등)과 달리, 우리가 os.getenv()로 직접
# 읽는 커스텀 환경변수(MFDS_API_KEY, BACKEND_BASE_URL 등)는
# .env를 명시적으로 로드해야 보입니다. adk web/adk run이 자체적으로 이미
# 로드했더라도 여기서 다시 불러도 안전합니다 (override=False가 기본이라
# 기존 값을 덮어쓰지 않음).
load_dotenv()


# =======================================================
# 제미나이 응답 생성 + 재시도 설정
# =======================================================
retry_config = types.GenerateContentConfig(
    http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(initial_delay=2, attempts=4),
    ),
    # 메뉴 다양성을 위해 약간 높은 temperature/top_p 사용 (너무 튀면 낮추세요)
    temperature=1.0,
    top_p=0.95,
)

# api.py의 /generate와 동일하게 thinking_budget을 제한한다 — automatic 기본값은
# 답변보다 훨씬 많은 내부 추론 토큰을 써서 응답이 크게 느려진다. agent.py도 같은
# 값을 써야 두 경로의 응답속도/품질을 동일한 조건에서 비교 테스트할 수 있다.
# api.py에서 7일 식단표 기준 3회 반복 측정한 결과 256이 1024보다 뚜렷이 빠르면서도
# (평균 13.3초 vs 20.2초) 3/3 성공했고, 0은 더 빠를 때도 있지만 JSON이 깨져
# 3번 중 2번 실패해서 쓰지 않는다 (2026-08-13, api.py의 _generate_with_gemini 주석
# 참고). ADK는 thinking_config를 generate_content_config가 아니라 planner로 넣어야
# 한다.
thinking_planner = BuiltInPlanner(thinking_config=types.ThinkingConfig(thinking_budget=256))

# =========================================================
# 백엔드 API 공통 설정 (APEXAI Healthcare 백엔드)
# =========================================================
# 로컬 개발 기준 기본값. 배포 시 환경변수로 덮어쓰세요.
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://localhost:8080")

# userId를 아직 앱 레벨에서 세션에 주입하는 로그인 흐름이 정해지지 않았으므로,
# 임시로 테스트용 고정 userId를 환경변수로 지정할 수 있게 해둠.
# (로그인/온보딩 흐름이 정해지면 register_user 대신 앱이 직접 state["user_id"]를 주입하는 방식으로 교체 권장)
DEFAULT_USER_ID = os.getenv("DEFAULT_USER_ID")


# =======================================================
# GET 요청 함수
# =======================================================
def _backend_get(path: str, params: dict | None = None) -> dict:
    try:
        resp = requests.get(f"{BACKEND_BASE_URL}{path}", params=params, timeout=5)
        if resp.status_code == 404:
            return {"status": "not_found"}
        resp.raise_for_status()
        return {"status": "success", "data": resp.json()}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =======================================================
# POST 요청 함수
# =======================================================

def _backend_post(path: str, body: dict) -> dict:
    try:
        resp = requests.post(f"{BACKEND_BASE_URL}{path}", json=body, timeout=5)
        if resp.status_code == 400:
            return {"status": "error", "message": resp.json().get("error", "입력값 오류"), "detail": resp.json()}
        if resp.status_code == 404:
            return {"status": "not_found"}
        resp.raise_for_status()
        return {"status": "success", "data": resp.json()}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =======================================================
# 사용자 ID 가져오기
# =======================================================

def _get_user_id(tool_context: ToolContext) -> int | None:
    """세션 state 또는 환경변수에서 userId를 가져옵니다. 없으면 None."""
    uid = tool_context.state.get("user_id")
    if uid:
        return uid
    if DEFAULT_USER_ID:
        tool_context.state["user_id"] = int(DEFAULT_USER_ID)
        return int(DEFAULT_USER_ID)
    return None


# =========================================================
# 사용자 등록 Tool (userId가 아직 없을 때만 필요)
# =========================================================

def register_user(email: str, nickname: str, tool_context: ToolContext) -> dict:
    """백엔드에 사용자를 생성하고 userId를 세션에 저장합니다.
    이미 세션에 userId가 있으면 호출할 필요 없습니다.

    Args:
        email: 사용자 이메일
        nickname: 사용자 닉네임
        tool_context: ADK가 자동으로 주입하는 컨텍스트 객체

    Returns:
        생성 결과 (userId 포함)
    """
    existing = _get_user_id(tool_context)
    if existing:
        return {"status": "already_registered", "user_id": existing}

    result = _backend_post("/api/users", {"email": email, "nickname": nickname})
    if result["status"] == "success":
        user_id = result["data"]["id"]
        tool_context.state["user_id"] = user_id
        return {"status": "success", "user_id": user_id}
    return result


# =========================================================
# 키와 몸무게 저장
# =========================================================

def set_user_profile(weight_kg: float, height_cm: float, body_fat_pct: float, tool_context: ToolContext) -> dict:
    """사용자의 체중, 키, 체지방률을 인바디 기록으로 백엔드에 저장합니다
    (백엔드 미연결 시 세션에만 저장). 영양 목표 계산에 사용합니다.

    Args:
        weight_kg: 체중 (kg)
        height_cm: 키 (cm)
        body_fat_pct: 체지방률 (%). 모르면 0을 전달하세요 (저장 안 함/기존 값 유지).
        tool_context: ADK가 자동으로 주입하는 컨텍스트 객체

    Returns:
        저장 결과를 담은 딕셔너리
    """
    # 세션 로컬 캐시는 백엔드 연결 실패 시 폴백용으로 항상 유지.
    # body_fat_pct가 0이면 "모름"으로 취급해서 기존에 저장된 값이 있으면 유지한다.
    existing = tool_context.state.get("user_profile") or {}
    profile = {
        "weight_kg": weight_kg,
        "height_cm": height_cm,
        "body_fat_pct": body_fat_pct if body_fat_pct > 0 else existing.get("body_fat_pct"),
    }
    tool_context.state["user_profile"] = profile

    user_id = _get_user_id(tool_context)
    if not user_id:
        return {
            "status": "success_local_only",
            **profile,
            "note": "userId가 없어 백엔드에는 저장되지 않았습니다 (register_user 먼저 호출 필요).",
        }

    body = {
        "userId": user_id,
        "measuredAt": datetime.now().isoformat(timespec="seconds"),
        "heightCm": height_cm,
        "weightKg": weight_kg,
    }
    if body_fat_pct > 0:
        body["bodyFatPct"] = body_fat_pct

    result = _backend_post("/api/inbody/records", body)
    if result["status"] == "success":
        return {"status": "success", **profile, "source": "backend"}
    # 백엔드 실패 시에도 로컬 캐시는 이미 저장되어 있으므로 완전 실패는 아님
    return {"status": "success_local_only", **profile, "backend_error": result}

# =======================================================
# 키와 몸무게 조회
# =======================================================
def get_user_profile(tool_context: ToolContext) -> dict:
    """저장된 사용자의 체중/키/체지방률 정보를 조회합니다. 인바디 최신 기록을 우선
    조회하고, 없으면 세션에 저장된 값을 사용합니다. 식단 추천 전에 항상 확인하세요.

    Returns:
        체중/키/체지방률 정보 (없으면 "미확인")
    """
    user_id = _get_user_id(tool_context)
    if user_id:
        result = _backend_get("/api/inbody/records/latest", {"userId": user_id})
        if result["status"] == "success":
            data = result["data"]
            profile = {
                "weight_kg": data.get("weightKg"),
                "height_cm": data.get("heightCm"),
                "body_fat_pct": data.get("bodyFatPct"),
            }
            tool_context.state["user_profile"] = profile
            return {"profile": profile, "measured_at": data.get("measuredAt"), "source": "backend"}

    # 백엔드에 기록이 없거나(404) userId가 없거나 백엔드 호출 실패 시 로컬 캐시로 폴백
    profile = tool_context.state.get("user_profile")
    if not profile:
        return {"profile": "미확인"}
    return {"profile": profile, "source": "local"}


# =========================================================
# 사용자 선호/목표 Tool (백엔드 스키마에 없는 항목 - 세션에만 저장)
# ---------------------------------------------------------
# 나이/성별/목표/알레르기/기피음식/예산/하루 식사 횟수는 현재 백엔드
# 사용자·인바디 API 스키마에 필드가 없습니다 (백엔드 DB는 그대로 두기로
# 했으므로, 여기서는 ADK 세션 state에만 저장합니다). 즉 세션이 끝나면
# 사라집니다 — 매 세션 초반에 get_user_preferences로 없으면 다시 물어봐야
# 합니다. 여러 세션에 걸쳐 남기려면 나중에 백엔드에 필드 추가를 요청하세요.
# =========================================================

# =======================================================
# 사용자 선호 저장
# =======================================================

def set_user_preferences(
    age: int,
    gender: str,
    goal: str,
    meals_per_day: int,
    budget_krw: int,
    allergies: list[str],
    disliked_foods: list[str],
    diet_direction: list[str],
    tool_context: ToolContext,
) -> dict:
    """나이/성별/목표/하루 예산/하루 식사 횟수/알레르기/기피 음식/식단 방향을
    세션에 저장합니다. 모르거나 해당 없는 값은 숫자는 0, 문자열은 빈 문자열,
    리스트는 빈 리스트로 전달하세요 — 그러면 기존에 저장돼 있던 값을 그대로
    유지합니다 (덮어쓰지 않음).

    Args:
        age: 나이 (모르면 0)
        gender: 성별 ("남성"/"여성"/기타, 모르면 "")
        goal: 목표 ("다이어트"/"린매스업"/"벌크업"/"유지" 등, 모르면 "")
        meals_per_day: 하루 식사 횟수 (모르면 0 → 기본 3끼로 취급)
        budget_krw: 하루 식비 예산(원) (모르면 0)
        allergies: 알레르기 식품 목록 (없으면 빈 리스트 [])
        disliked_foods: 싫어하는 음식 목록 (없으면 빈 리스트 [])
        diet_direction: 사용자가 말로 요청한 식단 방향 (예: ["가볍게", "저탄수",
            "저염", "단백질 위주"] 등, 없으면 빈 리스트 []). 알레르기처럼 누적
            저장되며, 대화가 끝날 때까지 계속 적용됩니다.
        tool_context: ADK가 자동으로 주입하는 컨텍스트 객체

    Returns:
        저장된 최종 선호 정보
    """
    prefs = dict(tool_context.state.get("user_preferences") or {})

    if age > 0:
        prefs["age"] = age
    if gender:
        prefs["gender"] = gender
    if goal:
        prefs["goal"] = goal
    if meals_per_day > 0:
        prefs["meals_per_day"] = meals_per_day
    if budget_krw > 0:
        prefs["budget_krw"] = budget_krw
    if allergies:
        # 새로 말한 알레르기는 기존 목록에 합침 (안전 관련이라 덮어쓰지 않고 누적)
        prefs["allergies"] = sorted(set(prefs.get("allergies", [])) | set(allergies))
    if disliked_foods:
        prefs["disliked_foods"] = sorted(set(prefs.get("disliked_foods", [])) | set(disliked_foods))
    if diet_direction:
        prefs["diet_direction"] = sorted(set(prefs.get("diet_direction", [])) | set(diet_direction))

    tool_context.state["user_preferences"] = prefs
    return {"status": "success", "preferences": prefs}

# =======================================================
# 사용자 선호 조회
# =======================================================

def get_user_preferences(tool_context: ToolContext) -> dict:
    """저장된 사용자 선호/목표 정보(나이/성별/목표/예산/식사횟수/알레르기/기피음식)를
    조회합니다. 식단 추천 전에 항상 확인하고, 없는 항목은 사용자에게 물어보세요.

    Returns:
        저장된 선호 정보 딕셔너리 (하나도 없으면 "미확인")
    """
    prefs = tool_context.state.get("user_preferences")
    if not prefs:
        return {"preferences": "미확인"}
    return {"preferences": prefs}


# =========================================================
# 영양성분 조회 · 재료 기반 레시피 추천 (식약처 공공데이터)
# ---------------------------------------------------------
# 실제 구현(식약처 식품영양성분DB, 조리식품 레시피 DB 호출 및 캐싱)은
# diet_agent/nutrition_tools.py에 있다 — api.py의 /generate stateless
# 서버와 정확히 같은 코드를 쓰기 위해서다 (agent.py에 따로 복사해두면
# FatSecret 제거·캐싱 추가 같은 수정을 두 군데서 반복해야 했다).
# search_food_nutrition은 시그니처가 완전히 같아서 그대로 가져다 쓰고,
# find_recipes_by_ingredients만 ADK 세션에 저장된 알레르기/기피 음식
# (user_preferences)을 자동으로 exclude_terms로 넘겨주는 얇은 래퍼를 둔다.
# =========================================================


def find_recipes_by_ingredients(ingredients: list[str], tool_context: ToolContext, max_results: int = 3) -> dict:
    """집에 있는 재료 목록으로 만들 수 있는 레시피를 찾아 추천합니다.
    사용자가 가진 재료가 레시피 재료 목록에 많이 겹치는 순으로 정렬해서
    상위 결과를 돌려줍니다. 저장된 알레르기/기피 음식(get_user_preferences)이
    재료 목록에 포함된 레시피는 자동으로 제외합니다 (안전을 위해 코드에서 직접
    필터링 - 단, 텍스트 일치 기반이라 100% 정확하지 않을 수 있으니 알레르기가
    있는 사용자에게는 항상 실제 재료를 한 번 더 확인하라고 안내하세요).

    Args:
        ingredients: 사용자가 가진 재료 이름 목록 (예: ["돼지고기", "김치", "두부"])
        tool_context: ADK가 자동으로 주입하는 컨텍스트 객체
        max_results: 반환할 최대 레시피 수 (기본 3개)

    Returns:
        매칭된 레시피 목록 (재료 일치 개수가 많은 순 정렬), 칼로리/영양성분/조리순서 포함
    """
    prefs = tool_context.state.get("user_preferences") or {}
    exclude_terms = list(set(prefs.get("allergies", [])) | set(prefs.get("disliked_foods", [])))
    return _shared_find_recipes_by_ingredients(ingredients, max_results, exclude_terms)

# =========================================================
# 식단/운동 기록 Tool
# ---------------------------------------------------------
# 주의: 현재 백엔드 API 명세에는 "식사 기록"이나 "운동 기록"을 저장/조회하는
# 엔드포인트가 없습니다 (사용자/인바디/채팅 3종만 존재). 따라서 아래 두 Tool은
# 이전과 동일하게 ADK 세션 state에만 기록되며, 세션이 끝나면 사라집니다.
# 여러 세션에 걸쳐 식사/운동 이력을 남기려면 김동규(백엔드)에게 관련 엔드포인트
# 추가를 요청하세요.
# =========================================================

# =======================================================
# 식사 기록
# =======================================================

def log_meal(
    meal_type: str,
    description: str,
    calories_kcal: float,
    protein_g: float,
    fat_g: float,
    carbs_g: float,
    sodium_mg: float,
    tool_context: ToolContext,
) -> dict:
    """사용자가 섭취한 식사 정보를 영양소 수치와 함께 세션 상태에 기록합니다.
    영양소는 search_food_nutrition으로 먼저 조회한 값을 넘기세요 — 이 값들이
    있어야 get_daily_nutrition_summary로 오늘 하루 부족/과다를 계산할 수 있습니다.
    모르는 값은 0으로 전달하세요 (합산에서 빠집니다).

    Args:
        meal_type: 식사 구분 (예: "아침", "점심", "저녁", "간식")
        description: 먹은 음식에 대한 간단한 설명
        calories_kcal: 칼로리 (모르면 0)
        protein_g: 단백질(g) (모르면 0)
        fat_g: 지방(g) (모르면 0)
        carbs_g: 탄수화물(g) (모르면 0)
        sodium_mg: 나트륨(mg) (모르면 0)
        tool_context: ADK가 자동으로 주입하는 컨텍스트 객체

    Returns:
        기록 결과를 담은 딕셔너리
    """
    history = tool_context.state.get("meal_history", [])
    history.append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "meal_type": meal_type,
        "description": description,
        "nutrition": {
            "calories_kcal": calories_kcal or None,
            "protein_g": protein_g or None,
            "fat_g": fat_g or None,
            "carbs_g": carbs_g or None,
            "sodium_mg": sodium_mg or None,
        },
    })
    history = history[-14:]  # 최근 14일치만 유지
    tool_context.state["meal_history"] = history

    return {"status": "success", "logged": history[-1]}

# =======================================================
# 최근 식사 조회
# =======================================================

def get_recent_meals(tool_context: ToolContext) -> dict:
    """최근 식사 기록을 조회합니다. 식단 추천 전에 사용합니다.

    Returns:
        최근 식사 기록 리스트
    """
    return {"recent_meals": tool_context.state.get("meal_history", [])}

# =======================================================
# 최근 운동 조회
# =======================================================
def get_recent_workouts_for_diet(tool_context: ToolContext) -> dict:
    """최근 운동 기록을 조회합니다. 운동 강도에 맞는 식단(특히 단백질/칼로리)을
    추천할 때 참고합니다.

    Returns:
        최근 운동 기록 리스트
    """
    return {"recent_workouts": tool_context.state.get("workout_history", [])}


# =========================================================
# 오늘의 영양 분석 Tool
# ---------------------------------------------------------
# log_meal에 같이 저장된 영양소 수치를 오늘 날짜 기준으로 합산하고, 목표치와
# 비교해서 부족/과다를 계산합니다. 백엔드 DB 변경 없이 세션 state(meal_history)
# 데이터만으로 동작합니다.
#
# 목표치 계산: 나이/성별/체중/키가 다 있으면 Mifflin-St Jeor 공식 + 목표
# (다이어트/린매스업/벌크업) 보정으로 칼로리·단백질·지방 목표를 계산하고,
# 없으면 체중 기반의 단순 배수로 대체합니다 (지시문의 계산 기준과 동일한 로직을
# 코드로도 한 번 더 구현해서, LLM 암산 오차 없이 정확한 숫자를 줍니다).
#
# 나트륨: 한국인 영양소 섭취기준(KDRI) 만성질환위험감소섭취량 2,000mg/일을
# 상한으로 사용합니다. MFDS 조회 결과(AMT_NUM13, 2026-07-14 검증)에서 나트륨을
# 가져옵니다. MFDS DB에 없는 음식만 먹은 날은 나트륨이 하나도 안 잡혀
# "추적 안 됨"으로 표시될 수 있습니다.
#
# 비타민: 아직 지원하지 않습니다 (별도 필드 검증 필요).
# =========================================================

_REFERENCE_PROTEIN_FOOD = {"name": "닭가슴살", "protein_g_per_100g": 23}  # 부족분 제안용 참고 식품


def _compute_daily_targets(profile: dict, prefs: dict, has_recent_workout: bool) -> dict:
    """저장된 프로필/선호 정보로 하루 칼로리·단백질·지방 목표를 계산합니다.
    지시문의 계산 기준과 동일한 로직 (Mifflin-St Jeor + 목표 보정, 또는
    체중 기반 단순 배수 폴백)."""
    weight = profile.get("weight_kg")
    height = profile.get("height_cm")
    age = prefs.get("age")
    gender = prefs.get("gender")
    goal = prefs.get("goal") or ""

    calorie_target = None
    protein_target = None
    fat_target = None

    if weight:
        if age and gender and height:
            if gender == "남성":
                bmr = 10 * weight + 6.25 * height - 5 * age + 5
            else:
                bmr = 10 * weight + 6.25 * height - 5 * age - 161
            activity_factor = ACTIVITY_FACTOR_ACTIVE if has_recent_workout else ACTIVITY_FACTOR_REST
            tdee = bmr * activity_factor
            goal_adjust = {"다이어트": -400, "린매스업": 250, "벌크업": 450}.get(goal, 0)
            calorie_target = tdee + goal_adjust

            protein_ratio = PROTEIN_RATIO_DIET if goal == "다이어트" else PROTEIN_RATIO_DEFAULT
            protein_target = calorie_target * protein_ratio / 4
            fat_target = calorie_target * FAT_RATIO / 9
        else:
            multiplier = 1.8 if has_recent_workout else 1.3
            protein_target = weight * multiplier
            fat_target = weight * 0.9  # 칼로리 목표가 없을 때의 대략적인 지방 상한 추정치

    return {
        "calorie_target_kcal": round(calorie_target) if calorie_target else None,
        "protein_target_g": round(protein_target) if protein_target else None,
        "fat_target_g": round(fat_target) if fat_target else None,
        "sodium_limit_mg": SODIUM_DAILY_LIMIT_MG,
    }


def get_daily_nutrition_summary(tool_context: ToolContext) -> dict:
    """오늘 log_meal로 기록된 식사들의 영양소를 합산하고, 목표치 대비
    단백질 부족/지방 과다/나트륨 과다를 계산합니다. 식단 최종 답변을 만들기
    전에 호출해서 "오늘 영양 분석" 코멘트를 만드는 데 사용하세요.

    Returns:
        오늘 섭취 총량, 목표치, 부족/과다 수치 및 간단한 제안 문구
    """
    today = datetime.now().strftime("%Y-%m-%d")
    history = tool_context.state.get("meal_history", [])
    today_meals = [m for m in history if m.get("date") == today]

    consumed = {"calories_kcal": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0, "sodium_mg": 0.0}
    sodium_tracked = False
    for m in today_meals:
        nut = m.get("nutrition") or {}
        for key in ("calories_kcal", "protein_g", "fat_g", "carbs_g"):
            v = nut.get(key)
            if v is not None:
                consumed[key] += v
        v = nut.get("sodium_mg")
        if v is not None:
            consumed["sodium_mg"] += v
            sodium_tracked = True

    profile = tool_context.state.get("user_profile") or {}
    prefs = tool_context.state.get("user_preferences") or {}
    has_recent_workout = bool(tool_context.state.get("workout_history"))

    targets = _compute_daily_targets(profile, prefs, has_recent_workout)

    result = {
        "date": today,
        "meals_logged_today": len(today_meals),
        "consumed": {k: round(v, 1) for k, v in consumed.items()},
        "targets": targets,
    }

    if targets["protein_target_g"] is not None:
        deficit = targets["protein_target_g"] - consumed["protein_g"]
        result["protein_deficit_g"] = round(deficit, 1) if deficit > 0 else 0
        if deficit > 0:
            suggested_g = max(10, round((deficit / _REFERENCE_PROTEIN_FOOD["protein_g_per_100g"]) * 100 / 10) * 10)
            result["protein_suggestion"] = (
                f"{_REFERENCE_PROTEIN_FOOD['name']} {suggested_g}g 정도를 추가하면 "
                f"부족한 단백질 약 {result['protein_deficit_g']}g을 채울 수 있습니다."
            )

    if targets["fat_target_g"] is not None:
        excess = consumed["fat_g"] - targets["fat_target_g"]
        result["fat_excess_g"] = round(excess, 1) if excess > 0 else 0

    if sodium_tracked:
        excess = consumed["sodium_mg"] - SODIUM_DAILY_LIMIT_MG
        result["sodium_excess_mg"] = round(excess, 1) if excess > 0 else 0
    else:
        result["sodium_note"] = "오늘 기록된 식사 중 나트륨 수치가 있는 항목이 없어 나트륨은 계산하지 못했습니다."

    result["vitamin_note"] = "비타민 섭취량 분석은 아직 지원하지 않습니다."

    return result


# =========================================================
# 식단 다양성 Tool
# ---------------------------------------------------------
# LLM이 프롬프트 예시("닭가슴살+현미밥+브로콜리")를 매번 그대로 재사용하는 걸
# 막기 위한 장치. 실제 후보 풀/샘플링 로직은 nutrition_tools.py의
# get_meal_variety_options에 있다 (api.py의 7일 식단표도 같은 문제를 겪어서
# 공유 모듈로 옮김). 여기서는 세션에 기록된 최근 식사(get_recent_meals)에서
# recent_text를 만들어 넘겨주는 얇은 래퍼만 둔다.
# =========================================================

def get_meal_variety_options(tool_context: ToolContext) -> dict:
    """식단을 다양하게 구성하기 위한 재료/조리법 후보를 무작위로 제공합니다.
    최근 식사 기록에 이미 나온 재료는 후보에서 제외됩니다.
    남은 식사 메뉴를 짤 때마다 이 도구를 호출해서 매번 다른 조합을 참고하세요.

    Returns:
        단백질/탄수화물/채소/조리법/음식 스타일 후보 리스트 (매번 무작위로 섞임)
    """
    recent = tool_context.state.get("meal_history", [])
    recent_text = " ".join(m.get("description", "") for m in recent[-6:])
    return _shared_get_meal_variety_options(recent_text)


# =========================================================
# 채팅 이력 Tool (백엔드 /api/chat 연동)
# ---------------------------------------------------------
# agentType은 백엔드에 하드코딩된 허용 목록(DIET, COACH)을 따릅니다.
# 이 에이전트는 영양/식단 담당이므로 항상 "DIET"로 고정합니다.
#
# 설계 메모: 사용자의 발화는 이 Tool로 에이전트가 직접 저장합니다.
# 반면 에이전트의 "최종 답변"은 에이전트 스스로 저장하도록 지시하면
# 같은 텍스트를 두 번 생성/호출해야 해서 번거롭고 누락 위험이 있습니다.
# 최종 답변 로깅은 이 Agent를 호출하는 앱/러너 쪽에서 runner.run()의
# 반환값을 받은 직후 POST /api/chat (agentType="DIET")로 저장하는 것을
# 권장합니다.
# =========================================================


# =======================================================
# 사용자 메시지 저장
# =======================================================
def save_chat_message(message: str, tool_context: ToolContext) -> dict:
    """사용자의 발화를 백엔드 채팅 이력에 저장합니다 (agentType=DIET 고정).
    userId가 없으면 저장을 건너뜁니다.

    Args:
        message: 저장할 메시지 원문 (사용자가 실제로 입력한 내용)
        tool_context: ADK가 자동으로 주입하는 컨텍스트 객체

    Returns:
        저장 결과
    """
    user_id = _get_user_id(tool_context)
    if not user_id:
        return {"status": "skipped", "reason": "userId 없음 (register_user 먼저 호출)"}

    return _backend_post(
        "/api/chat",
        {"userId": user_id, "agentType": "DIET", "message": message},
    )

# =======================================================
# 이전 채팅 조회
# =======================================================
def get_chat_history(tool_context: ToolContext) -> dict:
    """백엔드에 저장된 이 에이전트(DIET)와의 대화 이력을 조회합니다.
    세션이 새로 시작되어 이전 대화 맥락이 필요할 때 사용합니다.

    Returns:
        시간순 대화 이력 리스트
    """
    user_id = _get_user_id(tool_context)
    if not user_id:
        return {"history": []}

    result = _backend_get("/api/chat", {"userId": user_id, "agentType": "DIET"})
    if result["status"] == "success":
        return {"history": result["data"]}
    return {"history": [], "error": result}


# =========================================================
# 식단 에이전트 (root_agent)
# =========================================================

root_agent = Agent(
    name="diet_coaching_agent",
    model="gemini-2.5-flash",
    description="사용자의 운동 상태와 생활 패턴을 고려해 식단을 추천하고 관리하는 영양 코칭 에이전트",
    instruction="""
당신은 사용자의 식사 기록과 운동 상태를 바탕으로 맞춤형 식단을 조언하는 영양 코치입니다.

작업 순서 (반드시 이 순서대로 수행하세요):
1. 사용자가 먹은 것을 말하면 먼저 search_food_nutrition으로 칼로리/단백질/지방/
   탄수화물(가능하면 나트륨도)을 조회하세요.
2. 조회한 수치를 그대로 log_meal에 넘겨서 기록하세요 (조회가 안 된 항목은 0으로
   전달 — 그래야 오늘 합산 계산에서 정확히 빠집니다).
3. save_chat_message로 사용자의 발화 원문을 백엔드에 저장하세요 (userId가 없어서
   skipped로 나와도 무시하고 계속 진행하세요).
4. 기록 직후, 사용자가 추가로 묻지 않아도 반드시 get_recent_meals와
   get_recent_workouts_for_diet를 호출해서 최근 식사/운동 데이터를 가져오세요.
5. get_user_profile로 체중/키/체지방률을, get_user_preferences로 나이/성별/목표/
   예산/하루 식사 횟수/알레르기/기피 음식을 확인하세요.
6. get_daily_nutrition_summary를 호출해서 오늘 하루 부족/과다(단백질 부족,
   지방 과다, 나트륨 과다)를 계산하세요.
7. 남은 식사 메뉴를 짜기 전에 매번 get_meal_variety_options를 호출해서 재료/조리법
   후보를 받으세요. 이 도구는 호출할 때마다 무작위로 다른 후보를 주고, 최근 며칠간
   먹은 재료는 후보에서 제외해줍니다.
8. 아래 응답 형식에 맞춰 답변을 작성하세요.

다양성 규칙 (중요):
- 아래 "응답 형식"의 예시 메뉴("닭가슴살+현미밥+브로콜리")는 형식을 보여주기 위한
  것일 뿐입니다. 실제 답변에서 이 조합을 그대로 반복하지 마세요.
- get_recent_meals로 확인한 최근 며칠간 이미 제안했던 단백질원/탄수화물원/조리법과
  겹치지 않게 구성하세요.
- 매 끼니, 매 대화마다 단백질원(육류/생선/해산물/콩류/유제품 등), 탄수화물원, 채소,
  조리법(구이/찜/볶음/국/샐러드/덮밥 등), 음식 스타일(한식/일식/양식/동남아식 등)을
  돌아가며 바꿔서 제안하세요. get_meal_variety_options가 준 후보를 우선 활용하되,
  꼭 그 목록 안에서만 골라야 하는 건 아닙니다.
- 같은 끼니 유형(예: 점심)이라도 어제와 오늘 메뉴가 겹치지 않도록 하세요.
- 사용자가 대화 중 식단 방향(가볍게/저탄수/저염/단백질 위주/예산 낮게 등)을
  말하면 set_user_preferences의 diet_direction으로 저장하고, 이후 이 대화의
  모든 끼니 추천(오늘 남은 끼니든 7일 식단표든)에 그 방향을 계속 반영하세요 —
  다시 언급하지 않아도 대화가 끝날 때까지 유지됩니다.

사용자 등록 규칙:
- get_user_profile이나 다른 도구가 "userId 없음"류의 결과를 반환해도 대화를 막지 말고
  일반적인 성인 기준으로 계속 진행하세요.
- 만약 사용자가 먼저 회원가입/등록을 요청하면 이메일과 닉네임을 받아 register_user를
  호출하세요.

체중/키/체지방률 확인 규칙 (중요):
- get_user_profile 결과가 "미확인"이면, 이번 응답 끝에 짧게 체중과 키를 물어보세요.
  (예: "정확한 목표 칼로리/단백질 계산을 위해 체중(kg)과 키(cm)를 알려주실 수 있나요?")
  이때는 일반적인 성인 기준(단백질 100~120g)으로 임시 추천하되, 추정치임을 밝히세요.
- 체지방률은 몰라도 괜찮습니다 (있으면 체지방 제외 체중 기준으로 단백질을 더 정교하게
  계산하는 데 참고만 하고, 없다고 되묻지는 마세요).
- 사용자가 체중/키/체지방률을 답하면 set_user_profile로 즉시 저장하세요
  (모르는 값은 body_fat_pct=0 처럼 0으로 전달).
- 프로필이 이미 저장되어 있다면 다시 묻지 말고, 저장된 값을 기준으로 계산하세요.

나이/성별/목표/예산/식사횟수/알레르기/기피음식 확인 규칙:
- get_user_preferences 결과가 "미확인"이거나 일부 항목이 비어있으면, 처음 한두 번의
  대화에서만 자연스럽게 물어보세요 (한 번에 다 묻지 말고 "목표가 다이어트/린매스업/
  벌크업/유지 중 어디에 가까우신가요?" 처럼 대화 맥락에 맞게 1~2개씩). 매번 반복해서
  묻지 마세요 — 한 번 답하면 set_user_preferences로 저장하고 이후엔 그 값을 그대로 씀.
- 알레르기/기피 음식은 안전과 직결되므로, 언급되면 되묻지 말고 즉시
  set_user_preferences로 저장하세요.
- 나이/성별/목표를 모르면 아래 "추론 및 조정 기준"의 기본값(체중 기반 공식)으로
  계산하고 추정치임을 밝히세요. 필수로 요구하지는 마세요.

응답 형식 (중요, 다음 내용을 모두 포함):
- 방금 먹은 음식의 대략적인 칼로리 및 영양성분(칼로리/탄단지)을 알려주세요.
- get_daily_nutrition_summary 결과를 바탕으로 "오늘 영양 분석" 한두 문장을 넣으세요.
  예: "오늘은 단백질이 35g 부족합니다. 닭가슴살 150g 정도를 추가하면 좋습니다."
  protein_suggestion 필드가 있으면 그 문장을 그대로/자연스럽게 다듬어서 쓰세요.
  fat_excess_g가 0보다 크면 "지방 섭취가 목표보다 Xg 많습니다" 식으로 알려주세요.
  sodium_excess_mg가 0보다 크면 나트륨 과다도 언급하고, sodium_note가 있으면
  (나트륨 데이터가 없는 경우) 나트륨은 이번엔 계산 못 했다고 짧게만 언급하세요.
  비타민은 아직 분석 안 되니, 사용자가 비타민을 콕 집어 물어보지 않는 한 굳이
  먼저 언급하지 마세요.
- 오늘 남은 식사에 대해 각각 구체적인 메뉴와 목표 섭취량(g 단위)을 제시하세요.
  이미 지나간 식사는 빼고, 남은 식사 개수는 get_user_preferences의 meals_per_day를
  따르세요 (없으면 기본 3끼: 아침/점심/저녁 + 필요시 간식 1~2회).
- 남은 식사는 마크다운 표로 정리하세요. 컬럼: 끼니 | 메뉴 | 목표 섭취량 | 칼로리 |
  탄수화물 | 단백질 | 지방. 예:

  | 끼니 | 메뉴 | 목표 섭취량 | 칼로리 | 탄수화물 | 단백질 | 지방 |
  |---|---|---|---|---|---|---|
  | 점심 | 닭가슴살 + 현미밥 + 브로콜리 | 150g + 120g + 100g | 450kcal | 45g | 38g | 8g |

- 끝에 하루 전체 목표를 한 줄로 요약하세요 — 총 칼로리, 총 단백질(g)에 더해
  탄수화물/단백질/지방 비율(%)도 함께 제시하세요 (예: "총 1800kcal, 단백질 130g
  (탄수화물 45% · 단백질 30% · 지방 25%)").
- 예산이 저장돼 있고 낮은 편이면, 상대적으로 저렴한 단백질원(두부/계란/닭가슴살/
  콩류 등) 위주로 짜고 그 이유를 한 줄 언급하세요.
- 알레르기/기피 음식은 어떤 경우에도 메뉴에 넣지 마세요.
- 칭찬이나 격려는 짧게 한 줄로만 하고, 반드시 위 내용을 포함해서 답하세요.

금지 사항:
- "기록했어요! 다른 거 궁금하신가요?" 같은, 영양정보·추천 없이 사용자에게
  되묻기만 하는 응답으로 끝내지 마세요.
- 도구 조회 없이 추천하지 마세요. 반드시 1→2→3→4→5→6→7→8 순서를 지키세요.
- "단백질을 더 드세요"처럼 메뉴/양 없이 추상적으로만 권장하는 답변은 금지합니다.
- 알레르기가 있다고 말한 재료를 실수로라도 메뉴에 포함시키지 마세요.

추론 및 조정 기준:
- 나이/성별/체중/키가 모두 있으면 Mifflin-St Jeor 공식으로 기초대사량(BMR)을 계산:
  남성 BMR = 10×체중(kg) + 6.25×키(cm) − 5×나이 + 5
  여성 BMR = 10×체중(kg) + 6.25×키(cm) − 5×나이 − 161
  그 다음 최근 운동 기록을 참고해 활동계수(운동 거의 없음 1.2, 가벼운 활동 1.375,
  보통 활동 1.55, 활발함 1.725)를 곱해 유지 칼로리(TDEE)를 추정하세요.
- 목표에 따라 TDEE를 보정: 다이어트는 -300~500kcal, 린매스업은 +200~300kcal,
  벌크업은 +400~500kcal, 유지는 보정 없음. 목표를 모르면 유지로 취급.
- 나이/성별 정보가 없으면 기존 방식대로: 운동 강도가 높았다면 체중(kg) × 1.6~2.0g을
  하루 단백질 목표로 계산 (예: 체중 65kg → 약 104~130g), 쉬는 날이면
  체중(kg) × 1.2~1.4g. 이 경우 총 칼로리는 "대략적인 추정치"라고 명확히 밝히세요.
- 탄단지 비율은 목표에 따라 대략: 다이어트는 탄수화물40%/단백질35%/지방25%,
  벌크업/린매스업은 탄수화물45%/단백질30%/지방25%, 유지는 탄수화물50%/단백질25%/
  지방25% 정도를 기본값으로 삼고, 계산된 그램 수와 맞춰서 조정하세요.
- 체중이 미확인인 경우: 일반적인 성인 기준(단백질 100~120g)으로 임시 계산하고 추정치임을 밝힘
- 이미 섭취한 칼로리/영양성분을 고려해서 남은 식사의 양을 조정
- 사용자가 피로하거나 컨디션이 안 좋다고 하면, 회복에 도움이 되는 식품 위주로 제안

주의사항:
- 특정 질병(당뇨, 신장질환 등)과 관련된 식단 제한이 의심되면 일반적인 조언만 하고
  반드시 의사나 영양사 상담을 권유
- 극단적인 칼로리 제한이나 단식을 권장하지 않음
- 항상 추천 근거를 사용자에게 설명 (왜 이 메뉴/양을 권장하는지)
- Mifflin-St Jeor 등 공식으로 계산된 값도 어디까지나 추정치이며, 실제 건강 상태나
  질환에 따라 다를 수 있음을 필요시 알려주세요.

재료 기반 레시피 추천 (별도 흐름):
- 사용자가 "집에 이런 재료 있는데 뭐 해먹지" 식으로 물어보면, 위의 식사 기록
  작업 순서(1~7번) 대신 이 흐름을 따르세요.
- 언급한 재료들을 리스트로 정리해서 find_recipes_by_ingredients를 호출하세요.
- 결과가 여러 개면, 그중 사용자 목표(칼로리/단백질, get_user_profile 참고)에
  더 잘 맞는 것을 우선 추천하고, 나머지는 대안으로 짧게 언급하세요.
- 추천할 때 레시피명, 겹치는 재료, 대략적인 칼로리/단백질, 조리순서 핵심 2~3단계를
  요약해서 알려주세요 (steps 필드 전체를 그대로 나열하지 말고 간추리세요).
- 매칭되는 레시피가 없으면(not_found), 가진 재료로 대체 가능한 다른 재료를
  일반 지식으로 제안하거나, 재료를 더 말해달라고 요청하세요.
- find_recipes_by_ingredients는 저장된 알레르기/기피 음식이 재료에 포함된 레시피를
  자동으로 걸러냅니다 (filtered_allergens_or_disliked 필드 참고). 단, 텍스트 일치
  기반이라 100% 정확하지 않을 수 있으니, 알레르기가 있는 사용자에게는 "레시피 재료를
  한 번 더 직접 확인해달라"고 짧게 안내하세요.
- 이 흐름에서도 log_meal/search_food_nutrition을 강제로 쓸 필요는 없습니다 —
  아직 먹은 게 아니라 "뭘 해먹을지 고르는" 단계이기 때문입니다.

외식/프랜차이즈 메뉴 추천 (별도 흐름):
- 사용자가 "오늘 맥도날드 가는데", "버거킹에서 뭐 먹지", "스타벅스 가는데 뭐 마실까"
  처럼 특정 음식점/프랜차이즈를 언급하며 메뉴를 물으면, 위의 식사 기록 작업
  순서(1~7번) 대신 이 흐름을 따르세요.
- 별도의 메뉴 DB 도구는 없습니다. 언급된 음식점의 실제 판매 메뉴 중 사용자 상황에
  맞을 만한 후보를 당신의 지식으로 1~3개 떠올리세요 (예: 맥도날드 → 그릴드 치킨버거,
  상하이버거, 맥모닝 등). 단, 단종되었거나 확실하지 않은 메뉴명은 피하고, 그 브랜드가
  실제로 팔 것 같은 메뉴명만 후보로 삼으세요.
- 후보 각각에 대해 search_food_nutrition을 호출해 실제 칼로리/단백질/지방/탄수화물
  (가능하면 나트륨)을 조회하세요. 검색어에 "맥도날드 그릴드치킨버거"처럼 브랜드명을
  붙여도 되고, 결과가 없으면 브랜드명을 빼고 일반 메뉴명으로도 시도해보세요.
  숫자 없이 추천하지 마세요 — 조회 결과 없이 아는 대로 칼로리를 지어내지 마세요.
- get_daily_nutrition_summary로 오늘 남은 칼로리/단백질 여유와 나트륨 상태를 확인하고,
  get_user_preferences로 목표(다이어트/린매스업/벌크업/유지)와 알레르기/기피 음식을
  확인해서 후보 중 가장 적합한 것 하나를 고르세요 (목표가 다이어트/린매스업이면 단백질
  대비 칼로리가 좋은 쪽, 나트륨이 이미 과다면 짠 메뉴나 소스 많은 메뉴는 피하기 등).
- 알레르기/기피 음식과 겹치는 후보는 절대 추천하지 마세요.
- 답변은 짧게: 추천 메뉴(세트 구성 포함 가능, 예: "그릴드 치킨버거 + 제로콜라")와
  그렇게 고른 이유 한 줄, 대략적인 칼로리/단백질 수치를 함께 제시하세요. 대안이 있으면
  한 줄로만 짧게 덧붙이세요.
- 이 흐름은 "먹기 전 추천"이므로 log_meal을 자동으로 호출하지 마세요. 사용자가 그
  메뉴를 실제로 먹었다고 이후에 말하면(예: "방금 그거 먹었어") 그때 일반 식사 기록
  흐름(1~7번)을 따라 search_food_nutrition/log_meal로 기록하세요.

주간(7일) 식단표 요청 (별도 흐름):
- 사용자가 "일주일 식단표 짜줘", "이번주 식단표", "7일치 식단 계획해줘"처럼 여러
  날에 걸친 식단표를 명확히 요청하면, 위의 식사 기록 작업 순서(1~7번) 대신 이
  흐름을 따르세요.
1. get_user_profile과 get_user_preferences를 호출해서 체중/키/체지방률, 나이/
   성별/목표/예산/하루 식사횟수/알레르기/기피 음식/diet_direction(이전에 말한
   식단 방향)을 확인하세요. 체중/키가 미확인이면 평소처럼 짧게 물어보되, 답을
   안 줘도 일반 성인 기준으로 계속 진행하세요.
2. get_recent_meals로 오늘 날짜(date가 오늘)에 이미 기록된 끼니가 있는지
   확인하세요. 있는 슬롯은 실제로 먹은 것이니 그대로 유지하고 절대 다른 메뉴로
   바꾸지 마세요 (표에는 "(이미 드신 식사)"로 표시). 그 외 모든 요일/슬롯은
   자유롭게 새로 구성하세요.
3. 이번 요청에서 식단 방향(가볍게/저탄수/저염/단백질 위주/예산 낮게 등)을
   말했다면 set_user_preferences의 diet_direction으로 저장하세요. 이전에 이미
   저장된 diet_direction이 있다면 이번에 다시 언급하지 않았어도 계속 적용하고,
   표 전체(이미 먹은 끼니 제외)를 그 방향에 맞게 구성하세요. 예: "가볍게" →
   전반적으로 저칼로리·저지방 메뉴, "저탄수" → 탄수화물 비중 낮은 메뉴, "저염" →
   국/찌개·젓갈·장류 위주 메뉴 지양, "단백질 위주" → 매 끼니 단백질원 비중 확대,
   "예산 낮게" → 두부/계란/닭가슴살/콩류 등 저렴한 단백질원 위주.
4. 7일 × meals_per_day(미확인이면 기본 3끼: 아침/점심/저녁) 만큼 메뉴 이름을
   먼저 다 정하세요. get_meal_variety_options를 여러 번(예: 요일마다 한 번 정도)
   호출해서 단백질원/탄수화물원/채소/조리법/음식 스타일이 요일마다, 같은 끼니
   유형(예: 매일 점심)끼리도 겹치지 않게 다양성을 확보하세요.
5. 메뉴 이름이 모두 정해지면 search_food_nutrition_batch를 대화당 정확히 한
   번만 호출해서, 중복 제거한 전체 메뉴 이름 목록의 칼로리/영양성분을 한 번에
   조회하세요. 메뉴마다 따로따로 search_food_nutrition을 부르지 마세요 — 7일치
   메뉴를 하나씩 조회하면 응답 시간이 크게 늘어납니다. 일부 메뉴가 not_found/
   error로 나와도 다른 이름으로 바꿔 재조회하지 말고, 그 메뉴명은 유지한 채
   합리적인 대략치를 쓰고 답변 끝에 "일부 메뉴는 정확한 조회값이 없어 대략치를
   사용했다"고 짧게 밝히세요.
6. 알레르기/기피 음식은 어떤 요일에도 절대 포함하지 마세요.
7. 응답 형식: 월~일 7일 × 끼니를 한 줄씩, 하나의 마크다운 표로 정리하세요.
   컬럼: 요일 | 끼니 | 메뉴 | 칼로리 | 탄수화물 | 단백질 | 지방. 오늘 요일에서
   이미 log_meal로 기록된 슬롯은 메뉴 칸 끝에 "(이미 드신 식사)"를 붙이세요.
   예:

   | 요일 | 끼니 | 메뉴 | 칼로리 | 탄수화물 | 단백질 | 지방 |
   |---|---|---|---|---|---|---|
   | 월 | 아침 | 현미밥 + 닭가슴살 + 브로콜리 | 450kcal | 45g | 38g | 8g |
   | 월 | 점심 | 삼겹살 200g (이미 드신 식사) | 680kcal | 0g | 34g | 60g |
   | 월 | 저녁 | 두부 스테이크 + 잡곡밥 | 520kcal | 55g | 32g | 14g |
   | 화 | 아침 | ... | ... | ... | ... | ... |

   표 끝에 하루 평균 칼로리·단백질과 탄수화물/단백질/지방 비율(%)을 요약하고,
   diet_direction을 반영했다면 어떤 방향을 반영했는지 한 줄로 언급하세요.
8. 이 흐름에서는 log_meal을 자동으로 호출하지 마세요 (아직 안 먹은 "계획"이지
   기록이 아닙니다) — 오늘 이미 log_meal로 기록된 슬롯만 2번처럼 그대로 반영하고,
   나머지는 계획으로만 제시하세요.
- 사용자가 "다시 짜줘", "이번엔 저탄수로"처럼 표 전체를 다시 요청하면, 최신
  diet_direction을 반영해 표 전체(이미 먹은 끼니 제외)를 다시 구성하되, 1~8번
  순서를 다시 따르세요.
""",
    tools=[log_meal, get_recent_meals, get_recent_workouts_for_diet, search_food_nutrition,
           search_food_nutrition_batch, set_user_profile, get_user_profile, register_user,
           save_chat_message, get_chat_history, get_meal_variety_options, find_recipes_by_ingredients,
           set_user_preferences, get_user_preferences, get_daily_nutrition_summary],
    generate_content_config=retry_config,
    planner=thinking_planner,
)
