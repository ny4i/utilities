#!/usr/bin/env python3
"""
hamshack.py - config-driven power sequencer for the ham shack.

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

Overview
--------
The shack's power-up order is described in an external JSON config file
(hamshack.json, next to this script).  Powering up walks the "sequence" list
top-to-bottom; powering down walks it in reverse (LIFO: the last thing
switched on is the first switched off).

The config file is station-specific (it holds your DLI host and outlet
layout), so it is git-ignored.  A committed hamshack.sample.json documents the
format; copy it to hamshack.json and edit for your station.

Two kinds of step exist:

   dli      - a Digital Loggers (DLI) web-power-switch relay.  After the PUT
              that requests on/off, we do NOT trust that the change is
              instantaneous: the DLI honors on-sequence and cycle delays, so
              the *physical* state can lag the command by seconds.  We poll
              physical_state until it matches the request or a timeout elapses,
              and report PASS/FAIL accordingly.

   command  - an external command (e.g. the ShackMaster CLI, k4up/k4down).
              These own their own verification, so we run them and check the
              process exit code.  An optional retry window tolerates a device
              that needs time to become ready (see Command).

Credentials for the DLI reuse the existing, proven `curl --digest --netrc`
path, so there is a single source of credentials (the user's .netrc) and no
extra Python dependencies.

Usage:
   python hamshack.py up        # power-up sequence, each outlet verified
   python hamshack.py down      # power-down (reverse order), each outlet verified
   python hamshack.py status    # show physical_state of all managed outlets
   (on / off are accepted as aliases for up / down)
"""

import os
import sys
import json
import time
import argparse
import subprocess

# ---------------------------------------------------------------------------
# Paths and runtime configuration
# ---------------------------------------------------------------------------

_HERE   = os.path.dirname(os.path.abspath(__file__))
_PYTHON = sys.executable

CONFIG_PATH = os.path.join(_HERE, 'hamshack.json')
SAMPLE_PATH = os.path.join(_HERE, 'hamshack.sample.json')

# These are populated from the config file at startup (see load_config /
# main).  Defaults apply only if the config omits the optional keys.
DLI_HOST        = None
VERIFY_TIMEOUT  = 10.0   # seconds to wait for physical_state to match
VERIFY_INTERVAL = 0.5    # seconds between physical_state polls


# ---------------------------------------------------------------------------
# curl / DLI transport
# ---------------------------------------------------------------------------

def _curl(extra_args):
   """
   Run curl with digest auth and .netrc credentials.

   Returns (return_code, stdout_text).  stderr is surfaced to the console so
   auth/network problems are visible.  A non-zero return_code means curl (or
   the HTTP request) failed.
   """
   result = subprocess.run(
      ['curl', '--silent', '--show-error', '--digest', '--netrc'] + extra_args,
      capture_output=True, text=True)
   if result.stderr:
      sys.stderr.write(result.stderr)
   return result.returncode, result.stdout


def _outlet_url(outlet, leaf):
   """Build a DLI REST URL for a relative (0-based) outlet and a leaf resource."""
   return 'http://{}/restapi/relay/outlets/{}/{}/'.format(DLI_HOST, outlet, leaf)


def set_outlet(outlet, turn_on):
   """Request an outlet's persistent state.  Returns True if curl succeeded."""
   value = 'true' if turn_on else 'false'
   rc, _ = _curl([
      '-X', 'PUT', '-H', 'X-CSRF: x',
      '--data', 'value=' + value,
      _outlet_url(outlet, 'state')])
   return rc == 0


def read_physical_state(outlet):
   """
   Read an outlet's *physical* state.

   Returns True (on), False (off), or None if the value could not be read or
   parsed.  physical_state is the actual relay state, which is what we want to
   confirm the command really took effect.
   """
   rc, out = _curl(['-H', 'Accept: application/json', _outlet_url(outlet, 'physical_state')])
   if rc != 0:
      return None
   text = out.strip().lower()
   if 'true' in text:
      return True
   if 'false' in text:
      return False
   return None


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

class DliOutlet(object):
   """A DLI web-power-switch relay that we set and then verify by polling."""

   def __init__(self, outlet, name):
      self.outlet = outlet   # relative (0-based) outlet number
      self.name   = name

   def apply(self, turn_on):
      """Set the outlet, then poll physical_state until it matches or times out."""
      want = 'on' if turn_on else 'off'

      if not set_outlet(self.outlet, turn_on):
         print('  [FAIL] {} (outlet {}): command not accepted'.format(
            self.name, self.outlet))
         return False

      deadline = time.time() + VERIFY_TIMEOUT
      state = None
      while time.time() < deadline:
         state = read_physical_state(self.outlet)
         if state == turn_on:
            print('  [ OK ] {} (outlet {}) -> {}'.format(
               self.name, self.outlet, want))
            return True
         time.sleep(VERIFY_INTERVAL)

      observed = {True: 'on', False: 'off', None: 'unknown'}.get(state, 'unknown')
      print('  [FAIL] {} (outlet {}): wanted {}, physical_state is {} '
            'after {:.0f}s'.format(self.name, self.outlet, want, observed,
                                   VERIFY_TIMEOUT))
      return False


class Command(object):
   """
   An external command that owns its own verification (ShackMaster, K4).

   We run the on/off command and treat a zero exit code as success; the
   command's own stdout is passed through so its status is visible.

   retry_timeout > 0 makes the step tolerate a device that is not ready yet:
   the command is re-run every retry_interval seconds until it exits 0 or the
   window elapses.  This matters for a device powered by an upstream outlet in
   this same sequence (e.g. the ShackMaster) -- confirming the outlet's relay
   is closed does NOT mean the downstream device has finished booting and
   enumerating on USB, so the first command can fail with a write timeout.
   With the default retry_timeout of 0 the command runs exactly once.
   """

   def __init__(self, on_cmd, off_cmd, name, retry_timeout=0.0, retry_interval=3.0):
      self.on_cmd         = on_cmd
      self.off_cmd        = off_cmd
      self.name           = name
      self.retry_timeout  = retry_timeout
      self.retry_interval = retry_interval

   def apply(self, turn_on):
      cmd = self.on_cmd if turn_on else self.off_cmd
      verb = 'on' if turn_on else 'off'
      # A string command is run through the shell (PATH/PATHEXT resolution for
      # commands like k4up/k4down); a list is run directly (e.g. the ShackMaster CLI).
      shell = isinstance(cmd, str)

      deadline = time.time() + self.retry_timeout
      while True:
         result = subprocess.run(cmd, shell=shell)
         if result.returncode == 0:
            print('  [ OK ] {} ({} -> exit 0)'.format(self.name, verb))
            return True
         if time.time() >= deadline:
            print('  [FAIL] {} ({} -> exit {})'.format(
               self.name, verb, result.returncode))
            return False
         print('  ...... {} not ready (exit {}); retrying in {:.0f}s'.format(
            self.name, result.returncode, self.retry_interval))
         time.sleep(self.retry_interval)


# ---------------------------------------------------------------------------
# Config loading -> sequence of steps
# ---------------------------------------------------------------------------

def _fail_config(message):
   """Print a config error and exit; there is nothing safe to do without config."""
   sys.stderr.write('Config error: {}\n'.format(message))
   sys.exit(2)


def load_config(path=CONFIG_PATH):
   """Load and parse the JSON config file, or exit with a helpful message."""
   if not os.path.exists(path):
      _fail_config(
         '{} not found.\n'
         '   Copy {} to {} and edit it for your station.'.format(
            path, os.path.basename(SAMPLE_PATH), os.path.basename(path)))
   try:
      with open(path, 'r', encoding='utf-8') as fh:
         return json.load(fh)
   except ValueError as exc:
      _fail_config('invalid JSON in {}: {}'.format(path, exc))


def _subst(token):
   """
   Expand placeholders in a command token so configs stay portable:
      {python} -> the interpreter running this script
      {here}   -> the directory containing this script
   """
   return token.replace('{python}', _PYTHON).replace('{here}', _HERE)


def _build_argv(value, where):
   """A command may be a string (run via shell) or a list of tokens (run directly)."""
   if isinstance(value, list):
      return [_subst(str(t)) for t in value]
   if isinstance(value, str):
      return _subst(value)
   _fail_config('{}: command must be a string or a list of strings'.format(where))


def build_sequence(config):
   """Turn the config's "sequence" list into DliOutlet / Command step objects."""
   raw = config.get('sequence')
   if not isinstance(raw, list) or not raw:
      _fail_config('"sequence" must be a non-empty list')

   steps = []
   for i, entry in enumerate(raw):
      where = 'sequence[{}]'.format(i)
      kind = entry.get('type')
      try:
         if kind == 'dli':
            steps.append(DliOutlet(entry['outlet'], entry['name']))
         elif kind == 'command':
            steps.append(Command(
               _build_argv(entry['on'], where + '.on'),
               _build_argv(entry['off'], where + '.off'),
               entry['name'],
               retry_timeout=entry.get('retry_timeout', 0.0),
               retry_interval=entry.get('retry_interval', 3.0)))
         else:
            _fail_config('{}: unknown type {!r} (expected "dli" or "command")'.format(
               where, kind))
      except KeyError as exc:
         _fail_config('{}: missing required key {}'.format(where, exc))
   return steps


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def run_sequence(sequence, turn_on):
   """Walk the sequence (reversed for power-down); return the failure count."""
   steps = sequence if turn_on else list(reversed(sequence))
   print('Powering {} the shack...\n'.format('UP' if turn_on else 'DOWN'))

   failures = 0
   for step in steps:
      if not step.apply(turn_on):
         failures += 1

   print()
   if failures:
      print('{} step(s) FAILED verification.'.format(failures))
   else:
      print('All steps verified.')
   return failures


def show_status(sequence):
   """Print the physical_state of every DLI outlet in the sequence."""
   print('DLI outlets on {}:'.format(DLI_HOST))
   for step in sequence:
      if isinstance(step, DliOutlet):
         state = read_physical_state(step.outlet)
         label = {True: 'ON', False: 'OFF'}.get(state, 'UNKNOWN')
         print('  outlet {}: {:<8} {}'.format(step.outlet, label, step.name))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
   parser = argparse.ArgumentParser(
      description='Config-driven ham shack power sequencer (DLI outlets '
                  'verified via physical_state).')
   # 'on'/'off' are accepted as aliases for 'up'/'down' so the verbs match
   # shackmaster.py and either instinct works.
   parser.add_argument('action', choices=['up', 'on', 'down', 'off', 'status'],
                       help='up/on = power on, down/off = power off, '
                            'status = show outlet states')
   args = parser.parse_args()

   config = load_config()

   global DLI_HOST, VERIFY_TIMEOUT, VERIFY_INTERVAL
   DLI_HOST = config.get('dli_host')
   if not DLI_HOST:
      _fail_config('"dli_host" is required')
   VERIFY_TIMEOUT  = config.get('verify_timeout', VERIFY_TIMEOUT)
   VERIFY_INTERVAL = config.get('verify_interval', VERIFY_INTERVAL)

   sequence = build_sequence(config)

   if args.action == 'status':
      show_status(sequence)
      return

   failures = run_sequence(sequence, turn_on=(args.action in ('up', 'on')))
   sys.exit(1 if failures else 0)


if __name__ == '__main__':
   main()
