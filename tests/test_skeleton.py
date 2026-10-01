"""TASK-001 smoke tests: the skeleton imports and follows the basic rules."""

import ast
from pathlib import Path

from bahikhata import config


def test_config_paths_are_inside_project():
    for path in (config.DATA_DIR, config.IMAGES_DIR, config.CACHE_DIR,
                 config.LEDGER_DB_PATH, config.PROMPTS_DIR):
        assert config.PROJECT_ROOT in path.parents


def test_money_settings_are_integers():
    # Money is integer paisa everywhere (ARCHITECTURE.md §8).
    assert isinstance(config.AMOUNT_TOLERANCE_PAISA, int)
    assert isinstance(config.VAT_RATE_PERCENT, int)


def test_core_package_never_imports_streamlit():
    # ARCHITECTURE.md §17: bahikhata/ must not import Streamlit.
    package_dir = Path(config.__file__).parent
    for py_file in package_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(n.split(".")[0] == "streamlit" for n in names), py_file.name


def test_missing_api_key_gives_clear_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert config.has_gemini_api_key() is False
