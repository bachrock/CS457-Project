import argparse
import json
import random
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

MAX_MSG_SIZE = 1024
MAX_WRONG_GUESSES = 5
TURN_TIMEOUT_SEC = 60.0
SEND_TIMEOUT_SEC = 5.0

ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,12}$")
WORD_PATTERN = re.compile(r"^[a-z]{3,12}$")
LETTER_PATTERN = re.compile(r"^[a-z]$")

VALID_MSG_TYPES = {
    "CONNECT",
    "LOBBY_WAIT",
    "GAME_START",
    "MOVE",
    "STATE_UPDATE",
    "ERROR",
    "DISCONNECT",
    "GAME_OVER",
}

WORD_BANK = [
    "planet", "socket", "router", "packet", "python", "binary",
    "bridge", "cipher", "client", "server", "stream", "thread",
    "kernel", "vector", "matrix", "beacon", "signal", "switch",
]


def build_message(msg_type: str, player_id: str, payload: Optional[Dict[str, Any]] = None) -> bytes:
    """Serialize a protocol envelope into UTF-8 newline-delimited JSON bytes."""
    envelope = {
        "msg_type": msg_type,
        "player_id": player_id,
        "payload": payload if payload is not None else {},
        "timestamp": int(time.time()),
    }
    return (json.dumps(envelope, separators=(",", ":")) + "\n").encode("utf-8")


def extract_lines(buffer: bytearray) -> Tuple[List[str], bool]:
    """
    Extract complete newline-delimited lines from a mutable bytearray buffer.
    Clears the buffer and flags overflowed=True if a frame exceeds 1024 bytes.
    """
    lines: List[str] = []
    while True:
        newline_idx = buffer.find(b"\n")
        if newline_idx == -1:
            if len(buffer) > MAX_MSG_SIZE:
                buffer.clear()
                return lines, True
            break

        if newline_idx + 1 > MAX_MSG_SIZE:
            buffer.clear()
            return lines, True

        raw_line = bytes(buffer[:newline_idx])
        del buffer[: newline_idx + 1]

        if not raw_line.strip():
            continue

        lines.append(raw_line.decode("utf-8", errors="replace"))

    return lines, False


def parse_envelope(raw_line: str) -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[str, str]]]:
    """Validate top-level JSON envelope fields per protocol_blueprint.md."""
    try:
        data = json.loads(raw_line)
    except json.JSONDecodeError:
        return None, ("MALFORMED_MESSAGE", "Invalid JSON syntax.")

    if not isinstance(data, dict):
        return None, ("MALFORMED_MESSAGE", "Message root must be a JSON object.")

    required_fields = {"msg_type", "player_id", "payload", "timestamp"}
    if not required_fields.issubset(data.keys()):
        return None, ("MALFORMED_MESSAGE", "Missing required envelope field(s).")

    if (
        not isinstance(data["msg_type"], str)
        or not isinstance(data["player_id"], str)
        or not isinstance(data["payload"], dict)
        or not isinstance(data["timestamp"], int)
        or isinstance(data["timestamp"], bool)
    ):
        return None, ("MALFORMED_MESSAGE", "Invalid envelope field type(s).")

    if data["msg_type"] not in VALID_MSG_TYPES:
        return None, ("UNKNOWN_MSG_TYPE", f"Unknown msg_type: {data['msg_type']}")

    return data, None


@dataclass
class ClientSession:
    sock: socket.socket
    addr: tuple
    alias: Optional[str] = None
    role: Optional[str] = None
    secret_word: str = ""
    guessed_letters: Set[str] = field(default_factory=set)
    wrong_letters: List[str] = field(default_factory=list)
    wrong_remaining: int = MAX_WRONG_GUESSES
    status: str = "ACTIVE"  # "ACTIVE" or "ELIMINATED"
    alive: bool = True

    def masked_word(self) -> str:
        return "".join(ch if ch in self.guessed_letters else "_" for ch in self.secret_word)

    def to_player_state(self) -> dict:
        return {
            "masked_word": self.masked_word(),
            "wrong_letters": list(self.wrong_letters),
            "wrong_remaining": self.wrong_remaining,
            "status": self.status,
        }


class GameRoom:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.phase: str = "WAITING_FOR_PLAYERS"
        self.players: List[ClientSession] = []
        self.active_player: Optional[str] = None

    def _send_raw(self, client: ClientSession, data: bytes) -> bool:
        """Send bytes to a single client while state_lock is held."""
        if not client.alive:
            return False
        prev_timeout = client.sock.gettimeout()
        try:
            client.sock.settimeout(SEND_TIMEOUT_SEC)
            client.sock.sendall(data)
            return True
        except OSError:
            return False
        finally:
            try:
                client.sock.settimeout(prev_timeout)
            except OSError:
                pass

    def _send_error(self, client: ClientSession, code: str, detail: str) -> None:
        msg = build_message("ERROR", "SERVER", {"code": code, "detail": detail})
        self._send_raw(client, msg)

    def _close_socket_quietly(self, client: ClientSession) -> None:
        client.alive = False
        try:
            client.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client.sock.close()
        except OSError:
            pass

    def _reset_room_locked(self) -> None:
        """CLEANUP state: close sockets, wipe room state, return to WAITING_FOR_PLAYERS."""
        for p in self.players:
            self._close_socket_quietly(p)
        self.players.clear()
        self.active_player = None
        self.phase = "WAITING_FOR_PLAYERS"
        print("[ROOM] Cleanup complete. State -> WAITING_FOR_PLAYERS")

    def _trigger_game_over_locked(self, result: str, winner: Optional[str], reason: str) -> None:
        """Broadcast GAME_OVER with revealed words and transition through CLEANUP."""
        self.phase = "GAME_OVER"
        words_map = {p.alias: p.secret_word for p in self.players if p.alias}
        payload = {
            "result": result,
            "winner": winner,
            "reason": reason,
            "words": words_map,
        }
        msg = build_message("GAME_OVER", "SERVER", payload)
        for p in self.players:
            if p.alive:
                self._send_raw(p, msg)
        print(f"[ROOM] GAME_OVER ({result}, winner={winner}, reason={reason})")
        self._reset_room_locked()

    def _pick_shared_word(self) -> str:
        return random.choice(WORD_BANK)

    def _next_active_player_locked(self) -> Optional[str]:
        """Switch active_player to opponent, skipping any ELIMINATED player."""
        if len(self.players) < 2:
            return None
        p1, p2 = self.players[0], self.players[1]
        current = p1 if self.active_player == p1.alias else p2
        other = p2 if current is p1 else p1

        if other.status == "ACTIVE":
            return other.alias
        if current.status == "ACTIVE":
            return current.alias
        return None

    def handle_message(self, client: ClientSession, msg: dict) -> bool:
        """
        Process a parsed message inside state_lock.
        Returns False if this client's socket should terminate its loop.
        """
        with self.lock:
            if not client.alive:
                return False

            msg_type = msg["msg_type"]
            sender_id = msg["player_id"]
            payload = msg["payload"]

            # 1. Pre-registration (CONNECT expected)
            if client.alias is None:
                if msg_type != "CONNECT":
                    self._send_error(client, "INVALID_STATE", "Must send CONNECT first.")
                    self._close_socket_quietly(client)
                    return False

                if self.phase not in ("WAITING_FOR_PLAYERS", "LOBBY_WAIT") or len(self.players) >= 2:
                    self._send_error(client, "ROOM_FULL", "Game room already has two players.")
                    self._close_socket_quietly(client)
                    return False

                if not ALIAS_PATTERN.match(sender_id):
                    self._send_error(
                        client,
                        "MALFORMED_MESSAGE",
                        "Alias must be 1-12 chars (letters, digits, underscore).",
                    )
                    self._close_socket_quietly(client)
                    return False

                if any(p.alias == sender_id for p in self.players):
                    self._send_error(client, "DUPLICATE_ALIAS", f"Alias '{sender_id}' is already in use.")
                    self._close_socket_quietly(client)
                    return False

                # Register valid player
                client.alias = sender_id
                if len(self.players) == 0:
                    client.role = "Player_1"
                    self.players.append(client)
                    self.phase = "LOBBY_WAIT"
                    lobby_msg = build_message(
                        "LOBBY_WAIT",
                        "SERVER",
                        {"message": "Waiting for Player 2 to connect..."},
                    )
                    self._send_raw(client, lobby_msg)
                    print(f"[LOBBY] Player_1 '{client.alias}' joined from {client.addr}")
                    return True
                else:
                    client.role = "Player_2"
                    self.players.append(client)
                    self.phase = "GAME_START"

                    p1, p2 = self.players[0], self.players[1]
                    shared_word = self._pick_shared_word()
                    p1.secret_word = shared_word
                    p2.secret_word = shared_word
                    self.active_player = p1.alias

                    start_p1 = build_message(
                        "GAME_START",
                        "SERVER",
                        {
                            "your_role": "Player_1",
                            "opponent": p2.alias,
                            "word_length": len(p1.secret_word),
                            "opponent_word_length": len(p2.secret_word),
                            "max_wrong_guesses": MAX_WRONG_GUESSES,
                            "active_player": p1.alias,
                        },
                    )
                    start_p2 = build_message(
                        "GAME_START",
                        "SERVER",
                        {
                            "your_role": "Player_2",
                            "opponent": p1.alias,
                            "word_length": len(p2.secret_word),
                            "opponent_word_length": len(p1.secret_word),
                            "max_wrong_guesses": MAX_WRONG_GUESSES,
                            "active_player": p1.alias,
                        },
                    )

                    ok1 = self._send_raw(p1, start_p1)
                    ok2 = self._send_raw(p2, start_p2)
                    if not (ok1 and ok2):
                        survivor = p2.alias if not ok1 else p1.alias
                        self._trigger_game_over_locked("FORFEIT", survivor, "OPPONENT_DROPPED")
                        return False

                    self.phase = "PLAYER_TURN"
                    print(f"[GAME] Started: {p1.alias} vs {p2.alias} | Shared word: '{shared_word}'")
                    return True

            # 2. Registered player envelope identity check
            if sender_id != client.alias:
                self._send_error(client, "MALFORMED_MESSAGE", "player_id does not match registered alias.")
                return True

            # 3. Graceful DISCONNECT
            if msg_type == "DISCONNECT":
                self._handle_disconnect_locked(client, reason="OPPONENT_QUIT")
                return False

            # 4. State check for MOVE
            if msg_type != "MOVE" or self.phase != "PLAYER_TURN":
                self._send_error(client, "INVALID_STATE", f"Cannot process {msg_type} in state {self.phase}.")
                return True

            # 5. Turn check
            if self.active_player != client.alias:
                self._send_error(client, "OUT_OF_TURN", f"It is {self.active_player}'s turn.")
                return True

            # 6. Guess validation
            guess_type = payload.get("guess_type")
            guess = payload.get("guess")
            if not isinstance(guess_type, str) or not isinstance(guess, str):
                self._send_error(client, "INVALID_GUESS", "guess_type and guess must be strings.")
                return True

            if guess_type == "LETTER":
                if not LETTER_PATTERN.match(guess):
                    self._send_error(client, "INVALID_GUESS", "LETTER guess must be a single lowercase a-z character.")
                    return True
                if guess in client.guessed_letters:
                    self._send_error(client, "ALREADY_GUESSED", f"Letter '{guess}' was already guessed.")
                    return True
            elif guess_type == "WORD":
                if not WORD_PATTERN.match(guess) or len(guess) != len(client.secret_word):
                    self._send_error(
                        client,
                        "INVALID_GUESS",
                        f"WORD guess must be {len(client.secret_word)} lowercase a-z characters.",
                    )
                    return True
            else:
                self._send_error(client, "INVALID_GUESS", "guess_type must be 'LETTER' or 'WORD'.")
                return True

            # 7. EVALUATE_MOVE (Shared board state across both players)
            self.phase = "EVALUATE_MOVE"
            won_by_word = False

            if guess_type == "LETTER":
                for p in self.players:
                    p.guessed_letters.add(guess)
                if guess in client.secret_word:
                    correct = True
                    if "_" not in client.masked_word():
                        won_by_word = True
                else:
                    correct = False
                    for p in self.players:
                        p.wrong_letters.append(guess)
                        p.wrong_remaining -= 1
                        if p.wrong_remaining <= 0:
                            p.wrong_remaining = 0
                            p.status = "ELIMINATED"
            else:  # WORD
                if guess == client.secret_word:
                    correct = True
                    won_by_word = True
                    for p in self.players:
                        p.guessed_letters.update(p.secret_word)
                else:
                    correct = False  # Wrong word guess has no penalty

            next_active = self._next_active_player_locked()
            self.active_player = next_active

            players_snapshot = {p.alias: p.to_player_state() for p in self.players if p.alias}
            update_msg = build_message(
                "STATE_UPDATE",
                "SERVER",
                {
                    "last_move": {
                        "player_id": client.alias,
                        "guess_type": guess_type,
                        "guess": guess,
                        "correct": correct,
                    },
                    "players": players_snapshot,
                    "active_player": next_active if next_active else "",
                },
            )

            for p in self.players:
                if not self._send_raw(p, update_msg):
                    survivor = next((o.alias for o in self.players if o is not p), None)
                    self._trigger_game_over_locked("FORFEIT", survivor, "OPPONENT_DROPPED")
                    return False

            if won_by_word:
                self._trigger_game_over_locked("WIN", client.alias, "WORD_GUESSED")
                return False

            if all(p.status == "ELIMINATED" for p in self.players):
                self._trigger_game_over_locked("NO_WINNER", None, "BOTH_ELIMINATED")
                return False

            self.phase = "PLAYER_TURN"
            return True

    def send_malformed_error(self, client: ClientSession, code: str, detail: str) -> None:
        with self.lock:
            if client.alive:
                self._send_error(client, code, detail)

    def handle_disconnect(self, client: ClientSession, reason: str = "OPPONENT_DROPPED") -> None:
        with self.lock:
            self._handle_disconnect_locked(client, reason)

    def _handle_disconnect_locked(self, client: ClientSession, reason: str) -> None:
        if not client.alive:
            return

        if client not in self.players:
            self._close_socket_quietly(client)
            return

        if self.phase == "LOBBY_WAIT":
            print(f"[LOBBY] Player_1 '{client.alias}' left lobby.")
            self._close_socket_quietly(client)
            self.players.clear()
            self.active_player = None
            self.phase = "WAITING_FOR_PLAYERS"
            return

        if self.phase in ("GAME_START", "PLAYER_TURN", "EVALUATE_MOVE"):
            survivor = next((p.alias for p in self.players if p is not client and p.alive), None)
            self._trigger_game_over_locked("FORFEIT", survivor, reason)
            return

        self._close_socket_quietly(client)

    def is_active_player(self, client: ClientSession) -> bool:
        with self.lock:
            return (
                client.alive
                and self.phase == "PLAYER_TURN"
                and client.alias is not None
                and self.active_player == client.alias
            )


def client_handler(room: GameRoom, client: ClientSession) -> None:
    buffer = bytearray()
    try:
        while client.alive:
            is_turn_wait = client.alias is None or room.is_active_player(client)
            timeout_sec = TURN_TIMEOUT_SEC if is_turn_wait else 1.0
            client.sock.settimeout(timeout_sec)

            try:
                chunk = client.sock.recv(1024)
            except socket.timeout:
                if timeout_sec == TURN_TIMEOUT_SEC:
                    print(f"[TIMEOUT] Client {client.alias or client.addr} timed out (60s).")
                    room.handle_disconnect(client, reason="OPPONENT_DROPPED")
                    break
                continue
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
                room.handle_disconnect(client, reason="OPPONENT_DROPPED")
                break

            if not chunk:
                room.handle_disconnect(client, reason="OPPONENT_DROPPED")
                break

            buffer.extend(chunk)
            lines, overflowed = extract_lines(buffer)
            if overflowed:
                room.send_malformed_error(client, "MALFORMED_MESSAGE", "Message exceeded 1024 bytes.")
                continue

            for line in lines:
                parsed, err = parse_envelope(line)
                if err is not None:
                    code, detail = err
                    room.send_malformed_error(client, code, detail)
                    continue

                assert parsed is not None
                if not room.handle_message(client, parsed):
                    return
    finally:
        room.handle_disconnect(client, reason="OPPONENT_DROPPED")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Two-Player Hangman Duel Server")
    parser.add_argument("-p", "--port", type=int, required=True, help="Listening TCP port (binds to 0.0.0.0)")
    args = parser.parse_args()
    if not (1 <= args.port <= 65535):
        parser.error("Port must be between 1 and 65535.")
    return args


def main() -> None:
    args = parse_args()
    room = GameRoom()

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server_sock.bind(("0.0.0.0", args.port))
        server_sock.listen(5)
        server_sock.settimeout(1.0)
    except OSError as exc:
        print(f"[ERROR] Failed to bind/listen on 0.0.0.0:{args.port}: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"[SERVER] Listening on 0.0.0.0:{args.port} (State: WAITING_FOR_PLAYERS)")

    try:
        while True:
            try:
                conn, addr = server_sock.accept()
            except socket.timeout:
                continue
            session = ClientSession(sock=conn, addr=addr)
            t = threading.Thread(target=client_handler, args=(room, session), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("\n[SERVER] Shutting down.")
    finally:
        server_sock.close()


if __name__ == "__main__":
    main()