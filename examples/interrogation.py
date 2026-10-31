"""Multiplayer example: The Interrogation Room. One detective against four suspects, on a fixed case file.

    uv run python examples/interrogation.py [--questions 16] [--dir DIR]

The suspects get a short huddle in the waiting room, then they are separated. The detective questions one
suspect at a time, can show evidence and offer deals, and finally accuses someone. Everyone except the
culprit is innocent of the theft, but each suspect has a secret of their own worth lying for.
"""
import argparse
import tempfile
import threading
from pathlib import Path

from folderui import Action, File, FolderUI

SUSPECTS = ["Mara", "Theo", "Iris", "Jonah"]
CULPRIT = "Mara"
SECRETS = {"Mara": "debt", "Theo": "theft", "Iris": "affair", "Jonah": "bribe"}
SECRET_KINDS = ["affair", "bribe", "debt", "gambling", "theft", "none"]
HUDDLE_LIMIT = 3  # messages per suspect in the waiting room
DEAL_LIMIT = 2

CASE = """# The Hale Gallery theft

On the night of the Hale Gallery's anniversary party, the painting *"Harbor at Dusk"* (insured for
2.4 million) was taken from the vault at the end of the east corridor. It was discovered missing at 23:30.
The four people below were in the building's staff areas between 22:00 and 23:00 and are the suspects.

| Suspect | Role |
|---|---|
| Mara Voss | Curator of the gallery |
| Theo Lin | Head of the catering team |
| Iris Kade | Art critic, invited guest |
| Jonah Pell | Night security guard |

The gallery owner, Victor Hale, says he spent the evening "in his office, working".
"""

EVIDENCE = {
    "E1": "Vault access log: the vault door was opened at 22:41 with the master code. "
          "Only the curator and the owner know the master code.",
    "E2": "Camera log: the east corridor camera was offline from 22:25 to 22:55. "
          "It was switched off from the guard terminal.",
    "E3": "Forensics: green silk fibers were found on the vault door handle.",
    "E4": "Cloakroom list: Mara Voss checked in a coat and wore a green silk scarf; Iris Kade wore a red dress; "
          "Theo Lin and Jonah Pell were in uniform.",
    "E5": "Cellar inventory: three bottles of 1990 Bordeaux are missing from the wine cellar, "
          "whose stairs open onto the east corridor.",
    "E6": "Jonah Pell's phone: an online poker app was active from 22:20 to 22:50.",
    "E7": "Bank records: Mara Voss owes 40,000 to a private lender, R. Duval. The payment was due last week.",
    "E8": "The owner's office is next to the vault. Its cleaning log notes two wine glasses, one with red lipstick, "
          "found there the next morning.",
}

ROLES = {
    "Mara": """You are **Mara Voss**, the curator. **You stole the painting.**

What really happened:
- You owe 40,000 to a lender, R. Duval, and you were desperate.
- At 22:15 you gave Jonah, the guard, 200 in cash to switch off the east corridor camera, telling him it was
  for a "surprise anniversary video". He doesn't know why you really wanted it.
- At 22:41 you opened the vault with the master code and took the painting. You wore your green silk scarf.
- On the way you saw Theo coming up the cellar stairs carrying wine bottles, around 22:35.
- The painting is now hidden in the van of a framing company you use.

Your goal: **don't get accused.** You may lie, and you may try to steer suspicion onto someone else.
If the detective offers you a deal, you can accept it and name someone else; if the detective then accuses
that person, you win.
""",
    "Theo": """You are **Theo Lin**, head of catering. **You did not steal the painting.**

What really happened:
- From 22:30 to 22:40 you were in the wine cellar, stealing three bottles of 1990 Bordeaux to sell.
  If that comes out, you lose your job.
- At about 22:38, from the top of the cellar stairs, you saw a woman in a green scarf walk down the east
  corridor toward the vault.

Your secret: **theft** (the wine).
Your goal: **don't get accused, and keep your secret.** You may lie to protect it.
If the detective offers you a deal and you name the real thief, you walk free even if your secret comes out.
Name the wrong person and you lose.
""",
    "Iris": """You are **Iris Kade**, an art critic. **You did not steal the painting.**

What really happened:
- From 22:15 to 22:50 you were in Victor Hale's office with him. You are having an affair; his wife is
  one of the gallery's biggest patrons. If it comes out, it would ruin you both.
- The office shares a wall with the vault. Around 22:40 you heard the vault door's beep through the wall.
- Victor was with you the whole time.

Your secret: **affair**.
Your goal: **don't get accused, and keep your secret.** You may lie to protect it.
If the detective offers you a deal and you name the real thief, you walk free even if your secret comes out.
Name the wrong person and you lose.
""",
    "Jonah": """You are **Jonah Pell**, the night guard. **You did not steal the painting.**

What really happened:
- At 22:15 Mara, the curator, gave you 200 in cash to switch off the east corridor camera "for a surprise
  anniversary video". You switched it off at 22:25.
- From 22:20 to 22:50 you played online poker on your phone instead of doing your rounds.
- Taking money to switch off a camera would get you fired, maybe charged.

Your secret: **bribe** (the 200 from Mara).
Your goal: **don't get accused, and keep your secret.** You may lie to protect it.
If the detective offers you a deal and you name the real thief, you walk free even if your secret comes out.
Name the wrong person and you lose.
""",
}

DETECTIVE_RULES = """# You are the detective

Read case.md and evidence.md. The suspects had a short time together in the waiting room before you
separated them; you did not hear what they said.

You have **{questions} questions**. Each of these costs one:

- `./ask.sh <suspect> <question>`: ask one suspect a question; their answer is the response
- `./show.sh <suspect> <evidence id> <question>`: show them a piece of evidence (e.g. E3) and ask about it
- `./offer.sh <suspect> <terms>`: offer a deal (at most {deals}): they may accept and name the thief,
  or decline. A suspect who names the real thief walks free if you convict that person.

Free, at any time:

- `./secret.sh <suspect> <kind>`: record your guess of a suspect's personal secret, one of
  {kinds} (you can change it until you accuse)
- `./accuse.sh <suspect>`: name the thief. This ends the game.

Scoring: 3 points for the right thief, plus 1 for each correct secret. Transcripts are in transcripts/.
"""

SUSPECT_RULES = """# You are a suspect

Your private briefing is in role.md. The case as everyone knows it is in case.md.

1. **Waiting room**: before you are separated, the four suspects can talk. Each of you can send up to
   {huddle} messages with `./say.sh <message>`, then press `./ready.sh`. The detective will not hear this.
2. **Interrogation**: you will be questioned alone. When the detective asks you something, answer with
   `./answer.sh <your answer>`. If you are offered a deal, use `./accept.sh <name of the thief> <statement>`
   or `./decline.sh <statement>`. You never see what the others are asked or answer.

You may lie. The detective has evidence you have not seen.
"""


# ---------------- Game: pure logic, knows nothing about folders ----------------

class Interrogation:
    def __init__(self, questions=16):
        self.lock = threading.Lock()
        self.uis: list[FolderUI] = []
        self.events: list[tuple[set, str]] = []  # (recipients, text), append-only
        self.phase = "huddle"  # huddle -> interrogation -> over
        self.questions_left, self.deals_left = questions, DEAL_LIMIT
        self.said = {s: 0 for s in SUSPECTS}
        self.ready: set[str] = set()
        self.detective_ready = False
        self.pending: tuple[str, str] | None = None  # (suspect, "question" | "deal")
        self.transcripts = {s: [] for s in SUSPECTS}
        self.deals: dict[str, str] = {}  # suspect -> who they named
        self.secret_guesses: dict[str, str] = {}
        self.accused: str | None = None

    def tell(self, to, text):
        self.events.append((set(to), text))

    def act(self, who, kind, *args):
        with self.lock:
            try:
                result = getattr(self, f"do_{kind}")(who, *args)
            except ValueError as e:
                result = f"Rejected: {e}"
        for ui in self.uis:
            ui.refresh()
        return result

    def suspect(self, name):
        name = name.strip().capitalize()
        if name not in SUSPECTS:
            raise ValueError("suspect must be one of " + ", ".join(SUSPECTS))
        return name

    # ---- waiting room ----

    def do_say(self, who, text):
        if self.phase != "huddle" or who in self.ready:
            raise ValueError("the waiting room is closed for you")
        if self.said[who] >= HUDDLE_LIMIT:
            raise ValueError(f"you have used all {HUDDLE_LIMIT} messages; press ./ready.sh")
        if not 1 <= len(text) <= 500:
            raise ValueError("the message must be 1-500 characters")
        self.said[who] += 1
        self.tell(SUSPECTS + ["spectator"], f"[waiting room] {who}: {text}")
        return f"Said ({HUDDLE_LIMIT - self.said[who]} messages left)."

    def do_ready(self, who):
        if self.phase != "huddle":
            raise ValueError("not in the waiting room")
        self.ready.add(who)
        self.maybe_start()
        return "You are taken to a separate room."

    def do_begin(self, who):
        self.detective_ready = True
        self.maybe_start()
        return "The suspects are brought in."

    def maybe_start(self):
        if self.detective_ready and self.ready >= set(SUSPECTS):
            self.phase = "interrogation"
            self.tell(["detective", "spectator"] + SUSPECTS, "The suspects have been separated. The interrogation begins.")

    # ---- interrogation ----

    def check_detective_turn(self, cost=True):
        if self.phase != "interrogation" or self.pending:
            raise ValueError("you cannot do that now")
        if cost and self.questions_left <= 0:
            raise ValueError("you have no questions left; record secrets and accuse someone")

    def record(self, suspect, text):
        self.transcripts[suspect].append(text)
        self.tell(["detective", suspect, "spectator"], text)

    def do_ask(self, who, name, question):
        self.check_detective_turn()
        s = self.suspect(name)
        if not question:
            raise ValueError("usage: ./ask.sh <suspect> <question>")
        self.questions_left -= 1
        self.pending = (s, "question")
        self.record(s, f"Detective to {s}: {question}")
        return f"You ask {s}."

    def do_show(self, who, name, eid, question):
        self.check_detective_turn()
        s, eid = self.suspect(name), eid.strip().upper()
        if eid not in EVIDENCE:
            raise ValueError("evidence id must be one of " + ", ".join(EVIDENCE))
        self.questions_left -= 1
        self.pending = (s, "question")
        self.record(s, f"Detective shows {s} evidence {eid}: \"{EVIDENCE[eid]}\"\nDetective: {question or 'Explain this.'}")
        return f"You show {eid} to {s}."

    def do_offer(self, who, name, terms):
        self.check_detective_turn()
        s = self.suspect(name)
        if self.deals_left <= 0:
            raise ValueError("you have no deals left")
        if s in self.deals:
            raise ValueError(f"{s} has already taken a deal")
        self.questions_left -= 1
        self.deals_left -= 1
        self.pending = (s, "deal")
        self.record(s, f"Detective offers {s} a deal: {terms or 'Name the thief and you walk free.'}")
        return f"You offer {s} a deal."

    def check_answer(self, who, kind):
        if not self.pending or self.pending[0] != who or self.pending[1] != kind:
            raise ValueError("nobody is asking you that right now")

    def do_answer(self, who, text):
        self.check_answer(who, "question")
        if not text:
            raise ValueError("usage: ./answer.sh <your answer>")
        self.pending = None
        self.record(who, f"{who}: {text}")
        return "Answer given."

    def do_accept(self, who, name, statement):
        self.check_answer(who, "deal")
        named = self.suspect(name)
        if named == who:
            raise ValueError("you cannot name yourself")
        self.pending = None
        self.deals[who] = named
        self.record(who, f"{who} accepts the deal and names {named}: {statement}")
        return f"You named {named}."

    def do_decline(self, who, statement):
        self.check_answer(who, "deal")
        self.pending = None
        self.record(who, f"{who} declines the deal: {statement or '(no comment)'}")
        return "You declined."

    def do_secret(self, who, name, kind):
        if self.phase != "interrogation":
            raise ValueError("you cannot do that now")
        s, kind = self.suspect(name), kind.strip().lower()
        if kind not in SECRET_KINDS:
            raise ValueError("kind must be one of " + ", ".join(SECRET_KINDS))
        self.secret_guesses[s] = kind
        return f"Noted: you think {s}'s secret is {kind}."

    def do_accuse(self, who, name):
        self.check_detective_turn(cost=False)
        self.accused = self.suspect(name)
        self.phase = "over"
        self.tell(["detective", "spectator"] + SUSPECTS, self.verdict())
        return f"You accuse {self.accused}."

    # ---- results ----

    def verdict(self):
        right = self.accused == CULPRIT
        secrets = sum(1 for s in SUSPECTS if self.secret_guesses.get(s) == SECRETS[s])
        lines = [f"# Verdict", "", f"The detective accused **{self.accused}**. The thief was **{CULPRIT}**.", "",
                 f"Detective: {'correct' if right else 'wrong'} accusation, {secrets} secret(s) right "
                 f"→ {3 * right + secrets} points.", "",
                 "| Suspect | Accused | Secret | Detective's guess | Deal | Result |", "|---|---|---|---|---|---|"]
        for s in SUSPECTS:
            guessed = self.secret_guesses.get(s, "-")
            deal = f"named {self.deals[s]}" if s in self.deals else "-"
            if s == CULPRIT:
                result = "WIN (got away)" if not right else "LOSE (caught)"
            elif s in self.deals and self.deals[s] != CULPRIT:
                result = "LOSE (named the wrong person)"
            elif s in self.deals and right:
                result = "WIN (deal paid off)"
            elif self.accused == s:
                result = "LOSE (wrongly accused)"
            else:
                result = "WIN" + (" (secret kept)" if guessed != SECRETS[s] else ", but the secret came out")
            lines.append(f"| {s} | {'yes' if self.accused == s else 'no'} | {SECRETS[s]} | {guessed} | {deal} | {result} |")
        return "\n".join(lines)

    def view_for(self, who):
        return [e for to, e in self.events if who in to]


# ---------------- UIs ----------------

class SuspectUI(FolderUI):
    def __init__(self, path, game, me):
        super().__init__(path)
        self.game, self.me, self.seen = game, me, 0

    def draw(self):
        out = []
        g = self.game
        with g.lock:
            if g.phase == "over":
                self.close()
            out.append(File("rules.md", SUSPECT_RULES.format(huddle=HUDDLE_LIMIT)))
            out.append(File("role.md", ROLES[self.me]))
            out.append(File("case.md", CASE))
            room = [e for to, e in g.events if self.me in to and e.startswith("[waiting room]")]
            if room:
                out.append(File("waiting_room.md", "# Waiting room\n\n" + "\n\n".join(room) + "\n"))
            if g.transcripts[self.me]:
                out.append(File("transcript.md", "# Your interrogation\n\n" + "\n\n".join(g.transcripts[self.me]) + "\n"))
            if g.phase == "huddle" and self.me not in g.ready:
                if g.said[self.me] < HUDDLE_LIMIT:
                    out.append(Action("say"))
                out.append(Action("ready"))
            elif g.pending and g.pending[0] == self.me:
                if g.pending[1] == "question":
                    out.append(Action("answer"))
                else:
                    out.append(Action("accept"))
                    out.append(Action("decline"))
        return out

    def display(self, result):
        g = self.game
        with g.lock:
            mine = g.view_for(self.me)
            new, self.seen = mine[self.seen:], len(mine)
            parts = [result, *new]
            if self.closed:
                parts.append("-- The case is closed --")
            elif g.phase == "huddle":
                parts.append(f"You are in the waiting room with the other suspects. ./say.sh <message> "
                             f"({HUDDLE_LIMIT - g.said[self.me]} left), then ./ready.sh. Read role.md first.")
            elif "answer" in self.inputs:
                parts.append("Answer with ./answer.sh <your answer>.")
            elif "accept" in self.inputs:
                parts.append("Accept with ./accept.sh <name of the thief> <statement>, or ./decline.sh <statement>.")
        return "\n\n".join(x for x in parts if x)

    def action(self, name, args):
        text = " ".join(args).strip()
        if name in ("say", "answer"):
            return self.game.act(self.me, name, text)
        if name == "ready":
            return self.game.act(self.me, "ready")
        if name == "accept":
            if not args:
                return "Rejected: usage ./accept.sh <name of the thief> <statement>"
            return self.game.act(self.me, "accept", args[0], " ".join(args[1:]))
        if name == "decline":
            return self.game.act(self.me, "decline", text)
        return f"Rejected: unknown button {name}"


class DetectiveUI(FolderUI):
    def __init__(self, path, game, questions):
        super().__init__(path)
        self.game, self.questions, self.seen = game, questions, 0

    def draw(self):
        out = []
        g = self.game
        with g.lock:
            if g.phase == "over":
                self.close()
            out.append(File("rules.md", DETECTIVE_RULES.format(questions=self.questions, deals=DEAL_LIMIT,
                                                         kinds=", ".join(SECRET_KINDS))))
            out.append(File("case.md", CASE))
            out.append(File("evidence.md", "# Evidence\n\n" + "\n".join(f"- **{k}**: {v}" for k, v in EVIDENCE.items()) + "\n"))
            for s in SUSPECTS:
                if g.transcripts[s]:
                    out.append(File(f"transcripts/{s}.md", f"# Interrogation of {s}\n\n" + "\n\n".join(g.transcripts[s]) + "\n"))
            guesses = ", ".join(f"{s}: {k}" for s, k in g.secret_guesses.items()) or "none yet"
            out.append(File("status.md", f"# Status\n\nQuestions left: {g.questions_left}\nDeals left: {g.deals_left}\n"
                                   f"Deals taken: {g.deals or 'none'}\nYour secret guesses: {guesses}\n"))
            if g.phase == "huddle" and not g.detective_ready:
                out.append(Action("begin"))
            elif g.phase == "interrogation" and not g.pending:
                if g.questions_left > 0:
                    out.append(Action("ask"))
                    out.append(Action("show"))
                    if g.deals_left > 0:
                        out.append(Action("offer"))
                out.append(Action("secret"))
                out.append(Action("accuse"))
        return out

    def display(self, result):
        g = self.game
        with g.lock:
            mine = g.view_for("detective")
            new, self.seen = mine[self.seen:], len(mine)
            parts = [result, *new]
            if self.closed:
                parts.append("-- The case is closed --")
            elif g.phase == "huddle":
                parts.append("Read case.md and evidence.md, then press ./begin.sh to bring in the suspects.")
            elif self.inputs:
                parts.append(f"Questions left: {g.questions_left}, deals left: {g.deals_left}. "
                             "./ask.sh, ./show.sh, ./offer.sh, ./secret.sh, ./accuse.sh (see rules.md).")
        return "\n\n".join(x for x in parts if x)

    def action(self, name, args):
        g = self.game
        if name == "begin":
            return g.act("detective", "begin")
        if name == "ask":
            return g.act("detective", "ask", args[0], " ".join(args[1:])) if args else "Rejected: usage ./ask.sh <suspect> <question>"
        if name == "show":
            return g.act("detective", "show", args[0], args[1], " ".join(args[2:])) if len(args) >= 2 else \
                "Rejected: usage ./show.sh <suspect> <evidence id> <question>"
        if name == "offer":
            return g.act("detective", "offer", args[0], " ".join(args[1:])) if args else "Rejected: usage ./offer.sh <suspect> <terms>"
        if name == "secret":
            return g.act("detective", "secret", args[0], args[1]) if len(args) >= 2 else "Rejected: usage ./secret.sh <suspect> <kind>"
        if name == "accuse":
            return g.act("detective", "accuse", args[0]) if args else "Rejected: usage ./accuse.sh <suspect>"
        return f"Rejected: unknown button {name}"


class SpectatorUI(FolderUI):
    """Spectator: the truth, the waiting room and every transcript."""

    def __init__(self, path, game):
        super().__init__(path)
        self.game = game

    def draw(self):
        out = []
        g = self.game
        with g.lock:
            if g.phase == "over":
                self.close()
            out.append(File("truth.md", f"# The truth\n\nThe thief is {CULPRIT}.\n\n" + "\n\n---\n\n".join(ROLES.values())))
            out.append(File("log.md", "# Log\n\n" + "\n\n".join(g.view_for("spectator")) + "\n"))
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", type=int, default=16)
    ap.add_argument("--dir")
    args = ap.parse_args()
    root = Path(args.dir or tempfile.mkdtemp(prefix="folderui-interrogation-"))

    game = Interrogation(args.questions)
    uis = {"detective": DetectiveUI(root / "detective", game, args.questions)}
    uis.update({s: SuspectUI(root / s.lower(), game, s) for s in SUSPECTS})
    uis["spectator"] = SpectatorUI(root / "spectator", game)
    game.uis = list(uis.values())

    threads = [ui.run(background=True) for ui in uis.values()]
    for who, ui in uis.items():
        print(f"{who}: {ui.root}", flush=True)
    for t in threads:
        t.join()
    with game.lock:
        print(game.verdict())


if __name__ == "__main__":
    main()
