"""Dependency-free checks for the local book and export pipeline."""

import io
import json
import tempfile
import unittest
import wave
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from server.book_library import BookLibrary
from server.document_import import parse_document
from server.emotion_router import choose_reference
from server.epub_export import export_read_aloud
from server.reference_import import import_reference_pack
from server.speaker_suggestions import suggest_speakers
from server.pronunciations import spoken_text, validate_rules


def sample_wav():
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 1600)
    return data.getvalue()


class LocalPipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_reference_import_updates_same_source(self):
        voices = self.root / "voices"
        voices.mkdir()
        pack = self.root / "pack"
        (pack / "audio").mkdir(parents=True)
        (pack / "audio" / "one.wav").write_bytes(sample_wav())
        profile = {"schema_version": 2, "name": "Test", "target_language": "en",
                   "default_model": "loaded", "models": {"loaded": {}},
                   "default_reference": "default", "references": {
                       "default": {"audio": "C:/default.wav", "text": "Default.", "language": "en"}}}
        (voices / "test.json").write_text(json.dumps(profile), encoding="utf-8")
        catalog = {"source_project": "HSR", "references": [{
            "id": "1", "source_member_id": "line/one.wav", "audio": "audio/one.wav",
            "text": "Hello.", "language": "en", "emotion": "happy", "quality": "A"}]}
        (pack / "reference_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        import_reference_pack(pack, "test", voice_dir=voices, reference_dir=self.root / "refs")
        catalog["references"][0]["text"] = "Corrected."
        (pack / "reference_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        import_reference_pack(pack, "test", voice_dir=voices, reference_dir=self.root / "refs")
        result = json.loads((voices / "test.json").read_text(encoding="utf-8"))
        self.assertEqual(len(result["references"]), 2)
        imported = next(value for key, value in result["references"].items() if key != "default")
        self.assertEqual(imported["text"], "Corrected.")

    def test_book_versions_and_export(self):
        library = BookLibrary(self.root / "data")
        document = {"title": "Hello", "chapters": [{"title": "One", "paragraphs": ["Hello world."]}]}
        segments = [{"chapterIndex": 0, "paragraphIndex": 0, "start": 0,
                     "end": 12, "text": "Hello world."}]
        book = library.put_book(document, segments, kind="txt")
        self.assertEqual(library.put_book(document, segments, kind="txt")["id"], book["id"])
        segment_id = book["segments"][0]["id"]
        for index in range(8):
            library.add_version(book["id"], segment_id, sample_wav(), {"number": index})
        self.assertEqual(len(library.versions(book["id"])[segment_id]["versions"]), 7)
        self.assertTrue(library.audio_path(book["id"], segment_id).is_file())
        with wave.open(str(library.combined_wav(book["id"])), "rb") as combined:
            self.assertEqual(combined.getnframes(), 1600)
        epub = export_read_aloud(library, book["id"])
        with zipfile.ZipFile(epub) as archive:
            self.assertEqual(archive.namelist()[0], "mimetype")
            self.assertEqual(archive.read("mimetype"), b"application/epub+zip")
            ET.fromstring(archive.read("OEBPS/content.opf"))
            smil = ET.fromstring(archive.read("OEBPS/chapter-0001.smil"))
            self.assertTrue(smil.findall(".//{http://www.w3.org/ns/SMIL}audio"))

    def test_document_parsing_and_emotion_fallback(self):
        parsed = parse_document(b"# Heading\nA paragraph.\n", "md", "notes.md")
        self.assertEqual(parsed["chapters"][0]["title"], "Heading")
        self.assertIn("A paragraph.", parsed["chapters"][0]["paragraphs"])
        profile = {"default_reference": "neutral", "references": {
            "neutral": {"emotion": "neutral", "quality": "A"},
            "happy": {"emotion": "happy", "quality": "A", "intensity": 0.7}}}
        self.assertEqual(choose_reference(profile, "She laughed happily.")[0], "happy")
        self.assertEqual(choose_reference(profile, "She walked home.")[0], "neutral")

    def test_speaker_suggestions_require_explicit_unambiguous_prefix(self):
        book = {"document": {"chapters": [{"paragraphs": [
            "March 7th: Hello!", "She answered softly.", "Unknown: Hello!",
        ]}]}, "annotations": {}}
        voices = [{"id": "march", "name": "March 7th"}, {"id": "other", "name": "Other"}]
        suggestions = suggest_speakers(book, voices)
        self.assertEqual([(item["paragraph"], item["voice"]) for item in suggestions],
                         [("0:0", "march")])

    def test_pronunciation_rules_change_only_spoken_text(self):
        rules = validate_rules({"March 7th": "March Seventh"})
        self.assertEqual(spoken_text("Hello, March 7th.", rules), "Hello, March Seventh.")
        with self.assertRaises(ValueError):
            validate_rules({"": "invalid"})


if __name__ == "__main__":
    unittest.main()
