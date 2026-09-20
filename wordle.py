import random

WORDS = [
    "shell", "linux", "cache", "debug", "query",
    "stack", "array", "bytes", "proxy", "codec",
    "drive", "event", "error", "input", "logic",
    "patch", "queue", "route", "space", "state",
    "token", "trace", "virus", "write", "admin",
    "block", "build", "click", "crash", "fetch",
    "files", "float", "frame", "guest", "inode",
    "parse", "ports", "reset", "stdin", "print"
]


def check_guess(guess, word):
    result = []

    for i in range(5):
        if guess[i] == word[i]:
            result.append("🟩")

#need to add separate if for repeated letters
        elif guess[i] in word: 
            result.append("🟨")
        else:
            result.append("⬜")

    return result


def play_wordle():
    word = random.choice(WORDS)

    print("=== WORDLE ===")
    print("Guess the 5-letter word.")
    print("🟩 = correct position")
    print("🟨 = wrong position")
    print("⬜ = not in word")
    print()

    attempts = 6

    for attempt in range(attempts):
        while True:
            guess = input(
                f"Attempt {attempt + 1}/{attempts}: "
            ).lower().strip()

            if len(guess) != 5:
                print("Word must be exactly 5 letters.")
                continue

            if not guess.isalpha():
                print("Only use letters.")
                continue

            break

        result = check_guess(guess, word)

        print(" ".join(result))
        print()

        if guess == word:
            print("You got it!")
            print(f"The word was: {word}")
            return True

    print("You lost!")
    print(f"The word was: {word}")

    return False


if __name__ == "__main__":
    play_wordle()