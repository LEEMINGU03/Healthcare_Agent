# 백엔드 전체 API 명세서

- 대상 시스템: `healthcare` 백엔드 (Spring Boot, 이 저장소)
- 최종 수정: 2026-07-16
- 범위: 현재 구현된 모든 REST API (`controller/` 6종 전체)
- AI 서버 연동에 특화된 문서는 [`api-spec-ai.md`](./api-spec-ai.md) 참고

## 0. 공통 사항

- Base URL: `{BACKEND_URL}/api` (로컬 기본 `http://localhost:8080/api`)
- 인증: 현재 `SecurityConfig`에서 `/api/**` 전체가 `permitAll()`로 열려 있어 별도 인증 헤더 없이 호출 가능
- Content-Type: `application/json; charset=UTF-8`
- 공통 에러 응답 (`GlobalExceptionHandler` 기준)

  | 상황 | HTTP 상태 | 응답 바디 |
  |---|---|---|
  | 존재하지 않는 리소스 (예: userId 없음) | 404 | `{ "error": "존재하지 않는 사용자입니다. userId=1" }` |
  | 잘못된 enum 값 등 도메인 검증 실패 | 400 | `{ "error": "agentType은 DIET, COACH 중 하나여야 합니다: XXX" }` |
  | 요청 바디 검증 실패 (`@Valid`) | 400 | `{ "error": "입력값이 올바르지 않습니다.", "fieldErrors": { "필드명": "메시지" } }` |

---

## 1. Users — `/api/users`

### 1.1 회원 생성

`POST /api/users`

Request body

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| email | String | Y | 이메일 형식 검증 |
| nickname | String | Y | |
| gender | String | N | `MALE` \| `FEMALE` |
| goal | String | N | 목표 (예: 근육량 증가, 체지방 감소) |

Response `201 Created`

```json
{ "id": 1 }
```

---

## 2. Dashboard — `/api/dashboard`

### 2.1 대시보드 조회

`GET /api/dashboard?userId={userId}`

프로필, 최신 인바디, 3개월 추이, 전날 운동 기록을 한 번에 반환. 사용자가 없으면 `404`.

Response `200 OK`

```json
{
  "profile": {
    "nickname": "홍길동",
    "gender": "MALE",
    "heightCm": 175.0,
    "weightKg": 70.5,
    "bmrKcal": 1650,
    "goal": "근육량 증가"
  },
  "latest": {
    "measuredAt": "2026-07-10T08:00:00",
    "weightKg": 70.5,
    "skeletalMuscleKg": 32.1,
    "bodyFatKg": 12.8
  },
  "trend": {
    "userId": 1,
    "months": [
      { "month": "2026-05", "weightKg": 72.0, "skeletalMuscleKg": 31.5, "bodyFatKg": 13.5 },
      { "month": "2026-06", "weightKg": 71.2, "skeletalMuscleKg": 31.8, "bodyFatKg": 13.0 },
      { "month": "2026-07", "weightKg": 70.5, "skeletalMuscleKg": 32.1, "bodyFatKg": 12.8 }
    ]
  },
  "yesterdayWorkouts": [
    { "exerciseName": "스쿼트", "bodyPart": "THIGH" }
  ]
}
```

인바디 기록이 없는 사용자는 `latest`가 `null`, `trend.months`는 각 항목이 `null` 값으로 채워짐, `yesterdayWorkouts`는 빈 배열.

---

## 3. Chat — `/api/chat`

### 3.1 채팅 메시지 저장

`POST /api/chat`

Request body

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| userId | Long | Y | |
| agentType | String | Y | `DIET` \| `COACH` |
| role | String | Y | `USER` \| `ASSISTANT` |
| message | String | Y | |

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

### 3.2 채팅 이력 조회

`GET /api/chat?userId={userId}&agentType={agentType}&date={date}`

| 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| userId | Long | Y | |
| agentType | String | N | `DIET` \| `COACH` (미지정 시 전체) |
| date | String(ISO date) | N | `YYYY-MM-DD`, 해당 날짜만 조회 |

Response `200 OK`: 3.1 응답 스키마의 배열, `createdAt` 오름차순.

---

## 4. Exercises — `/api/exercises`

### 4.1 운동 마스터 조회

`GET /api/exercises?bodyPart={bodyPart}`

| 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| bodyPart | String | N | `BACK`\|`CHEST`\|`BICEPS`\|`TRICEPS`\|`SHOULDER`\|`CORE`\|`GLUTES`\|`THIGH`\|`CALF` (미지정 시 전체, `bodyPart` 오름차순 → `seq` 오름차순 정렬) |

Response `200 OK`

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

---

## 5. Inbody Records — `/api/inbody/records`

### 5.1 인바디 기록 생성

`POST /api/inbody/records`

Request body

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| userId | Long | Y | |
| measuredAt | String(ISO date-time) | Y | |
| heightCm | BigDecimal | N | |
| weightKg | BigDecimal | N | |
| bodyFatPct | BigDecimal | N | |
| skeletalMuscleKg | BigDecimal | N | |
| bodyFatKg | BigDecimal | N | |
| bmrKcal | Integer | N | |

서버가 `source="MANUAL"`, `imageUrl=null`로 고정 저장.

Response `201 Created`: 5.2와 동일 스키마

### 5.2 인바디 기록 목록 조회

`GET /api/inbody/records?userId={userId}`

Response `200 OK`: 측정일 최신순 배열

```json
[
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
]
```

### 5.3 최신 인바디 기록 조회

`GET /api/inbody/records/latest?userId={userId}`

Response `200 OK`: 5.2 항목 하나. 기록이 없으면 `404`.

### 5.4 인바디 3개월 추이 조회

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

기록이 없는 달은 각 수치가 `null`.

### 5.5 인바디 기록 수정

`PUT /api/inbody/records/{id}`

Request body: 5.1과 동일 스키마 (전체 필드 교체, `measuredAt`만 필수)

Response `200 OK`: 5.2와 동일 스키마. 존재하지 않는 `id`면 `404`.

### 5.6 인바디 기록 삭제

`DELETE /api/inbody/records/{id}`

Response `204 No Content`. 존재하지 않는 `id`면 `404`.

---

## 6. Workout Logs — `/api/workouts`

### 6.1 운동 기록 생성

`POST /api/workouts`

Request body

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| userId | Long | Y | |
| exerciseId | Long | Y | `exercises` 마스터 ID |
| workoutDate | String(ISO date) | Y | |
| source | String | N | `USER` \| `AI` (미지정 시 `USER`) |

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

### 6.2 운동 기록 조회

`GET /api/workouts?userId={userId}&date={date}`

| 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| userId | Long | Y | |
| date | String(ISO date) | N | 특정 날짜만 조회 (미지정 시 전체) |

Response `200 OK`: 6.1 응답 스키마의 배열, `workoutDate` 내림차순.
