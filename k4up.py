#!/usr/bin/env python3
"""
Bring up KPA1500 via WOL (if needed), wait for K4, then send commands.
Cross-platform: Linux, macOS, Windows.
"""

import socket
import struct
import sys
import time
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
KPA1500_MAC = "54:10:EC:D8:0A:E6"
KPA1500_PORT = 1500

# Ping/wait parameters
K4_WAIT_TIMEOUT = 60        # seconds total
PING_INTERVAL = 5           # seconds between pings
KPA1500_WAIT_TIMEOUT = 30   # seconds to wait for KPA1500 after WOL
K4_SETTLE_DELAY = 15        # seconds to wait after K4 responds to ping before sending commands
TS1_RETRY_COUNT = 5         # number of times to retry TS1; if no echo
TS1_RETRY_INTERVAL = 5      # seconds between TS1; retries

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


def wait_for_host(host: str, timeout: int, interval: int) -> bool:
    """Repeatedly ping a host until it responds or timeout is reached."""
    elapsed = 0
    while elapsed < timeout:
        if ping(host):
            return True
        time.sleep(interval)
        elapsed += interval
    return False


def send_wol(mac: str) -> None:
    """Send a Wake-on-LAN magic packet via UDP broadcast."""
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    magic = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(magic, ("255.255.255.255", 9))
    print(f"  WOL magic packet sent to {mac}")


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
    # Check KPA1500 reachability
    print(f"Checking KPA1500 at {KPA1500_IP}...")
    if not ping(KPA1500_IP):
        print("KPA1500 not reachable, attempting Wake-on-LAN...")
        send_wol(KPA1500_MAC)

        print(f"Waiting for KPA1500 to come up (timeout {KPA1500_WAIT_TIMEOUT}s)...")
        if not wait_for_host(KPA1500_IP, KPA1500_WAIT_TIMEOUT, PING_INTERVAL):
            print(
                f"Error: KPA1500 did not respond to ping within {KPA1500_WAIT_TIMEOUT} seconds after WOL.",
                file=sys.stderr,
            )
            return 1
        print("KPA1500 is now reachable.")
    else:
        print("KPA1500 is already reachable.")

    # Check K4 reachability - if not up, tell amp to turn it on
    print(f"Checking K4 at {K4_IP}...")
    k4_just_powered_on = False
    if not ping(K4_IP):
        print("K4 not reachable, sending ^TV200; to KPA1500 to power on K4...")
        try:
            send_udp(KPA1500_IP, KPA1500_PORT, "^TV200;")
        except OSError as e:
            print(f"Error: failed to send UDP data to {KPA1500_IP}:{KPA1500_PORT}: {e}", file=sys.stderr)
            return 1

        print(f"Waiting for K4 to come up (timeout {K4_WAIT_TIMEOUT}s)...")
        if not wait_for_host(K4_IP, K4_WAIT_TIMEOUT, PING_INTERVAL):
            print(
                f"Error: K4 did not respond to ping within {K4_WAIT_TIMEOUT} seconds.",
                file=sys.stderr,
            )
            return 1
        print("K4 is now reachable.")
        k4_just_powered_on = True
    else:
        print("K4 is already reachable.")

    # If K4 was just powered on, give it extra time to finish initializing
    if k4_just_powered_on:
        print(f"Waiting {K4_SETTLE_DELAY}s for K4 to finish booting...")
        time.sleep(K4_SETTLE_DELAY)

    # Send TS1;, wait 500ms, then verify with TS; - retry full cycle if no response
    print(f"Sending TS1; to K4 ({K4_IP}:{K4_PORT})...")
    confirmed = False
    for attempt in range(1, TS1_RETRY_COUNT + 1):
        try:
            send_tcp(K4_IP, K4_PORT, "TS1;")
            time.sleep(0.5)
            response = send_tcp(K4_IP, K4_PORT, "TS;")
            if "TS1;" in response:
                print(f"K4 confirmed transmit on (TS1;) on attempt {attempt}.")
                confirmed = True
                break
            print(f"Attempt {attempt}: no response to TS; (got {response!r}), waiting {TS1_RETRY_INTERVAL}s...")
        except OSError as e:
            print(f"Attempt {attempt}: TCP error - {e}, waiting {TS1_RETRY_INTERVAL}s...")
        if attempt < TS1_RETRY_COUNT:
            time.sleep(TS1_RETRY_INTERVAL)

    if not confirmed:
        print(f"Error: K4 did not confirm TS1; after {TS1_RETRY_COUNT} attempts.", file=sys.stderr)
        return 1

    print("Radio and amplifier are available.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
