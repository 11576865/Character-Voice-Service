"""Independent CVS inference client against a disposable HTTP GPT-SoVITS fixture.

This helper has no actual model or GPU dependencies. It exercises the
real CVS backend's lock, receipt, weight-switch and /tts request chain.
"""
import json
import os
import sys
from pathlib import Path

from fastapi import HTTPException

import server.backends.gpt_sovits as backend


def main(argv):
    endpoint, state_dir, model = argv
    backend.DATA_DIR = Path(state_dir)
    backend._urls = lambda: (
        endpoint + "/tts",
        endpoint + "/set_gpt_weights",
        endpoint + "/set_sovits_weights",
    )
    profile = {
        "target_language": "en",
        "reference_audio": "test-reference.wav",
        "reference_text": "fixture",
        "reference_language": "en",
        "parameters": {},
        "selected_model": {
            "id": model, "model_id": model,
            "engine": "gpt-sovits",
            "gpt_weights": model + ".gpt",
            "sovits_weights": model + ".sovits",
        },
    }
    try:
        audio = backend.synthesize(model + " text", 1.0, profile)
        print(json.dumps({"ok": True, "audio": audio.decode("ascii")}), flush=True)
        return 0
    except HTTPException as exc:
        print(json.dumps({"ok": False, "status": exc.status_code, "detail": exc.detail}), flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
