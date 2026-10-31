"""Fixed native shader names and configuration filenames."""

from functools import cache
import json
from pathlib import Path
import zlib


@cache
def registered_shaders() -> dict[int, tuple[str, str]]:
    path = Path(__file__).resolve().parents[1] / "data" / "shader_catalog.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, dict):
        raise ValueError("Shader catalog must contain a name-to-configuration mapping")
    result: dict[int, tuple[str, str]] = {}
    for name, configuration in entries.items():
        if not isinstance(name, str) or not isinstance(configuration, str):
            raise ValueError("Shader catalog names and configurations must be strings")
        if not name or Path(configuration).name != configuration or not configuration.endswith(".fxbin"):
            raise ValueError(f"Invalid shader catalog entry: {name}")
        shader_hash = zlib.crc32(name.encode("ascii")) & 0xFFFFFFFF
        if shader_hash in result:
            raise ValueError(f"Ambiguous shader catalog hash: {name}")
        result[shader_hash] = (name, configuration)
    return result
