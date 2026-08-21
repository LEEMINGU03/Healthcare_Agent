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

## Vercel 배포

`api/index.py`가 `diet_agent.api:app`을 그대로 노출하고, `pyproject.toml`의
`[tool.vercel] entrypoint = "diet_agent.api:app"` 설정으로 Vercel이 Python 서버리스
함수로 자동 인식합니다. 별도의 `vercel.json`은 필요 없습니다.

1. GitHub 저장소를 Vercel 프로젝트에 연결 (main 브랜치를 Production으로 지정)
2. Vercel 프로젝트 설정 > Environment Variables 에 아래 값 등록
   - `OPENAI_API_KEY` (필수)
   - `FATSECRET_CLIENT_ID`, `FATSECRET_CLIENT_SECRET` (필수)
   - `MFDS_API_KEY` (필수, 식약처 식품영양성분DB)
   - `FOODSAFETYKOREA_API_KEY` (필수, 식약처 레시피DB용 별도 키)
   - `OPENAI_MODEL` (선택, 기본값 `gpt-4o-mini`)
   - `GEMINI_API_KEY` 또는 `GOOGLE_API_KEY` (선택, OpenAI 키 없을 때 fallback)
   - `BACKEND_BASE_URL` (선택, 기본값 `http://localhost:8080` — 배포 시 실제 백엔드 주소로 지정)
3. main 브랜치에 push하면 Vercel이 자동 빌드/배포
4. 배포 후 `https://<프로젝트>.vercel.app/health` 로 정상 기동 확인

## git 규칙
- main으로 합치기전에 점검을 위해 develop에서 한번더 분기
