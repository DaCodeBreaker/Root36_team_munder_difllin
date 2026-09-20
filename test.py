import socket
import threading
import json
import random
import time
from datetime import datetime


# ============================================================
# CONFIG
# ============================================================

TCP_PORT = 5000
DISCOVERY_PORT = 5001


# ============================================================
# GAME CONFIG
# ============================================================

DAY_DURATION = 120         # seconds
NIGHT_DURATION = 30       # seconds
DISCUSSION_DURATION = 45  # seconds
VOTING_DURATION = 20      # seconds
TASKS_PER_DAY = 2
MAX_SABOTAGES = 2
MAX_ROOTKIT_USES = 2


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

# Stores leftover TCP data for each connection
recv_buffers = {}

# Protects recv_buffers
recv_lock = threading.Lock()


# ============================================================
# GAME STATE (SERVER-SIDE)
# ============================================================

# Phase: "Lobby", "Day", "Night", "Discussion", "Voting", "GameOver"
current_phase = "Lobby"
day_number = 0

# Audit logs: { player_id: [ {"Room": str, "Timestamp": str}, ... ] }
audit_logs = {}

# Votes this round: { voter_id: target_name }
votes = {}

# Sabotage: shared bad-team resource
sabotages_remaining = MAX_SABOTAGES
sabotage_targets = set()  # player_ids whose messages are jumbled this night

# Rootkit uses remaining (2 total per game)
rootkit_uses_remaining = MAX_ROOTKIT_USES

# Antivirus one-use flag
antivirus_used = False

# Night action tracking (reset each night)
night_actions_done = {
    "virus_kill": False,
    "detective_inspect": False,
    "antivirus_revive": False,
}

# Players who died this cycle (for audit leak next night)
recently_dead = []

# Room -> minigame mapping
ROOM_TASKS = {
    "GPU": "memory",
    "CyberSec": "hangman",
    "Web Dev": "wordle",
}

# Lock for game state modifications
game_lock = threading.Lock()

# Event to signal early day end (all tasks complete)
day_early_end = threading.Event()


# ============================================================
# CLIENT STATE
# ============================================================

# The client's own connection (set during client_game)
client_conn = None

# The client's own role (set when RoleAssign message received)
client_role = None

# The client's own alive status
client_alive = True


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
    Receive exactly one JSON message.

    TCP is a stream, so one recv() does not necessarily
    equal one send(). We use newline as the message separator
    and keep leftover data for the next call.
    """

    with recv_lock:
        data = recv_buffers.get(conn, b"")

    while b"\n" not in data:

        chunk = conn.recv(4096)

        if not chunk:
            with recv_lock:
                recv_buffers.pop(conn, None)

            return None

        data += chunk

    line, remaining = data.split(b"\n", 1)

    with recv_lock:
        recv_buffers[conn] = remaining

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


def send_to_player(player_id, message):
    """
    Send a message to a specific player by ID.
    """

    with lock:
        conn = connections.get(player_id)

    if conn is not None:
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
            "Name": name,
            "Room": "Common",
            "Role": None,
            "Alive": True,
            "Tasks": [],
            "TasksCompleted": 0,
        }

        connections[player_id] = conn

    with game_lock:
        audit_logs[player_id] = []

    return player_id


def remove_player(player_id):

    with lock:

        player = players.pop(player_id, None)
        conn = connections.pop(player_id, None)

    if conn:

        with recv_lock:
            recv_buffers.pop(conn, None)

        try:
            conn.close()
        except OSError:
            pass

    if player:

        broadcast({
            "Type": "Leave",
            "Player": player["Name"],
            "Message": f'{player["Name"]} left the game.'
        })


def find_player_id_by_name(name):

    with lock:

        for player_id, player in players.items():

            if player["Name"] == name:
                return player_id

    return None


def get_alive_players():
    """Return list of (player_id, player_dict) for alive players."""

    with lock:
        return [
            (pid, p) for pid, p in players.items()
            if p["Alive"]
        ]


def get_alive_by_team(team):
    """
    Return alive players by team.
    team = 'good' or 'bad'
    """

    good_roles = {"Process", "Detective", "Antivirus"}
    bad_roles = {"Virus", "Rootkit"}

    target_roles = good_roles if team == "good" else bad_roles

    with lock:
        return [
            (pid, p) for pid, p in players.items()
            if p["Alive"] and p["Role"] in target_roles
        ]


# ============================================================
# ROLE ASSIGNMENT
# ============================================================

def assign_roles():
    """
    Assign roles to all players.

    Distribution:
    4 players: 1 Virus, 1 Rootkit, 1 Detective, 1 Process
    5+ players: 1 Virus, 1 Rootkit, 1 Detective, 1 Antivirus, rest Process
    """

    with lock:
        player_ids = list(players.keys())

    num_players = len(player_ids)
    random.shuffle(player_ids)

    roles = ["Virus", "Rootkit", "Detective"]

    if num_players >= 5:
        roles.append("Antivirus")

    # Fill remaining with Process
    while len(roles) < num_players:
        roles.append("Process")

    # Assign roles
    for i, pid in enumerate(player_ids):

        with lock:
            players[pid]["Role"] = roles[i]

        # Send private role notification
        send_to_player(pid, {
            "Type": "RoleAssign",
            "Role": roles[i],
        })

    print(f"[SERVER] Roles assigned: {dict(zip(player_ids, roles))}")


# ============================================================
# AUDIT LOGGING
# ============================================================

def add_audit_entry(player_id, room):
    """Record a room movement in the player's audit log."""

    entry = {
        "Room": room,
        "Timestamp": datetime.now().strftime("%H:%M:%S"),
    }

    with game_lock:
        if player_id in audit_logs:
            audit_logs[player_id].append(entry)
        else:
            audit_logs[player_id] = [entry]


# ============================================================
# GAME LOGIC
# ============================================================

#function to move rooms
def switch_rooms(player_id, message,name):
    valid_rooms = ["Common","GPU","CyberSec","Web Dev"]
    room=message.get('Message')
    
    if room not in valid_rooms:
        send_message(
            connections[player_id],
            {
                "Type": "Chat","Player":name,
                "Message": "Not a valid room brochacho"
            }
        )
        return 0

    player = players.get(player_id)

    current_room = player["Room"]

    if current_room == room:
        send_message(
            connections[player_id],
            {
                "Type": "Chat",
                "Player": name,
                "Message": f"You are already in {room}."
            }
        )
        return 0

    player["Room"] = room

    # Record audit log entry for successful room change
    add_audit_entry(player_id, room)

    send_message(
        connections[player_id],
        {
            "Type": "Chat",
            "Player": name,
            "Message": f"You moved from {current_room} to {room}."
        }
    )

    return 1


# ============================================================
# TASK MANAGEMENT
# ============================================================

def assign_tasks_for_day():
    """Assign TASKS_PER_DAY random room-tasks to each alive good-team player."""

    good_roles = {"Process", "Detective", "Antivirus"}
    task_rooms = list(ROOM_TASKS.keys())

    with lock:
        alive_good = [
            (pid, p) for pid, p in players.items()
            if p["Alive"] and p["Role"] in good_roles
        ]

    for pid, player in alive_good:

        # Pick TASKS_PER_DAY random rooms (or fewer if not enough rooms)
        num = min(TASKS_PER_DAY, len(task_rooms))
        assigned = random.sample(task_rooms, num)

        with lock:
            players[pid]["Tasks"] = list(assigned)
            players[pid]["TasksCompleted"] = 0

        send_to_player(pid, {
            "Type": "TaskAssign",
            "Tasks": assigned,
        })


def check_all_tasks_complete():
    """Check if all alive good-team players have finished their tasks."""

    good_roles = {"Process", "Detective", "Antivirus"}

    with lock:
        for pid, player in players.items():

            if not player["Alive"]:
                continue

            if player["Role"] not in good_roles:
                continue

            if player["TasksCompleted"] < TASKS_PER_DAY:
                return False

    return True


def handle_task_request(player_id):
    """Handle a player's request to do a task in their current room."""

    with lock:
        player = players.get(player_id)

        if player is None:
            return

        name = player["Name"]

        if not player["Alive"]:
            send_to_player(player_id, {
                "Type": "Error",
                "Message": "You are dead. You cannot do tasks."
            })
            return

        room = player["Room"]
        tasks = player["Tasks"]

    if current_phase != "Day":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You can only do tasks during the day."
        })
        return

    if room not in ROOM_TASKS:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"No task available in {room}."
        })
        return

    if room not in tasks:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"You don't have a task assigned in {room}."
        })
        return

    # Tell client to launch the minigame
    minigame = ROOM_TASKS[room]

    send_to_player(player_id, {
        "Type": "TaskStart",
        "Minigame": minigame,
        "Room": room,
    })


def handle_task_result(player_id, message):
    """Handle the result of a completed minigame."""

    success = message.get("Success", False)
    room = message.get("Room", "")

    with lock:
        player = players.get(player_id)

        if player is None:
            return

        name = player["Name"]

    if not success:
        send_to_player(player_id, {
            "Type": "Chat",
            "Player": "SYSTEM",
            "Message": "Task failed. Try again."
        })
        return

    # Mark task as complete
    with lock:
        if room in player["Tasks"]:
            player["Tasks"].remove(room)
            player["TasksCompleted"] += 1

            send_to_player(player_id, {
                "Type": "TaskComplete",
                "Room": room,
                "Remaining": len(player["Tasks"]),
            })

            print(f"[SERVER] {name} completed task in {room}. "
                  f"{player['TasksCompleted']}/{TASKS_PER_DAY} done.")

    # Check if all tasks are done for early day end
    if check_all_tasks_complete():
        day_early_end.set()


# ============================================================
# NIGHT ACTIONS
# ============================================================

def handle_kill(player_id, message):
    """Handle Virus kill attempt."""

    target_name = message.get("Target", "")

    with lock:
        player = players.get(player_id)

        if player is None:
            return

        name = player["Name"]

    # Validations
    if current_phase != "Night":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You can only kill during the night."
        })
        return

    if player["Role"] != "Virus":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "Only the Virus can kill."
        })
        return

    if not player["Alive"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You are dead."
        })
        return

    with game_lock:
        if night_actions_done["virus_kill"]:
            send_to_player(player_id, {
                "Type": "Error",
                "Message": "You already attempted a kill this night."
            })
            return

    target_id = find_player_id_by_name(target_name)

    if target_id is None:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"Player '{target_name}' not found."
        })
        return

    with lock:
        target = players.get(target_id)

    if target is None or not target["Alive"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"'{target_name}' is already dead."
        })
        return

    # Cannot kill yourself
    if target_id == player_id:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You cannot kill yourself."
        })
        return

    with game_lock:
        night_actions_done["virus_kill"] = True

    # Send the kill minigame prompt to the Virus
    send_to_player(player_id, {
        "Type": "KillMinigame",
        "Target": target_name,
    })


def handle_kill_result(player_id, message):
    """Handle the result of the kill minigame."""

    success = message.get("Success", False)
    target_name = message.get("Target", "")

    with lock:
        player = players.get(player_id)

        if player is None:
            return

    if not success:
        send_to_player(player_id, {
            "Type": "Chat",
            "Player": "SYSTEM",
            "Message": "Kill failed. The target escaped."
        })
        return

    target_id = find_player_id_by_name(target_name)

    if target_id is None:
        return

    with lock:
        target = players.get(target_id)

        if target is None:
            return

        target["Alive"] = False

    with game_lock:
        recently_dead.append(target_id)

    send_to_player(player_id, {
        "Type": "Chat",
        "Player": "SYSTEM",
        "Message": f"You killed {target_name}."
    })

    print(f"[SERVER] {target_name} was killed by {player['Name']}.")

    # Check win condition after death
    winner = check_win_conditions()
    if winner:
        announce_winner(winner)


def handle_inspect(player_id, message):
    """Handle Detective audit inspection."""

    target_name = message.get("Target", "")

    with lock:
        player = players.get(player_id)

        if player is None:
            return

    # Validations
    if current_phase != "Night":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You can only inspect during the night."
        })
        return

    if player["Role"] != "Detective":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "Only the Detective can inspect."
        })
        return

    if not player["Alive"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You are dead."
        })
        return

    with game_lock:
        if night_actions_done["detective_inspect"]:
            send_to_player(player_id, {
                "Type": "Error",
                "Message": "You already inspected someone this night."
            })
            return

    target_id = find_player_id_by_name(target_name)

    if target_id is None:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"Player '{target_name}' not found."
        })
        return

    with game_lock:
        night_actions_done["detective_inspect"] = True
        log = list(audit_logs.get(target_id, []))

    # Send audit log only to the Detective
    send_to_player(player_id, {
        "Type": "AuditLog",
        "Target": target_name,
        "Log": log,
    })


def handle_revive(player_id, message):
    """Handle Antivirus revive."""

    global antivirus_used

    target_name = message.get("Target", "")

    with lock:
        player = players.get(player_id)

        if player is None:
            return

    # Validations
    if current_phase != "Night":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You can only revive during the night."
        })
        return

    if player["Role"] != "Antivirus":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "Only the Antivirus can revive."
        })
        return

    if not player["Alive"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You are dead."
        })
        return

    with game_lock:
        if antivirus_used:
            send_to_player(player_id, {
                "Type": "Error",
                "Message": "You have already used your revive."
            })
            return

        if night_actions_done["antivirus_revive"]:
            send_to_player(player_id, {
                "Type": "Error",
                "Message": "You already revived someone this night."
            })
            return

    target_id = find_player_id_by_name(target_name)

    if target_id is None:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"Player '{target_name}' not found."
        })
        return

    with lock:
        target = players.get(target_id)

    if target is None:
        return

    if target["Alive"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"'{target_name}' is not dead."
        })
        return

    # Revive the target
    with lock:
        target["Alive"] = True

    with game_lock:
        antivirus_used = True
        night_actions_done["antivirus_revive"] = True

        # Remove from recently_dead if they were there
        if target_id in recently_dead:
            recently_dead.remove(target_id)

    send_to_player(player_id, {
        "Type": "Chat",
        "Player": "SYSTEM",
        "Message": f"You revived {target_name}."
    })

    print(f"[SERVER] {target_name} was revived by {player['Name']}.")

    # Check win condition after revival
    winner = check_win_conditions()
    if winner:
        announce_winner(winner)


def handle_tamper(player_id, message):
    """Handle Rootkit tamper request — show the target's audit log."""

    global rootkit_uses_remaining

    target_name = message.get("Target", "")

    with lock:
        player = players.get(player_id)

        if player is None:
            return

    # Validations
    if current_phase != "Night":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You can only tamper during the night."
        })
        return

    if player["Role"] != "Rootkit":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "Only the Rootkit can tamper."
        })
        return

    if not player["Alive"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You are dead."
        })
        return

    with game_lock:
        if rootkit_uses_remaining <= 0:
            send_to_player(player_id, {
                "Type": "Error",
                "Message": "You have no tamper uses remaining."
            })
            return

    target_id = find_player_id_by_name(target_name)

    if target_id is None:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"Player '{target_name}' not found."
        })
        return

    with game_lock:
        log = list(audit_logs.get(target_id, []))

    # Send the target's audit log to the Rootkit for editing
    send_to_player(player_id, {
        "Type": "TamperPrompt",
        "Target": target_name,
        "Log": log,
    })


def handle_tamper_action(player_id, message):
    """Apply the Rootkit's chosen audit modification."""

    global rootkit_uses_remaining

    action = message.get("Action", "")
    target_name = message.get("Target", "")

    target_id = find_player_id_by_name(target_name)

    if target_id is None:
        return

    with game_lock:
        if rootkit_uses_remaining <= 0:
            send_to_player(player_id, {
                "Type": "Error",
                "Message": "No tamper uses remaining."
            })
            return

    if action == "edit":
        index = message.get("Index", 0)
        new_room = message.get("NewRoom", "")

        with game_lock:
            log = audit_logs.get(target_id, [])

            if 0 <= index < len(log):
                old_room = log[index]["Room"]
                log[index]["Room"] = new_room
                rootkit_uses_remaining -= 1

                send_to_player(player_id, {
                    "Type": "Chat",
                    "Player": "SYSTEM",
                    "Message": (
                        f"Tampered: changed entry {index + 1} "
                        f"from '{old_room}' to '{new_room}'. "
                        f"({rootkit_uses_remaining} uses left)"
                    ),
                })
            else:
                send_to_player(player_id, {
                    "Type": "Error",
                    "Message": "Invalid entry index."
                })

    elif action == "add":
        room = message.get("Room", "")
        timestamp = message.get("Timestamp", "")

        with game_lock:
            if target_id not in audit_logs:
                audit_logs[target_id] = []

            audit_logs[target_id].append({
                "Room": room,
                "Timestamp": timestamp,
            })

            rootkit_uses_remaining -= 1

            send_to_player(player_id, {
                "Type": "Chat",
                "Player": "SYSTEM",
                "Message": (
                    f"Tampered: added fake entry '{room}' at {timestamp}. "
                    f"({rootkit_uses_remaining} uses left)"
                ),
            })

    else:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "Invalid tamper action. Use 'edit' or 'add'."
        })


def handle_sabotage(player_id, message):
    """Handle bad-team sabotage."""

    global sabotages_remaining

    target_name = message.get("Target", "")

    with lock:
        player = players.get(player_id)

        if player is None:
            return

    bad_roles = {"Virus", "Rootkit"}

    # Validations
    if current_phase != "Night":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You can only sabotage during the night."
        })
        return

    if player["Role"] not in bad_roles:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "Only the bad team can sabotage."
        })
        return

    if not player["Alive"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You are dead."
        })
        return

    with game_lock:
        if sabotages_remaining <= 0:
            send_to_player(player_id, {
                "Type": "Error",
                "Message": "No sabotage uses remaining."
            })
            return

    target_id = find_player_id_by_name(target_name)

    if target_id is None:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"Player '{target_name}' not found."
        })
        return

    with game_lock:
        sabotages_remaining -= 1
        sabotage_targets.add(target_id)

    send_to_player(player_id, {
        "Type": "Chat",
        "Player": "SYSTEM",
        "Message": (
            f"Sabotage activated on {target_name}. "
            f"Their messages will be jumbled next night. "
            f"({sabotages_remaining} uses left)"
        ),
    })

    print(f"[SERVER] {player['Name']} sabotaged {target_name}.")


# ============================================================
# VOTING
# ============================================================

def handle_vote(player_id, message):
    """Handle a player's vote during the Voting phase."""

    target_name = message.get("Target", "")

    with lock:
        player = players.get(player_id)

        if player is None:
            return

        name = player["Name"]

    if current_phase != "Voting":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "Voting is not active right now."
        })
        return

    if not player["Alive"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "Dead players cannot vote."
        })
        return

    if player_id in votes:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You already voted."
        })
        return

    target_id = find_player_id_by_name(target_name)

    if target_id is None:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"Player '{target_name}' not found."
        })
        return

    with lock:
        target = players.get(target_id)

    if target is None or not target["Alive"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"'{target_name}' is not alive."
        })
        return

    votes[player_id] = target_name

    broadcast({
        "Type": "Chat",
        "Player": "SYSTEM",
        "Message": f"{name} has voted."
    })

    print(f"[SERVER] {name} voted for {target_name}.")

    # Check if all alive players have voted for early end
    alive_players = get_alive_players()

    if len(votes) >= len(alive_players):
        # Signal early voting end by setting an event
        # (the phase manager will handle this)
        pass


def tally_votes():
    """
    Count votes and determine elimination.
    Returns (eliminated_name, eliminated_id) or (None, None) on tie.
    """

    if not votes:
        return None, None

    # Count votes per target
    vote_counts = {}

    for voter_id, target_name in votes.items():
        vote_counts[target_name] = vote_counts.get(target_name, 0) + 1

    # Find maximum votes
    max_votes = max(vote_counts.values())

    # Check for tie
    top_targets = [
        name for name, count in vote_counts.items()
        if count == max_votes
    ]

    if len(top_targets) > 1:
        # Tie — no elimination
        return None, None

    eliminated_name = top_targets[0]
    eliminated_id = find_player_id_by_name(eliminated_name)

    return eliminated_name, eliminated_id


# ============================================================
# WIN CONDITIONS
# ============================================================

def check_win_conditions():
    """
    Check if a team has won.

    Bad team wins: alive bad >= alive good
    Good team wins: all bad are dead

    Returns "Good", "Bad", or None.
    """

    good_alive = get_alive_by_team("good")
    bad_alive = get_alive_by_team("bad")

    if len(bad_alive) == 0:
        return "Good"

    if len(bad_alive) >= len(good_alive):
        return "Bad"

    return None


def announce_winner(winner):
    """Broadcast the game over message."""

    global current_phase

    current_phase = "GameOver"

    if winner == "Good":
        msg = "THE GOOD TEAM WINS! All threats have been eliminated."
    else:
        msg = "THE BAD TEAM WINS! The system has been compromised."

    # Reveal all roles
    role_list = []

    with lock:
        for pid, p in players.items():
            role_list.append(f"  {p['Name']}: {p['Role']}")

    role_text = "\n".join(role_list)

    broadcast({
        "Type": "GameOver",
        "Winner": winner,
        "Message": msg,
        "Roles": role_text,
    })

    print(f"[SERVER] GAME OVER — {winner} team wins!")


# ============================================================
# MESSAGE JUMBLING (SABOTAGE EFFECT)
# ============================================================

def jumble_message(text):
    """Randomly shuffle the characters in a message."""

    chars = list(text)
    random.shuffle(chars)
    return "".join(chars)


# ============================================================
# PHASE MANAGER
# ============================================================

def run_game_loop():
    """
    Server-side game loop. Runs in its own thread.

    Lobby -> Day -> Night -> Discussion -> Voting -> Day -> ...
    """

    global current_phase, day_number, votes
    global night_actions_done, recently_dead, sabotage_targets

    # Wait for game_started signal
    game_started.wait()

    # Small delay so GameStart messages reach clients
    time.sleep(1)

    # Assign roles
    assign_roles()

    time.sleep(1)

    # ========================================================
    # MAIN GAME LOOP
    # ========================================================

    while current_phase != "GameOver":

        # ====================================================
        # DAY PHASE
        # ====================================================

        day_number += 1
        current_phase = "Day"
        day_early_end.clear()

        broadcast({
            "Type": "PhaseChange",
            "Phase": "Day",
            "DayNumber": day_number,
            "Duration": DAY_DURATION,
        })

        # Announce deaths from last night (except first day)
        if day_number > 1:

            with game_lock:
                dead_this_cycle = list(recently_dead)

            for dead_id in dead_this_cycle:
                with lock:
                    dead_player = players.get(dead_id)

                if dead_player:
                    broadcast({
                        "Type": "Death",
                        "Player": dead_player["Name"],
                        "Message": (
                            f"{dead_player['Name']} was found dead. "
                            f"Their process was terminated."
                        ),
                    })

            # Announce revivals
            # (handled within the revive action already)

        # Assign tasks
        assign_tasks_for_day()

        print(f"[SERVER] === DAY {day_number} === ({DAY_DURATION}s)")

        # Wait for day to end (timer or all tasks complete)
        day_early_end.wait(timeout=DAY_DURATION)

        if current_phase == "GameOver":
            break

        # ====================================================
        # NIGHT PHASE
        # ====================================================

        current_phase = "Night"

        # Reset night actions
        with game_lock:
            night_actions_done["virus_kill"] = False
            night_actions_done["detective_inspect"] = False
            night_actions_done["antivirus_revive"] = False

        broadcast({
            "Type": "PhaseChange",
            "Phase": "Night",
            "DayNumber": day_number,
            "Duration": NIGHT_DURATION,
        })

        # Leak dead player audit logs
        with game_lock:
            dead_to_leak = list(recently_dead)

        for dead_id in dead_to_leak:
            with lock:
                dead_player = players.get(dead_id)

            if dead_player:
                with game_lock:
                    log = list(audit_logs.get(dead_id, []))

                broadcast({
                    "Type": "AuditLeak",
                    "Player": dead_player["Name"],
                    "Log": log,
                })

        print(f"[SERVER] === NIGHT {day_number} === ({NIGHT_DURATION}s)")

        time.sleep(NIGHT_DURATION)

        if current_phase == "GameOver":
            break

        # Clear sabotage targets after the night they're active
        with game_lock:
            sabotage_targets.clear()

        # Clear recently dead (they've been leaked)
        with game_lock:
            recently_dead.clear()

        # ====================================================
        # DISCUSSION PHASE
        # ====================================================

        current_phase = "Discussion"

        broadcast({
            "Type": "PhaseChange",
            "Phase": "Discussion",
            "DayNumber": day_number,
            "Duration": DISCUSSION_DURATION,
        })

        print(
            f"[SERVER] === DISCUSSION === ({DISCUSSION_DURATION}s)"
        )

        time.sleep(DISCUSSION_DURATION)

        if current_phase == "GameOver":
            break

        # ====================================================
        # VOTING PHASE
        # ====================================================

        current_phase = "Voting"
        votes = {}

        broadcast({
            "Type": "PhaseChange",
            "Phase": "Voting",
            "DayNumber": day_number,
            "Duration": VOTING_DURATION,
        })

        print(f"[SERVER] === VOTING === ({VOTING_DURATION}s)")

        time.sleep(VOTING_DURATION)

        if current_phase == "GameOver":
            break

        # Tally votes
        eliminated_name, eliminated_id = tally_votes()

        if eliminated_name is not None and eliminated_id is not None:

            with lock:
                target = players.get(eliminated_id)

                if target is not None:
                    target["Alive"] = False

            with game_lock:
                recently_dead.append(eliminated_id)

            broadcast({
                "Type": "VoteResult",
                "Eliminated": eliminated_name,
                "Message": (
                    f"{eliminated_name} has been voted out. "
                    f"Their process was terminated."
                ),
            })

            print(
                f"[SERVER] {eliminated_name} was voted out."
            )

            # Check win condition
            winner = check_win_conditions()

            if winner:
                announce_winner(winner)
                break

        else:
            broadcast({
                "Type": "VoteResult",
                "Eliminated": None,
                "Message": "No consensus reached. Nobody was eliminated.",
            })

            print("[SERVER] No elimination — tied vote.")

    print("[SERVER] Game loop ended.")


# ============================================================
# PROCESS MESSAGE (SERVER-SIDE)
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

        # Dead players' messages are not broadcast
        if not player["Alive"]:
            send_to_player(player_id, {
                "Type": "Chat",
                "Player": "SYSTEM",
                "Message": "You are dead. Your messages are not broadcast."
            })
            return

        chat_text = message.get("Message", "")

        # Apply sabotage jumbling if active
        with game_lock:
            if player_id in sabotage_targets and current_phase == "Night":
                chat_text = jumble_message(chat_text)

        broadcast({
            "Type": "Chat",
            "Player": name,
            "Message": chat_text,
        })


    # --------------------------------------------------------
    # WHISPER
    # --------------------------------------------------------

    elif message_type == "Whisper":

        target_name = message.get("Player")

        target_id = find_player_id_by_name(target_name)

        if target_id is None:

            # Tell the sender that the player doesn't exist
            with lock:
                sender_conn = connections.get(player_id)

            if sender_conn is not None:

                send_message(
                    sender_conn,
                    {
                        "Type": "Error",
                        "Message": f"Player '{target_name}' doesn't exist."
                    }
                )

            return


        # Get the TARGET player's socket
        with lock:
            target_conn = connections.get(target_id)


        if target_conn is not None:

            send_message(
                target_conn,
                {
                    "Type": "Whisper",
                    "Player": name,
                    "Message": message.get("Message", "")
                }
            )


        # Also tell sender that whisper was sent
        with lock:
            sender_conn = connections.get(player_id)

        if sender_conn is not None:

            send_message(
                sender_conn,
                {
                    "Type": "WhisperSent",
                    "Player": target_name,
                    "Message": message.get("Message", "")
                }
            )


    # --------------------------------------------------------
    # VOTE
    # --------------------------------------------------------

    elif message_type == "Vote":

        handle_vote(player_id, message)


    # --------------------------------------------------------
    # Room Movement
    # --------------------------------------------------------

    elif message_type == "Move":
        switch_rooms(player_id, message, name)


    # --------------------------------------------------------
    # TASK REQUEST
    # --------------------------------------------------------

    elif message_type == "TaskRequest":
        handle_task_request(player_id)


    # --------------------------------------------------------
    # TASK RESULT
    # --------------------------------------------------------

    elif message_type == "TaskResult":
        handle_task_result(player_id, message)


    # --------------------------------------------------------
    # KILL
    # --------------------------------------------------------

    elif message_type == "Kill":
        handle_kill(player_id, message)


    # --------------------------------------------------------
    # KILL RESULT
    # --------------------------------------------------------

    elif message_type == "KillResult":
        handle_kill_result(player_id, message)


    # --------------------------------------------------------
    # INSPECT (Detective)
    # --------------------------------------------------------

    elif message_type == "Inspect":
        handle_inspect(player_id, message)


    # --------------------------------------------------------
    # REVIVE (Antivirus)
    # --------------------------------------------------------

    elif message_type == "Revive":
        handle_revive(player_id, message)


    # --------------------------------------------------------
    # TAMPER (Rootkit)
    # --------------------------------------------------------

    elif message_type == "Tamper":
        handle_tamper(player_id, message)


    # --------------------------------------------------------
    # TAMPER ACTION (Rootkit follow-up)
    # --------------------------------------------------------

    elif message_type == "TamperAction":
        handle_tamper_action(player_id, message)


    # --------------------------------------------------------
    # SABOTAGE (Bad team)
    # --------------------------------------------------------

    elif message_type == "Sabotage":
        handle_sabotage(player_id, message)


    # --------------------------------------------------------
    # VIEW TASKS
    # --------------------------------------------------------

    elif message_type == "ViewTasks":

        with lock:
            tasks = list(player.get("Tasks", []))
            completed = player.get("TasksCompleted", 0)

        send_to_player(player_id, {
            "Type": "TaskList",
            "Tasks": tasks,
            "Completed": completed,
            "Required": TASKS_PER_DAY,
        })


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

        player_id = add_player(
            conn,
            name
        )


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

        print(
            f"{name} is waiting for the game to start..."
        )

        game_started.wait()


        # ====================================================
        # GAME HAS STARTED
        # ====================================================

        print(
            f"{name} is now listening for game messages."
        )


        # Tell this player that the game started
        send_message(
            conn,
            {
                "Type": "GameStart"
            }
        )


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


    except (
        ConnectionError,
        json.JSONDecodeError,
        KeyError
    ) as e:

        print(
            f"Connection error with {addr}:",
            e
        )


    finally:

        if player_id is not None:

            remove_player(
                player_id
            )


# ============================================================
# ACCEPT PLAYERS
# ============================================================

def accept_players(server, max_players):

    while True:

        try:

            conn, addr = server.accept()


            # -----------------------------------------------
            # Check player limit
            # -----------------------------------------------

            with lock:
                current_players = len(players)


            if current_players >= max_players:

                print(
                    f"Rejected connection from {addr}: "
                    "game is full."
                )

                send_message(
                    conn,
                    {
                        "Type": "Error",
                        "Message": "Game is full."
                    }
                )

                conn.close()

                continue


            # -----------------------------------------------
            # Start player thread
            # -----------------------------------------------

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

                # Send TCP port back to client
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

    game_id = input(
        "Enter Game ID: "
    ).strip()


    max_players = int(
        input(
            "Enter maximum players: "
        )
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


    server.listen(
        max_players
    )


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
        args=(server, max_players),
        daemon=True
    ).start()


    # ========================================================
    # DISPLAY LOBBY
    # ========================================================

    print()
    print("================================")
    print("             LOBBY")
    print("================================")
    print(
        "Game ID:",
        game_id
    )
    print(
        "Maximum players:",
        max_players
    )
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


    # ========================================================
    # HOST RECEIVES SERVER MESSAGES
    # ========================================================

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


            # Start the game loop thread
            threading.Thread(
                target=run_game_loop,
                daemon=True
            ).start()

            # Releases every handle_player()
            # currently waiting.
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


    # ========================================================
    # BROADCAST GAME ID
    # ========================================================

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


    # ========================================================
    # GET HOST ADDRESS
    # ========================================================

    # The IP address comes from the UDP sender
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
                message,
                conn
            )


        except (
            ConnectionError,
            json.JSONDecodeError
        ):

            break


# ============================================================
# HANDLE SERVER MESSAGE (CLIENT-SIDE)
# ============================================================

def handle_server_message(message, conn=None):

    global client_role, client_alive

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
    # ROLE ASSIGNMENT
    # --------------------------------------------------------

    elif message_type == "RoleAssign":

        client_role = message["Role"]

        print()
        print("==============================")
        print(f"  YOUR ROLE: {client_role}")
        print("==============================")

        if client_role == "Process":
            print("  You are a normal process.")
            print("  Complete tasks and find the threats.")
        elif client_role == "Detective":
            print("  You can inspect audit logs at night.")
            print("  Use /inspect <name> during night.")
        elif client_role == "Antivirus":
            print("  You can revive ONE dead player.")
            print("  Use /revive <name> during night.")
        elif client_role == "Virus":
            print("  You are the killer.")
            print("  Use /kill <name> during night.")
        elif client_role == "Rootkit":
            print("  You can tamper with audit logs.")
            print("  Use /tamper <name> during night.")

        print("==============================")
        print()


    # --------------------------------------------------------
    # PHASE CHANGE
    # --------------------------------------------------------

    elif message_type == "PhaseChange":

        phase = message["Phase"]
        day_num = message.get("DayNumber", "")
        duration = message.get("Duration", "")

        print()
        print("=" * 40)

        if phase == "Day":
            print(f"  ☀  DAY {day_num}  ({duration}s)")
            print("  Complete your tasks!")
        elif phase == "Night":
            print(f"  🌙 NIGHT {day_num}  ({duration}s)")
            print("  Use your night abilities.")
        elif phase == "Discussion":
            print(f"  💬 DISCUSSION  ({duration}s)")
            print("  Discuss who the threats are.")
        elif phase == "Voting":
            print(f"  🗳  VOTING  ({duration}s)")
            print("  Use /vote <name> to vote.")

        print("=" * 40)
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
    # WHISPER RECEIVED
    # --------------------------------------------------------

    elif message_type == "Whisper":

        print(
            f'[Whisper from {message["Player"]}] '
            f'{message["Message"]}'
        )


    # --------------------------------------------------------
    # WHISPER SENT
    # --------------------------------------------------------

    elif message_type == "WhisperSent":

        print(
            f'[Whisper to {message["Player"]}] '
            f'{message["Message"]}'
        )


    # --------------------------------------------------------
    # ERROR
    # --------------------------------------------------------

    elif message_type == "Error":

        print(
            f'[ERROR] {message["Message"]}'
        )


    # --------------------------------------------------------
    # TASK ASSIGNMENT
    # --------------------------------------------------------

    elif message_type == "TaskAssign":

        tasks = message.get("Tasks", [])

        print()
        print("--- TASKS ASSIGNED ---")

        for i, room in enumerate(tasks, 1):
            print(f"  {i}. Go to {room} and complete the task")

        print("  Use /move to go to a room, then /task")
        print("----------------------")
        print()


    # --------------------------------------------------------
    # TASK START (launch minigame)
    # --------------------------------------------------------

    elif message_type == "TaskStart":

        minigame = message.get("Minigame", "")
        room = message.get("Room", "")
        success = False

        print(f"\n--- Starting task: {minigame} ---\n")

        try:
            if minigame == "memory":
                from memory import play_memory
                success = play_memory()

            elif minigame == "hangman":
                from hangman import play_hangman
                success = play_hangman()

            elif minigame == "wordle":
                from wordle import play_wordle
                success = play_wordle()

            else:
                print(f"Unknown minigame: {minigame}")

        except Exception as e:
            print(f"Minigame error: {e}")
            success = False

        # Send result back to server
        if conn is not None:
            send_message(conn, {
                "Type": "TaskResult",
                "Room": room,
                "Success": success,
            })


    # --------------------------------------------------------
    # TASK COMPLETE
    # --------------------------------------------------------

    elif message_type == "TaskComplete":

        room = message.get("Room", "")
        remaining = message.get("Remaining", 0)

        print(f"\n✓ Task in {room} completed!")

        if remaining > 0:
            print(f"  {remaining} task(s) remaining.")
        else:
            print("  All tasks complete!")

        print()


    # --------------------------------------------------------
    # TASK LIST
    # --------------------------------------------------------

    elif message_type == "TaskList":

        tasks = message.get("Tasks", [])
        completed = message.get("Completed", 0)
        required = message.get("Required", TASKS_PER_DAY)

        print()
        print(f"--- TASKS ({completed}/{required} done) ---")

        if tasks:
            for i, room in enumerate(tasks, 1):
                print(f"  {i}. {room} (pending)")
        else:
            print("  All tasks complete!")

        print("----------------------------")
        print()


    # --------------------------------------------------------
    # KILL MINIGAME
    # --------------------------------------------------------

    elif message_type == "KillMinigame":

        target_name = message.get("Target", "")

        print(f"\n--- Attempting to kill {target_name} ---\n")

        success = False

        try:
            from kill_minigame import kill_minigame
            success = kill_minigame()
        except Exception as e:
            print(f"Kill minigame error: {e}")
            success = False

        # Send result back to server
        if conn is not None:
            send_message(conn, {
                "Type": "KillResult",
                "Target": target_name,
                "Success": success,
            })


    # --------------------------------------------------------
    # DEATH ANNOUNCEMENT
    # --------------------------------------------------------

    elif message_type == "Death":

        player_name = message.get("Player", "")
        msg = message.get("Message", "")

        print()
        print("☠" * 20)
        print(f"  {msg}")
        print("☠" * 20)
        print()


    # --------------------------------------------------------
    # AUDIT LOG (Detective inspection result)
    # --------------------------------------------------------

    elif message_type == "AuditLog":

        target = message.get("Target", "")
        log = message.get("Log", [])

        print()
        print(f"--- AUDIT LOG: {target} ---")

        if log:
            for entry in log:
                print(
                    f"  {entry['Timestamp']}  {entry['Room']}"
                )
        else:
            print("  No entries.")

        print("---------------------------")
        print()


    # --------------------------------------------------------
    # AUDIT LEAK (dead player's log)
    # --------------------------------------------------------

    elif message_type == "AuditLeak":

        player_name = message.get("Player", "")
        log = message.get("Log", [])

        print()
        print(f"--- AUDIT LEAK: {player_name} (deceased) ---")

        if log:
            for entry in log:
                print(
                    f"  {entry['Timestamp']}  {entry['Room']}"
                )
        else:
            print("  No entries.")

        print("--------------------------------------------")
        print()


    # --------------------------------------------------------
    # TAMPER PROMPT (Rootkit sees target's audit log)
    # --------------------------------------------------------

    elif message_type == "TamperPrompt":

        target = message.get("Target", "")
        log = message.get("Log", [])

        print()
        print(f"--- TAMPER: {target}'s audit log ---")

        if log:
            for i, entry in enumerate(log):
                print(
                    f"  [{i + 1}] {entry['Timestamp']}  "
                    f"{entry['Room']}"
                )
        else:
            print("  No entries.")

        print()
        print("Actions:")
        print("  edit <index> <new_room>  — change an entry")
        print("  add <room> <HH:MM:SS>   — add a fake entry")
        print()

        action_input = input("Tamper action> ").strip()

        parts = action_input.split()

        if conn is not None:

            if len(parts) >= 3 and parts[0] == "edit":
                try:
                    index = int(parts[1]) - 1
                    new_room = " ".join(parts[2:])

                    send_message(conn, {
                        "Type": "TamperAction",
                        "Action": "edit",
                        "Target": target,
                        "Index": index,
                        "NewRoom": new_room,
                    })

                except ValueError:
                    print("Invalid index.")

            elif len(parts) >= 3 and parts[0] == "add":
                room = parts[1]
                timestamp = parts[2]

                send_message(conn, {
                    "Type": "TamperAction",
                    "Action": "add",
                    "Target": target,
                    "Room": room,
                    "Timestamp": timestamp,
                })

            else:
                print("Invalid tamper action.")


    # --------------------------------------------------------
    # VOTE RESULT
    # --------------------------------------------------------

    elif message_type == "VoteResult":

        eliminated = message.get("Eliminated")
        msg = message.get("Message", "")

        print()

        if eliminated:
            print("🗳" * 20)
            print(f"  {msg}")
            print("🗳" * 20)
        else:
            print(f"  {msg}")

        print()


    # --------------------------------------------------------
    # GAME OVER
    # --------------------------------------------------------

    elif message_type == "GameOver":

        winner = message.get("Winner", "")
        msg = message.get("Message", "")
        roles = message.get("Roles", "")

        print()
        print("=" * 50)
        print("  GAME OVER")
        print(f"  {msg}")
        print()
        print("  --- Role Reveal ---")
        print(roles)
        print("=" * 50)
        print()


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


# ============================================================
# SEND WHISPER
# ============================================================

def send_whisper(conn, message, target_name):

    send_message(
        conn,
        {
            "Type": "Whisper",
            "Message": message,
            "Player": target_name
        }
    )

def send_move_message(conn,message):
    send_message(   
            conn,
            {
                "Type": "Move",
                "Message": message
            }
        )

# ============================================================
# CLIENT GAME LOOP
# ============================================================

def client_game_loop(conn):

    while True:

        message = input("> ")


        # ====================================================
        # QUIT
        # ====================================================

        if message == "/quit":

            break


        # ====================================================
        # IGNORE EMPTY MESSAGE
        # ====================================================

        if message.strip() == "":
            continue


        # ====================================================
        # VOTE
        # ====================================================

        if message.startswith("/vote"):

            parts = message.split(maxsplit=1)

            if len(parts) < 2:
                target_name = input(
                    "Enter player name to vote for: "
                ).strip()
            else:
                target_name = parts[1].strip()

            send_message(conn, {
                "Type": "Vote",
                "Target": target_name,
            })


        # ====================================================
        # WHISPER
        # ====================================================

        elif message == "/whisper":
            target_name = input(
                "Enter the player name: "
            ).strip()

            whisper_message = input(
                f">(Whisper to {target_name}) "
            )

            send_whisper(
                conn,
                whisper_message,
                target_name
            )

        # ====================================================
        # ROOM MOVEMENT
        # ====================================================

        elif message == "/move":
            room_name = input(
                "Enter the folder you want to move into: "
            ).strip()

            send_move_message(
                conn,
                room_name
            )


        # ====================================================
        # TASK
        # ====================================================

        elif message == "/task":

            send_message(conn, {
                "Type": "TaskRequest",
            })


        # ====================================================
        # VIEW TASKS
        # ====================================================

        elif message == "/tasks":

            send_message(conn, {
                "Type": "ViewTasks",
            })


        # ====================================================
        # KILL (Virus)
        # ====================================================

        elif message.startswith("/kill"):

            parts = message.split(maxsplit=1)

            if len(parts) < 2:
                target_name = input(
                    "Enter target name: "
                ).strip()
            else:
                target_name = parts[1].strip()

            send_message(conn, {
                "Type": "Kill",
                "Target": target_name,
            })


        # ====================================================
        # INSPECT (Detective)
        # ====================================================

        elif message.startswith("/inspect"):

            parts = message.split(maxsplit=1)

            if len(parts) < 2:
                target_name = input(
                    "Enter player name to inspect: "
                ).strip()
            else:
                target_name = parts[1].strip()

            send_message(conn, {
                "Type": "Inspect",
                "Target": target_name,
            })


        # ====================================================
        # REVIVE (Antivirus)
        # ====================================================

        elif message.startswith("/revive"):

            parts = message.split(maxsplit=1)

            if len(parts) < 2:
                target_name = input(
                    "Enter player name to revive: "
                ).strip()
            else:
                target_name = parts[1].strip()

            send_message(conn, {
                "Type": "Revive",
                "Target": target_name,
            })


        # ====================================================
        # TAMPER (Rootkit)
        # ====================================================

        elif message.startswith("/tamper"):

            parts = message.split(maxsplit=1)

            if len(parts) < 2:
                target_name = input(
                    "Enter player name to tamper: "
                ).strip()
            else:
                target_name = parts[1].strip()

            send_message(conn, {
                "Type": "Tamper",
                "Target": target_name,
            })


        # ====================================================
        # SABOTAGE (Bad team)
        # ====================================================

        elif message.startswith("/sabotage"):

            parts = message.split(maxsplit=1)

            if len(parts) < 2:
                target_name = input(
                    "Enter player name to sabotage: "
                ).strip()
            else:
                target_name = parts[1].strip()

            send_message(conn, {
                "Type": "Sabotage",
                "Target": target_name,
            })


        # ====================================================
        # HELP
        # ====================================================

        elif message == "/help":

            print()
            print("=== COMMANDS ===")
            print("  /move          — Move to a room")
            print("  /task          — Do your task in current room")
            print("  /tasks         — View your assigned tasks")
            print("  /whisper       — Send a private message")
            print("  /vote <name>   — Vote to eliminate a player")
            print("  /kill <name>   — [Virus] Kill a player (night)")
            print("  /inspect <name>— [Detective] Inspect audit (night)")
            print("  /revive <name> — [Antivirus] Revive a player (night)")
            print("  /tamper <name> — [Rootkit] Tamper audit log (night)")
            print("  /sabotage <name> — [Bad team] Jumble messages (night)")
            print("  /help          — Show this help")
            print("  /quit          — Leave the game")
            print("================")
            print()


        # ====================================================
        # NORMAL CHAT
        # ====================================================

        else:

            send_chat(
                conn,
                message
            )


    try:
        conn.close()
    except OSError:
        pass


# ============================================================
# CLIENT GAME
# ============================================================

def client_game(conn, name):

    global client_conn

    client_conn = conn

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
    # CLIENT INPUT
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

        host_game(
            name
        )


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
