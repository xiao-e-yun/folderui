import os
import subprocess
import threading
import time

import pytest

from folderui import FolderUI, Server, Session


class Counter(FolderUI):
    """單人：add 會加一，到 3 就結束。"""

    def __init__(self, path):
        super().__init__(path)
        self.n = 0

    def draw(self):
        self.file("count.md", f"{self.n}\n")
        self.button("add", "<1-3>")
        self.reply(f"目前 {self.n}")

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
    thread = ui.start()
    s = Session(ui.root)
    assert s.ready(timeout=5)
    yield ui, s
    ui.close()
    thread.join(timeout=5)


def test_session_press(counter):
    ui, s = counter
    assert set(s.buttons()) == {"add"}
    ok, text = s.press("add", "2", timeout=5)
    assert not ok and "只能加 1" in text
    ok, text = s.press("add", "1", timeout=5)
    assert ok and text.startswith("加到 1") and "./add.sh" in text
    assert s.read("count.md") == "1\n"


def test_close_removes_buttons(counter):
    ui, s = counter
    for _ in range(3):
        ok, text = s.press("add", "1", timeout=5)
    assert ok and "已結束" in text
    assert s.buttons() == {}
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


class ReadyServer(Server):
    """兩個人都按 ready 才一起開始。"""

    def __init__(self):
        super().__init__()
        self.ready: set = set()

    def handle(self, who, cmd):
        self.ready.add(who)
        if len(self.ready) == len(self.clients):
            for p in self.clients:
                self.push(p, "go")
        return "✅ 準備好了"


class Lobby(FolderUI):
    def __init__(self, path, server, me):
        super().__init__(path)
        self.server, self.me, self.state = server, me, "lobby"

    def draw(self):
        if self.state == "lobby":
            self.button("ready")
        elif self.state == "waiting":
            pass  # 沒有按鈕 → 腳本繼續等
        else:
            self.button("play")

    def action(self, name, args):
        if name == "ready":
            self.state = "waiting"
            return self.server.call(self.me, "ready")
        self.close()
        return "played"

    def receive(self, msg):
        self.state = "started"
        self.notify("🔔 開始了")


def test_multiplayer_blocks_until_everyone_ready(tmp_path):
    server = ReadyServer()
    uis = {p: Lobby(tmp_path / f"p{p}", server, p) for p in range(2)}
    for p, ui in uis.items():
        server.connect(p, ui)
    server.start()
    threads = [ui.start() for ui in uis.values()]
    s0, s1 = Session(uis[0].root), Session(uis[1].root)
    assert s0.ready(timeout=5) and s1.ready(timeout=5)

    result = {}
    t = threading.Thread(target=lambda: result.update(r=s0.press("ready", timeout=10)))
    t.start()
    t.join(timeout=0.5)
    assert t.is_alive()  # 另一個人還沒準備好，p0 的按鈕卡住

    ok, text = s1.press("ready", timeout=10)
    t.join(timeout=10)
    assert ok and "開始了" in text and "./play.sh" in text
    ok, text = result["r"]
    assert ok and "開始了" in text

    for s in (s0, s1):
        s.press("play", timeout=10)
    for th in threads:
        th.join(timeout=5)
    server.stop()


def test_timeout_and_resume(tmp_path):
    server = ReadyServer()
    uis = {p: Lobby(tmp_path / f"p{p}", server, p) for p in range(2)}
    for p, ui in uis.items():
        server.connect(p, ui)
    server.start()
    for ui in uis.values():
        ui.start()
    assert Session(uis[0].root).ready(timeout=5)
    script = str(uis[0].root / "ready.sh")
    env = {**os.environ, "FOLDERUI_TIMEOUT": "1"}

    r = subprocess.run([script], capture_output=True, text=True, timeout=10, env=env)
    assert r.returncode == 2  # 逾時，但請求已送出

    r = subprocess.run([script, "other"], capture_output=True, text=True, timeout=10, env=env)
    assert r.returncode == 1 and "還在等待" in r.stdout  # 不同的指令會被拒絕

    Session(uis[1].root).press("ready", timeout=10)
    r = subprocess.run([script], capture_output=True, text=True, timeout=10, env=env)
    assert r.returncode == 0 and "開始了" in r.stdout  # 同樣的指令 → 拿到回應
    for ui in uis.values():
        ui.close()
    server.stop()
