from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    decision_seconds: float = 3.0
    request_seconds: float = 4.5
    max_body: int = 2 * 1024 * 1024
    max_sessions: int = 16
    cached_rounds: int = 64

DEFAULT = Config()
