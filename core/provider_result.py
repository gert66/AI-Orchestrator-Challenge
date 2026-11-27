from dataclasses import dataclass, field

@dataclass
class ProviderResult:
    argv: list
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    structured: bool
    schema_requested: bool
    parsed_json: object = None
    prompt_chars: int = 0
    error: str = field(default=None)
    provider: str = "unknown"

    def to_dict(self):
        return dict(self.__dict__)
