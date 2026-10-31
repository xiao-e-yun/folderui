"""Multiplayer example: an auction. One shared game object (pure game logic, guarded by a lock)
plus N player UIs and 1 spectator UI.

    uv run python examples/auction.py [--players 4] [--rounds 3] [--seed N] [--dir DIR]

Rules: each round every player picks 1 of 3 treasures to sell; the seller writes a pitch, buyers see
only the name and the pitch, then bid in turns in an open (English) auction.
"""
import argparse
import random
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from folderui import FolderUI

NAMES = ["Dragon Sword", "Moonlight Chalice", "Bronze Compass", "Phoenix Cloak", "Shipwreck Coin",
         "Sage's Manuscript", "Jade Mask", "Astrolabe", "Obsidian Ring", "Idol of the Old God",
         "Alchemist's Flask", "Crown Shard", "Crystal Skull", "Elven Harp", "Siren's Tear",
         "Meteorite Dagger", "Treasure Map", "Elixir of Youth"]

TRAITS = [("royal collection", 30), ("masterwork", 25), ("ancient", 20), ("gilded", 15), ("flawless", 10),
          ("rare", 10), ("ordinary", 0), ("dubious origin", -10), ("cracked", -15), ("cursed", -30),
          ("forgery", -40)]

STEP = 5  # minimum raise

RULES = """# Auction ({rounds} rounds)

1. At the start of each round every player gets 3 new treasures. Each has a base price and 2 traits
   that add to or subtract from its value; only you can see them. Pick 1 as this round's lot;
   the other two are discarded.
2. Players take turns as the seller and write a pitch. **Buyers see only the treasure's name and the
   pitch**, which may exaggerate or leave things out.
3. Open bidding: starting with the player after the seller, players take turns. On your turn you may
   raise or drop out of this lot (you cannot rejoin). The first bid is at least 1; each raise must be at
   least {step} above the current high bid. When only the high bidder is left, they win and pay the seller.
4. After each lot, its traits and true value are revealed. The full record is in history.md.
5. Everyone starts with 100 gold. Score = gold + true value of the treasures you own. Highest score wins.

## Commands (only the ones available right now exist)

- `./join.sh <name>`: join the game
- `./pick.sh <treasure name>`: choose this round's lot (see hand.md)
- `./describe.sh <pitch>`: write the pitch when you are the seller; buyers see only the name and this text
- `./bid.sh <amount>`: bid on your turn
- `./pass.sh`: drop out of this lot
"""


@dataclass
class Treasure:
    name: str
    base: int
    traits: list[tuple[str, int]]

    @property
    def value(self) -> int:
        return max(0, self.base + sum(v for _, v in self.traits))

    def card(self) -> str:
        lines = [f"**{self.name}** (true value {self.value})", f"- base price {self.base}"]
        return "\n".join(lines + [f"- {t} ({v:+d})" for t, v in self.traits])


# ---------------- Game: pure logic, knows nothing about folders ----------------

class Auction:
    step = STEP

    def __init__(self, n, rounds=1, seed=None, money=100):
        self.lock = threading.Lock()  # every read and write happens under this lock
        self.uis: list[FolderUI] = []  # UIs to refresh when the state changes
        self.events: list[str] = []    # messages everyone can see, append-only
        self.rng = random.Random(seed)
        self.n, self.rounds, self.round = n, rounds, 0
        self.names: list[str | None] = [None] * n
        self.money = [money] * n
        self.owned: list[list[Treasure]] = [[] for _ in range(n)]
        self.history: list[str] = []
        self.phase = "join"          # join -> (pick -> describe <-> bid) x rounds -> over
        self.seller = 0
        self.sold = 0                # lots finished this round
        self.pitch = ""
        self.high, self.leader = 0, None    # current high bid and bidder
        self.active: set[int] = set()        # buyers who have not dropped out of this lot
        self.turn = None                     # whose turn it is to bid
        self.bidlog: list[str] = []
        self.deal()

    def deal(self):
        self.round += 1
        self.hands = [[Treasure(name, self.rng.randint(20, 60), self.rng.sample(TRAITS, 2))
                       for name in self.rng.sample(NAMES, 3)] for _ in range(self.n)]
        self.lots: list[Treasure | None] = [None] * self.n
        self.seller = (self.round - 1) % self.n   # a different player sells first each round
        self.sold = 0

    def name(self, p):
        return self.names[p] or f"player_{p}"

    def say(self, text):
        self.events.append(text)

    def act(self, p, kind, arg):
        """Player p's move: raise ValueError if it breaks the rules; on success, refresh every UI."""
        with self.lock:
            result = getattr(self, f"do_{kind}")(p, arg)
        for ui in self.uis:
            ui.refresh()
        return result

    def do_join(self, p, name):
        if self.phase != "join" or self.names[p]:
            raise ValueError("you cannot join now")
        if not 1 <= len(name) <= 20 or name in self.names:
            raise ValueError("the name must be 1-20 characters and not already taken")
        self.names[p] = name
        if all(self.names):
            self.phase = "pick"
            self.say("All players have joined: " + ", ".join(self.names) + f"\n\n## Round 1/{self.rounds}")
        return f"✅ Welcome, {name}"

    def do_pick(self, p, tname):
        if self.phase != "pick" or self.lots[p]:
            raise ValueError("you cannot pick a treasure now")
        lot = next((t for t in self.hands[p] if t.name == tname), None)
        if lot is None:
            raise ValueError("choose one of: " + " / ".join(t.name for t in self.hands[p]))
        self.lots[p] = lot
        if all(self.lots):
            self.phase = "describe"
            self.say(f"Everyone has picked a lot. {self.name(self.seller)} sells first.")
        return f"✅ You picked {tname}"

    def do_describe(self, p, text):
        if self.phase != "describe" or p != self.seller:
            raise ValueError("it is not your turn to pitch")
        if not 1 <= len(text) <= 500:
            raise ValueError("the pitch must be 1-500 characters")
        self.pitch, self.phase = text, "bid"
        self.high, self.leader, self.bidlog = 0, None, []
        self.active = {b for b in range(self.n) if b != p}
        self.turn = (p + 1) % self.n
        self.say(f"🔔 {self.name(p)} is selling \"{self.lots[p].name}\":\n> {text}\n\n"
                 f"{self.name(self.turn)} bids first.")
        return "✅ Pitch submitted"

    def min_bid(self):
        return self.high + self.step if self.leader is not None else 1

    def do_bid(self, p, amount):
        if self.phase != "bid" or p != self.turn:
            raise ValueError("it is not your turn to bid")
        if amount is None:
            self.active.discard(p)
            self.bidlog.append(f"{self.name(p)} drops out")
        else:
            if not self.min_bid() <= amount <= self.money[p]:
                raise ValueError(f"the bid must be between {self.min_bid()} and {self.money[p]}"
                                 if self.min_bid() <= self.money[p]
                                 else "you cannot afford to raise; you can only pass")
            self.high, self.leader = amount, p
            self.bidlog.append(f"{self.name(p)} {amount}")
        self.say(f"📣 {self.bidlog[-1]}" + (f" (high bid: {self.name(self.leader)} {self.high})"
                                            if self.leader is not None else ""))
        rivals = self.active - {self.leader}
        if not rivals:
            self.settle()
        else:
            self.turn = next(c for k in range(1, self.n + 1)
                             if (c := (p + k) % self.n) in rivals)
        return f"✅ You bid {amount}" if amount else "✅ You dropped out of this lot"

    def settle(self):
        s, lot = self.seller, self.lots[self.seller]
        self.turn = None
        if self.leader is not None:
            w, price = self.leader, self.high
            self.money[w] -= price
            self.money[s] += price
            self.owned[w].append(lot)
            result = f"🔨 {self.name(w)} wins at {price}"
        else:
            self.owned[s].append(lot)
            result = f"No bids; {self.name(s)} keeps the treasure"
        self.say(f"{result}. Revealed:\n{lot.card()}")
        self.history.append(f"## Round {self.round}: {self.name(s)} sells \"{lot.name}\"\n\n"
                            f"Pitch:\n> {self.pitch}\n\n"
                            + "Bids: " + ", ".join(self.bidlog)
                            + f"\n\n{result}. Revealed:\n{lot.card()}\n")
        self.sold += 1
        if self.sold < self.n:
            self.seller, self.phase = (s + 1) % self.n, "describe"
        elif self.round < self.rounds:
            self.deal()
            self.phase = "pick"
            self.say(f"## Round {self.round}/{self.rounds}\nEveryone gets 3 new treasures; pick your lot again.")
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
            v["lots"] = [f"{self.name(p)}: {t.card()}" for p, t in enumerate(self.lots) if t]
        else:
            v.update(me=who, todo=self.todo(who), money=self.money[who],
                     owned=[t.card() for t in self.owned[who]],
                     hand=[t.card() for t in self.hands[who]] if self.phase in ("join", "pick") else None,
                     options=[t.name for t in self.hands[who]],
                     my_lot=self.lots[who].card() if self.lots[who] else None)
        return v

    def bidding(self):
        lines = ["Bids: " + (", ".join(self.bidlog) or "(none yet)")]
        if self.leader is not None:
            lines.append(f"High bid: {self.name(self.leader)} {self.high}")
        lines.append("Still in: " + ", ".join(self.name(b) for b in sorted(self.active)))
        lines.append(f"Turn: {self.name(self.turn)}")
        return "\n".join(lines)

    def final_result(self):
        scores = [self.money[p] + sum(t.value for t in self.owned[p]) for p in range(self.n)]
        lines = ["# Final result", "", "| Player | Gold | Treasure value | Score |", "|---|---|---|---|"]
        for p in sorted(range(self.n), key=lambda p: -scores[p]):
            lines.append(f"| {self.name(p)} | {self.money[p]} | {scores[p] - self.money[p]} | {scores[p]} |")
        best = max(scores)
        return "\n".join(lines + ["", "🏆 Winner: " + ", ".join(self.name(p) for p in range(self.n)
                                                              if scores[p] == best)])


# ---------------- UIs: one FolderUI per folder ----------------

def roster_md(v):
    rows = [f"# Players (round {v['round']}/{v['rounds']})", "", "| Name | Gold | Treasures |", "|---|---|---|"]
    return "\n".join(rows + [f"| {n} | 💰 {m} | 🏺 {k} |" for n, m, k in v["roster"]]) + "\n"


class PlayerUI(FolderUI):
    def __init__(self, path, game, me):
        super().__init__(path)
        self.game, self.me = game, me
        self.seen = 0  # how many events this player has already been shown in a response

    def draw(self):
        with self.game.lock:
            v = self.game.view(self.me)
        if v["phase"] == "over":
            self.close()
        self.file("rules.md", RULES.format(rounds=v["rounds"], step=STEP))
        self.file("players.md", roster_md(v))
        if v["history"]:  # append-only, so it works well as a log
            self.file("history.md", "# Auction history\n\n" + "\n".join(v["history"]))
        if v["owned"]:
            self.file("owned.md", "# Treasures you own\n\n" + "\n\n".join(v["owned"]) + "\n")
        if v["hand"]:
            self.file("hand.md", "# Your treasures (only you can see these)\n\n" + "\n\n".join(v["hand"]) + "\n")
        if v["my_lot"]:
            self.file("my_lot.md", "# Your lot\n\n" + v["my_lot"] + "\n")
        if v["lot_name"]:
            self.file("lot.md", f"# Lot: {v['lot_name']}\n\nSeller: {v['seller']}\n\n"
                      + (f"> {v['pitch']}\n\n{v['bidding']}\n" if v["pitch"] else "(the seller is writing a pitch...)\n"))

        todo = v["todo"]
        if todo in ("join", "pick", "describe"):
            self.input(todo)
        elif todo == "bid":
            if v["min_bid"] <= v["money"]:
                self.input("bid")
            self.input("pass")

    def display(self, result):
        with self.game.lock:
            v = self.game.view(self.me)
            new, self.seen = self.game.events[self.seen:], len(self.game.events)
        todo = v["todo"]
        if todo == "join":
            now = "👉 Join the game with ./join.sh <name> (rules in rules.md)."
        elif todo == "pick":
            now = (f"👉 Round {v['round']}/{v['rounds']}: pick a lot with ./pick.sh <treasure name> (see hand.md). "
                   "Options: " + " / ".join(v["options"]))
        elif todo == "describe":
            now = f"👉 You are the seller. Pitch your lot with ./describe.sh <pitch>:\n{v['my_lot']}"
        elif todo == "bid":
            how = (f"./bid.sh <amount> (at least {v['min_bid']}) or ./pass.sh" if "bid" in self.inputs
                   else "you cannot afford to raise; only ./pass.sh")
            now = (f"{v['seller']} is selling \"{v['lot_name']}\":\n> {v['pitch']}\n\n"
                   f"{v['bidding']}\n\n👉 Your turn to bid (you have 💰 {v['money']}): {how}")
        elif self.closed:
            now = "── Game over ──"
        else:
            now = ""
        return "\n\n".join(x for x in [result, *new, now] if x)

    def action(self, name, args):
        try:
            return self.move(name, " ".join(args).strip())
        except ValueError as e:
            raise ValueError(f"❌ {e}") from None

    def move(self, name, text):
        if name == "pass":
            return self.game.act(self.me, "bid", None)
        if not text:
            raise ValueError(f"usage: ./{name}.sh <text>")
        if name == "bid":
            if not text.isdigit():
                raise ValueError("the bid must be a positive integer")
            return self.game.act(self.me, "bid", int(text))
        return self.game.act(self.me, name, text)


class SpectatorUI(FolderUI):
    """Spectator: sees the truth about every lot and has no buttons. The log is an append-only log.md."""

    def __init__(self, path, game):
        super().__init__(path)
        self.game = game

    def draw(self):
        with self.game.lock:
            v = self.game.view("spectator")
            events = list(self.game.events)
        if v["phase"] == "over":
            self.close()
        self.file("players.md", roster_md(v))
        if v["lots"]:
            self.file("truth.md", "# The truth about every lot\n\n" + "\n\n".join(v["lots"]) + "\n")
        if v["lot_name"]:
            self.file("lot.md", f"# Lot: {v['lot_name']}\n\n> {v['pitch'] or '(the seller is writing a pitch...)'}\n\n"
                      + (v["bidding"] or "") + "\n")
        if events:
            self.file("log.md", "".join(e + "\n\n" for e in events))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--dir")
    args = ap.parse_args()
    if not 2 <= args.players <= 6:
        raise SystemExit("players must be between 2 and 6")
    root = Path(args.dir or tempfile.mkdtemp(prefix="folderui-auction-"))

    game = Auction(args.players, args.rounds, args.seed)
    uis = {p: PlayerUI(root / f"player_{p}", game, p) for p in range(args.players)}
    uis["spectator"] = SpectatorUI(root / "spectator", game)
    game.uis = list(uis.values())

    threads = [ui.run(background=True) for ui in uis.values()]
    for who, ui in uis.items():
        print(f"{who}: {ui.root}", flush=True)
    for t in threads:
        t.join()
    print(game.final_result())


if __name__ == "__main__":
    main()
