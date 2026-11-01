"""Connect to the Stats API and capture a GoalScored event to inspect the raw data.

Prints every non-UpdateState event as it arrives (plus the ball speed every
100 UpdateStates), then dumps the full payload of the first GoalScored
event and exits.  Gives up after 2000 messages or 60 s without data.

Usage:
    python scripts/capture_goal.py [port]     # default port 49123
"""
import json
import socket
import sys

from rlgymstream.stats_api.client import StatsApiClient

MAX_MESSAGES = 2000


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 49123
    s = socket.create_connection(("localhost", port), timeout=60)
    print("Connected to Stats API, waiting for a goal...")

    buf = b""
    count = 0
    try:
        while count < MAX_MESSAGES:
            try:
                data = s.recv(16384)
            except socket.timeout:
                print("No data for 60 s")
                break
            if not data:
                print("Connection closed by server")
                break
            buf += data
            while True:
                buf = buf.lstrip()
                start = buf.find(b"{")
                if start == -1:
                    buf = b""
                    break
                buf = buf[start:]
                end = StatsApiClient._find_json_end(buf)
                if end == -1:
                    break  # incomplete message — wait for more data
                raw, buf = buf[:end], buf[end:]
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event = msg.get("Event", "")
                d = msg.get("Data", {})
                if isinstance(d, str):  # Data is double-encoded JSON
                    try:
                        d = json.loads(d)
                    except json.JSONDecodeError:
                        d = {}
                count += 1

                if event == "GoalScored":
                    print("=== GoalScored ===")
                    print(json.dumps(d, indent=2))
                    return
                if event != "UpdateState":
                    print(f"Received event #{count}: {event}")
                    if event == "StatfeedEvent":
                        print(f"  Type={d.get('Type', '')} EventName={d.get('EventName', '')} "
                              f"MainTarget={d.get('MainTarget', {}).get('Name', '')}")
                elif count % 100 == 0:
                    speed = d.get("Game", {}).get("Ball", {}).get("Speed", "N/A")
                    print(f"  (UpdateState #{count}, ball speed={speed})")
        print(f"No GoalScored event after {count} messages")
    finally:
        s.close()


if __name__ == "__main__":
    main()
