#!/usr/bin/env python3
"""
findKPA1500.py - Discover Elecraft KPA1500 amplifiers on the local network.

Copyright (C) 2026 Tom Schaefer, NY4I

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

THIS SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
USE AT YOUR OWN RISK.

Cross-platform: macOS, Linux, Windows. Requires Python 3.8+ and psutil
(pip install psutil).

Broadcast-primary strategy: one packet per subnet, sweep only as backup.
  1. Enumerate every local IPv4 interface (psutil.net_if_addrs(), no ifconfig).
  2. For each interface, send a single broadcast ^ON; to that subnet's directed
     broadcast (e.g. 192.168.73.255) from a socket bound to the interface's IP,
     and listen for ^...; replies for the timeout window. Mirrors findk4.py.
  3. Sweep fallback: only on subnets where broadcast got no reply, unicast-probe
     every host in the subnet. Catches firmware or networks that drop broadcast
     UDP/1500 (managed switches with storm control, etc.).
  4. Best-effort MAC lookup via the OS ARP table for WOL-setup convenience.

Tested: current KPA1500 firmware processes ^ON; on UDP/1500 from a subnet
directed broadcast and replies unicast (^ON1; on, ^ON0; off).

Authoritative identification is always the protocol reply, not the OUI — the
OUI is only used to shrink the fast-path target list.

The amplifier must be powered on and on the same L2 subnet as one of this
host's interfaces (ARP and UDP unicast do not cross routers automatically).
"""

import argparse
import concurrent.futures
import ipaddress
import logging
import platform
import re
import socket
import subprocess
import sys

try:
    import psutil
except ImportError:
    sys.stderr.write(
        "Error: psutil is required. Install with: pip install psutil\n"
    )
    sys.exit(2)

KPA1500_UDP_PORT = 1500
PROBE_COMMAND = b"^ON;"
DEFAULT_UDP_TIMEOUT_S = 1.0
PROBE_WORKERS = 128
MAX_SUBNET_HOSTS = 4096

# Post-discovery enrichment: each entry is (label, command_bytes).
# ^RVM; -> ^RVM03.06; (main firmware revision)
# ^SN;  -> ^SN00207;  (serial number)
ENRICH_COMMANDS = (
    ("fw", b"^RVM;"),
    ("sn", b"^SN;"),
)

# Known Elecraft vendor OUIs used by the KPA1500 network module. Extend as
# users report new prefixes via the channel below.
ELECRAFT_OUIS = ("54:10:ec",)
REPORT_EMAIL = "ny4i@ny4i.com"

# macOS / Linux net-tools: "? (192.168.1.1) at 60:22:32:6f:95:4f ..."
ARP_UNIX_RE = re.compile(r"\(([\d.]+)\)\s+at\s+([0-9a-f:]+)", re.IGNORECASE)
# Windows arp -a:           "  192.168.1.1           60-22-32-6f-95-4f     dynamic"
ARP_WIN_RE = re.compile(
    r"^\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{2}(?:[-:][0-9a-f]{2}){5})\s",
    re.IGNORECASE | re.MULTILINE,
)
# iproute2 `ip neigh show`: "192.168.1.1 dev eth0 lladdr b8:27:eb:00:00:01 REACHABLE"
# Modern Debian/Pi OS lack net-tools' arp; iproute2 is always present.
ARP_IP_NEIGH_RE = re.compile(
    r"^(\d+\.\d+\.\d+\.\d+)\s+dev\s+\S+\s+lladdr\s+([0-9a-f:]+)",
    re.IGNORECASE | re.MULTILINE,
)

log = logging.getLogger("findkpa1500")


def detect_local_interfaces():
    """Return list of (src_ip, /24 subnet) tuples, one per usable IPv4 interface.

    Auto-detected interfaces are normalized to /24 regardless of the interface's
    actual netmask. Reason: ham networks are nearly always /24, and a literal
    /8 broadcast (e.g. 10.255.255.255) is both unwieldy to sweep and may not
    reach amps with narrower masks on the same wire. A user with a non-/24
    layout can override with --subnet.
    """
    ifaces = []
    seen = set()
    for ifname, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family != socket.AF_INET:
                continue
            ip = addr.address
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            try:
                net = ipaddress.ip_network(f"{ip}/24", strict=False)
            except ValueError:
                continue
            key = (ip, net)
            if key in seen:
                continue
            seen.add(key)
            log.debug("Interface %s: src=%s subnet=%s (forced /24)", ifname, ip, net)
            ifaces.append((ip, net))
    return ifaces


def probe_kpa1500(dst_ip, timeout, src_ip=None):
    """Send ^ON; via UDP/1500. Return reply bytes if it looks like a KPA1500 frame.

    src_ip pins the outbound interface (mirrors findk4.py's per-NIC bind so
    packets ignore default-route weirdness like VPNs).
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    sock.settimeout(timeout)
    try:
        if src_ip:
            try:
                sock.bind((src_ip, 0))
            except OSError as e:
                log.debug("Bind to %s failed (%s); skipping %s", src_ip, e, dst_ip)
                return None
        try:
            sock.sendto(PROBE_COMMAND, (dst_ip, KPA1500_UDP_PORT))
        except OSError:
            return None
        try:
            data, _ = sock.recvfrom(1024)
        except (socket.timeout, OSError):
            return None
        stripped = data.strip()
        if stripped.startswith(b"^") and stripped.endswith(b";"):
            return data
        return None
    finally:
        sock.close()


def normalize_mac(mac):
    """Lowercase, convert dashes to colons, zero-pad each octet."""
    parts = mac.lower().replace("-", ":").split(":")
    if len(parts) != 6:
        return mac.lower()
    return ":".join(p.zfill(2) for p in parts)


def _run_and_parse_arp(cmd, pattern):
    """Run cmd, parse stdout for ip/mac pairs. Returns {} on failure or no matches."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=5
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.debug("%s unavailable: %s", " ".join(cmd), e)
        return {}
    entries = {}
    for m in pattern.finditer(out):
        entries[m.group(1)] = normalize_mac(m.group(2))
    return entries


def read_arp_table():
    """Best-effort ARP table read for MAC display. Returns {ip: mac}; {} on failure.

    Linux is tried via iproute2's `ip neigh` first (always present), falling back
    to net-tools' `arp -an` (often absent on modern Debian/Pi OS). macOS uses
    `arp -an`; Windows uses `arp -a`.
    """
    system = platform.system().lower()
    if system == "windows":
        attempts = [(["arp", "-a"], ARP_WIN_RE)]
    elif system == "linux":
        attempts = [
            (["ip", "neigh", "show"], ARP_IP_NEIGH_RE),
            (["arp", "-an"], ARP_UNIX_RE),
        ]
    else:  # macOS, *BSD
        attempts = [(["arp", "-an"], ARP_UNIX_RE)]

    for cmd, pattern in attempts:
        entries = _run_and_parse_arp(cmd, pattern)
        if entries:
            return entries
    log.debug("No ARP source produced entries; MAC enrichment disabled")
    return {}


def query_amp(dst_ip, command, timeout, src_ip=None):
    """Send a single ^XX; command to the amp and return the framed reply bytes."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    sock.settimeout(timeout)
    try:
        if src_ip:
            try:
                sock.bind((src_ip, 0))
            except OSError:
                return None
        try:
            sock.sendto(command, (dst_ip, KPA1500_UDP_PORT))
            data, _ = sock.recvfrom(1024)
        except (OSError, socket.timeout):
            return None
        stripped = data.strip()
        if stripped.startswith(b"^") and stripped.endswith(b";"):
            return stripped
        return None
    finally:
        sock.close()


def parse_framed(reply, command):
    """Extract the payload from ^XX<payload>; given the original command bytes."""
    if reply is None:
        return None
    # command is like b'^RVM;' — strip its ^XX prefix (len-1) and the trailing ';'
    prefix_len = len(command) - 1
    return reply[prefix_len:-1].decode("ascii", errors="replace")


def enrich_amp(ip, timeout, src_ip=None):
    """Query each ENRICH_COMMANDS entry; return {label: payload_or_None}."""
    info = {}
    for label, cmd in ENRICH_COMMANDS:
        info[label] = parse_framed(query_amp(ip, cmd, timeout, src_ip), cmd)
    return info


def probe_targets(targets, udp_timeout, src_ip=None):
    """Parallel UDP probe a list of IPs from optional src_ip. Returns [(ip, reply), ...]."""
    found = []
    if not targets:
        return found
    with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        futures = {
            pool.submit(probe_kpa1500, ip, udp_timeout, src_ip): ip for ip in targets
        }
        for fut in concurrent.futures.as_completed(futures):
            reply = fut.result()
            if reply is not None:
                ip = futures[fut]
                found.append((ip, reply))
                log.info("KPA1500 at %s: %r", ip, reply)
    return found


def broadcast_discover(src_ip, net, timeout):
    """Send ^ON; to subnet directed broadcast from src_ip; collect ^...; replies."""
    found = []
    bcast = str(net.broadcast_address)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    except OSError as e:
        log.debug("Socket create failed for %s: %s", src_ip, e)
        return found
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind((src_ip, 0))
        except OSError as e:
            log.debug("Bind to %s failed: %s", src_ip, e)
            return found
        sock.settimeout(timeout)
        try:
            sock.sendto(PROBE_COMMAND, (bcast, KPA1500_UDP_PORT))
        except OSError as e:
            log.debug("Broadcast send via %s failed: %s", src_ip, e)
            return found
        # Drain replies until the socket times out — supports multiple amps
        while True:
            try:
                data, addr = sock.recvfrom(1024)
            except (socket.timeout, OSError):
                break
            stripped = data.strip()
            if not (stripped.startswith(b"^") and stripped.endswith(b";")):
                log.debug("Ignoring non-KPA1500 reply from %s: %r", addr, data)
                continue
            ip = addr[0]
            if ip == src_ip:
                continue  # ignore loopback echo of our own broadcast
            found.append((ip, data))
            log.info("KPA1500 at %s (broadcast via %s): %r", ip, src_ip, data)
    finally:
        sock.close()
    return found


def discover(interfaces, udp_timeout):
    """Broadcast-primary, sweep-fallback discovery.

    interfaces: list of (src_ip, subnet) tuples. src_ip may be None when the
    user specified --subnet without a corresponding local interface; in that
    case broadcast is skipped (it needs a bind address) and we go straight to
    sweep via the routing table.
    """
    all_found = []
    for src_ip, net in interfaces:
        via = f" via {src_ip}" if src_ip else ""
        if src_ip:
            log.info("Broadcasting ^ON; on %s%s", net, via)
            replies = broadcast_discover(src_ip, net, udp_timeout)
            if replies:
                all_found.extend((src_ip, ip, reply) for ip, reply in replies)
                continue
            log.info("No broadcast replies on %s%s; sweeping", net, via)
        else:
            log.info("Subnet %s has no local interface; sweeping via routing", net)

        if net.num_addresses > MAX_SUBNET_HOSTS:
            log.warning(
                "Subnet %s has %d addresses (> sweep cap %d); skipping unicast "
                "sweep. Broadcast was already tried.",
                net, net.num_addresses, MAX_SUBNET_HOSTS,
            )
            continue
        hosts = [str(ip) for ip in net.hosts()]
        log.info("Sweeping %d hosts in %s%s", len(hosts), net, via)
        all_found.extend(
            (src_ip, ip, reply)
            for ip, reply in probe_targets(hosts, udp_timeout, src_ip=src_ip)
        )

    # Dedupe by IP (multiple interfaces could observe the same amp)
    seen = set()
    deduped = []
    for src_ip, ip, reply in all_found:
        if ip not in seen:
            seen.add(ip)
            deduped.append((src_ip, ip, reply))
    return deduped


def main():
    parser = argparse.ArgumentParser(
        description="Discover Elecraft KPA1500 amplifiers on the local network."
    )
    parser.add_argument(
        "-s", "--subnet",
        action="append",
        help="CIDR to scan (e.g. 192.168.73.0/24). Repeatable. "
        "If omitted, all local IPv4 subnets are auto-detected.",
    )
    parser.add_argument(
        "-t", "--timeout",
        type=float, default=DEFAULT_UDP_TIMEOUT_S,
        help="UDP probe timeout in seconds (default: %(default)s).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    if args.subnet:
        try:
            subnets = [ipaddress.ip_network(s, strict=False) for s in args.subnet]
        except ValueError as e:
            log.error("Invalid subnet: %s", e)
            return 2
        interfaces = [(None, net) for net in subnets]
    else:
        interfaces = detect_local_interfaces()
        if not interfaces:
            log.error("No local IPv4 interfaces detected; specify --subnet.")
            return 2
        log.info(
            "Auto-detected interfaces: %s",
            ", ".join(f"{src}->{net}" for src, net in interfaces),
        )

    found = discover(interfaces, udp_timeout=args.timeout)
    if not found:
        print("No KPA1500 discovered.")
        return 1

    arp = read_arp_table()
    print(f"Discovered {len(found)} KPA1500(s):")
    unknown_oui = []
    for src_ip, ip, reply in sorted(found, key=lambda t: ipaddress.ip_address(t[1])):
        mac = arp.get(ip, "?")
        info = enrich_amp(ip, args.timeout, src_ip=src_ip)
        fw = info.get("fw") or "?"
        sn = info.get("sn") or "?"
        print(f"  {ip}  mac={mac}  fw={fw}  sn={sn}  reply={reply!r}")
        if mac != "?" and not any(mac.startswith(o) for o in ELECRAFT_OUIS):
            unknown_oui.append((ip, mac))

    if unknown_oui:
        print()
        print("NOTE: discovered KPA1500(s) with an unrecognized vendor OUI:")
        for ip, mac in unknown_oui:
            print(f"  {ip}  mac={mac}  (OUI prefix {mac[:8]} not in known list)")
        print(
            "Please report so the fast path can be updated: "
            f"email {REPORT_EMAIL} or open an issue on the project repo."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
