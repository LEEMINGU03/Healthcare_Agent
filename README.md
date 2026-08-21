## 환경 맞추기 ###
```bash
    uv sync
```
- goole adk (구글 에이전트 개발 키트 사용)

## AI 서버 실행

백엔드 서버의 `Docs_BE/api.md` 기준으로 AI 서버는 다음 엔드포인트를 제공합니다.

- `POST /generate`
- `GET /health`

로컬 실행:

```bash
uv run uvicorn diet_agent.api:app --host 0.0.0.0 --port 8000
```

백엔드의 `ai.base-url`은 로컬 기준 `http://localhost:8000`으로 설정하면 됩니다.
`OPENAI_API_KEY`를 설정하면 OpenAI 모델을 사용합니다. 모델은 `OPENAI_MODEL` 환경변수로
덮어쓸 수 있으며, 기본값은 `gpt-4o-mini`입니다.

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

`OPENAI_API_KEY`가 없고 `GOOGLE_API_KEY` 또는 `GEMINI_API_KEY`가 있으면 기존 Gemini 경로를
fallback으로 사용합니다.

## git 규칙
- main으로 합치기전에 점검을 위해 develop에서 한번더 분기
