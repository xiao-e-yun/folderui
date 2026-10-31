"""A random bot for The Interrogation Room; plays whichever role its folder is:

    python examples/interrogation_bot.py <folder>
"""
import random
import sys

from folderui import Session

SUSPECTS = ["Mara", "Theo", "Iris", "Jonah"]
KINDS = ["affair", "bribe", "debt", "gambling", "theft", "none"]


def main(path):
    s, rng = Session(path), random.Random()
    s.ready()
    asked = 0
    while inputs := s.inputs():
        if "begin" in inputs:
            s.input("begin")
        elif "say" in inputs and rng.random() < 0.5:
            s.input("say", rng.choice(["I was in the kitchen all night.", "Let's all keep it simple.", "I saw nothing."]))
        elif "ready" in inputs:
            s.input("ready")
        elif "answer" in inputs:
            s.input("answer", rng.choice(["I don't remember.", "I was elsewhere.", "Ask someone else."]))
        elif "accept" in inputs:
            if rng.random() < 0.5:
                s.input("accept", rng.choice(SUSPECTS), "It was them.")
            else:
                s.input("decline", "I have nothing to say.")
        elif "ask" in inputs and asked < 6:
            asked += 1
            kind = rng.choice(["ask", "show", "offer"] if "offer" in inputs else ["ask", "show"])
            target = rng.choice(SUSPECTS)
            if kind == "ask":
                s.input("ask", target, "Where were you at 22:40?")
            elif kind == "show":
                s.input("show", target, f"E{rng.randint(1, 8)}", "Explain this.")
            else:
                s.input("offer", target, "Name the thief and walk free.")
        elif "accuse" in inputs:
            for suspect in SUSPECTS:
                s.input("secret", suspect, rng.choice(KINDS))
            s.input("accuse", rng.choice(SUSPECTS))
    print(f"{path}: done")


if __name__ == "__main__":
    main(sys.argv[1])
