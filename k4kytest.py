#!/usr/bin/env python3
"""
k4kytest -- send CW to an Elecraft K4 with KY commands over its plain CAT port, and watch it.

THIS KEYS THE TRANSMITTER. Use a dummy load or low power.

It talks to the radio directly (TCP port 9200, plain ASCII CAT), with no QK4 or TCI in between,
so it answers questions about the radio itself:

    does a second KY cut off the first, or queue behind it?
    does a 60-character KY work, and what happens past 60?
    does KYW hold the abort back?

The text is split into KY commands the same way QK4 splits it (at a word boundary, with the space
carried to the start of the next piece), then sent. While the radio keys, TQ and TB are polled and
every change is printed with a timestamp. Listening tells you what was keyed; the timeline shows
when the radio was transmitting and how its buffer drained.

    python3 k4kytest.py --wait 10 'CQ CQ CQ DE NY4I NY4I NY4I K' --chunk 22
    python3 k4kytest.py --wait 10 'AAA BBB CCC DDD EEE FFF GGG HHH III JJJ KKK LLL MMM' --chunk 20
    python3 k4kytest.py --wait 10 'LONG TEXT' --kyw all --stop-after 4
    python3 k4kytest.py --wait 10 --qk4 '>TU >599 004 |SK|'   # replay a TCI macro as QK4 sends it
    python3 k4kytest.py 'TEST' --dry-run          # show the commands, connect to nothing
"""

import argparse
import select
import socket
import sys
import time

DEFAULT_HOST = "192.168.73.108"
DEFAULT_PORT = 9200
ABORT = "KY \x04;RX;"

# Characters the K4 acts on inside KY rather than keying (manual, KY entry).
FORBIDDEN = set(";@<>|")


def chunk(text, size):
    """QK4's split: prefer a word boundary, carry the space to the START of the next piece
    (the radio trims trailing spaces, so a space left at the end would run two words together)."""
    if size <= 0:
        return [text]
    pieces = []
    rest = text
    while len(rest) > size:
        cut = rest.rfind(" ", 0, size)
        if cut <= 0:
            cut = size
        pieces.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        pieces.append(rest)
    return pieces


def build_frames(text, size, kyw, pad=0):
    pieces = chunk(text, size)
    frames = []
    for i, piece in enumerate(pieces):
        last = i + 1 == len(pieces)
        if last and pad:
            piece = piece.ljust(pad)
        flag = "W" if kyw == "all" or (kyw == "last" and last) else " "
        frames.append("KY%s%s;" % (flag, piece))
    return frames


# --markers mirrors QK4's TCI path, so a macro can be replayed against the bare radio:
# CwMacro::parse, CatFrames::cwText and TciController::sendCwMacro.
SPEED_STEP = 5
MIN_WPM, MAX_WPM = 8, 100
PROSIGNS = {"KN": "(", "AR": "+", "BT": "=", "AS": "%", "SK": "*", "VE": "!"}


def speed_segments(text, base_wpm):
    """> and < change speed by 5 WPM, cumulative, and are consumed. Returns [(wpm, text)]."""
    wpm = max(MIN_WPM, min(base_wpm if base_wpm > 0 else 20, MAX_WPM))
    segments, current = [], ""
    for ch in text:
        if ch in "<>":
            if current:
                segments.append((wpm, current))
                current = ""
            wpm = max(MIN_WPM, min(wpm + (SPEED_STEP if ch == ">" else -SPEED_STEP), MAX_WPM))
        else:
            current += ch
    if current:
        segments.append((wpm, current))
    return segments


def apply_prosigns(text):
    """|XX| -> the K4's single character; an unknown prosign keeps its letters."""
    out, i = "", 0
    while i < len(text):
        close = text.find("|", i + 1) if text[i] == "|" else -1
        if close < 0:
            out += text[i]
            i += 1
            continue
        name = text[i + 1:close].upper()
        out += PROSIGNS.get(name, name)
        i = close + 1
    return out


def build_macro_frames(text, base_wpm, size, pad, kyw=None):
    """KS before each speed change, KYW on the last KY before a KS, speed restored at the end.

    Mirrors CwMacro::plan in QK4.  The question for the wait flag is "is a speed command coming
    NEXT", never "has the speed moved at some point" -- those differ exactly when a macro ends where
    it started ("TEST >FAST <AGAIN"), and getting it wrong costs a KYW stall of every later command
    for the length of the message, to protect a KS that sets the speed already in force.  A
    68-character message was measured at 35.9 seconds.
    """
    frames = []
    segments = speed_segments(text, base_wpm)
    current = base_wpm
    for i, (wpm, seg) in enumerate(segments):
        if wpm != current:
            frames.append("KS%03d;" % wpm)
            current = wpm
        if i + 1 < len(segments):
            ks_follows = segments[i + 1][0] != current
        else:
            ks_follows = base_wpm > 0 and current != base_wpm

        # kyw=None means "whatever the plan works out", which is what QK4 does.  Passing --kyw
        # explicitly OVERRIDES it, and the reason that matters is one open question: does a KS
        # reach text that is ALREADY in the radio's buffer?  If it does not, KYW is unnecessary
        # everywhere -- and since KYW also makes the radio stop answering TQ;TB; polls for the
        # length of the message, being able to send the same macro without it is the experiment.
        flag = ("last" if ks_follows else "none") if kyw is None else kyw
        frames += build_frames(apply_prosigns(seg), size, flag, pad)
    if base_wpm > 0 and current != base_wpm:
        frames.append("KS%03d;" % base_wpm)
    return frames


def countdown(seconds):
    for left in range(seconds, 0, -1):
        sys.stdout.write("\r  starting in %3ds - Ctrl-C to abort " % left)
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write("\r  starting now" + " " * 30 + "\n")
    sys.stdout.flush()


class Radio:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.setblocking(False)
        self.buf = ""

    def send(self, text):
        self.sock.sendall(text.encode("latin-1"))

    def read(self, timeout):
        """Complete ';'-terminated responses that arrive within timeout."""
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if ready:
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("radio closed the connection")
            self.buf += data.decode("latin-1", errors="replace")
        out = []
        while ";" in self.buf:
            msg, _, self.buf = self.buf.partition(";")
            out.append(msg.strip() + ";")
        return out

    def ask(self, query, prefix, timeout=2.0):
        self.send(query)
        deadline = time.time() + timeout
        while time.time() < deadline:
            for msg in self.read(0.1):
                if msg.startswith(prefix):
                    return msg
        return None

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def printable(frame):
    return frame.replace("\x04", "<EOT>")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", help="CW text to send (quote it)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--wait", type=int, default=0, metavar="N",
                    help="connect, then wait N seconds before keying")
    ap.add_argument("--chunk", type=int, default=60, metavar="N",
                    help="split into KY commands of at most N characters (QK4 uses 22; "
                         "0 = one KY however long, to test the 60-character limit)")
    ap.add_argument("--kyw", choices=("none", "last", "all"), default=None,
                    help="which KY commands get the W (wait) flag")
    ap.add_argument("--gap", type=float, default=0.0, metavar="SECONDS",
                    help="pause between KY commands (default: back to back, as QK4 sends them)")
    ap.add_argument("--stop-after", type=float, default=0.0, metavar="SECONDS",
                    help="send the abort (KY <EOT>;RX;) SECONDS after the first KY")
    ap.add_argument("--markers", action="store_true",
                    help="treat > and < as TCI speed markers (+/-5 WPM, cumulative) and |XX| as "
                         "prosigns, and send them the way QK4 does: KS before each speed change, "
                         "KYW on the last KY before a KS, the speed put back at the end. "
                         "Overrides --kyw")
    ap.add_argument("--pad", type=int, default=0, metavar="N",
                    help="space-pad the last KY of each run to N characters (QK4 pads to 22)")
    ap.add_argument("--qk4", action="store_true",
                    help="exactly what QK4 sends today: --markers --chunk 60 --pad 0")
    ap.add_argument("--wpm", type=int, default=20, metavar="N",
                    help="base speed for --markers with --dry-run (a live run reads KS from the radio)")
    ap.add_argument("--poll", type=float, default=0.1, metavar="SECONDS",
                    help="how often to ask TQ; and TB; while keying (default 0.1). The radio holds "
                         "these queries while a KYW is sending, so gaps in the timeline show that")
    ap.add_argument("--timeout", type=float, default=120.0, metavar="SECONDS",
                    help="give up waiting for the radio to finish after this long")
    ap.add_argument("--raw", action="store_true", help="print every response, not just TQ/TB/KY")
    ap.add_argument("--dry-run", action="store_true", help="print the commands and exit")
    args = ap.parse_args()

    if args.wait < 0:
        ap.error("--wait must be 0 or more")
    if args.poll <= 0:
        ap.error("--poll must be more than 0")
    if args.qk4:
        args.markers, args.chunk, args.pad = True, 60, 0
    # With --markers, < > and | are macro grammar and are consumed before anything reaches KY.
    bad = sorted(FORBIDDEN & set(args.text) - (set("<>|") if args.markers else set()))
    if bad:
        ap.error("text contains %s, which the K4 acts on inside KY instead of keying" % " ".join(bad))

    def frames_for(base_wpm):
        if args.markers:
            return build_macro_frames(args.text, base_wpm, args.chunk, args.pad, args.kyw)
        return build_frames(args.text, args.chunk, args.kyw or "none", args.pad)

    def show(frames):
        kys = [f for f in frames if f.startswith("KY")]
        print("%d characters -> %d KY command(s), %d command(s) in all:"
              % (len(args.text), len(kys), len(frames)))
        for f in frames:
            if f.startswith("KY"):
                print("    %-70s (%d chars of text)" % (repr(f), len(f) - 4))
            else:
                print("    %r" % f)
        if any(len(f) - 4 > 60 for f in kys):
            print("  NOTE: over the manual's 60-character KY limit")
        if any(set(f[3:-1]) & FORBIDDEN for f in kys):
            print("  NOTE: a KY carries a character the K4 acts on - check the text")

    if args.dry_run:
        show(frames_for(args.wpm))
        return 0

    try:
        radio = Radio(args.host, args.port)
    except OSError as e:
        print("cannot connect to %s:%d -- %s" % (args.host, args.port, e))
        return 1

    try:
        ks = radio.ask("KS;", "KS")
        wpm = int(ks[2:-1]) if ks and ks[2:-1].isdigit() else 0
        print("connected to %s:%d, keyer speed %s" % (args.host, args.port,
                                                     "%d WPM" % wpm if wpm else "unknown"))
        if args.markers and not wpm:
            print("keyer speed unknown - not sending a macro whose speeds depend on it")
            return 1
        frames = frames_for(wpm)
        show(frames)
        if args.wait:
            countdown(args.wait)
        radio.read(0.2)  # drop anything that arrived during the wait

        t0 = time.time()
        stamp = lambda: "+%6.2fs" % (time.time() - t0)
        for i, frame in enumerate(frames):
            if i and args.gap:
                time.sleep(args.gap)
            radio.send(frame)
            print("%s  >> %s" % (stamp(), printable(frame)))

        stopped = False
        seen_tx = False
        first_tx = last_tx_end = None
        seen_reply = False
        first_reply = None
        last = {}
        quiet_since = None
        next_poll = 0.0
        while time.time() - t0 < args.timeout:
            now = time.time()
            if args.stop_after and not stopped and now - t0 >= args.stop_after:
                radio.send(ABORT)
                stopped = True
                print("%s  >> %s" % (stamp(), printable(ABORT)))
            if now >= next_poll:
                radio.send("TQ;TB;")
                next_poll = now + args.poll

            for msg in radio.read(0.05):
                kind = msg[:2]
                if kind in ("TQ", "TX", "RX"):
                    txing = msg in ("TQ1;", "TX;")
                    if txing and first_tx is None:
                        first_tx = time.time()
                    if txing:
                        seen_tx = True
                        last_tx_end = None
                    elif seen_tx and last_tx_end is None:
                        last_tx_end = time.time()
                    kind = "TQ"
                    msg = "TQ1;" if txing else "TQ0;"
                if kind in ("TQ", "TB", "KY"):
                    if not seen_reply:
                        first_reply = time.time()
                    seen_reply = True
                if kind in ("TQ", "TB", "KY") or args.raw:
                    if last.get(kind) != msg:
                        print("%s  << %s" % (stamp(), msg))
                        last[kind] = msg

            # TB's count is the THREE DIGITS after the name -- TB000; is empty, TB003IB; is three
            # characters plus what the radio decoded.  The old test was startswith("TB0"), which
            # matched both and so tested nothing.
            tb = last.get("TB", "TB000;")
            idle = last.get("TQ") == "TQ0;" and tb[2:5] == "000"

            # `seen_reply`, not just `seen_tx`.  A KYW message makes the radio hold every following
            # host command -- including the TQ;TB; polls this loop sends -- until the text has been
            # keyed.  The first answer therefore arrives AFTER transmit has already finished, so
            # TQ1 is never observed and seen_tx stays false forever: the tool ran to its timeout on
            # a message that had completed in six seconds.  Having heard anything at all is the
            # honest precondition; the two-second quiet window is what actually decides.
            if (seen_tx or seen_reply) and idle:
                quiet_since = quiet_since or time.time()
                if time.time() - quiet_since > 2.0:  # longer than any word gap at contest speed
                    break
            else:
                quiet_since = None
        else:
            print("timed out after %.0fs" % args.timeout)
    except KeyboardInterrupt:
        print("\ninterrupted - sending abort")
        radio.send(ABORT)
        return 1
    finally:
        radio.close()

    print("\n" + "=" * 60)
    end = last_tx_end or time.time()

    # A KYW message makes the radio hold every following host command until the text has been
    # keyed, INCLUDING the TQ; polls this tool uses to see transmit start.  When that happens the
    # first TQ1 arrives near the END of the message, and "transmitted for" measured from it reports
    # a fraction of a second for a transmission of many -- a reading worse than none, because it
    # looks like an answer.  Anything later than a poll interval after the commands went out means
    # the radio was not answering, so the start is unobservable and is reported as such.
    blinded = first_reply is not None and (first_reply - t0) > max(0.5, args.poll * 3)

    if not seen_tx and not blinded:
        print("  radio never reported transmitting")
        return 1
    if blinded:
        print("  keyed for %.1fs measured from the send -- the radio stopped answering for the"
              % (end - t0))
        print("  first %.1fs (KYW holds host commands), so the start of transmit was not visible"
              % (first_reply - t0))
    else:
        print("  transmitted for %.1fs" % (end - first_tx))
    return 0


if __name__ == "__main__":
    sys.exit(main())
