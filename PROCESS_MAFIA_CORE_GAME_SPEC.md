# Process Mafia — Core Game Build Specification

## 1. Project Context

This project is a **LAN multiplayer terminal/CMD social-deduction game** inspired by *Among Us*, presented through a simulated Linux/process environment.

The current project already has working networking, room movement, chat, whisper, voting hooks, and separate minigame Python files.

> **CRITICAL: Keep all currently implemented functionality unchanged unless an integration change is absolutely required. Do NOT rewrite or replace working networking, `/whisper`, `/move`, room handling, TCP/UDP discovery, receive buffering, or existing minigames. Build the core game around the current implementation.**

---

# 2. Game Concept

Players are processes on a simulated Linux system.

There are two teams.

## Good Team

### Process
Normal good-team role.

- Has tasks.
- Must enter rooms to access tasks/minigames.
- Participates in discussion and voting.

### Detective
Good-team investigative role.

- Each night can inspect one player's audit log.
- Audit logs show rooms entered and timestamps.

### Antivirus
Good-team defensive role.

- Can revive **one player per game**.
- The one-use limit is server-side.

## Bad Team

There are two bad roles.

### Virus
The murderer.

- Can kill players.
- Uses the existing kill/minigame concept.
- The existing kill minigame must be preserved.

### Rootkit
The audit-manipulation role.

- Can alter/tamper with another player's audit log.
- This affects the audit information investigators see.
- Do not invent additional Rootkit powers.

---

# 3. Core Game Flow

The game repeatedly moves through:

```text
LOBBY
  ↓
DAY
  ↓
NIGHT
  ↓
DISCUSSION
  ↓
VOTING
  ↓
DAY
  ↓
...
```

The server is authoritative for the phase and all game state.

Keep these configurable near the configuration section:

```python
DAY_DURATION = ...
NIGHT_DURATION = ...
DISCUSSION_DURATION = ...
VOTING_DURATION = ...
TASKS_PER_DAY = 2
MAX_SABOTAGES = 2
```

Do not scatter hard-coded timers throughout the code.

---

# 4. Day Phase

During the day, good-team processes need to complete tasks.

For the first implementation:

```python
TASKS_PER_DAY = 2
```

The day ends when **either**:

1. The fixed day timer expires, OR
2. Everyone has completed their required tasks.

The server must determine task completion. Do not trust a client simply saying that a task is complete.

---

# 5. Existing Rooms and Minigames

The existing room system must remain unchanged.

Current rooms:

```python
[
    "Common",
    "GPU",
    "CyberSec",
    "Web Dev"
]
```

Players already use the existing `/move` functionality to change rooms.

Do not replace it.

Instead, integrate the existing `minigame.py` files into the rooms.

Conceptually:

```text
Player
  ↓
/move
  ↓
Room
  ↓
Task available in room
  ↓
Launch corresponding minigame.py
  ↓
Minigame result
  ↓
Server records task completion
```

A successful minigame counts as completed.

A failed minigame does not.

Do not rewrite the existing minigames unless absolutely necessary.

The server must record the result.

---

# 6. Player State

Extend the existing player dictionary only as required.

The current room field must remain.

Conceptually the game will need state similar to:

```python
{
    "Name": name,
    "Room": "Common",
    "Role": ...,
    "Alive": True,
    "Tasks": [...],
    "TasksCompleted": 0
}
```

Do not replace the current player structure wholesale. Integrate new fields into it.

Roles must remain private unless the game rules explicitly reveal them.

---

# 7. Audit Logs

This is a major game mechanic.

Every player's room movement must be recorded server-side.

Whenever a player successfully enters a room, record:

```text
Player
Room
Timestamp
```

Example:

```text
14:31:02  Common
14:31:18  GPU
14:32:04  CyberSec
14:33:21  Common
```

The timestamp must come from the server, not the client.

Conceptually:

```python
audit_logs[player_id] = [
    {
        "Room": "Common",
        "Timestamp": ...
    },
    {
        "Room": "GPU",
        "Timestamp": ...
    }
]
```

Integrate this into the existing successful `switch_rooms()` path.

Invalid room attempts should not create audit entries.

---

# 8. Detective Audit Ability

Each night, the Detective can inspect one player's audit log.

Server must verify:

- requester is the Detective
- requester is alive
- action is allowed during the current phase
- target exists

Return the target's audit log **only to the Detective**.

Do not broadcast private audit information.

The terminal output should clearly show rooms and timestamps.

---

# 9. Dead Player Audit Leak

When a player dies, their audit log is leaked during the night cycle.

This gives the good team evidence about the dead player's movements.

The audit log shown should reflect any Rootkit tampering that has actually been applied.

Do not reveal unrelated private player information.

---

# 10. Night Phase

The night phase must support:

- dead-player audit leak
- Detective audit inspection
- Virus kill
- Antivirus revive
- Rootkit audit manipulation
- bad-team sabotage

After the relevant night information/actions, the game proceeds to discussion and voting.

All night actions must be server-validated and disabled once the relevant phase ends.

---

# 11. Virus Kill

Preserve the existing kill minigame.

Flow:

```text
Virus selects target
        ↓
Server validates action
        ↓
Existing kill minigame
        ↓
Success?
   ┌────┴────┐
   YES       NO
    ↓         ↓
Target dies  Kill fails
```

Only the server applies the death after successful completion.

If the existing implementation already contains room/cooldown requirements, preserve them.

Do not invent new kill rules.

---

# 12. Antivirus Revive

The Antivirus can revive exactly **one player per game**.

Use a server-side flag such as:

```python
antivirus_used = False
```

Validate:

- requester is Antivirus
- requester is alive
- revive has not been used
- target is dead
- action is during the valid phase

After successful use:

```python
antivirus_used = True
```

It cannot be used again.

---

# 13. Rootkit Audit Manipulation

The Rootkit can alter another player's audit log.

Validate:

- requester is Rootkit
- requester is alive
- target exists
- action is allowed during the correct phase

The altered audit should be what investigators see.

Keep the audit system separate from the Rootkit ability so the Rootkit only modifies it through an explicit game action.

Do not add other Rootkit powers.

---

# 14. Sabotage

The bad team gets **2 sabotage uses per game**.

```python
MAX_SABOTAGES = 2
```

This is a shared bad-team resource.

The sabotage effect is:

> Select a player whose messages will be jumbled during the following night.

Flow:

```text
Bad team uses sabotage
        ↓
Select target
        ↓
Consume one use
        ↓
Store target + affected night
        ↓
Following night:
target's chat messages are jumbled
```

The server should perform the jumbling before broadcasting the affected player's message.

Do not create additional sabotage effects.

---

# 15. Discussion

After the night cycle, there is a discussion period.

Keep its duration configurable:

```python
DISCUSSION_DURATION = ...
```

Normal chat continues to work.

**The existing `/whisper` functionality must remain unchanged.**

Do not remove or replace `/whisper` while implementing the game loop.

---

# 16. Voting

After discussion, the voting phase begins.

Keep the duration configurable:

```python
VOTING_DURATION = ...
```

Use the existing `/vote` command/system.

The server collects and validates votes and determines the eliminated player.

The eliminated player:

- remains connected
- can still see/observe the game
- cannot participate normally
- their normal messages are no longer broadcast to the whole server

Do not invent additional dead-player mechanics.

---

# 17. Phase Manager

Implement an explicit server-side phase/state manager.

It should manage:

- current phase
- day/night number
- timers
- task completion checks
- phase transitions
- which actions are currently allowed

Do not scatter phase-management logic throughout networking functions.

The desired flow is:

```text
Lobby
  ↓
Day
  ↓
Night
  ↓
Discussion
  ↓
Voting
  ↓
Day
```

---

# 18. Win Conditions

Add a server-side win-condition system.

Check after important state changes such as:

- death
- elimination
- revival
- phase transitions

The system should support checking whether:

- the bad team has been eliminated
- the good team has been reduced to the point where the bad team wins

Keep win-condition logic separate from networking.

Do not invent additional win conditions.

---

# 19. Networking Rules

Keep the existing server-authoritative architecture.

Client:

```text
request
  ↓
server
  ↓
validate
  ↓
modify game state
  ↓
send result
```

The client must not directly decide:

- role
- death
- task completion
- room state
- audit entries
- kill success
- revive success
- sabotage availability
- vote result

---

# 20. Existing Functionality That MUST NOT Be Removed

Before changing anything, inspect and preserve the existing implementation.

### Networking
- TCP server
- TCP client
- UDP discovery
- newline-delimited JSON
- TCP receive buffering
- send locking
- receive locking
- connection handling

### Players
- `players`
- `connections`
- player IDs
- join/leave

### Chat
- normal chat
- broadcast

### Whisper
- `/whisper`
- target lookup
- private delivery
- whisper confirmation
- invalid-player error

### Rooms
- `"Room"` player state
- `/move`
- room validation
- room-change messages

### Minigames
- existing minigame files
- existing minigame implementations

### Host/client
- host mode
- client mode
- LAN discovery
- game start

**Do not replace these with a new implementation just to build the game system.**

---

# 21. Suggested Implementation Order

Build incrementally:

1. Add game state and phase manager.
2. Add configurable day/night/discussion/voting timers.
3. Integrate existing minigames with existing rooms.
4. Add task assignment and completion tracking.
5. Add audit logging to successful room changes.
6. Add role assignment.
7. Integrate Virus kill.
8. Add Detective audit inspection.
9. Add dead-player audit leak.
10. Add Antivirus revive.
11. Add Rootkit audit manipulation.
12. Add the two-use sabotage system.
13. Finish discussion/voting.
14. Add eliminated-player behavior.
15. Add win conditions.
16. Test the complete day/night loop with multiple LAN clients.

---

# 22. Testing Requirements

Test all existing functionality after each major change.

### Rooms
- valid movement
- invalid movement
- moving to current room
- audit entry creation
- timestamps

### Tasks
- correct room
- minigame launches
- successful minigame
- failed minigame
- task count
- day ends when all required tasks are complete
- day ends when timer expires

### Roles
- role assignment
- role privacy
- role-specific permissions

### Virus
- valid kill
- failed kill minigame
- invalid target
- death state

### Detective
- audit inspection
- private audit response
- dead-player audit leak

### Rootkit
- audit modification
- modified audit is what investigators see

### Antivirus
- successful revive
- second revive rejected

### Sabotage
- exactly two uses
- use count decreases
- third use rejected
- target's messages are jumbled during the following night
- messages return to normal afterward

### Voting
- votes collected
- elimination
- eliminated player remains connected
- eliminated player's normal messages are not broadcast
- eliminated player can observe the game

### Full loop

```text
Lobby
 → Day
 → Night
 → Discussion
 → Voting
 → Day
```

Verify the game eventually ends when a win condition is met.

---

# 23. CRITICAL DEVELOPMENT RULE

**Do not destroy working code to implement the game.**

For every feature:

1. Inspect the existing implementation.
2. Reuse existing functions.
3. Add only the required state.
4. Integrate with the current JSON message protocol.
5. Preserve existing commands.
6. Preserve existing minigames.
7. Test old functionality after changes.

If implementing a new architecture would require rewriting the existing networking code, **do not do it**. Integrate incrementally instead.

The current networking implementation is the foundation of the project. The objective is to build the actual Process Mafia game on top of it.
