from pathlib import Path

from dotenv import load_dotenv


def load_project_dotenv() -> None:
    """Load env files from both supported local locations.

    ADK commands and uvicorn can start with different current working directories,
    so relying on load_dotenv() discovery alone is brittle.
    """
    package_dir = Path(__file__).resolve().parent
    project_root = package_dir.parent

    load_dotenv(project_root / ".env")
    load_dotenv(package_dir / ".env")
