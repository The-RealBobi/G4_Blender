"""Small Blender-independent contracts shared by the G4 import/export adapters."""

from __future__ import annotations

def resolve_effective_joint_mappings(
    parents: dict[str, str | None], explicit_mappings: dict[str, str],
) -> dict[str, str]:
    """Resolve blank bone mappings from the closest mapped parent."""
    resolved: dict[str, str] = {}

    def resolve(name: str, visiting: set[str]) -> str:
        if name in resolved:
            return resolved[name]
        if name in visiting:
            return ""
        value = explicit_mappings.get(name, "")
        if value:
            resolved[name] = value
            return value
        parent = parents.get(name)
        inherited = resolve(parent, visiting | {name}) if parent else ""
        resolved[name] = inherited
        return inherited

    for name in parents:
        resolve(name, set())
    for name, value in explicit_mappings.items():
        if name not in resolved:
            resolved[name] = value
    return resolved


def resolve_animation_joint_drivers(
    parents: dict[str, str | None], explicit_mappings: dict[str, str],
) -> dict[str, str]:
    """Keep one animation driver per branch when children inherit a source joint."""
    effective = resolve_effective_joint_mappings(parents, explicit_mappings)
    drivers: dict[str, str] = {}
    for name, source_name in effective.items():
        if not source_name:
            continue
        parent = parents.get(name)
        visited: set[str] = set()
        inherited = False
        while parent is not None and parent not in visited:
            visited.add(parent)
            if effective.get(parent) == source_name:
                inherited = True
                break
            parent = parents.get(parent)
        if not inherited:
            drivers[name] = source_name
    return drivers
