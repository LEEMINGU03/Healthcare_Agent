from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from google import genai as google_genai

from diet_agent.config import load_project_dotenv

load_project_dotenv()

# =========================================================
# 식약처 식품영양성분DB (data.go.kr, 한식 위주 - FatSecret보다 우선 조회)
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


# =========================================================
# FatSecret (MFDS에 없을 때 보조로 사용)
# =========================================================

FATSECRET_CLIENT_ID = os.getenv("FATSECRET_CLIENT_ID")
FATSECRET_CLIENT_SECRET = os.getenv("FATSECRET_CLIENT_SECRET")

_fatsecret_token_cache: dict[str, Any] = {"token": None, "expires_at": 0}


def _get_fatsecret_token() -> str:
    if _fatsecret_token_cache["token"] and time.time() < _fatsecret_token_cache["expires_at"]:
        return _fatsecret_token_cache["token"]

    if not FATSECRET_CLIENT_ID or not FATSECRET_CLIENT_SECRET:
        raise ValueError("FatSecret API 키가 설정되지 않았습니다.")

    response = requests.post(
        "https://oauth.fatsecret.com/connect/token",
        auth=(FATSECRET_CLIENT_ID, FATSECRET_CLIENT_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "basic"},
        timeout=5,
    )
    response.raise_for_status()
    data = response.json()

    _fatsecret_token_cache["token"] = data["access_token"]
    _fatsecret_token_cache["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
    return data["access_token"]


def _parse_fatsecret_description(description: str | None) -> dict:
    import re

    description = description or ""
    parsed: dict[str, float] = {}

    patterns = {
        "calories_kcal": r"Calories:\s*([\d.]+)\s*kcal",
        "fat_g": r"Fat:\s*([\d.]+)\s*g",
        "carbs_g": r"Carbs:\s*([\d.]+)\s*g",
        "protein_g": r"Protein:\s*([\d.]+)\s*g",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            parsed[key] = float(match.group(1))

    serving_match = re.search(r"Per\s+([\d.]+)\s*g", description, re.IGNORECASE)
    if serving_match:
        parsed["serving_size_g"] = float(serving_match.group(1))

    return parsed


def _translate_food_name_to_english(food_name: str) -> str:
    if os.getenv("OPENAI_API_KEY"):
        from openai import OpenAI

        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL") or os.getenv("AI_MODEL", "gpt-4o-mini"),
            messages=[
                {
                    "role": "user",
                    "content": f"다음 한국 음식명을 영어로 번역해줘. 음식명만 짧게 답해: {food_name}",
                }
            ],
            temperature=0,
        )
        return (response.choices[0].message.content or food_name).strip()

    client = google_genai.Client(api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))
    result = client.models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        contents=f"다음 한국 음식명을 영어로 번역해줘. 음식명만 짧게 답해: {food_name}",
    )
    return result.text.strip() if result.text else food_name


def search_food_nutrition(food_name: str) -> dict:
    """음식의 칼로리 및 영양성분을 조회한다.
    한글 음식명은 식약처 식품영양성분DB(한식 위주, 더 정확함)에서 먼저 찾고,
    거기 없으면 영문으로 번역해서 FatSecret으로 조회한다.
    """
    is_korean = any('가' <= c <= '힣' for c in food_name)

    if is_korean and MFDS_API_KEY:
        mfds_result = _search_mfds_food(food_name)
        if mfds_result["status"] == "success":
            return mfds_result

    if not FATSECRET_CLIENT_ID or not FATSECRET_CLIENT_SECRET:
        return {"status": "error", "message": "FatSecret API 키가 설정되지 않았습니다."}

    try:
        search_term = food_name
        if is_korean:
            search_term = _translate_food_name_to_english(food_name)

        token = _get_fatsecret_token()
        response = requests.post(
            "https://platform.fatsecret.com/rest/server.api",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "method": "foods.search",
                "search_expression": search_term,
                "format": "json",
                "max_results": 1,
            },
            timeout=5,
        )
        response.raise_for_status()
        data = response.json()

        foods = data.get("foods", {}).get("food", [])
        if not foods:
            return {
                "status": "not_found",
                "searched_as": search_term,
                "message": f"'{food_name}'({search_term})에 대한 정보를 찾을 수 없습니다.",
            }

        if isinstance(foods, dict):
            foods = [foods]

        item = foods[0]
        parsed_nutrition = _parse_fatsecret_description(item.get("food_description"))
        return {
            "status": "success",
            "food_name": item.get("food_name"),
            "searched_as": search_term,
            "food_description": item.get("food_description"),
            **parsed_nutrition,
            "source": "FatSecret",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


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

    exclude_terms = set(exclude_terms or [])

    scored = []
    excluded_count = 0
    for r in recipes:
        parts_text = r.get("RCP_PARTS_DTLS") or ""
        if exclude_terms and any(term in parts_text for term in exclude_terms if term):
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
    if exclude_terms:
        response["filtered_allergens_or_disliked"] = sorted(exclude_terms)
        response["excluded_recipe_count"] = excluded_count
    return response
