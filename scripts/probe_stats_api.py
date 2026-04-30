"""Quick probe of the Stats API to see what it actually sends."""
import socket
import sys

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(5)
try:
    s.connect(("localhost", 49123))
    print("Connected!")
    # Read a few chunks
    all_data = b""
    for i in range(3):
        try:
            data = s.recv(8192)
            if not data:
                print("Connection closed by server")
                break
            all_data += data
            print(f"Chunk {i}: {len(data)} bytes (total: {len(all_data)})")
        except socket.timeout:
            print(f"Timeout after chunk {i}")
            break

    print(f"\nTotal received: {len(all_data)} bytes")
    print(f"\nFirst 1000 bytes (repr):")
    print(repr(all_data[:1000]))
    print(f"\nLast 500 bytes (repr):")
    print(repr(all_data[-500:]))

    # Check for common delimiters
    print(f"\nDelimiter analysis:")
    print(f"  \\r\\n count: {all_data.count(b'\\r\\n')}")
    print(f"  \\n count: {all_data.count(b'\\n')}")
    print(f"  \\r count: {all_data.count(b'\\r')}")
    print(f"  Null byte count: {all_data.count(b'\\x00')}")

    # Check if it starts with HTTP
    print(f"\nStarts with 'HTTP': {all_data[:4] == b'HTTP'}")
    print(f"Starts with '{{': {all_data[:1] == b'{{' if all_data else 'empty'}")

except Exception as e:
    print(f"Error: {type(e).__name__}: {e}")
finally:
    s.close()

