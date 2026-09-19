import time
import random
import os
import sys
import select
import termios
import tty


MESSAGES = [
    "Preparing knife...",
    "Preparing mentally...",
    "Sneaking from behind...",
    "Checking if anyone is watching...",
    "Walking suspiciously...",
    "Pretending to do a task...",
    "Looking innocent...",
    "Activating murder.exe...",
    "Finding the nearest victim...",
    "Making a very questionable decision...",
    "Sharpening imaginary knife...",
    "Hiding in the shadows...",
    "Waiting for the perfect moment...",
    "Practicing evil laugh...",
    "Making sure nobody is looking...",
]


KEYS = {
    " ": "SPACE",
    "\n": "ENTER",
    "\t": "TAB",
    "\x7f": "BACKSPACE",
}


def get_key(timeout):
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    try:
        tty.setcbreak(fd)

        ready, _, _ = select.select(
            [sys.stdin],
            [],
            [],
            timeout
        )

        if ready:
            return sys.stdin.read(1)

        return None

    finally:
        termios.tcsetattr(
            fd,
            termios.TCSADRAIN,
            old_settings
        )


def kill_minigame():

    os.system("clear")

    print("=== KILL ===")
    print()

    # Shuffle the messages so they appear in random order
    messages = MESSAGES.copy()
    random.shuffle(messages)

    # Random number of preparation messages
    num_messages = random.randint(2, 4)

    for message in messages[:num_messages]:

        os.system("clear")

        print("=== KILL ===")
        print()
        print(message)

        time.sleep(1)

    # Choose a random key
    correct_key = random.choice(list(KEYS.keys()))
    key_name = KEYS[correct_key]

    os.system("clear")

    print("=== KILL ===")
    print()
    print(f"PRESS [{key_name}] NOW!")

    # Fixed response time
    time_limit = 1.5

    pressed_key = get_key(time_limit)

    os.system("clear")

    if pressed_key == correct_key:

        print("=== KILL SUCCESS ===")
        print()
        print("Target terminated.")

        return True

    elif pressed_key is None:

        print("=== KILL FAILED ===")
        print()
        print("You hesitated.")
        print("The target escaped.")

        return False

    else:

        pressed_name = KEYS.get(
            pressed_key,
            repr(pressed_key)
        )

        print("=== KILL FAILED ===")
        print()
        print(f"You pressed [{pressed_name}]")
        print(f"You needed [{key_name}]")
        print("The target escaped.")

        return False


if __name__ == "__main__":
    kill_minigame()