import argparse
import json
import re
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

MAX_MSG_SIZE = 1024
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,12}$")

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


class HangmanClient:
    def __init__(self, server_ip: str, port: int, alias: str) -> None:
        self.server_ip = server_ip
        self.port = port
        self.alias = alias
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.running = threading.Event()
        self.running.set()
        self.send_lock = threading.Lock()
        self.active_player: Optional[str] = None
        self.word_length: int = 0

    def connect(self) -> None:
        try:
            self.sock.settimeout(10.0)
            self.sock.connect((self.server_ip, self.port))
            self.sock.settimeout(None)
        except OSError as exc:
            print(f"[ERROR] Unable to connect to {self.server_ip}:{self.port} ({exc}).")
            sys.exit(1)

        self.send_message("CONNECT", {})

    def send_message(self, msg_type: str, payload: dict) -> bool:
        raw = build_message(msg_type, self.alias, payload)
        with self.send_lock:
            try:
                self.sock.sendall(raw)
                return True
            except OSError:
                return False

    def _print_state_update(self, payload: dict) -> None:
        last = payload.get("last_move", {})
        players = payload.get("players", {})
        self.active_player = payload.get("active_player")

        outcome = "HIT" if last.get("correct") else "MISS"
        print(
            f"\n[MOVE] {last.get('player_id')} guessed {last.get('guess_type')} "
            f"'{last.get('guess')}' -> {outcome}"
        )
        for name, pdata in players.items():
            wrong = ", ".join(pdata.get("wrong_letters", [])) or "none"
            print(
                f"  {name:12s} | Word: {pdata.get('masked_word')} | "
                f"Wrong: [{wrong}] | Remaining: {pdata.get('wrong_remaining')} | "
                f"Status: {pdata.get('status')}"
            )

        if self.active_player:
            turn_note = "(YOUR TURN)" if self.active_player == self.alias else "(Opponent's turn)"
            print(f"[TURN] Active player: {self.active_player} {turn_note}")

    def receiver_loop(self) -> None:
        buffer = bytearray()
        while self.running.is_set():
            try:
                chunk = self.sock.recv(1024)
            except OSError:
                if self.running.is_set():
                    print("\n[DISCONNECTED] Lost connection to server.")
                self.running.clear()
                break

            if not chunk:
                if self.running.is_set():
                    print("\n[DISCONNECTED] Server closed the connection.")
                self.running.clear()
                break

            buffer.extend(chunk)
            lines, overflowed = extract_lines(buffer)
            if overflowed:
                print("\n[WARN] Received oversized frame (>1024 bytes); discarded.")
                continue

            for line in lines:
                msg, err = parse_envelope(line)
                if err is not None or msg is None:
                    print(f"\n[WARN] Received malformed server frame: {err}")
                    continue

                mtype = msg["msg_type"]
                payload = msg["payload"]

                if mtype == "LOBBY_WAIT":
                    print(f"\n[LOBBY] {payload.get('message', 'Waiting for opponent...')}")

                elif mtype == "GAME_START":
                    self.active_player = payload.get("active_player")
                    self.word_length = payload.get("word_length", 0)
                    print("\n=== GAME STARTED ===")
                    print(f"Role: {payload.get('your_role')} | Opponent: {payload.get('opponent')}")
                    print(
                        f"Your word length: {self.word_length} | "
                        f"Opponent word length: {payload.get('opponent_word_length')} | "
                        f"Max wrong guesses: {payload.get('max_wrong_guesses')}"
                    )
                    turn_note = "(YOUR TURN)" if self.active_player == self.alias else "(Opponent's turn)"
                    print(f"First turn: {self.active_player} {turn_note}")

                elif mtype == "STATE_UPDATE":
                    self._print_state_update(payload)

                elif mtype == "ERROR":
                    print(f"\n[SERVER ERROR - {payload.get('code')}] {payload.get('detail')}")

                elif mtype == "GAME_OVER":
                    print("\n=== GAME OVER ===")
                    print(f"Result: {payload.get('result')} | Winner: {payload.get('winner')}")
                    print(f"Reason: {payload.get('reason')}")
                    print(f"Secret Words: {payload.get('words')}")
                    self.running.clear()
                    self.close()
                    return

    def input_loop(self) -> None:
        print("Commands: <single letter a-z> | word <fullword> | quit")
        while self.running.is_set():
            try:
                raw = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                raw = "quit"

            if not self.running.is_set():
                break
            if not raw:
                continue

            lowered = raw.lower()
            if lowered in ("quit", "exit"):
                self.send_message("DISCONNECT", {"reason": "QUIT"})
                self.running.clear()
                self.close()
                break

            if lowered.startswith("word "):
                guess_word = lowered.split(maxsplit=1)[1].strip()
                self.send_message("MOVE", {"guess_type": "WORD", "guess": guess_word})
            elif len(lowered) == 1 and lowered.isalpha():
                self.send_message("MOVE", {"guess_type": "LETTER", "guess": lowered})
            else:
                print("Invalid input. Enter a single letter (e.g., 'e'), 'word <guess>', or 'quit'.")

    def close(self) -> None:
        self.running.clear()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Two-Player Hangman Duel Client")
    parser.add_argument("-i", "--hostname", required=True, help="Server DNS hostname or IP")
    parser.add_argument("-p", "--port", type=int, required=True, help="Server TCP port")
    parser.add_argument("-u", "--username", default=None, help="Player alias (1-12 chars: A-Z, a-z, 0-9, _)")
    args = parser.parse_args()
    if not (1 <= args.port <= 65535):
        parser.error("Port must be between 1 and 65535.")
    return args


def main() -> None:
    args = parse_args()

    # 2.2 Graceful DNS Resolution Exception Handling (Mandatory Requirement)
    try:
        server_ip = socket.gethostbyname(args.hostname)
    except socket.gaierror:
        print(f"[ERROR] Unable to resolve DNS hostname '{args.hostname}'.")
        print("Please check your Cisco router DNS configuration ('ip dns server' / 'ip host') or verify domain spelling.")
        sys.exit(1)

    alias = args.username
    while not alias or not ALIAS_PATTERN.match(alias):
        if alias is not None:
            print("Alias must be 1-12 characters (letters, digits, or underscore).")
        try:
            alias = input("Enter player alias: ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)

    client = HangmanClient(server_ip=server_ip, port=args.port, alias=alias)
    client.connect()

    recv_thread = threading.Thread(target=client.receiver_loop, daemon=True)
    recv_thread.start()

    client.input_loop()
    recv_thread.join(timeout=1.0)


if __name__ == "__main__":
    main()