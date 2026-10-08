"""Phase 2 plan step 4 — the frozen-file backfill, made permanent.

Two invariants the plan asks for ("`.env.example` still matches config —
no silent no-ops", and deferred deps actually getting pinned):

1. every key in `.env.example` — uncommented or commented override — is
   read somewhere (a `Settings` field, or `os.environ`/`os.getenv` directly),
   so an operator who sets it gets an effect;
2. every third-party module ``app/`` imports is pinned in
   `requirements.txt`, so a direct import never rides on a transitive
   dependency again (the ``numpy`` catch of the Phase 2 audit).
"""
import ast
import pathlib
import re
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
REPO = BACKEND.parent

# A config line: `KEY=value` or `# KEY=value` with an ENV-style name.
_CONFIG_LINE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=")

# Import name -> pip distribution name, where they differ.
IMPORT_TO_PIP = {
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "prometheus_client": "prometheus-client",
    "sklearn": "scikit-learn",
    "pydantic_settings": "pydantic-settings",
    "psycopg": "psycopg",
    "PIL": "pillow",
    "cv2": "opencv-python",
}


def _env_keys() -> set[str]:
    keys = set()
    for line in (REPO / ".env.example").read_text().splitlines():
        match = _CONFIG_LINE.match(line)
        if match:
            keys.add(match.group(1))
    return keys


def _os_environ_keys() -> set[str]:
    """Env vars app code reads directly, without going through Settings."""
    keys = set()
    for path in (BACKEND / "app").rglob("*.py"):
        for match in re.finditer(
            r"""(?:os\.environ\.get|os\.getenv)\(\s*["']([A-Z][A-Z0-9_]*)["']""",
            path.read_text(),
        ):
            keys.add(match.group(1))
    return keys


def _requirement_names() -> set[str]:
    names = set()
    for line in (BACKEND / "requirements.txt").read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        names.add(stripped.split(">=")[0].split("[")[0].strip().lower())
    return names


def _app_third_party_imports() -> set[str]:
    third_party = set()
    stdlib = set(sys.stdlib_module_names)
    for path in (BACKEND / "app").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module.split(".")[0]]
            else:
                continue
            third_party.update(
                m for m in modules if m not in stdlib and m != "app"
            )
    return third_party


def test_every_env_example_key_is_actually_read_somewhere():
    from app.config import Settings

    readable = set(Settings.model_fields) | _os_environ_keys()
    silent_no_ops = sorted(_env_keys() - readable)
    assert not silent_no_ops, (
        f".env.example offers {silent_no_ops}, which nothing reads — an "
        "operator would change them and get no effect."
    )


def test_every_app_import_is_pinned_in_requirements():
    from app.config import Settings  # noqa: F401  (ensures import path is sane)

    pinned = _requirement_names()
    unpinned = sorted(
        m
        for m in _app_third_party_imports()
        if IMPORT_TO_PIP.get(m, m).lower() not in pinned
    )
    assert not unpinned, (
        f"{unpinned} imported by backend/app but absent from "
        "requirements.txt — direct imports must be pinned, not left to "
        "transitive dependencies."
    )
