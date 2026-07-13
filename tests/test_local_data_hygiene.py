from pathlib import Path

import main
import server


def test_database_and_logs_live_in_per_user_app_data():
    assert server.DB_PATH == server.APP_DATA_DIR / "music.db"
    assert main.LOG_PATH == main.APP_DATA_DIR / "rainette-music.log"
    assert server.DB_PATH.parent.name == "Rainette Music"


def test_repository_ignores_generated_user_state():
    root = Path(__file__).resolve().parents[1]
    rules = (root / ".gitignore").read_text(encoding="utf-8")
    for generated in ("music.db", "rainette-music.log", "rainette-launcher.log", "__pycache__/"):
        assert generated in rules
