import socket
import threading
import json
import random
import time
import subprocess
import select
import sys
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
NIGHT_DURATION = 30       # seconds
DISCUSSION_DURATION = 45  # seconds
VOTING_DURATION = 20      # seconds
GAME_OVER_DELAY = 5       # seconds before returning to the lobby
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

# Audit logs: { player_id: [ {"Type": "MOVE"|"ACTION", "Room": str, "Timestamp": str}, ... ] }
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
    "system_admin_inspect": False,
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

# Short descriptions shown in the HUD so tasks feel like actual terminal work.
TASK_DESCRIPTIONS = {
    "GPU": "Match memory blocks to verify GPU buffer allocation.",
    "CyberSec": "Analyze a suspicious security term from the incident logs.",
    "Web Dev": "Verify a deployment keyword from the web service logs.",
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

# The client's own name
client_name = ""

# The client's own alive status
client_alive = True

# Active minigame subprocess
active_minigame_process = None

# Set when the server/client state forces the current minigame to stop.
# This prevents an interrupted minigame thread from sending a stale result.
minigame_cancelled = False

# ============================================================
# UI STATE (CLIENT-SIDE)
# ============================================================

ui_players = {}      # dict mapping player name -> dict with "Alive": bool
ui_current_room = "Lobby"
ui_room_players = None # last known room occupants; None means not checked yet
ui_tasks = []        # list of dicts: {"room": str, "completed": bool}
ui_chat_log = []     # list of strings
ui_audit_leaks = []  # leaked audit log entries for the current discussion
ui_phase = "Lobby"
ui_day_num = 0
ui_time_left = 0

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


def broadcast_to_room(room, message):
    """Send a message only to players who are in the given room right now."""

    with lock:
        room_connections = [
            connections[pid]
            for pid, player in players.items()
            if player.get("Room") == room and pid in connections
        ]

    for conn in room_connections:
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

        if current_phase != "Lobby" and current_phase != "GameOver":
            winner = check_win_conditions()
            if winner:
                announce_winner(winner)


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

    good_roles = {"Process", "System Admin", "Antivirus"}
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
    4 players: 1 Virus, 1 Rootkit, 1 System Admin, 1 Process
    5+ players: 1 Virus, 1 Rootkit, 1 System Admin, 1 Antivirus, rest Process
    """

    with lock:
        player_ids = list(players.keys())

    num_players = len(player_ids)
    random.shuffle(player_ids)

    roles = ["Virus", "System Admin"]

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
        "Type": "MOVE",
        "Room": room,
        "Timestamp": datetime.now().strftime("%H:%M:%S"),
    }

    with game_lock:
        if player_id in audit_logs:
            audit_logs[player_id].append(entry)
        else:
            audit_logs[player_id] = [entry]


def add_action_audit_entry(player_id, room=None):
    """Record that the player performed an action without revealing what it was."""

    if room is None:
        with lock:
            player = players.get(player_id)
            room = player.get("Room", "Unknown") if player else "Unknown"

    entry = {
        "Type": "ACTION",
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
            "Message": f"You moved from {current_room} to {room}.  Players here: {players_str}"
        }
    )

    # Send structured room state so the HUD can update without parsing text.
    send_to_player(player_id, {
        "Type": "RoomInfo",
        "Room": room,
        "Players": players_here
    })

    return 1


# ============================================================
# TASK MANAGEMENT
# ============================================================

def assign_tasks_for_day():
    """Assign TASKS_PER_DAY random room-tasks to each alive good-team player."""

    good_roles = {"Process", "System Admin", "Antivirus"}
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

    good_roles = {"Process", "System Admin", "Antivirus"}

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

    # This is a real game action, but the audit log deliberately does not
    # reveal that it was a task.
    add_action_audit_entry(player_id, room)

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

    # Log the action without revealing that it was a kill.
    add_action_audit_entry(player_id, player["Room"])

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

        # Capture the room at the exact moment the kill result is handled.
        # The alert is sent only to players currently in this room.
        action_room = player.get("Room", "Lobby")

    if not success:
        send_to_player(player_id, {
            "Type": "Chat",
            "Player": "SYSTEM",
            "Message": "Kill failed. The target escaped."
        })

        broadcast_to_room(action_room, {
            "Type": "Chat",
            "Player": "SYSTEM",
            "Message": "A kill was attempted, but it failed."
        })
        return

    target_id = find_player_id_by_name(target_name)

    if target_id is None:
        return

    with lock:
        target = players.get(target_id)

        if target is None:
            return

        if target["Room"] != action_room:
            send_to_player(player_id, {
                "Type": "Chat",
                "Player": "SYSTEM",
                "Message": f"Kill failed. '{target_name}' left the room!"
            })

            broadcast_to_room(action_room, {
                "Type": "Chat",
                "Player": "SYSTEM",
                "Message": "A kill was attempted, but it failed."
            })
            return

        target["Alive"] = False

    with game_lock:
        recently_dead.append(target_id)

    # Announce the kill only to players who are in the room at this instant.
    broadcast_to_room(action_room, {
        "Type": "Chat",
        "Player": "SYSTEM",
        "Message": "A kill was committed."
    })

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
    """Handle System Admin audit inspection."""

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

    if player["Role"] != "System Admin":
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "Only the System Admin can inspect."
        })
        return

    if not player["Alive"]:
        send_to_player(player_id, {
            "Type": "Error",
            "Message": "You are dead."
        })
        return

    with game_lock:
        if night_actions_done["system_admin_inspect"]:
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

    # The audit log records only that an action occurred.
    add_action_audit_entry(player_id, player["Room"])

    with game_lock:
        night_actions_done["system_admin_inspect"] = True
        log = list(audit_logs.get(target_id, []))

    # Send audit log only to the System Admin
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

    # Log the revive as an anonymous action.
    add_action_audit_entry(player_id, player["Room"])

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

    with lock:
        actor = players.get(player_id)

    if actor is None:
        return

    actor_room = actor.get("Room", "Unknown")

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

            valid_index = 0 <= index < len(log)

        if valid_index:
            # Record the Rootkit's tamper as an action in its own log.
            add_action_audit_entry(player_id, actor_room)

            with game_lock:
                log = audit_logs.get(target_id, [])
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

        # Log the Rootkit action separately from the fake entry it creates.
        add_action_audit_entry(player_id, actor_room)

        with game_lock:
            if target_id not in audit_logs:
                audit_logs[target_id] = []

            audit_logs[target_id].append({
                "Type": "ACTION",
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

    # Log sabotage only as an anonymous action.
    add_action_audit_entry(player_id, player["Room"])

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

    # Voting is also an action, but the audit log does not reveal what was voted.
    add_action_audit_entry(player_id, player["Room"])

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
    global night_actions_done, recently_dead, sabotage_targets, game_started

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

        # Reset daily actions
        with game_lock:
            night_actions_done["system_admin_inspect"] = False
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

    print(f"[SERVER] Game loop ended. Returning to lobby in {GAME_OVER_DELAY} seconds.")
    time.sleep(GAME_OVER_DELAY)
    
    with lock:
        for pid, p in players.items():
            p["Alive"] = True
            p["Role"] = None
            p["Room"] = "Lobby"
            p["Tasks"] = []
            p["TasksCompleted"] = 0
            
    with game_lock:
        audit_logs.clear()
        for pid in players:
            audit_logs[pid] = []
        recently_dead.clear()
        sabotage_targets.clear()
        day_number = 0
        current_phase = "Lobby"
        game_started.clear()
        
    broadcast({
        "Type": "RoomInfo",
        "Room": "Lobby",
        "Players": [p["Name"] for p in players.values()]
    })
    
    broadcast({
        "Type": "PhaseChange",
        "Phase": "Lobby"
    })
    
    broadcast({
        "Type": "Chat",
        "Player": "SYSTEM",
        "Message": "The lobby has been reset for a new game. Type /start or start game to begin!"
    })
    
    print("[SERVER] Game state reset to Lobby.")


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
    # START GAME
    # --------------------------------------------------------
    
    if message_type == "StartGame":
        if current_phase == "Lobby" and not game_started.is_set():
            with lock:
                player_count = len(players)
            if player_count < 3:
                send_to_player(player_id, {
                    "Type": "Error",
                    "Message": f"Need at least 3 players to start (currently {player_count})."
                })
            else:
                broadcast({
                    "Type": "Chat",
                    "Player": "SYSTEM",
                    "Message": f"{name} started the game!"
                })
                threading.Thread(target=run_game_loop, daemon=True).start()
                game_started.set()
        return


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
    # INSPECT (System Admin)
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
            "Type": "RoomInfo",
            "Room": room,
            "Players": players_here
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

        # Send the current lobby state to the new player
        with lock:
            all_players = [p["Name"] for p in players.values()]
        
        send_message(conn, {
            "Type": "PlayerList",
            "Players": all_players
        })


        # Tell everyone that this player joined
        broadcast({
            "Type": "Join",
            "Player": name,
            "Message": f"{name} just popped in! (Type /start or start game to begin)"
        })


        # ====================================================
        # LISTEN FOR GAME MESSAGES
        # ====================================================


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

    global client_conn, client_name
    client_name = name

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
    print("Type /start or start game in the chat to begin the game.")
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
    # HOST IS NOW A PLAYER
    # ========================================================

    # Initialize the host's local UI state just like a normal client.
    global client_role, client_alive
    global ui_players, ui_current_room, ui_room_players
    global ui_tasks, ui_chat_log, ui_phase, ui_day_num

    client_role = None
    client_alive = True
    ui_players = {}
    ui_current_room = "Lobby"
    ui_room_players = None
    ui_tasks = []
    ui_chat_log = []
    ui_audit_leaks = []
    ui_phase = "Lobby"
    ui_day_num = 0

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
# CLIENT UI RENDERING
# ============================================================

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"
YELLOW = "\033[93m"

# One lock for terminal rendering. The receive thread and the input
# thread can both request a redraw, so only one can draw at a time.
ui_draw_lock = threading.RLock()
ui_dirty = True

# Keep the HUD compact enough for normal laptop terminals.
HUD_WIDTH = 78

# ANSI escape sequence for a real redraw rather than appending output.
CLEAR_SCREEN = "\033[2J\033[H"
ENTER_ALT_SCREEN = "\033[?1049h\033[H\033[2J"
EXIT_ALT_SCREEN = "\033[?1049l"


def enter_hud_screen():
    """Switch to a dedicated terminal screen for the HUD."""
    sys.stdout.write(ENTER_ALT_SCREEN)
    sys.stdout.flush()


def exit_hud_screen():
    """Restore whatever was on the terminal before the game HUD."""
    sys.stdout.write(EXIT_ALT_SCREEN)
    sys.stdout.flush()


def clear_terminal():
    """Clear the HUD screen and put the cursor at the top-left."""
    sys.stdout.write(CLEAR_SCREEN)
    sys.stdout.flush()


def strip_ansi(text):
    import re
    return re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", str(text))


def visible_width(text):
    """
    Return an approximate terminal display width.

    wcwidth is used when available. The fallback handles the common
    full-width Unicode characters without requiring a dependency.
    """
    clean = strip_ansi(text)

    try:
        from wcwidth import wcswidth
        width = wcswidth(clean)
        if width >= 0:
            return width
    except ImportError:
        pass

    import unicodedata
    width = 0
    for char in clean:
        if unicodedata.combining(char):
            continue
        if unicodedata.east_asian_width(char) in ("F", "W"):
            width += 2
        else:
            width += 1
    return width


def truncate_visible(text, max_width):
    """Truncate coloured/unicode text without letting it exceed the box."""
    if visible_width(text) <= max_width:
        return text

    clean = strip_ansi(text)
    result = ""
    width = 0

    for char in clean:
        char_width = visible_width(char)
        if width + char_width > max_width - 3:
            break
        result += char
        width += char_width

    return result + "..."


def wrap_visible(text, max_width):
    """
    Wrap text based on visible terminal width.
    ANSI codes are intentionally stripped from wrapped chat lines;
    this keeps the box reliable even for coloured messages.
    """
    import textwrap

    clean = strip_ansi(text)
    if not clean:
        return [""]

    return textwrap.wrap(
        clean,
        width=max_width,
        break_long_words=True,
        break_on_hyphens=False
    ) or [""]


def box_line(content="", align="left"):
    """Render exactly one HUD row. Long content is not silently truncated."""
    content = str(content)

    # box_line is for content that is already known to fit on one line.
    # Long content should use print_wrapped_box() instead.
    if visible_width(content) > HUD_WIDTH:
        content = strip_ansi(content)[:HUD_WIDTH]

    width = visible_width(content)
    padding = max(0, HUD_WIDTH - width)

    if align == "center":
        left = padding // 2
        right = padding - left
        return f"║{' ' * left}{content}{' ' * right}║"

    return f"║{content}{' ' * padding}║"


def print_wrapped_box(content, indent=""):
    """Print content across as many HUD rows as necessary."""
    available = max(1, HUD_WIDTH - visible_width(indent))
    lines = wrap_visible(content, available)

    for line in lines:
        print(box_line(indent + line))


def section_header(title):
    # Section titles are short in normal use; wrap them safely if needed.
    return box_line(f" {CYAN}{BOLD}{title}{RESET}")


def log_event(msg):
    """Add one message/event to the HUD history."""
    global ui_chat_log, ui_dirty

    # Store the event once. Rendering is handled separately.
    ui_chat_log.append(str(msg))

    # Keep the HUD compact.
    if len(ui_chat_log) > 12:
        ui_chat_log = ui_chat_log[-12:]
    ui_dirty = True


def stop_active_minigame(reason=""):
    """Stop the current minigame immediately and return control to the HUD."""
    global active_minigame_process, minigame_cancelled, ui_dirty

    proc = active_minigame_process
    if proc is None:
        return False

    minigame_cancelled = True

    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=0.5)
    except Exception:
        pass

    active_minigame_process = None
    ui_dirty = True

    if reason:
        log_event(f"{YELLOW}[!] {reason} Minigame stopped.{RESET}")

    return True


def draw_hud():
    """
    Render the ENTIRE HUD.

    This function is presentation-only:
    - It reads current UI/client state.
    - It does NOT process network messages.
    - It does NOT call handle_server_message().
    - It does NOT modify game state.
    """
    if active_minigame_process is not None:
        return

    with ui_draw_lock:
        clear_terminal()

        top = "╔" + "═" * HUD_WIDTH + "╗"
        mid = "╠" + "═" * HUD_WIDTH + "╣"
        bottom = "╚" + "═" * HUD_WIDTH + "╝"

        print(top)
        print(box_line(f"{CYAN}{BOLD}PROCESS MAFIA{RESET}", align="center"))
        print(mid)

        # --------------------------------------------------------
        # PLAYER
        # --------------------------------------------------------
        role = client_role if client_role else "Unknown"
        status = f"{GREEN}ALIVE{RESET}" if client_alive else f"{RED}DEAD{RESET}"
        player_text = (
            f" Player: {client_name or 'Unknown'}"
            f"  |  Status: {status}"
            f"  |  Role: {role}"
            f"  |  Phase: {ui_phase} (Day {ui_day_num})"
        )
        print(box_line(player_text))

        print(mid)

        # --------------------------------------------------------
        # LOBBY
        # --------------------------------------------------------
        print(section_header("LOBBY"))

        if not ui_players:
            print(box_line("   No players"))
        else:
            names = []
            for player_name, data in ui_players.items():
                alive = data.get("Alive", True)
                if alive:
                    names.append(f"{GREEN}● {player_name}{RESET}")
                else:
                    names.append(f"{RED}✕ {player_name}{RESET}")

            # Keep three players per row.
            for i in range(0, len(names), 3):
                print(box_line("   " + "   ".join(names[i:i + 3])))

        print(mid)

        # --------------------------------------------------------
        # CURRENT ROOM
        # --------------------------------------------------------
        print(section_header(f"CURRENT ROOM: {ui_current_room}"))

        if ui_current_room == "Lobby":
            print(box_line("   Lobby — no room tracking needed."))
        elif ui_room_players is None:
            print(box_line("   Room occupants: unknown"))
            print(box_line("   Use /ls to refresh who is currently here."))
        elif ui_room_players:
            print(box_line("   Room occupants (last update):"))
            room_names = []
            for player_name in ui_room_players:
                room_names.append(f"{GREEN}● {player_name}{RESET}")

            for i in range(0, len(room_names), 3):
                print(box_line("   " + "   ".join(room_names[i:i + 3])))
            print(box_line("   /ls → refresh this list"))
        else:
            print(box_line("   Nobody else here (last update)."))
            print(box_line("   /ls → refresh this list"))

        print(mid)

        # --------------------------------------------------------
        # TASKS
        # --------------------------------------------------------
        print(section_header("TASKS"))

        if not ui_tasks:
            print(box_line("   No tasks assigned yet."))
        else:
            completed_count = sum(
                1 for task in ui_tasks if task.get("completed", False)
            )

            print(
                box_line(
                    f"   Progress: {completed_count}/{len(ui_tasks)}"
                )
            )

            for task in ui_tasks:
                room = task.get("room", "Unknown")
                completed = task.get("completed", False)

                if completed:
                    status = f"{GREEN}✓{RESET}"
                else:
                    status = f"{YELLOW}○{RESET}"

                print(box_line(f"   {status} {room}: {TASK_DESCRIPTIONS.get(room, 'Perform the assigned terminal task.')}"))

        print(mid)

        # --------------------------------------------------------
        # CHAT / EVENTS
        # --------------------------------------------------------
        print(section_header("RECENT ACTIVITY"))

        if not ui_chat_log:
            print(box_line("   Nothing yet."))
        else:
            # Keep activity readable: each event is a compact bullet.
            # Long messages wrap naturally onto following HUD rows.
            for raw_msg in ui_chat_log[-12:]:
                clean_msg = strip_ansi(raw_msg).strip()
                if clean_msg:
                    print_wrapped_box(clean_msg, indent="   • ")

        print(mid)

        # --------------------------------------------------------
        # LEAKED AUDIT LOGS
        # --------------------------------------------------------
        if ui_audit_leaks:
            print(section_header("LEAKED AUDIT LOGS"))
            for line in ui_audit_leaks:
                print_wrapped_box(line, indent="   ")
            print(mid)

        # --------------------------------------------------------
        # COMMAND INPUT
        # --------------------------------------------------------
        print(bottom)
        print(f"{CYAN}> {RESET}", end="", flush=True)


# ============================================================
# HANDLE SERVER MESSAGE (CLIENT-SIDE)
# ============================================================

def handle_server_message(message, conn=None):
    """
    Process exactly one server message.

    IMPORTANT ARCHITECTURE:
        server message
            -> update UI state
            -> optionally add event
            -> redraw HUD once

    draw_hud() NEVER calls this function.
    """
    global client_role, client_alive
    global ui_phase, ui_day_num, ui_current_room
    global ui_room_players, ui_tasks, ui_players, ui_audit_leaks
    global active_minigame_process, ui_dirty

    message_type = message.get("Type")
    redraw = True

    # A server event means the game state changed. If a minigame is running,
    # immediately stop it so the minigame can never keep the terminal after
    # a death, game over, phase change, disconnect, etc.
    # The main client loop will redraw the normal boxed HUD afterwards.
    if active_minigame_process is not None:
        stop_active_minigame(
            f"Server event ({message_type}) received."
        )

    if message_type == "PlayerList":
        for player_name in message.get("Players", []):
            ui_players.setdefault(player_name, {"Alive": True})

    elif message_type == "RoomInfo":
        ui_current_room = message.get("Room", ui_current_room)
        ui_room_players = list(message.get("Players", []))

    elif message_type == "Join":
        player_name = message.get("Player", "")
        if player_name:
            ui_players.setdefault(player_name, {"Alive": True})

        join_message = message.get("Message", f"{player_name} joined.")
        log_event(f"{GREEN}[+] {join_message}{RESET}")

    elif message_type == "Leave":
        player_name = message.get("Player", "")
        if player_name in ui_players:
            del ui_players[player_name]

        log_event(
            f"{RED}[-] {message.get('Message', player_name + ' left.')}{RESET}"
        )

    elif message_type == "GameStart":
        log_event(f"{CYAN}{BOLD}=== GAME HAS STARTED ==={RESET}")

    elif message_type == "RoleAssign":
        client_role = message.get("Role", "Unknown")

        log_event(
            f"{CYAN}{BOLD}--- YOUR ROLE: {client_role} ---{RESET}"
        )

        if client_role == "Process":
            log_event(
                f"{CYAN}You are a normal process. Complete tasks and find the threats.{RESET}"
            )
        elif client_role == "System Admin":
            log_event(
                f"{CYAN}You can inspect audit logs. Use /inspect <name> during the day.{RESET}"
            )
        elif client_role == "Antivirus":
            log_event(
                f"{CYAN}You can revive ONE dead player. Use /revive <name> during the day.{RESET}"
            )
        elif client_role == "Virus":
            log_event(
                f"{RED}You are the killer. Use /kill <name> during the day.{RESET}"
            )
        elif client_role == "Rootkit":
            log_event(
                f"{RED}You can tamper with audit logs. Use /tamper <name> during the day.{RESET}"
            )

    elif message_type == "PhaseChange":
        ui_phase = message.get("Phase", ui_phase)
        ui_day_num = message.get("DayNumber", ui_day_num)
        duration = message.get("Duration", "")

        if ui_phase == "Day":
            # A new day starts a fresh audit-evidence section.
            ui_audit_leaks = []

            # All players start the game in Common.
            if ui_current_room == "Lobby":
                ui_current_room = "Common"
                ui_room_players = None

            log_event(
                f"{YELLOW}☀ DAY {ui_day_num} ({duration}s) - Complete your tasks!{RESET}"
            )
        elif ui_phase == "Night":
            log_event(
                f"{CYAN}NIGHT {ui_day_num} ({duration}s) - Use your night abilities.{RESET}"
            )
        elif ui_phase == "Discussion":
            log_event(
                f"{CYAN}DISCUSSION ({duration}s) - Discuss who the threats are.{RESET}"
            )
        elif ui_phase == "Voting":
            log_event(
                f"{YELLOW}VOTING ({duration}s) - Use /vote <name> to vote.{RESET}"
            )
        elif ui_phase == "Lobby":
            log_event(f"{CYAN}[EVENT] Lobby ready.{RESET}")

    elif message_type == "Chat":
        player = message.get("Player", "")
        msg = message.get("Message", "")

        if player == "SYSTEM":
            # RoomInfo is now enough to update the room section.
            # Ignore the old multi-line room-info text if an older
            # server still sends it.
            ignored = {
                "--- ROOM INFO ---",
                "-----------------",
            }

            for line in msg.splitlines():
                line = line.strip()
                if not line:
                    continue

                if line in ignored:
                    continue

                if (
                    line.startswith("You are in:")
                    or line.startswith("Other players here:")
                    or line.startswith("Available rooms:")
                ):
                    continue

                log_event(f"{CYAN}[EVENT] {line}{RESET}")
        else:
            log_event(f"{BOLD}{player} ›{RESET} {msg}")

    elif message_type == "Whisper":
        player = message.get("Player", "")
        msg = message.get("Message", "")
        log_event(f"{YELLOW}[WHISPER] {player} → You{RESET}")
        log_event(f"{YELLOW}> {msg}{RESET}")

    elif message_type == "WhisperSent":
        player = message.get("Player", "")
        msg = message.get("Message", "")
        log_event(f"{YELLOW}[WHISPER] You → {player}{RESET}")
        log_event(f"{YELLOW}> {msg}{RESET}")

    elif message_type == "Error":
        log_event(
            f"{RED}[ERROR] {message.get('Message', 'Unknown error')}{RESET}"
        )

    elif message_type == "TaskAssign":
        tasks_list = message.get("Tasks", [])

        # Keep one consistent internal representation.
        ui_tasks = [
            {
                "room": room,
                "completed": False
            }
            for room in tasks_list
        ]

        log_event(f"{GREEN}[+] TASKS ASSIGNED{RESET}")

    elif message_type == "TaskStart":
        minigame = message.get("Minigame", "")
        room = message.get("Room", "")

        def run_task():
            global active_minigame_process, minigame_cancelled

            minigame_cancelled = False
            clear_terminal()
            print(
                f"{CYAN}--- Starting task: {minigame} ---{RESET}\n",
                flush=True
            )

            success = False

            try:
                cmd = (
                    f"from {minigame} import play_{minigame}; "
                    f"import sys; "
                    f"sys.exit(0 if play_{minigame}() else 1)"
                )

                proc = subprocess.Popen(
                    ["python3", "-c", cmd]
                )
                active_minigame_process = proc

                proc.wait()
                success = proc.returncode == 0

            except Exception as e:
                print(f"Minigame error: {e}")
                success = False

            finally:
                active_minigame_process = None

            # If the minigame was interrupted by a server event, do not send
            # a stale success/failure result back to the server.
            was_cancelled = minigame_cancelled
            ui_dirty = True

            if conn is not None and not was_cancelled:
                send_message(
                    conn,
                    {
                        "Type": "TaskResult",
                        "Room": room,
                        "Success": success,
                    }
                )

        threading.Thread(
            target=run_task,
            daemon=True
        ).start()

        # The minigame thread owns the terminal now.
        redraw = False

    elif message_type == "TaskComplete":
        room = message.get("Room", "")
        remaining = message.get("Remaining", 0)

        for task in ui_tasks:
            if task.get("room") == room and not task.get("completed", False):
                task["completed"] = True
                break

        log_event(f"{GREEN}✓ Task in {room} completed!{RESET}")

        if remaining == 0:
            log_event(f"{GREEN}All tasks complete!{RESET}")

    elif message_type == "TaskList":
        tasks_list = message.get("Tasks", [])
        completed_count = message.get("Completed", 0)

        # Sync task names without inventing completion details.
        if tasks_list:
            ui_tasks = [
                {
                    "room": room,
                    "completed": False
                }
                for room in tasks_list
            ]

        log_event(
            f"{CYAN}Tasks: {completed_count} completed.{RESET}"
        )

    elif message_type == "KillMinigame":
        target_name = message.get("Target", "")

        def run_kill():
            global active_minigame_process, minigame_cancelled

            minigame_cancelled = False
            clear_terminal()
            print(
                f"{RED}--- Attempting to kill {target_name} ---{RESET}\n",
                flush=True
            )

            success = False

            try:
                cmd = (
                    "from kill_minigame import kill_minigame; "
                    "import sys; "
                    "sys.exit(0 if kill_minigame() else 1)"
                )

                proc = subprocess.Popen(
                    ["python3", "-c", cmd]
                )
                active_minigame_process = proc

                proc.wait()
                success = proc.returncode == 0

            except Exception as e:
                print(f"Kill minigame error: {e}")
                success = False

            finally:
                active_minigame_process = None

            # If a death/game-over/other server event interrupted the kill,
            # never report the old minigame result.
            was_cancelled = minigame_cancelled
            ui_dirty = True

            if conn is not None and not was_cancelled:
                send_message(
                    conn,
                    {
                        "Type": "KillResult",
                        "Target": target_name,
                        "Success": success,
                    }
                )

        threading.Thread(
            target=run_kill,
            daemon=True
        ).start()

        redraw = False

    elif message_type == "Death":
        player_name = message.get("Player", "")
        msg = message.get("Message", "")

        if player_name in ui_players:
            ui_players[player_name]["Alive"] = False

        if player_name == client_name:
            client_alive = False
            log_event(
                f"{RED}{BOLD}YOU ARE DEAD! Your process was terminated.{RESET}"
            )
        else:
            log_event(f"{RED}[DEATH] {msg}{RESET}")

    elif message_type == "AuditLog":
        target = message.get("Target", "")
        log_list = message.get("Log", [])

        log_event(f"{CYAN}[AUDIT LOG] {target}{RESET}")

        if not log_list:
            log_event("  No entries.")
        else:
            for entry in log_list:
                timestamp = entry.get("Timestamp", "")
                room = entry.get("Room", "")
                if entry.get("Type") == "ACTION":
                    log_event(
                        f"  {timestamp}  Performed an action in {room}."
                    )
                else:
                    log_event(
                        f"  {timestamp}  Moved to {room}."
                    )

    elif message_type == "AuditLeak":
        player_name = message.get("Player", "")
        log_list = message.get("Log", [])

        # Keep leaked audits in their own HUD section so the normal event
        # history cannot push the evidence off-screen.
        if not ui_audit_leaks:
            ui_audit_leaks.append(
                f"{CYAN}[AUDIT LEAKS] Evidence from this discussion{RESET}"
            )

        ui_audit_leaks.append(
            f"{CYAN}{player_name} (deceased){RESET}"
        )

        if not log_list:
            ui_audit_leaks.append("  No entries.")
        else:
            for entry in log_list:
                timestamp = entry.get("Timestamp", "")
                room = entry.get("Room", "")
                if entry.get("Type") == "ACTION":
                    ui_audit_leaks.append(
                        f"  {timestamp}  Performed an action in {room}."
                    )
                else:
                    ui_audit_leaks.append(
                        f"  {timestamp}  Moved to {room}."
                    )

        log_event(
            f"{CYAN}[AUDIT] {player_name}'s audit log leaked.{RESET}"
        )

    elif message_type == "TamperPrompt":
        target = message.get("Target", "")
        log_list = message.get("Log", [])

        log_event(f"{RED}[TAMPER] {target}'s audit log{RESET}")

        if not log_list:
            log_event("  No entries.")
        else:
            for i, entry in enumerate(log_list):
                log_event(
                    f"  [{i + 1}] {entry.get('Timestamp', '')}  "
                    f"{entry.get('Room', '')}"
                )

        log_event(
            f"{RED}Use /tamperedit or /tamperadd for the requested change.{RESET}"
        )

    elif message_type == "VoteResult":
        eliminated = message.get("Eliminated")
        msg = message.get("Message", "")

        if eliminated:
            log_event(f"{RED}[VOTE] {msg}{RESET}")
        else:
            log_event(f"{YELLOW}[VOTE] {msg}{RESET}")

        if eliminated in ui_players:
            ui_players[eliminated]["Alive"] = False

    elif message_type == "GameOver":
        ui_phase = "GameOver"
        msg = message.get("Message", "")
        roles = message.get("Roles", "")

        log_event(f"{CYAN}{BOLD}=== GAME OVER ==={RESET}")
        log_event(f"{CYAN}{msg}{RESET}")

        for line in roles.splitlines():
            if line.strip():
                log_event(f"{CYAN}{line.strip()}{RESET}")

    else:
        # Unknown messages should not crash the HUD.
        redraw = False

    if redraw:
        ui_dirty = True


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

    global ui_dirty

    # Put the HUD on its own terminal screen so previous HUD frames
    # can never remain visible underneath the current one.
    enter_hud_screen()

    # Initial draw when loop starts
    draw_hud()
    ui_dirty = False

    while True:

        message = ""
        while True:
            if active_minigame_process is not None:
                while active_minigame_process is not None:
                    time.sleep(0.1)
                ui_dirty = True
                
            if ui_dirty and active_minigame_process is None:
                draw_hud()
                ui_dirty = False

            r, _, _ = select.select([sys.stdin], [], [], 0.1)
            if r:
                line = sys.stdin.readline()
                if not line:
                    exit_hud_screen()
                    return # EOF
                message = line
                break

        # ====================================================
        # QUIT
        # ====================================================

        if message.strip() == "/quit":

            exit_hud_screen()
            break


        # ====================================================
        # IGNORE EMPTY MESSAGE
        # ====================================================

        if message.strip() == "":
            ui_dirty = True
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
        # INSPECT (System Admin)
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

            log_event(f"{CYAN}{BOLD}COMMANDS{RESET}")
            log_event("/ls — current room and players")
            log_event("/move — move to a room")
            log_event("/task — do your task")
            log_event("/tasks — view tasks")
            log_event("/whisper — private message")
            log_event("/chat <msg> — public message")
            log_event("/vote <name> — vote")
            log_event("/kill <name> — Virus")
            log_event("/inspect <name> — System Admin")
            log_event("/revive <name> — Antivirus")
            log_event("/tamper <name> — Rootkit")
            log_event("/sabotage <name> — bad team")
            log_event("/help — show commands")
            log_event("/quit — leave")
            ui_dirty = True


        # ====================================================
        # NORMAL CHAT
        # ====================================================
        
        elif message.startswith("/chat "):
            chat_msg = message[6:].strip()
            if chat_msg:
                send_chat(conn, chat_msg)

        elif message.startswith("/start") or message.lower().strip() == "start game":
            send_message(conn, {"Type": "StartGame"})

        elif message.startswith("/"):
            log_event(f"{RED}[ERROR] Unknown command. Type /help for a list of commands.{RESET}")
            ui_dirty = True

        else:
            # If not a command and in Lobby or game active, try sending as chat
            if ui_phase == "Lobby":
                send_chat(conn, message)
            else:
                print("Please use a command (e.g., /chat <message> to talk). Type /help for a list of commands.")


    try:
        conn.close()
    except OSError:
        pass


# ============================================================
# CLIENT GAME
# ============================================================

def client_game(conn, name):

    global client_conn, client_name
    global client_role, client_alive
    global ui_players, ui_current_room, ui_room_players
    global ui_tasks, ui_chat_log, ui_phase, ui_day_num

    client_conn = conn
    client_name = name

    # Start this client with a clean UI state.
    client_role = None
    client_alive = True
    ui_players = {}
    ui_current_room = "Lobby"
    ui_room_players = None
    ui_tasks = []
    ui_chat_log = []
    ui_phase = "Lobby"
    ui_day_num = 0

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