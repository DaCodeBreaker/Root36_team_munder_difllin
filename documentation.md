# Process Mafia

A LAN multiplayer social-deduction game played entirely through a terminal.

Process Mafia combines a Linux/process theme with hidden roles, room-based tasks, audit logs, sabotage, killing, investigation, and voting. Players interact with the game through terminal commands while the server maintains the authoritative game state.

---

## Table of Contents

- [Overview](#overview)
- [Game Objective](#game-objective)
- [Roles](#roles)
- [Game Flow](#game-flow)
- [Rooms and Tasks](#rooms-and-tasks)
- [Commands](#commands)
- [Audit Logs](#audit-logs)
- [Networking](#networking)
- [Project Structure](#project-structure)
- [Running the Game](#running-the-game)
- [Technical Architecture](#technical-architecture)
- [Minigames](#minigames)
- [Configuration](#configuration)
- [Important Rules](#important-rules)

---

## Overview

**Process Mafia** is a multiplayer social-deduction game inspired by games such as Among Us, but presented as a Linux-style terminal environment.

Each player is represented as a computer process. Some processes belong to the good team, while others are malware attempting to compromise the system.

The game is played over a local network. One player hosts the game and other players join through LAN discovery.

The server is authoritative: player roles, movement, tasks, kills, votes, audit logs, and win conditions are maintained on the server rather than trusted from the client.

---

## Game Objective

There are three possible objectives depending on the player's role:

### Good Team

The good team attempts to identify and eliminate the malware.

Good-side roles:

- Process
- System Admin
- Antivirus
- Jester

The normal good team wins when all bad-team players are eliminated.

### Bad Team

The bad team attempts to eliminate the good processes.

Bad-side roles:

- Virus
- Rootkit

The bad team wins when the number of living bad-team players is greater than or equal to the number of living good-team players.

### Jester

The Jester has a separate objective.

The Jester behaves like a normal good-side process during the game, including completing tasks.

However:

> If the Jester is eliminated specifically by a vote, the Jester immediately wins the game.

The Jester does not win by being killed by the Virus, disconnecting, or through a normal team win condition.

---

# Roles

## Process

The standard good-team role.

### Abilities

- Complete assigned tasks.
- Move between rooms.
- Chat and whisper.
- Vote during voting.
- Help identify the malware using game information and audit logs.

---

## Virus

The primary killing role of the bad team.

### Abilities

- Kill another player.
- The target must be alive.
- The target must be in the same room.
- The Virus cannot kill another bad-team member.
- A kill requires successfully completing the kill minigame.
- Kill cooldown: 30 seconds.

The kill action is logged as an anonymous action rather than explicitly revealing that the action was a kill.

---

## Rootkit

The audit-log manipulation role of the bad team.

### Abilities

- Inspect another player's audit log.
- Modify an existing audit-log entry.
- Add a fake audit-log entry.
- Two tamper uses are available per game.
- The target must be in the same room.

The Rootkit can therefore manipulate evidence that other players may use to determine who was where.

---

## System Admin

The investigation role on the good team.

### Ability

The System Admin can inspect another player's audit log.

The target must be in the same room.

The audit log contains recorded room movements and anonymous actions with timestamps.

The System Admin has one inspection action per night cycle.

---

## Antivirus

The support role on the good team.

### Ability

The Antivirus can revive one dead player during the game.

Rules:

- Only one revive is available per game.
- The target must be dead.
- The target must be in the same room.
- The Antivirus must be alive.

---

## Jester

The independent objective role.

### Assignment

- Only possible when there are at least 5 players.
- There is a 50% chance for a game with 5+ players to contain one Jester.
- The Jester replaces one normal Process.
- There can be at most one Jester.

### Behavior

The Jester otherwise behaves like a normal good-side task player.

### Win condition

The Jester wins immediately if they are voted out.

---

# Game Flow

The game begins in the **Lobby**.

A game requires at least **3 players** to start.

Once started, the server assigns roles and begins the game loop.

The main phases are:

```text
Lobby
  ↓
Day
  ↓
Discussion
  ↓
Voting
  ↓
Day
  ↓
...
```

The game ends when a win condition is reached.

---

## Day

During the Day:

- Players can move between rooms.
- Players receive tasks.
- Players can complete minigames.
- Virus can attempt kills.
- Rootkit can tamper with audit logs.
- Antivirus can revive a player.
- System Admin can inspect audit logs.
- Bad-team sabotage can be used.

Public chat is disabled during the Day so that players focus on completing tasks.

The Day lasts up to **120 seconds**, but it can end early when all alive task-doing players finish their assigned tasks.

---

## Discussion

During Discussion:

- Deaths from the previous cycle are announced.
- Audit logs belonging to players who died during the cycle are leaked.
- Players can communicate using public chat.
- Players can use whispers.

Discussion lasts **45 seconds**.

---

## Voting

During Voting:

- Alive players vote for another alive player.
- Each player can vote once.
- A player receiving the most votes is eliminated.
- A tied vote results in no elimination.

Voting lasts **20 seconds**.

If the eliminated player is the Jester, the Jester wins immediately.

---

# Rooms and Tasks

The current game contains four task rooms:

| Room | Task | Description |
|---|---|---|
| File System | Ping | Verify packet transmission to remote servers |
| Memory | Memory | Match memory blocks |
| Security | Hangman | Analyze a suspicious security term |
| Web Server | Wordle | Verify a deployment keyword |

Players receive **2 random tasks per Day** from the normal task rooms.

The server records task completion and can end the Day early when all alive task-doing players have completed their assigned tasks.

---

# Commands

Commands are entered directly into the terminal.

Use:

```text
/help
```

to display the available commands in-game.

## General Commands

### View current room

```text
/ls
```

Shows the current room information and players in the room.

### Move

```text
/move
```

The game asks which room you want to enter.

Available rooms:

```text
File System
Memory
Security
Web Server
```

### Do task

```text
/task
```

Starts the task assigned to your current room.

### View tasks

```text
/tasks
```

Displays your assigned tasks and their completion status.

### Public chat

```text
/chat <message>
```

Example:

```text
/chat Is anyone else in Memory?
```

Public chat is available during the appropriate game phases.

### Whisper

```text
/whisper
```

The client asks for the target player and message.

Whispers are sent privately to the selected player.

---

# Role-Specific Commands

## Vote

```text
/vote <player>
```

Example:

```text
/vote nginx.exe
```

Used during the Voting phase.

---

## Virus — Kill

```text
/kill <player>
```

Example:

```text
/kill python.exe
```

The Virus must:

1. Be alive.
2. Be in the same room as the target.
3. Target a living player who is not on the bad team.
4. Successfully complete the kill minigame.

---

## System Admin — Inspect

```text
/inspect <player>
```

Shows the selected player's audit log to the System Admin.

---

## Antivirus — Revive

```text
/revive <player>
```

Attempts to revive the selected dead player.

---

## Rootkit — Tamper

```text
/tamper <player>
```

Starts the Rootkit's audit-log tampering interaction.

### Edit an existing entry

```text
/tamperedit <target> <index> <new_room>
```

Example:

```text
/tamperedit python.exe 2 Memory
```

### Add a fake entry

```text
/tamperadd <target> <room> <HH:MM:SS>
```

Example:

```text
/tamperadd python.exe Security 14:32:10
```

The Rootkit has a limited number of tamper uses.

---

## Bad Team — Sabotage

```text
/sabotage <player>
```

Applies a sabotage effect to the selected player.

The targeted player's messages are jumbled during the following Night.

There are two sabotage uses available per game.

---

# Audit Logs

The server maintains an audit log for every player.

An audit entry contains information such as:

```text
Type
Room
Timestamp
```

Example:

```text
MOVE     Memory       14:31:12
ACTION   Memory       14:31:25
MOVE     Security     14:31:48
```

The audit system is important because it provides objective information about player movement.

However, the Rootkit can manipulate another player's audit log.

When a player dies during a cycle, their audit log is leaked during the following Discussion phase.

---

# Networking

Process Mafia is designed for LAN multiplayer.

## Transport

The game uses:

- **TCP** for reliable game communication.
- **UDP** for LAN game discovery.

TCP is used for:

- Chat
- Role assignment
- Room movement
- Tasks
- Votes
- Kills
- Investigations
- Revives
- Audit information
- Game-state updates

UDP is used to allow clients on the local network to discover the host.

## Ports

Default ports:

```text
TCP: 5050
UDP: 5051
```

The host listens on:

```text
0.0.0.0
```

so other devices on the LAN can connect.

---

# Project Structure

The project is primarily Python-based.

A typical setup contains:

```text
Process-Mafia/
│
├── main.py
├── memory.py
├── hangman.py
├── wordle.py
├── ping.py
└── documentation.md
```

The main game/server/client logic is contained in `main.py`.

The task implementations are terminal minigames that expose functions such as:

```python
play_memory()
play_hangman()
play_wordle()
play_ping()
```

The main game launches the selected minigame and uses its return value to determine whether the task succeeded.

---

# Running the Game

## Requirements

- Python 3
- A terminal capable of handling standard terminal input
- Multiple computers on the same LAN for multiplayer

The kill minigame uses terminal-specific input functionality such as:

```text
termios
tty
select
```

so the game is intended to run in a Unix/Linux-style terminal environment.

---

## Start the game

Run:

```bash
python3 main.py
```

You will be asked for your name and whether you want to host or join.

### Host

Choose:

```text
HOST
```

The host creates the LAN game and also joins as a player.

### Client

Choose:

```text
CLIENT
```

Then enter the Game ID provided by the host.

---

# Technical Architecture

The game follows a client-server architecture.

```text
                 ┌──────────────────┐
                 │      SERVER      │
                 │                  │
                 │ Game State       │
                 │ Roles            │
                 │ Players          │
                 │ Tasks            │
                 │ Audit Logs       │
                 │ Votes            │
                 │ Win Conditions   │
                 └────────┬─────────┘
                          │
                  TCP game messages
                          │
          ┌───────────────┼───────────────┐
          │               │               │
          ▼               ▼               ▼
     ┌─────────┐     ┌─────────┐     ┌─────────┐
     │ Client  │     │ Client  │     │ Client  │
     │ Player  │     │ Player  │     │ Player  │
     └─────────┘     └─────────┘     └─────────┘

                 UDP LAN Discovery
```

The server is authoritative for important game state.

Clients send requests such as:

```json
{
    "Type": "Move",
    "Message": "Memory"
}
```

The server validates the request, updates its state, and sends the appropriate result back to the client.

---

# Message Protocol

Communication between clients and the server uses JSON messages separated by newlines over TCP.

Example:

```json
{
    "Type": "Chat",
    "Message": "hello"
}
```

Other message types include:

```text
StartGame
Chat
Whisper
Move
TaskRequest
TaskResult
Kill
KillResult
Inspect
Revive
Tamper
TamperAction
Sabotage
Vote
RoomInfo
TaskAssign
TaskStart
TaskComplete
PhaseChange
Death
AuditLeak
GameOver
```

This keeps the networking layer independent from the terminal presentation.

---

# Minigames

## Ping

The Ping task displays randomly generated IP addresses.

The player must enter each address exactly as displayed.

A successful sequence prints simulated packet replies and returns:

```python
True
```

---

## Memory

The Memory task generates an 8-card board containing matching symbol pairs.

The player selects two positions at a time and attempts to find all matching pairs.

Success returns:

```python
True
```

---

## Hangman

The Hangman task selects a Linux/technical word.

The player guesses one letter at a time with a maximum number of incorrect guesses.

Success returns:

```python
True
```

Failure returns:

```python
False
```

---

## Wordle

The Wordle task selects a five-letter technical word.

The player gets six attempts.

The result uses:

```text
🟩 Correct position
🟨 Correct letter, wrong position
⬜ Letter not present
```

---

# Configuration

Game timing and limits are configured near the top of `main.py`.

Current values include:

```python
DAY_DURATION = 120
NIGHT_DURATION = 30
DISCUSSION_DURATION = 45
VOTING_DURATION = 20
GAME_OVER_DELAY = 5

TASKS_PER_DAY = 2

MAX_SABOTAGES = 2
MAX_ROOTKIT_USES = 2
```

Networking ports:

```python
TCP_PORT = 5050
DISCOVERY_PORT = 5051
```

These values can be changed to adjust the game without changing the core game logic.

---

# Important Rules

### Minimum players

At least **3 players** are required to start a game.

### Jester

The Jester is only possible with **5 or more players** and has a 50% chance of appearing.

### Tasks

Alive good-side task players receive **2 tasks per Day**.

### Kill

The Virus can only kill a player in the same room and must successfully complete the kill minigame.

### Audit logs

Room movements and actions are recorded server-side.

### Rootkit

The Rootkit can alter another player's audit log using limited uses.

### Antivirus

The Antivirus can revive one player per game.

### Sabotage

The bad team has two sabotage uses per game.

### Voting

Only alive players can vote.

A tied vote causes no elimination.

### Game Over

Once a win condition is reached, the server broadcasts the result and reveals the roles.

---

# Design Philosophy

Process Mafia is designed around the idea of turning familiar Linux concepts into social-deduction mechanics:

```text
Processes       → Players
Malware         → Hidden enemies
Rooms           → System components
Tasks           → Terminal minigames
Audit logs      → Evidence
Rootkit         → Evidence manipulation
Virus           → Elimination
Antivirus       → Recovery
System Admin    → Investigation
Jester          → Independent objective
```

The result is a social-deduction game that can be played entirely through a terminal while still having a structured multiplayer game state underneath.
