#!/usr/bin/env python3
"""
shackmaster.py - CLI tool for RigExpert ShackMaster Power 600

Usage:
   python shackmaster.py on               # turn power output ON
   python shackmaster.py off              # turn power output OFF
   python shackmaster.py status           # print ON or OFF
   python shackmaster.py monitor          # live analog values (Ctrl+C to stop)
   python shackmaster.py monitor --interval 5.0
"""

import sys
import subprocess

def _ensure_dependencies():
   required = {'pywinusb': 'pywinusb'}
   missing = []
   for module, package in required.items():
      try:
         __import__(module)
      except ImportError:
         missing.append(package)
   if missing:
      print('Installing dependencies: {}...'.format(', '.join(missing)))
      subprocess.check_call(
         [sys.executable, '-m', 'pip', 'install', '--quiet'] + missing)
      print('Done.\n')

_ensure_dependencies()

import time
import struct
import argparse
import threading
import datetime
from pywinusb import hid

VENDOR_ID        = 0x0483
PRODUCT_ID       = 0xa1de
REPORT_ID        = 7
RESPONSE_TIMEOUT = 5.0   # seconds to wait for a device response


# ---------------------------------------------------------------------------
# Device class
# ---------------------------------------------------------------------------

class ShackMasterDevice:
   """
   Wraps a single ShackMaster Power 600 HID connection.

   Designed as a context manager so the caller never has to remember to
   call close():

      with ShackMasterDevice() as dev:
         dev.power_on()

   Thread safety: pywinusb delivers RX data on its own thread.  We use a
   threading.Event + a plain bytes attribute to hand data back to the
   calling thread.  Only one command should be in-flight at a time (the
   CLI never issues concurrent commands), so no additional locking is needed.
   """

   def __init__(self):
      self._device  = None
      self._report  = None
      self._rx_event = threading.Event()
      self._rx_data  = None   # bytes set by _rx_handler, read by _send_recv

   # --- context manager ---------------------------------------------------

   def __enter__(self):
      if not self.open():
         raise RuntimeError(
            'ShackMaster Power 600 not found. '
            'Check USB connection (vendor 0x{:04X} / product 0x{:04X}).'.format(
               VENDOR_ID, PRODUCT_ID))
      return self

   def __exit__(self, *args):
      self.close()

   # --- connection --------------------------------------------------------

   def open(self):
      """Find the device, open it, and register the RX handler."""
      hid_filter = hid.HidDeviceFilter(vendor_id=VENDOR_ID, product_id=PRODUCT_ID)
      devices = hid_filter.get_devices()
      if not devices:
         return False

      for dev in devices:
         dev.open()
         for rep in dev.find_output_reports():
            if rep.report_id == REPORT_ID:
               self._device = dev
               self._report = rep
               break
         if self._report:
            break
         dev.close()

      if not (self._device and self._report):
         return False

      self._device.set_raw_data_handler(self._rx_handler)
      return True

   def close(self):
      if self._device:
         self._device.close()
         self._device = None
         self._report = None

   # --- internal transport ------------------------------------------------

   def _rx_handler(self, data):
      """
      Called by pywinusb on its reader thread when an IN report arrives.
      data[0] = report_id (0x07)
      data[1] = PDU length
      data[2 : 2+length] = payload bytes
      """
      length = data[1]
      self._rx_data = bytes(data[2 : 2 + length])
      self._rx_event.set()

   def _send(self, payload_bytes):
      """Send a single HID output report."""
      packet = [REPORT_ID, len(payload_bytes)] + list(payload_bytes) + [0] * 65
      self._report.send(packet[:64])

   def _send_recv(self, payload_bytes, timeout=RESPONSE_TIMEOUT):
      """
      Send a command and block until one IN report arrives or timeout.
      Returns the raw payload bytes.
      Raises TimeoutError if no response arrives in time.
      """
      self._rx_event.clear()
      self._rx_data = None
      self._send(payload_bytes)
      if not self._rx_event.wait(timeout):
         raise TimeoutError(
            'No response from device within {:.1f}s'.format(timeout))
      return self._rx_data

   # --- public commands ---------------------------------------------------

   def get_status(self):
      """
      Send POWER\\n and return 'ON' or 'OFF'.
      The device echoes the state several times in one packet; we take the
      first token.
      """
      response = self._send_recv(b'POWER\n')
      text = response.decode('ascii', errors='replace').strip()
      tokens = text.split()
      return tokens[0] if tokens else 'UNKNOWN'

   def power_on(self):
      """Send PSW1\\r\\n (no IN response; brief sleep lets the relay settle)."""
      self._send(b'PSW1\r\n')
      time.sleep(1.0)

   def power_off(self):
      """Send PSW0\\r\\n (no IN response; brief sleep lets the relay settle)."""
      self._send(b'PSW0\r\n')
      time.sleep(1.0)

   def get_analog_values(self):
      """
      Send the 0x0C descriptor and return a dict of parsed measurements.
      See _parse_analog() for field details.
      """
      response = self._send_recv(bytes([0x0C]))
      return _parse_analog(response)


# ---------------------------------------------------------------------------
# Protocol parsing
# ---------------------------------------------------------------------------

def _scale(value, divisor):
   """Divide value by divisor, or return None if value is None."""
   return None if value is None else value / divisor


def _fmt_version(major, minor, build):
   """Format firmware version, or return 'N/A' if any component is None."""
   if None in (major, minor, build):
      return 'N/A'
   return '{}.{}.{}'.format(major, minor, build)


def _parse_analog(payload):
   """
   Parse the 'Get analog values' (0x0C) response payload.

   payload[0]  = 0x0C descriptor  (absolute HID packet offset 2)
   payload[1:] = measurement data (absolute offsets 3..49)

   All offsets in the comments below are the *absolute* offsets from the
   HID packet (as documented in the protocol PDF).  We subtract 2 to get
   the payload-relative offset passed to struct.unpack_from / indexing.
   """

   n = len(payload)

   def u8(abs_off):
      idx = abs_off - 2
      return payload[idx] if idx < n else None

   def i8(abs_off):
      idx = abs_off - 2
      return struct.unpack_from('b', payload, idx)[0] if idx < n else None

   def i16(abs_off):
      idx = abs_off - 2
      return struct.unpack_from('>h', payload, idx)[0] if idx + 2 <= n else None

   def u16(abs_off):
      idx = abs_off - 2
      return struct.unpack_from('>H', payload, idx)[0] if idx + 2 <= n else None

   def u32(abs_off):
      idx = abs_off - 2
      return struct.unpack_from('>I', payload, idx)[0] if idx + 4 <= n else None

   ts_unix = u32(24)
   ts_str  = (datetime.datetime.fromtimestamp(ts_unix, datetime.timezone.utc)
              .strftime('%Y-%m-%d %H:%M:%S UTC') if ts_unix is not None else 'N/A')

   return {
      # 12V main output
      'voltage_12v':   _scale(u8(3),  10.0),   # V
      'current_12v':   _scale(i16(4), 10.0),   # A

      # USB ports (voltage x10, current x100) — may be None if firmware
      # returns fewer bytes than the PDF specifies
      'voltage_usb1':  _scale(u8(6),   10.0),
      'current_usb1':  _scale(i16(7),  100.0),
      'voltage_usb2':  _scale(u8(40),  10.0),
      'current_usb2':  _scale(i16(41), 100.0),
      'voltage_usb3':  _scale(u8(43),  10.0),
      'current_usb3':  _scale(i16(44), 100.0),
      'voltage_usb4':  _scale(u8(46),  10.0),
      'current_usb4':  _scale(i16(47), 100.0),

      # Environment
      'temp_inside':   i8(9),              # °C
      'temp_outside':  i8(10),             # °C
      'mains_voltage': i16(11),            # V RMS
      'mains_freq':    _scale(i16(13), 10.0),  # Hz

      # Load summary
      'fan_duty':      u8(15),
      'total_power':   i16(16),            # W
      'total_current': _scale(i16(18), 10.0),  # A
      'max_power':     i16(20),            # W
      'max_current':   _scale(i16(22), 10.0),  # A

      # Device info
      'timestamp':     ts_str,
      'error1':        u16(28),
      'error2':        u16(30),
      'serial':        u32(32),
      'accel_pos':     u8(36),
      'version':       _fmt_version(u8(37), u8(38), u8(39)),
   }


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _print_analog(vals):
   print('ShackMaster Power 600  |  {}  |  S/N: {:d}  |  FW: {}'.format(
      vals['timestamp'], vals['serial'], vals['version']))
   print()

   header = '  {:<10}  {:>8}  {:>9}  {:>8}'.format(
      'Channel', 'Voltage', 'Current', 'Power')
   print(header)
   print('  ' + '-' * (len(header) - 2))

   # (i_fmt: 12V has 0.1A resolution; USB ports have 0.01A resolution)
   channels = [
      ('12V',   vals['voltage_12v'],  vals['current_12v'],  '.1f'),
      ('USB 1', vals['voltage_usb1'], vals['current_usb1'], '.2f'),
      ('USB 2', vals['voltage_usb2'], vals['current_usb2'], '.2f'),
      ('USB 3', vals['voltage_usb3'], vals['current_usb3'], '.2f'),
      ('USB 4', vals['voltage_usb4'], vals['current_usb4'], '.2f'),
   ]

   for name, v, i, i_fmt in channels:
      if v is None or i is None:
         print('  {:<10}  {:>7}   {:>8}   {:>7}'.format(name, 'N/A', 'N/A', 'N/A'))
      else:
         p = v * i
         print(('  {:<10}  {:>7.1f}V  {:>8' + i_fmt + '}A  {:>7.1f}W').format(
            name, v, i, p))

   def fmt(val, spec, suffix=''):
      return ('{:{spec}}{}'.format(val, suffix, spec=spec)
              if val is not None else 'N/A')

   print()
   print('  Total          {}  {}  (limit: {} / {})'.format(
      fmt(vals['total_current'], '.2f', 'A'),
      fmt(vals['total_power'],   '.0f', 'W'),
      fmt(vals['max_power'],     'd',   'W'),
      fmt(vals['max_current'],   '.1f', 'A')))
   print()
   print('  Mains          {}  {}'.format(
      fmt(vals['mains_voltage'], '.0f', 'V RMS'),
      fmt(vals['mains_freq'],    '.1f', ' Hz')))
   print('  Temperature    inside {}  /  outside {}'.format(
      fmt(vals['temp_inside'],  'd', 'C'),
      fmt(vals['temp_outside'], 'd', 'C')))
   print('  Fan            {}'.format(fmt(vals['fan_duty'], 'd', '%')))

   e1, e2 = vals['error1'], vals['error2']
   if e1 or e2:
      print()
      print('  *** ERRORS:  error1=0x{:04X}  error2=0x{:04X} ***'.format(
         e1 or 0, e2 or 0))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_on(device, _args):
   print('Turning power ON...')
   device.power_on()
   status = device.get_status()
   print('Power status: {}'.format(status))


def cmd_off(device, _args):
   print('Turning power OFF...')
   device.power_off()
   status = device.get_status()
   print('Power status: {}'.format(status))


def cmd_status(device, _args):
   status = device.get_status()
   print('Power status: {}'.format(status))


def cmd_monitor(device, args):
   interval = args.interval
   print('Monitoring — press Ctrl+C to stop\n')
   try:
      first = True
      while True:
         vals = device.get_analog_values()
         if not first:
            # Move cursor to top of screen (ANSI; works on Windows 10+ and Linux)
            sys.stdout.write('\033[2J\033[H')
         first = False
         _print_analog(vals)
         time.sleep(interval)
   except KeyboardInterrupt:
      print('\nStopped.')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
   parser = argparse.ArgumentParser(
      description='RigExpert ShackMaster Power 600 — command line interface',
      formatter_class=argparse.RawDescriptionHelpFormatter,
      epilog='\n'.join([
         'examples:',
         '  python shackmaster.py on',
         '  python shackmaster.py status',
         '  python shackmaster.py monitor --interval 5',
      ]))

   sub = parser.add_subparsers(dest='command', required=True)

   sub.add_parser('on',     help='Turn power supply output ON')
   sub.add_parser('off',    help='Turn power supply output OFF')
   sub.add_parser('status', help='Print current power supply status (ON/OFF)')

   mon = sub.add_parser('monitor', help='Continuously display analog measurements')
   mon.add_argument(
      '--interval', type=float, default=2.0,
      metavar='SECONDS', help='Polling interval in seconds (default: 2.0)')

   args = parser.parse_args()

   dispatch = {
      'on':      cmd_on,
      'off':     cmd_off,
      'status':  cmd_status,
      'monitor': cmd_monitor,
   }

   try:
      with ShackMasterDevice() as device:
         dispatch[args.command](device, args)
   except RuntimeError as exc:
      print('Error: {}'.format(exc), file=sys.stderr)
      sys.exit(1)
   except TimeoutError as exc:
      print('Timeout: {}'.format(exc), file=sys.stderr)
      sys.exit(1)


if __name__ == '__main__':
   main()
