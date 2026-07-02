# hamshack — ham shack power sequencer

`hamshack.py` powers the shack up and down in a defined order, **verifying each
step actually took effect** rather than firing commands blindly. It drives:

- **Digital Loggers (DLI) web-power-switch outlets** — set via the REST API and
  confirmed by polling the outlet's `physical_state`.
- **External device commands** — arbitrary programs (e.g. the ShackMaster power
  supply CLI, and the K4/KPA1500 scripts described below) that own their own
  verification; `hamshack` runs them and checks the exit code.

Power-up walks the configured sequence top-to-bottom. Power-**down** walks it in
**reverse** (LIFO: the last thing switched on is the first switched off).

## Requirements

- **Python 3**
- **`curl`** on the `PATH` (used for the DLI REST calls, with digest auth)
- A **`.netrc`** entry supplying the DLI credentials (see *Credentials* below)
- Any external commands referenced in the config (e.g. `shackmaster.py`,
  `k4up`, `k4down`) must be runnable

## Usage

```
python hamshack.py up        # power the shack up (alias: on)
python hamshack.py down      # power the shack down, reverse order (alias: off)
python hamshack.py status    # show physical_state of every managed DLI outlet
```

`up`/`on` and `down`/`off` are interchangeable.

Exit code is `0` when every step verified, and non-zero if any step failed —
so it is safe to use in scripts.

### Running from anywhere (Windows PATH wrapper)

The config and helper scripts are located **relative to `hamshack.py`**, not the
current directory, so the tool works from any working directory. A one-line
wrapper on the `PATH` lets you invoke it by name — e.g. `C:\utils\hamshack.cmd`:

```bat
@echo off
python c:\projects\utilities\hamshack.py %*
```

Then simply: `hamshack up`, `hamshack down`, `hamshack status`.

## Configuration

Configuration lives in **`hamshack.json`**, in the same directory as the script.
It is **station-specific and git-ignored**. A committed **`hamshack.sample.json`**
documents the format.

```
copy hamshack.sample.json hamshack.json   # Windows
cp   hamshack.sample.json hamshack.json    # Linux/macOS
```

Then edit `hamshack.json` for your station. If it is missing, `hamshack.py`
exits with a message telling you to create it.

### Format

| Key | Meaning |
|---|---|
| `dli_host` | Hostname or IP of the DLI web power switch. **Required.** |
| `verify_timeout` | Max seconds to wait for an outlet's `physical_state` to match the request (default `10`). |
| `verify_interval` | Seconds between `physical_state` polls (default `0.5`). |
| `sequence` | Ordered list of steps (see below). **Required.** |

Each entry in `sequence` is one of two step types:

**DLI outlet:**
```json
{ "type": "dli", "outlet": 2, "name": "Green Heron" }
```
- `outlet` is the **relative, 0-based** outlet number used by the DLI REST API
  — i.e. **physical outlet number minus 1** (physical outlet 3 → `"outlet": 2`).

**Command:**
```json
{
  "type": "command",
  "name": "ShackMaster output",
  "on":  ["{python}", "{here}/shackmaster.py", "on"],
  "off": ["{python}", "{here}/shackmaster.py", "off"],
  "retry_timeout": 30.0,
  "retry_interval": 3.0
}
```
- `on` / `off` may be a **string** (run through the shell, so `PATH` lookup
  works — e.g. `"k4up"`) or a **list of tokens** (run directly, no shell).
- Placeholders expand for portability: `{python}` → the running interpreter,
  `{here}` → the directory containing `hamshack.py`.
- `retry_timeout` (optional, default `0`) re-runs the command every
  `retry_interval` seconds until it succeeds or the window elapses. Useful when
  a device needs time to boot after its upstream outlet powers on (confirming
  the relay closed does not mean the downstream USB device has finished
  enumerating). With `retry_timeout` of `0` the command runs exactly once.

Keys beginning with `_` (such as `_comment`) are ignored.

## Credentials (DLI)

`hamshack.py` calls curl with `--digest --netrc`, so **credentials are never
stored in the config or the code** — they come from your `.netrc`:

```
machine 192.168.73.195 login admin password YOURPASSWORD
```

Lock the file down to your account (Windows):
```
icacls "%USERPROFILE%\.netrc" /inheritance:r /grant:r "%USERNAME%:F"
```

> **curl-on-Windows gotcha:** a global `user = "..."` line in an auto-loaded
> `.curlrc` (`%APPDATA%\.curlrc`) **overrides `--netrc`** for every curl call,
> which makes per-host `.netrc` entries silently ignored. Keep credentials out
> of `.curlrc` and put them per-host in `.netrc`.

## Related scripts in this repository

`hamshack.py` orchestrates other tools; it does not reimplement them. The K4
radio steps invoke **separate, standalone scripts that also live in this
repository** and can be run on their own:

- **`k4up.py`** — brings up the Elecraft **KPA1500** amplifier via Wake-on-LAN
  (if needed), waits for the **K4** to respond, then sends the power-on
  commands. Cross-platform (Linux/macOS/Windows).
- **`k4down.py`** — shuts down the **K4**, **K4/0**, and **KPA1500** over the
  network. Cross-platform.

In the sample/station config these are referenced as the commands `k4up` and
`k4down` (thin `PATH` wrappers that call `k4up.py` / `k4down.py`). You can point
the config directly at the `.py` files instead, e.g.:
```json
{ "type": "command", "name": "Elecraft K4",
  "on":  ["{python}", "{here}/k4up.py"],
  "off": ["{python}", "{here}/k4down.py"] }
```

> **Note:** The K4/0 does **not** support Wake-on-LAN, so it cannot be powered
> up by a magic packet; it is handled by the `k4up.py` / `k4down.py` logic.

The ShackMaster power-supply step invokes **`shackmaster.py`** (also in this
repo), a CLI for the RigExpert ShackMaster Power 600.

## How verification works

- **DLI outlets:** after the `PUT` that requests on/off, the DLI may take a few
  seconds to actuate the relay (it honors on-sequence and cycle delays), so a
  single immediate read can report the old value. `hamshack` polls
  `physical_state` until it matches the request or `verify_timeout` elapses,
  then reports `[ OK ]` or `[FAIL]`.
- **Commands:** a zero exit code is success. With a retry window, the command is
  re-run until it succeeds or the window elapses.
- The final line summarizes the run, and the process exit code reflects whether
  every step verified.
