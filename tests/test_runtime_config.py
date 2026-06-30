import os
import subprocess
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "deploy" / "frontend-runtime-config.sh"


def _render_runtime_config(tmp_path: Path, *, api_key: str | None = None, vite_api_key: str | None = None) -> str:
    output_path = tmp_path / "runtime-config.js"
    env = os.environ.copy()
    env["CONFIG_PATH"] = str(output_path)
    if api_key is None:
        env.pop("API_KEY", None)
    else:
        env["API_KEY"] = api_key
    if vite_api_key is None:
        env.pop("VITE_API_KEY", None)
    else:
        env["VITE_API_KEY"] = vite_api_key
    subprocess.run(["/bin/sh", str(SCRIPT_PATH)], check=True, env=env)
    return output_path.read_text(encoding="utf-8")


def test_runtime_config_does_not_fall_back_to_backend_api_key(tmp_path: Path) -> None:
    rendered = _render_runtime_config(tmp_path, api_key="server-secret", vite_api_key=None)

    assert 'apiKey: ""' in rendered
    assert "server-secret" not in rendered


def test_runtime_config_uses_vite_api_key_when_explicitly_set(tmp_path: Path) -> None:
    rendered = _render_runtime_config(tmp_path, api_key="server-secret", vite_api_key="public-client-key")

    assert 'apiKey: "public-client-key"' in rendered
    assert "server-secret" not in rendered
