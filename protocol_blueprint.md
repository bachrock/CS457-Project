## Overview Table
| Message Type | Direction | Purpose & Description |
|---|---|---|
| CONNECT | Client -> Server | Client requests to join game room with player alias. |
| LOBBY_WAIT | Server -> Client  | Server notifies Client 1 that it is waiting for Player 2 to connect. |
| GAME_START | Server -> Clients | Server notifies both clients that game has started, assigns roles, and tells the clients the word lengths (Player 1 / Player 2). |
| MOVE | Client -> Server | Active player submits a letter or word guess. |
| STATE_UPDATE| Server -> Clients | Server broadcasts updated masked word, incorrectly guessed letters, remaining guesses, and active player turn. |
| ERROR	| Server -> Client | Server notifies client of out-of-turn move or malformed message. |
| DISCONNECT | Client -> Server | Client notifies server of intentional departure/quit. |
| GAME_OVER	| Server -> Clients | Server broadcasts final game outcome (Winner / No Winner / Forfeit). |

<br>

## Framing Rule: Newline-Delimited JSON (\n Framing)
Framing Rule: Every JSON object is UTF-8 encoded and terminated by a newline character \n (0x0A). The receiver accumulates incoming bytes into a stream buffer until a \n is encountered, extracts the complete line, and deserializes the JSON object.
- Max message size is 1024 bytes.
- Empty lines are ignored.
- Invalid JSON gets an `ERROR` response with code `MALFORMED_MESSAGE`.

### Wire Stream Example (coalescing)

Two messages arriving together in a single `recv()` chunk:

```
{"msg_type":"MOVE","player_id":"Alice","payload":{"guess_type":"LETTER","guess":"e"},"timestamp":1727000015}\n{"msg_type":"MOVE","player_id":"Alice","payload":{"guess_type":"LETTER","guess":"a"},"timestamp":1727000016}\n
```

### Wire Stream Example (fragmentation)

One message split across two `recv()` chunks:

```
chunk 1: {"msg_type":"MOVE","player_id":"Al
chunk 2: ice","payload":{"guess_type":"LETTER","guess":"e"},"timestamp":1727000015}\n
```

The receiver keeps chunk 1 in its buffer and waits. It only parses once the `\n` arrives in chunk 2.

### Wire Stream Example (continuous stream, mixed message types)

What the server's socket might receive over a few seconds, as one unbroken stream of bytes:

```
{"msg_type":"CONNECT","player_id":"Alice","payload":{},"timestamp":1727000000}\n{"msg_type":"MOVE","player_id":"Alice","payload":{"guess_type":"LETTER","guess":"e"},"timestamp":1727000015}\n{"msg_type":"MOVE","player_id":"Alice","payload":{"guess_type":"WORD","guess":"planet"},"timestamp":1727000021}\n{"msg_type":"DISCONNECT","player_id":"Alice","payload":{"reason":"QUIT"},"timestamp":1727000030}\n
```

The receiver extracts four messages by splitting on each `\n`:

| # | Extracted message | `msg_type` |
|---|---|---|
| 1 | `{"msg_type":"CONNECT",...}` | CONNECT |
| 2 | `{"msg_type":"MOVE",...,"guess":"e",...}` | MOVE |
| 3 | `{"msg_type":"MOVE",...,"guess":"planet",...}` | MOVE |
| 4 | `{"msg_type":"DISCONNECT",...}` | DISCONNECT |

<br>

## Global Message Envelope
| Field | Type | Rules |
|---|---|---|
| `msg_type` | string | Uppercase only. Must be one of: `CONNECT`, `LOBBY_WAIT`, `GAME_START`, `MOVE`, `STATE_UPDATE`, `ERROR`, `DISCONNECT`, `GAME_OVER`. |
| `player_id` | string | Sender's alias. 1-12 characters: letters, numbers, and underscore are allowed. Server messages use `"SERVER"`. |
| `payload` | object | Always present. Contents depend on `msg_type`. Use `{}` when the message has no data. |
| `timestamp` | integer | Time in seconds |

<br>

## Complete Field Specifications by Message Type

### CONNECT (Client -> Server)
Sent once, right after the TCP connection opens. The client requests to join the game using the alias in `player_id`.

| Payload field | Type | Rules |
|---|---|---|
| *(none)* | | `payload` is an empty object `{}` |

```json
{"msg_type":"CONNECT","player_id":"Alice","payload":{},"timestamp":1727000000}
```

Server behavior:
- Alias invalid or already taken: server sends `ERROR` and closes the connection.
- First valid player: server replies with `LOBBY_WAIT`.
- Second valid player: server sends `GAME_START` to both.

### LOBBY_WAIT (Server -> Client)

Sent only to the first player to connect, while the server waits for an opponent.

| Payload field | Type | Rules |
|---|---|---|
| `message` | string | Human-readable status text |

```json
{"msg_type":"LOBBY_WAIT","player_id":"SERVER","payload":{"message":"Waiting for Player 2 to connect..."},"timestamp":1727000001}
```

### GAME_START (Server -> Clients)

Sent to each client separately (so `your_role` is different for each). Each player gets their own secret word; the clients only learn the lengths.

| Payload field | Type | Rules |
|---|---|---|
| `your_role` | string | `"Player_1"` or `"Player_2"` |
| `opponent` | string | Opponent's alias |
| `word_length` | integer | Length of this client's word |
| `opponent_word_length` | integer | Length of the opponent's word |
| `max_wrong_guesses` | integer | Always `5` |
| `active_player` | string | Alias of the player who moves first (Player 1) |

```json
{"msg_type":"GAME_START","player_id":"SERVER","payload":{"your_role":"Player_1","opponent":"Bob","word_length":6,"opponent_word_length":8,"max_wrong_guesses":5,"active_player":"Alice"},"timestamp":1727000010}
```

### MOVE (Client -> Server)

The active player guesses either one letter or the whole word. Guesses are checked against the sender's own word.

| Payload field | Type | Rules |
|---|---|---|
| `guess_type` | string | `"LETTER"` or `"WORD"` |
| `guess` | string | If `LETTER`: exactly 1 character, a-z. If `WORD`: 3-12 characters, a-z |

```json
{"msg_type":"MOVE","player_id":"Alice","payload":{"guess_type":"LETTER","guess":"e"},"timestamp":1727000015}
```

Rules:
- A wrong letter costs 1 wrong guess. A wrong word guess has no penalty.
- A correct word guess wins the game.
- Repeating a letter already guessed is rejected with `ERROR` (no penalty, turn does not pass).

### STATE_UPDATE (Server -> Clients)

Broadcast to both clients after every accepted move.

| Payload field | Type | Rules |
|---|---|---|
| `last_move` | object | `{"player_id": string, "guess_type": string, "guess": string, "correct": boolean}` |
| `players` | object | One entry per alias (see below) |
| `active_player` | string | Alias of the player who moves next |

Each entry in `players` has:

| Field | Type | Rules |
|---|---|---|
| `masked_word` | string | Revealed letters with `_` for unknown ones, e.g. `"_a__ma_"` |
| `wrong_letters` | array of strings | Wrong letters guessed so far |
| `wrong_remaining` | integer | 0-5 |
| `status` | string | `"ACTIVE"` or `"ELIMINATED"` |

```json
{"msg_type":"STATE_UPDATE","player_id":"SERVER","payload":{"last_move":{"player_id":"Alice","guess_type":"LETTER","guess":"e","correct":false},"players":{"Alice":{"masked_word":"______","wrong_letters":["e"],"wrong_remaining":4,"status":"ACTIVE"},"Bob":{"masked_word":"________","wrong_letters":[],"wrong_remaining":5,"status":"ACTIVE"}},"active_player":"Bob"},"timestamp":1727000016}
```

If a player is `ELIMINATED`, the server skips their turns.

### ERROR (Server -> Client)

Sent only to the client that caused the problem. Game state does not change.

| Payload field | Type | Rules |
|---|---|---|
| `code` | string | One of the codes below |
| `detail` | string | Human-readable explanation |

| Code | When it happens |
|---|---|
| `MALFORMED_MESSAGE` | Bad JSON, missing envelope field, or wrong field type |
| `UNKNOWN_MSG_TYPE` | `msg_type` is not one of the 8 types |
| `INVALID_STATE` | Valid message at the wrong time (e.g. `MOVE` before `GAME_START`) |
| `OUT_OF_TURN` | `MOVE` from the player who is not active |
| `INVALID_GUESS` | Letter is not a single a-z character, or word has non-letters / bad length |
| `ALREADY_GUESSED` | Letter was already guessed |
| `DUPLICATE_ALIAS` | Alias already in use (connection is closed) |
| `ROOM_FULL` | Two players already connected (connection is closed) |

```json
{"msg_type":"ERROR","player_id":"SERVER","payload":{"code":"OUT_OF_TURN","detail":"It is Bob's turn."},"timestamp":1727000017}
```

### DISCONNECT (Client -> Server)

The client tells the server it is quitting, then closes its socket.

| Payload field | Type | Rules |
|---|---|---|
| `reason` | string | Optional. Max 50 characters. Defaults to `"QUIT"` |

```json
{"msg_type":"DISCONNECT","player_id":"Bob","payload":{"reason":"QUIT"},"timestamp":1727000030}
```

The server does not reply to the sender. See the forfeit rules below.

### GAME_OVER (Server -> Clients)

Broadcast once when the game ends. The server then closes both sockets.

| Payload field | Type | Rules |
|---|---|---|
| `result` | string | `"WIN"`, `"NO_WINNER"`, or `"FORFEIT"` |
| `winner` | string or null | Winner's alias, or `null` if `NO_WINNER` |
| `reason` | string | `"WORD_GUESSED"`, `"BOTH_ELIMINATED"`, `"OPPONENT_QUIT"`, or `"OPPONENT_DROPPED"` |
| `words` | object | Both secret words revealed, e.g. `{"Alice":"planet","Bob":"keyboard"}` |

```json
{"msg_type":"GAME_OVER","player_id":"SERVER","payload":{"result":"WIN","winner":"Alice","reason":"WORD_GUESSED","words":{"Alice":"planet","Bob":"keyboard"}},"timestamp":1727000040}
```

## Disconnect and Forfeit Handling

A player can leave in three ways:

| Event | How the server detects it | `reason` in GAME_OVER |
|---|---|---|
| Player sends `DISCONNECT` | Receives the message | `OPPONENT_QUIT` |
| Player closes the socket without `DISCONNECT` | `recv()` returns `b""` (EOF) | `OPPONENT_DROPPED` |
| Crash or network failure | `ConnectionResetError`, `BrokenPipeError`, or `TimeoutError` | `OPPONENT_DROPPED` |

What the server does:
- **In the lobby (waiting for opponent):** remove the player and keep waiting. No `GAME_OVER`.
- **During the game:** the remaining player wins by forfeit. The server sends `GAME_OVER` with `result: "FORFEIT"` and closes both sockets. This applies even if it was not the leaving player's turn.
- **After the game is over:** ignore it and close the socket.
