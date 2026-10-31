import os
import subprocess
import threading
import time

import pytest

from folderui import FolderUI, Session


class Counter(FolderUI):
    """Single player: add increments by one and closes at 3."""

    def __init__(self, path):
        super().__init__(path)
        self.n = 0

    def draw(self):
        self.file("count.md", f"{self.n}\n")
        self.input("add")

    def action(self, name, args):
        if args != ["1"]:
            raise ValueError("can only add 1")
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
    assert not ok and "can only add 1" in text
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
    assert r.returncode == 1 and "can only add 1" in r.stdout


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
            self.input("ready")
        elif started:
            self.input("play")
        # ready but not started -> no buttons -> the script keeps waiting

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
    assert not ok and "unavailable" in text and calls == []
