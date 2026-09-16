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
    python3 tcitester.py --radio             # adds frequency, mode, split, sub RX
    python3 tcitester.py --all               # every settable command, including drive
    python3 tcitester.py --only rit_enable
    python3 tcitester.py --hold 5           # keep each change for 5 s so you can watch the UI
    python3 tcitester.py --step             # wait for Enter between every stage

SAFETY. Nothing here keys the transmitter - there is no PTT test and no tune. --safe touches
receive settings only. `drive` changes transmit POWER, so it is excluded from --safe and needs
--all or --only; it is restored afterwards like everything else. Run with the radio in TX Test
mode if you want belt and braces.

WATCHING THE RADIO OR THE QK4 WINDOW. By default a value is changed and restored within a couple
of seconds, which is too fast to see. --hold N keeps the new value for N seconds before restoring,
and --step waits for Enter at each stage so you can compare the radio, QK4's own display and the
client at your own pace.

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

# Every settable command, with the exact wire form for each - some carry a channel index, so a
# single "name:0,value;" template does not fit them all.
#
# TIERS
#   safe  - receive-side settings. They do not move you off frequency or change what you hear.
#   radio - changes the operating state: frequency, mode, split, second receiver.
#   tx    - touches the transmitter's power.
#
# PTT is absent ON PURPOSE. There is no test here that keys the radio, and there should not be:
# an automated tool that transmits is a tool that eventually transmits when you did not expect it.
TESTS = [
    # name, read command, set format, target (literal or callable(current)), tier, description
    ("rit_enable",        "rit_enable:0;",        "rit_enable:0,%s;",        "true",  "safe",
     "RIT on/off (K4 RT)"),
    ("xit_enable",        "xit_enable:0;",        "xit_enable:0,%s;",        "true",  "safe",
     "XIT on/off (K4 XT)"),
    ("rit_offset",        "rit_offset:0;",        "rit_offset:0,%s;",        "250",   "safe",
     "RIT/XIT offset (K4 RO)"),
    ("xit_offset",        "xit_offset:0;",        "xit_offset:0,%s;",        "-250",  "safe",
     "Same RO register as rit_offset - both names, one control"),
    ("rx_nb_enable",      "rx_nb_enable:0;",      "rx_nb_enable:0,%s;",      "true",  "safe",
     "Noise blanker (K4 NB)"),
    ("rx_nr_enable",      "rx_nr_enable:0;",      "rx_nr_enable:0,%s;",      "true",  "safe",
     "Noise reduction (K4 NR)"),
    ("agc_mode",          "agc_mode:0;",          "agc_mode:0,%s;",          None,    "safe",
     "AGC speed (K4 GT)"),
    ("rx_filter_band",    "rx_filter_band:0;",    "rx_filter_band:0,%s;",    None,    "safe",
     "Filter width (K4 BW) - edges in, width out"),

    ("vfo",               "vfo:0,0;",             "vfo:0,0,%s;",             None,    "radio",
     "VFO A frequency (K4 FA) - MOVES THE RADIO"),
    ("dds",               "dds:0;",               "dds:0,%s;",               None,    "radio",
     "Alias for the receive VFO"),
    ("modulation",        "modulation:0;",        "modulation:0,%s;",        None,    "radio",
     "Operating mode (K4 MD)"),
    ("split_enable",      "split_enable:0;",      "split_enable:0,%s;",      "true",  "radio",
     "Split on/off (K4 FT)"),
    ("rx_channel_enable", "rx_channel_enable:0,1;", "rx_channel_enable:0,1,%s;", "true", "radio",
     "Sub RX on/off (K4 SB)"),

    ("drive",             "drive:0;",             "drive:0,%s;",             "25",    "tx",
     "TRANSMIT POWER (K4 PC)"),
    # tune_drive is NOT here. The K4 has no separate tune-power command, so the server reports it
    # as a copy of drive and deliberately ignores a SET. Testing it would only prove that nothing
    # happens - and an earlier version DID honour it, which changed the operating power instead.
]


def flip(current):
    return "false" if current == "true" else "true"


def other_mode(current):
    # Stay inside what the server advertises, and prefer a mode that is obvious on the radio.
    for candidate in ("usb", "lsb", "cw", "am"):
        if candidate != current:
            return candidate
    return "usb"


def shift_frequency(current):
    try:
        return str(int(current) + 1000)
    except ValueError:
        return None


def other_agc(current):
    return "normal" if current == "fast" else "fast"


def narrower_band(current_line):
    """rx_filter_band carries two edges; change the width by 200 Hz."""
    try:
        parts = current_line.rstrip(";").partition(":")[2].split(",")
        low, high = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        return None
    return "%d,%d" % (low, high - 200 if (high - low) > 400 else high + 200)


# Targets that depend on the current value.
DYNAMIC = {
    "rit_enable": flip,
    "xit_enable": flip,
    "split_enable": flip,
    "rx_channel_enable": flip,
    "rx_nb_enable": flip,
    "rx_nr_enable": flip,
    "agc_mode": other_agc,
    "modulation": other_mode,
    "vfo": shift_frequency,
    "dds": shift_frequency,
}


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


def pause(step, hold, message):
    """--step waits for a keypress; --hold just sleeps. Neither stops reading the socket for long
    enough to matter - the server buffers, and the next read drains it."""
    if step:
        try:
            input("      [%s - press Enter]" % message)
        except EOFError:
            pass
    elif hold:
        print("      holding %ds - %s" % (hold, message))
        time.sleep(hold)


def run_test(conn, test, dry_run, step=False, hold=0):
    name, read_cmd, set_fmt, target, tier, description = test
    print("\n--- %s : %s" % (name, description))
    before = read_value(conn, read_cmd, name)
    if before is None:
        print("    SKIP  no reply to %s - not implemented" % read_cmd)
        return "skip"
    print("    before: %s" % before)

    current = value_of(before)
    if name == "rx_filter_band":
        target = narrower_band(before)
        restore = before.rstrip(";").partition(":")[2].split(",", 1)[1]
    else:
        if name in DYNAMIC:
            target = DYNAMIC[name](current)
        restore = current

    if target is None:
        print("    SKIP  cannot derive a target from %s" % before)
        return "skip"
    if target == restore:
        print("    SKIP  already at the only value worth trying")
        return "skip"

    pause(step, 0, "note the current value on the radio and in QK4")

    if dry_run:
        print("    DRY   would send %s" % (set_fmt % target))
        return "dry"

    print("    send  : %s" % (set_fmt % target))
    conn.send(set_fmt % target)

    want = target if name == "rx_filter_band" else target.split(",")[-1]
    confirmed = wait_for_broadcast(conn, name, want)
    if confirmed:
        print("    \033[32mPASS\033[0m  radio confirmed: %s" % confirmed)
        result = "pass"
        pause(step, hold, "the radio and QK4 should BOTH show the new value now")
    else:
        now = read_value(conn, read_cmd, name)
        print("    \033[31mFAIL\033[0m  no broadcast; still reads %s" % now)
        result = "fail"
        pause(step, hold, "nothing should have changed")

    print("    restore: %s" % (set_fmt % restore))
    conn.send(set_fmt % restore)
    wait_for_broadcast(conn, name, restore if name == "rx_filter_band" else restore.split(",")[-1],
                       seconds=2.0)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--safe", action="store_true",
                    help="receive-side settings only (the default)")
    ap.add_argument("--radio", action="store_true",
                    help="also frequency, mode, split and the sub receiver")
    ap.add_argument("--all", action="store_true",
                    help="every settable command, including transmit power. Never keys the radio")
    ap.add_argument("--only", help="run a single test by name")
    ap.add_argument("--list", action="store_true", help="list the tests and exit")
    ap.add_argument("--dry-run", action="store_true", help="read values, send nothing")
    ap.add_argument("--hold", type=int, default=0, metavar="N",
                    help="keep each change for N seconds before restoring, so the UI can be watched")
    ap.add_argument("--step", action="store_true",
                    help="wait for Enter at each stage instead of running straight through")
    args = ap.parse_args()

    if args.list:
        for name, _, _, _, tier, desc in TESTS:
            print("  %-20s %-6s %s" % (name, tier, desc))
        print("\n  PTT is deliberately absent: nothing here keys the transmitter.")
        return 0

    if args.only:
        chosen = [t for t in TESTS if t[0] == args.only]
        if not chosen:
            print("no such test: %s (try --list)" % args.only)
            return 1
    elif args.all:
        chosen = list(TESTS)
    elif args.radio:
        chosen = [t for t in TESTS if t[4] in ("safe", "radio")]
    else:
        chosen = [t for t in TESTS if t[4] == "safe"]

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
        for test in chosen:
            r = run_test(conn, test, args.dry_run, args.step, args.hold)
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
