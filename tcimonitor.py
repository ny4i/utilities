#!/usr/bin/env python3
"""
tcimonitor -- a live, top-style view of everything a TCI server reports.

Written for QK4 (github.com/ny4i/QK4), which speaks TCI to an Elecraft K4, but it is a plain TCI
client and will talk to any TCI server.

WHY THIS EXISTS.  A conformance sweep that sends queries and checks replies is structurally blind
to the other half of the protocol: the messages a server sends UNPROMPTED.  That blind spot is
where a real bug lived - a transmit-status message was never sent, every command involved was
"implemented", and a query-based audit reported no problem.

This tool watches the unprompted half.  It subscribes to the sensors, then sits and renders
whatever turns up.  If a meter is not moving on screen, the server is not sending it.

    python3 tcimonitor.py                    # localhost:50001, 200 ms sensors
    python3 tcimonitor.py --interval 50      # faster meters
    python3 tcimonitor.py --host 192.168.1.5
    python3 tcimonitor.py --raw              # also log every message, unrendered

SAFE AGAINST A LIVE RADIO.  It sends exactly two commands, both subscriptions
(rx_sensors_enable, tx_sensors_enable), and never a SET.  Nothing here can move the radio or key
the transmitter.

NO DEPENDENCIES.  RFC 6455 framing is implemented here over a raw socket rather than pulling in a
websockets library: the framing is fifty lines, and a second independent implementation of the
same spec is a better check on a server than running the same library on both ends.

Ctrl-C to quit.
"""

import argparse
import base64
import hashlib
import os
import socket
import struct
import sys
import threading
import time

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_PORT = 50001


def accept_key(client_key: bytes) -> str:
    return base64.b64encode(hashlib.sha1(client_key + WS_GUID).digest()).decode()


def handshake(sock, host, port, resource="/"):
    key = base64.b64encode(os.urandom(16))
    req = (
        f"GET {resource} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key.decode()}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode()
    sock.sendall(req)

    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("server closed during handshake")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if b"101" not in lines[0]:
        raise RuntimeError("handshake refused: %s" % lines[0].decode(errors="replace"))

    got = ""
    for ln in lines[1:]:
        name, _, value = ln.partition(b":")
        if name.strip().lower() == b"sec-websocket-accept":
            got = value.strip().decode()
    want = accept_key(key)
    if got != want:
        # Not pedantry: a proxy or a plain HTTP server can answer 101 without being a WebSocket
        # peer. Verifying the digest is what makes the handshake mean something.
        raise RuntimeError("Sec-WebSocket-Accept mismatch: got %r, want %r" % (got, want))
    return rest


def encode_text(payload: bytes) -> bytes:
    """A client MUST mask every frame it sends (RFC 6455 5.3)."""
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
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + mask + masked


def decode_frames(buf: bytes):
    """Yield (opcode, payload) for every COMPLETE frame; return the remainder."""
    out = []
    while True:
        if len(buf) < 2:
            break
        b0, b1 = buf[0], buf[1]
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        ln = b1 & 0x7F
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
        if masked:
            # A server must not mask. Say so rather than silently coping.
            raise RuntimeError("server sent a MASKED frame -- protocol violation")
        if len(buf) < i + ln:
            break
        out.append((opcode, buf[i:i + ln]))
        buf = buf[i + ln:]
    return out, buf


class Connection:
    """A TCI connection with a blocking, frame-at-a-time read."""

    def __init__(self, host, port, timeout=5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = handshake(self.sock, host, port)
        self.timeout = timeout
        # decode_frames() consumes EVERY complete frame in the buffer, and the init burst arrives
        # as many frames in one TCP segment. Without somewhere to park the surplus, returning the
        # first would silently discard the rest.
        self.pending = []

    def send(self, text):
        self.sock.sendall(encode_text(text.encode()))

    def recv_text(self, timeout=None):
        """Next TEXT payload as str, or None on timeout."""
        deadline = time.time() + (self.timeout if timeout is None else timeout)
        while True:
            while self.pending:
                op, payload = self.pending.pop(0)
                if op == 0x1:
                    return payload.decode(errors="replace")
                # opcode 2 is binary audio; ignore here.
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

    def drain_burst(self, terminator="ready;", timeout=5.0):
        """Collect the init burst up to and including `terminator`."""
        out = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = self.recv_text(timeout=max(0.1, deadline - time.time()))
            if m is None:
                break
            out.append(m)
            if m == terminator:
                break
        return out

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass




# S9 = -73 dBm, 6 dB per S-unit below it. The bar spans S0 to roughly S9+60.
S9_DBM = -73.0
DB_PER_S_UNIT = 6.0
BAR_MIN_DBM = S9_DBM - 9 * DB_PER_S_UNIT  # S0, -127
BAR_MAX_DBM = S9_DBM + 60.0               # S9+60, -13

ESC = "\x1b["


def s_unit_text(dbm):
    """dBm rendered the way an operator reads a meter."""
    if dbm is None:
        return "--"
    if dbm >= S9_DBM:
        over = int(round((dbm - S9_DBM) / 10.0) * 10)
        return "S9" if over <= 0 else "S9+%d" % over
    units = 9 - (S9_DBM - dbm) / DB_PER_S_UNIT
    return "S%d" % max(0, int(round(units)))


def bar(dbm, width=34):
    if dbm is None:
        return "░" * width
    span = BAR_MAX_DBM - BAR_MIN_DBM
    frac = (dbm - BAR_MIN_DBM) / span
    frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)
    filled = int(frac * width)
    return "█" * filled + "░" * (width - filled)


def hz(value):
    if value is None:
        return "-- --- ---"
    s = "%09d" % int(value)
    return "%s.%s.%s" % (s[:-6].lstrip("0") or "0", s[-6:-3], s[-3:])


class State:
    """Everything the server has told us, updated as messages arrive."""

    def __init__(self):
        self.fields = {}
        self.rx_dbm = {0: None, 1: None}
        self.tx = {"mic": None, "power": None, "peak": None, "swr": None}
        self.counts = {"total": 0, "sensor": 0}
        self.last_line = ""
        self.sensor_times = []
        self.started = time.time()

    def feed(self, line):
        self.counts["total"] += 1
        self.last_line = line
        body = line.rstrip(";")
        name, _, argstr = body.partition(":")
        name = name.lower()
        args = argstr.split(",") if argstr else []

        def arg(i, default=None):
            return args[i] if len(args) > i else default

        if name in ("rx_sensors", "rx_channel_sensors", "tx_sensors"):
            self.counts["sensor"] += 1
            now = time.time()
            self.sensor_times.append(now)
            # Keep a one-second window for the rate readout.
            self.sensor_times = [t for t in self.sensor_times if now - t <= 1.0]

        try:
            if name == "vfo":
                # vfo:<trx>,<channel>,<hz>. Receiver 1 is the Sub RX; channel 1 of receiver 0 is
                # the split transmit VFO.
                self.fields["vfo_%s_%s" % (arg(0), arg(1))] = int(arg(2))
            elif name == "dds":
                self.fields["dds"] = int(arg(1))
            elif name == "tx_frequency":
                self.fields["tx_freq"] = int(arg(0))
            elif name == "modulation":
                self.fields["mode_%s" % arg(0)] = arg(1)
            elif name == "rx_filter_band":
                self.fields["filter_%s" % arg(0)] = (int(arg(1)), int(arg(2)))
            elif name == "rx_volume":
                # rx_volume:<trx>,<channel>,<dB>. QK4 reports its own playback mix here.
                self.fields["vol_%s" % arg(0)] = int(arg(2))
            elif name in ("cw_keyer_speed", "cw_macros_speed"):
                self.fields["wpm"] = int(arg(0))
            elif name == "rx_enable":
                self.fields["rx_enabled_%s" % arg(0)] = arg(1) == "true"
            elif name in ("rx_nb_enable", "rx_nr_enable", "rx_anf_enable", "rx_apf_enable",
                          "rx_nf_enable"):
                self.fields["%s_%s" % (name, arg(0))] = arg(1) == "true"
            elif name == "trx_count":
                self.fields["trx_count"] = int(arg(0))
            elif name == "trx":
                self.fields["tx"] = arg(1) == "true"
            elif name == "split_enable":
                self.fields["split"] = arg(1) == "true"
            elif name == "rx_channel_enable":
                self.fields["chan%s" % arg(1)] = arg(2) == "true"
            elif name in ("rit_enable", "xit_enable"):
                self.fields["%s_%s" % (name, arg(0))] = arg(1) == "true"
            elif name in ("rit_offset", "xit_offset"):
                self.fields["%s_%s" % (name, arg(0))] = int(arg(1))
            elif name == "agc_mode":
                self.fields["agc_%s" % arg(0)] = arg(1)
            elif name == "sql_enable":
                self.fields["sql_on"] = arg(1) == "true"
            elif name == "sql_level":
                self.fields["sql"] = arg(1)
            elif name in ("drive", "tune_drive"):
                self.fields[name] = arg(1)
            elif name == "mic_level":
                self.fields["mic_level"] = arg(0)
            elif name in ("device", "protocol"):
                self.fields[name] = argstr
            elif name == "rx_sensors":
                self.rx_dbm[0] = float(arg(1))
            elif name == "rx_channel_sensors":
                # rx_channel_sensors:<trx>,<channel>,<dbm>. The sub receiver arrives twice, once
                # as receiver 1 and once as channel 1 of receiver 0; either is fine to show.
                receiver, channel = int(arg(0)), int(arg(1))
                self.rx_dbm[1 if (receiver == 1 or channel == 1) else 0] = float(arg(2))
            elif name == "tx_sensors":
                self.tx["mic"] = float(arg(1))
                self.tx["power"] = float(arg(2))
                self.tx["peak"] = float(arg(3))
                self.tx["swr"] = float(arg(4))
        except (TypeError, ValueError, IndexError):
            # A malformed message is data about the server, not a reason to die mid-session.
            self.fields["last_bad"] = line


def render(state, host, port, raw_lines):
    f = state.fields
    up = int(time.time() - state.started)
    rate = len(state.sensor_times)

    tx_on = f.get("tx")
    # Pad the PLAIN text before colouring: escape sequences count toward a %-width field but
    # occupy no columns, so colouring first misaligns every column to the right of it.
    badge_text = "● TRANSMIT" if tx_on else "● receive"
    badge_colour = "1;31" if tx_on else "1;32"
    tx_badge = "\x1b[%sm%-24s\x1b[0m" % (badge_colour, badge_text)
    sub_on = f.get("chan1")

    out = []
    out.append("\x1b[1m TCI Monitor\x1b[0m  %s:%d%s" % (host, port, " " * 8))
    out.append(" up %02d:%02d:%02d   messages %-7d sensors %d/s (%d total)"
               % (up // 3600, (up // 60) % 60, up % 60, state.counts["total"], rate,
                  state.counts["sensor"]))
    out.append(" " + "─" * 72)
    out.append(" device   %-24s protocol  %s"
               % (f.get("device", "--"), f.get("protocol", "--")))
    out.append(" state    %s" % tx_badge)
    out.append("")

    def filt(r):
        band = f.get("filter_%d" % r)
        return "%+5d..%+5d" % band if band else "    --     "

    def vol(r):
        v = f.get("vol_%d" % r)
        return "%+3d dB" % v if v is not None else "  -- dB"

    def flags(r):
        on = [n.split("_")[1].upper()
              for n in ("rx_nb_enable", "rx_nr_enable", "rx_anf_enable", "rx_apf_enable",
                        "rx_nf_enable")
              if f.get("%s_%d" % (n, r))]
        return " ".join(on) if on else "-"

    # Receiver 0 (Main) and receiver 1 (Sub). trx_count says how many the server advertises.
    out.append(" \x1b[1mRX0 Main\x1b[0m  %-14s %-6s agc %-7s filter %s  vol %s  %s"
               % (hz(f.get("vfo_0_0")), f.get("mode_0", "--"), f.get("agc_0", "--"),
                  filt(0), vol(0), flags(0)))
    if sub_on:
        out.append(" \x1b[1mRX1 Sub \x1b[0m  %-14s %-6s agc %-7s filter %s  vol %s  %s"
                   % (hz(f.get("vfo_1_0")), f.get("mode_1", "--"), f.get("agc_1", "--"),
                      filt(1), vol(1), flags(1)))
    else:
        out.append(" RX1 Sub    (off)%s" % (" " * 64))
    out.append(" TX freq   %-14s  split %-4s  wpm %-4s  trx_count %s"
               % (hz(f.get("tx_freq")), "ON" if f.get("split") else "off",
                  f.get("wpm", "--"), f.get("trx_count", "--")))
    out.append("")
    out.append(" rit      %-4s %+6d Hz            xit       %-4s %+6d Hz"
               % ("ON" if f.get("rit_enable_0") else "off", f.get("rit_offset_0", 0),
                  "ON" if f.get("xit_enable_0") else "off", f.get("xit_offset_0", 0)))
    out.append(" drive    %-4s                    sql       %-4s %s dBm"
               % (f.get("drive", "--"), "ON" if f.get("sql_on") else "off", f.get("sql", "--")))
    out.append("")
    out.append(" \x1b[1mRX signal\x1b[0m")
    a = state.rx_dbm[0]
    out.append("   A  %s  %s  %s"
               % (bar(a), ("%8.1f dBm" % a) if a is not None else "      -- dBm", s_unit_text(a)))
    if sub_on:
        b = state.rx_dbm[1]
        out.append("   B  %s  %s  %s"
                   % (bar(b), ("%8.1f dBm" % b) if b is not None else "      -- dBm", s_unit_text(b)))
    else:
        out.append("   B  %s  (sub rx off)%s" % ("░" * 34, " " * 12))
    out.append("")
    out.append(" \x1b[1mTX\x1b[0m")
    t = state.tx
    out.append("   mic level %-4s (setting)     alc/mic signal below (measured)"
               % f.get("mic_level", "--"))
    out.append("   power %s W    peak %s W    swr %s    mic %s dBm"
               % (("%6.1f" % t["power"]) if t["power"] is not None else "    --",
                  ("%6.1f" % t["peak"]) if t["peak"] is not None else "    --",
                  ("%5.2f" % t["swr"]) if t["swr"] is not None else "   --",
                  ("%6.1f" % t["mic"]) if t["mic"] is not None else "    --"))
    out.append("")
    out.append(" last  \x1b[2m%s\x1b[0m" % state.last_line[:66])
    if raw_lines:
        out.append("")
        for line in raw_lines[-8:]:
            out.append("   \x1b[2m%s\x1b[0m" % line[:70])
    out.append("")
    out.append(" \x1b[2mCtrl-C to quit. This tool sends only sensor subscriptions - never a SET.\x1b[0m")

    # Home the cursor and clear each line, rather than clearing the screen: a full clear flickers.
    sys.stdout.write(ESC + "H")
    for line in out:
        sys.stdout.write(line + ESC + "K\n")
    sys.stdout.write(ESC + "J")
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--interval", type=int, default=200,
                    help="sensor reporting interval in ms (spec allows 30-1000)")
    ap.add_argument("--raw", action="store_true", help="also show the last few raw messages")
    args = ap.parse_args()

    try:
        conn = Connection(args.host, args.port, timeout=5.0)
    except OSError as e:
        print("cannot connect to %s:%d -- %s" % (args.host, args.port, e))
        print("is the TCI server running and enabled?")
        return 1

    state = State()
    for line in conn.drain_burst():
        state.feed(line)

    conn.send("rx_sensors_enable:true,%d;" % args.interval)
    conn.send("tx_sensors_enable:true,%d;" % args.interval)

    raw = []
    sys.stdout.write(ESC + "2J" + ESC + "?25l")  # clear once, hide the cursor
    try:
        last_draw = 0.0
        while True:
            line = conn.recv_text(timeout=0.1)
            if line is not None:
                state.feed(line)
                if args.raw:
                    raw.append(line)
                    raw[:] = raw[-40:]
            now = time.time()
            if now - last_draw >= 0.1:   # 10 Hz is smooth and costs nothing
                render(state, args.host, args.port, raw if args.raw else None)
                last_draw = now
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(ESC + "?25h\n")  # show the cursor again
        sys.stdout.flush()
        try:
            conn.send("rx_sensors_enable:false;")
            conn.send("tx_sensors_enable:false;")
        except OSError:
            pass
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
