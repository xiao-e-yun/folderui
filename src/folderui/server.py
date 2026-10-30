"""多人用的伺服器：一個執行緒依序處理所有客戶端的請求，並推播給客戶端。

    客戶端 ──call(who, cmd)──▶ Server.handle()     請求／回應（同步）
    客戶端 ◀──push(who, msg)── Server              推播（非同步）

handle() raise 的例外會傳回呼叫端（ValueError 代表規則不允許）。
推播走客戶端的無上限佇列，伺服器永遠不會等待客戶端，所以不會死鎖。
"""
import queue
import threading
import traceback


class Server:
    def __init__(self):
        self.clients: dict = {}
        self._requests: queue.Queue = queue.Queue()
        self._stopped = False

    # ---- 子類別實作 ----

    def handle(self, who, cmd):
        raise NotImplementedError

    def on_start(self) -> None:
        pass

    # ---- API ----

    def connect(self, who, client) -> None:
        """註冊一個可以接收推播的客戶端（任何有 push(msg) 的物件）。"""
        self.clients[who] = client

    def call(self, who, cmd, timeout: float = 30):
        box: queue.Queue = queue.Queue(maxsize=1)
        self._requests.put((who, cmd, box))
        try:
            ok, value = box.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("伺服器沒有回應") from None
        if ok:
            return value
        raise value

    def push(self, who, msg) -> None:
        self.clients[who].push(msg)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="folderui:server", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        self.on_start()
        while not self._stopped:
            try:
                who, cmd, box = self._requests.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                box.put((True, self.handle(who, cmd)))
            except Exception as e:
                if not isinstance(e, ValueError):
                    traceback.print_exc()
                box.put((False, e))
