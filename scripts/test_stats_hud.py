"""Test script to verify Stats API HUD visibility commands and client integration."""

import asyncio
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rlgymstream.stats_api.client import StatsApiClient, send_stats_command
from rlgymstream.match.launcher import MatchLauncher
from rlgymstream.config import AppConfig


async def test_send_stats_command():
    print("Testing send_stats_command standalone function...")
    received_lines = []

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        data = await reader.readline()
        received_lines.append(data.decode("utf-8"))
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    server_task = asyncio.create_task(server.serve_forever())

    # Send command
    success = send_stats_command(port=port, command="SetHUDVisibility", data={"bVisible": False})
    assert success, "send_stats_command returned False"

    await asyncio.sleep(0.1)
    server.close()
    await server.wait_closed()
    server_task.cancel()

    assert len(received_lines) == 1, f"Expected 1 line, got {len(received_lines)}"
    parsed = json.loads(received_lines[0].strip())
    assert parsed == {"Command": "SetHUDVisibility", "Data": {"bVisible": False}}, f"Unexpected payload: {parsed}"
    print("[PASS] send_stats_command test passed!")


async def test_stats_api_client_hud():
    print("Testing StatsApiClient HUD commands and auto-hide...")
    received_lines = []

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        async def read_commands():
            while not reader.at_eof():
                line = await reader.readline()
                if line:
                    received_lines.append(line.decode("utf-8"))

        read_task = asyncio.create_task(read_commands())

        # Send a sample event: CountdownBegin
        event_payload = json.dumps({"Event": "CountdownBegin", "Data": "{}"}) + "\n"
        writer.write(event_payload.encode("utf-8"))
        await writer.drain()

        # Wait a bit
        await asyncio.sleep(0.4)
        read_task.cancel()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server_task = asyncio.create_task(server.serve_forever())

    client = StatsApiClient(host="127.0.0.1", port=port, auto_hide_hud=True)
    client_task = asyncio.create_task(client.run())

    # Wait for connection and auto-hide on connect & CountdownBegin
    await asyncio.sleep(0.5)

    # Also test manual send
    client.set_hud_visibility(False)
    await asyncio.sleep(0.2)

    client.stop()
    client_task.cancel()
    server.close()
    await server.wait_closed()
    server_task.cancel()

    commands = [json.loads(line.strip()) for line in received_lines if line.strip()]
    print(f"Received {len(commands)} commands: {commands}")

    assert len(commands) >= 2, f"Expected at least 2 commands sent, got {len(commands)}"
    for cmd in commands:
        assert cmd["Command"] == "SetHUDVisibility"
        assert cmd["Data"] == {"bVisible": False}

    print("[PASS] StatsApiClient HUD test passed!")


def test_launcher_hide_hud():
    print("Testing MatchLauncher._hide_hud()...")
    config = AppConfig()
    launcher = MatchLauncher(config)

    # 1. Without stats_client (fallback to send_stats_command, shouldn't crash if port not open)
    launcher._hide_hud()
    print("[PASS] Fallback when no stats_client or socket doesn't raise exception")

    # 2. With stats_client attached
    class DummyClient:
        def __init__(self):
            self.hud_hidden = False

        def set_hud_visibility(self, visible: bool = False) -> bool:
            self.hud_hidden = not visible
            return True

    dummy = DummyClient()
    launcher.set_stats_client(dummy)
    launcher._hide_hud()
    assert dummy.hud_hidden is True, "DummyClient did not receive set_hud_visibility(False)"
    print("[PASS] MatchLauncher._hide_hud() uses attached stats_client correctly")


async def main():
    await test_send_stats_command()
    await test_stats_api_client_hud()
    test_launcher_hide_hud()
    print("\nAll HUD replacement tests passed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
