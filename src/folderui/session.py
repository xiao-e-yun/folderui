"""用程式操作 FolderUI：和按鈕腳本走同一個檔案協定（給 bot、測試、自訂工具迴圈用）。"""
import json
import time
from pathlib import Path

from .fsutil import read_text, write_text


class Session:
    def __init__(self, path: str | Path, poll: float = 0.1):
        self.root = Path(path)
        self.poll = poll

    def ready(self, timeout: float | None = None) -> bool:
        """等到資料夾畫好（第一次可以讀到按鈕列表）。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not (self.root / ".state.json").exists():
            if deadline is not None and time.monotonic() > deadline:
                return False
            time.sleep(self.poll)
        return True

    def inputs(self) -> list[str]:
        """目前可按的按鈕路徑；空的代表已結束（或還輪不到你）。"""
        return json.loads(read_text(self.root / ".state.json", "{}") or "{}").get("inputs", [])

    def read(self, path: str) -> str:
        return read_text(self.root / path, "")

    def input(self, path: str, *args: str, timeout: float | None = None) -> tuple[bool, str]:
        """按下按鈕並等待回應，回傳 (是否成功, 回應內容)。"""
        request = self.root / ".request"
        if request.exists():
            raise RuntimeError("上一個操作還在等待回應")
        (self.root / ".done").unlink(missing_ok=True)
        write_text(request, "".join(f"{x}\0" for x in (path, *args)))
        deadline = None if timeout is None else time.monotonic() + timeout
        while request.exists():
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("等待回應逾時")
            time.sleep(self.poll)
        code, _, text = read_text(self.root / ".response", "1\n").partition("\n")
        (self.root / ".done").unlink(missing_ok=True)
        return code.strip() == "0", text.rstrip("\n")
