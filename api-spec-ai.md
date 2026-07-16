# AI 서버 ↔ 백엔드 연동 API 명세서

- 대상 시스템: `healthcare` 백엔드(Spring Boot, 이 저장소) ↔ AI 서버(코칭/식단 AI, 별도 저장소)
- 최종 수정: 2026-07-16
- 범위: AI 연동에 직접 관련된 API만 다룸 (Users/Dashboard 등 프론트엔드 전용 API는 제외)

> 표기: **[구현됨]** 코드에 실제 존재하는 API / **[초안]** 아직 코드에 없고 이 문서에서 제안하는 API — AI 서버 측과 필드를 확정한 뒤 구현 필요

## 0. 공통 사항

- Base URL: `{BACKEND_URL}/api` (로컬 기본 `http://localhost:8080/api`)
- 인증: 현재 `SecurityConfig`에서 `/api/**` 전체가 `permitAll()`로 열려 있어 별도 인증 헤더 없이 호출 가능. 추후 인증이 추가되면 본 문서를 갱신해야 함.
- Content-Type: `application/json; charset=UTF-8`
- 공통 에러 응답 (`GlobalExceptionHandler` 기준)

  | 상황 | HTTP 상태 | 응답 바디 |
  |---|---|---|
  | 존재하지 않는 리소스 (예: userId 없음) | 404 | `{ "error": "존재하지 않는 사용자입니다. userId=1" }` |
  | 잘못된 enum 값 등 도메인 검증 실패 | 400 | `{ "error": "agentType은 DIET, COACH 중 하나여야 합니다: XXX" }` |
  | 요청 바디 검증 실패 (`@Valid`) | 400 | `{ "error": "입력값이 올바르지 않습니다.", "fieldErrors": { "필드명": "메시지" } }` |

---

## 1. AI 서버 → 백엔드 [구현됨]

AI 서버가 대화 응답/운동 추천 결과를 저장하거나, 추천에 필요한 사용자 데이터를 조회할 때 호출하는 기존 API.

### 1.1 채팅 메시지 저장 (사용자 발화 / AI 응답 기록 공용)

`POST /api/chat`

AI 응답을 생성한 뒤 `role=ASSISTANT`로 저장할 때 사용. (사용자 발화 저장에도 동일 API가 쓰이며 `role=USER`로 호출됨)

Request body

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| userId | Long | Y | 사용자 ID |
| agentType | String | Y | `DIET` \| `COACH` |
| role | String | Y | `USER` \| `ASSISTANT` |
| message | String | Y | 메시지 본문 |

Response `201 Created`

```json
{
  "id": 10,
  "userId": 1,
  "agentType": "COACH",
  "role": "ASSISTANT",
  "message": "오늘은 하체 운동을 추천드려요.",
  "createdAt": "2026-07-16T09:00:00"
}
```

### 1.2 채팅 이력 조회 (AI 응답 생성 시 컨텍스트로 사용)

`GET /api/chat?userId={userId}&agentType={agentType}&date={date}`

| 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| userId | Long | Y | 사용자 ID |
| agentType | String | N | `DIET` \| `COACH` (미지정 시 전체) |
| date | String(ISO date) | N | `YYYY-MM-DD`, 해당 날짜 대화만 조회 |

Response `200 OK`: `ChatMessageResponse` 배열 (1.1의 응답 객체와 동일 스키마), `createdAt` 오름차순 정렬.

### 1.3 운동 기록 저장 (AI 추천 운동 기록)

`POST /api/workouts`

AI가 추천한 운동을 사용자 운동 기록으로 남길 때 사용. `source=AI`로 저장.

Request body

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| userId | Long | Y | 사용자 ID |
| exerciseId | Long | Y | `exercises` 마스터의 ID (1.4에서 조회) |
| workoutDate | String(ISO date) | Y | 운동 수행/추천 날짜 |
| source | String | N | `USER` \| `AI` (미지정 시 `USER`) — AI 서버는 반드시 `"AI"`로 지정 |

Response `201 Created`

```json
{
  "id": 5,
  "exerciseId": 12,
  "exerciseName": "스쿼트",
  "bodyPart": "THIGH",
  "workoutDate": "2026-07-15",
  "source": "AI"
}
```

### 1.4 운동 마스터 조회 (추천 후보 목록)

`GET /api/exercises?bodyPart={bodyPart}`

| 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| bodyPart | String | N | `BACK`\|`CHEST`\|`BICEPS`\|`TRICEPS`\|`SHOULDER`\|`CORE`\|`GLUTES`\|`THIGH`\|`CALF` (미지정 시 전체) |

Response `200 OK`: `ExerciseResponse` 배열

```json
[
  {
    "id": 12,
    "bodyPart": "THIGH",
    "seq": 1,
    "name": "스쿼트",
    "instructions": "...",
    "setsMin": 3,
    "setsMax": 4,
    "repType": "REPS",
    "repsMin": 10,
    "repsMax": 15,
    "imageUrl": "/images/exercises/thigh_01.png"
  }
]
```

### 1.5 인바디 최신 기록 조회 (식단/코칭 추천 근거)

`GET /api/inbody/records/latest?userId={userId}`

Response `200 OK` (`InbodyRecordResponse`) — 인바디 기록이 없으면 `404`.

```json
{
  "id": 3,
  "userId": 1,
  "measuredAt": "2026-07-10T08:00:00",
  "heightCm": 175.0,
  "weightKg": 70.5,
  "bodyFatPct": 18.2,
  "skeletalMuscleKg": 32.1,
  "bodyFatKg": 12.8,
  "bmrKcal": 1650,
  "source": "MANUAL",
  "imageUrl": null
}
```

### 1.6 인바디 3개월 추이 조회

`GET /api/inbody/records/trend?userId={userId}`

Response `200 OK`

```json
{
  "userId": 1,
  "months": [
    { "month": "2026-05", "weightKg": 72.0, "skeletalMuscleKg": 31.5, "bodyFatKg": 13.5 },
    { "month": "2026-06", "weightKg": 71.2, "skeletalMuscleKg": 31.8, "bodyFatKg": 13.0 },
    { "month": "2026-07", "weightKg": 70.5, "skeletalMuscleKg": 32.1, "bodyFatKg": 12.8 }
  ]
}
```

기록이 없는 달은 각 수치가 `null`로 채워짐.

---

## 2. 백엔드 → AI 서버 [초안 — 미구현]

현재 코드에는 백엔드가 AI 서버를 호출하는 클라이언트/엔드포인트가 없음. AI가 챗봇 형태이므로, 사용자가 `POST /api/chat`(role=USER)으로 메시지를 보내면 백엔드가 대화 이력을 AI 서버에 전달해 응답을 받아오는 흐름을 가정한 초안. **AI 서버 팀과 협의 후 필드/경로를 확정해 구현해야 함.**

### 2.1 채팅 응답 생성 요청 (안)

`POST {AI_SERVER_URL}/chat/completions`

Request body (안) — 전체 대화 이력을 메시지 배열로 전달 (OpenAI 스타일)

| 필드 | 타입 | 설명 |
|---|---|---|
| userId | Long | 사용자 ID |
| agentType | String | `DIET` \| `COACH` |
| messages | Array\<Message\> | 대화 이력 + 이번 사용자 메시지. 순서대로 오래된 것 → 최신 순 |
| messages[].role | String | `USER` \| `ASSISTANT` |
| messages[].content | String | 메시지 본문 |
| profile | Object | (선택) 사용자 프로필(성별/목표) — 1.5/1.6 결과 포함 여부는 AI 서버와 협의 |

```json
{
  "userId": 1,
  "agentType": "COACH",
  "messages": [
    { "role": "USER", "content": "요즘 하체가 부실한 것 같아요" },
    { "role": "ASSISTANT", "content": "스쿼트와 런지를 추천드려요." },
    { "role": "USER", "content": "오늘 운동 추천해줘" }
  ]
}
```

`messages`는 백엔드가 1.2(`GET /api/chat`)로 조회한 기존 이력에 이번 사용자 메시지를 이어붙여 구성하는 것을 가정. 백엔드는 사용자 메시지를 1.1로 먼저 저장한 뒤 이 요청을 보내는 순서를 따름.

Response body (안)

| 필드 | 타입 | 설명 |
|---|---|---|
| message | String | AI 응답 본문 — 백엔드가 이 값을 1.1로 저장(role=ASSISTANT) 후 클라이언트에 반환 |

```json
{
  "message": "오늘은 스쿼트 3세트, 런지 3세트를 추천드려요."
}
```

---

## 확인 필요 사항 (TBD)

- 2장(백엔드 → AI 서버) 요청/응답 필드 및 엔드포인트 경로는 AI 서버 스펙 확정 후 갱신.
- `messages` 배열의 최대 길이(최근 N개로 자를지 여부) 및 `profile` 포함 여부 협의 필요.
- `/api/**` 인증 정책이 추후 추가될 경우 AI 서버 인증 방식(API Key 등) 함께 정의 필요.
