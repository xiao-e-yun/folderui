import os
import subprocess
import threading
import time

import pytest

from folderui import FolderUI, Session


class Counter(FolderUI):
    """單人：add 會加一，到 3 就結束。"""

    def __init__(self, path):
        super().__init__(path)
        self.n = 0

    def draw(self):
        self.file("count.md", f"{self.n}\n")
        self.input("add")

    def action(self, name, args):
        if args != ["1"]:
            raise ValueError("只能加 1")
        self.n += 1
        if self.n == 3:
            self.close()
        return f"加到 {self.n}"


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
    assert not ok and "只能加 1" in text
    ok, text = s.input("add", "1", timeout=5)
    assert ok and text == "加到 1"
    assert s.read("count.md") == "1\n"


def test_close_removes_buttons(counter):
    ui, s = counter
    for _ in range(3):
        ok, text = s.input("add", "1", timeout=5)
    assert ok and text == "加到 3"
    assert s.inputs() == []
    for _ in range(50):  # 回應被讀走後，下一幀才會移除保留的腳本
        if not (ui.root / "add.sh").exists():
            break
        time.sleep(0.1)
    assert not (ui.root / "add.sh").exists()


def test_shell_button(counter):
    ui, s = counter
    r = subprocess.run([str(ui.root / "add.sh"), "1"], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and "加到 1" in r.stdout
    r = subprocess.run([str(ui.root / "add.sh"), "x"], capture_output=True, text=True, timeout=10)
    assert r.returncode == 1 and "只能加 1" in r.stdout


class Lobby:
    """兩個人都按 ready 才一起開始：共用狀態 + 一把鎖 + refresh()。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.ready: set = set()
        self.events: list[str] = []
        self.uis: list[FolderUI] = []

    def press_ready(self, who):
        with self.lock:
            self.ready.add(who)
            if len(self.ready) == len(self.uis):
                self.events.append("🔔 開始了")
        for ui in self.uis:
            ui.refresh()
        return "✅ 準備好了"


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
        # 已準備但還沒開始 → 沒有按鈕 → 腳本繼續等

    def display(self, result):  # 每次回應呼叫一次 → 只附上還沒看過的訊息
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
    assert t.is_alive()  # 另一個人還沒準備好，p0 的按鈕卡住

    ok, text = s1.input("ready", timeout=10)
    t.join(timeout=10)
    assert ok and "開始了" in text and s1.inputs() == ["play"]
    ok, text = result["r"]
    assert ok and "開始了" in text

    for s in (s0, s1):
        ok, text = s.input("play", timeout=10)
        assert ok and text == "played"  # 已經看過的訊息不會重複
    for th in threads:
        th.join(timeout=10)
        assert not th.is_alive()


def test_interrupted_and_resume(tmp_path):
    lobby, threads, (s0, s1) = make_lobby(tmp_path)
    script = str(lobby.uis[0].root / "ready.sh")

    with pytest.raises(subprocess.TimeoutExpired):  # 模擬 agent 的指令逾時被砍掉，但請求已送出
        subprocess.run([script], capture_output=True, text=True, timeout=1)

    r = subprocess.run([script, "other"], capture_output=True, text=True, timeout=10)
    assert r.returncode == 1 and "還在等待" in r.stdout  # 不同的指令會被拒絕

    s1.input("ready", timeout=10)
    r = subprocess.run([script], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and "開始了" in r.stdout  # 同樣的指令 → 拿到回應
    for ui in lobby.uis:
        ui.close()


def test_stale_input_is_rejected_without_calling_action(counter):
    ui, s = counter
    calls = []
    ui.action = lambda name, args: calls.append(name)
    ok, text = s.input("gone", timeout=5)  # 畫面上沒有這個按鈕（例如舊畫面的腳本）
    assert not ok and "現在不能用" in text and calls == []
