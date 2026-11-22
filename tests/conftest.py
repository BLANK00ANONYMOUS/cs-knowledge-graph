import os
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
os.environ.setdefault("KG_NO_KEYRING", "1")   # tests never touch the real OS credential store
for var in ("GITHUB_TOKEN", "GH_TOKEN"):
    os.environ.pop(var, None)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """An empty vault in a temp folder, wired in as the configured vault."""
    import kg
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setattr(kg, "load_config", lambda: (v, tmp_path / "books"))
    return v


def note(path: Path, meta: str, body: str = "> summary\n", bom: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\n{meta.strip()}\n---\n{body}"
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return path
