"""FolderUI: turn a folder into an immediate-mode GUI for humans and AI agents.

Subclasses implement:
    draw()                 Re-declare the whole screen every frame; must not change state: file(), input(), close()
    action(name, args)     Called when a button is pressed: return text (a rejection is just text, too)
    display(result)        (optional) Called once per response; its return value is the response text.
                           Defaults to returning result unchanged.

The folder contains only the files and button scripts (<path>.sh) declared by draw(). A button script
blocks until the first frame after the action that has a button available (or close()), then prints
display(result) and exits with code 0. Like HTTP, anything else (another action in flight, a button
that is gone, an exception in action() or display()) prints "Operation unavailable" and exits with 1.

When another thread changes the state (e.g. another player's move), call refresh() to redraw this UI.

Protocol between scripts and host (hidden files):
    .request     written by the script: button path and arguments, NUL-separated
    .response    written by the host: first line is the exit code, the rest is the response text
    .done        the host renames .request to .done after responding; the script deletes it after reading
                 (a response nobody read is dropped when a different action is submitted)
    .lock        directory held by a script while it submits, so concurrent presses are rejected
    .state.json  buttons available right now (for Session)
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
LOCK = ".lock"

SCRIPT = """\
#!/bin/sh
# folderui button: {name}
cd {root} || exit 1
if ! mkdir .lock 2>/dev/null; then
  echo "❌ Operation unavailable: another action is being submitted"
  exit 1
fi
printf '%s\\0' {qname} "$@" > .request.new
if [ -e .request ]; then  # interrupted last time (e.g. the agent's command timed out): same command = keep waiting
  if ! cmp -s .request .request.new; then
    rm -f .request.new
    rmdir .lock
    echo "❌ Operation unavailable: the previous action is still waiting; run the same command again to keep waiting"
    exit 1
  fi
  rm -f .request.new
elif [ -e .done ] && cmp -s .done .request.new; then  # response is ready but has not been read yet
  rm -f .request.new
else  # a new action; an unread response to a different action is dropped
  rm -f .done .response
  mv .request.new .request
fi
rmdir .lock
while [ -e .request ]; do sleep 0.2; done
code=$(head -n 1 .response)
tail -n +2 .response
rm -f .done
exit "$code"
"""


def _check_path(path: str) -> str:
    parts = path.split("/")
    if path.startswith("/") or any(not p or p.startswith(".") for p in parts):
        raise ValueError(f"path must be relative, with no empty or hidden segments: {path!r}")
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
        self._disk_files: dict[str, str] = {}      # files on disk managed by the framework
        self._disk_scripts: dict[str, str] = {}
        self._pending: str | None = None  # action result waiting to be sent
        self._pending_input: str | None = None  # keep this button's script until its response is read

    # ---- implemented by subclasses ----

    def draw(self) -> None:
        pass

    def action(self, name: str, args: list[str]) -> str | None:
        raise NotImplementedError(f"no handler for button {name}")

    def display(self, result: str) -> str:
        """Called once per response (so it may record what this participant has already seen).
        result is the action's return value; the return value is the response text.
        self.inputs lists the buttons available right now."""
        return result

    @property
    def inputs(self) -> list[str]:
        """Paths of the buttons available right now (declared by the latest draw())."""
        return list(self._inputs)

    # ---- call from draw() ----

    def file(self, path: str, content: str) -> None:
        self._files[_check_path(path)] = content

    def input(self, path: str) -> None:
        """Declare a button -> <path>.sh"""
        if (path := _check_path(path)) not in self._inputs:
            self._inputs.append(path)

    def close(self) -> None:
        """Finish: remove all buttons, answer the waiting script, then return from run()."""
        self.closed = True
        self._dirty.set()

    # ---- any thread ----

    def refresh(self) -> None:
        """The state was changed by another thread: redraw this UI (does not block)."""
        self._dirty.set()

    def run(self, background: bool = False) -> threading.Thread | None:
        """Run the main loop until close(). With background=True, run it in a thread and return the thread."""
        if background:
            t = threading.Thread(target=self.run, name=f"folderui:{self.root.name}", daemon=True)
            t.start()
            return t
        for name in PROTOCOL_FILES:
            (self.root / name).unlink(missing_ok=True)
        if (self.root / LOCK).is_dir():
            (self.root / LOCK).rmdir()
        self._redraw()
        deadline = None
        while not (self.closed and self._pending is None and self._pending_input is None):
            if self.closed and self._pending is None:  # closed: wait up to close_grace seconds for the last response to be read
                deadline = deadline or time.monotonic() + self.close_grace
                if time.monotonic() > deadline:
                    break
            self._dirty.wait(self.poll)
            changed = self._dirty.is_set()  # clear only after seeing it set; a refresh() racing a timeout is never lost
            if changed:
                self._dirty.clear()  # the redraw below runs after this, so it sees the newest state
            changed |= self._take_request()
            if self._pending is None and self._pending_input and not (self.root / ".done").exists():
                self._pending_input = None  # the response has been read
                changed = True
            if changed:
                self._redraw()
            if self._pending is not None and (self._inputs or self.closed):
                self._respond()
        return None

    # ---- internals ----

    def _take_request(self) -> bool:
        if self._pending is not None:
            return False
        try:
            raw = (self.root / ".request").read_bytes()
        except FileNotFoundError:
            return False
        name, *args = [x.decode("utf-8", "replace") for x in raw.split(b"\0")[:-1]] or [""]
        if name not in self._inputs:  # the screen was redrawn as the script ran: a rendering race, answered here without action/display
            self._write_response(1, f"❌ Operation unavailable: {name} is not on screen")
            return False
        try:
            result = self.action(name, args) or ""
        except Exception:  # a bug in the app, like an HTTP 500
            self._log_error("action")
            self._write_response(1, "❌ Operation unavailable: the app failed to handle it")
            return True  # the state may have changed halfway, so redraw
        self._pending_input = name
        self._pending = result
        return True

    def _respond(self) -> None:
        try:
            self._write_response(0, self.display(self._pending))
        except Exception:
            self._log_error("display")
            self._write_response(1, "❌ Operation unavailable: the app failed to handle it")
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
        if self._pending_input:  # re-running the same command resumes waiting, so keep the script until the response is read
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
            self._log_error(fn.__name__)

    def _log_error(self, where: str) -> None:
        print(f"[folderui:{self.root.name}] error in {where}():", file=sys.stderr)
        traceback.print_exc()
