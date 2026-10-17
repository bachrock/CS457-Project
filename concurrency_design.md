# Concurrency Design: Two-Player Hangman Duel
 
Builds on `protocol_blueprint.md` (messages, framing, error codes) and `fsm_specification.md` (server states).
 
## 1. Concurrency Model
 
**Multi-threading:** one accept thread plus one daemon handler thread per client socket, with a single `threading.Lock` (`state_lock`) protecting all shared game state.
 
**Why:** the game has exactly two players and small messages (max 1024 bytes), so thread overhead is irrelevant. Blocking `recv()` keeps each handler a simple loop, and one lock keeps synchronization easy to reason about.
 
**Capacity:** 2 active players plus short-lived handlers for extra clients, who are rejected with `ROOM_FULL` and closed.
 
## 2. Connection Lifecycle
 
1. **Listen:** the main thread binds and listens on the TCP port (room phase `WAITING_FOR_PLAYERS`).
2. **Accept:** the accept thread calls `accept()`, creates a client object (socket, its own frame buffer, `alive` flag), and starts a handler thread. It does not touch the room.
3. **Registration:** the handler reads `CONNECT` and, under `state_lock`, validates the alias (format and uniqueness). The first valid client becomes Player_1 and gets `LOBBY_WAIT`. The second becomes Player_2 and both receive `GAME_START`. A third client gets `ROOM_FULL`, and an invalid or duplicate alias gets an error, with the connection closed.
4. **Game loop:** each handler loops on `recv()`, extracts newline-delimited messages, parses them outside the lock, then processes each message inside one critical section (Section 5). Phase is `PLAYER_TURN`.
5. **Teardown:** triggered by `GAME_OVER`, a `DISCONNECT` message, EOF (`recv()` returns `b""`), a socket error, or 60 s of silence from the active player. Under the lock the server sends `GAME_OVER` (`FORFEIT` if a player left), marks clients not alive, shuts down and closes both sockets, resets the room, and returns to `WAITING_FOR_PLAYERS`. A player leaving the lobby simply returns the room to `WAITING_FOR_PLAYERS` with no `GAME_OVER`.
## 3. Shared Resources
 
| Resource | Mutated by | Protection |
|---|---|---|
| Room phase (FSM state) | Any handler | `state_lock` |
| Registered players and aliases (client socket list) | CONNECT, disconnect, cleanup | `state_lock` |
| Secret words | Game start | `state_lock` |
| Masked words, wrong letters, `wrong_remaining`, player `status` (the round scores) | Accepted MOVE | `state_lock` |
| Active player (turn state) | Accepted MOVE | `state_lock` |
| Writes to client sockets | Any handler | `state_lock` |
 
Thread-local, needing no lock: each handler's frame buffer and its own `recv()` calls. Parsing and serialization are pure functions with no shared state.
 
## 4. Lock Acquisition Strategy
 
- **One lock:** `state_lock` guards the whole room and every socket send. A single lock means no lock-ordering deadlocks.
- **Critical section:** acquire with `with state_lock:` after a message is parsed. Inside it, in order: check alias, game phase, active turn, guess validity, and repeated letter; apply the move; choose the next active player (skipping `ELIMINATED`); send `STATE_UPDATE` or `GAME_OVER`. Release when the block ends, including on exceptions.
- **Never hold the lock across** `recv()`, `accept()`, `sleep()`, or `join()`. Sockets use a 5 s timeout, so a send inside the lock cannot hang.
- **Sends happen inside the lock** so that two threads cannot interleave bytes on one socket and clients always receive updates in order.
- **Idempotent teardown:** each client has an `alive` flag checked under the lock, so a second disconnect event is ignored.
- **Non-reentrant lock:** helper functions that run under the lock never try to acquire it again.
## 5. Race Conditions Prevented
 
| Scenario | Prevention |
|---|---|
| Both players send MOVE at the same instant | Turn check and state update share one critical section. The non-active player gets `OUT_OF_TURN` |
| Two clients connect with the same alias | Alias check and registration are atomic. The second gets `DUPLICATE_ALIAS` |
| Second and third clients connect together | Player-count check and role assignment are atomic. The extra client gets `ROOM_FULL`, and `GAME_START` is sent once |
| Winning guess arrives as the opponent disconnects | The first thread to take the lock ends the game, and the other event is ignored. Exactly one `GAME_OVER` |
| Both players drop together | Teardown is idempotent via the `alive` flag |
| Next round reuses an alias while old handlers unwind | Handlers act only if their own client object is still alive, never by alias alone |
