# AI Prompting & Constraint Strategy

This document outlines the system prompts, architectural constraints, and strategy used to guide AI tools in generating schema-compliant parser and serialization functions for the Hangman game protocol.

---

## 1. Protocol Schema Reference

All generated code must adhere strictly to the following message types and structure.

| Message Type | Direction | Payload Schema |
| :--- | :--- | :--- |
| `CONNECT` | Client $\rightarrow$ Server | `{ type: "CONNECT", playerAlias: string }` |
| `LOBBY_WAIT` | Server $\rightarrow$ Client | `{ type: "LOBBY_WAIT", message: string }` |
| `GAME_START` | Server $\rightarrow$ Clients | `{ type: "GAME_START", wordLength: number, activePlayer: string, playerRole: "P1" \| "P2" }` |
| `MOVE` | Client $\rightarrow$ Server | `{ type: "MOVE", guessType: "LETTER" \| "WORD", value: string }` |
| `STATE_UPDATE` | Server $\rightarrow$ Clients | `{ type: "STATE_UPDATE", maskedWord: string, incorrectGuesses: string[], remainingGuesses: number, activePlayer: string }` |
| `ERROR` | Server $\rightarrow$ Client | `{ type: "ERROR", code: string, message: string }` |
| `DISCONNECT` | Client $\rightarrow$ Server | `{ type: "DISCONNECT", reason?: string }` |
| `GAME_OVER` | Server $\rightarrow$ Clients | `{ type: "GAME_OVER", result: "WIN" \| "LOSS" \| "FORFEIT", winnerAlias?: string, targetWord: string }` |

---

## 2. System Prompt Master Template

Use the system prompt below when prompting AI code generators to implement parser/serialization functions.

> **System Prompt Directive:**
> You are a strict TypeScript/JSON protocol engineer. Your sole task is to implement serialization and parsing functions for a two-player online Hangman game protocol.
>
> **Strict Guardrails & Rules:**
> 1. **No Schema Drift:** Do NOT invent, remove, or modify message type names, payload fields, or property casing.
> 2. **Explicit Validation:** All parsing functions MUST validate incoming payloads against the exact schema (e.g., check that string types are non-empty, numbers are integers, and enum types match allowed values).
> 3. **Error Handling:** If an incoming message fails parsing or validation, return/throw a structured `MalformedMessageError` containing details about the exact missing or invalid field.
> 4. **No Side Effects:** Parser and serializer utilities must be pure, deterministic functions without internal state.
> 5. **Output Constraints:** Return ONLY valid, fully typed, production-ready implementation code with runtime type guards. Do not include placeholder comments like `// TODO: handle rest of fields`.

---

## 3. Concrete AI Execution Prompts

### Prompt 1: Serialization Functions
**Goal:** Generate client and server serialization utilities that produce valid JSON payloads matching the schema.

```text
Act as a TypeScript protocol engineer. 

Generate typed serializer functions for each message type in our spec:
1. serializeConnect(playerAlias: string): string
2. serializeMove(guessType: 'LETTER' | 'WORD', value: string): string
3. serializeStateUpdate(payload: StateUpdatePayload): string
4. serializeGameOver(payload: GameOverPayload): string

Requirements:
- Input arguments must be strongly typed.
- Enforce lowercase/uppercase validation on 'value' (letters converted to lowercase).
- Output must be a deterministic JSON string matching our schema definitions.
```

### Prompt 2: Parser & Runtime Validation Functions
**Goal:** Generate inbound message parsers that process raw JSON strings and safely narrow them to discriminated union types.

```text
Act as a TypeScript protocol engineer. 

Implement a master message parser function: `parseInboundMessage(rawJson: string): GameMessage`.

Requirements:
1. Parse the raw JSON string safely.
2. Inspect the top-level `type` field using a discriminated switch/case handler.
3. Validate all fields per message type:
   - `CONNECT`: `playerAlias` must be a non-empty string between 1-20 characters.
   - `MOVE`: `guessType` must be strictly "LETTER" or "WORD". If "LETTER", `value` must be a single alphabetic character.
   - `STATE_UPDATE`: `remainingGuesses` must be an integer between 0 and 6.
4. If validation fails for any field, throw a `MalformedMessageError(fieldType, reason)`.
5. Return the narrow TypeScript discriminated union type `GameMessage`.
```

---

## 4. Verification & Testing Prompt

**Goal:** Ensure the AI-generated parsers pass boundary testing and handle edge cases.

```text
Act as a QA Automation Engineer. Write unit tests using Jest/Vitest for the generated `parseInboundMessage` function.

Cover the following test cases:
1. Valid messages for all 8 message types parse successfully.
2. Malformed JSON string throws `MalformedMessageError`.
3. Unknown or missing `type` property throws invalid message error.
4. A `MOVE` message with a letter string length > 1 (e.g., "AB") is correctly rejected.
5. Negative `remainingGuesses` values in `STATE_UPDATE` are rejected.
```
