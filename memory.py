import random
import os
import time

def create_board():
    symbols = list("@@##$$!!")
    random.shuffle(symbols)
    return symbols


def display_board(board, revealed, matched):
    print()

    for i in range(8):
        if i in revealed or i in matched:
            print(f" {board[i]} ", end="")
        else:
            print(" ? ", end="")

        # 4 columns
        if (i + 1) % 4 == 0:
            print()

    print()


def play_memory():
    board = create_board()

    revealed = set()
    matched = set()
    moves = 0

    print("=== MEMORY GAME ===")
    print("Memorize the board!")

    display_board(board, set(range(8)), matched)

    time.sleep(2)

    os.system("clear")

    print("Find all matching pairs.")
    print("Positions are numbered 1-8.")
    print("Positions are numbered 1-8.")

    while len(matched) < 8:
        os.system("clear")
        display_board(board, revealed, matched)

        # First card
        while True:
            try:
                first = int(input("First position: ")) - 1

                if first < 0 or first >= 8:
                    print("Choose a position from 1-8.")
                elif first in matched:
                    print("That card has already been matched.")
                else:
                    break

            except ValueError:
                print("Enter a number.")

        revealed.add(first)

        os.system("clear")
        display_board(board, revealed, matched)

        # Second card
        while True:
            try:
                second = int(input("Second position: ")) - 1

                if second < 0 or second >= 8:
                    print("Choose a position from 1-8.")
                elif second == first:
                    print("Choose a different card.")
                elif second in matched:
                    print("That card has already been matched.")
                else:
                    break

            except ValueError:
                print("Enter a number.")

        revealed.add(second)

        os.system("clear")
        display_board(board, revealed, matched)

        moves += 1

        if board[first] == board[second]:
            print("MATCH!")
            matched.add(first)
            matched.add(second)

        else:
            print("Not a match.")

            input("Press Enter to continue...")

            revealed.remove(first)
            revealed.remove(second)

    print()
    print("You found all the pairs!")
    print(f"Completed in {moves} moves.")


if __name__ == "__main__":
    play_memory()