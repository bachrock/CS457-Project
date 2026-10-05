# AI Prompting & Constraint Strategy

This document shows how AI coding assistants are constrained to implement the framing, serialization, and parsing code for the Hangman protocol defined in `protocol_blueprint.md`. The blueprint is the single source of truth. Any AI output that differs from it is rejected.

## 1. Constraint Strategy

1. **Paste the spec, never paraphrase it.** Every prompt includes the schema tables below verbatim.
2. **Closed vocabulary.** Message types, field names, enum values, and error codes are listed; the AI may not add or rename any.
3. **Small, single-purpose prompts.** One module per prompt (framing, serializers, parser), each with unit tests.
4. **Pure functions first.** Parsing and serialization have no sockets and no global state, so they can be tested without a network.
5. **Tests as the gate.** Generated code is accepted only if it passes tests derived from the blueprint's sample payloads.
6. **Drift review.** After each generation, diff field names and enums against the blueprint (checklist in section 5).

## 2. Protocol Reference (pasted into every prompt)

**Framing:** newline-delimited JSON. Each message is UTF-8 JSON on one line ended by `\n` (0x0A). Max 1024 bytes per message. Empty lines are ignored. Invalid JSON gets `ERROR` code `MALFORMED_MESSAGE`.

**Envelope (every message):**

| Field | Type | Rules |
|---|---|---|
| `msg_type` | string | One of the 8 types below, uppercase |
| `player_id` | string | 1-12 chars `[A-Za-z0-9_]`; server uses `"SERVER"` |
| `payload` | object | Always present; `{}` if empty |
| `timestamp` | integer | Unix epoch seconds |

**Messages:**

| `msg_type` | Direction | Payload |
|---|---|---|
| `CONNECT` | Client -> Server | `{}` |
| `LOBBY_WAIT` | Server -> Client | `{"message": string}` |
| `GAME_START` | Server -> Clients | `{"your_role": "Player_1"\|"Player_2", "opponent": string, "word_length": int, "opponent_word_length": int, "max_wrong_guesses": 5, "active_player": string}` |
| `MOVE` | Client -> Server | `{"guess_type": "LETTER"\|"WORD", "guess": string}` (LETTER: 1 char a-z; WORD: 3-12 chars a-z) |
| `STATE_UPDATE` | Server -> Clients | `{"last_move": {"player_id": string, "guess_type": string, "guess": string, "correct": bool}, "players": {alias: {"masked_word": string, "wrong_letters": [string], "wrong_remaining": int 0-5, "status": "ACTIVE"\|"ELIMINATED"}}, "active_player": string}` |
| `ERROR` | Server -> Client | `{"code": string, "detail": string}` |
| `DISCONNECT` | Client -> Server | `{"reason": string (optional, max 50, default "QUIT")}` |
| `GAME_OVER` | Server -> Clients | `{"result": "WIN"\|"NO_WINNER"\|"FORFEIT", "winner": string\|null, "reason": "WORD_GUESSED"\|"BOTH_ELIMINATED"\|"OPPONENT_QUIT"\|"OPPONENT_DROPPED", "words": {alias: string}}` |

**Error codes:** `MALFORMED_MESSAGE`, `UNKNOWN_MSG_TYPE`, `INVALID_STATE`, `OUT_OF_TURN`, `INVALID_GUESS`, `ALREADY_GUESSED`, `DUPLICATE_ALIAS`, `ROOM_FULL`.

## 3. System Prompt (used for every coding session)

> You are a strict Python 3 network-protocol engineer. You implement code for a two-player Hangman game over TCP using the protocol specification I paste below. The specification is final.
>
> **Rules:**
> 1. **No schema drift.** Use exactly the field names, casing, enum values, and error codes in the spec (snake_case, e.g. `msg_type`, `guess_type`, `wrong_remaining`). Never invent, rename, or remove fields or message types. Do not use camelCase.
> 2. **Framing is newline-delimited JSON.** Never assume one `recv()` equals one message. Buffer bytes, split on `b"\n"`, keep the trailing partial line, ignore empty lines, enforce the 1024-byte limit.
> 3. **EOF and errors.** `recv()` returning `b""` means the peer closed: handle it and `break`. Catch `ConnectionResetError`, `BrokenPipeError`, `ConnectionAbortedError`, and `TimeoutError` and route them to the disconnect handler.
> 4. **Validate everything.** Check envelope fields, types, ranges, and enums. On failure raise `ProtocolError(code, detail)` using only the codes in the spec.
> 5. **Pure functions.** Serializers and parsers take and return plain values; no sockets, globals, or I/O.
> 6. **No generic boilerplate.** No placeholder `TODO`s, no extra message types, no features outside the spec. If the spec is ambiguous, ask me instead of guessing.
> 7. **Output only code** (plus type hints and brief docstrings).

## 4. Task Prompts

### Prompt 1: Framing buffer

```text
[System prompt + protocol reference pasted above]

Write class FrameBuffer in framing.py:
- feed(data: bytes) -> list[bytes]: appends data, returns every complete line
  (without the trailing \n), skips empty lines, keeps any partial line buffered.
- If the buffered partial line exceeds 1024 bytes without a \n, raise
  ProtocolError("MALFORMED_MESSAGE", "message too long").
- A single feed() containing several messages must return all of them (coalescing).
- A message split across feeds must only be returned once its \n arrives (fragmentation).
Also write pytest tests using the coalescing and fragmentation wire examples from the blueprint.
```

### Prompt 2: Serializers

```text
[System prompt + protocol reference pasted above]

Write protocol.py serializer functions, one per message type, each returning
bytes (UTF-8 JSON ending in b"\n"), using the exact envelope and payload fields in the spec:
serialize_connect, serialize_lobby_wait, serialize_game_start, serialize_move,
serialize_state_update, serialize_error, serialize_disconnect, serialize_game_over.
- timestamp = int(time.time()), passed in as an optional argument for testing.
- serialize_move raises ProtocolError("INVALID_GUESS", ...) if the guess violates the MOVE rules.
- Use json.dumps(..., separators=(",", ":")). Output must fit in 1024 bytes.
```

### Prompt 3: Parser and validator

```text
[System prompt + protocol reference pasted above]

Write parse_message(line: bytes) -> dict in protocol.py:
1. Decode UTF-8 and json.loads. On failure raise ProtocolError("MALFORMED_MESSAGE", ...).
2. Validate envelope: msg_type, player_id (1-12 chars [A-Za-z0-9_] or "SERVER"),
   payload (dict), timestamp (int).
3. Unknown msg_type -> ProtocolError("UNKNOWN_MSG_TYPE", ...).
4. Validate each type's payload exactly as in the spec. For MOVE: guess_type is
   "LETTER" or "WORD"; LETTER guess is exactly one a-z char; WORD guess is 3-12 a-z chars
   (wrong shape -> "INVALID_GUESS"). Uppercase guesses are rejected, not converted.
5. Return the validated dict. Do not mutate or add fields.
Also write pytest tests using every sample JSON line from the blueprint.
```

### Prompt 4: Verification tests

```text
Write pytest tests that confirm the generated code matches the blueprint:
1. Every sample JSON message in the blueprint parses, and re-serializing it yields identical fields.
2. Malformed JSON -> MALFORMED_MESSAGE; unknown msg_type -> UNKNOWN_MSG_TYPE.
3. MOVE with guess "AB", "1", or an uppercase letter -> INVALID_GUESS.
4. Two messages in one feed() chunk are both extracted; one message split across chunks is extracted once.
```

## 5. Drift Review Checklist

- [ ] Field names are snake_case and identical to the blueprint
- [ ] Enum values match exactly (`NO_WINNER`, not `LOSS`; `Player_1`, not `P1`)
- [ ] Envelope always has all four fields
- [ ] No message type or error code outside the spec
- [ ] Framing uses a buffer and `\n` split, never one `recv()` = one message
- [ ] `b""` from `recv()` breaks the loop
- [ ] Socket exceptions are caught and routed to the disconnect handler
- [ ] Tests from Prompt 4 pass
