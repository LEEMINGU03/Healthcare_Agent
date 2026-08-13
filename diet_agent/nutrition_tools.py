from __future__ import annotations

import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from diet_agent.config import load_project_dotenv

load_project_dotenv()

# =========================================================
# 영양 목표 계산에 쓰는 공통 상수
# ---------------------------------------------------------
# agent.py(_compute_daily_targets)와 api.py(_compute_nutrition_targets)가 같은
# 활동계수/비율/나트륨 상한을 쓰도록 여기 하나로 모아둔다 — 두 군데서 각자
# 숫자를 들고 있으면 한쪽만 고쳤을 때 슬쩍 어긋나기 쉽다.
# =========================================================

SODIUM_DAILY_LIMIT_MG = 2000  # KDRI 나트륨 만성질환위험감소섭취량(성인 기준)
ACTIVITY_FACTOR_REST = 1.2
ACTIVITY_FACTOR_ACTIVE = 1.55
PROTEIN_RATIO_DIET = 0.35
PROTEIN_RATIO_DEFAULT = 0.30
FAT_RATIO = 0.25

# =========================================================
# 식약처 식품영양성분DB (data.go.kr, 한식 위주)
# agent.py의 원래 구현을 그대로 가져오되, ToolContext/ADK 세션 의존성을 제거해서
# /generate처럼 세션 없는 stateless 호출에서도 그대로 쓸 수 있게 했다.
# =========================================================

MFDS_API_KEY = os.getenv("MFDS_API_KEY")
MFDS_ENDPOINT = "https://apis.data.go.kr/1471000/FoodNtrCpntDbInfo02/getFoodNtrCpntDbInq02"


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _search_mfds_food(food_name: str, num_rows: int = 5) -> dict:
    if not MFDS_API_KEY:
        return {"status": "error", "message": "MFDS_API_KEY가 설정되지 않았습니다."}

    try:
        resp = requests.get(
            MFDS_ENDPOINT,
            params={
                "serviceKey": MFDS_API_KEY,
                "type": "json",
                "numOfRows": num_rows,
                "pageNo": 1,
                "FOOD_NM_KR": food_name,
            },
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        header = data.get("header", {})
        if header.get("resultCode") != "00":
            return {"status": "error", "message": header.get("resultMsg")}

        items = (data.get("body") or {}).get("items") or []
        if not items:
            return {"status": "not_found"}

        item = next((i for i in items if _to_float(i.get("AMT_NUM1")) is not None), items[0])

        return {
            "status": "success",
            "food_name": item.get("FOOD_NM_KR"),
            "serving_size": item.get("SERVING_SIZE"),
            "calories_kcal": _to_float(item.get("AMT_NUM1")),
            "protein_g": _to_float(item.get("AMT_NUM3")),
            "fat_g": _to_float(item.get("AMT_NUM4")),
            "carbs_g": _to_float(item.get("AMT_NUM6")),
            "sugar_g": _to_float(item.get("AMT_NUM7")),
            "sodium_mg": _to_float(item.get("AMT_NUM13")),
            "source": "MFDS",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


_NUTRITION_CACHE_TTL_SECONDS = 10 * 60  # "다시 짜줘" 식으로 같은 대화 안에서 메뉴가 겹칠 때 재조회 방지
_nutrition_cache: dict[str, tuple[float, dict]] = {}


def search_food_nutrition(food_name: str) -> dict:
    """음식의 칼로리 및 영양성분을 식약처 식품영양성분DB(한식 위주)에서 조회한다.

    결과는 잠깐(10분) 메모리에 캐싱한다 — 성공/not_found만 캐싱하고, 네트워크
    오류(error)는 일시적일 수 있어 캐싱하지 않고 매번 다시 시도한다.
    """
    cache_key = food_name.strip().lower()
    cached = _nutrition_cache.get(cache_key)
    if cached is not None:
        cached_at, cached_result = cached
        if time.time() - cached_at < _NUTRITION_CACHE_TTL_SECONDS:
            return cached_result

    result = _search_mfds_food(food_name)
    if result["status"] in ("success", "not_found"):
        _nutrition_cache[cache_key] = (time.time(), result)
    return result


def search_food_nutrition_batch(food_names: list[str]) -> dict[str, dict]:
    """여러 음식명을 병렬로 조회한다. 7일 식단표처럼 한 번에 여러 메뉴의 영양성분이
    필요할 때, 메뉴 하나마다 모델을 다시 호출하지 않고 한 번의 도구 호출로 끝내기 위한
    배치 버전이다. 결과는 {입력한 음식명: search_food_nutrition 결과} 형태의 dict.
    """
    unique_names = list(dict.fromkeys(name for name in food_names if name))
    if not unique_names:
        return {}

    with ThreadPoolExecutor(max_workers=min(20, len(unique_names))) as executor:
        results = list(executor.map(search_food_nutrition, unique_names))

    return dict(zip(unique_names, results))


# =========================================================
# 재료 기반 레시피 추천 (식약처 조리식품의 레시피 DB, COOKRCP01)
# =========================================================

FOODSAFETYKOREA_API_KEY = os.getenv("FOODSAFETYKOREA_API_KEY")
RECIPE_ENDPOINT_BASE = "http://openapi.foodsafetykorea.go.kr/api"
RECIPE_SERVICE_ID = "COOKRCP01"

_recipe_cache: dict[str, Any] = {"items": None, "fetched_at": 0.0}
_RECIPE_CACHE_TTL_SECONDS = 6 * 60 * 60  # 레시피는 자주 안 바뀌므로 6시간 캐싱


def _fetch_all_recipes(force_refresh: bool = False) -> list:
    now = time.time()
    if (
        not force_refresh
        and _recipe_cache["items"] is not None
        and (now - _recipe_cache["fetched_at"] < _RECIPE_CACHE_TTL_SECONDS)
    ):
        return _recipe_cache["items"]

    if not FOODSAFETYKOREA_API_KEY:
        return []

    all_rows = []
    page_size = 1000
    start = 1
    try:
        while True:
            end = start + page_size - 1
            url = f"{RECIPE_ENDPOINT_BASE}/{FOODSAFETYKOREA_API_KEY}/{RECIPE_SERVICE_ID}/json/{start}/{end}"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            block = data.get(RECIPE_SERVICE_ID, {})
            result = block.get("RESULT", {})
            if result.get("CODE") not in (None, "INFO-000"):
                break

            rows = block.get("row") or []
            if not rows:
                break
            all_rows.extend(rows)

            total_count = int(block.get("total_count", len(all_rows)) or len(all_rows))
            if len(all_rows) >= total_count or len(rows) < page_size:
                break
            start += page_size
    except Exception:
        pass

    if all_rows:
        _recipe_cache["items"] = all_rows
        _recipe_cache["fetched_at"] = now
    return _recipe_cache["items"] or []


def find_recipes_by_ingredients(
    ingredients: list[str], max_results: int = 3, exclude_terms: list[str] | None = None
) -> dict:
    """집에 있는 재료 목록으로 만들 수 있는 레시피를 찾아 추천한다.
    exclude_terms(알레르기/기피 음식)가 재료 목록에 포함된 레시피는 자동으로 제외한다.
    """
    if not FOODSAFETYKOREA_API_KEY:
        return {"status": "error", "message": "FOODSAFETYKOREA_API_KEY가 설정되지 않았습니다."}

    recipes = _fetch_all_recipes()
    if not recipes:
        return {"status": "error", "message": "레시피 데이터를 가져오지 못했습니다 (키/네트워크 확인 필요)."}

    exclude_set = set(exclude_terms or [])

    scored = []
    excluded_count = 0
    for r in recipes:
        parts_text = r.get("RCP_PARTS_DTLS") or ""
        if exclude_set and any(term in parts_text for term in exclude_set if term):
            excluded_count += 1
            continue
        matched = [ing for ing in ingredients if ing and ing in parts_text]
        if matched:
            scored.append((len(matched), matched, r))

    if not scored:
        msg = "가지고 계신 재료로 매칭되는 레시피를 찾지 못했습니다."
        if excluded_count:
            msg += f" (알레르기/기피 음식 때문에 {excluded_count}건 제외됨)"
        return {"status": "not_found", "message": msg}

    scored.sort(key=lambda x: x[0], reverse=True)

    results = []
    for match_count, matched, r in scored[:max_results]:
        steps = [r.get(f"MANUAL{i:02d}") for i in range(1, 21) if r.get(f"MANUAL{i:02d}")]
        results.append(
            {
                "recipe_name": r.get("RCP_NM"),
                "matched_ingredients": matched,
                "matched_count": match_count,
                "cooking_method": r.get("RCP_WAY2"),
                "category": r.get("RCP_PAT2"),
                "serving_weight": r.get("INFO_WGT"),
                "calories_kcal": _to_float(r.get("INFO_ENG")),
                "carbs_g": _to_float(r.get("INFO_CAR")),
                "protein_g": _to_float(r.get("INFO_PRO")),
                "fat_g": _to_float(r.get("INFO_FAT")),
                "sodium_mg": _to_float(r.get("INFO_NA")),
                "ingredients_text": r.get("RCP_PARTS_DTLS"),
                "steps": steps,
                "image_url": r.get("ATT_FILE_NO_MAIN"),
            }
        )

    response: dict[str, Any] = {"status": "success", "results": results}
    if exclude_set:
        response["filtered_allergens_or_disliked"] = sorted(exclude_set)
        response["excluded_recipe_count"] = excluded_count
    return response


# =========================================================
# 식단 다양성 후보
# ---------------------------------------------------------
# 원래 agent.py에만 있던 로직이다. LLM이 프롬프트/과거 답변의 예시 메뉴를 매번
# 그대로 재사용하는 걸 막기 위해, 카테고리별 재료/조리법/스타일 풀에서 무작위로
# 일부를 뽑아 후보로 제공한다. recent_text에 이미 등장한 항목은 후보에서 뺀다.
# api.py(stateless)에서도 7일 식단표를 짤 때 같은 문제(매일 닭가슴살+현미밥
# 반복)가 있어서 agent.py 전용 코드였던 걸 여기로 옮겨 공유한다.
# =========================================================

_PROTEIN_POOL = [
    "닭가슴살", "닭안심", "소고기 홍두깨살", "돼지고기 안심", "돼지고기 뒷다리살",
    "고등어", "연어", "참치", "오징어", "새우", "두부", "달걀", "그릭요거트", "렌틸콩",
]
_CARB_POOL = [
    "현미밥", "잡곡밥", "고구마", "감자", "통밀빵", "오트밀", "퀴노아", "메밀면",
    "현미떡", "통밀 또띠아", "단호박",
]
_VEG_POOL = [
    "브로콜리", "시금치나물", "무생채", "샐러드 채소", "가지구이", "버섯볶음",
    "오이무침", "콩나물무침", "미역줄기볶음", "파프리카",
]
_STYLE_POOL = [
    "구이", "찜", "볶음", "샐러드", "국/찌개", "덮밥", "비빔", "오븐구이", "샤브샤브",
]
_CUISINE_POOL = ["한식", "일식", "양식", "동남아식", "지중해식"]


def get_meal_variety_options(recent_text: str = "") -> dict:
    """식단을 다양하게 구성하기 위한 재료/조리법 후보를 무작위로 제공한다.
    recent_text(최근 식사 설명/대화 이력을 이어붙인 문자열)에 이미 등장한 재료는
    후보에서 제외한다. 호출할 때마다 무작위로 다른 조합이 나온다.
    """

    def _filtered_sample(pool: list[str], k: int) -> list[str]:
        candidates = [item for item in pool if item not in recent_text]
        if len(candidates) < k:
            candidates = pool  # 다 겹치면 그냥 전체 풀에서 뽑음
        return random.sample(candidates, min(k, len(candidates)))

    return {
        "protein_options": _filtered_sample(_PROTEIN_POOL, 5),
        "carb_options": _filtered_sample(_CARB_POOL, 4),
        "veg_options": _filtered_sample(_VEG_POOL, 4),
        "cooking_style_options": random.sample(_STYLE_POOL, 4),
        "cuisine_style_options": random.sample(_CUISINE_POOL, 2),
        "note": "이 목록은 후보일 뿐입니다. 반드시 여기서만 골라야 하는 건 아니며, "
                "최근 식사와 겹치지 않는 새로운 조합을 만드는 데 참고하세요.",
    }
