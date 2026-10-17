# State & Score Broadcast Sequence Diagram
 
```mermaid
sequenceDiagram
    participant A as Active Player
    participant S as Server Handler Thread
    participant R as Room State
    participant B as Other Player
 
    A->>S: MOVE with guess_type and guess
    S->>R: acquire state_lock
    alt Not active player or bad guess or repeated letter
        S-->>A: ERROR OUT_OF_TURN or INVALID_GUESS or ALREADY_GUESSED
        Note over S,B: No state change. No broadcast. Turn does not switch.
    else Legal move
        S->>R: check guess against sender word
        S->>R: update masked_word, wrong_letters, wrong_remaining, status
        S->>R: switch active_player and skip ELIMINATED
        Note over S,B: Same STATE_UPDATE sent to all clients while lock is held
        S-->>A: STATE_UPDATE with last_move, players, active_player
        S-->>B: STATE_UPDATE with last_move, players, active_player
    end
    S->>R: release state_lock
```
 
**Narrative:** the server handler takes `state_lock` and validates the move: it must come from the active player and be a legal, not-yet-guessed guess. An invalid move gets an `ERROR` to the sender only, so nothing changes and the turn stays put. A legal move is checked against the sender's own word, and the server updates the sender's masked word, wrong letters, `wrong_remaining`, and `status`. It then switches `active_player` to the opponent, skipping any `ELIMINATED` player. Finally it sends the same `STATE_UPDATE` to both clients back-to-back while still holding the lock. Both players therefore receive identical turn and score information (`wrong_remaining`, `status`, `active_player`) from the same moment, and no other thread can change the state in between.
