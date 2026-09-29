from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class TTSEngine(ABC):
    """Stable Character Voice Service adapter boundary for speech engines."""

    engine_id: str
    display_name: str

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Return a small, non-sensitive engine health summary."""

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """Describe features the service/client may safely depend on."""

    @abstractmethod
    def synthesize(self, text: str, speed: float, profile: dict) -> bytes:
        """Synthesize one WAV response from a resolved character selection."""
