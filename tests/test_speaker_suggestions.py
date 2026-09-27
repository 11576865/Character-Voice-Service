from server.speaker_suggestions import suggest_speakers


VOICES = [
    {"id": "march-7th", "name": "三月七"},
    {"id": "silver-wolf", "name": "银狼"},
    {"id": "kafka", "name": "Kafka"},
]


def _book(paragraphs, annotations=None):
    return {"document": {"chapters": [{"title": "One", "paragraphs": paragraphs}]},
            "annotations": annotations or {}}


def test_explicit_prefix_and_chinese_or_english_attribution():
    suggestions = suggest_speakers(_book([
        "三月七：我们出发吧。",
        "“先等等。”银狼说道。",
        '"Stay close," Kafka said.',
    ]), VOICES)
    assert [(item["paragraph"], item["voice"], item["confidence"]) for item in suggestions] == [
        ("0:0", "march-7th", "high"),
        ("0:1", "silver-wolf", "high"),
        ("0:2", "kafka", "high"),
    ]
    assert all(item["evidence"] for item in suggestions)


def test_two_person_turn_is_medium_and_ambiguous_first_line_is_omitted():
    suggestions = suggest_speakers(_book([
        "三月七和银狼走进房间。",
        "“你先说。”",
        "“我没有意见。”三月七说道。",
        "“那就这么决定了。”",
    ]), VOICES)
    assert [(item["paragraph"], item["voice"], item["reason"]) for item in suggestions] == [
        ("0:2", "march-7th", "speech_attribution"),
        ("0:3", "silver-wolf", "two_speaker_turn"),
    ]
    assert suggestions[-1]["confidence"] == "medium"


def test_saved_annotation_is_locked_and_feeds_next_dialogue_turn():
    suggestions = suggest_speakers(_book([
        "三月七和银狼正在交谈。",
        "“这段已经由用户确认。”",
        "“这一句可以谨慎推断。”",
        "三月七：即使明确也不能覆盖。",
    ], {
        "0:1": {"voice": "march-7th", "source": "manual", "locked": True},
        "0:3": {"voice": "silver-wolf", "source": "manual", "locked": True},
    }), VOICES)
    assert [(item["paragraph"], item["voice"]) for item in suggestions] == [
        ("0:2", "silver-wolf"),
    ]


def test_ambiguous_shared_display_name_is_not_suggested():
    voices = [{"id": "one", "name": "Same"}, {"id": "two", "name": "Same"}]
    assert suggest_speakers(_book(["Same: hello"]), voices) == []
