"""Place native entry, looping and exit clips within an explicit preview range."""
from dataclasses import dataclass


@dataclass(frozen=True)
class EffectPhases:
    start: float
    entry_end: float
    exit_start: float
    end: float
    scene_fps: float


def effect_phases(clips: dict, start: float, end: float, scene_fps: float) -> EffectPhases:
    entry, loop, exit_clip = (clips[name] for name in ('in', 'loop', 'out'))
    duration = lambda clip: (clip['end_frame']-clip['start_frame']) / (clip['fps'] or 60) * scene_fps
    entry_end = start + duration(entry)
    exit_start = end - duration(exit_clip)
    if scene_fps <= 0 or duration(loop) <= 0 or entry_end > exit_start:
        raise ValueError('Scene range is too short for the native effect entry and exit clips')
    return EffectPhases(start, entry_end, exit_start, end, scene_fps)


def phase_source_frame(phases: EffectPhases, clips: dict, frame: float) -> tuple[str, float]:
    if frame <= phases.entry_end:
        name, elapsed = 'in', max(0.0, frame-phases.start)
    elif frame >= phases.exit_start:
        name, elapsed = 'out', min(frame, phases.end)-phases.exit_start
    else:
        name, elapsed = 'loop', frame-phases.entry_end
    clip = clips[name]
    offset = elapsed * (clip['fps'] or 60) / phases.scene_fps
    if name == 'loop':
        offset %= clip['end_frame']-clip['start_frame']
    return name, min(clip['end_frame'], clip['start_frame']+offset)
