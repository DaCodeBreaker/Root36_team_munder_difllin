import random

WORDS = [
    "python",
    "network",
    "kernel",
    "process",
    "server",
    "terminal",
    "malware",
    "database",
    "compiler",
    "algorithm",
    "linux",
    "ubuntu",
    "socket",
    "client",
    "packet",
    "router",
    "firewall",
    "command",
    "console",
    "system",
    "binary",
    "script",
    "syntax",
    "debugger",
    "runtime",
    "program",
    "memory",
    "thread",
    "filesystem",
    "directory"
]

HANGMAN = [
    """
     +---+
         |
         |
         |
        ===
    """,
    """
     +---+
     O   |
         |
         |
        ===
    """,
    """
     +---+
     O   |
     |   |
         |
        ===
    """,
    """
     +---+
     O   |
    /|   |
         |
        ===
    """,
    """
     +---+
     O   |
    /|\\  |
         |
        ===
    """,
    """
     +---+
     O   |
    /|\\  |
    /    |
        ===
    """,
    """
     +---+
     O   |
    /|\\  |
    / \\  |
        ===
    """
]


def play_hangman():
    word = random.choice(WORDS)

    guessed = set()
    wrong_guesses = 0
    max_wrong = 6

    print("=== HANGMAN ===")

    while wrong_guesses < max_wrong:

        # Display current word
        display = ""

        for letter in word:
            if letter in guessed:
                display += letter + " "
            else:
                display += "_ "

        print(HANGMAN[wrong_guesses])
        print("Word:", display)
        print("Wrong guesses:", wrong_guesses, "/", max_wrong)

        if all(letter in guessed for letter in word):
            print("\nYou won!")
            print("The word was:", word)
            return True

        guess = input("Guess a letter: ").lower().strip()

        # Validate input
        if len(guess) != 1 or not guess.isalpha():
            print("Enter exactly one letter.\n")
            continue

        if guess in guessed:
            print("You already guessed that letter.\n")
            continue

        guessed.add(guess)

        if guess in word:
            print("Correct!\n")
        else:
            print("Wrong!\n")
            wrong_guesses += 1

    print(HANGMAN[wrong_guesses])
    print("You lost!")
    print("The word was:", word)

    return False


if __name__ == "__main__":
    play_hangman()