import os
import dotenv


dotenv.load_dotenv()


def get_env_variable(key: str) -> str:
    value = os.getenv(key)
    if value is None:
        raise ValueError(
            f"'{key}' 환경 변수가 설정되지 않았습니다. .env 파일을 확인해주세요."
        )
    return value


GEMINI_API_KEY = get_env_variable("GEMINI_API_KEY")
FATSECRET_CLIENT_ID = get_env_variable("FATSECRET_CLIENT_ID")
FATSECRET_CLIENT_SECRET = get_env_variable("FATSECRET_CLIENT_SECRET")
MFDS_API_KEY= get_env_variable("MFDS_API_KEY")
FOODSAFETYKOREA_API_KEY= get_env_variable("FOODSAFETYKOREA_API_KEY")
