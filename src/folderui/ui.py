"""FolderUI：把一個資料夾變成給人類或 AI agent 用的立即式 GUI。

子類別實作：
    draw()                 每一幀重新宣告畫面，不可修改狀態：file()、input()、close()
    action(name, args)     按鈕被按下時呼叫：回傳文字，或 raise 代表失敗
    display(result)        （可選）回應時呼叫一次，回傳值就是回應的內容；預設直接回傳 result

資料夾裡只有 draw() 宣告的檔案和按鈕腳本（<path>.sh）。執行按鈕腳本後，腳本會一直等待，
直到「操作處理完之後，第一個有按鈕可按的畫面」出現（或 close()）才印出 display(result) 並結束。
result 是 action 的回傳值或錯誤訊息；exit code 0 代表成功，1 代表失敗。

狀態被其他執行緒改變時（例如其他玩家的操作），呼叫 refresh() 讓這個 UI 重畫。

腳本與主機之間的協定（隱藏檔）：
    .request     腳本寫入：按鈕路徑與參數，以 NUL 分隔
    .response    主機寫入：第一行為 exit code，之後為回應內容
    .done        主機回應後把 .request 改名為 .done；腳本讀完回應後刪除它
    .state.json  目前可按的按鈕（給 Session 用）
"""
import json
import os
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
# folderui 按鈕：{name}
cd {root} || exit 1
printf '%s\\0' {qname} "$@" > .request.new
if [ -e .request ]; then  # 上次被中斷（例如 agent 的指令逾時）：同樣的指令＝繼續等待
  if ! cmp -s .request .request.new; then
    rm -f .request.new
    echo "❌ 上一個操作還在等待回應；再執行一次同樣的指令可以繼續等待"
    exit 1
  fi
  rm -f .request.new
elif [ -e .done ] && cmp -s .done .request.new; then  # 回應已經好了但還沒讀
  rm -f .request.new
else
  rm -f .done .response
  mv .request.new .request
fi
while [ -e .request ]; do sleep 0.2; done
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
        self._dirty = threading.Event()
        self._files: dict[str, str] = {}
        self._inputs: list[str] = []
        self._disk_files: dict[str, str] = {}      # 磁碟上由本框架管理的檔案
        self._disk_scripts: dict[str, str] = {}
        self._pending: tuple[int, str] | None = None
        self._pending_input: str | None = None  # 回應被讀走前保留這個按鈕的腳本

    # ---- 子類別實作 ----

    def draw(self) -> None:
        pass

    def action(self, name: str, args: list[str]) -> str | None:
        raise ValueError(f"沒有處理按鈕 {name}")

    def display(self, result: str) -> str:
        """回應時呼叫一次（所以可以在這裡記錄「已經給這個人看到哪裡」）。
        result 是 action 的回傳值或錯誤訊息；回傳值就是回應的內容。self.inputs 是目前可按的按鈕。"""
        return result

    @property
    def inputs(self) -> list[str]:
        """目前可按的按鈕路徑（最近一幀 draw() 宣告的）。"""
        return list(self._inputs)

    # ---- 在 draw() 中呼叫 ----

    def file(self, path: str, content: str) -> None:
        self._files[_check_path(path)] = content

    def input(self, path: str) -> None:
        """宣告一個按鈕 → <path>.sh"""
        if (path := _check_path(path)) not in self._inputs:
            self._inputs.append(path)

    def close(self) -> None:
        """結束：移除所有按鈕，回應正在等待的腳本，然後 run() 返回。"""
        self.closed = True
        self._dirty.set()

    # ---- 任何執行緒 ----

    def refresh(self) -> None:
        """狀態被其他執行緒改變了：讓這個 UI 重畫（不會等待）。"""
        self._dirty.set()

    def run(self, background: bool = False) -> threading.Thread | None:
        """執行主迴圈，直到 close()。background=True 時在背景執行緒執行並回傳該執行緒。"""
        if background:
            t = threading.Thread(target=self.run, name=f"folderui:{self.root.name}", daemon=True)
            t.start()
            return t
        for name in PROTOCOL_FILES:
            (self.root / name).unlink(missing_ok=True)
        self._redraw()
        deadline = None
        while not (self.closed and self._pending is None and self._pending_input is None):
            if self.closed and self._pending is None:  # 已結束：最後的回應最多等 close_grace 秒被讀走
                deadline = deadline or time.monotonic() + self.close_grace
                if time.monotonic() > deadline:
                    break
            changed = self._dirty.wait(self.poll)
            self._dirty.clear()
            changed |= self._take_request()
            if self._pending is None and self._pending_input and not (self.root / ".done").exists():
                self._pending_input = None  # 回應已被讀走
                changed = True
            if changed:
                self._redraw()
            if self._pending is not None and (self._inputs or self.closed):
                self._respond()
        return None

    # ---- 內部 ----

    def _take_request(self) -> bool:
        if self._pending is not None:
            return False
        try:
            raw = (self.root / ".request").read_bytes()
        except FileNotFoundError:
            return False
        name, *args = [x.decode("utf-8", "replace") for x in raw.split(b"\0")[:-1]] or [""]
        if name not in self._inputs:  # 腳本執行時畫面已經重畫了：渲染層的問題，不經過 action/display，立刻回應
            self._write_response(1, f"❌ 按鈕 {name} 現在不能用（畫面已經更新）")
            return False
        self._pending_input = name
        try:
            result = self.action(name, args)
            self._pending = (0, result or "")
        except Exception as e:
            if not isinstance(e, ValueError):  # ValueError 視為使用者錯誤，其餘是程式 bug
                traceback.print_exc()
            self._pending = (1, f"❌ {e}")
        return True

    def _respond(self) -> None:
        code, result = self._pending
        try:
            text = self.display(result)
        except Exception:
            print(f"[folderui:{self.root.name}] display() 發生錯誤：", file=sys.stderr)
            traceback.print_exc()
            text = result
        self._write_response(code, text)
        self._pending = None

    def _write_response(self, code: int, text: str | None) -> None:
        text = (text or "").strip("\n")
        write_text(self.root / ".response", f"{code}\n" + (text + "\n" if text else ""))
        os.replace(self.root / ".request", self.root / ".done")

    def _redraw(self) -> None:
        self._files, self._inputs = {}, []
        self._call(self.draw)
        if self.closed:
            self._inputs = []
        scripts = {f"{p}.sh": self._script(p) for p in self._inputs}
        if self._pending_input:  # 逾時後重新執行同一個指令＝繼續等待或拿回應，所以腳本要留到回應被讀走
            scripts.setdefault(f"{self._pending_input}.sh", self._script(self._pending_input))
        self._sync(self._disk_files, self._files, None)
        self._sync(self._disk_scripts, scripts, 0o755)
        write_text(self.root / ".state.json", json.dumps({"inputs": self._inputs}, ensure_ascii=False) + "\n")

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

    def _script(self, name: str) -> str:
        return SCRIPT.format(name=name, root=shlex.quote(str(self.root)), qname=shlex.quote(name))

    def _call(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            print(f"[folderui:{self.root.name}] {fn.__name__}() 發生錯誤：", file=sys.stderr)
            traceback.print_exc()
