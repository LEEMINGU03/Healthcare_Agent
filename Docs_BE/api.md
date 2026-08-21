# API 명세 (MVP)

프론트↔백엔드, 백엔드↔AI 서버 두 계약의 단일 출처(single source of truth). `architecture.md`의
API 설명은 이 문서를 가리키기만 한다. 스키마는 `database.md`/`schema.sql`, 엔티티는
`src/main/java/com/example/Healthcare_BE`의 `entity` 패키지들이 이 문서와 1:1로 맞아야 한다.

## 1. 공통 규약

| 항목 | 규칙 |
|---|---|
| Base path | `/api` (백엔드), AI 서버는 별도 base URL — 4장 참고 |
| Content-Type | `application/json` (요청·응답 모두) |
| 인증 | 없음 (MVP). 고정 더미 유저 1명 — [[stack-architecture]] |
| ID | UUID 문자열 |
| 날짜 | `date`는 `yyyy-MM-dd`, 타임스탬프는 ISO-8601 offset (`2026-07-16T09:00:00+09:00`) |
| enum 값 | Java enum과 동일한 대문자 표기 그대로 JSON에 노출 (아래 표) |
| 필드 표기 | JSON은 camelCase, DB 컬럼은 snake_case — 매핑은 JPA 엔티티가 담당 |

**enum 값 목록** (모두 `database.md`의 CHECK 제약과 1:1):

| 필드 | 값 |
|---|---|
| `type` (채팅) | `COACHING`, `NUTRITION` |
| `role` (메시지) | `USER`, `ASSISTANT` |
| `gender` | `MALE`, `FEMALE` |
| `previousWorkout` | `UPPER_BODY`, `LOWER_BODY` |
| `dayOfWeek` | `MON`, `TUE`, `WED`, `THU`, `FRI`, `SAT`, `SUN` |
| `slot` (끼니) | `BREAKFAST`, `LUNCH`, `DINNER` |

### 에러 응답

Spring Boot 내장 **RFC 7807 ProblemDetail**을 그대로 쓴다. 커스텀 에러 DTO를 새로 만들지 않는다 —
`@RestControllerAdvice`에서 예외별로 `ProblemDetail`을 채워 던지면 프레임워크가 직렬화한다.

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "detail": "인바디 기록이 없습니다.",
  "instance": "/api/inbody/recent"
}
```

검증 실패(요청 필드 위반)는 `400`, 리소스 없음은 `404`, AI 서버 호출 실패/타임아웃은 `502`로 통일한다.

## 2. 엔드포인트 목록

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/inbody/recent` | My Recent Inbody Data 패널 |
| POST | `/api/chat` | 코칭/영양 채팅 (메인 입력창 + 첫 인사말 포함) |
| GET | `/api/chat/sessions` | 최근 채팅내역 목록 |
| GET | `/api/chat/sessions/{sessionId}` | 세션 상세 (메시지 + 결과 전체) |

메인 화면 상단의 "ooo님 반갑습니다 / 지난 루틴을 기반하여 오늘은 상체 하시는 날입니다"는 별도
프로필 조회 API가 없다. **이것도 채팅의 일부** — 새 세션의 첫 AI 메시지이며 `POST /api/chat`으로
받아온다 (3.2 참고). 이름·성별·키 등 프로필 데이터 자체는 여전히 `users` 테이블에 있고, AI 요청을
조립할 때만 백엔드 내부에서 쓰인다 (4.1의 `profile` 객체).

## 3. 프론트 → 백엔드

### 3.1 `GET /api/inbody/recent`

하단 My Recent Inbody Data 패널용. 수치 3개 + 막대 그래프 3줄에 대응. 유저의 최신 측정 1건.

**응답 `200 OK`**
```json
{
  "measuredAt": "2026-06-20",
  "weightKg": 70.0,
  "skeletalMuscleMassKg": 32.0,
  "bodyFatMassKg": 12.0,
  "bmrKcal": 1650
}
```

**응답 `404 Not Found`** — 측정 기록이 아직 하나도 없을 때 (인바디 입력 경로가 미결이라 이 상태가
실제로 발생할 수 있다. 프론트는 "데이터 없음" 화면을 준비해야 한다).

막대 그래프의 기준 구간(정상 범위)은 프론트가 렌더링 시 계산하는 것으로 가정한다.

### 3.2 `POST /api/chat`

코칭 AI / 영양 AI 채팅 공통. 우측 탭 전환, 메인 화면 하단 입력창, **그리고 최초 진입 시의 인사말**까지
전부 이 엔드포인트 하나로 처리한다. 사이트 최초 진입 시 메인 채팅은 운동 AI이므로 `type`은
`COACHING`이 기본값이다.

**두 가지 호출 패턴이 있다.**

1. **인사말 요청** — `sessionId: null`, `message: null`(또는 생략). 페이지 최초 진입 시,
   또는 좌측 "새 채팅시작"을 눌렀을 때 프론트가 자동으로 호출한다. 사용자가 아직 아무 말도
   하지 않은 상태이므로 `USER` 메시지는 저장하지 않고, AI가 이름·이전 운동·인바디를 바탕으로
   생성한 인사말(+추천)만 `ASSISTANT` 메시지로 저장한다. 이 응답의 `reply`가 메인 화면 상단
   "ooo님 반갑습니다 / 지난 루틴을 기반하여 오늘은 상체 하시는 날입니다" 자리에 그대로 들어간다.
2. **일반 대화** — `message`가 채워진 경우. 기존과 동일하게 `USER` + `ASSISTANT` 메시지를 저장한다.

**요청 (일반 대화)**
```json
{
  "type": "COACHING",
  "message": "오늘 가슴 위주로 하고 싶어",
  "sessionId": null,
  "settings": {
    "upperBody": "가슴",
    "lowerBody": null,
    "durationMinutes": 60
  }
}
```

**요청 (인사말)**
```json
{ "type": "COACHING", "message": null, "sessionId": null, "settings": null }
```

| 필드 | 제약 |
|---|---|
| `type` | 필수, `COACHING` \| `NUTRITION` |
| `message` | 선택. **`sessionId`가 null일 때만** 비워둘 수 있다(인사말 요청). `sessionId`가 있는데 `message`가 비어있으면 `400` — 기존 대화에 인사말을 다시 요청하는 것은 정의되지 않는다 |
| `sessionId` | 선택. null이면 새 세션 생성, 값이 있으면 기존 세션에 이어붙임 |
| `settings.upperBody` / `lowerBody` | 선택 문자열. 미선택 시 null |
| `settings.durationMinutes` | 선택, 양수 |

`sessionId`가 null일 때 새 세션의 `title`은, 인사말 요청이면 "새 채팅"으로 고정하고 일반 대화면
`message`를 20자 이내로 잘라 채운다 (가정 — 미결 사항 참고).

**응답 `200 OK`**
```json
{
  "sessionId": "3f2a1c34-...",
  "reply": "말씀하신대로 상체루틴 운동루틴을 추천하여 제작하겠습니다.",
  "result": { "routine": { "...": "4장 result 스키마 참고" } }
}
```

`reply`는 말풍선 텍스트, `result`는 카드/표로 렌더링할 구조화 데이터다 (`type`에 따라 `routine`
또는 `mealPlan`, AI가 되묻기만 하는 턴이나 인사말 응답에서는 `null`).

DB에는 `result`를 통째로 저장하지 않고 정규화된 테이블(`routines`/`routine_exercises`,
`meal_plans`/`meal_plan_days`/`meal_plan_meals`)에 나눠 담는다 — 이 응답의 JSON 모양은 항상
그대로이고, 백엔드가 저장 시 분해하고 조회 시 다시 조립한다.

**에러**

| 상태 | 조건 |
|---|---|
| `400` | `type` 누락, `sessionId`가 있는데 `message`가 비어있음, `sessionId`가 다른 유저/타입의 세션을 가리킴 |
| `404` | `sessionId`가 존재하지 않음 |
| `502` | AI 서버 호출 실패, 타임아웃, 응답 파싱 실패 |

### 3.3 `GET /api/chat/sessions`

좌측 "최근 채팅내역" 목록. `type` 쿼리 파라미터로 코칭/영양 탭을 구분한다.

**요청**: `GET /api/chat/sessions?type=COACHING`

**응답 `200 OK`**
```json
[
  { "sessionId": "3f2a1c34-...", "type": "COACHING", "title": "오늘 가슴 위주로 하고 싶어", "createdAt": "2026-07-16T09:00:00+09:00" }
]
```

정렬은 `createdAt` 내림차순 (세션 생성순 — 미결 사항 참고). 기록이 없으면 빈 배열.

### 3.4 `GET /api/chat/sessions/{sessionId}`

세션 상세. 메시지 전체와, 어시스턴트 메시지에 딸린 루틴/식단표 결과를 함께 재조립해 반환한다.

**응답 `200 OK`**
```json
{
  "sessionId": "3f2a1c34-...",
  "type": "COACHING",
  "messages": [
    { "role": "USER", "content": "오늘 가슴 위주로 하고 싶어", "result": null },
    { "role": "ASSISTANT", "content": "말씀하신대로...", "result": { "routine": { "...": "4장 참고" } } }
  ]
}
```

**응답 `404 Not Found`** — `sessionId`가 존재하지 않음.

## 4. 백엔드 → AI 서버

**AI 서버의 실제 base URL·인증 방식은 아직 확정되지 않았다** (미결 사항 참고). 아래는 백엔드가
정의해 AI 서버 담당자와 합의해야 할 초안이며, 합의되는 즉시 이 문서를 갱신한다.

### 4.1 `POST {ai.base-url}/generate`

```json
{
  "type": "COACHING",
  "message": "오늘 가슴 위주로 하고 싶어",
  "settings": { "upperBody": "가슴", "lowerBody": null, "durationMinutes": 60 },
  "profile": {
    "name": "홍길동", "gender": "MALE", "heightCm": 175.0,
    "targetGainKg": 3.0, "previousWorkout": "UPPER_BODY"
  },
  "inbody": {
    "measuredAt": "2026-06-20", "weightKg": 70.0,
    "skeletalMuscleMassKg": 32.0, "bodyFatMassKg": 12.0, "bmrKcal": 1650
  },
  "history": [
    { "role": "USER", "content": "..." },
    { "role": "ASSISTANT", "content": "..." }
  ]
}
```

`profile.name`은 3.2의 인사말 요청("ooo님 반갑습니다")을 AI가 생성하려면 반드시 필요해 추가했다 —
이전에는 프론트가 별도 프로필 조회로 이름을 표시했지만, 이제 인사말 자체를 AI가 만들기 때문이다.

`inbody`는 기록이 없으면 `null`로 보낸다 (3.1의 404 케이스와 대응).

**인사말 요청**(3.2의 `message: null` 패턴)일 때는 `message`를 `null`로, `history`를 빈 배열로 보낸다.
AI는 `profile`/`inbody`만으로 이름을 부르는 인사말과 오늘의 추천을 생성한다 — "오늘의 추천을 어떤
근거로 정할지"는 AI 내부 로직이며 이 계약은 입력만 규정한다.

**응답**
```json
{ "reply": "...", "result": null }
```

### 4.2 `result` 스키마 — `type: COACHING`

와이어프레임의 운동루틴 카드(운동명 / 상세 설명 / 3~4세트 8~12회 / 이미지)에 대응.
`routine_exercises` 테이블과 1:1.

```json
{
  "routine": {
    "title": "COACHING AI 운동루틴",
    "exercises": [
      {
        "order": 1,
        "name": "등업",
        "sets": "3~4세트",
        "reps": "8~12회",
        "description": "어깨너비의 약간 넓게 바를 잡고 ...",
        "imageUrl": "https://..."
      }
    ]
  }
}
```

### 4.3 `result` 스키마 — `type: NUTRITION`

와이어프레임의 주간 식단표(MON~SUN × 3행)에 대응. 3행은 아침/점심/저녁으로 가정.
`meal_plan_days`/`meal_plan_meals` 테이블과 1:1.

```json
{
  "mealPlan": {
    "title": "NUTRITION AI 식단표",
    "days": [
      {
        "dayOfWeek": "MON",
        "meals": [
          { "slot": "BREAKFAST", "menu": "...", "calories": 500,
            "carbsG": 60, "proteinG": 30, "fatG": 15 }
        ]
      }
    ]
  }
}
```

### 4.4 타임아웃·재시도

AI 응답이 동기이므로(`architecture.md` 1장 확정 사항) 프론트 요청이 그동안 블로킹된다. 백엔드 → AI
호출에 **연결 타임아웃 3초, 읽기 타임아웃 30초**를 건다 (가정 — AI 서버의 실제 응답 시간에 따라
조정 필요). 재시도는 하지 않는다 — 실패 시 3.2의 `502`를 그대로 프론트에 전달한다.

## 5. 미결 사항

아래는 API 명세에 직접 영향을 주지만 아직 답을 받지 못한 것들이다. 확정되는 대로 이 문서와
`architecture.md`/`database.md`를 함께 갱신한다.

- **인바디 쓰기 경로**: `POST /api/inbody`가 필요한지, 필요하다면 요청 바디가 사용자 직접 입력인지
  기기 연동 값인지 미정. 현재는 3.1의 `GET`만 있다.
- **버튼 동작**: "진행시켜", "설정 초기화", "7월 식단표 제작", "식단표 수정", "종합 데이터" —
  각각 별도 엔드포인트가 필요한지, 있다면 요청/응답이 무엇인지 미정.
- **AI 서버 base URL·인증 방식**: 4장 전체가 초안이며 AI 서버 담당자와 합의 전이다.
- **`GET /api/chat/sessions` 페이지네이션**: MVP는 전체 반환으로 가정. 목록이 많아지면 필요.
