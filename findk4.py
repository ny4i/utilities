#!/usr/bin/env python
# Program to broadcast to all NICs to find any K4s on the network
# Tom Schaefer, NY4I
# ny4i@ny4i.com
# May 2024
# Refactored: December 2024

"""
Elecraft K4 Radio Discovery Tool

Broadcasts UDP discovery packets on all network interfaces to find
Elecraft K4 transceivers on the local network and reports their
serial numbers and IP addresses.

Compatible with Python 2.7+ and Python 3.x
"""

from __future__ import print_function
import argparse
import logging
import psutil
import socket
import sys

# Python 2/3 compatibility
PY2 = sys.version_info[0] == 2
PY3 = sys.version_info[0] == 3

# Type hints only available in Python 3.5+
if sys.version_info >= (3, 5):
    from typing import List, Tuple, Dict, Set, Optional
else:
    # Dummy definitions for Python 2/3.0-3.4
    def _dummy_type(*args, **kwargs):
        return None
    List = Tuple = Dict = Set = Optional = _dummy_type

# IP address validation - use ipaddress module if available (Python 3.3+)
# Otherwise use socket.inet_aton for basic validation
try:
    import ipaddress
    HAS_IPADDRESS = True
except ImportError:
    HAS_IPADDRESS = False

# Constants
UDP_SEND_PORT = 9100  # UDP port the K4 listens on for the findk4 byte string
MESSAGE = b"findk4"  # Discovery message sent to K4 radios
DEFAULT_TIMEOUT = 3  # Default socket timeout in seconds
K4_RETURN_START = b'k4'  # Expected start of K4 response
RETURN_DELIMITER = ":"  # Delimiter in K4 response string
RECV_BUFFER_SIZE = 4096  # Maximum UDP packet size for K4 responses
SERIAL_NUMBER_WIDTH = 5  # K4 serial numbers are zero-padded to 5 digits
BROADCAST_ADDRESS = "255.255.255.255"  # Broadcast to all hosts

# Setup logging
logger = logging.getLogger(__name__)



def get_network_interfaces():
    # type: () -> List[str]
    """
    Get all non-loopback IPv4 addresses on this host, including VLANs.

    Returns:
        List of IPv4 addresses as strings
    """
    ip_list = []

    addrs = psutil.net_if_addrs()  # {ifname: [snicaddr, ...]}
    for ifname, addr_list in addrs.items():
        for addr in addr_list:
            if addr.family == socket.AF_INET:
                ip = addr.address
                if ip.startswith("127."):
                    continue  # skip loopback
                ip_list.append(ip)
                logger.debug("Interface %s has IPv4 address %s", ifname, ip)

    logger.debug("Found %d network interface(s)", len(ip_list))
    return ip_list


# def get_network_interfaces():
#     # type: () -> List[str]
#     """
#     Get all IPv4 network interfaces on this host.
# 
#     Returns:
#         List of IPv4 addresses as strings
# 
#     Raises:
#         RuntimeError: If unable to get network interfaces
#     """
#     try:
#         # Use positional args only for Python 2 compatibility
#         interfaces = socket.getaddrinfo(
#             socket.gethostname(),
#             None,
#             socket.AF_INET  # Only IPv4-capable interfaces
#         )
#         ip_list = [ip[-1][0] for ip in interfaces]
#         logger.debug("Found %d network interface(s)", len(ip_list))
#         for ip in ip_list:
#             logger.debug("Interface found with IP address %s", ip)
#         return ip_list
#     except socket.gaierror as e:
#         raise RuntimeError("Failed to get network interfaces: {0}".format(e))


def broadcast_discovery(ip, port):
    # type: (str, int) -> int
    """
    Broadcast K4 discovery message on given interface.

    Args:
        ip: IP address of the interface to broadcast on
        port: UDP port to send discovery message to

    Returns:
        The local port number used for sending (needed for receiving responses)

    Raises:
        OSError: If socket operations fail
    """
    logger.debug("Sending message to network via NIC with IP address %s", ip)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind((ip, 0))  # Bind to interface, let OS assign port
        except (OSError, socket.error) as e:
            raise Exception("Failed to bind to interface {0}: {1}".format(ip, e))

        lhost, lport = sock.getsockname()
        logger.debug("Bound to %s:%d for sending", lhost, lport)

        try:
            sock.sendto(MESSAGE, (BROADCAST_ADDRESS, port))
            logger.debug("Broadcast message sent on port %d", lport)
        except (OSError, socket.error) as e:
            raise Exception("Failed to send broadcast message: {0}".format(e))

        return lport
    finally:
        sock.close()


def validate_ip_address(ip_str):
    # type: (str) -> bool
    """
    Validate that a string is a valid IP address.

    Args:
        ip_str: String to validate as IP address

    Returns:
        True if valid IP address, False otherwise
    """
    if HAS_IPADDRESS:
        try:
            ipaddress.ip_address(ip_str if PY3 else unicode(ip_str))
            return True
        except ValueError:
            return False
    else:
        # Fallback validation using socket.inet_aton
        try:
            socket.inet_aton(ip_str)
            return True
        except socket.error:
            return False


def parse_k4_response(data):
    # type: (bytes) -> Optional[Dict[str, str]]
    """
    Parse K4 response data into structured format.

    Expected format: b'k4:0:192.168.73.108:278'
    Fields: rig_type, rig_index, rig_ip, rig_serial

    Args:
        data: Raw bytes received from K4 radio

    Returns:
        Dictionary with parsed fields, or None if invalid
    """
    if not data.startswith(K4_RETURN_START):
        logger.debug("Response does not start with expected prefix")
        return None

    try:
        data_str = data.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("Failed to decode K4 response as UTF-8")
        return None

    parts = data_str.split(RETURN_DELIMITER)

    if len(parts) != 4:
        logger.warning("Malformed K4 response: expected 4 fields, got %d", len(parts))
        return None

    rig_type, rig_index, rig_ip, rig_serial = parts

    # Validate IP address
    if not validate_ip_address(rig_ip):
        logger.warning("Invalid IP address in K4 response: %s", rig_ip)
        return None

    return {
        'type': rig_type,
        'index': rig_index,
        'ip': rig_ip,
        'serial': rig_serial
    }


def listen_for_responses(lport, timeout):
    # type: (int, int) -> List[Dict[str, str]]
    """
    Listen for K4 responses on the specified port.

    Args:
        lport: Local port to listen on
        timeout: Socket timeout in seconds

    Returns:
        List of parsed K4 responses
    """
    responses = []

    sock_rec = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock_rec.settimeout(timeout)
        try:
            sock_rec.bind(('', lport))
        except (OSError, socket.error) as e:
            logger.error("Failed to bind receive socket to port %d: %s", lport, e)
            return responses

        logger.debug("Listening for responses on port %d", lport)

        # Use while loop to handle multiple replies from same NIC
        # in case multiple K4s are on one network
        while True:
            try:
                data, addr = sock_rec.recvfrom(RECV_BUFFER_SIZE)
                logger.debug("Received message from %s: %s", addr, data)

                parsed = parse_k4_response(data)
                if parsed:
                    responses.append(parsed)
                else:
                    logger.warning("Unexpected data received from %s: %s", addr, data)

            except socket.timeout:
                logger.debug("Socket timeout - no more responses")
                break
            except (OSError, socket.error) as e:
                logger.error("Error receiving data: %s", e)
                break
    finally:
        sock_rec.close()

    return responses


def discover_k4_radios(timeout):
    # type: (int) -> Set[Tuple[str, str]]
    """
    Discover all K4 radios on the network.

    Args:
        timeout: Socket timeout in seconds

    Returns:
        Set of (ip, serial) tuples for discovered radios
    """
    discovered = set()

    try:
        ip_list = get_network_interfaces()
    except RuntimeError as e:
        logger.error(str(e))
        return discovered

    for ip in ip_list:
        try:
            lport = broadcast_discovery(ip, UDP_SEND_PORT)
            responses = listen_for_responses(lport, timeout)

            for response in responses:
                radio_id = (response['ip'], response['serial'])
                if radio_id not in discovered:
                    discovered.add(radio_id)
                    serial_padded = response['serial'].zfill(SERIAL_NUMBER_WIDTH)
                    # K4/0 (K4 Zero) uses "K4Z-" prefix, regular K4 uses "K4-"
                    hostname_prefix = "K4Z" if response['type'].lower() == "k4z" else "K4"
                    logger.info(
    "Found %s serial number %s at IP address %s (%s-SN%s.local)",
    "K4Z" if response['type'].lower() == "k4z" else "K4",
    response['serial'],
    response['ip'],
    "K4Z" if response['type'].lower() == "k4z" else "K4",
    serial_padded
)

        except Exception as e:
            logger.error("Error processing interface %s: %s", ip, e)
            continue

    return discovered


def main():
    # type: () -> int
    """
    Main entry point for K4 discovery tool.

    Returns:
        Exit code: 0 if radios found, 1 if none found
    """
    parser = argparse.ArgumentParser(
        description='Discover Elecraft K4 radios on the local network',
        epilog='Broadcasts UDP discovery packets on all network interfaces'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose debug output'
    )
    parser.add_argument(
        '-t', '--timeout',
        type=int,
        default=DEFAULT_TIMEOUT,
        help='Socket timeout in seconds (default: {0})'.format(DEFAULT_TIMEOUT)
    )

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(levelname)s: %(message)s'
    )

    logger.debug("Python version: %s", sys.version)
    logger.debug("Starting K4 discovery with timeout=%d seconds", args.timeout)
    logger.debug("Discovery message: %s", MESSAGE)

    # Discover radios
    discovered = discover_k4_radios(args.timeout)

    # Report results
    num_radios = len(discovered)
    if num_radios > 0:
        radio_word = "radio" if num_radios == 1 else "radios"
        print("Found {0} K4 {1} accessible to this computer".format(num_radios, radio_word))
        return 0
    else:
        print("No K4 radios found")
        return 1


if __name__ == "__main__":
    sys.exit(main())
