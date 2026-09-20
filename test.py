import socket
import threading
import json
import random
import time
import subprocess
import select
import sys
import os
import shutil
from datetime import datetime


# ============================================================
# CONFIG
# ============================================================

TCP_PORT = 5050
DISCOVERY_PORT = 5051


# ============================================================
# GAME CONFIG
# ============================================================

DAY_DURATION = 120        # seconds
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

# Day action tracking (reset each day)
night_actions_done = {
    "detective_inspect": False,
    "antivirus_revive": False,
}

last_kill_time = 0

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

# Active minigame subprocess
active_minigame_process = None

class ClientState:
    def __init__(self):
        self.phase = "Waiting"
        self.day_number = 0
        self.duration = 0
        self.phase_start_time = 0
        
        # Room info
        self.current_room = "Common"
        self.room_players = []
        
        # Players dict: Name -> {"Alive": bool}
        self.players = {}
        
        # Chat history (list of formatted strings)
        self.chat_history = []
        
        # Tasks: List of dicts e.g. {"Name": "TaskName", "Completed": bool}
        self.tasks = []
        self.tasks_required = 0
        self.tasks_completed = 0
        
        # Audit log (if Detective)
        self.audit_logs = []
        
        self.game_over = False
        self.game_over_msg = ""
        self.game_over_roles = ""
        self.last_HUD_draw = 0
        self.needs_redraw = True

client_state = ClientState()
my_client_name = ""


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

    roles = ["Virus", "Detective"]

    if num_players > 5:
        roles.extend(["Rootkit", "Antivirus"])

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
    
    # Notify client of their new room explicitly
    send_message(
        connections[player_id],
        {
            "Type": "RoomChange",
            "Room": room
        }
    )
    
    with lock:
        players_here = [
            p["Name"] for p in players.values()
            if p["Alive"] and p["Room"] == room and p["Name"] != name
        ]
        
    players_str = ", ".join(players_here) if players_here else "Nobody else is here."

    send_message(
        connections[player_id],
        {
            "Type": "Chat",
            "Player": "SYSTEM",
            "Message": f"You moved from {current_room} to {room}.\n  Players here: {players_str}"
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
    if current_phase != "Day":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You can only kill during the day."
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

    global last_kill_time
    with game_lock:
        if time.time() - last_kill_time < 30:
            send_to_player(player_id, {
                "Type": "Error",
                "Message": f"Kill is on cooldown. Wait {int(30 - (time.time() - last_kill_time))} seconds."
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

    if target["Role"] in ["Rootkit", "Virus"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"You cannot kill your fellow bad team member, {target_name}!"
        })
        return

    if target["Room"] != player["Room"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"'{target_name}' is not in your room."
        })
        return


    with game_lock:
        last_kill_time = time.time()

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

        if target["Room"] != player["Room"]:
            send_to_player(player_id, {
                "Type": "Chat",
                "Player": "SYSTEM",
                "Message": f"Kill failed. '{target_name}' left the room!"
            })
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
    if current_phase != "Day":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You can only inspect during the day."
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

    with lock:
        target = players.get(target_id)
        
    if target is None:
        return
        
    if target["Room"] != player["Room"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"'{target_name}' is not in your room."
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
    if current_phase != "Day":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You can only revive during the day."
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

    if target["Room"] != player["Room"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"'{target_name}' is not in your room."
        })
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
    if current_phase != "Day":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You can only tamper during the day."
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

    with lock:
        target = players.get(target_id)
        
    if target is None:
        return
        
    if target["Room"] != player["Room"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"'{target_name}' is not in your room."
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

    if current_phase != "Day":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You can only tamper during the day."
        })
        return

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
    if current_phase != "Day":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You can only sabotage during the day."
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

    with lock:
        target = players.get(target_id)
        
    if target is None:
        return
        
    if target["Room"] != player["Room"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": f"'{target_name}' is not in your room."
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

    Lobby -> Day -> Discussion -> Voting -> Day -> ...
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

        # Reset day actions
        with game_lock:
            night_actions_done["virus_kill"] = False
            night_actions_done["detective_inspect"] = False
            night_actions_done["antivirus_revive"] = False
        day_number += 1
        current_phase = "Day"
        day_early_end.clear()

        broadcast({
            "Type": "PhaseChange",
            "Phase": "Day",
            "DayNumber": day_number,
            "Duration": DAY_DURATION,
        })

        # Reset daily actions
        with game_lock:
            night_actions_done["detective_inspect"] = False
            night_actions_done["antivirus_revive"] = False

        # Assign tasks
        assign_tasks_for_day()

        print(f"[SERVER] === DAY {day_number} === ({DAY_DURATION}s)")

        # Wait for day to end (timer or all tasks complete)
        day_early_end.wait(timeout=DAY_DURATION)

        if current_phase == "GameOver":
            break

        # ====================================================
        # DISCUSSION PHASE
        # ====================================================

        current_phase = "Discussion"
        
        # Announce deaths and leak audits from the Day
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
                # Leak dead player audit logs
                with game_lock:
                    log = list(audit_logs.get(dead_id, []))

                broadcast({
                    "Type": "AuditLeak",
                    "Player": dead_player["Name"],
                    "Log": log,
                })
                
        # Clear sabotage targets and recently dead
        with game_lock:
            sabotage_targets.clear()
            recently_dead.clear()

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

        if current_phase == "Day":
            send_to_player(player_id, {
                "Type": "Error",
                "Message": "Public chat is disabled during the Day phase. Focus on tasks!"
            })
            return

        chat_text = message.get("Message", "")

        # Apply sabotage jumbling if active
        with game_lock:
            if player_id in sabotage_targets and current_phase == "Day":
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


    # --------------------------------------------------------
    # LS (Current Room and Players)
    # --------------------------------------------------------

    elif message_type == "Ls":

        room = player["Room"]
        with lock:
            players_here = [
                p["Name"] for p in players.values()
                if p["Alive"] and p["Room"] == room and p["Name"] != name
            ]

        players_str = ", ".join(players_here) if players_here else "Nobody else is here."

        send_to_player(player_id, {
            "Type": "Chat",
            "Player": "SYSTEM",
            "Message": (
                f"\n--- ROOM INFO ---\n"
                f"  You are in: {room}\n"
                f"  Other players here: {players_str}\n"
                f"  Available rooms: Common, GPU, CyberSec, Web Dev\n"
                f"-----------------"
            )
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

        with lock:
            player_list = {p_id: {"Alive": players[p_id]["Alive"]} for p_id in players}
            my_room = players[name]["Room"] if name in players else "Common"
        
        send_message(
            conn,
            {
                "Type": "PlayerList",
                "Players": player_list
            }
        )
        send_message(
            conn,
            {
                "Type": "RoomChange",
                "Room": my_room
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


import re

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def format_color(text, color_code):
    return f"\033[{color_code}m{text}\033[0m"

def add_chat(msg_type, text, player="", color="0"):
    client_state.chat_history.append({"type": msg_type, "text": text, "player": player, "color": color})
    if len(client_state.chat_history) > 10:
        client_state.chat_history.pop(0)

def draw_hud():
    global active_minigame_process, client_role, client_alive
    if active_minigame_process is not None:
        return # Do not draw HUD while minigame is active

    cols, rows = shutil.get_terminal_size((80, 24))
    
    # Calculate time remaining
    time_remaining_str = ""
    if client_state.phase != "Waiting" and not client_state.game_over and client_state.duration > 0:
        elapsed = int(time.time() - client_state.phase_start_time)
        rem = max(0, client_state.duration - elapsed)
        mins, secs = divmod(rem, 60)
        time_remaining_str = f"{mins:02}:{secs:02}"
    
    # Clear screen and move cursor to top-left
    sys.stdout.write("\033[2J\033[H")
    
    # Header
    sys.stdout.write(format_color("╔" + "═" * (cols - 2) + "╗\n", "36"))
    title = " PROCESS MAFIA "
    if client_state.game_over:
        title = " GAME OVER "
    padding = (cols - 2 - len(title)) // 2
    sys.stdout.write(format_color("║", "36") + " " * padding + format_color(title, "1;36") + " " * (cols - 2 - padding - len(title)) + format_color("║\n", "36"))
    sys.stdout.write(format_color("╠" + "═" * (cols - 2) + "╣\n", "36"))
    
    # Phase & Timer
    phase_str = f" {client_state.phase.upper()} "
    if client_state.day_number > 0:
        phase_str += f"{client_state.day_number} "
    
    role_str = f" ROLE: {client_role if client_role else 'UNKNOWN'} "
    alive_str = " ALIVE " if client_alive else " DEAD "
    alive_colored = format_color(alive_str, "1;32") if client_alive else format_color(alive_str, "1;31")
    
    room_str = f" ROOM: {client_state.current_room} "
    
    # Line 1: Phase & Time
    l1_left = format_color(phase_str, "1;33")
    l1_right = format_color(time_remaining_str + " ", "1")
    space1 = max(0, cols - 2 - len(phase_str) - len(time_remaining_str + " "))
    sys.stdout.write(format_color("║", "36") + l1_left + " " * space1 + l1_right + format_color("║\n", "36"))
    
    # Line 2: Role & Alive
    space2 = max(0, cols - 2 - len(role_str) - len(alive_str))
    sys.stdout.write(format_color("║", "36") + role_str + " " * space2 + alive_colored + format_color("║\n", "36"))
    
    # Line 3: Room
    space3 = max(0, cols - 2 - len(room_str))
    sys.stdout.write(format_color("║", "36") + format_color(room_str, "36") + " " * space3 + format_color("║\n", "36"))
    
    sys.stdout.write(format_color("╠" + "═" * (cols - 2) + "╣\n", "36"))
    
    # Tasks Section
    sys.stdout.write(format_color("║", "36") + format_color(" TASKS", "1") + " " * (cols - 8) + format_color("║\n", "36"))
    for t in client_state.tasks:
        icon = format_color("✓", "32") if t["Completed"] else format_color("○", "33")
        t_str = f"   {icon} {t['Name']} "
        clean_len = 5 + len(t['Name']) # 3 spaces + 1 icon + 1 space + name + 1 space
        sys.stdout.write(format_color("║", "36") + t_str + " " * (cols - 2 - clean_len) + format_color("║\n", "36"))
    if not client_state.tasks:
        sys.stdout.write(format_color("║", "36") + "   No tasks assigned. ".ljust(cols - 2) + format_color("║\n", "36"))
    
    # Players Section
    sys.stdout.write(format_color("║", "36") + " " * (cols - 2) + format_color("║\n", "36"))
    sys.stdout.write(format_color("║", "36") + format_color(" PLAYERS", "1") + " " * (cols - 10) + format_color("║\n", "36"))
    
    player_names = list(client_state.players.keys())
    for i in range(0, len(player_names), 2):
        p1 = player_names[i]
        p1_alive = client_state.players[p1]["Alive"]
        p1_icon = format_color("●", "32") if p1_alive else format_color("✕", "31")
        p1_room_str = client_state.current_room if p1 in client_state.room_players or p1 == my_client_name else "???"
        if not p1_alive: p1_room_str = "GRAVEYARD"
        
        p1_str = f"   {p1_icon} {p1[:10].ljust(12)} {p1_room_str[:10].ljust(10)}"
        p1_clean_len = 3 + 1 + 1 + 12 + 1 + 10
        
        if i + 1 < len(player_names):
            p2 = player_names[i+1]
            p2_alive = client_state.players[p2]["Alive"]
            p2_icon = format_color("●", "32") if p2_alive else format_color("✕", "31")
            p2_room_str = client_state.current_room if p2 in client_state.room_players or p2 == my_client_name else "???"
            if not p2_alive: p2_room_str = "GRAVEYARD"
            p2_str = f"   {p2_icon} {p2[:10].ljust(12)} {p2_room_str[:10].ljust(10)}"
            p2_clean_len = 3 + 1 + 1 + 12 + 1 + 10
        else:
            p2_str = ""
            p2_clean_len = 0
            
        space_len = max(0, cols - 2 - p1_clean_len - p2_clean_len)
        sys.stdout.write(format_color("║", "36") + p1_str + p2_str + " " * space_len + format_color("║\n", "36"))

    sys.stdout.write(format_color("╠" + "═" * (cols - 2) + "╣\n", "36"))
    
    # Chat section
    sys.stdout.write(format_color("║", "36") + format_color(" CHAT & EVENTS", "1") + " " * (cols - 16) + format_color("║\n", "36"))
    
    for msg in client_state.chat_history:
        color = msg["color"]
        if msg["type"] == "chat":
            prefix = f"{msg['player'][:8]} › "
            text = msg['text']
        elif msg["type"] == "whisper":
            prefix = f"[WHISPER] {msg['player'][:8]} › "
            text = msg['text']
            color = "35"
        else:
            prefix = " * "
            text = msg['text']
            
        full_clean = prefix + text
        if len(full_clean) > cols - 6:
            full_clean = full_clean[:cols-9] + "..."
            text = full_clean[len(prefix):]
            
        formatted = format_color(prefix, "1;" + color) + format_color(text, color)
        space = max(0, cols - 2 - len(full_clean) - 3)
        sys.stdout.write(format_color("║", "36") + f"   {formatted}" + " " * space + format_color("║\n", "36"))
        
    # Fill remaining rows if needed
    used_rows = 15 + len(client_state.chat_history) + len(client_state.tasks) + (len(player_names) + 1) // 2
    for _ in range(max(0, rows - used_rows - 3)):
        sys.stdout.write(format_color("║", "36") + " " * (cols - 2) + format_color("║\n", "36"))

    sys.stdout.write(format_color("╚" + "═" * (cols - 2) + "╝\n", "36"))
    
    if client_state.game_over:
        sys.stdout.write(format_color(client_state.game_over_msg + "\n", "1;31"))
        sys.stdout.write(format_color(client_state.game_over_roles + "\n", "1;33"))

    # Print input prompt
    sys.stdout.write("\rProcessMafia> ")
    sys.stdout.flush()

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
            
            if client_state.needs_redraw:
                draw_hud()
                client_state.needs_redraw = False


        except (
            ConnectionError,
            json.JSONDecodeError
        ):

            break


# ============================================================
# HANDLE SERVER MESSAGE (CLIENT-SIDE)
# ============================================================

def handle_server_message(message, conn=None):
    global client_role, client_alive, active_minigame_process
    
    message_type = message.get("Type")
    
    if message_type == "Join":
        name = message.get("Player", "")
        if name:
            client_state.players[name] = {"Alive": True}
        add_chat("system", message["Message"], color="32")
        client_state.needs_redraw = True

    elif message_type == "Leave":
        name = message.get("Player", "")
        if name and name in client_state.players:
            del client_state.players[name]
        add_chat("system", message["Message"], color="33")
        client_state.needs_redraw = True

    elif message_type == "GameStart":
        add_chat("system", "GAME HAS STARTED", color="1;36")
        client_state.needs_redraw = True

    elif message_type == "RoleAssign":
        client_role = message["Role"]
        add_chat("system", f"YOUR ROLE: {client_role}", color="1;35")
        if client_role == "Process":
            add_chat("system", "Complete tasks and find the threats.", color="35")
        elif client_role == "Detective":
            add_chat("system", "You can inspect audit logs during the day.", color="35")
        elif client_role == "Antivirus":
            add_chat("system", "You can revive ONE dead player.", color="35")
        elif client_role == "Virus":
            add_chat("system", "You are the killer.", color="35")
        elif client_role == "Rootkit":
            add_chat("system", "You can tamper with audit logs.", color="35")
        client_state.needs_redraw = True

    elif message_type == "PhaseChange":
        client_state.phase = message["Phase"]
        client_state.day_number = message.get("DayNumber", 0)
        client_state.duration = message.get("Duration", 0)
        client_state.phase_start_time = time.time()
        
        if active_minigame_process is not None:
            try:
                active_minigame_process.terminate()
            except Exception:
                pass
            add_chat("error", "The phase has ended. Minigame interrupted!", color="31")
            active_minigame_process = None
            
        phase_msg = f"{client_state.phase.upper()} {client_state.day_number} STARTED"
        add_chat("system", phase_msg, color="1;33")
        client_state.needs_redraw = True

    elif message_type == "Chat":
        p = message.get("Player", "")
        if p == "SYSTEM":
            # Check if this is Ls response
            text = message["Message"]
            if "--- ROOM INFO ---" in text:
                add_chat("system", "Room Info received.", color="36")
                # Parse room info (this is hacky, but avoids changing server)
                for line in text.split("\n"):
                    if line.strip().startswith("Other players here:"):
                        players_str = line.split(":", 1)[1].strip()
                        if players_str != "Nobody else is here.":
                            client_state.room_players = [player.strip() for player in players_str.split(",")]
                        else:
                            client_state.room_players = []
            else:
                add_chat("system", text, color="36")
        else:
            add_chat("chat", message["Message"], player=p, color="0")
        client_state.needs_redraw = True

    elif message_type == "Whisper":
        add_chat("whisper", message["Message"], player=message.get("Player", ""), color="35")
        client_state.needs_redraw = True

    elif message_type == "WhisperSent":
        add_chat("whisper", message["Message"], player=message.get("Player", "") + " (You)", color="35")
        client_state.needs_redraw = True

    elif message_type == "Error":
        add_chat("error", message["Message"], color="31")
        client_state.needs_redraw = True

    elif message_type == "TaskAssign":
        tasks_list = message.get("Tasks", [])
        client_state.tasks = [{"Name": t, "Completed": False} for t in tasks_list]
        add_chat("system", f"Assigned {len(tasks_list)} tasks.", color="36")
        client_state.needs_redraw = True

    elif message_type == "TaskStart":
        minigame = message.get("Minigame", "")
        room = message.get("Room", "")

        def run_task():
            global active_minigame_process
            sys.stdout.write("\033[2J\033[H")
            print(f"╔══════════════════════════════════╗")
            print(f"║          {minigame.upper().ljust(22)}  ║")
            print(f"╠══════════════════════════════════╣")
            success = False
            try:
                cmd = f"from {minigame} import play_{minigame}; import sys; sys.exit(0 if play_{minigame}() else 1)"
                proc = subprocess.Popen(["python3", "-c", cmd])
                active_minigame_process = proc
                proc.wait()
                success = (proc.returncode == 0)
            except Exception as e:
                print(f"Minigame error: {e}")
                success = False
            finally:
                active_minigame_process = None

            if conn is not None:
                import json
                data = (json.dumps({
                    "Type": "TaskResult",
                    "Room": room,
                    "Success": success,
                }) + "\n").encode()
                conn.sendall(data)
            client_state.needs_redraw = True

        import threading
        threading.Thread(target=run_task, daemon=True).start()

    elif message_type == "TaskComplete":
        room = message.get("Room", "")
        for t in client_state.tasks:
            if t["Name"] == room:
                t["Completed"] = True
        add_chat("system", f"Task in {room} completed!", color="32")
        client_state.needs_redraw = True

    elif message_type == "TaskList":
        add_chat("system", "Tasks updated.", color="36")
        client_state.needs_redraw = True

    elif message_type == "KillMinigame":
        target_name = message.get("Target", "")

        def run_kill():
            global active_minigame_process
            sys.stdout.write("\033[2J\033[H")
            print(f"╔══════════════════════════════════╗")
            print(f"║          KILL MINIGAME           ║")
            print(f"╠══════════════════════════════════╣")
            success = False
            try:
                cmd = "from kill_minigame import kill_minigame; import sys; sys.exit(0 if kill_minigame() else 1)"
                proc = subprocess.Popen(["python3", "-c", cmd])
                active_minigame_process = proc
                proc.wait()
                success = (proc.returncode == 0)
            except Exception as e:
                print(f"Kill minigame error: {e}")
                success = False
            finally:
                active_minigame_process = None

            if conn is not None:
                import json
                data = (json.dumps({
                    "Type": "KillResult",
                    "Target": target_name,
                    "Success": success,
                }) + "\n").encode()
                conn.sendall(data)
            client_state.needs_redraw = True

        import threading
        threading.Thread(target=run_kill, daemon=True).start()

    elif message_type == "Death":
        player_name = message.get("Player", "")
        msg = message.get("Message", "")
        if player_name in client_state.players:
            client_state.players[player_name]["Alive"] = False
            
        if player_name == my_client_name:
            global client_alive
            client_alive = False
            add_chat("system", "YOU DIED!", color="1;31")
        
        add_chat("system", msg, color="1;31")
        client_state.needs_redraw = True

    elif message_type == "AuditLog" or message_type == "AuditLeak" or message_type == "TamperPrompt":
        log = message.get("Log", [])
        target = message.get("Target", message.get("Player", ""))
        add_chat("system", f"Audit Log for {target}: {len(log)} entries", color="36")
        for entry in log:
            add_chat("system", f" {entry['Timestamp']} {entry['Room']}", color="36")
        if message_type == "TamperPrompt":
            add_chat("system", "Use /tamperedit or /tamperadd to modify logs.", color="33")
        client_state.needs_redraw = True

    elif message_type == "VoteResult":
        eliminated = message.get("Eliminated")
        msg = message.get("Message", "")
        if eliminated:
            add_chat("system", msg, color="1;31")
        else:
            add_chat("system", msg, color="33")
        client_state.needs_redraw = True

    elif message_type == "GameOver":
        client_state.game_over = True
        client_state.game_over_msg = message.get("Message", "")
        client_state.game_over_roles = message.get("Roles", "")
        client_state.needs_redraw = True

    elif message_type == "PlayerList":
        # Custom message to initialize players
        p_list = message.get("Players", {})
        client_state.players = p_list
        client_state.needs_redraw = True
        
    elif message_type == "RoomChange":
        client_state.current_room = message.get("Room", client_state.current_room)
        client_state.needs_redraw = True



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

        message = ""
        while True:
            if active_minigame_process is not None:
                while active_minigame_process is not None:
                    time.sleep(0.1)
                if client_state.needs_redraw:
                    draw_hud()
                    client_state.needs_redraw = False
                
            r, _, _ = select.select([sys.stdin], [], [], 0.1)
            if r:
                line = sys.stdin.readline()
                if not line:
                    return # EOF
                message = line
                break

        # ====================================================
        # QUIT
        # ====================================================

        if message.strip() == "/quit":

            break


        # ====================================================
        # IGNORE EMPTY MESSAGE
        # ====================================================

        if message.strip() == "":
            continue

        message = message.strip()


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

        elif message.startswith("/tamperedit"):
            parts = message.split(maxsplit=3)
            if len(parts) >= 4:
                target = parts[1]
                try:
                    index = int(parts[2]) - 1
                    new_room = parts[3]
                    send_message(conn, {
                        "Type": "TamperAction",
                        "Action": "edit",
                        "Target": target,
                        "Index": index,
                        "NewRoom": new_room,
                    })
                except ValueError:
                    print("Invalid index.")
            else:
                print("Usage: /tamperedit <target> <index> <new_room>")

        elif message.startswith("/tamperadd"):
            parts = message.split(maxsplit=3)
            if len(parts) >= 4:
                target = parts[1]
                new_room = parts[2]
                timestamp = parts[3]
                send_message(conn, {
                    "Type": "TamperAction",
                    "Action": "add",
                    "Target": target,
                    "Room": new_room,
                    "Timestamp": timestamp,
                })
            else:
                print("Usage: /tamperadd <target> <room> <HH:MM:SS>")

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
        # LS (Room Info)
        # ====================================================

        elif message == "/ls":

            send_message(conn, {
                "Type": "Ls",
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

            help_text = (
                "=== COMMANDS ===\n"
                "  /ls            — List current room and players here\n"
                "  /move          — Move to a room\n"
                "  /task          — Do your task in current room\n"
                "  /tasks         — View your assigned tasks\n"
                "  /whisper       — Send a private message\n"
                "  /chat <msg>    — Send a public message\n"
                "  /vote <name>   — Vote to eliminate a player\n"
                "  /kill <name>   — [Virus] Kill a player (day)\n"
                "  /inspect <name>— [Detective] Inspect audit (day)\n"
                "  /revive <name> — [Antivirus] Revive a player (day)\n"
                "  /tamper <name> — [Rootkit] Tamper audit log (day)\n"
                "  /sabotage <name> — [Bad team] Jumble messages (day)\n"
                "  /help          — Show this help\n"
                "  /quit          — Leave the game\n"
                "================"
            )
            for line in help_text.split('\n'):
                add_chat("system", line, color="36")
            client_state.needs_redraw = True


        # ====================================================
        # NORMAL CHAT
        # ====================================================
        
        elif message.startswith("/chat "):
            chat_msg = message[6:].strip()
            if chat_msg:
                send_chat(conn, chat_msg)

        elif message.startswith("/"):
            add_chat("error", "Unknown command. Type /help for a list of commands.", color="31")
            client_state.needs_redraw = True

        else:
            add_chat("error", "Please use a command (e.g., /chat <message> to talk). Type /help for a list of commands.", color="33")
            client_state.needs_redraw = True
            
        if client_state.needs_redraw:
            draw_hud()
            client_state.needs_redraw = False


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

    draw_hud()

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