"""Verify the brace-counting parser works with real Stats API data."""
import socket
import json
from rlgymstream.stats_api.client import StatsApiClient

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(5)
s.connect(("localhost", 49123))

# Collect enough data for multiple messages
all_data = b""
for _ in range(10):
    try:
        chunk = s.recv(65536)
        if not chunk:
            break
        all_data += chunk
    except socket.timeout:
        break
s.close()

print(f"Total data: {len(all_data)} bytes")

# Use the parser
client = StatsApiClient()
buf = all_data
msg_count = 0
while buf:
    stripped = buf.lstrip()
    if not stripped:
        break
    if stripped[0:1] != b"{":
        idx = stripped.find(b"{")
        if idx == -1:
            break
        stripped = stripped[idx:]
    buf = stripped

    end = client._find_json_end(buf)
    if end == -1:
        print(f"Incomplete message remaining: {len(buf)} bytes")
        break

    raw = buf[:end]
    buf = buf[end:]

    try:
        msg = json.loads(raw)
        event = msg.get("Event", "")
        data = msg.get("Data", {})

        # Handle double-encoded Data
        if isinstance(data, str):
            data = json.loads(data)

        msg_count += 1
        if msg_count <= 3:
            print(f"\nMessage {msg_count}: Event={event}")
            if event == "UpdateState":
                players = data.get("Players", [])
                print(f"  Players: {[p.get('Name') for p in players]}")
                game = data.get("Game", {})
                print(f"  Time: {game.get('TimeSeconds')}s, Score: ", end="")
                for t in game.get("Teams", []):
                    print(f"{t.get('Name')}={t.get('Score')} ", end="")
                print()
            else:
                print(f"  Data keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
    except json.JSONDecodeError as e:
        print(f"JSON decode error: {e}")
        print(f"  Raw (first 200): {raw[:200]!r}")

print(f"\nTotal messages parsed: {msg_count}")

