import dataclasses
import os
import subprocess
import threading
import time

import pytest

from folderui import PASS, Action, Editable, File, Folder, FolderUI, Node, Session


class Counter(FolderUI):
    """Single player: add increments by one and closes at 3."""

    def __init__(self, path):
        super().__init__(path)
        self.n = 0

    def draw(self):
        return [File("count.md", f"{self.n}\n"), Action("add", help="add 1: ./add.sh 1")]

    def action(self, name, args):
        if args != ["1"]:
            return "can only add 1"
        self.n += 1
        if self.n == 3:
            self.close()
        return f"now {self.n}"


@pytest.fixture
def counter(tmp_path):
    ui = Counter(tmp_path / "ui")
    thread = ui.run(background=True)
    s = Session(ui.root)
    assert s.ready(timeout=5)
    yield ui, s
    ui.close()
    thread.join(timeout=5)


def test_session_press(counter):
    ui, s = counter
    assert s.inputs() == ["add"]
    ok, text = s.input("add", "2", timeout=5)
    assert ok and text == "can only add 1"  # a rejection is just the app's text
    ok, text = s.input("add", "1", timeout=5)
    assert ok and text == "now 1"
    assert s.read("count.md") == "1\n"


def test_close_removes_buttons(counter):
    ui, s = counter
    for _ in range(3):
        ok, text = s.input("add", "1", timeout=5)
    assert ok and text == "now 3"
    assert s.inputs() == []
    for _ in range(50):  # the kept script is removed on the frame after the response is read
        if not (ui.root / "add.sh").exists():
            break
        time.sleep(0.1)
    assert not (ui.root / "add.sh").exists()


def test_shell_button(counter):
    ui, s = counter
    r = subprocess.run([str(ui.root / "add.sh"), "1"], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and "now 1" in r.stdout
    r = subprocess.run([str(ui.root / "add.sh"), "x"], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and "can only add 1" in r.stdout


class Lobby:
    """Start once both players press ready: shared state + a lock + refresh()."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ready: set = set()
        self.events: list[str] = []
        self.uis: list[FolderUI] = []

    def press_ready(self, who):
        with self.lock:
            self.ready.add(who)
            if len(self.ready) == len(self.uis):
                self.events.append("🔔 started")
        for ui in self.uis:
            ui.refresh()
        return "✅ ready"


class LobbyUI(FolderUI):
    def __init__(self, path, lobby, me):
        super().__init__(path)
        self.lobby, self.me, self.played = lobby, me, False
        self.seen = 0

    def draw(self):
        with self.lobby.lock:
            ready, started = self.me in self.lobby.ready, len(self.lobby.ready) == len(self.lobby.uis)
        if self.played:
            self.close()
        elif not ready:
            return [Action("ready")]
        elif started:
            return [Action("play")]
        return []  # ready but not started -> no buttons -> the script keeps waiting

    def display(self, result):  # called once per response -> include only unseen messages
        with self.lobby.lock:
            new, self.seen = self.lobby.events[self.seen:], len(self.lobby.events)
        return "\n".join([result, *new])

    def action(self, name, args):
        if name == "ready":
            return self.lobby.press_ready(self.me)
        self.played = True
        return "played"


def make_lobby(tmp_path):
    lobby = Lobby()
    lobby.uis = [LobbyUI(tmp_path / f"p{p}", lobby, p) for p in range(2)]
    threads = [ui.run(background=True) for ui in lobby.uis]
    sessions = [Session(ui.root) for ui in lobby.uis]
    assert all(s.ready(timeout=5) for s in sessions)
    return lobby, threads, sessions


def test_multiplayer_blocks_until_everyone_ready(tmp_path):
    lobby, threads, (s0, s1) = make_lobby(tmp_path)

    result = {}
    t = threading.Thread(target=lambda: result.update(r=s0.input("ready", timeout=10)))
    t.start()
    t.join(timeout=0.5)
    assert t.is_alive()  # the other player isn't ready, so p0's button blocks

    ok, text = s1.input("ready", timeout=10)
    t.join(timeout=10)
    assert ok and "started" in text and s1.inputs() == ["play"]
    ok, text = result["r"]
    assert ok and "started" in text

    for s in (s0, s1):
        ok, text = s.input("play", timeout=10)
        assert ok and text == "played"  # messages already seen are not repeated
    for th in threads:
        th.join(timeout=10)
        assert not th.is_alive()


def test_interrupted_and_resume(tmp_path):
    lobby, threads, (s0, s1) = make_lobby(tmp_path)
    script = str(lobby.uis[0].root / "ready.sh")

    with pytest.raises(subprocess.TimeoutExpired):  # simulate the agent's command being killed on timeout; the request was already sent
        subprocess.run([script], capture_output=True, text=True, timeout=1)

    r = subprocess.run([script, "other"], capture_output=True, text=True, timeout=10)
    assert r.returncode == 1 and "still waiting" in r.stdout  # a different command is rejected

    s1.input("ready", timeout=10)
    r = subprocess.run([script], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and "started" in r.stdout  # the same command gets the response
    for ui in lobby.uis:
        ui.close()


def test_stale_input_is_rejected_without_calling_action(counter):
    ui, s = counter
    calls = []
    ui.action = lambda name, args: calls.append(name)
    ok, text = s.input("gone", timeout=5)  # no such button on screen (e.g. a script from an old screen)
    assert not ok and text == "Operation Unavailable\ngone is not on screen." and calls == []


def test_concurrent_press_is_rejected_by_lock(counter):
    ui, s = counter
    (ui.root / ".lock").mkdir()  # another script is in the middle of submitting
    r = subprocess.run([str(ui.root / "add.sh"), "1"], capture_output=True, text=True, timeout=10)
    assert r.returncode == 1 and r.stdout == "Operation Unavailable\nAnother action is being submitted.\n"
    (ui.root / ".lock").rmdir()
    ok, text = s.input("add", "1", timeout=5)
    assert ok and text == "now 1"


def test_exception_in_action_is_printed(counter):
    ui, s = counter
    ui.action = lambda name, args: 1 / 0
    ok, text = s.input("add", "1", timeout=5)  # the app's problem: delivered, the error is the response
    assert ok and text == "ZeroDivisionError: division by zero"
    ui.action = Counter.action.__get__(ui)
    ok, text = s.input("add", "1", timeout=5)  # the UI keeps working
    assert ok and text == "now 1"


def wait_for(fn, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def start(ui):
    thread = ui.run(background=True)
    s = Session(ui.root)
    assert s.ready(timeout=5)
    return thread, s


def stop(ui, thread):
    ui.close()
    thread.join(timeout=10)


def test_modes_and_help(counter):
    ui, s = counter
    assert oct((ui.root / "count.md").stat().st_mode & 0o777) == "0o444"
    assert oct((ui.root / "add.sh").stat().st_mode & 0o777) == "0o755"
    r = subprocess.run([str(ui.root / "add.sh"), "--help"], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and r.stdout == "add 1: ./add.sh 1\n" and ui.n == 0  # help does not press it


def test_user_edits_stay_until_the_content_changes(counter):
    ui, s = counter
    view = ui.root / "count.md"
    view.chmod(0o644)
    view.write_text("my notes\n")
    ui.refresh()
    time.sleep(0.3)
    assert view.read_text() == "my notes\n"  # same content drawn: not rewritten
    s.input("add", "1", timeout=5)
    assert view.read_text() == "1\n"  # the content changed: rewritten
    view.unlink()
    ui.refresh()
    assert wait_for(lambda: view.exists() and view.read_text() == "1\n")  # deleted: written again


class Inventory(Node):
    """A node with its own button: checks what it can, sends the rest up as a 'move' event."""

    def __init__(self, items):
        self.items = items

    def render(self, ctx):
        box = ctx.folder("inventory")
        box.file("items.md", "\n".join(self.items) + "\n")
        box.action("apply")
        box.action("other")

    def action(self, name, args, ctx):
        if name == "inventory/apply":
            if not args:
                return "nothing to move"  # answered here, never reaches the UI
            return ctx.emit("move", *args)
        return PASS


class Shop(FolderUI):
    def __init__(self, path):
        super().__init__(path)
        self.items, self.seen = ["rope"], []

    def draw(self):
        return [Folder("player", [Inventory(self.items)])]

    def action(self, name, args):
        self.seen.append((name, args))
        if name == "player/move":
            self.items.append(args[0])
            return f"moved {args[0]}"
        return f"top got {name}"


def test_events_bubble_from_nodes_to_the_ui(tmp_path):
    ui = Shop(tmp_path / "ui")
    thread, s = start(ui)
    assert s.inputs() == ["player/inventory/apply", "player/inventory/other"]
    assert s.input("player/inventory/apply", timeout=5) == (True, "nothing to move") and ui.seen == []
    assert s.input("player/inventory/apply", "metal", timeout=5) == (True, "moved metal")
    assert ui.seen == [("player/move", ["metal"])]
    assert s.read("player/inventory/items.md") == "rope\nmetal\n"
    assert s.input("player/inventory/other", timeout=5) == (True, "top got player/inventory/other")
    stop(ui, thread)


@dataclasses.dataclass
class Prefs:
    greeting: str = dataclasses.field(default="hello", metadata={"help": "What to say"})
    volume: int = 5

    def check(self, new):
        if not 0 <= new["volume"] <= 10:
            raise ValueError("volume must be 0-10")


class PrefsUI(FolderUI):
    def __init__(self, path):
        super().__init__(path)
        self.prefs = Prefs()

    def draw(self):
        return [Editable("prefs.toml", self.prefs), File("view.md", f"{self.prefs.greeting} x{self.prefs.volume}\n"),
                Action("noop")]

    def action(self, name, args):
        return "ok"


def test_editable_binds_an_object(tmp_path):
    ui = PrefsUI(tmp_path / "ui")
    thread, s = start(ui)
    prefs = ui.root / "prefs.toml"
    assert oct(prefs.stat().st_mode & 0o777) == "0o644"
    text = prefs.read_text()
    assert "# What to say" in text and 'greeting = "hello"' in text and "volume = 5" in text
    prefs.write_text(text.replace("volume = 5", "volume = 7"))
    assert wait_for(lambda: s.read("view.md") == "hello x7\n") and ui.prefs.volume == 7
    prefs.write_text('volume = 11\n')  # check() says no: nothing changes, the user's file stays, the reason is shown
    assert wait_for(lambda: (ui.root / "prefs.toml.error").exists())
    assert "volume must be 0-10" in s.read("prefs.toml.error") and ui.prefs.volume == 7
    assert prefs.read_text() == "volume = 11\n"
    prefs.write_text('volume = "loud"\n')
    assert wait_for(lambda: "should look like 7" in s.read("prefs.toml.error"))
    prefs.write_text('greeting = "hi"\n')  # keys left out keep their values
    assert wait_for(lambda: s.read("view.md") == "hi x7\n")
    assert wait_for(lambda: not (ui.root / "prefs.toml.error").exists())
    stop(ui, thread)


def test_editable_reads_the_users_file_from_before(tmp_path):
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "prefs.toml").write_text("volume = 3\n")
    ui = PrefsUI(tmp_path / "ui")
    thread, s = start(ui)
    assert wait_for(lambda: s.read("view.md") == "hello x3\n")
    stop(ui, thread)
    assert (tmp_path / "ui" / "prefs.toml").exists()  # closing leaves the user's file
