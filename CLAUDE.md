# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

This is a collection of amateur radio (ham radio) utilities for contest log processing, DX cluster validation, and radio equipment discovery. All scripts are standalone command-line tools with no build system or test framework.

## Languages and Runtime

- **Perl**: Primary language (most utilities). Requires Perl 5 with modules:
  - `Net::DNS::Nslookup` - DNS resolution
  - `Net::Telnet` - Telnet connectivity testing
  - `POSIX` - Date/time formatting

- **Python**: Used for findk4.py. Compatible with Python 2.7+ and Python 3.x. Uses conditional imports for version-specific features (type hints available in Python 3.5+, ipaddress module in Python 3.3+).

## Script Purposes

### Contest Log Processing
- `CountOps.pl` - Parses ARRL Field Day ADIF logs to count QSOs per operator and generate OPERATORS: line for Cabrillo
- `CheckCabExchanges.pl` - Validates ARRL contest Cabrillo logs for correct section/state exchanges
- `CheckCABExchanges_WFD.pl` - Extended version that also validates contest class format (e.g., "1D", "3F")
- `CheckClubOps.pl` - Detects when club operators appear as contacts in their own log (club-to-club QSOs)

### Network/DNS Utilities
- `checkHost.pl` - Validates DX cluster hosts from input data files. Tests DNS resolution and telnet connectivity on specified ports. Outputs valid clusters to dated files (DXCLUSTERS.DAT.YYYY-MM-DD and trcluster.dat.YYYY-MM-DD)
- `testDNS.pl` - Simple DNS resolution test for multiple domains

### Radio Equipment Discovery
- `findk4.py` - Broadcasts UDP packets on all network interfaces to discover Elecraft K4 transceivers on the local network. Displays serial numbers and IP addresses of discovered radios. Supports `-v/--verbose` for debug output and `-t/--timeout` to customize discovery timeout.

## Running Scripts

All scripts are designed to process input from STDIN or command-line files:

```bash
# Perl scripts with file input
./CountOps.pl < field-day-log.adi
./CheckCabExchanges.pl < contest-log.cbr
cat DXCLUSTERS.DAT | ./checkHost.pl

# Python script (no input required, compatible with Python 2.7+ and 3.x)
python findk4.py               # Basic discovery (uses default python)
python3 findk4.py -v           # Verbose debug output
python findk4.py -t 5          # Use 5-second timeout
```

## Data Formats

### Contest Logs (Cabrillo format)
Lines start with `QSO:` followed by space-delimited fields:
```
QSO: 14096 RY 2020-01-04 1800 W4TA 599 FL K1SM 599 MA 0
```
Fields: frequency, mode, date, time, my_call, my_rst, my_section, their_call, their_rst, their_section, radio_number

### ADIF Format (Field Day logs)
Single-line records with angle-bracketed fields:
```
<CALL:6>KC4SXO <BAND:2>6m <QSO_DATE:8>20220625 <TIME_ON:6>180611 <OPERATOR:5>W2SUB <EOR>
```

### DX Cluster Data
CSV-like format with quoted fields:
```
"ClusterName","hostname.domain.com","7300","DX Spider"
```
Fields: name, hostname, port, cluster_type

### K4 Discovery Protocol
- Broadcast: Send UDP packet "findk4" to port 9100 on 255.255.255.255
- Response: Colon-delimited byte string `k4:ID:IP:SerialNumber` (e.g., `k4:0:192.168.73.108:278`)

## ARRL Section/State Abbreviations

Scripts validate against ARRL contest section codes. Valid sections include:
- New England: CT, EMA, ME, NH, RI, VT, WMA
- Atlantic: DE, EPA, MDC, WPA, ENY, NLI, NNJ, NNY, SNJ, WNY
- Southeastern: AL, GA, KY, NC, NFL, SC, SFL, WCF, TN, VA, PR, VI
- And many more (see regex patterns in CheckCabExchanges.pl:14-18)

DX (non-US/Canada) stations send numeric exchanges instead of sections.

## Code Patterns

### Perl Script Structure
- Use regex to identify record types (`/^QSO/`, `/^OPERATORS:/`, etc.)
- Split space-delimited fields with `/\s{1,}/`
- Hash tables for tracking data (operators, call signs, exchanges)
- Validate exchanges with comprehensive regex patterns

### Network Testing Pattern (checkHost.pl)
1. Validate DNS resolution works by testing known-good hosts (ve7cc.net, dx.k3lr.com)
2. For each cluster: check if hostname is IP address (skip DNS) or resolve DNS
3. Test telnet connectivity with 5-second timeout
4. Write successful connections to output files
5. Report statistics (% bad hostnames, % no connect, % good)

### Python Network Discovery Pattern (findk4.py)
Python 2.7+/3.x compatible implementation with logging and comprehensive error handling:
1. **Python 2/3 Compatibility**:
   - `from __future__ import print_function` for consistent print behavior
   - Type hints use PEP 484 comment syntax (`# type: (str) -> bool`) compatible with Python 2
   - Conditional imports: `typing` module only imported in Python 3.5+, `ipaddress` in Python 3.3+
   - `.format()` strings instead of f-strings for Python 2 compatibility
   - Exception handling catches both `OSError` and `socket.error` for cross-version compatibility
2. Use `argparse` for command-line arguments (`--verbose`, `--timeout`)
3. Use `logging` module for debug/info output (not print statements)
4. Extract functionality into well-documented functions with type hints (comment-style for Python 2)
5. Manual socket cleanup using try/finally blocks (Python 2 doesn't support context managers for sockets)
6. Comprehensive error handling for network operations (bind, send, receive)
7. Validate response data (IP address format using `ipaddress` module or fallback to `socket.inet_aton`)
8. Deduplicate radios discovered on multiple interfaces using sets
9. Return proper exit codes (0 if radios found, 1 if none found)
