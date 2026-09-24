"""Sequential 20-paragraph integration test for a running voice service.

This is intentionally not collected by pytest: it calls the real service and
GPT-SoVITS. Run it explicitly after both services are ready.
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
import wave
from datetime import datetime
from pathlib import Path


DEFAULT_PARAGRAPHS = [
    "The morning light reached the windows before the city began to stir.",
    "A quiet breeze moved through the trees and carried the sound of distant traffic.",
    "She opened the book, found her place, and continued reading from the previous evening.",
    "Each sentence arrived at an even pace, with enough time to hear every word clearly.",
    "Outside, footsteps crossed the pavement and slowly disappeared around the corner.",
    "The next chapter began with a journey across a wide and unfamiliar landscape.",
    "Clouds gathered above the hills, but the road ahead remained bright and easy to follow.",
    "At noon, the travelers stopped beside a river and listened to the water moving over stone.",
    "They spoke about the distance already covered and the path that still remained.",
    "After a short rest, everyone packed their belongings and continued toward the north.",
    "The afternoon passed calmly, marked only by birdsong and the steady rhythm of walking.",
    "Near sunset, a small village appeared between the fields at the foot of the mountain.",
    "Warm lights shone from the houses, promising food, conversation, and a safe place to sleep.",
    "The innkeeper welcomed the group and showed them to a table near the fireplace.",
    "While dinner was prepared, they exchanged stories about places they had visited before.",
    "One story made the entire room laugh, and even the tired travelers forgot the long road.",
    "Later, the village grew quiet as doors closed and the final lamps were turned down.",
    "She returned to her room, placed the book beside the bed, and looked out at the stars.",
    "Tomorrow would bring another early start and another stretch of unknown country.",
    "For now, the journey could wait, and the night ended in peaceful silence.",
]


def request_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def discover_voice(base_url: str, requested_voice: str | None, timeout: float) -> str:
    data = request_json(f"{base_url}/v1/voices", timeout)
    voices = [item for item in data.get("voices", []) if not item.get("error")]
    ids = [item["id"] for item in voices]
    if requested_voice:
        if requested_voice not in ids:
            raise RuntimeError(
                f"Voice {requested_voice!r} is not available. Available IDs: {ids}"
            )
        return requested_voice
    if len(ids) != 1:
        raise RuntimeError(
            "Omit --voice only when exactly one valid voice is available. "
            f"Available IDs: {ids}"
        )
    return ids[0]


def inspect_wav(audio: bytes) -> dict:
    if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise ValueError("response does not have a RIFF/WAVE header")
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
    except (EOFError, wave.Error) as exc:
        raise ValueError(f"invalid WAV container: {exc}") from exc
    if channels <= 0 or sample_width <= 0 or sample_rate <= 0 or frame_count <= 0:
        raise ValueError("WAV contains invalid or empty audio parameters")
    return {
        "channels": channels,
        "sample_width_bytes": sample_width,
        "sample_rate_hz": sample_rate,
        "frame_count": frame_count,
        "duration_seconds": round(frame_count / sample_rate, 3),
    }


def synthesize(base_url: str, voice: str, text: str, speed: float, timeout: float):
    body = json.dumps(
        {
            "voice": voice,
            "input": text,
            "response_format": "wav",
            "speed": speed,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/v1/audio/speech",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.headers.get_content_type(), response.read()


def load_paragraphs(path: Path | None) -> list[str]:
    if path is None:
        return DEFAULT_PARAGRAPHS.copy()
    paragraphs = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    paragraphs = [line for line in paragraphs if line]
    if len(paragraphs) != 20:
        raise ValueError(f"paragraph file must contain exactly 20 non-empty lines, got {len(paragraphs)}")
    return paragraphs


def run(args: argparse.Namespace) -> tuple[dict, Path]:
    base_url = args.base_url.rstrip("/")
    voice = discover_voice(base_url, args.voice, args.timeout)
    paragraphs = load_paragraphs(args.paragraphs_file)
    started_at = datetime.now().astimezone()
    run_dir = args.output_dir or Path("test-results") / "continuous-read" / started_at.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)

    results = []
    wall_start = time.perf_counter()
    for index, paragraph in enumerate(paragraphs, start=1):
        request_start = time.perf_counter()
        item = {"index": index, "text": paragraph, "success": False}
        try:
            status, content_type, audio = synthesize(
                base_url, voice, paragraph, args.speed, args.timeout
            )
            elapsed = time.perf_counter() - request_start
            item.update(
                elapsed_seconds=round(elapsed, 3),
                http_status=status,
                content_type=content_type,
                response_bytes=len(audio),
            )
            wav_path = run_dir / f"{index:02d}.wav"
            wav_path.write_bytes(audio)
            if content_type not in {"audio/wav", "audio/x-wav"}:
                raise ValueError(f"unexpected Content-Type: {content_type}")
            item["wav"] = inspect_wav(audio)
            item["file"] = wav_path.name
            item["success"] = True
            print(f"[{index:02d}/20] OK   {elapsed:8.3f}s  {len(audio):9d} bytes")
        except urllib.error.HTTPError as exc:
            elapsed = time.perf_counter() - request_start
            detail = exc.read().decode("utf-8", errors="replace")
            item.update(elapsed_seconds=round(elapsed, 3), http_status=exc.code, error=detail)
            print(f"[{index:02d}/20] FAIL {elapsed:8.3f}s  HTTP {exc.code}")
        except Exception as exc:
            elapsed = time.perf_counter() - request_start
            item.update(elapsed_seconds=round(elapsed, 3), error=str(exc))
            print(f"[{index:02d}/20] FAIL {elapsed:8.3f}s  {exc}")
        results.append(item)

    successful_times = [item["elapsed_seconds"] for item in results if item["success"]]
    report = {
        "started_at": started_at.isoformat(),
        "base_url": base_url,
        "voice": voice,
        "speed": args.speed,
        "paragraph_count": len(paragraphs),
        "success_count": len(successful_times),
        "failure_count": len(paragraphs) - len(successful_times),
        "first_generation_seconds": results[0].get("elapsed_seconds"),
        "average_success_seconds": (
            round(statistics.mean(successful_times), 3) if successful_times else None
        ),
        "total_wall_seconds": round(time.perf_counter() - wall_start, 3),
        "all_wav_valid": all(item["success"] for item in results),
        "results": results,
    }
    report_path = run_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report, report_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:9881")
    parser.add_argument("--voice", help="Voice ID; auto-selected when exactly one exists")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--paragraphs-file", type=Path, help="UTF-8 file with 20 non-empty lines")
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    try:
        report, report_path = run(parse_args())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print("\nSummary")
    print(f"  Success: {report['success_count']}/{report['paragraph_count']}")
    print(f"  Failure: {report['failure_count']}")
    print(f"  First generation: {report['first_generation_seconds']}s")
    print(f"  Average successful generation: {report['average_success_seconds']}s")
    print(f"  Total wall time: {report['total_wall_seconds']}s")
    print(f"  All WAV valid: {report['all_wav_valid']}")
    print(f"  Report: {report_path.resolve()}")
    return 0 if report["failure_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
