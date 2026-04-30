"""Check what player names the Stats API reports for duplicate bots."""
import socket
import json
from rlgymstream.stats_api.client import StatsApiClient

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(5)
s.connect(("localhost", 49123))

all_data = b""
for _ in range(5):
    try:
        chunk = s.recv(65536)
        if not chunk:
            break
        all_data += chunk
    except socket.timeout:
        break
s.close()

client = StatsApiClient()
buf = all_data.lstrip()
if buf[0:1] != b"{":
    buf = buf[buf.find(b"{"):]
end = client._find_json_end(buf)
raw = buf[:end]
msg = json.loads(raw)
data = msg.get("Data", {})
if isinstance(data, str):
    data = json.loads(data)

print("Player names from Stats API:")
for p in data.get("Players", []):
    print(f"  Team {p.get('TeamNum')}: '{p.get('Name')}' (Shortcut: {p.get('Shortcut')})")

