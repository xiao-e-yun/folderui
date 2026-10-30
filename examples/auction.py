"""多人範例：拍賣。1 個伺服器（純遊戲邏輯）＋ N 個玩家客戶端 ＋ 1 個觀眾客戶端。

    uv run python examples/auction.py [--players 4] [--rounds 3] [--seed N] [--dir DIR]

規則：每輪每人從 3 件寶物選 1 件拍賣；賣家寫介紹，買家只看得到名稱和介紹，輪流公開喊價。
"""
import argparse
import random
import tempfile
from dataclasses import dataclass
from pathlib import Path

from folderui import FolderUI, Server

NAMES = ["龍紋古劍", "月光琉璃盞", "青銅羅盤", "鳳羽披風", "沉船金幣", "賢者手稿",
         "翡翠面具", "星象儀", "黑曜石戒指", "古神雕像", "鍊金術士之瓶", "王冠碎片",
         "水晶頭骨", "精靈豎琴", "海妖之淚", "隕鐵匕首", "藏寶圖", "不老藥"]

TRAITS = [("皇室收藏", 30), ("名家之作", 25), ("古老", 20), ("鑲金", 15), ("完好無缺", 10),
          ("稀有", 10), ("普通", 0), ("來路不明", -10), ("有裂痕", -15), ("被詛咒", -30),
          ("贗品", -40)]

RULES = """# 拍賣（共 {rounds} 輪）

1. 每輪開始時每人拿到 3 件新寶物，每件有底價和 2 個詞條（會加減價值），只有你自己看得到。
   選 1 件當你這輪的拍品，其餘兩件丟棄。
2. 每位玩家輪流當賣家，寫一段介紹。**買家只看得到寶物名稱和這段介紹**，可以誇大或隱瞞。
3. 公開喊價：從賣家的下一位開始輪流，輪到你時可以加價或退出這件拍品（退出後不能再加入）。
   第一口至少 1，之後每次至少比目前最高價多 {step}。只剩最高出價者時得標，付錢給賣家。
4. 每場結束後公開寶物的詞條和真實價值，所有紀錄都在 history.md。
5. 起始每人 100 金。總分 = 金錢 + 擁有寶物的真實價值，最高者獲勝。
"""


STEP = 5  # 加價的最小幅度


@dataclass
class Treasure:
    name: str
    base: int
    traits: list[tuple[str, int]]

    @property
    def value(self) -> int:
        return max(0, self.base + sum(v for _, v in self.traits))

    def card(self) -> str:
        lines = [f"**{self.name}**（真實價值 {self.value}）", f"- 底價 {self.base}"]
        return "\n".join(lines + [f"- {t}（{v:+d}）" for t, v in self.traits])


@dataclass
class Update:
    view: dict
    events: list[str]


# ---------------- 伺服器：純遊戲邏輯，不知道資料夾的存在 ----------------

class AuctionServer(Server):
    step = STEP

    def __init__(self, n, rounds=1, seed=None, money=100):
        super().__init__()
        self.rng = random.Random(seed)
        self.n, self.rounds, self.round = n, rounds, 0
        self.names: list[str | None] = [None] * n
        self.money = [money] * n
        self.owned: list[list[Treasure]] = [[] for _ in range(n)]
        self.history: list[str] = []
        self.phase = "join"          # join → (pick → describe ⇄ bid) × rounds → over
        self.seller = 0
        self.sold = 0                # 這輪已經拍完幾場
        self.pitch = ""
        self.high, self.leader = 0, None    # 目前最高價與出價者
        self.active: set[int] = set()        # 還沒退出這件拍品的買家
        self.turn = None                     # 輪到誰喊價
        self.bidlog: list[str] = []
        self.outbox: dict = {}
        self.deal()

    def deal(self):
        self.round += 1
        self.hands = [[Treasure(name, self.rng.randint(20, 60), self.rng.sample(TRAITS, 2))
                       for name in self.rng.sample(NAMES, 3)] for _ in range(self.n)]
        self.lots: list[Treasure | None] = [None] * self.n
        self.seller = (self.round - 1) % self.n   # 每輪換人先賣
        self.sold = 0

    def name(self, p):
        return self.names[p] or f"player_{p}"

    def say(self, text, to=None):
        for who in (self.clients if to is None else to):
            self.outbox.setdefault(who, []).append(text)

    def publish(self):
        for who in self.clients:
            self.push(who, Update(self.view(who), self.outbox.pop(who, [])))

    def handle(self, p, cmd):
        kind, arg = cmd
        result = getattr(self, f"do_{kind}")(p, arg)
        self.publish()
        return result

    def do_join(self, p, name):
        if self.phase != "join" or self.names[p]:
            raise ValueError("現在不能加入")
        if not 1 <= len(name) <= 20 or name in self.names:
            raise ValueError("名字需為 1-20 字，且不能和別人重複")
        self.names[p] = name
        if all(self.names):
            self.phase = "pick"
            self.say("所有玩家已加入：" + "、".join(self.names) + f"\n\n## 第 1/{self.rounds} 輪")
        return f"✅ 歡迎，{name}"

    def do_pick(self, p, tname):
        if self.phase != "pick" or self.lots[p]:
            raise ValueError("現在不能選寶物")
        lot = next((t for t in self.hands[p] if t.name == tname), None)
        if lot is None:
            raise ValueError("選項必須是：" + " / ".join(t.name for t in self.hands[p]))
        self.lots[p] = lot
        if all(self.lots):
            self.phase = "describe"
            self.say(f"每位玩家都選好了拍品，由 {self.name(self.seller)} 先賣。")
        return f"✅ 你選了 {tname}"

    def do_describe(self, p, text):
        if self.phase != "describe" or p != self.seller:
            raise ValueError("現在不是你介紹的時候")
        if not 1 <= len(text) <= 500:
            raise ValueError("介紹需為 1-500 字")
        self.pitch, self.phase = text, "bid"
        self.high, self.leader, self.bidlog = 0, None, []
        self.active = {b for b in range(self.n) if b != p}
        self.turn = (p + 1) % self.n
        self.say(f"🔔 {self.name(p)} 拍賣「{self.lots[p].name}」：\n> {text}\n\n由 {self.name(self.turn)} 先喊價。")
        return "✅ 已送出介紹"

    def min_bid(self):
        return self.high + self.step if self.leader is not None else 1

    def do_bid(self, p, amount):
        if self.phase != "bid" or p != self.turn:
            raise ValueError("現在不是你喊價的時候")
        if amount is None:
            self.active.discard(p)
            self.bidlog.append(f"{self.name(p)} 退出")
        else:
            if not self.min_bid() <= amount <= self.money[p]:
                raise ValueError(f"出價需在 {self.min_bid()}-{self.money[p]} 之間"
                                 if self.min_bid() <= self.money[p] else "你的錢不夠加價，只能退出")
            self.high, self.leader = amount, p
            self.bidlog.append(f"{self.name(p)} {amount}")
        self.say(f"📣 {self.bidlog[-1]}" + (f"（目前最高：{self.name(self.leader)} {self.high}）"
                                            if self.leader is not None else ""))
        rivals = self.active - {self.leader}
        if not rivals:
            self.settle()
        else:
            self.turn = next(c for k in range(1, self.n + 1)
                             if (c := (p + k) % self.n) in rivals)
        return f"✅ 已出價 {amount}" if amount else "✅ 已退出這件拍品"

    def settle(self):
        s, lot = self.seller, self.lots[self.seller]
        self.turn = None
        if self.leader is not None:
            w, price = self.leader, self.high
            self.money[w] -= price
            self.money[s] += price
            self.owned[w].append(lot)
            result = f"🔨 {self.name(w)} 以 {price} 得標"
        else:
            self.owned[s].append(lot)
            result = f"流標，{self.name(s)} 留下寶物"
        self.say(f"{result}。揭曉：\n{lot.card()}")
        self.history.append(f"## 第 {self.round} 輪：{self.name(s)} 賣「{lot.name}」\n\n"
                            f"介紹：\n> {self.pitch}\n\n"
                            + "喊價：" + "、".join(self.bidlog)
                            + f"\n\n{result}。揭曉：\n{lot.card()}\n")
        self.sold += 1
        if self.sold < self.n:
            self.seller, self.phase = (s + 1) % self.n, "describe"
        elif self.round < self.rounds:
            self.deal()
            self.phase = "pick"
            self.say(f"## 第 {self.round}/{self.rounds} 輪\n每人拿到 3 件新寶物，請重新選拍品。")
        else:
            self.phase = "over"
            self.say(self.final_result())

    def todo(self, p):
        if self.phase == "join" and not self.names[p]:
            return "join"
        if self.phase == "pick" and not self.lots[p]:
            return "pick"
        if self.phase == "describe" and p == self.seller:
            return "describe"
        if self.phase == "bid" and p == self.turn:
            return "bid"
        return None

    def view(self, who) -> dict:
        v = {
            "phase": self.phase,
            "round": self.round,
            "rounds": self.rounds,
            "history": list(self.history),
            "roster": [(self.name(p), self.money[p], len(self.owned[p])) for p in range(self.n)],
            "seller": self.name(self.seller),
            "lot_name": self.lots[self.seller].name if self.phase in ("describe", "bid") else None,
            "pitch": self.pitch if self.phase == "bid" else None,
            "bidding": self.bidding() if self.phase == "bid" else None,
            "min_bid": self.min_bid(),
        }
        if who == "spectator":
            v["lots"] = [f"{self.name(p)}：{t.card()}" for p, t in enumerate(self.lots) if t]
        else:
            v.update(me=who, todo=self.todo(who), money=self.money[who],
                     owned=[t.card() for t in self.owned[who]],
                     hand=[t.card() for t in self.hands[who]] if self.phase in ("join", "pick") else None,
                     options=[t.name for t in self.hands[who]],
                     my_lot=self.lots[who].card() if self.lots[who] else None)
        return v

    def bidding(self):
        lines = ["喊價：" + ("、".join(self.bidlog) or "（還沒有人出價）")]
        if self.leader is not None:
            lines.append(f"目前最高：{self.name(self.leader)} {self.high}")
        lines.append("還在競標：" + "、".join(self.name(b) for b in sorted(self.active)))
        lines.append(f"輪到：{self.name(self.turn)}")
        return "\n".join(lines)

    def final_result(self):
        scores = [self.money[p] + sum(t.value for t in self.owned[p]) for p in range(self.n)]
        lines = ["# 最終結果", "", "| 玩家 | 金錢 | 寶物價值 | 總分 |", "|---|---|---|---|"]
        for p in sorted(range(self.n), key=lambda p: -scores[p]):
            lines.append(f"| {self.name(p)} | {self.money[p]} | {scores[p] - self.money[p]} | {scores[p]} |")
        best = max(scores)
        return "\n".join(lines + ["", "🏆 獲勝：" + "、".join(self.name(p) for p in range(self.n)
                                                          if scores[p] == best)])


# ---------------- 客戶端：每個資料夾一個 FolderUI ----------------

def roster_md(v):
    rows = [f"# 玩家（第 {v['round']}/{v['rounds']} 輪）", "", "| 名字 | 金錢 | 寶物 |", "|---|---|---|"]
    return "\n".join(rows + [f"| {n} | 💰 {m} | 🏺 {k} 件 |" for n, m, k in v["roster"]]) + "\n"


class PlayerUI(FolderUI):
    def __init__(self, path, server, me):
        super().__init__(path)
        self.server, self.me = server, me
        self.view = server.view(me)

    def receive(self, update):
        self.view = update.view
        for e in update.events:
            self.notify(e)
        if self.view["phase"] == "over":
            self.close()

    def draw(self):
        v = self.view
        self.file("rules.md", RULES.format(rounds=v["rounds"], step=STEP))
        self.file("players.md", roster_md(v))
        if v["history"]:  # 只往後加 → 適合當紀錄
            self.file("history.md", "# 拍賣紀錄\n\n" + "\n".join(v["history"]))
        if v["owned"]:
            self.file("owned.md", "# 你擁有的寶物\n\n" + "\n\n".join(v["owned"]) + "\n")
        if v["hand"]:
            self.file("hand.md", "# 你的寶物（只有你看得到）\n\n" + "\n\n".join(v["hand"]) + "\n")
        if v["my_lot"]:
            self.file("my_lot.md", "# 你的拍品\n\n" + v["my_lot"] + "\n")
        if v["lot_name"]:
            self.file("lot.md", f"# 拍品：{v['lot_name']}\n\n賣家：{v['seller']}\n\n"
                      + (f"> {v['pitch']}\n\n{v['bidding']}\n" if v["pitch"] else "（賣家正在寫介紹…）\n"))

        todo = v["todo"]
        if todo == "join":
            self.button("join", "<名字>")
            self.status("請輸入名字加入遊戲（規則見 rules.md）。")
        elif todo == "pick":
            self.button("pick", "<寶物名稱>，詳見 hand.md", options=v["options"])
            self.status(f"第 {v['round']}/{v['rounds']} 輪：選一件寶物拿去拍賣，其餘兩件丟棄。")
        elif todo == "describe":
            self.button("describe", "<介紹文字>（買家只看得到名稱和這段介紹）")
            self.status(f"輪到你當賣家：\n{v['my_lot']}")
        elif todo == "bid":
            if v["min_bid"] <= v["money"]:
                self.button("bid", f"<金額 {v['min_bid']}-{v['money']}>  公開喊價")
            self.button("pass", "退出這件拍品")
            self.status(f"輪到你喊價。{v['seller']} 正在拍賣「{v['lot_name']}」：\n> {v['pitch']}\n\n"
                       f"{v['bidding']}\n\n你有 💰 {v['money']}。")

    def action(self, name, args):
        text = " ".join(args).strip()
        if name == "pass":
            return self.server.call(self.me, ("bid", None))
        if not text:
            raise ValueError(f"用法：./{name}.sh <內容>")
        if name == "bid":
            if not text.isdigit():
                raise ValueError("出價必須是正整數")
            return self.server.call(self.me, ("bid", int(text)))
        return self.server.call(self.me, (name, text))


class SpectatorUI(FolderUI):
    """觀眾：看得到所有拍品的真相，沒有按鈕。紀錄寫成只往後加的 log.md。"""

    def __init__(self, path, server):
        super().__init__(path)
        self.view = server.view("spectator")
        self.log: list[str] = []

    def receive(self, update):
        self.view = update.view
        self.log += update.events
        if self.view["phase"] == "over":
            self.close()

    def draw(self):
        v = self.view
        self.file("players.md", roster_md(v))
        if v["lots"]:
            self.file("truth.md", "# 所有拍品的真相\n\n" + "\n\n".join(v["lots"]) + "\n")
        if v["lot_name"]:
            self.file("lot.md", f"# 拍品：{v['lot_name']}\n\n> {v['pitch'] or '（賣家正在寫介紹…）'}\n\n"
                      + (v["bidding"] or "") + "\n")
        if self.log:
            self.file("log.md", "".join(e + "\n\n" for e in self.log))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--dir")
    args = ap.parse_args()
    if not 2 <= args.players <= 6:
        raise SystemExit("玩家數需為 2-6")
    root = Path(args.dir or tempfile.mkdtemp(prefix="folderui-auction-"))

    server = AuctionServer(args.players, args.rounds, args.seed)
    clients = {p: PlayerUI(root / f"player_{p}", server, p) for p in range(args.players)}
    clients["spectator"] = SpectatorUI(root / "spectator", server)
    for who, ui in clients.items():
        server.connect(who, ui)

    server.start()
    threads = [ui.start() for ui in clients.values()]
    for who, ui in clients.items():
        print(f"{who}: {ui.root}", flush=True)
    for t in threads:
        t.join()
    server.stop()
    print(server.final_result())


if __name__ == "__main__":
    main()
