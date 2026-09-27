"""Conservative, inspectable reference selection for a single character."""

import re


_CUES = {
    "happy": ("开心", "高兴", "笑着", "兴奋", "joy", "happy", "laughed", "smiled"),
    "sad": ("难过", "悲伤", "哭", "哽咽", "sad", "cried", "wept", "sorrow"),
    "angry": ("愤怒", "生气", "怒吼", "angry", "furious", "shouted", "yelled"),
    "fear": ("害怕", "恐惧", "颤抖", "afraid", "scared", "terrified", "trembled"),
    "surprised": ("惊讶", "吃惊", "什么？", "surprised", "astonished", "what?!"),
}


def emotion_cues(text: str) -> dict[str, list[str]]:
    """Return literal rule matches, not a claim about the speaker's intent."""
    normalized = text.casefold()
    matches = {}
    for emotion, cues in _CUES.items():
        found = [cue for cue in cues if (cue in normalized if any(ord(ch) > 127 for ch in cue)
                 else re.search(r"\b" + re.escape(cue) + r"\b", normalized))]
        if found:
            matches[emotion] = found
    return matches


def choose_reference(profile: dict, text: str) -> tuple[str, str]:
    """Return (reference_id, reason); ambiguous text stays on the default."""
    default = profile["default_reference"]
    matches = emotion_cues(text)
    if not matches:
        return default, "default: no emotion cue"
    if len(matches) > 1:
        return default, "default: conflicting emotion cues"
    emotion = next(iter(matches))
    eligible = [
        (ref_id, ref) for ref_id, ref in profile["references"].items()
        if ref.get("emotion", "").casefold() == emotion
        and ref.get("quality", "").casefold() not in {"c", "poor", "unrated"}
    ]
    if not eligible:
        return default, f"default: no reviewed {emotion} reference"
    eligible.sort(key=lambda item: (
        item[1].get("quality", "").casefold() not in {"a", "good"},
        -(item[1].get("intensity") or 0), item[0],
    ))
    return eligible[0][0], f"emotion: {emotion}"
