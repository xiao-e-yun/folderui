"""Multiplayer example: Frontier, a simultaneous-turn (WeGo) territory game with fog of war.

    uv run python examples/frontier.py [--players 3] [--size N] [--turns 12] [--seed N] [--dir DIR]

Everyone gives orders to their units at the same time; when every player has pressed end_turn, the turn
resolves at once. Each unit has its own buttons (units/<id>/move.sh), so the screen changes a lot every turn.
"""
import argparse
import math
import random
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from folderui import FolderUI

LETTERS = "ABCDEFGH"
RECRUIT_COST = 2  # gold per point of strength
DIRS = {"N": (0, -1), "S": (0, 1), "E": (1, 0), "W": (-1, 0)}


def default_size(n):
    return {2: 10, 3: 10, 4: 12, 5: 14, 6: 14}.get(n, 16)

RULES = """# Frontier ({turns} turns)

You are **{letter}** on the map. Everyone orders at the same time; a turn resolves when every player has
pressed `./end_turn.sh`.

## The map (map.md)

- `.` plains, `f` forest, `^` mountain (impassable), `?` not visible
- `T` neutral town, `a`-`{last_lower}` town owned by player A-{last}, `A`-`{last}` a unit of that player (strength in units.md)
- Coordinates are `x,y`: x grows to the east, y grows to the south.
- Fog of war: you see only cells within 2 steps of your own units and towns.

## Orders (you can change them until you end the turn)

- `./units/<id>/move.sh <N|S|E|W>`: move one step
- `./units/<id>/hold.sh`: stay (the default). Units holding in a forest or on a town defend at +1.
- `./towns/<x>_<y>/recruit.sh <n>`: at an owned town, raise a unit of strength n for {cost} gold per point
- `./say.sh <message>`: a public message to every player (negotiate, threaten, lie: it is up to you)
- `./end_turn.sh`: lock in your orders and wait for the others

## Resolution

1. Enemy units that try to swap cells fight on the border first.
2. Units that end on the same cell fight: each side adds up its strength (+1 for a holding defender in a
   forest or town). The strongest side survives with its strength minus the runner-up's; a tie destroys both.
   Your own units on the same cell merge.
3. A town belongs to whoever has a unit on it. Then recruits appear, and every town pays 1 gold (a capital pays 2).
4. A player with no units and no towns is out.

The game ends after {turns} turns or when one player is left. Score = 3 per town + total unit strength.
"""


@dataclass
class Unit:
    id: int
    owner: int
    x: int
    y: int
    strength: int


# ---------------- Game: pure logic, knows nothing about folders ----------------

class Frontier:
    def __init__(self, n, turns=12, seed=None, size=None):
        self.lock = threading.Lock()
        self.uis: list[FolderUI] = []
        self.events: list[tuple[set | None, str]] = []  # (recipients or None for everyone, text), append-only
        self.rng = random.Random(seed)
        self.n, self.turns, self.turn = n, turns, 0
        self.size = size or default_size(n)
        self.names: list[str | None] = [None] * n
        self.gold = [3] * n
        self.alive = set(range(n))
        self.ended: set[int] = set()
        self.phase = "join"  # join -> orders -> over
        self.capitals = self.place_capitals()
        self.towns: dict[tuple, int | None] = {c: p for p, c in self.capitals.items()}
        self.units: dict[int, Unit] = {}
        self.orders: dict[int, str] = {}       # unit id -> direction ("N"...); missing means hold
        self.recruits: dict[tuple, int] = {}   # town -> strength
        self.next_id = 1
        self.terrain = self.make_terrain()
        for p, (x, y) in self.capitals.items():
            self.spawn(p, x, y, 3)
            c = (self.size - 1) / 2
            dx, dy = (1 if x < c else -1, 0) if abs(x - c) >= abs(y - c) else (0, 1 if y < c else -1)
            self.spawn(p, x + dx, y + dy, 2)  # the second unit starts one step toward the center

    def place_capitals(self):
        """Spread the capitals evenly on a ring around the center of the map."""
        c, r = (self.size - 1) / 2, (self.size - 1) / 2 - 1
        start = self.rng.random() * 2 * math.pi
        return {p: (round(c + r * math.cos(start + 2 * math.pi * p / self.n)),
                    round(c + r * math.sin(start + 2 * math.pi * p / self.n))) for p in range(self.n)}

    def make_terrain(self):
        size = self.size
        grid = [["." for _ in range(size)] for _ in range(size)]
        reserved = {(x + dx, y + dy) for x, y in self.capitals.values() for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
        cells = [(x, y) for x in range(size) for y in range(size) if (x, y) not in reserved]
        self.rng.shuffle(cells)
        towns, forests, mountains = 2 * self.n, size * size * 14 // 100, size * size * 8 // 100
        for x, y in cells[:towns]:
            self.towns[(x, y)] = None
        for x, y in cells[towns:towns + forests]:
            grid[y][x] = "f"
        for x, y in cells[towns + forests:towns + forests + mountains]:
            grid[y][x] = "^"
        return grid

    def spawn(self, p, x, y, strength):
        self.units[self.next_id] = Unit(self.next_id, p, x, y, strength)
        self.next_id += 1

    def letter(self, p):
        return LETTERS[p]

    def name(self, p):
        return f"{self.names[p] or f'player_{p}'} ({self.letter(p)})"

    def say(self, text, to=None):
        self.events.append((None if to is None else set(to), text))

    def act(self, p, kind, *args):
        """Player p's order. Returns the text to show; rule violations come back as 'Rejected: ...'."""
        with self.lock:
            try:
                result = getattr(self, f"do_{kind}")(p, *args)
            except ValueError as e:
                result = f"Rejected: {e}"
        for ui in self.uis:
            ui.refresh()
        return result

    # ---- orders ----

    def check_orders(self, p):
        if self.phase != "orders" or p not in self.alive:
            raise ValueError("you cannot give orders now")
        if p in self.ended:
            raise ValueError("you already ended your turn")

    def own_unit(self, p, uid):
        u = self.units.get(uid)
        if u is None or u.owner != p:
            raise ValueError(f"you have no unit #{uid}")
        return u

    def do_join(self, p, name):
        if self.phase != "join" or self.names[p]:
            raise ValueError("you cannot join now")
        if not 1 <= len(name) <= 20 or name in self.names:
            raise ValueError("the name must be 1-20 characters and not already taken")
        self.names[p] = name
        if all(self.names):
            self.phase, self.turn = "orders", 1
            self.say("All players have joined: " + ", ".join(self.name(q) for q in range(self.n)))
        return f"Welcome, {name}. You are {self.letter(p)}."

    def do_move(self, p, uid, d):
        self.check_orders(p)
        u = self.own_unit(p, uid)
        if d not in DIRS:
            raise ValueError("direction must be N, S, E or W")
        x, y = u.x + DIRS[d][0], u.y + DIRS[d][1]
        if not (0 <= x < self.size and 0 <= y < self.size) or self.terrain[y][x] == "^":
            raise ValueError(f"unit #{uid} cannot move {d} into {x},{y}")
        self.orders[uid] = d
        return f"Unit #{uid} will move {d} to {x},{y}."

    def do_hold(self, p, uid):
        self.check_orders(p)
        self.own_unit(p, uid)
        self.orders.pop(uid, None)
        return f"Unit #{uid} will hold."

    def do_recruit(self, p, town, n):
        self.check_orders(p)
        if self.towns.get(town) != p:
            raise ValueError("you do not own that town")
        if not 0 <= n <= 5:
            raise ValueError("strength must be 0-5 (0 cancels)")
        planned = sum(s for t, s in self.recruits.items() if t != town and self.towns.get(t) == p)
        if (planned + n) * RECRUIT_COST > self.gold[p]:
            raise ValueError(f"not enough gold: you have {self.gold[p]}, recruits cost {RECRUIT_COST} per point")
        if n:
            self.recruits[town] = n
        else:
            self.recruits.pop(town, None)
        return f"Town {town[0]},{town[1]} will recruit strength {n}." if n else "Recruit cancelled."

    def do_say(self, p, text):
        if self.phase != "orders" or p not in self.alive:
            raise ValueError("you cannot talk now")
        if not 1 <= len(text) <= 300:
            raise ValueError("the message must be 1-300 characters")
        self.say(f"[turn {self.turn}] {self.name(p)} says: {text}")
        return "Message sent."

    def do_end_turn(self, p):
        self.check_orders(p)
        self.ended.add(p)
        if self.ended >= self.alive:
            self.resolve()
        return "Orders locked in."

    # ---- resolution ----

    def resolve(self):
        reports: dict[int, list[str]] = {p: [] for p in range(self.n)}
        everything: list[str] = []  # each line once, for the spectator

        def report(cell, text, involved):
            everything.append(text)
            for p in self.alive:
                if p in involved or cell in self.visible(p):
                    reports[p].append(text)

        def bonus(u):
            holding = u.id not in self.orders
            return 1 if holding and (self.terrain[u.y][u.x] == "f" or (u.x, u.y) in self.towns) else 0

        target = {}
        for u in self.units.values():
            d = self.orders.get(u.id)
            target[u.id] = (u.x + DIRS[d][0], u.y + DIRS[d][1]) if d else (u.x, u.y)

        # 1. enemies swapping cells fight on the border
        for a in list(self.units.values()):
            for b in list(self.units.values()):
                if (a.id < b.id and a.owner != b.owner and a.id in self.units and b.id in self.units
                        and target[a.id] == (b.x, b.y) and target[b.id] == (a.x, a.y)):
                    text = (f"Border clash between {a.x},{a.y} and {b.x},{b.y}: "
                            f"{self.name(a.owner)} {a.strength} vs {self.name(b.owner)} {b.strength}")
                    winner, loser = (a, b) if a.strength >= b.strength else (b, a)
                    winner.strength -= loser.strength
                    del self.units[loser.id]
                    if winner.strength <= 0:
                        del self.units[winner.id]
                        report((a.x, a.y), text + ": both destroyed.", {a.owner, b.owner})
                    else:
                        report((a.x, a.y), text + f": {self.name(winner.owner)} wins with {winner.strength} left.",
                               {a.owner, b.owner})

        # 2. move, then fight or merge on each cell
        cells: dict[tuple, list[Unit]] = {}
        for u in self.units.values():
            cells.setdefault(target[u.id], []).append(u)
        for cell, here in cells.items():
            sides: dict[int, list[Unit]] = {}
            for u in here:
                sides.setdefault(u.owner, []).append(u)
            power = {p: sum(u.strength + bonus(u) for u in us) for p, us in sides.items()}
            raw = {p: sum(u.strength for u in us) for p, us in sides.items()}
            for u in here:
                del self.units[u.id]
            ranked = sorted(power, key=lambda p: -power[p])
            best = ranked[0]
            left = raw[best] if len(ranked) == 1 else min(raw[best], power[best] - power[ranked[1]])
            if len(ranked) > 1:
                text = (f"Battle at {cell[0]},{cell[1]}: "
                        + ", ".join(f"{self.name(p)} {power[p]}" for p in ranked)
                        + (f": {self.name(best)} wins with {left} left." if left > 0 else ": all destroyed."))
                report(cell, text, set(sides))
            if left > 0:
                keep = min(sides[best], key=lambda u: u.id)
                keep.x, keep.y, keep.strength = cell[0], cell[1], left
                self.units[keep.id] = keep

        # 3. captures, recruits, income
        occupant = {(u.x, u.y): u.owner for u in self.units.values()}
        for town, owner in self.towns.items():
            new = occupant.get(town, owner)
            if new != owner:
                report(town, f"{self.name(new)} captured the town at {town[0]},{town[1]}"
                       + (f" from {self.name(owner)}." if owner is not None else "."),
                       {new} | ({owner} if owner is not None else set()))
                self.towns[town] = new
        for town, n in self.recruits.items():
            p = self.towns.get(town)
            if p is not None and occupant.get(town, p) == p and self.gold[p] >= n * RECRUIT_COST:
                self.gold[p] -= n * RECRUIT_COST
                merged = next((u for u in self.units.values() if (u.x, u.y) == town), None)
                if merged:
                    merged.strength += n
                else:
                    self.spawn(p, town[0], town[1], n)
                reports[p].append(f"Recruited strength {n} at {town[0]},{town[1]}.")
                everything.append(f"{self.name(p)} recruited strength {n} at {town[0]},{town[1]}.")
        for town, p in self.towns.items():
            if p is not None:
                self.gold[p] += 2 if town == self.capitals[p] else 1

        # 4. eliminations and the next turn
        for p in sorted(self.alive):
            if not any(u.owner == p for u in self.units.values()) and p not in self.towns.values():
                self.alive.discard(p)
                self.say(f"{self.name(p)} is out.")
        for p, lines in reports.items():
            if lines:
                self.say(f"Turn {self.turn} report:\n" + "\n".join(f"- {line}" for line in lines), to={p})
        if everything:
            self.say(f"Turn {self.turn} (all reports):\n" + "\n".join(f"- {line}" for line in everything),
                     to={"spectator"})
        self.orders, self.recruits, self.ended = {}, {}, set()
        if self.turn >= self.turns or len(self.alive) <= 1:
            self.phase = "over"
            self.say(self.final_result())
        else:
            self.turn += 1
            self.say(f"== Turn {self.turn}/{self.turns} ==")

    # ---- views ----

    def visible(self, p):
        if p == "spectator":
            return {(x, y) for x in range(self.size) for y in range(self.size)}
        eyes = [(u.x, u.y) for u in self.units.values() if u.owner == p]
        eyes += [t for t, o in self.towns.items() if o == p]
        return {(x, y) for x in range(self.size) for y in range(self.size)
                if any(abs(x - ex) + abs(y - ey) <= 2 for ex, ey in eyes)}

    def map_md(self, p):
        seen = self.visible(p)
        occupant = {(u.x, u.y): u.owner for u in self.units.values()}
        xs = range(self.size)
        rows = ["```"]
        if self.size > 10:  # two header rows: tens, then ones
            rows.append("   " + " ".join(str(x // 10) if x >= 10 else " " for x in xs))
        rows.append("   " + " ".join(str(x % 10) for x in xs))
        for y in range(self.size):
            cells = []
            for x in xs:
                c = (x, y)
                if c not in seen:
                    cells.append("?")
                elif c in occupant:
                    cells.append(self.letter(occupant[c]))
                elif c in self.towns:
                    o = self.towns[c]
                    cells.append("T" if o is None else self.letter(o).lower())
                else:
                    cells.append(self.terrain[y][x])
            rows.append(f"{y:>2} " + " ".join(cells))
        rows.append("```")
        towns = [f"- {x},{y}: " + ("neutral" if o is None else self.name(o)) + (" (capital)" if (x, y) in self.capitals.values() else "")
                 for (x, y), o in sorted(self.towns.items()) if (x, y) in seen]
        who = "Spectator view" if p == "spectator" else f"You are {self.name(p)}"
        return f"# Map (turn {self.turn}/{self.turns})\n\n{who}\n\n" + "\n".join(rows) + "\n\n## Visible towns\n\n" + "\n".join(towns) + "\n"

    def units_md(self, p):
        seen = self.visible(p)
        lines = [f"# Units (turn {self.turn}/{self.turns})", ""]
        if p != "spectator":
            lines += [f"Gold: {self.gold[p]}", "", "## Yours", "", "| Unit | At | Strength | Order |", "|---|---|---|---|"]
            for u in sorted(self.units.values(), key=lambda u: u.id):
                if u.owner == p:
                    lines.append(f"| #{u.id} | {u.x},{u.y} | {u.strength} | {('move ' + self.orders[u.id]) if u.id in self.orders else 'hold'} |")
            lines += ["", "## Others in sight", ""]
        others = [u for u in self.units.values() if u.owner != p and (u.x, u.y) in seen]
        lines += [f"- {self.name(u.owner)} at {u.x},{u.y}, strength {u.strength}" for u in others] or ["- none"]
        return "\n".join(lines) + "\n"

    def score(self, p):
        return 3 * sum(1 for o in self.towns.values() if o == p) + sum(u.strength for u in self.units.values() if u.owner == p)

    def final_result(self):
        order = sorted(range(self.n), key=lambda p: -self.score(p))
        lines = ["# Final result", "", "| Player | Towns | Strength | Score |", "|---|---|---|---|"]
        for p in order:
            towns = sum(1 for o in self.towns.values() if o == p)
            lines.append(f"| {self.name(p)} | {towns} | {self.score(p) - 3 * towns} | {self.score(p)} |")
        best = self.score(order[0])
        return "\n".join(lines + ["", "Winner: " + ", ".join(self.name(p) for p in order if self.score(p) == best)])


# ---------------- UIs: one FolderUI per folder ----------------

class PlayerUI(FolderUI):
    def __init__(self, path, game, me):
        super().__init__(path)
        self.game, self.me = game, me
        self.seen = 0          # events already shown to this player
        self.briefed = None    # the turn this player was last briefed on

    def draw(self):
        g = self.game
        with g.lock:
            if g.phase == "over" or (g.phase == "orders" and self.me not in g.alive):
                self.close()
            last = LETTERS[g.n - 1]
            self.file("rules.md", RULES.format(turns=g.turns, letter=g.letter(self.me), cost=RECRUIT_COST,
                                               last=last, last_lower=last.lower()))
            if g.phase == "join":
                if not g.names[self.me]:
                    self.input("join")
                return
            self.file("map.md", g.map_md(self.me))
            self.file("units.md", g.units_md(self.me))
            mine = [e for to, e in g.events if to is None or self.me in to]
            self.file("log.md", "# Log\n\n" + "\n\n".join(mine) + "\n")
            if g.phase == "orders" and self.me in g.alive and self.me not in g.ended:
                for u in g.units.values():
                    if u.owner == self.me:
                        self.input(f"units/{u.id}/move")
                        self.input(f"units/{u.id}/hold")
                for (x, y), o in g.towns.items():
                    if o == self.me:
                        self.input(f"towns/{x}_{y}/recruit")
                self.input("say")
                self.input("end_turn")

    def display(self, result):
        g = self.game
        with g.lock:
            mine = [e for to, e in g.events if to is None or self.me in to]
            new, self.seen = mine[self.seen:], len(mine)
            parts = [result, *new]
            if self.closed:
                parts.append("-- Game over --")
            elif g.phase == "join":
                parts.append("Join the game with ./join.sh <name> (rules in rules.md).")
            elif self.inputs and self.briefed != g.turn:  # first response of a new turn: a full briefing
                self.briefed = g.turn
                units = [f"#{u.id} at {u.x},{u.y} (strength {u.strength})"
                         for u in sorted(g.units.values(), key=lambda u: u.id) if u.owner == self.me]
                towns = [f"{x}_{y}" for (x, y), o in sorted(g.towns.items()) if o == self.me]
                parts.append(f"Turn {g.turn}/{g.turns}. You are {g.name(self.me)} with {g.gold[self.me]} gold.\n"
                             f"Units: {', '.join(units) or 'none'}\nTowns: {', '.join(towns) or 'none'}\n"
                             "Give orders with ./units/<id>/move.sh <N|S|E|W>, ./towns/<x>_<y>/recruit.sh <n>, "
                             "./say.sh <message>, then ./end_turn.sh. See map.md and units.md.")
        return "\n\n".join(x for x in parts if x)

    def action(self, name, args):
        g, text = self.game, " ".join(args).strip()
        parts = name.split("/")
        if name == "join":
            return g.act(self.me, "join", text) if text else "Rejected: usage ./join.sh <name>"
        if name == "say":
            return g.act(self.me, "say", text)
        if name == "end_turn":
            return g.act(self.me, "end_turn")
        if parts[0] == "units":
            uid = int(parts[1])
            if parts[2] == "hold":
                return g.act(self.me, "hold", uid)
            return g.act(self.me, "move", uid, text.upper())
        if parts[0] == "towns":
            x, y = map(int, parts[1].split("_"))
            if not text.isdigit():
                return "Rejected: usage ./towns/<x>_<y>/recruit.sh <strength>"
            return g.act(self.me, "recruit", (x, y), int(text))
        return f"Rejected: unknown button {name}"


class SpectatorUI(FolderUI):
    """Spectator: sees the whole map and every event, has no buttons."""

    def __init__(self, path, game):
        super().__init__(path)
        self.game = game

    def draw(self):
        g = self.game
        with g.lock:
            if g.phase == "over":
                self.close()
            self.file("map.md", g.map_md("spectator"))
            self.file("units.md", g.units_md("spectator"))
            public = [e for to, e in g.events if to is None or "spectator" in to]  # not each player's copy of a report
            self.file("log.md", "# Log\n\n" + "\n\n".join(public) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", type=int, default=3)
    ap.add_argument("--size", type=int, help="map width and height (default depends on the player count)")
    ap.add_argument("--turns", type=int, default=12)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--dir")
    args = ap.parse_args()
    if not 2 <= args.players <= len(LETTERS):
        raise SystemExit(f"players must be between 2 and {len(LETTERS)}")
    root = Path(args.dir or tempfile.mkdtemp(prefix="folderui-frontier-"))

    game = Frontier(args.players, args.turns, args.seed, args.size)
    uis = {p: PlayerUI(root / f"player_{p}", game, p) for p in range(args.players)}
    uis["spectator"] = SpectatorUI(root / "spectator", game)
    game.uis = list(uis.values())

    threads = [ui.run(background=True) for ui in uis.values()]
    for who, ui in uis.items():
        print(f"{who}: {ui.root}", flush=True)
    for t in threads:
        t.join()
    with game.lock:
        print(game.final_result())


if __name__ == "__main__":
    main()
