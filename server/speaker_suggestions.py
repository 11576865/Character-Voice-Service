"""Conservative, explainable speaker suggestions for book paragraphs."""

import re


_PREFIX = re.compile(r"^\s*([^:：\n]{1,32})\s*[:：]\s*\S")
_DIALOGUE_START = re.compile(r"^\s*[\"'“‘「『]")
_CN_SPEECH = r"(?:说(?:道)?|问(?:道)?|答(?:道)?|回答|喊(?:道)?|叫(?:道)?|低声说|轻声说|笑道|哭道)"
_EN_SPEECH = r"(?:said|asked|replied|answered|shouted|whispered|called|cried)"


def _alias_pattern(alias: str) -> str:
    escaped = re.escape(alias)
    if alias and alias[0].isascii() and alias[-1].isascii():
        return rf"(?<![\w-]){escaped}(?![\w-])"
    return escaped


def _voice_names(voices: list[dict]) -> tuple[dict[str, set[str]], dict[str, str]]:
    by_label: dict[str, set[str]] = {}
    display = {}
    for voice in voices:
        if voice.get("error") or not voice.get("id"):
            continue
        voice_id = str(voice["id"])
        display[voice_id] = str(voice.get("name") or voice_id)
        for label in (voice_id, voice.get("name")):
            if label:
                by_label.setdefault(str(label).strip().casefold(), set()).add(voice_id)
    return by_label, display


def _mentioned_voices(text: str, labels: dict[str, set[str]]) -> set[str]:
    result = set()
    for label, voices in labels.items():
        if re.search(_alias_pattern(label), text, re.IGNORECASE):
            result.update(voices)
    return result


def _attributed_voice(text: str, labels: dict[str, set[str]]) -> str | None:
    candidates = set()
    for label, voices in labels.items():
        if len(voices) != 1:
            continue
        name = _alias_pattern(label)
        patterns = (
            rf"{name}\s*{_CN_SPEECH}",
            rf"{name}\s+{_EN_SPEECH}\b",
            rf"\b{_EN_SPEECH}\s+{name}",
        )
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            candidates.update(voices)
    return next(iter(candidates)) if len(candidates) == 1 else None


def suggest_speakers(book: dict, voices: list[dict]) -> list[dict]:
    """Suggest explicit speakers and cautious two-person dialogue alternation.

    Saved annotations are treated as locked decisions. Ambiguous dialogue is omitted.
    """
    labels, display = _voice_names(voices)
    suggestions = []
    annotations = book.get("annotations", {})
    for chapter_index, chapter in enumerate(book["document"]["chapters"]):
        scene_pair: set[str] | None = None
        last_speaker: str | None = None
        for paragraph_index, paragraph in enumerate(chapter["paragraphs"]):
            key = f"{chapter_index}:{paragraph_index}"
            text = str(paragraph)
            mentions = _mentioned_voices(text, labels)
            if len(mentions) == 2:
                scene_pair = mentions

            locked = annotations.get(key)
            if locked:
                voice = locked.get("voice")
                if isinstance(voice, str) and (_DIALOGUE_START.match(text) or
                                               _attributed_voice(text, labels)):
                    last_speaker = voice
                continue

            match = _PREFIX.match(text)
            prefix_candidates = labels.get(match.group(1).strip().casefold(), set()) if match else set()
            voice = next(iter(prefix_candidates)) if len(prefix_candidates) == 1 else None
            reason = "explicit_prefix"
            evidence = "段落以唯一匹配的角色名开头"
            confidence = "high"

            if voice is None:
                voice = _attributed_voice(text, labels)
                reason = "speech_attribution"
                evidence = "段落包含角色名和说话提示词"

            if voice is None and _DIALOGUE_START.match(text) and scene_pair and \
                    last_speaker in scene_pair:
                voice = next(iter(scene_pair - {last_speaker}))
                reason = "two_speaker_turn"
                evidence = f"两人场景中接续 {display.get(last_speaker, last_speaker)} 的下一轮对话"
                confidence = "medium"

            if voice is None:
                continue
            suggestions.append({
                "paragraph": key,
                "chapterIndex": chapter_index,
                "paragraphIndex": paragraph_index,
                "voice": voice,
                "voiceName": display.get(voice, voice),
                "confidence": confidence,
                "reason": reason,
                "evidence": evidence,
                "text": text[:240],
            })
            last_speaker = voice
    return suggestions
