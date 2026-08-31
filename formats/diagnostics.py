"""Stable diagnostics shared by native readers and Blender adapters."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    source: str = "<memory>"
    offset: int | None = None
    severity: str = "error"

    def user_message(self) -> str:
        location = self.source
        if self.offset is not None:
            location = f"{location} at 0x{self.offset:X}"
        return f"{self.message} ({location})"


@dataclass
class DiagnosticReport:
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def add(self, code: str, message: str, source: str = "<memory>", offset: int | None = None, severity: str = "error") -> None:
        self.diagnostics.append(Diagnostic(code, message, source, offset, severity))

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "error")

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors


class NativeFormatError(ValueError):
    def __init__(self, diagnostic: Diagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.user_message())
