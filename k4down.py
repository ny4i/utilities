#!/usr/bin/env python3
"""
Shut down K4 and KPA1500.
Cross-platform: Linux, macOS, Windows.
"""

import socket
import sys
import platform
import subprocess

#######################################
# Configuration
#######################################

# K4
K4_IP = "192.168.73.108"
K4_PORT = 9200

# KPA1500
KPA1500_IP = "192.168.73.109"
KPA1500_PORT = 1500

# K4/0
K4Z_IP = "192.168.73.159"
K4Z_PORT = 9200


#######################################
# Functions
#######################################

def ping(host: str, timeout: int = 1) -> bool:
    """Ping a host. Returns True if reachable."""
    system = platform.system().lower()
    if system == "windows":
        args = ["ping", "-n", "1", "-w", str(timeout * 1000), host]
    else:
        args = ["ping", "-c", "1", "-W", str(timeout), host]
    try:
        return subprocess.run(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode == 0
    except OSError:
        return False


def send_udp(host: str, port: int, data: str, timeout: float = 2.0) -> None:
    """Send data via UDP."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(data.encode(), (host, port))


def send_tcp(host: str, port: int, data: str, timeout: float = 2.0) -> str:
    """Send data via TCP and return any response."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.sendall(data.encode())
        try:
            return sock.recv(1024).decode()
        except socket.timeout:
            return ""


#######################################
# Main
#######################################

def main() -> int:
    # Check if K4 is reachable
    print(f"Checking K4 at {K4_IP}...")
    if not ping(K4_IP):
        print("K4 is not reachable - skipping PS0;.")
    else:
        print(f"Sending PS0; to K4 ({K4_IP}:{K4_PORT})...")
        try:
            send_tcp(K4_IP, K4_PORT, "PS0;")
            print("PS0; sent (K4 will power off).")
        except OSError as e:
            print(f"Warning: failed to send PS0; to K4 - {e}", file=sys.stderr)

    # Check if K4/0 is reachable
    print(f"Checking K4/0 at {K4Z_IP}...")
    if not ping(K4Z_IP):
        print("K4/0 is not reachable - skipping PS0;.")
    else:
        print(f"Sending PS0; to K4/0 ({K4Z_IP}:{K4Z_PORT})...")
        try:
            send_tcp(K4Z_IP, K4Z_PORT, "PS0;")
            print("PS0; sent (K4/0 will power off).")
        except OSError as e:
            print(f"Warning: failed to send PS0; to K4/0 - {e}", file=sys.stderr)

    # Send ^ON0; to KPA1500 to power it off
    print(f"Sending ^ON0; to KPA1500 ({KPA1500_IP}:{KPA1500_PORT})...")
    try:
        send_udp(KPA1500_IP, KPA1500_PORT, "^ON0;")
        print("^ON0; sent (KPA1500 will power off).")
    except OSError as e:
        print(f"Error: failed to send ^ON0; to KPA1500 - {e}", file=sys.stderr)
        return 1

    print("Shutdown commands sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
