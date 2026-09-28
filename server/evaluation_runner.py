"""Run resumable, local model Evaluations without hiding provenance."""

import io
import threading
import time
import wave
from typing import Callable

from server.model_registry import ModelRegistry
from server.model_scan import read_roots
from server.voice_profiles import read_valid_profile, resolve_profile_selection


class EvaluationRunner:
    def __init__(self, manager, synthesize: Callable[[str, float, dict], bytes]):
        self.manager = manager
        self.synthesize = synthesize

    @staticmethod
    def _duration_ms(audio: bytes) -> float:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate()
        if rate <= 0:
            raise ValueError("Generated WAV has an invalid sample rate")
        return round(frames * 1000 / rate, 3)

    def _selection(self, record: dict, revision: dict) -> tuple[dict, str, dict]:
        profile = read_valid_profile(self.manager.voice_dir / f'{record["character_id"]}.json')
        reference_id = record.get("reference_set_id") or profile["default_reference"]
        selection = resolve_profile_selection(profile, reference_id=reference_id)
        artifacts = ModelRegistry.resolve_artifacts(revision, read_roots(self.manager.roots_path))
        selection["selected_model"] = {
            "id": revision.get("model_id") or revision["revision_id"],
            "name": revision.get("name") or revision["revision_id"],
            "engine": revision["engine"],
            "version": revision.get("layout") or "",
            "revision_id": revision["revision_id"],
            "gpt_weights": str(artifacts["gpt"]),
            "sovits_weights": str(artifacts["sovits"]),
            "managed": True,
            "parameters": {},
        }
        requested = dict(record.get("requested_parameters") or {})
        speed = float(requested.pop("speed", 1.0))
        if speed <= 0:
            raise ValueError("Evaluation speed must be positive")
        selection["parameters"].update(requested)
        effective = {**selection["parameters"], "speed": speed}
        return selection, reference_id, effective

    def run(self, evaluation_id: str, cancel: threading.Event) -> dict:
        store = self.manager.evaluations
        record = store.get(evaluation_id)
        if record["status"] == "completed":
            return record
        registry = self.manager.registry.read()
        revision_ids = [record["candidate_revision_id"]]
        if record.get("baseline_revision_id"):
            revision_ids.append(record["baseline_revision_id"])
        store.update(evaluation_id, status="running")
        try:
            for revision_id in revision_ids:
                revision = registry["revisions"].get(revision_id)
                if not revision:
                    raise ValueError(f"Evaluation revision is no longer registered: {revision_id}")
                selection, reference_id, effective = self._selection(record, revision)
                store.update(evaluation_id, effective_parameters=effective,
                             unsupported_parameters=[])
                for sample in record["sample_set"]["samples"]:
                    if cancel.is_set():
                        return store.update(evaluation_id, status="cancelled")
                    previous = next((item for item in store.get(evaluation_id)["results"]
                                     if item["sample_id"] == sample["sample_id"] and
                                     item["model_revision_id"] == revision_id), None)
                    if previous and previous.get("status") == "success" and \
                            store.audio_is_valid(evaluation_id, previous):
                        continue
                    started = time.perf_counter()
                    base = {"sample_id": sample["sample_id"],
                            "model_revision_id": revision_id,
                            "actual_reference_id": reference_id,
                            "effective_parameters": effective}
                    try:
                        audio = self.synthesize(sample["text"], effective["speed"], selection)
                        synthesis_ms = round((time.perf_counter() - started) * 1000, 3)
                        duration_ms = self._duration_ms(audio)
                        artifact_id, digest = store.write_audio(evaluation_id, audio)
                        result = {**base, "status": "success", "audio_artifact_id": artifact_id,
                                  "audio_sha256": digest, "queue_ms": 0.0, "load_ms": None,
                                  "synthesis_ms": synthesis_ms, "audio_duration_ms": duration_ms,
                                  "real_time_factor": round(synthesis_ms / duration_ms, 6),
                                  "error_code": None, "error_message": None}
                    except Exception as exc:
                        result = {**base, "status": "failed", "audio_artifact_id": None,
                                  "audio_sha256": None, "queue_ms": 0.0, "load_ms": None,
                                  "synthesis_ms": round((time.perf_counter() - started) * 1000, 3),
                                  "audio_duration_ms": None, "real_time_factor": None,
                                  "error_code": type(exc).__name__,
                                  "error_message": str(exc)[:1000]}
                    store.update(evaluation_id, result=result)
            return store.update(evaluation_id, status="completed")
        except Exception:
            store.update(evaluation_id, status="failed")
            raise
