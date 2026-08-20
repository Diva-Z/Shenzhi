"""Serialize per-persona memory file writes to prevent interleaved appends."""
import threading
from pathlib import Path
from typing import Dict


class PersonaMemoryCoordinator:
    """Per-persona write lock ensuring daily.md appends are atomic.

    Multiple flush workers from different sessions of the same persona
    could concurrently open(daily_file, 'a'), causing interleaved content.
    This coordinator serializes writes per persona.
    """
    _instances: Dict[str, "PersonaMemoryCoordinator"] = {}
    _class_lock = threading.Lock()

    def __init__(self, persona_name: str):
        self.persona_name = persona_name
        self._write_lock = threading.Lock()

    @classmethod
    def get(cls, persona_name: str) -> "PersonaMemoryCoordinator":
        with cls._class_lock:
            if persona_name not in cls._instances:
                cls._instances[persona_name] = cls(persona_name)
            return cls._instances[persona_name]

    def append_daily(self, daily_file: Path, content: str):
        """Append content to daily memory file under the write lock."""
        with self._write_lock:
            with open(daily_file, "a", encoding="utf-8") as f:
                f.write(content)

    @classmethod
    def reset_all(cls):
        """For testing: clear all instances."""
        with cls._class_lock:
            cls._instances.clear()
