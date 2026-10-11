import os
import secrets
import stat
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VOICE_DIR = PROJECT_ROOT / "voices"
DATA_DIR = Path(os.environ.get("CVS_DATA_DIR", str(PROJECT_ROOT / "data"))).expanduser()


def _read_private_admin_token(path: Path) -> str | None:
    """Return committed token, or None for an in-flight exclusive create."""
    # Never follow an explicitly redirected admin token file. The new data
    # directory is private by default, but old/externally managed roots
    # need this defense as well.
    if path.is_symlink():
        raise RuntimeError("CVS admin token path must not be a symlink")
    if not path.is_file():
        raise RuntimeError("CVS admin token path is not a regular file")
    if os.name != "nt":
        permissions = stat.S_IMODE(path.stat().st_mode)
        if permissions & 0o077:
            path.chmod(0o600)
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        return None
    return token


def _admin_token() -> str:
    configured = os.environ.get("CVS_ADMIN_TOKEN", "").strip()
    if configured:
        return configured

    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = DATA_DIR / "admin-token.txt"

    # O_CREAT|O_EXCL selects one token publisher across independent CVS
    # processes. The losing processes wait for that same complete token,
    # rather than returning different secrets or overwriting the winner.
    # On POSIX, 0600 is enforced even when the process umask is permissive.
    for _ in range(100):
        try:
            descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            token = _read_private_admin_token(path)
            if token:
                return token
            time.sleep(0.05)  # Another process may be fsyncing the new file.
            continue

        generated = secrets.token_urlsafe(32)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(generated + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return generated

    # An empty file can mean that the first creator crashed. Never
    # silently replace a possibly committed credential with a new one.
    raise RuntimeError(
        "CVS admin token initialization incomplete: empty admin-token.txt; "
        "inspect it before manual recovery"
    )

ADMIN_TOKEN = _admin_token()

GPT_SOVITS_BASE_URL = os.environ.get(
    "CVS_GPT_SOVITS_BASE_URL", "http://127.0.0.1:9880"
).rstrip("/")
GPT_SOVITS_TTS_URL = f"{GPT_SOVITS_BASE_URL}/tts"
GPT_SOVITS_HEALTH_URL = f"{GPT_SOVITS_BASE_URL}/docs"
GPT_SOVITS_SET_GPT_WEIGHTS_URL = f"{GPT_SOVITS_BASE_URL}/set_gpt_weights"
GPT_SOVITS_SET_SOVITS_WEIGHTS_URL = f"{GPT_SOVITS_BASE_URL}/set_sovits_weights"

INDEX_TTS_BASE_URL = os.environ.get(
    "CVS_INDEX_TTS_BASE_URL", "http://127.0.0.1:9882"
).rstrip("/")
INDEX_TTS_HEALTH_URL = f"{INDEX_TTS_BASE_URL}/health"
INDEX_TTS_TTS_URL = f"{INDEX_TTS_BASE_URL}/synthesize"

HOST = os.environ.get("CVS_HOST", "127.0.0.1")
PORT = int(os.environ.get("CVS_PORT", "9881"))

SAVE_GENERATED_WAV = os.environ.get("CVS_SAVE_GENERATED_WAV", "0") == "1"
SAVE_DIR = Path(os.environ.get("CVS_SAVE_DIR", str(Path.home() / "Music"))).expanduser()
