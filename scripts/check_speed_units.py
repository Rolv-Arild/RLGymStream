"""Check actual speed values from the Stats API to verify units."""
import socket
import json

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

# Parse first message
from rlgymstream.stats_api.client import StatsApiClient
client = StatsApiClient()
buf = all_data
stripped = buf.lstrip()
if stripped[0:1] != b"{":
    idx = stripped.find(b"{")
    stripped = stripped[idx:]
end = client._find_json_end(stripped)
raw = stripped[:end]
msg = json.loads(raw)
data = msg.get("Data", {})
if isinstance(data, str):
    data = json.loads(data)

for p in data.get("Players", []):
    name = p.get("Name", "")
    speed = p.get("Speed", 0)
    boost = p.get("Boost", 0)
    supersonic = p.get("bSupersonic", False)
    kmh_current = round(speed * 0.036)
    # RL max speed is 2300 uu/s = 2300 cm/s
    # At 2300 cm/s: 2300 * 0.036 = 82.8 km/h — that's WAY too low
    # Supersonic in RL is ~95 km/h IRL equivalent
    # But the actual max in-game is 2300 uu/s
    # Let's just show raw values
    print(f"{name}: speed={speed:.1f} uu/s, boost={boost}, supersonic={supersonic}")
    print(f"  Current conversion (×0.036): {kmh_current} km/h")
    print(f"  If uu = cm/s, then m/s = {speed/100:.1f}, km/h = {speed*0.036:.1f}")

ball = data.get("Game", {}).get("Ball", {})
ball_speed = ball.get("Speed", 0)
print(f"\nBall: speed={ball_speed:.1f} uu/s")
print(f"  Current conversion: {round(ball_speed * 0.036)} km/h")
print(f"  Max ball speed in RL is ~6000 uu/s")
print(f"  At 6000: {6000 * 0.036} km/h (should be ~216 km/h)")

