import socket
import threading
import json


# ============================================================
# CONFIG
# ============================================================

TCP_PORT = 5000
DISCOVERY_PORT = 5001


# ============================================================
# SERVER STATE
# ============================================================

players = {}
connections = {}

next_player_id = 1

# Protects players/connections dictionaries
lock = threading.Lock()

# Protects sending data over sockets
send_lock = threading.Lock()

# This is NOT set until the host types "start game"
game_started = threading.Event()


# ============================================================
# MESSAGE FUNCTIONS
# ============================================================

def send_message(conn, message):
    """
    Send one JSON message over TCP.
    A newline marks the end of the message.
    """

    data = (json.dumps(message) + "\n").encode()

    with send_lock:
        conn.sendall(data)


def receive_message(conn):
    """
    Receive one complete JSON message.

    TCP is a stream, so we use newline as our
    message separator.
    """

    data = b""

    while b"\n" not in data:
        chunk = conn.recv(4096)

        if not chunk:
            return None

        data += chunk

    line, _ = data.split(b"\n", 1)

    return json.loads(line.decode())


def broadcast(message):
    """
    Send a message to every connected player.
    """

    with lock:
        player_connections = list(connections.values())

    for conn in player_connections:

        try:
            send_message(conn, message)

        except ConnectionError:
            pass


# ============================================================
# PLAYER MANAGEMENT
# ============================================================

def add_player(conn, name):

    global next_player_id

    with lock:

        player_id = next_player_id
        next_player_id += 1

        players[player_id] = {
            "Name": name
        }

        connections[player_id] = conn

    return player_id


def remove_player(player_id):

    with lock:

        player = players.pop(player_id, None)
        conn = connections.pop(player_id, None)

    if conn:
        conn.close()

    if player:

        broadcast({
            "Type": "Leave",
            "Player": player["Name"],
            "Message": f'{player["Name"]} left the game.'
        })


# ============================================================
# GAME LOGIC
# ============================================================

def process_message(player_id, message):

    with lock:
        player = players.get(player_id)

    if player is None:
        return

    name = player["Name"]

    message_type = message.get("Type")


    # --------------------------------------------------------
    # CHAT
    # --------------------------------------------------------

    if message_type == "Chat":

        broadcast({
            "Type": "Chat",
            "Player": name,
            "Message": message.get("Message", "")
        })


    # --------------------------------------------------------
    # Voting
    # --------------------------------------------------------

    elif message_type == "Vote":

        print(
            f"{name} Voted",
            message
        )

        # Put your actual chat-game logic here later.

# ============================================================
# PLAYER CONNECTION HANDLER
# ============================================================

def handle_player(conn, addr):

    player_id = None

    try:

        # ====================================================
        # PLAYER JOIN
        # ====================================================

        player_info = receive_message(conn)

        if player_info is None:
            conn.close()
            return

        name = player_info["Name"]

        player_id = add_player(conn, name)

        print()
        print(f"{name} connected from {addr}")

        # Tell everyone that this player joined
        broadcast({
            "Type": "Join",
            "Player": name,
            "Message": f"{name} just popped in!"
        })


        # ====================================================
        # WAIT FOR HOST TO START GAME
        # ====================================================

        print(f"{name} is waiting for the game to start...")

        game_started.wait()


        # ====================================================
        # GAME HAS STARTED
        # ====================================================

        print(f"{name} is now listening for game messages.")

        # Tell this player the game has started
        send_message(conn, {
            "Type": "GameStart"
        })


        # ====================================================
        # LISTEN FOR GAME MESSAGES
        # ====================================================

        while True:

            message = receive_message(conn)

            if message is None:
                break

            print(
                f"{name} -> {message}"
            )

            process_message(
                player_id,
                message
            )


    except (ConnectionError, json.JSONDecodeError, KeyError) as e:

        print(
            f"Connection error with {addr}:",
            e
        )


    finally:

        if player_id is not None:
            remove_player(player_id)


# ============================================================
# ACCEPT PLAYERS
# ============================================================

def accept_players(server):

    while True:

        try:

            conn, addr = server.accept()


            thread = threading.Thread(
                target=handle_player,
                args=(conn, addr),
                daemon=True
            )

            thread.start()

        except OSError:

            break


# ============================================================
# UDP DISCOVERY
# ============================================================

def discovery_loop(discovery, game_id):

    while True:

        try:

            data, addr = discovery.recvfrom(1024)

            requested_game = data.decode()


            if requested_game == game_id:

                discovery.sendto(
                    str(TCP_PORT).encode(),
                    addr
                )


        except OSError:

            break


# ============================================================
# HOST GAME
# ============================================================

def host_game(name):

    game_id = input("Enter Game ID: ").strip()

    max_players = int(
        input("Enter maximum players: ")
    )


    # ========================================================
    # TCP SERVER
    # ========================================================

    server = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM
    )

    server.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_REUSEADDR,
        1
    )

    server.bind(
        ("0.0.0.0", TCP_PORT)
    )

    server.listen(max_players)


    # ========================================================
    # UDP DISCOVERY SERVER
    # ========================================================

    discovery = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM
    )

    discovery.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_REUSEADDR,
        1
    )

    discovery.bind(
        ("0.0.0.0", DISCOVERY_PORT)
    )


    # ========================================================
    # START DISCOVERY THREAD
    # ========================================================

    threading.Thread(
        target=discovery_loop,
        args=(discovery, game_id),
        daemon=True
    ).start()


    # ========================================================
    # START PLAYER ACCEPT THREAD
    # ========================================================

    threading.Thread(
        target=accept_players,
        args=(server,),
        daemon=True
    ).start()


    print()
    print("================================")
    print("             LOBBY")
    print("================================")
    print("Game ID:", game_id)
    print("Maximum players:", max_players)
    print()
    print("Waiting for players...")
    print()


    # ========================================================
    # HOST CONNECTS TO OWN SERVER
    # ========================================================

    host_connection = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM
    )

    host_connection.connect(
        ("127.0.0.1", TCP_PORT)
    )


    # Host joins as a player
    send_message(
        host_connection,
        {
            "Type": "Join",
            "Name": name
        }
    )


    # Host receives messages from server
    threading.Thread(
        target=client_receive_loop,
        args=(host_connection,),
        daemon=True
    ).start()


    # ========================================================
    # WAIT FOR HOST COMMAND
    # ========================================================

    while True:

        command = input("> ").strip().lower()

        if command == "start game":

            print()
            print("================================")
            print("          GAME STARTED")
            print("================================")
            print()

            # This releases every handle_player()
            # that is currently waiting.
            game_started.set()

            break

        else:

            print(
                "Type 'start game' to begin."
            )


    # ========================================================
    # HOST IS NOW A PLAYER
    # ========================================================

    client_game_loop(
        host_connection
    )


# ============================================================
# JOIN GAME
# ============================================================

def join_game(game_id):

    discovery = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM
    )

    discovery.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_BROADCAST,
        1
    )

    discovery.settimeout(3)


    print()
    print(
        "Searching for game:",
        game_id
    )


    # Broadcast Game ID
    discovery.sendto(
        game_id.encode(),
        ("<broadcast>", DISCOVERY_PORT)
    )


    try:

        port_data, host_addr = discovery.recvfrom(
            1024
        )

    except socket.timeout:

        print()
        print("Game not found.")

        discovery.close()

        return None


    # IP comes from the UDP sender
    host_ip = host_addr[0]

    host_port = int(
        port_data.decode()
    )


    discovery.close()


    # ========================================================
    # TCP CONNECTION
    # ========================================================

    client = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM
    )

    client.connect(
        (host_ip, host_port)
    )


    print(
        "Connected to game!"
    )


    return client


# ============================================================
# CLIENT RECEIVE LOOP
# ============================================================

def client_receive_loop(conn):

    while True:

        try:

            message = receive_message(conn)

            if message is None:
                break

            handle_server_message(
                message
            )

        except (
            ConnectionError,
            json.JSONDecodeError
        ):

            break


# ============================================================
# HANDLE SERVER MESSAGE
# ============================================================

def handle_server_message(message):

    message_type = message.get("Type")


    # --------------------------------------------------------
    # PLAYER JOINED
    # --------------------------------------------------------

    if message_type == "Join":

        print(
            message["Message"]
        )


    # --------------------------------------------------------
    # PLAYER LEFT
    # --------------------------------------------------------

    elif message_type == "Leave":

        print(
            message["Message"]
        )


    # --------------------------------------------------------
    # GAME STARTED
    # --------------------------------------------------------

    elif message_type == "GameStart":

        print()
        print("==============================")
        print("       GAME HAS STARTED")
        print("==============================")
        print()


    # --------------------------------------------------------
    # CHAT
    # --------------------------------------------------------

    elif message_type == "Chat":

        print(
            f'{message["Player"]}: '
            f'{message["Message"]}'
        )


    # --------------------------------------------------------
    # VOTIING
    # --------------------------------------------------------

    elif message_type == "Vote":

        print(
            "Voted:",
            message
        )


# ============================================================
# SEND CHAT
# ============================================================

def send_chat(conn, message):

    send_message(
        conn,
        {
            "Type": "Chat",
            "Message": message
        }
    )
def send_whisper(conn, message):
    send_message(   
        conn,
        {
            "Type": "Chat",
            "Message": message
        }
    )

# ============================================================
# CLIENT GAME LOOP
# ============================================================

def client_game_loop(conn):

    while True:

        message = input("> ")

        if message == "/quit":
            break


        if message.strip() == "":
            continue

        if '/vote' in message:
            pass
        else:
            send_chat(
            conn,
            message
        )


    conn.close()


# ============================================================
# CLIENT GAME
# ============================================================

def client_game(conn, name):

    # ========================================================
    # TELL SERVER WHO WE ARE
    # ========================================================

    send_message(
        conn,
        {
            "Type": "Join",
            "Name": name
        }
    )


    # ========================================================
    # RECEIVE SERVER MESSAGES
    # ========================================================

    threading.Thread(
        target=client_receive_loop,
        args=(conn,),
        daemon=True
    ).start()


    # ========================================================
    # WAIT FOR GAME START
    # ========================================================

    client_game_loop(
        conn
    )


# ============================================================
# MAIN
# ============================================================

def main():

    name = input(
        "Enter your name: "
    ).strip()


    choice = input(
        "HOST or CLIENT: "
    ).strip().lower()


    # ========================================================
    # HOST
    # ========================================================

    if choice == "host":

        host_game(name)


    # ========================================================
    # CLIENT
    # ========================================================

    elif choice == "client":

        game_id = input(
            "Enter Game ID: "
        ).strip()


        connection = join_game(
            game_id
        )


        if connection:

            client_game(
                connection,
                name
            )


    # ========================================================
    # INVALID
    # ========================================================

    else:

        print(
            "Invalid option."
        )


# ============================================================
# PROGRAM START
# ============================================================

if __name__ == "__main__":

    main()