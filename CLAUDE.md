# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A diet/nutrition coaching agent built on **Google ADK** (Agent Development Kit). The entire app is currently a single ADK agent (`root_agent`) defined in `diet_agent/agent.py`, with a large system prompt (Korean) and a set of Python tool functions the LLM calls during a conversation.

## Commands

```bash
uv sync              # install/sync dependencies (Python 3.13, managed via uv)
uv run adk web       # launch the ADK dev UI to chat with the agent locally
uv run adk run diet_agent   # run the agent from the CLI
uv run uvicorn diet_agent.api:app --host 0.0.0.0 --port 8000   # launch the AI HTTP server for the backend
```

There is no test suite in this repo yet. `.flake8` configures line length/ignores for linting but `flake8` is not a project dependency (editor-only, via the `ms-python.black-formatter` / flake8 VS Code extensions) — there's no `uv run flake8` guarantee.

### Environment variables (`.env`)

Required (validated eagerly by `env.py`, though note `env.py` itself is not currently imported by `diet_agent/agent.py` — the agent reads env vars directly via `os.getenv`):
- `OPENAI_API_KEY`
- `FATSECRET_CLIENT_ID`, `FATSECRET_CLIENT_SECRET`
- `MFDS_API_KEY` (식약처/data.go.kr nutrition DB)
- `FOODSAFETYKOREA_API_KEY` (separate legacy foodsafetykorea.go.kr key, different from `MFDS_API_KEY`)

Also read directly in `agent.py` (not validated by `env.py`):
- `OPENAI_MODEL` — defaults to `gpt-4o-mini`
- `GEMINI_API_KEY` / `GOOGLE_API_KEY` — optional fallback when `OPENAI_API_KEY` is absent
- `BACKEND_BASE_URL` — defaults to `http://localhost:8080`
- `DEFAULT_USER_ID` — optional fixed user id for local testing when there's no login flow yet

## Architecture

### Main modules

- `diet_agent/agent.py` contains the ADK chat agent. The file is organized top-to-bottom as: backend HTTP helpers → per-domain tool functions (grouped by comment-delimited sections) → the `root_agent = Agent(...)` definition with its instruction prompt and `tools=[...]` list at the very bottom. When adding a new tool, it must also be added to that `tools=[]` list and referenced in the instruction prompt's numbered workflow, or the model won't call it in the right order.
- `diet_agent/api.py` contains the backend-facing FastAPI server. It implements `POST /generate` and `GET /health` according to `Docs_BE/api.md` section 4. The backend should call `POST {AI_SERVER_URL}/generate` and receives `{ "reply": "...", "result": ... }`.

### Two persistence layers, and which fields go where

- **Backend REST API** (`BACKEND_BASE_URL`, "APEXAI Healthcare" backend): only has endpoints for users, inbody (height/weight/body fat) records, and chat history (`/api/users`, `/api/inbody/records`, `/api/chat`). `register_user` / `set_user_profile` / `get_user_profile` / `save_chat_message` / `get_chat_history` talk to it via `_backend_get`/`_backend_post`, which never raise — they return `{"status": "error"/"not_found"/"success"}` dicts.
- **ADK session state only** (`tool_context.state`, lost when the session ends): age/gender/goal/budget/meals-per-day/allergies/disliked foods (`set_user_preferences`/`get_user_preferences`), `meal_history` (`log_meal`, capped at last 14 entries), `workout_history` (read-only here via `get_recent_workouts_for_diet` — nothing in this file writes it). These exist only in session state because the backend schema has no fields for them; if that changes, the backend should gain the fields rather than this file growing more local-only state.
- `set_user_profile`/`set_user_profile` and `set_user_preferences` treat `0`/`""`/`[]` as "unknown, keep existing value" rather than "clear it" — always pass sentinel-empty values instead of omitting fields.

### External nutrition/recipe data sources (query order matters)

1. **MFDS 식품영양성분DB** (`_search_mfds_food`, `apis.data.go.kr/1471000/FoodNtrCpntDbInfo02`) — tried first for Korean food names, more accurate for Korean dishes. Uses `MFDS_API_KEY` (a data.go.kr key).
2. **FatSecret** (`search_food_nutrition` fallback, OAuth2 client-credentials cached in `_fatsecret_token_cache`) — used when MFDS has no match, or for non-Korean names. Korean input is first translated to English via a `gemini-2.5-flash` call before the FatSecret search.
3. **식약처 조리식품 레시피 DB (COOKRCP01)** (`_fetch_all_recipes`/`find_recipes_by_ingredients`) — a *different*, older gateway (`openapi.foodsafetykorea.go.kr`) with its own key (`FOODSAFETYKOREA_API_KEY`, not the MFDS key). There's no server-side ingredient filter, so all ~1,700 recipes are paginated in and cached in-process for 6 hours (`_recipe_cache`), then scored locally by counting ingredient-name substring overlaps. Allergy/disliked-food filtering here is also substring-based on `RCP_PARTS_DTLS` text — not guaranteed accurate, which is why the agent instruction tells the model to tell allergic users to double-check.

### Daily nutrition targets are computed in code, not by the LLM

`_compute_daily_targets` (Mifflin-St Jeor BMR + activity factor + goal adjustment, or a weight-based fallback when age/gender/height are missing) and `get_daily_nutrition_summary` (sums today's `meal_history` entries against those targets) intentionally duplicate the calculation rules that are *also* spelled out in the agent's instruction prompt ("추론 및 조정 기준" section). This duplication is deliberate — it exists so the numbers reported to the user come from deterministic code rather than LLM arithmetic. If you change the formula, update both the code and the matching prose in the instruction string, or they'll drift.

### The instruction prompt encodes a strict control flow

The `instruction=` string on `root_agent` is not just persona/tone — it defines three separate branching workflows the model must follow:
1. **Normal "I ate X" logging** — a fixed 8-step tool-call order (search nutrition → log meal → save chat → fetch recent meals/workouts → fetch profile/prefs → compute daily summary → get variety options → respond in a fixed response format).
2. **"What can I cook with these ingredients"** — uses `find_recipes_by_ingredients` instead, skips `log_meal`/`search_food_nutrition`.
3. **"What should I order at [restaurant]"** — no dedicated tool; the model recalls plausible real menu items from its own knowledge, then must verify each via `search_food_nutrition` before recommending, and must not call `log_meal` (nothing has been eaten yet).

`get_meal_variety_options` exists purely to stop the model from repeating the same example menu ("닭가슴살+현미밥+브로콜리") from the prompt — it hands back randomized ingredient/style candidates each call and excludes items seen in recent `meal_history`.

## Git workflow

Per `README.md`: branch off `develop` before merging into `main`, so changes get a review pass on `develop` first.
