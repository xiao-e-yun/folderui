"""FolderUI：把一個資料夾變成給人類或 AI agent 用的立即式 GUI。

子類別實作三個方法：
    draw()                 每一幀重新宣告畫面：file()、button()、status()。不可修改狀態。
    action(name, args)     按鈕被按下時呼叫：回傳文字，或 raise 代表失敗。只有這裡修改狀態。
    receive(msg)           收到 push() 的訊息（例如伺服器推播）。

資料夾裡只有 draw() 宣告的檔案和按鈕腳本（<path>.sh）。執行按鈕腳本後，腳本會一直等待，
直到「操作處理完之後，第一個有按鈕可按的畫面」出現（或 close()）才回應：
    action 的回傳值 + 這段期間的 notify() + status() + 目前可按的按鈕

腳本與主機之間的協定（隱藏檔）：
    .request    腳本寫入：按鈕路徑與參數，以 NUL 分隔
    .response   主機寫入：第一行為 exit code，之後為回應內容
    .done       主機回應後把 .request 改名為 .done；腳本讀完回應後刪除它
    .state.json   目前的 status 與按鈕列表（給程式型 agent 用）
"""
import json
import os
import queue
import shlex
import sys
import threading
import time
import traceback
from pathlib import Path

from .fsutil import write_text

PROTOCOL_FILES = (".request", ".request.new", ".response", ".done")

SCRIPT = """\
#!/bin/sh
# folderui 按鈕：{name}{hint}
cd {root} || exit 1
printf '%s\\0' {qname} "$@" > .request.new
if [ -e .request ]; then
  if cmp -s .request .request.new; then
    rm -f .request.new
    echo "⏳ 繼續等待…" >&2
  else
    rm -f .request.new
    echo "❌ 上一個操作還在等待回應；再執行一次同樣的指令可以繼續等待"
    exit 1
  fi
elif [ -e .done ] && cmp -s .done .request.new; then
  rm -f .request.new
else
  rm -f .done .response
  mv .request.new .request
  echo "⏳ 已送出，等待中…" >&2
fi
limit=$(( ${{FOLDERUI_TIMEOUT:-0}} * 5 )); i=0
while [ -e .request ]; do
  sleep 0.2; i=$((i+1))
  if [ "$limit" -gt 0 ] && [ "$i" -ge "$limit" ]; then
    echo "⏳ 還在等待中，再執行一次同樣的指令即可繼續等待"
    exit 2
  fi
done
code=$(head -n 1 .response)
tail -n +2 .response
rm -f .done
exit "$code"
"""


def _check_path(path: str) -> str:
    parts = path.split("/")
    if path.startswith("/") or any(not p or p.startswith(".") for p in parts):
        raise ValueError(f"路徑不能是絕對路徑、空段落或隱藏檔：{path!r}")
    return path


class FolderUI:
    poll = 0.1
    close_grace = 5.0

    def __init__(self, path: str | Path):
        self.root = Path(path).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.closed = False
        self._inbox: queue.Queue = queue.Queue()
        self._files: dict[str, str] = {}
        self._buttons: dict[str, tuple[str, list[str] | None]] = {}
        self._status = ""
        self._disk_files: dict[str, str] = {}      # 磁碟上由本框架管理的檔案
        self._disk_scripts: dict[str, str] = {}
        self._notes: list[str] = []
        self._pending: tuple[int, str] | None = None
        self._pending_button: tuple[str, str] | None = None  # (路徑, hint)：回應被讀走前保留它的腳本

    # ---- 子類別實作 ----

    def draw(self) -> None:
        pass

    def action(self, name: str, args: list[str]) -> str | None:
        raise ValueError(f"沒有處理按鈕 {name}")

    def receive(self, msg) -> None:
        pass

    # ---- 在 draw() 中呼叫 ----

    def file(self, path: str, content: str) -> None:
        self._files[_check_path(path)] = content

    def button(self, path: str, hint: str = "", options: list[str] | None = None) -> None:
        self._buttons[_check_path(path)] = (hint, options)

    def status(self, text: str) -> None:
        """目前狀況：每次回應時附在最後，也寫進 .state.json。"""
        self._status = text

    # ---- 在 action() / receive() 中呼叫 ----

    def notify(self, text: str) -> None:
        """累積一則消息，在下一次回應時一起送出。"""
        self._notes.append(text)

    def close(self) -> None:
        """結束：移除所有按鈕，回應正在等待的腳本，然後停止。"""
        self.closed = True

    # ---- 給其他執行緒 ----

    def push(self, msg) -> None:
        self._inbox.put(msg)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name=f"folderui:{self.root.name}", daemon=True)
        t.start()
        return t

    # ---- 主迴圈 ----

    def run(self) -> None:
        for name in PROTOCOL_FILES:
            (self.root / name).unlink(missing_ok=True)
        self._redraw()
        deadline = None
        while not (self.closed and self._pending is None and self._pending_button is None):
            if self.closed and self._pending is None:  # 已結束：最後的回應最多等 close_grace 秒被讀走
                deadline = deadline or time.monotonic() + self.close_grace
                if time.monotonic() > deadline:
                    break
            changed = False
            try:
                msg = self._inbox.get(timeout=self.poll)
                self._call(self.receive, msg)
                changed = True
            except queue.Empty:
                pass
            changed |= self._take_request()
            changed |= self._drain()
            if self._pending is None and self._pending_button and not (self.root / ".done").exists():
                self._pending_button = None  # 回應已被讀走
                changed = True
            if changed:
                self._redraw()
            if self._pending is not None and (self._buttons or self.closed):
                self._respond()

    def _drain(self) -> bool:
        changed = False
        while True:
            try:
                msg = self._inbox.get_nowait()
            except queue.Empty:
                return changed
            self._call(self.receive, msg)
            changed = True

    def _take_request(self) -> bool:
        if self._pending is not None:
            return False
        try:
            raw = (self.root / ".request").read_bytes()
        except FileNotFoundError:
            return False
        name, *args = [x.decode("utf-8", "replace") for x in raw.split(b"\0")[:-1]] or [""]
        if name not in self._buttons:
            self._pending = (1, f"❌ 按鈕 {name} 已經不存在")
            return True
        self._pending_button = (name, self._buttons[name][0])
        try:
            result = self.action(name, args)
            self._pending = (0, result or "")
        except Exception as e:
            if not isinstance(e, ValueError):  # ValueError 視為使用者錯誤，其餘是程式 bug
                traceback.print_exc()
            self._pending = (1, f"❌ {e}")
        return True

    def _respond(self) -> None:
        code, text = self._pending
        parts = [text] if text else []
        parts += self._notes
        if self._status:
            parts.append(self._status)
        if self.closed:
            parts.append("── 已結束 ──")
        else:
            lines = ["── 可以做的事 ──"]
            for path, (hint, options) in self._buttons.items():
                lines.append(f"./{path}.sh" + (f"  {hint}" if hint else ""))
                if options:
                    lines.append("    選項：" + " / ".join(options))
            parts.append("\n".join(lines))
        write_text(self.root / ".response", f"{code}\n" + "\n\n".join(parts) + "\n")
        os.replace(self.root / ".request", self.root / ".done")
        self._notes = []
        self._pending = None

    def _redraw(self) -> None:
        self._files, self._buttons, self._status = {}, {}, ""
        self._call(self.draw)
        if self.closed:
            self._buttons = {}
        scripts = {f"{p}.sh": self._script(p, hint) for p, (hint, _) in self._buttons.items()}
        if self._pending_button:  # 逾時後重新執行同一個指令＝繼續等待或拿回應，所以腳本要留到回應被讀走
            name, hint = self._pending_button
            scripts.setdefault(f"{name}.sh", self._script(name, hint))
        self._sync(self._disk_files, self._files, None)
        self._sync(self._disk_scripts, scripts, 0o755)
        write_text(self.root / ".state.json", json.dumps({
            "status": self._status,
            "buttons": {p: {"hint": h, "options": o} for p, (h, o) in self._buttons.items()},
        }, ensure_ascii=False, indent=2) + "\n")

    def _sync(self, disk: dict[str, str], want: dict[str, str], mode: int | None) -> None:
        for path in set(disk) - set(want):
            (self.root / path).unlink(missing_ok=True)
            self._prune(self.root / path)
            del disk[path]
        for path, content in want.items():
            if disk.get(path) != content:
                target = self.root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                write_text(target, content, mode)
                disk[path] = content

    def _prune(self, path: Path) -> None:
        for d in path.parents:
            if d == self.root:
                return
            try:
                d.rmdir()
            except OSError:
                return

    def _script(self, name: str, hint: str) -> str:
        hint = " ".join(hint.split())
        return SCRIPT.format(name=name, hint=f" — {hint}" if hint else "",
                             root=shlex.quote(str(self.root)), qname=shlex.quote(name))

    def _call(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            print(f"[folderui:{self.root.name}] {fn.__name__}() 發生錯誤：", file=sys.stderr)
            traceback.print_exc()
