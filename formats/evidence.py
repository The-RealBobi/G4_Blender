"""Evidence-first discovery for formats mentioned by the executable."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .contracts import ResourceEvidence, ResourceIdentity


@dataclass(frozen=True)
class FormatEvidence:
    extension: str
    count: int
    samples: tuple[ResourceEvidence, ...]
    roots: tuple[str, ...]
    status: str


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def discover_format_evidence(
    extension: str,
    roots: Iterable[Path | str],
    sample_limit: int = 8,
) -> FormatEvidence:
    """Find real files before a format is admitted to the addon.

    Hidden work folders are intentionally included. The function never parses
    a file and therefore remains safe for incomplete dumps.
    """

    normalized = extension.lower()
    if not normalized.startswith("."):
        normalized = "." + normalized
    root_names: list[str] = []
    paths: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for root_value in roots:
        root = Path(root_value).expanduser()
        root_names.append(str(root))
        if not root.exists():
            continue
        for path in root.rglob(f"*{normalized}"):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            paths.append((root, path))
    paths.sort(key=lambda item: str(item[1]).casefold())
    samples = tuple(
        ResourceEvidence(
            ResourceIdentity(
                path=str(path),
                extension=normalized,
                source_root=str(root),
                sha256=_digest(path),
            ),
            confidence="confirmed-file",
        )
        for root, path in paths[:sample_limit]
    )
    return FormatEvidence(
        extension=normalized,
        count=len(paths),
        samples=samples,
        roots=tuple(root_names),
        status="confirmed-file" if paths else "executable-only",
    )


def build_format_evidence(roots: Iterable[Path | str], extensions: Iterable[str]) -> tuple[FormatEvidence, ...]:
    return tuple(discover_format_evidence(extension, roots) for extension in extensions)
