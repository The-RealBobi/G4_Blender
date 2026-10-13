"""Constant-color surface bands for authored outline edges."""
from __future__ import annotations

OUTLINE_SEAM_ATTRIBUTE = "g4_outline_seam"
OUTLINE_COLOR_ATTRIBUTE = "G4 Outline Parameters"
OUTLINE_AUTHORED_KEY = "g4_outline_authored"
STABLE_OUTLINE_COLOR = (255, 153, 191, 127)
BAND_OUTLINE_COLOR = (255, 51, 191, 127)
# A small fraction of each incident triangle keeps paired mask boundaries close.
OUTLINE_BAND_FRACTION = 0.05
Barycentric = tuple[float, float, float]


def clip_outline_polygon(polygon: list[Barycentric], axis: int, inside: bool, width: float) -> list[Barycentric]:
    result = []
    for a, b in zip(polygon, polygon[1:] + polygon[:1]):
        a_inside = a[axis] <= width if inside else a[axis] >= width
        b_inside = b[axis] <= width if inside else b[axis] >= width
        if a_inside:
            result.append(a)
        if a_inside != b_inside:
            fraction = (width - a[axis]) / (b[axis] - a[axis])
            result.append(tuple(x + (y - x) * fraction for x, y in zip(a, b)))
    return result


def outline_triangle_bands(
    faces: list[tuple[int, int, int]], seams: set[tuple[int, int]],
    width: float = OUTLINE_BAND_FRACTION,
) -> list[list[tuple[list[Barycentric], tuple[int, int, int, int]]]]:
    """Partition triangles without any interpolated COLOR or unmatched edge splits."""
    if not 0.0 < width < 0.25:
        raise ValueError("Outline band width must be between 0 and 0.25")
    cuts = {tuple(sorted(edge)) for edge in seams}
    found = set()
    polygons = []
    edge_splits: dict[tuple[int, int], set[float]] = {}
    for face in faces:
        remaining = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
        parts = []
        for axis in range(3):
            edge = tuple(sorted((face[(axis + 1) % 3], face[(axis + 2) % 3])))
            if edge in cuts:
                found.add(edge)
                parts.append((clip_outline_polygon(remaining, axis, True, width), BAND_OUTLINE_COLOR))
                remaining = clip_outline_polygon(remaining, axis, False, width)
        parts.append((remaining, STABLE_OUTLINE_COLOR))
        polygons.append(parts)
        for polygon, _ in parts:
            for bary in polygon:
                for axis in range(3):
                    if abs(bary[axis]) < 1e-12:
                        a, b = sorted((i for i in range(3) if i != axis), key=lambda i: face[i])
                        if 1e-12 < bary[b] < 1.0 - 1e-12:
                            edge_splits.setdefault((face[a], face[b]), set()).add(round(bary[b], 12))
    if found != cuts:
        raise ValueError("Outline marks reference missing surface edges")
    result = []
    for face, parts in zip(faces, polygons):
        triangles = []
        local_vertices = {bary for polygon, _ in parts for bary in polygon}
        for polygon, color in parts:
            boundary = []
            for start, end in zip(polygon, polygon[1:] + polygon[:1]):
                boundary.append(start)
                candidates = set(local_vertices)
                for axis in range(3):
                    if abs(start[axis]) > 1e-12 or abs(end[axis]) > 1e-12:
                        continue
                    a, b = sorted((i for i in range(3) if i != axis), key=lambda i: face[i])
                    splits = edge_splits.get((face[a], face[b]), ())
                    low, high = sorted((start[b], end[b]))
                    for fraction in sorted(splits, reverse=end[b] < start[b]):
                        if low + 1e-10 < fraction < high - 1e-10:
                            bary = [0.0, 0.0, 0.0]
                            bary[a], bary[b] = 1.0 - fraction, fraction
                            candidates.add(tuple(bary))
                direction = tuple(b - a for a, b in zip(start, end))
                squared_length = sum(value * value for value in direction)
                if squared_length == 0:
                    continue
                inserts = {}
                for point in candidates:
                    t = sum((p - a) * d for p, a, d in zip(point, start, direction)) / squared_length
                    if 1e-10 < t < 1.0 - 1e-10 and all(abs(p - a - t * d) < 1e-10 for p, a, d in zip(point, start, direction)):
                        inserts[round(t, 12)] = point
                boundary.extend(inserts[t] for t in sorted(inserts))
            if len(boundary) == 3:
                triangles.append((boundary, color))
            elif len(boundary) > 3:
                center = tuple(sum(bary[i] for bary in boundary) / len(boundary) for i in range(3))
                for a, b in zip(boundary, boundary[1:] + boundary[:1]):
                    triangles.append(([center, a, b], color))
        result.append(triangles)
    return result


def native_outline_edges(positions: list[tuple[float, float, float]],
                         faces: list[tuple[int, ...]], greens: list[list[float]]) -> set[tuple[int, int]]:
    """Recover COLOR seam discontinuities, including duplicated native vertices."""
    sides = {}
    for face, values in zip(faces, greens):
        for index, a in enumerate(face):
            j = (index + 1) % len(face)
            b = face[j]
            pa, pb = positions[a], positions[b]
            if pa == pb:
                continue
            key = tuple(sorted((pa, pb)))
            color = (values[index], values[j]) if pa < pb else (values[j], values[index])
            sides.setdefault(key, []).append((tuple(sorted((a, b))), color))
    marks = set()
    for entries in sides.values():
        if len(entries) == 2 and any(abs(a - b) > 2.0 / 255 for a, b in zip(entries[0][1], entries[1][1])):
            marks.update(edge for edge, _ in entries)
    return marks
