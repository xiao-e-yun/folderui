"""FolderUI: turn a folder into an immediate-mode GUI for humans and AI agents.

Subclasses implement:
    draw()                 Return the screen as a list of nodes (Folder, File, Action, Editable, or your own).
                           Called every frame; must not change state.
    action(name, args)     A button was pressed (name is its full path) or a node sent an event up with ctx.emit().
                           Return the response text, or raise to say it failed (e.g. a rejection).
    display(result)        (optional) Called once per response; its return value is the response text.
                           Defaults to returning result unchanged.

Files are written only when what draw() says differs from the last frame (or the file was deleted), so a file
the user changed stays as it is until its content changes. Modes tell what each file is: 444 to read,
644 to edit (Editable), 755 to run (Action). A button script blocks until the first frame after the action that
has a button available (or close()), then prints display(result). Exit codes:
    0  action() and display() returned normally
    1  action() or display() raised: the error message is the response (a crash's traceback goes to stderr)
    2  the framework could not deliver the action ("Operation Unavailable": another action in flight, or a button
       that is gone); action() was not called
`./<button>.sh --help` prints the button's help without pressing it.

When another thread changes the state (e.g. another player's move), call refresh() to redraw this UI.
Editable data is updated on this UI's own thread, between frames.

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

from .fsutil import read_text, write_text
from .nodes import PASS, Ctx

PROTOCOL_FILES = (".request", ".request.new", ".response", ".done")
LOCK = ".lock"

SCRIPT = """\
#!/bin/sh
# folderui button: {name}
if [ "$1" = "--help" ]; then
  cat <<'FOLDERUI_HELP'
{help}
FOLDERUI_HELP
  exit 0
fi
cd {root} || exit 2
if ! mkdir .lock 2>/dev/null; then
  echo "Operation Unavailable"
  echo "Another action is being submitted."
  exit 2
fi
printf '%s\\0' {qname} "$@" > .request.new
if [ -e .request ]; then  # interrupted last time (e.g. the agent's command timed out): same command = keep waiting
  if ! cmp -s .request .request.new; then
    rm -f .request.new
    rmdir .lock
    echo "Operation Unavailable"
    echo "The previous action is still waiting; run the same command again to keep waiting."
    exit 2
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


def _format_error(e: Exception) -> str:
    return "".join(traceback.format_exception_only(type(e), e)).strip()


def _error_text(e: Exception) -> str:
    """What the user sees when the app raised: a ValueError is the app saying no, in its own words; anything
    else is a crash, shown with its type."""
    return str(e) if isinstance(e, ValueError) and str(e) else _format_error(e)


def _signature(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


class _Frame:
    """What one draw() declared."""

    def __init__(self):
        self.files: dict[str, tuple[str, int]] = {}
        self.actions: dict[str, tuple[str, tuple]] = {}   # path -> (help, chain of nodes that placed it)
        self.editables: dict = {}                          # path -> Editable

    def _claim(self, path: str) -> None:
        if path in self.files or path in self.editables or (path.endswith(".sh") and path[:-3] in self.actions):
            raise ValueError(f"{path} is declared twice")

    def add_file(self, path, content, mode=0o444):
        self._claim(path)
        self.files[path] = (content, mode)

    def add_action(self, path, help, chain):
        if path in self.actions or f"{path}.sh" in self.files:
            raise ValueError(f"{path} is declared twice")
        self.actions[path] = (help, chain)

    def add_editable(self, path, node):
        self._claim(path)
        self.editables[path] = node


class FolderUI:
    poll = 0.1
    close_grace = 5.0

    def __init__(self, path: str | Path):
        self.root = Path(path).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.closed = False
        self._dirty = threading.Event()
        self._frame = _Frame()
        self._disk: dict[str, tuple[str, int]] = {}    # files the framework wrote: path -> (content, mode)
        self._edit_seen: dict[str, object] = {}         # editable path -> file signature last written or processed
        self._edit_errors: dict[str, str] = {}
        self._pending: str | None = None  # action result waiting to be sent
        self._pending_input: str | None = None  # keep this button's script until its response is read

    # ---- implemented by subclasses ----

    def draw(self):
        return []

    def action(self, name: str, args: list) -> str | None:
        raise NotImplementedError(f"no handler for {name}")

    def display(self, result: str) -> str:
        """Called once per response (so it may record what this participant has already seen).
        result is the action's return value; the return value is the response text.
        self.inputs lists the buttons available right now."""
        return result

    @property
    def inputs(self) -> list[str]:
        """Paths of the buttons available right now (declared by the latest draw())."""
        return list(self._frame.actions)

    # ---- any thread ----

    def close(self) -> None:
        """Finish: remove all buttons, answer the waiting script, then return from run()."""
        self.closed = True
        self._dirty.set()

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
            changed |= self._take_edits()
            changed |= self._take_request()
            if self._pending is None and self._pending_input and not (self.root / ".done").exists():
                self._pending_input = None  # the response has been read
                changed = True
            if changed:
                self._redraw()
            if self._pending is not None and (self._frame.actions or self.closed):
                self._respond()
        return None

    # ---- internals ----

    def _read(self, path: str) -> str:
        return read_text(self.root / path, "") or ""

    def _dispatch(self, path: str, args: list, chain: tuple):
        """Offer an event to the nodes in chain, innermost first, then to action()."""
        for i in range(len(chain) - 1, -1, -1):
            node, base = chain[i]
            rel = path[len(base) + 1:] if base else path
            result = node.action(rel, args, Ctx(self, None, base, chain[:i + 1]))
            if result is not PASS:
                return result
        return self.action(path, args)

    def _take_request(self) -> bool:
        if self._pending is not None:
            return False
        try:
            raw = (self.root / ".request").read_bytes()
        except FileNotFoundError:
            return False
        name, *args = [x.decode("utf-8", "replace") for x in raw.split(b"\0")[:-1]] or [""]
        if name not in self._frame.actions:  # the screen was redrawn as the script ran: answered here, nothing called
            self._write_response(2, f"Operation Unavailable\n{name} is not on screen.")
            return False
        try:
            result = self._dispatch(name, args, self._frame.actions[name][1]) or ""
        except Exception as e:  # the app said no (or crashed): exit 1 with the reason
            if not isinstance(e, ValueError):
                self._log_error("action")
            self._write_response(1, _error_text(e))
            return True  # the state may have changed halfway, so redraw
        self._pending_input = name
        self._pending = result
        return True

    def _take_edits(self) -> bool:
        """Apply what the user saved into Editable files since we last looked."""
        changed = False
        for path, node in self._frame.editables.items():
            if path not in self._edit_seen:
                continue  # not written yet: the next redraw does it
            sig = _signature(self.root / path)
            if sig == self._edit_seen[path]:
                continue
            changed = True
            self._edit_seen[path] = sig
            if sig is None:
                continue  # deleted: the redraw writes it again
            try:
                node.apply(self._read(path))
                self._edit_errors.pop(path, None)
            except ValueError as e:
                self._edit_errors[path] = f"Your last change to {path.rsplit('/', 1)[-1]} was not applied: {e}"
            except Exception as e:
                self._log_error("Editable")
                self._edit_errors[path] = f"Your last change was not applied: {_format_error(e)}"
        return changed

    def _respond(self) -> None:
        code = 0
        try:
            text = self.display(self._pending)
        except Exception as e:
            if not isinstance(e, ValueError):
                self._log_error("display")
            code, text = 1, _error_text(e)
        self._write_response(code, text)
        self._pending = None

    def _write_response(self, code: int, text: str | None) -> None:
        text = (text or "").strip("\n")
        write_text(self.root / ".response", f"{code}\n" + (text + "\n" if text else ""))
        os.replace(self.root / ".request", self.root / ".done")

    def _redraw(self) -> None:
        frame = _Frame()
        try:
            root = Ctx(self, frame)
            for node in self.draw() or []:
                root.render(node)
        except Exception:
            self._log_error("draw")
            frame = self._frame  # keep showing the last good screen
        if self.closed:
            frame.actions = {}
        self._frame = frame
        want = dict(frame.files)
        for path, (help, _) in frame.actions.items():
            want[f"{path}.sh"] = (self._script(path, help), 0o755)
        if self._pending_input and self._pending_input not in frame.actions:  # re-running the same command resumes waiting
            want.setdefault(f"{self._pending_input}.sh", (self._script(self._pending_input, ""), 0o755))
        for path, node in frame.editables.items():
            if path not in self._disk and path not in self._edit_seen and (self.root / path).exists():
                self._edit_seen[path] = None  # the user's file from before: read it next tick instead of overwriting it
                self._disk[path] = (node.text(), 0o644)
                self._dirty.set()
                continue
            want[path] = (node.text(), 0o644)
        self._sync(want)
        write_text(self.root / ".state.json", json.dumps({"inputs": list(frame.actions)}, ensure_ascii=False) + "\n")

    def _sync(self, want: dict) -> None:
        for path in set(self._disk) - set(want):
            if path in self._frame.editables:
                continue  # the user's file from before, not read yet
            if path in self._edit_seen:  # an Editable no longer drawn: the file is the user's, so it stays
                del self._edit_seen[path]
            else:
                (self.root / path).unlink(missing_ok=True)
                self._prune(self.root / path)
            del self._disk[path]
        for path, (content, mode) in want.items():
            target = self.root / path
            if self._disk.get(path) != (content, mode) or not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                write_text(target, content, mode)
                self._disk[path] = (content, mode)
                if path in self._frame.editables:
                    self._edit_seen[path] = _signature(target)

    def _prune(self, path: Path) -> None:
        for d in path.parents:
            if d == self.root:
                return
            try:
                d.rmdir()
            except OSError:
                return

    def _script(self, name: str, help: str) -> str:
        help = (help or f"./{name}.sh").replace("\nFOLDERUI_HELP", "\n FOLDERUI_HELP")
        return SCRIPT.format(name=name, root=shlex.quote(str(self.root)), qname=shlex.quote(name), help=help)

    def _log_error(self, where: str) -> None:
        print(f"[folderui:{self.root.name}] error in {where}():", file=sys.stderr)
        traceback.print_exc()
