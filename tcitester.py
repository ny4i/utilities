#!/usr/bin/env python3
"""
tcitester -- drive a TCI server's SET commands one at a time and check the radio agreed.

THIS ONE WRITES. tcimonitor.py is deliberately read-only so it is safe to leave running against a
live rig; keeping the write capability in a separate script is what preserves that property. Do
not merge them.

Each test is a round trip, not a fire-and-forget:

    1. read the current value and remember it
    2. send a new one
    3. wait for the server to BROADCAST the change - which only happens when the radio confirms it,
       so a broadcast is evidence the command reached the K4 and came back
    4. restore the original value
    5. report PASS, FAIL (no change seen) or SKIP (not implemented)

Step 3 is the whole point. A reply to the SET proves nothing: the server answers with the value it
currently holds, by design, so a SET that never reached the radio replies exactly like one that
did. Only the unsolicited broadcast distinguishes them.

    python3 tcitester.py --list              # what it can test, no connection made
    python3 tcitester.py --dry-run           # connect, read values, send nothing
    python3 tcitester.py --safe              # receive-side only: RIT, NB, NR, AGC, filter
    python3 tcitester.py --all               # adds transmit-side: drive
    python3 tcitester.py --only rit_enable

SAFETY. Nothing here keys the transmitter - there is no PTT test and no tune. --safe touches
receive settings only. `drive` changes transmit POWER, so it is excluded from --safe and needs
--all or --only; it is restored afterwards like everything else. Run with the radio in TX Test
mode if you want belt and braces.

Ctrl-C is safe at any point, but a test interrupted between steps 2 and 4 leaves that one setting
changed - the original is printed before each change so you can put it back by hand.
"""

import argparse
import base64
import hashlib
import os
import socket
import struct
import sys
import time

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_PORT = 50001


# ---------------------------------------------------------------------------------------------
# RFC 6455, inline so this stands alone like everything else here.
# ---------------------------------------------------------------------------------------------

def accept_key(client_key):
    return base64.b64encode(hashlib.sha1(client_key + WS_GUID).digest()).decode()


def handshake(sock, host, port):
    key = base64.b64encode(os.urandom(16))
    sock.sendall((
        "GET / HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (host, port, key.decode())
    ).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("server closed during handshake")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    if b"101" not in head.split(b"\r\n")[0]:
        raise RuntimeError("handshake refused")
    got = ""
    for ln in head.split(b"\r\n")[1:]:
        name, _, value = ln.partition(b":")
        if name.strip().lower() == b"sec-websocket-accept":
            got = value.strip().decode()
    if got != accept_key(key):
        raise RuntimeError("Sec-WebSocket-Accept mismatch - not a WebSocket peer")
    return rest


def encode_text(payload):
    header = bytearray([0x81])
    n = len(payload)
    if n <= 125:
        header.append(0x80 | n)
    elif n <= 0xFFFF:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    mask = os.urandom(4)
    return bytes(header) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def decode_frames(buf):
    out = []
    while True:
        if len(buf) < 2:
            break
        opcode = buf[0] & 0x0F
        ln = buf[1] & 0x7F
        i = 2
        if ln == 126:
            if len(buf) < 4:
                break
            ln = struct.unpack(">H", buf[2:4])[0]
            i = 4
        elif ln == 127:
            if len(buf) < 10:
                break
            ln = struct.unpack(">Q", buf[2:10])[0]
            i = 10
        if len(buf) < i + ln:
            break
        out.append((opcode, buf[i:i + ln]))
        buf = buf[i + ln:]
    return out, buf


class Connection:
    def __init__(self, host, port, timeout=5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = handshake(self.sock, host, port)
        self.pending = []
        self.timeout = timeout

    def send(self, text):
        self.sock.sendall(encode_text(text.encode()))

    def recv_text(self, timeout=None):
        deadline = time.time() + (self.timeout if timeout is None else timeout)
        while True:
            while self.pending:
                op, payload = self.pending.pop(0)
                if op == 0x1:
                    return payload.decode(errors="replace")
            frames, self.buf = decode_frames(self.buf)
            if frames:
                self.pending.extend(frames)
                continue
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return None
            if not chunk:
                return None
            self.buf += chunk

    def drain_burst(self, timeout=5.0):
        out = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = self.recv_text(timeout=max(0.1, deadline - time.time()))
            if m is None:
                break
            out.append(m)
            if m == "ready;":
                break
        return out

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------
# The tests. (name, read command, values to try, safe-for-receive-only)
# ---------------------------------------------------------------------------------------------

TESTS = [
    ("rit_enable",     "rit_enable:0;",     ["true", "false"],  True,  "RIT on/off (K4 RT)"),
    ("xit_enable",     "xit_enable:0;",     ["true", "false"],  True,  "XIT on/off (K4 XT)"),
    ("rit_offset",     "rit_offset:0;",     ["250", "-250"],    True,  "RIT/XIT offset, ONE shared register (K4 RO)"),
    ("rx_nb_enable",   "rx_nb_enable:0;",   ["true", "false"],  True,  "Noise blanker (K4 NB)"),
    ("rx_nr_enable",   "rx_nr_enable:0;",   ["true", "false"],  True,  "Noise reduction (K4 NR)"),
    ("agc_mode",       "agc_mode:0;",       ["fast", "normal"], True,  "AGC speed (K4 GT)"),
    ("rx_filter_band", "rx_filter_band:0;", None,               True,  "Filter width (K4 BW) - edges in, width out"),
    ("drive",          "drive:0;",          ["25"],             False, "TRANSMIT POWER (K4 PC)"),
]


def value_of(line):
    """Last argument of a TCI message, which is the value for everything tested here."""
    body = line.rstrip(";")
    _, _, args = body.partition(":")
    return args.split(",")[-1] if args else ""


def read_value(conn, read_cmd, name):
    conn.send(read_cmd)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        m = conn.recv_text(timeout=0.5)
        if m and m.startswith(name + ":"):
            return m
    return None


def band_width(line):
    """Width in Hz from an rx_filter_band message, or None."""
    try:
        parts = line.rstrip(";").partition(":")[2].split(",")
        return int(parts[2]) - int(parts[1])
    except (IndexError, ValueError):
        return None


def wait_for_broadcast(conn, name, want, seconds=3.0):
    """A broadcast matching `want` is the radio confirming. Returns the line, or None.

    rx_filter_band is compared by WIDTH, not by the literal edges. TCI passes two edges and the
    K4 takes a width, so only the width survives the round trip: the centre is recomputed from the
    mode and the IF shift. Asking for -1000,3800 legitimately comes back as -900,3900 - same 4800
    Hz width, re-centred. Comparing the last argument reported that correct result as a failure.
    """
    want_width = band_width("x:0," + want) if name == "rx_filter_band" else None
    deadline = time.time() + seconds
    while time.time() < deadline:
        m = conn.recv_text(timeout=0.4)
        if not m or not m.startswith(name + ":"):
            continue
        if want_width is not None:
            if band_width(m) == want_width:
                return m
        elif value_of(m) == want:
            return m
    return None


def run_test(conn, name, read_cmd, values, description, dry_run):
    print("\n--- %s : %s" % (name, description))
    before = read_value(conn, read_cmd, name)
    if before is None:
        print("    SKIP  no reply to %s - not implemented" % read_cmd)
        return "skip"
    print("    before: %s" % before)

    if name == "rx_filter_band":
        # Two edges rather than one value; width is what actually changes on the radio.
        parts = before.rstrip(";").partition(":")[2].split(",")
        try:
            low, high = int(parts[1]), int(parts[2])
        except (IndexError, ValueError):
            print("    SKIP  cannot parse the current band")
            return "skip"
        target = "%d,%d" % (low, high - 200 if (high - low) > 400 else high + 200)
        restore = "%d,%d" % (low, high)
        values = [target]
    else:
        restore = value_of(before)
        values = [v for v in values if v != restore] or values

    if dry_run:
        print("    DRY   would send %s:0,%s;" % (name, values[0]))
        return "dry"

    target = values[0]
    print("    send  : %s:0,%s;" % (name, target))
    conn.send("%s:0,%s;" % (name, target))

    want = target if name == "rx_filter_band" else target.split(",")[-1]
    confirmed = wait_for_broadcast(conn, name, want)
    if confirmed:
        print("    \033[32mPASS\033[0m  radio confirmed: %s" % confirmed)
        result = "pass"
    else:
        now = read_value(conn, read_cmd, name)
        print("    \033[31mFAIL\033[0m  no broadcast; still reads %s" % now)
        result = "fail"

    print("    restore: %s:0,%s;" % (name, restore))
    conn.send("%s:0,%s;" % (name, restore))
    wait_for_broadcast(conn, name, restore if name == "rx_filter_band" else restore.split(",")[-1],
                       seconds=2.0)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--safe", action="store_true", help="receive-side settings only (default)")
    ap.add_argument("--all", action="store_true", help="also test transmit power")
    ap.add_argument("--only", help="run a single test by name")
    ap.add_argument("--list", action="store_true", help="list the tests and exit")
    ap.add_argument("--dry-run", action="store_true", help="read values, send nothing")
    args = ap.parse_args()

    if args.list:
        for name, _, _, safe, desc in TESTS:
            print("  %-16s %-6s %s" % (name, "safe" if safe else "TX", desc))
        return 0

    chosen = [t for t in TESTS if (args.only == t[0]) if args.only] or \
             [t for t in TESTS if args.all or t[3]]
    if args.only and not chosen:
        print("no such test: %s (try --list)" % args.only)
        return 1

    try:
        conn = Connection(args.host, args.port)
    except OSError as e:
        print("cannot connect to %s:%d -- %s" % (args.host, args.port, e))
        return 1

    burst = conn.drain_burst()
    print("connected: %d-command init burst" % len(burst))
    if args.dry_run:
        print("DRY RUN - nothing will be sent to the radio")

    tally = {}
    try:
        for name, read_cmd, values, _safe, desc in chosen:
            r = run_test(conn, name, read_cmd, values, desc, args.dry_run)
            tally[r] = tally.get(r, 0) + 1
    except KeyboardInterrupt:
        print("\ninterrupted - check the last 'send' above; it may not have been restored")
    finally:
        conn.close()

    print("\n" + "=" * 60)
    print("  " + "   ".join("%s %d" % (k, v) for k, v in sorted(tally.items())))
    return 1 if tally.get("fail") else 0


if __name__ == "__main__":
    sys.exit(main())
