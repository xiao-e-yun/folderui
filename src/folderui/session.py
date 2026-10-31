"""Drive a FolderUI from code, using the same file protocol as the button scripts (for bots, tests, custom tool loops)."""
import json
import time
from pathlib import Path

from .fsutil import read_text, write_text


class Session:
    def __init__(self, path: str | Path, poll: float = 0.1):
        self.root = Path(path)
        self.poll = poll

    def ready(self, timeout: float | None = None) -> bool:
        """Wait until the folder has been drawn for the first time."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not (self.root / ".state.json").exists():
            if deadline is not None and time.monotonic() > deadline:
                return False
            time.sleep(self.poll)
        return True

    def inputs(self) -> list[str]:
        """Paths of the buttons available right now; empty means closed (or not your turn yet)."""
        return json.loads(read_text(self.root / ".state.json", "{}") or "{}").get("inputs", [])

    def read(self, path: str) -> str:
        return read_text(self.root / path, "")

    def input(self, path: str, *args: str, timeout: float | None = None) -> tuple[bool, str]:
        """Press a button and wait for the response. Returns (success, response text)."""
        request = self.root / ".request"
        if request.exists():
            raise RuntimeError("the previous action is still waiting for a response")
        (self.root / ".done").unlink(missing_ok=True)
        write_text(request, "".join(f"{x}\0" for x in (path, *args)))
        deadline = None if timeout is None else time.monotonic() + timeout
        while request.exists():
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("timed out waiting for a response")
            time.sleep(self.poll)
        code, _, text = read_text(self.root / ".response", "1\n").partition("\n")
        (self.root / ".done").unlink(missing_ok=True)
        return code.strip() == "0", text.rstrip("\n")
