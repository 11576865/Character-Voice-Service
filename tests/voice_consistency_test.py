"""Generate the same text repeatedly and record objective output differences.

This real-backend integration test does not score voice quality. It records the
sampling baseline, generation times, WAV metadata, hashes, and durations for
later listening and comparison.
"""

from __future__ import annotations

import argparse
import hashlib
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


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VOICE_DIR = PROJECT_ROOT / "voices"
DEFAULT_TEXT = "The stars are especially beautiful tonight. Let us take our time and enjoy the journey together."
SAMPLING_DEFAULTS = {
    "seed": -1,
    "temperature": 1.0,
    "top_k": 15,
    "top_p": 1.0,
}


def request_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def discover_voice(base_url: str, requested_voice: str | None, timeout: float) -> str:
    data = request_json(f"{base_url}/v1/voices", timeout)
    ids = [item["id"] for item in data.get("voices", []) if not item.get("error")]
    if requested_voice:
        if requested_voice not in ids:
            raise RuntimeError(f"Voice {requested_voice!r} is unavailable. Available IDs: {ids}")
        return requested_voice
    if len(ids) != 1:
        raise RuntimeError(
            "Omit --voice only when exactly one valid voice is available. "
            f"Available IDs: {ids}"
        )
    return ids[0]


def load_sampling_parameters(voice: str) -> dict:
    profile_path = VOICE_DIR / f"{voice}.json"
    if not profile_path.is_file():
        raise RuntimeError(f"Local voice profile not found: {profile_path}")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    configured = profile.get("parameters", {})
    return {
        name: configured.get(name, default)
        for name, default in SAMPLING_DEFAULTS.items()
    }


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


def synthesize(base_url: str, request_parameters: dict, timeout: float):
    request = urllib.request.Request(
        f"{base_url}/v1/audio/speech",
        data=json.dumps(request_parameters, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.headers.get_content_type(), response.read()


def summarize(results: list[dict], sampling_parameters: dict) -> dict:
    successes = [item for item in results if item["success"]]
    hashes = [item["sha256"] for item in successes]
    durations = [item["wav"]["duration_seconds"] for item in successes]
    sizes = [item["response_bytes"] for item in successes]
    generation_times = [item["generation_seconds"] for item in successes]
    first_hash = hashes[0] if hashes else None

    return {
        "successful_runs": len(successes),
        "failed_runs": len(results) - len(successes),
        "sampling_parameter_differences": {
            name: {
                "values": [value],
                "varied_between_requests": False,
            }
            for name, value in sampling_parameters.items()
        },
        "file_differences": {
            "unique_sha256_count": len(set(hashes)),
            "all_files_byte_identical": len(set(hashes)) <= 1 if hashes else None,
            "size_bytes_min": min(sizes) if sizes else None,
            "size_bytes_max": max(sizes) if sizes else None,
            "runs_matching_first_file": sum(value == first_hash for value in hashes),
        },
        "duration_differences": {
            "minimum_seconds": min(durations) if durations else None,
            "maximum_seconds": max(durations) if durations else None,
            "mean_seconds": round(statistics.mean(durations), 3) if durations else None,
            "range_seconds": round(max(durations) - min(durations), 3) if durations else None,
        },
        "generation_time": {
            "minimum_seconds": min(generation_times) if generation_times else None,
            "maximum_seconds": max(generation_times) if generation_times else None,
            "mean_seconds": round(statistics.mean(generation_times), 3) if generation_times else None,
        },
    }


def run(args: argparse.Namespace) -> tuple[dict, Path]:
    if args.count <= 0:
        raise ValueError("--count must be greater than zero")
    base_url = args.base_url.rstrip("/")
    voice = discover_voice(base_url, args.voice, args.timeout)
    sampling_parameters = load_sampling_parameters(voice)
    request_parameters = {
        "voice": voice,
        "input": args.text,
        "response_format": "wav",
        "speed": args.speed,
    }
    started_at = datetime.now().astimezone()
    run_dir = args.output_dir or PROJECT_ROOT / "test-results" / "voice-consistency" / started_at.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)

    results = []
    for index in range(1, args.count + 1):
        item = {
            "index": index,
            "success": False,
            "request_parameters": request_parameters.copy(),
            "sampling_parameters": sampling_parameters.copy(),
        }
        request_start = time.perf_counter()
        try:
            status, content_type, audio = synthesize(base_url, request_parameters, args.timeout)
            elapsed = round(time.perf_counter() - request_start, 3)
            if content_type not in {"audio/wav", "audio/x-wav"}:
                raise ValueError(f"unexpected Content-Type: {content_type}")
            wav = inspect_wav(audio)
            output_path = run_dir / f"{index:02d}.wav"
            output_path.write_bytes(audio)
            item.update(
                success=True,
                http_status=status,
                content_type=content_type,
                generation_seconds=elapsed,
                response_bytes=len(audio),
                sha256=hashlib.sha256(audio).hexdigest(),
                wav=wav,
                file=output_path.name,
            )
            print(
                f"[{index:02d}/{args.count:02d}] OK   {elapsed:7.3f}s  "
                f"duration={wav['duration_seconds']:7.3f}s  sha256={item['sha256'][:12]}"
            )
        except urllib.error.HTTPError as exc:
            elapsed = round(time.perf_counter() - request_start, 3)
            item.update(
                generation_seconds=elapsed,
                http_status=exc.code,
                error=exc.read().decode("utf-8", errors="replace"),
            )
            print(f"[{index:02d}/{args.count:02d}] FAIL {elapsed:7.3f}s  HTTP {exc.code}")
        except Exception as exc:
            elapsed = round(time.perf_counter() - request_start, 3)
            item.update(generation_seconds=elapsed, error=str(exc))
            print(f"[{index:02d}/{args.count:02d}] FAIL {elapsed:7.3f}s  {exc}")
        results.append(item)

    report = {
        "purpose": "Record objective variation only; this is not a voice quality score.",
        "started_at": started_at.isoformat(),
        "base_url": base_url,
        "voice": voice,
        "repeat_count": args.count,
        "request_parameters": request_parameters,
        "sampling_parameters": sampling_parameters,
        "summary": summarize(results, sampling_parameters),
        "results": results,
    }
    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report, report_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:9881")
    parser.add_argument("--voice", help="Voice ID; auto-selected when exactly one exists")
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    try:
        report, report_path = run(parse_args())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    summary = report["summary"]
    print("\nObjective difference summary (not a quality score)")
    print(f"  Successful runs: {summary['successful_runs']}/{report['repeat_count']}")
    print(f"  Sampling parameters: {report['sampling_parameters']}")
    print(f"  Unique files: {summary['file_differences']['unique_sha256_count']}")
    print(f"  Duration range: {summary['duration_differences']['range_seconds']}s")
    print(f"  Report: {report_path.resolve()}")
    return 0 if summary["failed_runs"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
