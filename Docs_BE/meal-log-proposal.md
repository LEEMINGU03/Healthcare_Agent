# 제안: "오늘 먹은 끼니" 기록 (meal log)

- 작성: AI 에이전트(diet_agent) 담당 쪽 제안, 백엔드 검토 필요
- 배경: 지금 `/generate` 계약엔 유저 ID조차 없고, 세션이 끝나면(새 채팅 시작) `history`도 비워져서
  "오늘 이미 뭘 먹었는지"를 여러 세션/여러 날에 걸쳐 알 방법이 없다. 같은 세션 안에서는 `history`로
  임시 대응해뒀지만(2026-07-30 반영), 이건 근본 해결이 아니다.

## 제안하는 변경

### 1. DB: `meal_logs` 테이블 추가

| 컬럼 | 타입 | 설명 |
|---|---|---|
| id | UUID | PK |
| user_id | UUID | FK → users |
| eaten_at | date | 먹은 날짜 |
| slot | BREAKFAST/LUNCH/DINNER | |
| menu | text | 메뉴명 |
| calories | int | |
| carbs_g / protein_g / fat_g | numeric | |
| source | AI_PLAN / USER_LOGGED | AI가 추천한 걸 그대로 기록한 건지, 사용자가 직접 "먹었다"고 확정한 건지 구분 |

`routines`/`meal_plans`처럼 완전히 새로운 도메인 테이블 하나 추가하는 정도로 충분하다고 본다.

### 2. `POST {ai.base-url}/generate` 요청에 필드 추가 (`api.md` 4.1)

```json
{
  "...": "기존 필드 그대로",
  "todayMeals": [
    { "slot": "BREAKFAST", "menu": "계란 두 개랑 토스트", "calories": 290, "carbsG": 30, "proteinG": 15, "fatG": 10 }
  ]
}
```

- 백엔드가 `meal_logs`에서 오늘 날짜분만 조회해서 채워줌. 없으면 빈 배열.
- **하위 호환**: 기존 필드는 안 건드리고 추가만 하는 거라, 이 필드가 없어도(구버전 백엔드) AI 서버는 그냥 "오늘 아무것도 안 먹은 것"으로 처리하면 되므로 문제 없음.

### 3. AI 서버(diet_agent) 쪽 처리

이미 2026-07-30에 넣은 "history 기반 오늘 끼니 유지" 로직과 거의 동일한 패턴이라, 추가 작업 부담은 크지 않다:
- `todayMeals`에 있는 슬롯은 그대로 유지(다른 메뉴로 안 바꿈)
- 없는 슬롯만 새로 추천
- 최근 며칠 메뉴와 안 겹치게 구성 (이것도 `todayMeals`/`history`만으론 부족하면, 향후 `recentMeals`(최근 N일)로 확장 가능)

## 언제 `meal_logs`에 기록할지 (미결, 백엔드와 협의 필요)

가장 간단한 시작점: `POST /api/chat` 응답에 `result.mealPlan`이 생성될 때마다, 그 날짜분을 `source=AI_PLAN`으로 자동 기록. "진짜 먹었다"는 것과는 다르지만(추천받은 것뿐), 최소한 "방금 추천한 걸 또 추천하는" 반복은 막을 수 있다.

더 정확하게 하려면 사용자가 "그거 먹었어" 같은 확정 액션을 했을 때만 `source=USER_LOGGED`로 기록하는 방식도 고려 가능 — 이건 프론트 쪽 버튼/플로우 설계가 필요해서 이번 제안 범위 밖으로 뒀다.

## 영향 없는 부분

- 기존 `/generate` 응답 스키마(`reply`/`result`)는 그대로.
- 기존 필드 하나도 안 건드림 — `todayMeals`는 새 optional 필드일 뿐.
