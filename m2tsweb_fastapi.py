import os, asyncio, subprocess, json, shutil, psutil, uuid, time, re, sys, termios, tempfile, fcntl, socket, struct
import logging
import auth as _auth
from datetime import datetime
from fastapi import FastAPI, Request, Response, Form
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse, JSONResponse
from contextlib import asynccontextmanager
import uvicorn
from templates import render_dashboard, render_mobile, render_multiview

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("m2tsweb_fastapi")

# ---------------------------------------------------------------------------
# In-memory log ring buffer + SSE broadcaster
# ---------------------------------------------------------------------------
import collections, threading

_LOG_BUFFER_SIZE  = 500          # max entries kept in memory
_log_buffer: collections.deque  = collections.deque(maxlen=_LOG_BUFFER_SIZE)
_log_subscribers: list           = []   # list of asyncio.Queue
_log_lock         = threading.Lock()


class _RingHandler(logging.Handler):
    """Capture every log record into the ring buffer and fan out to SSE queues."""

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "ts":    datetime.fromtimestamp(record.created).strftime("%Y-%m-%dT%H:%M:%S"),
            "ms":    f"{record.msecs:03.0f}",
            "level": record.levelname,
            "name":  record.name,
            "msg":   record.getMessage(),
        }
        with _log_lock:
            _log_buffer.append(entry)
            queues = list(_log_subscribers)

        # Fan out to all live SSE connections (non-blocking put_nowait)
        for q in queues:
            try:
                q.put_nowait(entry)
            except Exception:
                pass


_ring_handler = _RingHandler()
_ring_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_ring_handler)

# ---------------------------------------------------------------------------
# Input sanitization helpers
# ---------------------------------------------------------------------------

# Allowed base directories for recordings. If non-empty, all output paths
# must resolve inside one of these trees. Set via ALLOWED_REC_DIRS env var
# as a colon-separated list, e.g. "/recordings:/mnt/nas".
_ALLOWED_REC_DIRS: list[str] = [
    d for d in os.environ.get("ALLOWED_REC_DIRS", "").split(":")
    if d.strip()
]

def _safe_output_path(path: str) -> str:
    """Validate and normalise a user-supplied recording output path.

    Rules:
    1. Must be an absolute path.
    2. No null bytes.
    3. After resolving '..' components, must not escape the allowed
       directories (when ALLOWED_REC_DIRS is configured).

    Raises ValueError with a human-readable message on failure.
    """
    if not path:
        raise ValueError("Output path must not be empty.")
    if "\x00" in path:
        raise ValueError("Output path contains null bytes.")
    if not os.path.isabs(path):
        raise ValueError("Output path must be absolute (must start with '/').")

    # Normalise without hitting the filesystem (os.path.realpath would
    # follow symlinks, which we don't want at validation time).
    normalised = os.path.normpath(path)

    if _ALLOWED_REC_DIRS:
        if not any(
            normalised.startswith(os.path.normpath(d) + os.sep) or
            normalised == os.path.normpath(d)
            for d in _ALLOWED_REC_DIRS
        ):
            raise ValueError(
                f"Output path '{normalised}' is outside the allowed "
                f"recording directories: {_ALLOWED_REC_DIRS}"
            )
    return normalised


_VAAPI_DEVICE_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")
_FFMPEG_FILTER_BANNED = re.compile(r"[;&|`$<>]")  # shell-injection characters

def _safe_vaapi_device(device: str) -> str:
    """Allow only valid DRM render node paths, e.g. renderD128."""
    device = device.strip()
    if not device:
        return device
    if not _VAAPI_DEVICE_RE.match(device):
        raise ValueError(
            f"Invalid VAAPI device '{device}'. "
            "Expected a path like 'renderD128' or '/dev/dri/renderD128'."
        )
    return device


def _safe_video_filter(vf: str) -> str:
    """Reject video filter strings that contain shell-injection characters."""
    vf = vf.strip()
    if _FFMPEG_FILTER_BANNED.search(vf):
        raise ValueError(
            f"Video filter contains disallowed characters: '{vf}'"
        )
    return vf


# ---------------------------------------------------------------------------
# Fan PWM helpers
# ---------------------------------------------------------------------------

def _pwm_write(pwm_path: str, value: int) -> bool:
    """Write a PWM value (0-255) to a sysfs hwmon PWM node.

    Returns True on success.  Requires either root or a sudoers rule that
    allows passwordless write to the path (set up by setup.sh).
    """
    value = max(0, min(255, int(value)))
    try:
        with open(pwm_path, "w") as f:
            f.write(str(value))
        return True
    except PermissionError:
        # Fall back to sudo tee — works if the sudoers rule is in place
        try:
            result = subprocess.run(
                ["sudo", "tee", pwm_path],
                input=str(value).encode(),
                capture_output=True,
                timeout=3,
            )
            return result.returncode == 0
        except Exception as exc:
            log.warning("fan_pwm: sudo tee fallback failed for %s: %s", pwm_path, exc)
            return False
    except Exception as exc:
        log.warning("fan_pwm: write to %s failed: %s", pwm_path, exc)
        return False


def _pwm_read(pwm_path: str) -> int | None:
    """Read the current PWM value from a sysfs hwmon node."""
    try:
        with open(pwm_path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _fan_rpm_path(pwm_path: str) -> str | None:
    """Derive the fan RPM input path from a PWM path.

    e.g. /sys/class/hwmon/hwmon2/pwm6 → /sys/class/hwmon/hwmon2/fan6_input
    """
    import re
    m = re.search(r"pwm(\d+)$", pwm_path)
    if not m:
        return None
    fan_num = m.group(1)
    return os.path.join(os.path.dirname(pwm_path), f"fan{fan_num}_input")


def _fan_rpm_read(pwm_path: str) -> int | None:
    """Read the current fan RPM for the given PWM path."""
    rpm_path = _fan_rpm_path(pwm_path)
    if not rpm_path:
        return None
    try:
        with open(rpm_path) as f:
            return int(f.read().strip())
    except Exception:
        return None


# Track per-input fan state: input_id -> {"active": bool, "last_stop": float}
_fan_state: dict = {}

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
active_inputs  = {}   # live hardware streams        key: "B-I" e.g. "0-0"

def _make_pipe_nonblocking(pipe) -> None:
    """Set a subprocess pipe to O_NONBLOCK at the OS level.

    This is the actual fix for the thread-leak bug: a plain
    process.stdout.read(N) blocks until N bytes are available OR EOF —
    if the process is alive but has simply stopped producing data (lost
    signal, frozen encoder, stalled source), that read blocks forever.
    asyncio.wait_for() timing out only abandons our *view* of that read;
    the underlying OS thread stays parked on the syscall permanently,
    leaking one thread from the pool every single timeout cycle.

    With O_NONBLOCK set, read() instead raises BlockingIOError
    immediately if no data is available, which is safe to retry from a
    fresh asyncio coroutine with no thread ever left blocked.
    """
    try:
        fd = pipe.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    except Exception as exc:
        log.warning("_make_pipe_nonblocking failed: %s", exc)


def _nonblocking_read(pipe, size: int) -> bytes:
    """Read up to `size` bytes from a non-blocking pipe.

    Returns b'' only on genuine EOF (process closed the pipe / exited).
    Returns None if no data is currently available (caller should treat
    this as "try again shortly", not as EOF) — this is the key
    difference from a plain read(), which would have blocked instead.
    """
    try:
        data = os.read(pipe.fileno(), size)
        return data   # b'' here means real EOF
    except BlockingIOError:
        return None   # no data right now — not EOF, just nothing yet
    except (OSError, ValueError):
        return b''    # pipe closed/invalid — treat as EOF
active_records = {}   # active recordings             key: record_id
scheduled_jobs = {}   # pending scheduled jobs        key: job_id
active_hls     = {}   # HLS segment writers           key: "B-I"
SHOULD_BE_LIVE = {}   # inputs that should stay alive key: "B-I" → {restart_count, last_restart, faulted}
inputs_lock    = asyncio.Lock()
records_lock   = asyncio.Lock()
schedule_lock  = asyncio.Lock()
hls_lock       = asyncio.Lock()
input_config_lock = asyncio.Lock()

# Lock ordering: the only place two of these locks are ever held at once
# (nested, not just sequential) is schedule_runner(), which acquires
# records_lock while already holding schedule_lock. That is the sole
# nesting relationship in the codebase as of this writing, and it is
# always used in that direction (schedule_lock -> records_lock), never
# reversed. If a future change needs to hold schedule_lock and
# records_lock together anywhere else, acquire them in that same order
# to avoid a deadlock. Everywhere else, these locks (plus
# _fan_config_lock and user_prefs_lock, defined further below) are each
# acquired independently and released before the next one is taken —
# there is no other nesting to preserve.
CPU_COUNT      = psutil.cpu_count()

WATCHDOG_MAX_RESTARTS = 5
WATCHDOG_WINDOW       = 60
WATCHDOG_BACKOFF      = [2, 4, 8, 16, 30]

HLS_DIR            = "/tmp/hls"
HLS_SEGMENTS       = 6
HLS_DURATION       = 2
HLS_CLIENT_TIMEOUT = 12   # seconds without a playlist poll → client considered gone

FORMAT_EXT = {
    "ts":  ("mpegts",   "ts"),
    "mp4": ("mp4",      "mp4"),
    "mkv": ("matroska", "mkv"),
    "mov": ("mov",      "mov"),
}

# ADB keycodes for Android TV
ADB_KEYCODES = {
    "up":         19,
    "down":       20,
    "left":       21,
    "right":      22,
    "enter":      66,
    "back":        4,
    "home":        3,
    "play_pause": 85,
    "vol_up":     24,
    "vol_down":   25,
}

# Roku ECP key names (POST to http://<ip>:8060/keypress/<key>)
ROKU_KEYS = {
    "up":    "Up",
    "down":  "Down",
    "left":  "Left",
    "right": "Right",
    "enter": "Select",
    "back":  "Back",
    "home":  "Home",
}

# ---------------------------------------------------------------------------
# Encoder configuration
# ---------------------------------------------------------------------------

# Human-readable labels shown in the UI
ENCODER_LABELS: dict[str, str] = {
    # H.264
    "h264_qsv":   "Intel QSV H.264",
    "h264_nvenc": "NVIDIA H.264",
    "h264_amf":   "AMD H.264",
    "h264_vaapi": "VAAPI H.264",
    "libx264":    "Software H.264 (CPU)",
    # H.265 / HEVC
    "hevc_qsv":   "Intel QSV H.265",
    "hevc_nvenc": "NVIDIA H.265",
    "hevc_amf":   "AMD H.265",
    "hevc_vaapi": "VAAPI H.265",
    "libx265":    "Software H.265 (CPU)",
    # AV1
    "av1_vaapi":  "VAAPI AV1 (Arc)",
    # VP9
    "vp9_vaapi":  "VAAPI VP9 (Arc)",
}

# Encoder presets per codec family.
# These are passed as -preset to magewell2ts (-p flag) and to ffmpeg directly
# Only shown in UI when the selected codec supports presets.
ENCODER_PRESETS: dict[str, list[str]] = {
    "h264_qsv":   ["veryfast", "faster", "fast", "medium", "slow", "veryslow"],
    "hevc_qsv":   ["veryfast", "faster", "fast", "medium", "slow", "veryslow"],
    "h264_nvenc": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
    "hevc_nvenc": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
    "h264_amf":   ["speed", "balanced", "quality"],
    "hevc_amf":   ["speed", "balanced", "quality"],
    "h264_vaapi": [],   # no preset flag for vaapi
    "hevc_vaapi": [],
    "av1_vaapi":  [],
    "vp9_vaapi":  [],
    "libx264":    ["ultrafast", "superfast", "veryfast", "faster", "fast",
                   "medium", "slow", "slower", "veryslow"],
    "libx265":    ["ultrafast", "superfast", "veryfast", "faster", "fast",
                   "medium", "slow", "slower", "veryslow"],
}


def _detect_available_encoders() -> list[str]:
    """Return only video encoders ffmpeg reports as available on this machine."""
    candidates = [
        "h264_qsv", "hevc_qsv",
        "h264_nvenc", "hevc_nvenc",
        "h264_amf", "hevc_amf",
        "h264_vaapi", "hevc_vaapi",
        "av1_vaapi", "vp9_vaapi",
        "libx264", "libx265",
    ]
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        available = [e for e in candidates if e in result.stdout]
        if available:
            return available
    except Exception as e:
        log.warning("Encoder probe failed: %s", e)
    return ["libx264"]   # safe fallback


AVAILABLE_ENCODERS: list[str] = _detect_available_encoders()
log.info("Available encoders: %s", AVAILABLE_ENCODERS)

# ---------------------------------------------------------------------------
# Audio codec configuration (legacy — magewell2ts handles audio itself;
# retained for SSE/template compatibility)
# ---------------------------------------------------------------------------

# Candidate audio codecs in priority order.
# libfdk_aac requires ffmpeg compiled with --enable-libfdk-aac; we probe
# for it at startup and only offer it if present.
AUDIO_CODEC_CANDIDATES = [
    ("aac",        "AAC",                   2),   # (codec, label, max_channels)
    ("libfdk_aac", "AAC (libfdk — HQ)",     8),
    ("ac3",        "AC-3 / Dolby Digital",  6),
    ("eac3",       "E-AC-3 / DD Plus",      8),
    ("dca",        "DTS",                   6),
    ("pcm_s16le",  "PCM 16-bit (lossless)", 16),
]

# Map codec → max channels it can encode
AUDIO_CODEC_MAX_CH: dict[str, int] = {c: mx for c, _, mx in AUDIO_CODEC_CANDIDATES}


def _detect_available_audio_codecs() -> list[dict]:
    """Return audio codecs that ffmpeg reports as available on this machine."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        out = result.stdout
        available = []
        for codec, label, max_ch in AUDIO_CODEC_CANDIDATES:
            if codec in out:
                available.append({"value": codec, "label": label, "max_ch": max_ch})
        if available:
            return available
    except Exception as e:
        log.warning("Audio codec probe failed: %s", e)
    return [{"value": "aac", "label": "AAC", "max_ch": 2}]   # safe fallback


AVAILABLE_AUDIO_CODECS: list[dict] = _detect_available_audio_codecs()
log.info("Available audio codecs: %s", [c['value'] for c in AVAILABLE_AUDIO_CODECS])

# Channel layout options.  "capture_ch" is the value passed to -channels
# "out_ch" is the -ac value for the encoder. "layout" is the ffmpeg
# channel layout name used in filters. (Legacy — retained for compatibility.)
CHANNEL_LAYOUTS = [
    {"value": "stereo", "label": "Stereo (2ch)",   "capture_ch": 2,  "out_ch": 2,  "layout": "stereo"},
    {"value": "5.1",    "label": "5.1 Surround",   "capture_ch": 8,  "out_ch": 6,  "layout": "5.1"},
    {"value": "7.1",    "label": "7.1 Surround",   "capture_ch": 8,  "out_ch": 8,  "layout": "7.1"},
    {"value": "8ch",    "label": "8 ch (raw)",      "capture_ch": 8,  "out_ch": 8,  "layout": "octagonal"},
    {"value": "16ch",   "label": "16 ch (raw)",     "capture_ch": 16, "out_ch": 16, "layout": "hexadecagonal"},
]
CHANNEL_LAYOUT_MAP: dict[str, dict] = {cl["value"]: cl for cl in CHANNEL_LAYOUTS}

# ---------------------------------------------------------------------------
# Capture command builder
# ---------------------------------------------------------------------------

def _build_capture_cmd(input_id: str, cfg: dict) -> list:
    """Return the argv list to launch the capture process for input_id.

    The process must write raw MPEG-TS to stdout. Delegates to magewell2ts,
    which handles encoding internally.
    """
    driver  = cfg.get("driver", "magewell")
    encoder = cfg.get("encoder", AVAILABLE_ENCODERS[0])
    q       = int(cfg.get("q", 25))

    if driver == "magewell":
        # Build magewell2ts command from per-input config
        cmd = [
            "magewell2ts",
            "-m",
            "-b", str(cfg.get("board", 0)),
            "-i", str(cfg.get("channel", 1)),
            "-c", encoder,
            "-q", str(q),
        ]

        # Optional: encoder preset (-p). Only add if set and codec supports it.
        preset = cfg.get("preset", "")
        if preset and ENCODER_PRESETS.get(encoder):
            cmd += ["-p", preset]

        # Optional: lookahead (-a). 0 = disabled, default 35.
        lookahead = int(cfg.get("lookahead", 35))
        cmd += ["-a", str(lookahead)]

        # Optional: GOP size in seconds (-g). 0 disables fixed GOP.
        gop_secs = cfg.get("gop_secs", 1.5)
        cmd += ["-g", str(gop_secs)]

        # Optional: GPU / RAM buffer tuning
        gpu_buffers     = int(cfg.get("gpu_buffers",     16))
        video_buffers   = int(cfg.get("video_buffers",   16))
        extra_hw_frames = int(cfg.get("extra_hw_frames", 32))
        cmd += ["--gpu-buffers",     str(gpu_buffers)]
        cmd += ["--video-buffers",   str(video_buffers)]
        cmd += ["--extra-hw-frames", str(extra_hw_frames)]

        # Optional: 10-bit p010 format
        if cfg.get("p010", False):
            cmd.append("--p010")

        # Optional: video only (no audio)
        if cfg.get("no_audio", False):
            cmd.append("-n")

        # Optional: VAAPI/QSV device override (e.g. renderD129)
        device = cfg.get("vaapi_device", "").strip()
        if device:
            cmd += ["-d", device]

        return cmd

    else:
        raise ValueError(f"Unknown driver '{driver}' for input {input_id}")


# ---------------------------------------------------------------------------
# Hardware discovery
# ---------------------------------------------------------------------------

def _parse_list_output(text: str) -> list:
    """Parse magewell2ts --list output.

    magewell2ts --list assigns global channel indices [1], [2], ... across
    all boards.  However magewell2ts -i takes a per-board 1-based position:
    -i 1 is the first input on the given board, -i 2 the second, etc.

    This function stores:
      "input"   — the global [N] index, used as the key ("B-N")
      "channel" — the per-board -i value (1-based position within the board)
      "serial"  — card serial number (stable across slot changes)

    Example for your hardware:
      Board 1: [1] → channel 1  (only input on DVI card)
      Board 0: [2] → channel 1  (first input on quad)
               [3] → channel 2
               [4] → channel 3
               [5] → channel 4
    """
    # First pass: collect all entries in order
    raw = []
    current_board  = 0
    current_serial = ""
    # Optional temperature prefix: [N] 75.8ºC Video Signal ...
    # or old format:               [N] Video Signal ...
    _TEMP_PREFIX = r"(?:[\d.]+\s*[º°]C\s*)?"
    for raw_line in text.splitlines():
        # Strip log-level prefixes added by magewell2ts
        line = re.sub(r'^(?:info|critical|warning|error):\s*', '', raw_line).strip()
        board_m = re.match(r"Board:\s*(\d+)", line)
        if board_m:
            current_board  = int(board_m.group(1))
            # SerialNo may appear on the same line as Board (new format)
            # or on a separate line (old format) — handle both
            serial_inline = re.search(r"SerialNo:\s*(\S+)", line)
            current_serial = serial_inline.group(1).rstrip(",") if serial_inline else ""
            continue
        serial_m = re.search(r"SerialNo:\s*(\S+)", line)
        if serial_m:
            current_serial = serial_m.group(1).rstrip(",")
            continue
        input_m = re.match(
            r"\s*\[(\d+)\]\s+" + _TEMP_PREFIX + r"Video Signal\s+(\w+)", line
        )
        if input_m:
            raw.append({
                "board":  current_board,
                "input":  int(input_m.group(1)),
                "signal": input_m.group(2),
                "serial": current_serial,
                "desc":   "",
            })
            continue
        nosig_m = re.match(
            r"\s*\[(\d+)\]\s+" + _TEMP_PREFIX + r"(No\s+\w+|Unlocked)",
            line, re.IGNORECASE
        )
        if nosig_m:
            raw.append({
                "board":  current_board,
                "input":  int(nosig_m.group(1)),
                "signal": "NONE",
                "serial": current_serial,
                "desc":   "",
            })
            continue
        if raw and not raw[-1]["desc"]:
            res_m = re.match(r"\s+(\d+x\d+\w[\d.]+)", line)
            if res_m:
                raw[-1]["desc"] = res_m.group(1)

    # Second pass: assign per-board channel numbers (1-based position)
    # Sort each board's inputs by global index to get stable ordering
    board_counters = {}
    results = []
    for entry in sorted(raw, key=lambda e: (e["board"], e["input"])):
        b = entry["board"]
        board_counters[b] = board_counters.get(b, 0) + 1
        results.append({**entry, "channel": board_counters[b]})

    return results

def _run_list() -> list:
    try:
        result = subprocess.run(
            ["magewell2ts", "--list"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout + result.stderr
        return _parse_list_output(output)
    except Exception as e:
        log.warning("magewell2ts --list failed: %s", e)
        return []


def _run_all_hardware() -> list:
    """Return magewell_entries (Decklink support removed)."""
    return _run_list()


def _input_key(board: int, inp: int, channel: int = None) -> str:
    # Use per-board channel number if provided, otherwise fall back to global index
    return f"{board}-{channel if channel is not None else inp}"


def _split_key(key: str):
    """Split a key into (board, index), both ints."""
    prefix, idx = key.split("-", 1)
    return int(prefix), int(idx)


def _label(key: str, channel: int = None) -> str:
    b, i = _split_key(key)
    # Key is now board-channel, so i is already the per-board input number
    return f"Board {b} · Input {i}"


def _sort_ids(ids):
    """Sort input-ID strings: board-input keys, numeric on both parts."""
    def sort_key(k):
        parts = k.split("-", 1)
        return (int(parts[0]), int(parts[1]))
    return sorted(ids, key=sort_key)

# ---------------------------------------------------------------------------
# Per-input configuration
# ---------------------------------------------------------------------------
CONFIG_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "input_config.json")
FAN_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fan_config.json")

_fan_config      = {}
_fan_config_lock = asyncio.Lock()
_fan_active_profile = "default"

def _load_fan_config() -> dict:
    try:
        with open(FAN_CONFIG_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("Failed to read fan_config.json: %s", exc)
        return {}

def _save_fan_config(cfg: dict) -> None:
    config_dir = os.path.dirname(FAN_CONFIG_FILE)
    fd, tmp = tempfile.mkstemp(dir=config_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, FAN_CONFIG_FILE)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise

async def _cc_activate_mode(cc_cfg: dict, mode_uid: str) -> bool:
    """Activate a CoolerControl mode via POST /modes-active/{uid}."""
    if not mode_uid:
        return False
    url   = cc_cfg.get("url", "http://localhost:11987").rstrip("/")
    token = cc_cfg.get("token", "")
    if not token:
        return False
    try:
        import urllib.request as _ureq
        req = _ureq.Request(
            f"{url}/modes-active/{mode_uid}",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
            data=b"",
        )
        loop = asyncio.get_running_loop()
        def _do():
            with _ureq.urlopen(req, timeout=5) as r:
                return r.status
        status = await loop.run_in_executor(None, _do)
        return status == 200
    except Exception as exc:
        log.warning("CoolerControl activate failed: %s", exc)
        return False


def _load_config_file() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: dict, active: list, hidden: set, user_prefs: dict = None):
    """Atomically write config to disk using a temp file + os.replace.

    This guarantees the config file is never left in a partially-written
    state if the process crashes or the disk fills up mid-write.
    """
    try:
        data = {}
        for k, v in cfg.items():
            entry = {
                "q":           v["q"],
                "adb_ip":      v.get("adb_ip", ""),   # legacy — kept for compatibility
                "remote_type": v.get("remote_type", "adb" if v.get("adb_ip") else "none"),
                "remote_ip":   v.get("remote_ip", v.get("adb_ip", "")),
                "driver":      v.get("driver", "magewell"),
                "encoder":     v.get("encoder", AVAILABLE_ENCODERS[0]),
            }
            driver = entry["driver"]
            if driver == "magewell":
                entry.update({
                    "board":           v.get("board", 0),
                    "input":           v.get("input", 0),
                    "channel":         v.get("channel", 1),
                    "serial":          v.get("serial", ""),
                    "preset":          v.get("preset", ""),
                    "lookahead":       v.get("lookahead", 35),
                    "gop_secs":        v.get("gop_secs", 1.5),
                    "gpu_buffers":     v.get("gpu_buffers", 16),
                    "video_buffers":   v.get("video_buffers", 16),
                    "extra_hw_frames": v.get("extra_hw_frames", 32),
                    "p010":            v.get("p010", False),
                    "no_audio":        v.get("no_audio", False),
                    "vaapi_device":    v.get("vaapi_device", ""),
                    "edid_path":       v.get("edid_path", ""),
                    "edid_refresh":    v.get("edid_refresh", False),
                })
            data[k] = entry
        data["__active__"] = list(active)
        data["__hidden__"] = list(hidden)
        if user_prefs is not None:
            data["__user_prefs__"] = user_prefs
        elif "__user_prefs__" in _load_config_file():
            data["__user_prefs__"] = _load_config_file()["__user_prefs__"]

        # Write to a temp file in the same directory, then atomically rename.
        # os.replace() is atomic on POSIX — the config is never half-written.
        config_dir = os.path.dirname(CONFIG_FILE)
        fd, tmp_path = tempfile.mkstemp(dir=config_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, CONFIG_FILE)
        except Exception:
            # Clean up the orphaned temp file before re-raising
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    except Exception as e:
        log.error("Could not save input config: %s", e)


def _build_entry(key: str, board_or_dl, inp: int, channel: int,
                  signal: str, desc: str, saved: dict, saved_adb: dict,
                  driver: str = "magewell",
                  extra: dict = None, serial: str = "") -> dict:
    base = {
        "q":           saved.get(key, {}).get("q", 25) if isinstance(saved.get(key), dict) else saved.get(key, 25),
        "adb_ip":      saved_adb.get(key, ""),   # legacy
        "remote_type": saved.get(key, {}).get("remote_type", "adb" if saved_adb.get(key) else "none") if isinstance(saved.get(key), dict) else ("adb" if saved_adb.get(key) else "none"),
        "remote_ip":   saved.get(key, {}).get("remote_ip", saved_adb.get(key, "")) if isinstance(saved.get(key), dict) else saved_adb.get(key, ""),
        "driver":      driver,
        "encoder":     saved.get(key, {}).get("encoder", AVAILABLE_ENCODERS[0]) if isinstance(saved.get(key), dict) else AVAILABLE_ENCODERS[0],
        "signal":      signal,
        "desc":        desc,
    }
    saved_mw = saved.get(key, {}) if isinstance(saved.get(key), dict) else {}
    base.update({
        "board":           board_or_dl,
        "input":           inp,
        "channel":         channel,
        "serial":          serial or saved_mw.get("serial", ""),
        "preset":          saved_mw.get("preset",          ""),
        "lookahead":       saved_mw.get("lookahead",        35),
        "gop_secs":        saved_mw.get("gop_secs",         1.5),
        "gpu_buffers":     saved_mw.get("gpu_buffers",      16),
        "video_buffers":   saved_mw.get("video_buffers",    16),
        "extra_hw_frames": saved_mw.get("extra_hw_frames",  32),
        "p010":            saved_mw.get("p010",             False),
        "no_audio":        saved_mw.get("no_audio",         False),
        "vaapi_device":    saved_mw.get("vaapi_device",     ""),
        "edid_path":       saved_mw.get("edid_path",        ""),
        "edid_refresh":    saved_mw.get("edid_refresh",     False),
    })
    if extra:
        base.update(extra)
    return base


def _bootstrap():
    saved         = _load_config_file()
    # saved is a flat dict: key → entry dict (or __active__, __hidden__, __user_prefs__)
    saved_entries  = {k: v for k, v in saved.items() if not k.startswith("__") and isinstance(v, dict)}
    saved_q        = {k: int(v.get("q", 25))              for k, v in saved_entries.items()}
    saved_adb      = {k: str(v.get("adb_ip", ""))         for k, v in saved_entries.items()}
    saved_active   = [str(x) for x in saved.get("__active__", [])]
    saved_hidden   = set(str(x) for x in saved.get("__hidden__", []))
    log.info("Bootstrap: saved_active=%s, hidden=%s", saved_active, sorted(saved_hidden))

    mw_entries = _run_list()

    hw_by_key: dict = {}
    for e in mw_entries:
        key = _input_key(e["board"], e["input"], e["channel"])
        hw_by_key[key] = e

    def _hw_or_saved(key):
        """Return best-effort config tuple for a key not in current hw scan."""
        if key in hw_by_key:
            return hw_by_key[key]
        if key in saved_entries:
            return saved_entries[key]
        # Absolute fallback
        try:
            b, i = int(key.split("-")[0]), int(key.split("-")[1])
        except Exception:
            b, i = 0, 0
        # i is the channel number — _input_key encodes "board-channel" in the key,
        # so preserve it here rather than hardcoding 1, which would silently
        # capture the wrong input whenever the hardware scan misses a card.
        return {"driver": "magewell", "board": b, "input": i, "channel": i, "signal": "NONE", "desc": ""}

    def _make_entry(key, hw_data):
        return _build_entry(
            key,
            hw_data.get("board", 0), hw_data.get("input", 0), hw_data.get("channel", 1),
            hw_data.get("signal", "UNKNOWN"), hw_data.get("desc", ""),
            saved_entries, saved_adb,
            driver="magewell",
        )

    cfg    = {}
    active = []

    if not saved_active and not saved_hidden:
        if hw_by_key:
            for key, e in hw_by_key.items():
                cfg[key] = _make_entry(key, e)
                active.append(key)
            active = _sort_ids(active)
            log.info("First run: discovered %d input(s): %s", len(active), active)
        else:
            active = ["0-0"]
            cfg["0-0"] = _build_entry("0-0", 0, 0, 1, "UNKNOWN", "",
                                       saved_entries, saved_adb, driver="magewell")
            log.warning("No hardware found on first run — placeholder created")
        return cfg, active, saved_hidden

    for key in saved_active:
        hw_data = _hw_or_saved(key)
        cfg[key] = _make_entry(key, hw_data)
        if key not in hw_by_key:
            log.info("Saved input %s not in hardware scan — kept as offline", key)
        active.append(key)

    for key, e in hw_by_key.items():
        if key not in active and key not in saved_hidden:
            cfg[key] = _make_entry(key, e)
            active.append(key)
            log.info("New hardware input detected: %s — adding automatically", key)

    for key in saved_hidden:
        if key not in cfg:
            hw_data = _hw_or_saved(key)
            cfg[key] = _make_entry(key, hw_data)

    active = _sort_ids(active)
    log.info("Restored %d active input(s): %s", len(active), active)
    if saved_hidden:
        log.info("Hidden (user-removed): %s", sorted(saved_hidden))
    return cfg, active, saved_hidden

input_config, INPUT_IDS, HIDDEN_IDS = _bootstrap()

# Load fan/CC config
_fan_config = _load_fan_config()

# Activate CoolerControl default mode at startup so the correct
# profile is applied immediately rather than waiting for activity
def _cc_startup_activate() -> None:
    cc_cfg = _fan_config.get("coolercontrol", {})
    if not cc_cfg.get("enabled") or not cc_cfg.get("token"):
        return
    mode_uid = cc_cfg.get("mode_default", "")
    if not mode_uid:
        return
    import urllib.request as _ureq
    url   = cc_cfg.get("url", "http://localhost:11987").rstrip("/")
    token = cc_cfg.get("token", "")
    try:
        req = _ureq.Request(
            f"{url}/modes-active/{mode_uid}",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
            data=b"",
        )
        with _ureq.urlopen(req, timeout=5) as r:
            if r.status == 200:
                log.info("Startup: CoolerControl default mode activated (%s)", mode_uid)
    except Exception as exc:
        log.warning("Startup: CoolerControl default mode activation failed: %s", exc)

_cc_startup_activate()
_save_config(input_config, INPUT_IDS, HIDDEN_IDS)

# Per-user recording directory preferences  key: IP string → rec_dir string
_raw_cfg = _load_config_file()
USER_PREFS: dict = _raw_cfg.get("__user_prefs__", {})
user_prefs_lock = asyncio.Lock()

os.makedirs(HLS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Startup: kill any orphaned curl/ffmpeg processes left from a previous crash
# that are still connected to our port.  These show up as phantom viewers.
# ---------------------------------------------------------------------------
def _kill_orphaned_stream_clients() -> None:
    """Kill curl and ffmpeg processes that are pulling from our stream port."""
    port_str = str(6502)
    killed   = 0
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name    = (proc.info.get("name") or "").lower()
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if name in ("curl", "ffmpeg") and f":{port_str}/stream/" in cmdline:
                    try:
                        os.killpg(os.getpgid(proc.pid), 9)
                    except Exception:
                        proc.kill()
                    # Reap synchronously here — this runs once at startup
                    # before the event loop exists, so we use a plain
                    # blocking wait with a generous timeout rather than
                    # the async _kill_and_reap helper used elsewhere.
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        pass
                    killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass
    if killed:
        log.warning("Startup: killed %d orphaned stream client process(es)", killed)

_kill_orphaned_stream_clients()

# ---------------------------------------------------------------------------
# Startup: write saved EDIDs once for all configured Magewell inputs.
# Running this at startup (rather than on every capture start) avoids the
# double-tune problem where magewell2ts -w briefly initialises the hardware,
# causing the source device to see two tune events in quick succession.
# ---------------------------------------------------------------------------
def _write_startup_edids() -> None:
    written = 0
    for input_id, cfg in input_config.items():
        if cfg.get("driver") != "magewell":
            continue
        edid_path = cfg.get("edid_path", "").strip()
        if not edid_path or not os.path.isfile(edid_path):
            continue
        board   = cfg.get("board",   0)
        channel = cfg.get("channel", 1)
        try:
            result = subprocess.run(
                ["magewell2ts", "-w", edid_path,
                 "-b", str(board), "-i", str(channel)],
                capture_output=True, timeout=15,
            )
            if result.returncode == 0:
                log.info("Startup EDID written for input %s: %s",
                         input_id, edid_path)
                written += 1
            else:
                log.warning("Startup EDID write failed for input %s "
                            "(code %d): %s",
                            input_id, result.returncode,
                            (result.stdout + result.stderr).decode(errors="replace").strip())
        except Exception as exc:
            log.warning("Startup EDID write error for input %s: %s",
                        input_id, exc)
    if written:
        # Small delay after writing EDIDs so the hardware fully settles
        # before any capture process opens the inputs.
        import time as _time
        _time.sleep(1.5)

_write_startup_edids()
# ---------------------------------------------------------------------------

def _magewell_module_loaded() -> bool:
    """Return True if the ProCapture kernel module is currently loaded."""
    module_name = "ProCapture"
    try:
        result = subprocess.run(
            ["lsmod"], capture_output=True, text=True, timeout=5
        )
        loaded = module_name in result.stdout
        if not loaded:
            log.debug("Magewell: lsmod searched for '%s' — not found", module_name)
        return loaded
    except Exception as exc:
        log.warning("Magewell: lsmod check failed: %s", exc)
        return False


_installer_path_cache: str | None = None   # None = not yet loaded


def _get_installer_path() -> str:
    """Return saved Magewell installer path, using an in-memory cache.

    The path almost never changes at runtime so we avoid hitting the
    filesystem on every SSE stats tick (every 2 seconds).
    """
    global _installer_path_cache
    if _installer_path_cache is None:
        cfg = _load_config_file()
        _installer_path_cache = cfg.get("__user_prefs__", {}).get("magewell_installer", "").strip()
    return _installer_path_cache


def _save_installer_path(path: str) -> None:
    """Persist Magewell installer path into __user_prefs__ in config and update cache."""
    global _installer_path_cache
    cfg = _load_config_file()
    cfg.setdefault("__user_prefs__", {})["magewell_installer"] = path
    config_dir = os.path.dirname(CONFIG_FILE)
    fd, tmp = tempfile.mkstemp(dir=config_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_FILE)
        _installer_path_cache = path   # keep cache in sync
    except Exception as exc:
        log.error("Failed to save installer path: %s", exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass


# True when the module was missing at last startup check
DRIVER_MISSING: bool = False

def _check_driver_at_startup() -> None:
    """Detect missing Magewell module at startup and attempt auto-reinstall.

    If the module is absent AND an installer path is configured, runs
    install.sh via sudo (passwordless sudoers rule required — set up by
    setup.sh) and reloads the module.  Sets the global DRIVER_MISSING flag
    so the dashboard can show a banner if action is still needed.
    """
    global DRIVER_MISSING

    if _magewell_module_loaded():
        log.info("Magewell: ProCapture module loaded OK")
        DRIVER_MISSING = False
        return

    DRIVER_MISSING = True
    log.warning("Magewell: ProCapture module NOT loaded")

    installer = _get_installer_path()
    if not installer:
        log.warning("Magewell: no installer path configured — open the dashboard to set it up")
        return

    install_script = os.path.join(installer, "install.sh")
    if not os.path.isfile(install_script):
        log.error("Magewell: install.sh not found at %s", install_script)
        return

    log.info("Magewell: attempting automatic reinstall via %s", install_script)
    try:
        result = subprocess.run(
            ["sudo", "-n", install_script],
            capture_output=True, text=True, timeout=120,
            cwd=installer,
        )
        # sudo -n exits with code 1 and stderr "sudo: a password is required"
        # when no passwordless rule exists — catch this explicitly
        if result.returncode != 0 and "password is required" in result.stderr:
            log.error(
                "Magewell: sudo requires a password — run 'sudo ./setup.sh' once "
                "to configure the passwordless sudoers rule for the installer"
            )
            return
        for line in (result.stdout + result.stderr).splitlines():
            log.info("Magewell install: %s", line)
        if result.returncode == 0:
            log.info("Magewell: install.sh completed — reloading module")
            subprocess.run(["sudo", "-n", "modprobe", "ProCapture"],
                           capture_output=True, timeout=10)
            if _magewell_module_loaded():
                log.info("Magewell: module loaded successfully after reinstall")
                DRIVER_MISSING = False
            else:
                log.error("Magewell: module still not loaded after reinstall")
        else:
            log.error("Magewell: install.sh exited with code %d", result.returncode)
    except subprocess.TimeoutExpired:
        log.error("Magewell: install.sh timed out after 120s")
    except Exception as exc:
        log.error("Magewell: reinstall failed: %s", exc)


_check_driver_at_startup()

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
_auth.init()

# Paths that bypass auth entirely (HLS players, health check)
# /stream/ must be public so internal ffmpeg processes (preview, HLS writer,
# recording worker) can connect via http://127.0.0.1:6502/stream/{id} without
# a session cookie — omitting this causes all three to receive a login-page
# response and produce no video.
_PUBLIC_PATHS = {
    "/health", "/login", "/setup", "/logout", "/favicon.ico",
    # HDHomeRun emulation — read by Plex/Channels/Emby/Jellyfin, which
    # cannot authenticate through Broadcast Hub's login form.
    "/discover.json", "/lineup.json", "/lineup.xml", "/lineup_status.json",
}
_PUBLIC_PREFIXES = (
    "/hls/", "/stream/",
    # UPnP/DLNA — read by VLC and other DLNA browsers, which likewise
    # cannot authenticate.
    "/upnp/",
)

async def _auth_middleware(request: Request, call_next):
    path = request.url.path

    # Always allow public paths
    if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)

    # If no password has been set yet, force setup
    if not _auth.is_configured():
        if request.method == "GET":
            return _auth.setup_page()
        return await call_next(request)

    # Check session cookie
    if not _auth.is_authenticated(request):
        if request.method == "GET":
            return _auth.login_page(next_url=request.url.path)
        # For POST/etc from an unauthenticated client, return 401
        return Response(status_code=401, content="Session expired — please log in.")

    return await call_next(request)

# ---------------------------------------------------------------------------
# Disk space guard
# ---------------------------------------------------------------------------
MINIMUM_FREE_BYTES = int(os.environ.get("MIN_FREE_BYTES", 500 * 1024 * 1024))  # 500 MB

def _check_disk_space(path: str) -> tuple[bool, int]:
    """Return (ok, free_bytes) for the filesystem containing *path*."""
    try:
        parent = os.path.dirname(path)
        # Walk up until we find an existing directory
        while parent and not os.path.exists(parent):
            parent = os.path.dirname(parent)
        stat = os.statvfs(parent or "/")
        free = stat.f_bavail * stat.f_frsize
        return free >= MINIMUM_FREE_BYTES, free
    except Exception as exc:
        log.warning("Disk space check failed for %s: %s", path, exc)
        return True, -1   # assume ok if we can't check

# ---------------------------------------------------------------------------
# Background stats loop
# ---------------------------------------------------------------------------
async def update_stats_loop():
    while True:
        async with inputs_lock:
            snapshot = {i: v["p_obj"] for i, v in active_inputs.items()}

        new_stats = {}
        for i, p_obj in snapshot.items():
            try:
                new_stats[i] = {
                    "cpu": p_obj.cpu_percent(interval=None) / CPU_COUNT,
                    "mem": p_obj.memory_info().rss / (1024 * 1024),
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        async with inputs_lock:
            for i, stats in new_stats.items():
                if i in active_inputs:
                    active_inputs[i]["stats"] = stats

        await asyncio.sleep(2)


async def watchdog_loop():
    while True:
        await asyncio.sleep(3)

        to_respawn = []
        now = time.time()

        async with inputs_lock:
            for input_id, watch in list(SHOULD_BE_LIVE.items()):
                if watch.get("faulted"):
                    continue
                if input_id in active_inputs:
                    continue

                restart_count = watch.get("restart_count", 0)
                last_restart  = watch.get("last_restart", 0)

                if now - last_restart > WATCHDOG_WINDOW * 2:
                    restart_count = 0
                    SHOULD_BE_LIVE[input_id]["restart_count"] = 0

                if restart_count >= WATCHDOG_MAX_RESTARTS:
                    log.error("Watchdog: %s faulted after %d restarts — giving up", input_id, restart_count)
                    SHOULD_BE_LIVE[input_id]["faulted"] = True
                    continue

                backoff = WATCHDOG_BACKOFF[min(restart_count, len(WATCHDOG_BACKOFF) - 1)]
                if now - last_restart < backoff:
                    continue

                to_respawn.append(input_id)

        for input_id in to_respawn:
            log.warning("Watchdog: respawning %s (attempt %d)", input_id, SHOULD_BE_LIVE[input_id]['restart_count'] + 1)
            async with inputs_lock:
                SHOULD_BE_LIVE[input_id]["restart_count"] = SHOULD_BE_LIVE[input_id].get("restart_count", 0) + 1
                SHOULD_BE_LIVE[input_id]["last_restart"]  = time.time()
                await _ensure_input(input_id)

# ---------------------------------------------------------------------------
# HLS writer
# ---------------------------------------------------------------------------
async def hls_writer(input_id: str):
    out_dir     = os.path.join(HLS_DIR, input_id)
    os.makedirs(out_dir, exist_ok=True)
    playlist    = os.path.join(out_dir, "index.m3u8")
    seg_pattern = os.path.join(out_dir, "seg%05d.ts")

    proc = subprocess.Popen(
        [
            "ffmpeg", "-y",
            "-i", f"http://127.0.0.1:6502/stream/{input_id}",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-f", "hls",
            "-hls_time", str(HLS_DURATION),
            "-hls_list_size", str(HLS_SEGMENTS),
            "-hls_flags", "delete_segments+append_list",
            "-hls_segment_filename", seg_pattern,
            playlist,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        # Own process group so we can kill ffmpeg AND its curl children together
        start_new_session=True,
    )
    async with hls_lock:
        active_hls[input_id] = {
            "process":     proc,
            "out_dir":     out_dir, "playlist": playlist,
            "viewers":     0,             # legacy int kept for compat
            "hls_clients": {},            # ip -> last_seen timestamp
            "started_at":  time.time(),
        }
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, proc.wait)
    async with hls_lock:
        active_hls.pop(input_id, None)
    try:
        shutil.rmtree(out_dir, ignore_errors=True)
    except Exception:
        pass


# Sentinel set to prevent duplicate hls_writer tasks spawning during the
# window between create_task() and the writer registering in active_hls.
_hls_starting: set = set()

async def ensure_hls(input_id: str):
    async with hls_lock:
        if input_id in active_hls or input_id in _hls_starting:
            return
        _hls_starting.add(input_id)
    try:
        asyncio.create_task(hls_writer(input_id))
    finally:
        # Give the writer a moment to register itself in active_hls before
        # clearing the sentinel so a rapid second call doesn't slip through.
        async def _clear():
            await asyncio.sleep(2)
            _hls_starting.discard(input_id)
        asyncio.create_task(_clear())


async def stop_hls(input_id: str):
    async with hls_lock:
        entry = active_hls.get(input_id)
        _hls_starting.discard(input_id)
    if entry:
        proc = entry["process"]
        await _kill_and_reap(proc, label=f"HLS {input_id}")


# ---------------------------------------------------------------------------
# Multiview per-client stream
# ---------------------------------------------------------------------------
# Simple approach: one ffmpeg process per cell connection, reading directly
# from the stream pipe. No queues, no buffering, no memory accumulation.
# ffmpeg is killed immediately when the client disconnects via GeneratorExit.

async def _mv_stream_gen(input_id: str, request: Request):
    """Tap directly into the magewell2ts distributor queue — no ffmpeg middleman.

    Uses the exact same viewer registration as /stream/ so cleanup is
    guaranteed and there is no buffering accumulation.
    """
    client_ip = request.client.host if request.client else "unknown"
    # maxsize=500 (was 50): at 16KB/chunk this absorbs ~8MB of momentary
    # consumer stall (e.g. disk I/O hiccup in a recording ffmpeg process)
    # without silently dropping MPEG-TS chunks mid-stream. A too-small
    # queue here was traced to sporadic frame corruption on long (1-2hr+)
    # 4K60 recordings — see distributor() drop logging for confirmation.
    queue = asyncio.Queue(maxsize=500)
    viewer = {"queue": queue, "ip": client_ip, "connected_at": time.time()}

    async with inputs_lock:
        await _ensure_input(input_id, viewer)
        if input_id not in SHOULD_BE_LIVE:
            SHOULD_BE_LIVE[input_id] = {"restart_count": 0, "last_restart": 0, "faulted": False}

    consecutive_timeouts = 0
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=5.0)
                consecutive_timeouts = 0
            except asyncio.TimeoutError:
                consecutive_timeouts += 1
                if consecutive_timeouts >= 3:
                    break
                continue
            yield chunk
    except GeneratorExit:
        pass
    finally:
        async with inputs_lock:
            if input_id in active_inputs:
                active_inputs[input_id]["viewers"] = [
                    v for v in active_inputs[input_id]["viewers"]
                    if v["queue"] is not queue
                ]
                remaining = len(active_inputs[input_id]["viewers"])
                active_inputs[input_id]["viewer_count"] = remaining
                if remaining == 0:
                    SHOULD_BE_LIVE.pop(input_id, None)
        log.info("Multiview: %s disconnected from %s", client_ip, input_id)


# Keep active_mv for shutdown tracking
async def stop_mv(input_id: str):
    pass  # Per-client processes self-clean on disconnect

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# EDID refresh loop
# ---------------------------------------------------------------------------
# Eco cards lose their EDID when no driver has the input open. This loop
# periodically re-writes the saved EDID to each Magewell input that has one
# configured, but ONLY when no capture process is active for that input.
# Writing while a capture is active would cause a retune on the source device.
#
# Interval: every 25 seconds. A source device that re-probes after signal
# loss will see a fresh EDID within 25 seconds, well before most retry logic
# gives up and falls back to a minimal default negotiation.
EDID_REFRESH_INTERVAL = 25   # seconds between refresh cycles

async def edid_refresh_loop():
    loop = asyncio.get_running_loop()
    while True:
        try:
            await asyncio.sleep(EDID_REFRESH_INTERVAL)
            async with input_config_lock:
                cfg_snap = {k: dict(v) for k, v in input_config.items()}
            async with inputs_lock:
                live_inputs = set(active_inputs.keys())

            for input_id, cfg in cfg_snap.items():
                if cfg.get("driver") != "magewell":
                    continue
                edid_path = cfg.get("edid_path", "").strip()
                if not edid_path or not os.path.isfile(edid_path):
                    continue
                # Skip if periodic refresh is disabled for this input
                if not cfg.get("edid_refresh", False):
                    continue
                # Skip if this input is currently active — writing EDID
                # while a capture is running causes the source to retune
                if input_id in live_inputs:
                    continue
                board   = cfg.get("board",   0)
                channel = cfg.get("channel", 1)
                try:
                    result = await loop.run_in_executor(
                        None,
                        lambda b=board, c=channel, p=edid_path: subprocess.run(
                            ["magewell2ts", "-w", p, "-b", str(b), "-i", str(c)],
                            capture_output=True, timeout=10,
                        )
                    )
                    if result.returncode == 0:
                        log.debug("EDID refresh: written for idle input %s", input_id)
                    else:
                        log.debug("EDID refresh: failed for input %s (code %d)",
                                  input_id, result.returncode)
                except Exception as exc:
                    log.debug("EDID refresh error for input %s: %s", input_id, exc)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("edid_refresh_loop error: %s", exc)


# ---------------------------------------------------------------------------
# Fan / CoolerControl manager loop
# ---------------------------------------------------------------------------
async def fan_manager_loop():
    """Watch active_records and active_hls to switch CoolerControl modes
    or per-input PWM fans based on activity. Polls every 3 seconds."""
    global _fan_active_profile
    loop        = asyncio.get_running_loop()
    last_active = 0.0

    while True:
        try:
            await asyncio.sleep(3)

            async with _fan_config_lock:
                cc_cfg   = _fan_config.get("coolercontrol", {})
            cc_enabled  = cc_cfg.get("enabled", False)
            cooldown    = 60  # seconds before returning to default

            is_recording   = len(active_records) > 0
            is_transcoding = len(active_hls) > 0
            async with inputs_lock:
                is_streaming = len(active_inputs) > 0

            if is_transcoding:
                target = "transcoding"
            elif is_recording:
                target = "recording"
            elif is_streaming:
                target = "streaming"
            else:
                target = "default"

            now = loop.time()
            if target != "default":
                last_active = now

            # Hold active profile during cooldown
            if target == "default" and (now - last_active) < cooldown:
                target = _fan_active_profile if _fan_active_profile != "default" else "default"

            if _fan_active_profile == target:
                continue

            # Profile changed — apply via CoolerControl if enabled
            if cc_enabled:
                mode_uid = cc_cfg.get(f"mode_{target}", "")
                if mode_uid:
                    ok = await _cc_activate_mode(cc_cfg, mode_uid)
                    if ok:
                        log.info("CoolerControl: switched to '%s' mode (profile: %s)",
                                 mode_uid, target)
                    else:
                        log.warning("CoolerControl: failed to switch to profile '%s'", target)
            else:
                # Fall back to per-input PWM fan control
                async with input_config_lock:
                    cfg_snap = {k: dict(v) for k, v in input_config.items()}
                async with inputs_lock:
                    live = set(active_inputs.keys())
                for input_id, cfg in cfg_snap.items():
                    pwm_path = cfg.get("fan_pwm_path", "").strip()
                    if not pwm_path:
                        continue
                    if target != "default":
                        pwm = cfg.get("fan_pwm_active", 64)
                    else:
                        pwm = cfg.get("fan_pwm_idle", 64)
                    await loop.run_in_executor(None, _pwm_write, pwm_path, pwm)

            log.info("Fan manager: %s → %s", _fan_active_profile, target)
            _fan_active_profile = target

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("fan_manager_loop error: %s", exc)


async def _kill_and_reap(proc, label: str = "") -> bool:
    """Kill a process group and reliably reap the zombie afterward.

    A bare `proc.wait(timeout=0.5)` can raise TimeoutExpired if the
    process is slow to die (e.g. mid-syscall into a hardware SDK), and if
    that exception is swallowed by a broad except-clause, the process is
    never reaped — it becomes a zombie that can outlive even a full
    service restart, since it gets reparented to PID 1 and may not be
    reliably cleaned up by init in all cases.

    This helper retries the wait with increasing patience and always
    reaps eventually, falling back to polling via os.waitpid with
    WNOHANG in a background thread if the standard wait still doesn't
    return in time.
    """
    pid = proc.pid
    try:
        # Only kill the whole process group if this process actually has
        # its own group (start_new_session=True at Popen time). Otherwise
        # os.getpgid(pid) can resolve to *our own* process group on some
        # platforms/timings, and killpg would be catastrophic. Fall back
        # to a plain proc.kill() (SIGKILL to just this process) instead.
        pgid = os.getpgid(pid)
        if pgid != os.getpgid(0):
            os.killpg(pgid, 9)
        else:
            proc.kill()
    except ProcessLookupError:
        return True   # already gone
    except Exception as exc:
        log.warning("_kill_and_reap: kill failed for %s (pid=%d): %s", label, pid, exc)
        try:
            proc.kill()
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    for attempt_timeout in (0.5, 1.5, 3.0):
        try:
            await loop.run_in_executor(None, lambda: proc.wait(timeout=attempt_timeout))
            return True
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            break

    # Final fallback: keep trying in the background so we never leak a
    # zombie even if the process is unusually slow to exit. This does not
    # block the caller.
    async def _background_reap():
        for _ in range(30):  # up to ~30s more
            try:
                await loop.run_in_executor(None, lambda: proc.wait(timeout=1.0))
                log.info("_kill_and_reap: %s (pid=%d) reaped after extended wait", label, pid)
                return
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                return
        log.warning(
            "_kill_and_reap: %s (pid=%d) still not reaped after extended wait — "
            "may become a zombie", label, pid,
        )

    asyncio.create_task(_background_reap())
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    _tty_fd    = None
    _tty_state = None
    try:
        _tty_fd    = sys.stdin.fileno()
        _tty_state = termios.tcgetattr(_tty_fd)
    except Exception:
        pass

    stats_task    = asyncio.create_task(update_stats_loop())
    watchdog_task = asyncio.create_task(watchdog_loop())
    schedule_task = asyncio.create_task(schedule_runner())
    fan_task      = asyncio.create_task(fan_manager_loop())
    edid_task     = asyncio.create_task(edid_refresh_loop())

    # UPnP/DLNA SSDP — best-effort; a failure here (e.g. port 1900 already
    # in use by another SSDP responder on this machine) is logged but
    # never prevents the rest of Broadcast Hub from starting normally.
    ssdp_transport = await _start_ssdp_listener()
    ssdp_announce_task = asyncio.create_task(_ssdp_announce_loop(ssdp_transport))

    try:
        yield
    finally:
        stats_task.cancel()
        watchdog_task.cancel()
        schedule_task.cancel()
        fan_task.cancel()
        edid_task.cancel()
        ssdp_announce_task.cancel()
        if ssdp_transport is not None:
            try:
                ssdp_transport.close()
            except Exception:
                pass
        await asyncio.gather(
            stats_task, watchdog_task, schedule_task, fan_task, edid_task,
            ssdp_announce_task,
            return_exceptions=True,
        )

        # Restore all managed fans to idle before shutdown
        loop = asyncio.get_event_loop()
        for input_id, cfg in input_config.items():
            pwm_path = cfg.get("fan_pwm_path", "").strip()
            if pwm_path:
                idle = cfg.get("fan_pwm_idle", 64)
                await loop.run_in_executor(None, _pwm_write, pwm_path, idle)

        for rec in list(active_records.values()):
            await _kill_and_reap(rec["process"], label="recording (shutdown)")

        for entry in list(active_hls.values()):
            await _kill_and_reap(entry["process"], label="HLS (shutdown)")

        for entry in list(active_inputs.values()):
            try:
                entry["process"].stdout.close()
            except Exception:
                pass
            await _kill_and_reap(entry["process"], label="input (shutdown)")

        if _tty_fd is not None and _tty_state is not None:
            try:
                termios.tcsetattr(_tty_fd, termios.TCSADRAIN, _tty_state)
            except Exception:
                pass

app = FastAPI(lifespan=lifespan)

# Startup self-check: verify `lifespan` is actually wired as an async
# context manager. This exists because of a real incident where the
# @asynccontextmanager decorator was accidentally moved from `lifespan`
# onto an unrelated helper function during an edit — `lifespan` silently
# lost its decorator, and every cleanup call in the app started failing
# with "coroutine was never awaited" instead of a clear error, which took
# a long debugging session to trace. This check fails loudly and
# immediately at import time instead, so the same class of mistake is
# caught in seconds rather than days.
if not hasattr(lifespan, "__wrapped__"):
    raise RuntimeError(
        "lifespan is missing @asynccontextmanager — FastAPI's lifespan "
        "wiring is broken. Check that the decorator sits directly above "
        "`async def lifespan(app):` and hasn't been moved elsewhere."
    )
app.middleware("http")(_auth_middleware)

# ---------------------------------------------------------------------------
# Stderr drain helper
# ---------------------------------------------------------------------------
async def _drain_stderr(label: str, proc: subprocess.Popen) -> None:
    """Drain stderr in a single background thread — never spawns more than one
    thread per process regardless of output rate.

    Using run_in_executor per readline was spawning a new thread for every
    line of ffmpeg output, causing thread count to grow unbounded under load.
    """
    if proc.stderr is None:
        return

    def _read_all():
        """Read stderr to completion in one blocking thread, keep last 5 lines."""
        last_lines = []
        temp_found = None
        try:
            for raw in proc.stderr:
                decoded = raw.decode(errors="replace").rstrip()
                if decoded:
                    last_lines.append(decoded)
                    if len(last_lines) > 5:
                        last_lines.pop(0)
                    # Parse Magewell card temperature from startup line
                    # e.g. "Temperature: 66.3ºC" or "Driver: 123, Temperature: 66.3ºC"
                    m = re.search(r"Temperature:\s*([\d.]+)", decoded)
                    if m:
                        try:
                            temp_found = float(m.group(1))
                        except ValueError:
                            pass
        except Exception:
            pass
        finally:
            try: proc.stderr.close()
            except Exception: pass
        return last_lines, temp_found

    loop = asyncio.get_running_loop()
    try:
        last_lines, temp_found = await loop.run_in_executor(None, _read_all)
        # Store temperature in active_inputs if found
        if temp_found is not None:
            async with inputs_lock:
                if input_id in active_inputs:
                    active_inputs[input_id]["temperature"] = temp_found
        rc = proc.poll()
        if rc and rc != 0 and last_lines:
            for line in last_lines:
                log.warning("%s: %s", label, line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Stream distributor
# ---------------------------------------------------------------------------
# A viewer is considered stale if its queue has been full (put_nowait failed)
# repeatedly OR if last_read hasn't advanced in VIEWER_STALE_SECS seconds.
# This catches clients whose GeneratorExit never fires (e.g. uvicorn silently
# stops iterating the generator on a broken pipe without cancelling it).
VIEWER_STALE_SECS = 30   # seconds without consuming a chunk → evict

async def distributor(input_id, process):
    loop = asyncio.get_running_loop()
    idle_since    = None
    last_evict_check = loop.time()
    try:
        while True:
            async with inputs_lock:
                if input_id not in active_inputs:
                    break
                viewers = active_inputs[input_id].get("viewers", [])
                if len(viewers) == 0:
                    if idle_since is None:
                        idle_since = loop.time()
                    elif loop.time() - idle_since > 10:
                        break
                else:
                    idle_since = None

                # Periodically evict stale viewers whose cleanup path never fired
                now = loop.time()
                if now - last_evict_check > 10:
                    last_evict_check = now
                    wall_now = time.time()
                    live, evicted = [], []
                    for v in viewers:
                        age = wall_now - v.get("last_read", wall_now)
                        if age > VIEWER_STALE_SECS and v["queue"].full():
                            evicted.append(v)
                        else:
                            live.append(v)
                    if evicted:
                        active_inputs[input_id]["viewers"]      = live
                        active_inputs[input_id]["viewer_count"] = len(live)
                        for v in evicted:
                            log.warning(
                                "Distributor: evicted stale viewer %s from %s "
                                "(no data consumed for %.0fs)",
                                v["ip"], input_id,
                                wall_now - v.get("last_read", wall_now),
                            )
                        if len(live) == 0:
                            SHOULD_BE_LIVE.pop(input_id, None)

            # Check first whether the process has already died — avoids
            # issuing a doomed read against a closed/dead pipe.
            if process.poll() is not None:
                break

            # Non-blocking read: returns immediately whether or not data
            # is ready, so this can NEVER leave a thread permanently
            # parked on a stalled-but-alive process (e.g. lost signal,
            # frozen encoder). This is a quick, cheap operation — no
            # executor/thread needed at all.
            chunk = _nonblocking_read(process.stdout, 16384)
            if chunk is None:
                # No data available right now — not an error, just wait
                # briefly and let the outer loop re-check viewer/poll state.
                await asyncio.sleep(0.05)
                continue
            if not chunk:
                break   # genuine EOF — process closed its stdout
            async with inputs_lock:
                if input_id in active_inputs:
                    for viewer in list(active_inputs[input_id]["viewers"]):
                        try:
                            viewer["queue"].put_nowait(chunk)
                        except asyncio.QueueFull:
                            # This chunk is silently lost to this viewer —
                            # log it (rate-limited) so persistent drops are
                            # visible rather than manifesting only as
                            # downstream frame corruption with no clear cause.
                            last_warn = viewer.get("_last_drop_warn", 0)
                            now_t = time.time()
                            if now_t - last_warn > 30:
                                viewer["_last_drop_warn"] = now_t
                                drop_count = viewer.get("_drop_count", 0) + 1
                                viewer["_drop_count"] = drop_count
                                log.warning(
                                    "Distributor: queue full, dropped chunk for "
                                    "viewer %s on %s (drops so far: %d) — "
                                    "consumer is falling behind",
                                    viewer.get("ip", "?"), input_id, drop_count,
                                )
    finally:
        exit_code = process.poll()
        log.info("Distributor: input %s process exited (code=%s)", input_id, exit_code)
        try:
            process.stdout.close()
        except Exception:
            pass
        await _kill_and_reap(process, label=f"distributor input {input_id}")
        async with inputs_lock:
            if input_id in active_inputs:
                del active_inputs[input_id]
        log.info("Distributor: removed %s from active_inputs", input_id)

# ---------------------------------------------------------------------------
# Recording worker
# ---------------------------------------------------------------------------
ADB_HOME_DELAY = 60  # seconds after recording ends before sending Home

# Recordings longer than this trigger an automatic capture-process restart
# once they finish. Long 4K60 HEVC sessions can accumulate buffer/encoder
# drift (frame tearing) over multiple hours; a fresh process clears it.
CAPTURE_RESTART_THRESHOLD_SECS = 90 * 60  # 90 minutes

async def _restart_capture_process(input_id: str) -> bool:
    """Kill the running capture process for input_id, if any.
    The distributor / next viewer request will spawn a fresh one
    automatically, clearing any accumulated encoder/buffer drift."""
    async with inputs_lock:
        entry = active_inputs.get(input_id)
        if not entry:
            return False
    ok = await _kill_and_reap(entry["process"], label=f"restart input {input_id}")
    if ok:
        log.info("Capture restart: killed long-running process for %s", input_id)
    else:
        log.warning("Capture restart: kill issued for %s but reap is still pending", input_id)
    return ok


async def recording_worker(record_id: str, input_id: str, output_path: str,
                            fmt: str, duration: int, adb_home: bool = False):
    ff_fmt, _ = FORMAT_EXT[fmt]
    duration_args = ["-t", str(duration)] if duration > 0 else []
    rec_started_at = time.time()
    proc = subprocess.Popen(
        [
            "ffmpeg", "-y",
            "-i", f"http://127.0.0.1:6502/stream/{input_id}",
            "-c:v", "copy", "-c:a", "copy",
            *duration_args, "-f", ff_fmt, output_path,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,   # own process group, consistent with other ffmpeg launches
    )
    async with records_lock:
        if record_id in active_records:
            active_records[record_id]["process"] = proc
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, proc.wait)
    async with records_lock:
        if record_id in active_records:
            del active_records[record_id]

    # If this recording ran long enough to risk accumulated encoder/buffer
    # drift (frame tearing on long 4K60 sessions), restart the underlying
    # capture process now so the next recording starts clean. Safe to do
    # immediately — magewell2ts re-spawns automatically on next viewer
    # request, with only a brief (<1s) reconnect gap.
    rec_elapsed = time.time() - rec_started_at
    if rec_elapsed >= CAPTURE_RESTART_THRESHOLD_SECS:
        log.info(
            "Recording %s ran %.0f min — restarting capture process for %s "
            "to clear accumulated drift",
            record_id, rec_elapsed / 60, input_id,
        )
        await _restart_capture_process(input_id)

    # Optionally send Home command after recording ends (ADB or Roku)
    if adb_home:
        async with input_config_lock:
            cfg         = input_config.get(input_id, {})
            remote_type = cfg.get("remote_type", "adb" if cfg.get("adb_ip") else "none")
            remote_ip   = cfg.get("remote_ip", cfg.get("adb_ip", "")).strip()
        if remote_ip:
            await asyncio.sleep(ADB_HOME_DELAY)
            try:
                if remote_type == "roku":
                    import urllib.request
                    await loop.run_in_executor(
                        None,
                        lambda: urllib.request.urlopen(
                            urllib.request.Request(
                                f"http://{remote_ip}:8060/keypress/Home",
                                method="POST", data=b"",
                            ),
                            timeout=3,
                        )
                    )
                    log.info("Roku Home sent to %s after recording %s", remote_ip, record_id)
                else:
                    await loop.run_in_executor(
                        None,
                        lambda: subprocess.run(
                            ["adb", "-s", remote_ip, "shell", "input", "keyevent",
                             str(ADB_KEYCODES["home"])],
                            capture_output=True, timeout=5
                        )
                    )
                    log.info("ADB home sent to %s after recording %s", remote_ip, record_id)
            except Exception as e:
                log.warning("Home command failed for %s: %s", record_id, e)

# ---------------------------------------------------------------------------
# Schedule runner
# ---------------------------------------------------------------------------
async def schedule_runner():
    while True:
        now = time.time()
        async with schedule_lock:
            for job_id in list(scheduled_jobs.keys()):
                job = scheduled_jobs[job_id]
                if job["start_ts"] <= now:
                    if now - job["start_ts"] > 60:
                        del scheduled_jobs[job_id]
                        continue
                    record_id = str(uuid.uuid4())[:8]
                    async with records_lock:
                        active_records[record_id] = {
                            "input_id":    job["input_id"],
                            "output_path": job["output_path"],
                            "fmt":         job["fmt"],
                            "duration":    job["duration"],
                            "started_at":  time.time(),
                            "process":     None,
                            "label":       job.get("label", ""),
                        }
                    asyncio.create_task(
                        recording_worker(record_id, job["input_id"],
                                         job["output_path"], job["fmt"],
                                         job["duration"],
                                         job.get("adb_home", False))
                    )
                    del scheduled_jobs[job_id]
        await asyncio.sleep(5)

# ---------------------------------------------------------------------------
# Ensure magewell2ts is running for input_id "B-I"
# Must be called with inputs_lock held.
# ---------------------------------------------------------------------------
async def _ensure_input(input_id: str, viewer=None):
    if input_id not in active_inputs:
        async with input_config_lock:
            cfg = input_config.get(input_id, {})
        try:
            cmd = _build_capture_cmd(input_id, cfg)
        except ValueError as e:
            log.error("_ensure_input: cannot start %s: %s", input_id, e)
            return
        log.info("Starting input %s: %s", input_id, " ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, bufsize=0,
        )
        _make_pipe_nonblocking(proc.stdout)
        p_obj = psutil.Process(proc.pid)
        p_obj.cpu_percent(None)
        log.info("_ensure_input: started capture for %s (pid=%d, driver=%s, encoder=%s)", input_id, proc.pid, cfg.get('driver','magewell'), cfg.get('encoder', AVAILABLE_ENCODERS[0]))
        active_inputs[input_id] = {
            "process":      proc,
            "p_obj":        p_obj,
            "viewers":      [viewer] if viewer is not None else [],
            "viewer_count": 1 if viewer is not None else 0,
            "stats":        {"cpu": 0.0, "mem": 0.0},
            "task":         asyncio.create_task(distributor(input_id, proc)),
            "stderr_task":  asyncio.create_task(_drain_stderr(f"capture/{input_id}", proc)),
        }
    elif viewer is not None:
        active_inputs[input_id]["viewers"].append(viewer)
        active_inputs[input_id]["viewer_count"] = active_inputs[input_id].get("viewer_count", 0) + 1

# ---------------------------------------------------------------------------
# Routes — streaming
# ---------------------------------------------------------------------------
@app.get("/stream/{input_id:path}")
async def stream(input_id: str, request: Request):
    if input_id.isdigit():
        return RedirectResponse(url=f"/stream/0-{input_id}", status_code=301)

    client_ip = request.client.host if request.client else "unknown"
    # maxsize=500 (was 50): see _mv_stream_gen comment above — same fix,
    # same rationale. This is the queue actually feeding Channels DVR
    # and Broadcast Hub's own recording ffmpeg process, so it's the most
    # likely site of the silent chunk-drop causing long-recording corruption.
    queue = asyncio.Queue(maxsize=500)
    viewer = {
        "queue":        queue,
        "ip":           client_ip,
        "connected_at": time.time(),
        "last_read":    time.time(),   # updated each time a chunk is consumed
    }

    log.info("Stream: new connection to %s from %s", input_id, client_ip)
    async with inputs_lock:
        await _ensure_input(input_id, viewer)
        new_count = active_inputs[input_id].get("viewer_count", 0)
        log.debug("Stream: %s now has %d viewer(s)", input_id, new_count)
        if new_count == 1 and input_id not in SHOULD_BE_LIVE:
            SHOULD_BE_LIVE[input_id] = {"restart_count": 0, "last_restart": 0, "faulted": False}

    async def stream_from_queue():
        consecutive_timeouts = 0
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=5.0)
                    consecutive_timeouts = 0
                    viewer["last_read"] = time.time()   # mark as alive
                except asyncio.TimeoutError:
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= 3:
                        log.warning("Stream: %s → %s timed out waiting for data, closing", client_ip, input_id)
                        break
                    continue
                yield chunk
        except GeneratorExit:
            pass
        finally:
            async with inputs_lock:
                if input_id in active_inputs:
                    active_inputs[input_id]["viewers"] = [
                        v for v in active_inputs[input_id]["viewers"]
                        if v["queue"] is not queue
                    ]
                    remaining = len(active_inputs[input_id]["viewers"])
                    active_inputs[input_id]["viewer_count"] = remaining
                    log.info("Stream: %s disconnected from %s, %d viewer(s) remaining", client_ip, input_id, remaining)
                    if remaining == 0:
                        SHOULD_BE_LIVE.pop(input_id, None)

    return StreamingResponse(stream_from_queue(), media_type="video/mp2t")


@app.get("/preview/{input_id:path}")
async def preview(input_id: str, request: Request):
    async with inputs_lock:
        await _ensure_input(input_id)
    await asyncio.sleep(0.5)

    ffmpeg_proc = subprocess.Popen(
        [
            "ffmpeg",
            "-i", f"http://127.0.0.1:6502/stream/{input_id}",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-f", "mpegts", "pipe:1",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        start_new_session=True,   # own process group — kills curl children too
    )
    _make_pipe_nonblocking(ffmpeg_proc.stdout)

    async def stream_preview():
        consecutive_empty = 0
        try:
            while True:
                if ffmpeg_proc.poll() is not None:
                    break
                chunk = _nonblocking_read(ffmpeg_proc.stdout, 16384)
                if chunk is None:
                    await asyncio.sleep(0.05)
                    continue
                if not chunk:
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        break
                    continue
                consecutive_empty = 0
                yield chunk
        except GeneratorExit:
            pass
        finally:
            try:
                ffmpeg_proc.stdout.close()
            except Exception:
                pass
            await _kill_and_reap(ffmpeg_proc, label="preview ffmpeg")

    return StreamingResponse(stream_preview(), media_type="video/mp2t")

# ---------------------------------------------------------------------------
# Route — set Q
# ---------------------------------------------------------------------------
@app.post("/input/{input_id:path}/set_q")
async def set_input_q(input_id: str, q: int = Form(...)):
    q = max(1, min(51, q))
    async with input_config_lock:
        if input_id not in INPUT_IDS:
            return Response(status_code=404)
        input_config[input_id]["q"] = q
        _save_config(input_config, INPUT_IDS, HIDDEN_IDS)
    restarted = False
    async with inputs_lock:
        if input_id in active_inputs:
            restarted = await _kill_and_reap(active_inputs[input_id]["process"], label=f"input {input_id}")
    return JSONResponse({"ok": True, "q": q, "restarted": restarted})


# ---------------------------------------------------------------------------
# Route — set encoder
# ---------------------------------------------------------------------------
@app.post("/input/{input_id:path}/set_encoder")
async def set_encoder(input_id: str, encoder: str = Form(...)):
    if encoder not in AVAILABLE_ENCODERS:
        return JSONResponse(
            {"ok": False, "error": f"Encoder '{encoder}' not available on this system"},
            status_code=400,
        )
    async with input_config_lock:
        if input_id not in INPUT_IDS:
            return JSONResponse({"ok": False, "error": "Input not found"}, status_code=404)
        input_config[input_id]["encoder"] = encoder
        _save_config(input_config, INPUT_IDS, HIDDEN_IDS)
    restarted = False
    async with inputs_lock:
        if input_id in active_inputs:
            restarted = await _kill_and_reap(active_inputs[input_id]["process"], label=f"input {input_id}")
    return JSONResponse({"ok": True, "encoder": encoder, "restarted": restarted})


# ---------------------------------------------------------------------------
# Route — set Magewell-specific config (preset, lookahead, gop, buffers, p010, no_audio, device, edid)
# ---------------------------------------------------------------------------
@app.post("/input/{input_id:path}/set_magewell_cfg")
async def set_magewell_cfg(
    input_id:        str,
    preset:          str   = Form(""),
    lookahead:       int   = Form(35),
    gop_secs:        float = Form(1.5),
    gpu_buffers:     int   = Form(16),
    video_buffers:   int   = Form(16),
    extra_hw_frames: int   = Form(32),
    p010:            str   = Form("0"),
    no_audio:        str   = Form("0"),
    vaapi_device:    str   = Form(""),
    edid_path:       str   = Form(""),
    edid_refresh:    str   = Form("0"),
):
    async with input_config_lock:
        if input_id not in INPUT_IDS:
            return JSONResponse({"ok": False, "error": "Input not found"}, status_code=404)
        if input_config[input_id].get("driver") != "magewell":
            return JSONResponse({"ok": False, "error": "Input is not a Magewell device"}, status_code=400)
        encoder = input_config[input_id].get("encoder", AVAILABLE_ENCODERS[0])
        if preset and preset not in ENCODER_PRESETS.get(encoder, []):
            preset = ""
        try:
            safe_device = _safe_vaapi_device(vaapi_device)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        edid_path = edid_path.strip()
        if edid_path and not os.path.isfile(edid_path):
            return JSONResponse({"ok": False, "error": f"EDID file not found: {edid_path}"}, status_code=400)
        if edid_path and not edid_path.lower().endswith(".bin"):
            return JSONResponse({"ok": False, "error": "EDID file must be a .bin file"}, status_code=400)
        input_config[input_id].update({
            "preset":          preset.strip(),
            "lookahead":       max(0, min(lookahead, 120)),
            "gop_secs":        max(0.0, round(gop_secs, 2)),
            "gpu_buffers":     max(16, min(gpu_buffers,     256)),
            "video_buffers":   max(1,  min(video_buffers,   256)),
            "extra_hw_frames": max(32, min(extra_hw_frames, 256)),
            "p010":            p010 in ("1", "true", "on"),
            "no_audio":        no_audio in ("1", "true", "on"),
            "vaapi_device":    safe_device,
            "edid_path":       edid_path,
            "edid_refresh":    edid_refresh in ("1", "true", "on"),
        })
        _save_config(input_config, INPUT_IDS, HIDDEN_IDS)
    restarted = False
    async with inputs_lock:
        if input_id in active_inputs:
            restarted = await _kill_and_reap(active_inputs[input_id]["process"], label=f"input {input_id}")
    return JSONResponse({"ok": True, "restarted": restarted})

# ---------------------------------------------------------------------------
# Routes — EDID management (Magewell inputs only)
# ---------------------------------------------------------------------------

@app.get("/input/{input_id:path}/edid/list")
async def edid_list(input_id: str):
    """Return .bin files found in the Magewell installer directory.

    Also searches a few well-known system locations where the Magewell
    driver package installs EDID files.
    """
    async with input_config_lock:
        if input_config.get(input_id, {}).get("driver") != "magewell":
            return JSONResponse({"ok": False, "error": "Not a Magewell input"}, status_code=400)

    installer  = _get_installer_path()
    saved_edid = input_config.get(input_id, {}).get("edid_path", "")

    # Build candidate directories in priority order.
    # We walk one level of subdirectories for every candidate so that
    # layouts like ~/EDID/Alternate/ are included automatically.
    home = os.path.expanduser("~")
    candidate_roots = []

    # 1. ~/EDID and siblings — where users typically keep custom EDIDs
    for name in ("EDID", "edid", "Edid"):
        d = os.path.join(home, name)
        if os.path.isdir(d):
            candidate_roots.append(d)

    # 2. Magewell installer directory and its EDID subdirectory
    if installer:
        candidate_roots.append(installer)
        for sub in ("EDID", "edid", "resources", "bin"):
            d = os.path.join(installer, sub)
            if os.path.isdir(d):
                candidate_roots.append(d)

    # 3. SDK source trees — cover Magewell2TS/EDID layout
    for name in ("src", "src_BACKUP"):
        sdk_base = os.path.join(home, name, "Magewell")
        if os.path.isdir(sdk_base):
            try:
                for vendor_entry in os.scandir(sdk_base):
                    if not vendor_entry.is_dir():
                        continue
                    for sub in ("EDID", "edid", "Magewell2TS/EDID"):
                        d = os.path.join(vendor_entry.path, sub)
                        if os.path.isdir(d):
                            candidate_roots.append(d)
            except PermissionError:
                pass

    # 4. Common system install locations
    for d in ("/lib/firmware/magewell", "/usr/share/magewell",
              "/usr/local/share/magewell", "/opt/magewell"):
        if os.path.isdir(d):
            candidate_roots.append(d)

    # Expand each root: include the root itself and any immediate subdirectories
    search_dirs = []
    for root in candidate_roots:
        if root not in search_dirs:
            search_dirs.append(root)
        try:
            for entry in os.scandir(root):
                if entry.is_dir() and entry.path not in search_dirs:
                    search_dirs.append(entry.path)
        except PermissionError:
            pass

    found = {}  # path -> name, deduplicated
    for d in search_dirs:
        try:
            for entry in sorted(os.scandir(d), key=lambda e: e.name.lower()):
                if entry.is_file() and entry.name.lower().endswith(".bin"):
                    if entry.path not in found:
                        found[entry.path] = entry.name
        except PermissionError:
            pass

    bins = [{"path": p, "name": n} for p, n in found.items()]
    return JSONResponse({
        "ok":         True,
        "bins":       bins,
        "saved_edid": saved_edid,
    })


@app.get("/input/{input_id:path}/edid/read")
async def edid_read(input_id: str):
    """Read the current EDID from the capture input and return decoded info.

    Runs:  magewell2ts -r <tmpfile> -b <board> -i <channel>
    Then attempts to decode it with edid-decode (if installed), falling
    back to a hex dump so there is always something useful to display.
    """
    async with input_config_lock:
        cfg = input_config.get(input_id, {})

    if cfg.get("driver") != "magewell":
        return JSONResponse({"ok": False, "error": "Not a Magewell input"}, status_code=400)

    board   = cfg.get("board",   0)
    channel = cfg.get("channel", 1)

    loop = asyncio.get_running_loop()
    try:
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
            tmp_path = tmp.name

        read_result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["magewell2ts", "-r", tmp_path, "-b", str(board), "-i", str(channel)],
                capture_output=True, text=True, timeout=15,
            )
        )

        if read_result.returncode != 0 or not os.path.getsize(tmp_path):
            output = (read_result.stdout + read_result.stderr).strip()
            return JSONResponse({
                "ok":    False,
                "error": f"magewell2ts -r failed: {output or 'no output'}",
            })

        # Try edid-decode first, then read-edid, then fall back to hex
        decoded = ""
        for cmd in (["edid-decode", tmp_path], ["parse-edid"]):
            try:
                kwargs = {"input": open(tmp_path, "rb").read()} if cmd[0] == "parse-edid" else {}
                r = await loop.run_in_executor(
                    None,
                    lambda c=cmd, kw=kwargs: subprocess.run(
                        c, capture_output=True, text=True, timeout=10, **kw
                    )
                )
                if r.returncode == 0 and r.stdout.strip():
                    decoded = r.stdout.strip()
                    break
            except (FileNotFoundError, Exception):
                pass

        if not decoded:
            # Hex dump fallback — always works
            raw = open(tmp_path, "rb").read()
            lines = []
            for off in range(0, len(raw), 16):
                chunk = raw[off:off + 16]
                hex_part = " ".join(f"{b:02x}" for b in chunk)
                lines.append(f"{off:04x}:  {hex_part}")
            decoded = "\n".join(lines)

        # Pull file size for info
        size = os.path.getsize(tmp_path)
        return JSONResponse({
            "ok":      True,
            "decoded": decoded,
            "size":    size,
        })

    except subprocess.TimeoutExpired:
        return JSONResponse({"ok": False, "error": "magewell2ts timed out reading EDID"}, status_code=504)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@app.post("/input/{input_id:path}/edid/write")
async def edid_write(input_id: str, edid_path: str = Form(...)):
    """Write an EDID .bin file to the capture input.

    Runs:  magewell2ts -w <edid_path> -b <board> -i <channel>

    Reminder: the written EDID does not survive a reboot.  The saved
    edid_path in input_config.json is used to re-apply it automatically
    each time the capture process starts (via _build_capture_cmd).
    """
    edid_path = edid_path.strip()
    if not edid_path:
        return JSONResponse({"ok": False, "error": "No EDID path provided"}, status_code=400)
    if not os.path.isfile(edid_path):
        return JSONResponse({"ok": False, "error": f"File not found: {edid_path}"}, status_code=400)
    if not edid_path.lower().endswith(".bin"):
        return JSONResponse({"ok": False, "error": "EDID file must be a .bin file"}, status_code=400)

    async with input_config_lock:
        cfg = input_config.get(input_id, {})

    if cfg.get("driver") != "magewell":
        return JSONResponse({"ok": False, "error": "Not a Magewell input"}, status_code=400)

    board   = cfg.get("board",   0)
    channel = cfg.get("channel", 1)

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["magewell2ts", "-w", edid_path, "-b", str(board), "-i", str(channel)],
                capture_output=True, text=True, timeout=15,
            )
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            return JSONResponse({
                "ok":    False,
                "error": f"magewell2ts -w failed (code {result.returncode}): {output}",
            })
        log.info("EDID written to input %s: %s", input_id, edid_path)
        return JSONResponse({"ok": True, "output": output})
    except subprocess.TimeoutExpired:
        return JSONResponse({"ok": False, "error": "magewell2ts timed out writing EDID"}, status_code=504)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Route — set remote device (ADB or Roku)
# ---------------------------------------------------------------------------
@app.post("/input/{input_id:path}/set_remote")
async def set_remote(
    input_id:    str,
    remote_type: str = Form("none"),   # "none" | "adb" | "roku"
    remote_ip:   str = Form(""),
):
    remote_type = remote_type.strip().lower()
    remote_ip   = remote_ip.strip()
    if remote_type not in ("none", "adb", "roku"):
        return JSONResponse({"ok": False, "error": "remote_type must be none, adb, or roku"}, status_code=400)

    async with input_config_lock:
        if input_id not in INPUT_IDS:
            return JSONResponse({"ok": False, "error": "Input not found"}, status_code=404)
        input_config[input_id]["remote_type"] = remote_type
        input_config[input_id]["remote_ip"]   = remote_ip
        # Keep legacy adb_ip in sync for backwards compat
        input_config[input_id]["adb_ip"] = remote_ip if remote_type == "adb" else ""
        _save_config(input_config, INPUT_IDS, HIDDEN_IDS)

    connected    = False
    connect_msg  = ""

    if remote_type == "adb" and remote_ip:
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["adb", "connect", remote_ip],
                    capture_output=True, text=True, timeout=8
                )
            )
            output    = (result.stdout + result.stderr).strip()
            connected = result.returncode == 0 and "connected" in output.lower()
            connect_msg = output
        except subprocess.TimeoutExpired:
            connect_msg = "adb connect timed out"
        except FileNotFoundError:
            connect_msg = "adb not found in PATH"
        except Exception as e:
            connect_msg = str(e)

    elif remote_type == "roku" and remote_ip:
        # Verify the Roku is reachable by hitting its device-info endpoint
        import urllib.request
        loop = asyncio.get_running_loop()
        try:
            def _probe():
                req = urllib.request.urlopen(
                    f"http://{remote_ip}:8060/query/device-info", timeout=4
                )
                return req.status == 200
            connected   = await loop.run_in_executor(None, _probe)
            connect_msg = f"Roku at {remote_ip} reachable" if connected else "No response from Roku"
        except Exception as e:
            connect_msg = f"Roku unreachable: {e}"

    return JSONResponse({
        "ok":          True,
        "remote_type": remote_type,
        "remote_ip":   remote_ip,
        "connected":   connected,
        "connect_msg": connect_msg,
    })


# Legacy endpoint — keep working for any existing clients
@app.post("/input/{input_id:path}/set_adb_ip")
async def set_adb_ip(input_id: str, adb_ip: str = Form(...)):
    adb_ip = adb_ip.strip()
    async with input_config_lock:
        if input_id not in INPUT_IDS:
            return JSONResponse({"ok": False, "error": "Input not found"}, status_code=404)
        input_config[input_id]["adb_ip"]      = adb_ip
        input_config[input_id]["remote_type"] = "adb" if adb_ip else "none"
        input_config[input_id]["remote_ip"]   = adb_ip
        _save_config(input_config, INPUT_IDS, HIDDEN_IDS)
    connected = False
    connect_msg = ""
    if adb_ip:
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["adb", "connect", adb_ip],
                    capture_output=True, text=True, timeout=8
                )
            )
            output = (result.stdout + result.stderr).strip()
            connected = result.returncode == 0 and "connected" in output.lower()
            connect_msg = output
        except Exception as e:
            connect_msg = str(e)
    return JSONResponse({"ok": True, "adb_ip": adb_ip, "connected": connected, "connect_msg": connect_msg})


# ---------------------------------------------------------------------------
# Route — remote keypress (ADB or Roku)
# ---------------------------------------------------------------------------
@app.post("/input/{input_id:path}/remote_key")
async def remote_key(input_id: str, key: str = Form(...)):
    async with input_config_lock:
        cfg         = input_config.get(input_id, {})
        remote_type = cfg.get("remote_type", "adb" if cfg.get("adb_ip") else "none")
        remote_ip   = cfg.get("remote_ip", cfg.get("adb_ip", "")).strip()

    if not remote_ip:
        return JSONResponse({"ok": False, "error": "No remote device configured"}, status_code=400)

    loop = asyncio.get_running_loop()

    if remote_type == "roku":
        roku_key = ROKU_KEYS.get(key)
        if not roku_key:
            return JSONResponse({"ok": False, "error": f"Unknown key: {key}"}, status_code=400)
        import urllib.request
        try:
            def _press():
                req = urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://{remote_ip}:8060/keypress/{roku_key}",
                        method="POST", data=b"",
                    ),
                    timeout=3,
                )
                return req.status == 200
            ok = await loop.run_in_executor(None, _press)
            return JSONResponse({"ok": ok, "key": key, "roku_key": roku_key})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=504)

    else:
        # ADB path
        keycode = ADB_KEYCODES.get(key)
        if keycode is None:
            return JSONResponse({"ok": False, "error": f"Unknown key: {key}"}, status_code=400)
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["adb", "-s", remote_ip, "shell", "input", "keyevent", str(keycode)],
                    capture_output=True, text=True, timeout=5
                )
            )
            ok = result.returncode == 0
            return JSONResponse({"ok": ok, "key": key, "keycode": keycode,
                                 "error": result.stderr.strip() if not ok else ""})
        except subprocess.TimeoutExpired:
            return JSONResponse({"ok": False, "error": "ADB command timed out"}, status_code=504)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# Legacy ADB keypress endpoint
@app.post("/input/{input_id:path}/adb_key")
async def adb_key(input_id: str, key: str = Form(...)):
    return await remote_key(input_id, key)


# ---------------------------------------------------------------------------
# Routes — per-user recording directory preference
# ---------------------------------------------------------------------------
@app.get("/prefs/rec_dir")
async def get_rec_dir(request: Request):
    user_key = request.client.host if request.client else "unknown"
    async with user_prefs_lock:
        rec_dir = USER_PREFS.get(user_key, {}).get("rec_dir", "")
    return JSONResponse({"ok": True, "rec_dir": rec_dir})


@app.post("/prefs/rec_dir")
async def set_rec_dir(request: Request, rec_dir: str = Form(...)):
    user_key = request.client.host if request.client else "unknown"
    rec_dir = rec_dir.strip()
    async with user_prefs_lock:
        USER_PREFS.setdefault(user_key, {})["rec_dir"] = rec_dir
        _save_config(input_config, INPUT_IDS, HIDDEN_IDS, USER_PREFS)
    return JSONResponse({"ok": True, "rec_dir": rec_dir})


# ---------------------------------------------------------------------------
# Route — list all hardware inputs
# ---------------------------------------------------------------------------
@app.get("/inputs/list")
async def list_inputs():
    loop = asyncio.get_running_loop()
    mw_entries = await loop.run_in_executor(None, _run_all_hardware)

    async with input_config_lock:
        active_set = set(INPUT_IDS)
        hidden_set = set(HIDDEN_IDS)
        cfg_snap   = {k: v for k, v in input_config.items()}

    hw_by_key = {}
    for e in mw_entries:
        key = _input_key(e["board"], e["input"], e["channel"])
        hw_by_key[key] = e

    rows = []
    seen_keys = set()
    for key, e in hw_by_key.items():
        saved = cfg_snap.get(key, {}
        )
        rows.append({
            "key":     key,
            "label":   _label(key),
            "driver":  e.get("driver", "magewell"),
            "signal":  e["signal"],
            "desc":    e.get("desc", ""),
            "active":  key in active_set,
            "hidden":  key in hidden_set,
            "q":       saved.get("q", 25),
            "encoder": saved.get("encoder", AVAILABLE_ENCODERS[0]),
        })
        seen_keys.add(key)
    for key in INPUT_IDS:
        if key not in seen_keys:
            saved = cfg_snap.get(key, {})
            rows.append({
                "key":     key,
                "label":   _label(key),
                "driver":  saved.get("driver", "magewell"),
                "signal":  "UNKNOWN",
                "desc":    "",
                "active":  True,
                "hidden":  False,
                "q":       saved.get("q", 25),
                "encoder": saved.get("encoder", AVAILABLE_ENCODERS[0]),
            })
            seen_keys.add(key)
    # Always include hidden inputs so the UI can offer to restore them,
    # even if they weren't discovered by the hardware scan this time.
    for key in hidden_set:
        if key not in seen_keys:
            saved = cfg_snap.get(key, {})
            rows.append({
                "key":     key,
                "label":   _label(key),
                "driver":  saved.get("driver", "magewell"),
                "signal":  "UNKNOWN",
                "desc":    "",
                "active":  False,
                "hidden":  True,
                "q":       saved.get("q", 25),
                "encoder": saved.get("encoder", AVAILABLE_ENCODERS[0]),
            })
            seen_keys.add(key)

    rows.sort(key=lambda r: (0 if r["driver"] == "magewell" else 1, r["key"]))
    return JSONResponse({
        "ok":               True,
        "inputs":           rows,
        "hw_found":         len(hw_by_key) > 0,
        "available_encoders": [
            {"value": e, "label": ENCODER_LABELS.get(e, e)}
            for e in AVAILABLE_ENCODERS
        ],
    })


# ---------------------------------------------------------------------------
# Route — apply input visibility changes
# ---------------------------------------------------------------------------
@app.post("/inputs/apply")
async def apply_inputs(request: Request):
    body = await request.json()
    new_active = [str(k) for k in body.get("active", [])]
    new_hidden = set(str(k) for k in body.get("hidden", []))

    if not new_active:
        return JSONResponse({"ok": False, "error": "Must keep at least one input active"}, status_code=400)

    async with input_config_lock:
        saved_q    = {k: v["q"] for k, v in input_config.items()}
        saved_adb  = {k: v.get("adb_ip", "") for k, v in input_config.items()}
        hw_entries = await asyncio.get_running_loop().run_in_executor(None, _run_list)
        hw_by_key  = {_input_key(e["board"], e["input"], e["channel"]): e for e in hw_entries}

        for key in new_active:
            if key in hw_by_key:
                # Hardware scan is authoritative for board/input/signal/desc
                e = hw_by_key[key]
                if key not in input_config:
                    input_config[key] = _build_entry(
                        key, e["board"], e["input"], e["channel"],
                        e["signal"], e["desc"], saved_q, saved_adb
                    )
                else:
                    input_config[key]["board"]   = e["board"]
                    input_config[key]["input"]   = e["input"]
                    input_config[key]["channel"] = e["channel"]
                    input_config[key]["signal"]  = e["signal"]
                    input_config[key]["desc"]    = e["desc"]
            elif key not in input_config:
                # Not in scan and not previously configured: derive from key
                try:
                    b, i = _split_key(key)
                except Exception:
                    b, i = 0, 0
                input_config[key] = _build_entry(key, b, i, 1, "UNKNOWN", "", saved_q, saved_adb)
            # key in config but not in scan: leave untouched (card may be offline)

        deactivated = [k for k in INPUT_IDS if k not in new_active]

    for key in deactivated:
        async with inputs_lock:
            if key in active_inputs:
                await _kill_and_reap(active_inputs[key]["process"], label=f"input {key}")
                del active_inputs[key]
        await stop_hls(key)

    async with input_config_lock:
        INPUT_IDS.clear()
        INPUT_IDS.extend(_sort_ids(new_active))
        HIDDEN_IDS.clear()
        HIDDEN_IDS.update(new_hidden)
        _save_config(input_config, INPUT_IDS, HIDDEN_IDS)

    return JSONResponse({"ok": True, "active": list(INPUT_IDS)})


# ---------------------------------------------------------------------------
# Route — rescan hardware
# ---------------------------------------------------------------------------
@app.post("/inputs/rescan")
async def rescan_inputs():
    loop = asyncio.get_running_loop()
    mw_entries = await loop.run_in_executor(None, _run_all_hardware)

    hw_by_key = {}
    for e in mw_entries:
        key = _input_key(e["board"], e["input"], e["channel"])
        hw_by_key[key] = e

    if not hw_by_key:
        return JSONResponse({"ok": False, "error": "No capture devices found during rescan"})

    async with input_config_lock:
        saved_entries = {k: v for k, v in input_config.items()}
        adb_map       = {k: v.get("adb_ip", "") for k, v in saved_entries.items()}

        remapped = []  # [(old_key, new_key, reason)]
        added    = []

        # ── Serial-based remapping for Magewell inputs ────────────────────
        # Build a lookup: (serial, channel_within_board) → hw entry
        # This is stable across slot changes since serial never changes.
        hw_by_serial: dict[tuple, dict] = {}
        for e in mw_entries:
            sn  = e.get("serial", "")
            ch  = e.get("channel", 1)
            if sn:
                hw_by_serial[(sn, ch)] = e

        # For each existing Magewell config entry that has a serial,
        # check if the board number has changed in the new scan.
        for old_key, cfg in list(saved_entries.items()):
            if cfg.get("driver") != "magewell":
                continue
            sn  = cfg.get("serial", "")
            ch  = cfg.get("channel", 1)
            if not sn:
                continue
            hw_e = hw_by_serial.get((sn, ch))
            if hw_e is None:
                continue  # card not present in this scan
            new_board = hw_e["board"]
            old_board = cfg.get("board", -1)
            if new_board == old_board:
                continue  # no change needed

            # Board number changed — remap the entry
            new_key = _input_key(new_board, hw_e["input"], hw_e["channel"])
            if new_key == old_key:
                continue

            log.info("Rescan: remapping %s → %s (serial %s, board %d → %d)",
                     old_key, new_key, sn, old_board, new_board)

            # Copy config to new key, update board/input/channel
            new_cfg = dict(cfg)
            new_cfg["board"]   = new_board
            new_cfg["input"]   = hw_e["input"]
            new_cfg["channel"] = hw_e["channel"]
            new_cfg["signal"]  = hw_e["signal"]

            input_config[new_key] = new_cfg
            if old_key in input_config:
                del input_config[old_key]

            # Update INPUT_IDS / HIDDEN_IDS
            if old_key in INPUT_IDS:
                idx = INPUT_IDS.index(old_key)
                INPUT_IDS[idx] = new_key
            if old_key in HIDDEN_IDS:
                HIDDEN_IDS.discard(old_key)
                HIDDEN_IDS.add(new_key)

            # Update adb_map for later use
            if old_key in adb_map:
                adb_map[new_key] = adb_map.pop(old_key)

            remapped.append((old_key, new_key, f"board {old_board}→{new_board}"))

        # Refresh saved_entries after remapping
        saved_entries = {k: v for k, v in input_config.items()}

        # ── Add genuinely new inputs ──────────────────────────────────────
        for key, e in hw_by_key.items():
            if key in INPUT_IDS or key in HIDDEN_IDS:
                # Already known — just refresh signal status
                if key in input_config:
                    input_config[key]["signal"] = e.get("signal", "NONE")
                    input_config[key].setdefault("driver", "magewell")
                continue
            input_config[key] = _build_entry(
                key, e["board"], e["input"], e["channel"],
                e["signal"], e["desc"],
                saved_entries, adb_map,
                driver="magewell", serial=e.get("serial", ""),
            )
            INPUT_IDS.append(key)
            added.append(key)

        _save_config(input_config, INPUT_IDS, HIDDEN_IDS)

    if remapped:
        log.info("Rescan: remapped %d input(s): %s",
                 len(remapped),
                 ", ".join(f"{o}→{n}" for o, n, _ in remapped))

    return JSONResponse({
        "ok":      True,
        "inputs":  list(INPUT_IDS),
        "added":   added,
        "remapped": [{"from": o, "to": n, "reason": r} for o, n, r in remapped],
    })


# ---------------------------------------------------------------------------
# Route — restore a hidden input
# ---------------------------------------------------------------------------
@app.post("/input/{input_id:path}/restore")
async def restore_input(input_id: str):
    """Remove an input from the hidden set, making it visible on the dashboard again."""
    async with input_config_lock:
        if input_id not in HIDDEN_IDS:
            return JSONResponse({"ok": False, "error": f"{input_id} is not hidden"}, status_code=400)
        HIDDEN_IDS.discard(input_id)
        # Ensure the input is in INPUT_IDS so it appears on the dashboard
        if input_id not in INPUT_IDS:
            INPUT_IDS.append(input_id)
        _save_config(input_config, INPUT_IDS, HIDDEN_IDS)
        log.info("Restored hidden input: %s", input_id)
    return JSONResponse({"ok": True})

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Route — restore a hidden input
# ---------------------------------------------------------------------------
@app.post("/input/{input_id:path}/restore")
async def restore_input(input_id: str):
    """Remove an input from the hidden set, making it visible on the dashboard again."""
    async with input_config_lock:
        if input_id not in HIDDEN_IDS:
            return JSONResponse({"ok": False, "error": f"{input_id} is not hidden"}, status_code=400)
        HIDDEN_IDS.discard(input_id)
        # Ensure the input is in INPUT_IDS so it appears on the dashboard
        if input_id not in INPUT_IDS:
            INPUT_IDS.append(input_id)
        _save_config(input_config, INPUT_IDS, HIDDEN_IDS)
        log.info("Restored hidden input: %s", input_id)
    return JSONResponse({"ok": True})
# ---------------------------------------------------------------------------
# Routes — CoolerControl integration
# ---------------------------------------------------------------------------

@app.get("/fans/config")
async def fans_get_config():
    async with _fan_config_lock:
        cfg = dict(_fan_config)
    return JSONResponse({"ok": True, "config": cfg})


@app.post("/fans/cc-config")
async def fans_save_cc_config(request: Request):
    """Save CoolerControl settings to fan_config.json."""
    body = await request.json()
    async with _fan_config_lock:
        _fan_config["coolercontrol"] = body
        _save_fan_config(_fan_config)
    log.info("CoolerControl config saved")
    return JSONResponse({"ok": True})


@app.get("/coolercontrol/modes")
async def cc_list_modes():
    """Proxy mode list from CoolerControl daemon."""
    async with _fan_config_lock:
        cc_cfg = _fan_config.get("coolercontrol", {})
    url   = cc_cfg.get("url", "http://localhost:11987").rstrip("/")
    token = cc_cfg.get("token", "")
    if not token:
        return JSONResponse({"ok": False, "error": "No token configured — save first"})
    try:
        import urllib.request as _ureq
        req = _ureq.Request(f"{url}/modes",
                            headers={"Authorization": f"Bearer {token}"})
        loop = asyncio.get_running_loop()
        def _fetch():
            with _ureq.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        data = await loop.run_in_executor(None, _fetch)
        return JSONResponse({"ok": True, "modes": data.get("modes", [])})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


@app.post("/coolercontrol/activate/{mode_uid}")
async def cc_activate(mode_uid: str):
    """Activate a CoolerControl mode and update active profile."""
    global _fan_active_profile
    async with _fan_config_lock:
        cc_cfg = _fan_config.get("coolercontrol", {})
    ok = await _cc_activate_mode(cc_cfg, mode_uid)
    if ok:
        for key in ("mode_default", "mode_recording", "mode_transcoding"):
            if cc_cfg.get(key) == mode_uid:
                _fan_active_profile = key.replace("mode_", "")
                break
    return JSONResponse({"ok": ok})


# ---------------------------------------------------------------------------
# Routes — HDHomeRun tuner emulation
# ---------------------------------------------------------------------------
# Lets Plex, Channels DVR, Emby, and Jellyfin discover Broadcast Hub as a
# network tuner and auto-build a channel list from active inputs, instead
# of the user manually entering stream URLs. This is the HTTP half of the
# HDHomeRun protocol only (discover.json / lineup.json / lineup_status.json)
# — it does not implement UDP broadcast auto-discovery (port 65001), so
# clients need to be pointed at this device's IP once (e.g. "Add tuner by
# IP" in Plex/Channels/Emby), after which it behaves like a real HDHomeRun.
#
# Purely additive: new GET routes only, reads existing input_config /
# active_inputs state, does not touch the capture/distributor pipeline.

HDHR_DEVICE_ID = "10000001"   # arbitrary but stable fake device ID
HDHR_FRIENDLY_NAME = "Broadcast Hub"
HDHR_MODEL_NUMBER  = "HDTC-2US"          # a real HDHomeRun model string
HDHR_FIRMWARE_NAME = "hdhomerun3_atsc"


def _hdhr_base_url(request: Request) -> str:
    """Build this server's externally-reachable base URL from the
    incoming request's Host header, so lineup URLs are correct regardless
    of which network interface/IP the client used to reach us."""
    host = request.headers.get("host", f"{request.url.hostname}:{request.url.port or 6502}")
    return f"http://{host}"


@app.get("/discover.json")
async def hdhr_discover(request: Request):
    base = _hdhr_base_url(request)
    async with input_config_lock:
        tuner_count = len(INPUT_IDS)
    return JSONResponse({
        "FriendlyName":    HDHR_FRIENDLY_NAME,
        "ModelNumber":     HDHR_MODEL_NUMBER,
        "FirmwareName":    HDHR_FIRMWARE_NAME,
        "FirmwareVersion": "20240101",
        "DeviceID":        HDHR_DEVICE_ID,
        "DeviceAuth":      "broadcasthub",
        "TunerCount":      max(tuner_count, 1),
        "BaseURL":         base,
        "LineupURL":       f"{base}/lineup.json",
    })


@app.get("/lineup_status.json")
async def hdhr_lineup_status():
    return JSONResponse({
        "ScanInProgress": 0,
        "ScanPossible":   0,
        "Source":         "Cable",
        "SourceList":     ["Cable"],
    })


@app.get("/lineup.json")
async def hdhr_lineup(request: Request):
    base = _hdhr_base_url(request)
    async with input_config_lock:
        ids_snapshot = list(INPUT_IDS)
        cfg_snapshot = {k: dict(v) for k, v in input_config.items() if k in ids_snapshot}

    channels = []
    for idx, key in enumerate(_sort_ids(ids_snapshot), start=1):
        cfg = cfg_snapshot.get(key, {})
        guide_number = str(cfg.get("hdhr_channel_number") or idx)
        guide_name   = cfg.get("hdhr_channel_name") or _label(key)
        channels.append({
            "GuideNumber": guide_number,
            "GuideName":   guide_name,
            "URL":         f"{base}/stream/{key}",
        })
    return JSONResponse(channels)


@app.get("/lineup.xml")
async def hdhr_lineup_xml(request: Request):
    """Some clients request the XML form of the lineup instead of JSON."""
    base = _hdhr_base_url(request)
    async with input_config_lock:
        ids_snapshot = list(INPUT_IDS)
        cfg_snapshot = {k: dict(v) for k, v in input_config.items() if k in ids_snapshot}

    import xml.sax.saxutils as _sax
    rows = []
    for idx, key in enumerate(_sort_ids(ids_snapshot), start=1):
        cfg = cfg_snapshot.get(key, {})
        guide_number = str(cfg.get("hdhr_channel_number") or idx)
        guide_name   = cfg.get("hdhr_channel_name") or _label(key)
        rows.append(
            "  <Program>\n"
            f"    <GuideNumber>{_sax.escape(guide_number)}</GuideNumber>\n"
            f"    <GuideName>{_sax.escape(guide_name)}</GuideName>\n"
            f"    <URL>{_sax.escape(base)}/stream/{_sax.escape(key)}</URL>\n"
            "  </Program>"
        )
    xml_body = "<Lineup>\n" + "\n".join(rows) + "\n</Lineup>"
    return Response(content=xml_body, media_type="application/xml")


# ---------------------------------------------------------------------------
# UPnP / DLNA MediaServer — makes Broadcast Hub appear under "Universal
# Plug'n'Play" in VLC and other DLNA browsers, with a "Channels" folder
# listing each active input, the same way a real HDHomeRun's built-in DLNA
# server does. This is a *separate* discovery mechanism from the HDHomeRun
# HTTP endpoints above — those are for Plex/Channels/Emby/Jellyfin, this
# is for generic DLNA clients like VLC's "Universal Plug'n'Play" browser.
#
# Three pieces, all purely additive (new UDP listener + new HTTP routes,
# nothing in the capture/distributor pipeline is touched):
#   1. SSDP: a UDP multicast listener/responder on 239.255.255.250:1900
#      that answers M-SEARCH requests and periodically announces itself.
#   2. Device description XML: declares this as a MediaServer:1 device
#      with ContentDirectory and ConnectionManager services.
#   3. A minimal ContentDirectory SOAP "Browse" implementation that
#      returns a root "Channels" container, and inside it one item per
#      active input pointing at the existing /stream/{id} URL.
# ---------------------------------------------------------------------------

UPNP_UUID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upnp_uuid.txt")
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
UPNP_HTTP_PORT = 6502   # must match the port uvicorn actually serves on


def _load_or_create_upnp_uuid() -> str:
    """Keep the device UUID stable across restarts so DLNA clients that
    cache device identity don't treat every restart as a brand-new device."""
    try:
        with open(UPNP_UUID_FILE) as f:
            existing = f.read().strip()
            if existing:
                return existing
    except FileNotFoundError:
        pass
    new_uuid = str(uuid.uuid4())
    try:
        with open(UPNP_UUID_FILE, "w") as f:
            f.write(new_uuid)
    except Exception as exc:
        log.warning("Could not persist UPnP UUID, will regenerate on restart: %s", exc)
    return new_uuid


UPNP_DEVICE_UUID = _load_or_create_upnp_uuid()


def _get_local_ip() -> str:
    """Best-effort local LAN IP for building UPnP LOCATION headers, since
    SSDP responses must point at a real, reachable address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


_UPNP_LOCAL_IP = _get_local_ip()


class _SSDPProtocol(asyncio.DatagramProtocol):
    """Answers M-SEARCH discovery requests on the SSDP multicast group."""

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        try:
            text = data.decode(errors="replace")
        except Exception:
            return
        if not text.startswith("M-SEARCH"):
            return
        if 'ssdp:discover' not in text.lower():
            return

        st_match = re.search(r"^ST:\s*(.+?)\r?$", text, re.MULTILINE | re.IGNORECASE)
        st = st_match.group(1).strip() if st_match else "ssdp:all"

        targets = [
            "upnp:rootdevice",
            f"uuid:{UPNP_DEVICE_UUID}",
            "urn:schemas-upnp-org:device:MediaServer:1",
            "urn:schemas-upnp-org:service:ContentDirectory:1",
            "urn:schemas-upnp-org:service:ConnectionManager:1",
        ]
        if st not in ("ssdp:all",) and st not in targets:
            return   # not asking about anything we advertise

        matches = targets if st == "ssdp:all" else [st]
        location = f"http://{_UPNP_LOCAL_IP}:{UPNP_HTTP_PORT}/upnp/description.xml"
        for match_st in matches:
            usn = (
                f"uuid:{UPNP_DEVICE_UUID}::{match_st}"
                if match_st != f"uuid:{UPNP_DEVICE_UUID}"
                else match_st
            )
            response = (
                "HTTP/1.1 200 OK\r\n"
                "CACHE-CONTROL: max-age=1800\r\n"
                f"LOCATION: {location}\r\n"
                "SERVER: Linux/1.0 UPnP/1.0 BroadcastHub/1.0\r\n"
                f"ST: {match_st}\r\n"
                f"USN: {usn}\r\n"
                "EXT: \r\n"
                "\r\n"
            ).encode()
            try:
                self.transport.sendto(response, addr)
            except Exception as exc:
                log.warning("SSDP response send failed: %s", exc)

    def error_received(self, exc):
        log.warning("SSDP listener error: %s", exc)


async def _start_ssdp_listener():
    """Bind a UDP socket to the SSDP multicast group and start answering
    M-SEARCH requests. Runs for the lifetime of the app as a background
    task; failures here (e.g. port 1900 already in use by another SSDP
    responder on the same machine) are logged but never fatal to the rest
    of Broadcast Hub."""
    try:
        sock_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock_.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock_.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock_.bind(("", SSDP_PORT))
        mreq = struct.pack("4sl", socket.inet_aton(SSDP_ADDR), socket.INADDR_ANY)
        sock_.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock_.setblocking(False)

        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _SSDPProtocol(), sock=sock_
        )
        log.info("SSDP: listening on %s:%d for UPnP discovery", SSDP_ADDR, SSDP_PORT)
        return transport
    except Exception as exc:
        log.warning(
            "SSDP: could not bind multicast listener (%s) — UPnP/DLNA "
            "auto-discovery (e.g. VLC's Universal Plug'n'Play list) will "
            "not work, but nothing else in Broadcast Hub is affected.", exc,
        )
        return None


async def _ssdp_announce_loop(transport):
    """Periodically multicast ssdp:alive NOTIFY announcements so passive
    listeners (like VLC's UPnP browser, which builds its device list from
    both active search and overheard announcements) pick this device up
    without needing to actively search first."""
    if transport is None:
        return
    location = f"http://{_UPNP_LOCAL_IP}:{UPNP_HTTP_PORT}/upnp/description.xml"
    targets = [
        ("upnp:rootdevice", f"uuid:{UPNP_DEVICE_UUID}::upnp:rootdevice"),
        (f"uuid:{UPNP_DEVICE_UUID}", f"uuid:{UPNP_DEVICE_UUID}"),
        ("urn:schemas-upnp-org:device:MediaServer:1",
         f"uuid:{UPNP_DEVICE_UUID}::urn:schemas-upnp-org:device:MediaServer:1"),
        ("urn:schemas-upnp-org:service:ContentDirectory:1",
         f"uuid:{UPNP_DEVICE_UUID}::urn:schemas-upnp-org:service:ContentDirectory:1"),
    ]
    while True:
        for nt, usn in targets:
            msg = (
                "NOTIFY * HTTP/1.1\r\n"
                f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
                "CACHE-CONTROL: max-age=1800\r\n"
                f"LOCATION: {location}\r\n"
                "SERVER: Linux/1.0 UPnP/1.0 BroadcastHub/1.0\r\n"
                f"NT: {nt}\r\n"
                "NTS: ssdp:alive\r\n"
                f"USN: {usn}\r\n"
                "\r\n"
            ).encode()
            try:
                transport.sendto(msg, (SSDP_ADDR, SSDP_PORT))
            except Exception as exc:
                log.warning("SSDP announce failed: %s", exc)
        await asyncio.sleep(890)   # well under the 1800s cache-control max-age


@app.get("/upnp/description.xml")
async def upnp_description(request: Request):
    base = _hdhr_base_url(request)
    xml_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <URLBase>{base}</URLBase>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>{HDHR_FRIENDLY_NAME}</friendlyName>
    <manufacturer>Broadcast Hub</manufacturer>
    <modelName>Broadcast Hub Capture Server</modelName>
    <modelDescription>Live capture inputs exposed as DLNA channels</modelDescription>
    <UDN>uuid:{UPNP_DEVICE_UUID}</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <SCPDURL>/upnp/cd_scpd.xml</SCPDURL>
        <controlURL>/upnp/cd_control</controlURL>
        <eventSubURL>/upnp/cd_event</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/upnp/cm_scpd.xml</SCPDURL>
        <controlURL>/upnp/cm_control</controlURL>
        <eventSubURL>/upnp/cm_event</eventSubURL>
      </service>
    </serviceList>
    <presentationURL>/</presentationURL>
  </device>
</root>"""
    return Response(content=xml_body, media_type="text/xml")


@app.get("/upnp/cd_scpd.xml")
async def upnp_cd_scpd():
    xml_body = """<?xml version="1.0" encoding="UTF-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action>
      <name>Browse</name>
      <argumentList>
        <argument><name>ObjectID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
        <argument><name>BrowseFlag</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>
        <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
        <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
        <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
        <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
        <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""
    return Response(content=xml_body, media_type="text/xml")


@app.get("/upnp/cm_scpd.xml")
async def upnp_cm_scpd():
    xml_body = """<?xml version="1.0" encoding="UTF-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action>
      <name>GetProtocolInfo</name>
      <argumentList>
        <argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
        <argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""
    return Response(content=xml_body, media_type="text/xml")


@app.post("/upnp/cm_control")
async def upnp_cm_control():
    """Minimal ConnectionManager control endpoint — just enough for
    clients that call GetProtocolInfo before browsing."""
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body><u:GetProtocolInfoResponse '
        'xmlns:u="urn:schemas-upnp-org:service:ConnectionManager:1">'
        '<Source>http-get:*:video/mpeg:*</Source><Sink></Sink>'
        '</u:GetProtocolInfoResponse></s:Body></s:Envelope>'
    )
    return Response(content=body, media_type="text/xml")


def _didl_escape(text: str) -> str:
    import xml.sax.saxutils as _sax
    return _sax.escape(text or "")


@app.post("/upnp/cd_control")
async def upnp_cd_control(request: Request):
    """Minimal ContentDirectory Browse implementation.

    Structure: root (\"0\") contains one container, \"channels\" (\"1\").
    Browsing \"1\" returns one item per active input, each pointing at
    the existing /stream/{id} URL — no new streaming logic, just a DLNA
    listing wrapped around what Broadcast Hub already serves.
    """
    raw = await request.body()
    text = raw.decode(errors="replace")

    m = re.search(r"<ObjectID>(.*?)</ObjectID>", text, re.DOTALL)
    object_id = m.group(1).strip() if m else "0"
    m = re.search(r"<BrowseFlag>(.*?)</BrowseFlag>", text, re.DOTALL)
    browse_flag = m.group(1).strip() if m else "BrowseDirectChildren"

    base = _hdhr_base_url(request)

    async with input_config_lock:
        ids_snapshot = list(INPUT_IDS)
        cfg_snapshot = {k: dict(v) for k, v in input_config.items() if k in ids_snapshot}
    sorted_ids = _sort_ids(ids_snapshot)

    didl_items = []
    if object_id == "0":
        # Root: one child container, "Channels"
        didl_items.append(
            f'<container id="channels" parentID="0" restricted="1" childCount="{len(sorted_ids)}">'
            f'<dc:title>Channels</dc:title><upnp:class>object.container</upnp:class></container>'
        )
        total_matches = 1
    else:
        # "channels" (or anything else): list every active input as a playable item
        for idx, key in enumerate(sorted_ids, start=1):
            cfg = cfg_snapshot.get(key, {})
            title = cfg.get("hdhr_channel_name") or _label(key)
            stream_url = f"{base}/stream/{key}"
            didl_items.append(
                f'<item id="ch-{_didl_escape(key)}" parentID="channels" restricted="1">'
                f'<dc:title>{_didl_escape(title)}</dc:title>'
                f'<upnp:class>object.item.videoItem</upnp:class>'
                f'<res protocolInfo="http-get:*:video/mpeg:*">{_didl_escape(stream_url)}</res>'
                f'</item>'
            )
        total_matches = len(sorted_ids)

    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        + "".join(didl_items) +
        '</DIDL-Lite>'
    )

    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body><u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
        f'<Result>{_didl_escape(didl)}</Result>'
        f'<NumberReturned>{len(didl_items)}</NumberReturned>'
        f'<TotalMatches>{total_matches}</TotalMatches>'
        '<UpdateID>1</UpdateID>'
        '</u:BrowseResponse></s:Body></s:Envelope>'
    )
    return Response(content=envelope, media_type="text/xml")


# ---------------------------------------------------------------------------
# Route — system telemetry (temps + fan RPMs)
# ---------------------------------------------------------------------------

@app.get("/telemetry")
async def get_telemetry():
    """Read CPU, GPU, Arc and fan temps/RPMs from sysfs."""
    import glob as _glob

    def _read_milli(path: str):
        """Read a millidegree or millivalue sysfs file, return float or None."""
        try:
            return int(open(path).read().strip()) / 1000.0
        except Exception:
            return None

    def _read_int(path: str):
        try:
            return int(open(path).read().strip())
        except Exception:
            return None

    temps = {}

    # CPU Package (coretemp hwmon1, temp1 = Package id 0)
    cpu = _read_milli("/sys/class/hwmon/hwmon1/temp1_input")
    if cpu is not None:
        temps["cpu"] = {"label": "CPU", "value": cpu}

    # iGPU UHD 770 (i915, hwmon4)
    igpu = _read_milli("/sys/class/hwmon/hwmon4/temp1_input")
    if igpu is not None:
        temps["igpu"] = {"label": "iGPU", "value": igpu}

    # Arc A310 — find via DRM card device hwmon glob
    arc_temp = None
    for pattern in [
        "/sys/class/drm/card*/device/hwmon/hwmon*/temp1_input",
        "/sys/class/hwmon/hwmon5/temp1_input",
    ]:
        for p in _glob.glob(pattern):
            v = _read_milli(p)
            if v is not None and v > 0:
                arc_temp = v
                break
        if arc_temp is not None:
            break
    if arc_temp is not None:
        temps["arc"] = {"label": "Arc A310", "value": arc_temp}

    # Magewell input temps — from most recently parsed capture output
    async with input_config_lock:
        mw_inputs = {k: dict(v) for k, v in input_config.items()
                     if v.get("driver") == "magewell"}
    async with inputs_lock:
        live = dict(active_inputs)
    for iid in live:
        t = live[iid].get("temperature")
        if t is not None:
            label = mw_inputs.get(iid, {}).get("label", iid)
            temps[f"mw_{iid}"] = {"label": f"MW {iid}", "value": t}

    # Fan RPMs from nct6798 (hwmon2)
    fans = []
    for n in range(1, 8):
        rpm_path = f"/sys/class/hwmon/hwmon2/fan{n}_input"
        rpm = _read_int(rpm_path)
        if rpm is not None:
            fans.append({"id": f"fan{n}", "label": f"Fan {n}", "rpm": rpm})

    return JSONResponse({"ok": True, "temps": temps, "fans": fans})


# ---------------------------------------------------------------------------
# Route — SSE stats
# ---------------------------------------------------------------------------
@app.get("/api/stats")
async def stats_sse(request: Request):
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            try:
                now = time.time()
                async with inputs_lock:
                    inputs_data = {}
                    for i, v in active_inputs.items():
                        viewer_list = [
                            {"ip": vw["ip"], "elapsed": int(now - vw["connected_at"])}
                            for vw in v.get("viewers", [])
                        ]
                        inputs_data[i] = {
                            "live":        True,
                            "viewers":     v.get("viewer_count", 0),
                            "viewer_list": viewer_list,
                            "cpu":         f"{v['stats']['cpu']:.1f}",
                            "mem":         f"{v['stats']['mem']:.1f}",
                        }
                async with hls_lock:
                    hls_ids = list(active_hls.keys())
                    # Prune stale HLS clients and merge their counts into inputs_data
                    for i, hls_entry in active_hls.items():
                        clients = hls_entry.get("hls_clients", {})
                        # Drop clients that haven't polled the playlist recently
                        active_clients = {
                            ip: ts for ip, ts in clients.items()
                            if now - ts <= HLS_CLIENT_TIMEOUT
                        }
                        hls_entry["hls_clients"] = active_clients
                        hls_entry["viewers"] = len(active_clients)
                        hls_viewer_list = [
                            {"ip": ip, "elapsed": int(now - ts)}
                            for ip, ts in active_clients.items()
                        ]
                        if i in inputs_data:
                            # Stream is also in active_inputs (ffmpeg connects as a direct
                            # viewer — don't double-count it, just add real HLS clients)
                            inputs_data[i]["viewers"] += len(active_clients)
                            inputs_data[i]["viewer_list"] += hls_viewer_list
                        else:
                            # HLS-only: underlying /stream/ may have gone idle; still live
                            inputs_data[i] = {
                                "live":        True,
                                "viewers":     len(active_clients),
                                "viewer_list": hls_viewer_list,
                                "cpu":         "0.0",
                                "mem":         "0.0",
                            }
                async with records_lock:
                    recs_data = [
                        {"id": rid, "elapsed": int(time.time() - r["started_at"]),
                         "duration": r["duration"]}
                        for rid, r in active_records.items()
                    ]
                async with input_config_lock:
                    cfg_snapshot  = {i: input_config[i]["q"] for i in INPUT_IDS if i in input_config}
                    ids_snapshot  = list(INPUT_IDS)
                    adb_snapshot  = {i: input_config[i].get("adb_ip", "") for i in INPUT_IDS if i in input_config}
                    meta_snapshot = {
                        i: {
                            "label":    _label(i),
                            "signal":   input_config[i].get("signal", "UNKNOWN"),
                            "desc":     input_config[i].get("desc", ""),
                            "adb_ip":   input_config[i].get("adb_ip", ""),
                            "driver":   input_config[i].get("driver", "magewell"),
                            "encoder":  input_config[i].get("encoder", AVAILABLE_ENCODERS[0]),
                            "faulted":  SHOULD_BE_LIVE.get(i, {}).get("faulted", False),
                            "restarts": SHOULD_BE_LIVE.get(i, {}).get("restart_count", 0),
                        }
                        for i in INPUT_IDS if i in input_config
                    }
                payload = {
                    "inputs":             inputs_data,
                    "hls":                hls_ids,
                    "recordings":         recs_data,
                    "q":                  cfg_snapshot,
                    "adb":                adb_snapshot,
                    "input_ids":          ids_snapshot,
                    "meta":               meta_snapshot,
                    "driver_missing":     DRIVER_MISSING,
                    "installer_path":     _get_installer_path(),
                    "available_encoders": [
                        {"value": e, "label": ENCODER_LABELS.get(e, e)}
                        for e in AVAILABLE_ENCODERS
                    ],
                    "available_audio_codecs": AVAILABLE_AUDIO_CODECS,
                    "channel_layouts":        CHANNEL_LAYOUTS,
                    "encoder_presets":        ENCODER_PRESETS,
                    "fan_profile":            _fan_active_profile,
                }
                yield f"data: {json.dumps(payload)}\n\n"
            except Exception as _sse_exc:
                log.warning("SSE stats error (non-fatal): %s", _sse_exc)
            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ---------------------------------------------------------------------------
# Routes — HLS
# ---------------------------------------------------------------------------
@app.get("/hls/{input_id:path}/index.m3u8")
async def hls_playlist(input_id: str, request: Request):
    await ensure_hls(input_id)
    playlist_path = os.path.join(HLS_DIR, input_id, "index.m3u8")
    for _ in range(20):
        if os.path.exists(playlist_path):
            break
        await asyncio.sleep(0.5)
    else:
        return Response(status_code=503, content="HLS stream not ready yet")

    # Each playlist fetch is the natural HLS heartbeat — record the client IP
    client_ip = request.client.host if request.client else "unknown"
    async with hls_lock:
        if input_id in active_hls:
            active_hls[input_id]["hls_clients"][client_ip] = time.time()

    with open(playlist_path) as f:
        content = f.read()
    return Response(content=content, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-cache, no-store",
                             "Access-Control-Allow-Origin": "*"})


@app.get("/hls/{input_id:path}/{segment}")
async def hls_segment(input_id: str, segment: str):
    if not segment.endswith(".ts"):
        return Response(status_code=400)
    seg_path = os.path.join(HLS_DIR, input_id, segment)
    if not os.path.exists(seg_path):
        return Response(status_code=404)
    with open(seg_path, "rb") as f:
        data = f.read()
    return Response(content=data, media_type="video/mp2t",
                    headers={"Cache-Control": "max-age=10",
                             "Access-Control-Allow-Origin": "*"})


@app.post("/hls/stop/{input_id:path}")
async def hls_stop(input_id: str):
    await stop_hls(input_id)
    return RedirectResponse(url="/", status_code=303)

# ---------------------------------------------------------------------------
# Mobile page
# ---------------------------------------------------------------------------
@app.get("/mobile", response_class=HTMLResponse)
async def mobile():
    async with input_config_lock:
        all_ids = list(INPUT_IDS)
    async with inputs_lock:
        live_ids = list(active_inputs.keys())
    async with hls_lock:
        hls_ids = list(active_hls.keys())
    return render_mobile(all_ids, live_ids, hls_ids)


# ---------------------------------------------------------------------------
# Multiview page
# ---------------------------------------------------------------------------
@app.get("/multiview", response_class=HTMLResponse)
async def multiview():
    async with input_config_lock:
        all_ids = list(INPUT_IDS)
        cfg     = dict(input_config)
    async with inputs_lock:
        live_ids = list(active_inputs.keys())
    return render_multiview(all_ids, live_ids, cfg)


@app.get("/multiview-feed/{input_id:path}")
async def multiview_feed(input_id: str, request: Request):
    """Per-cell multiview stream. One ffmpeg per connection, killed on disconnect."""
    return StreamingResponse(
        _mv_stream_gen(input_id, request),
        media_type="video/mp2t"
    )


@app.post("/multiview-stop/{input_id:path}")
async def multiview_stop(input_id: str):
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Routes — recording
# ---------------------------------------------------------------------------
@app.post("/record/start")
async def record_start(
    input_id:    str = Form(...),
    rec_dir:     str = Form(""),
    programme:   str = Form(""),
    output_path: str = Form(""),   # legacy fallback if sent directly
    fmt:         str = Form(...),
    duration:    int = Form(0),
    label:       str = Form(""),
    adb_home:    str = Form("0"),
):
    if fmt not in FORMAT_EXT:
        return Response(status_code=400, content="Invalid format")

    _, ext = FORMAT_EXT[fmt]

    # Build output path from directory + programme name if provided
    if rec_dir or programme:
        directory = rec_dir.strip().rstrip("/") or "/recordings"
        raw_name  = programme.strip() or label.strip() or "recording"
        # Slugify: keep alphanum, spaces→underscores, strip the rest
        safe_name = re.sub(r"[^\w\s-]", "", raw_name)
        safe_name = re.sub(r"\s+", "_", safe_name).strip("_")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        filename  = f"{safe_name}_{timestamp}.{ext}"
        output_path = f"{directory}/{filename}"
    elif not output_path:
        return Response(status_code=400, content="No output path provided")

    try:
        output_path = _safe_output_path(output_path)
    except ValueError as exc:
        return Response(status_code=400, content=str(exc))

    ok, free = _check_disk_space(output_path)
    if not ok:
        free_mb = free // (1024 * 1024)
        log.warning("Recording rejected — insufficient disk space: %d MB free at %s", free_mb, output_path)
        return Response(
            status_code=507,
            content=f"Insufficient disk space: only {free_mb} MB free "
                    f"(minimum {MINIMUM_FREE_BYTES // (1024 * 1024)} MB required).",
        )

    display_label = label or programme or output_path.split("/")[-1]
    do_adb_home   = adb_home in ("1", "true", "on")

    record_id = str(uuid.uuid4())[:8]
    async with records_lock:
        active_records[record_id] = {
            "input_id": input_id, "output_path": output_path,
            "fmt": fmt, "duration": duration,
            "started_at": time.time(), "process": None,
            "label": display_label,
        }
    # Persist the adb_home preference so it's restored next time the modal opens
    async with input_config_lock:
        if input_id in input_config:
            input_config[input_id]["adb_home"] = do_adb_home
            _save_config(input_config, INPUT_IDS, HIDDEN_IDS)
    asyncio.create_task(
        recording_worker(record_id, input_id, output_path, fmt, duration, do_adb_home)
    )
    return RedirectResponse(url="/", status_code=303)


@app.post("/record/stop/{record_id}")
async def record_stop(record_id: str):
    async with records_lock:
        if record_id in active_records:
            try: active_records[record_id]["process"].kill()
            except Exception: pass
            del active_records[record_id]
    return RedirectResponse(url="/", status_code=303)


@app.post("/schedule/add")
async def schedule_add(
    input_id:    str = Form(...),
    output_path: str = Form(...),
    fmt:         str = Form(...),
    start_time:  str = Form(...),
    duration:    int = Form(...),
    label:       str = Form(""),
):
    if fmt not in FORMAT_EXT:
        return Response(status_code=400, content="Invalid format")
    try:
        start_ts = datetime.fromisoformat(start_time).timestamp()
    except ValueError:
        return Response(status_code=400, content="Invalid start time")
    if start_ts < time.time():
        return Response(status_code=400, content="Start time is in the past")
    try:
        output_path = _safe_output_path(output_path)
    except ValueError as exc:
        return Response(status_code=400, content=str(exc))
    job_id = str(uuid.uuid4())[:8]
    async with schedule_lock:
        scheduled_jobs[job_id] = {
            "input_id": input_id, "output_path": output_path,
            "fmt": fmt, "start_ts": start_ts, "duration": duration, "label": label,
        }
    return RedirectResponse(url="/", status_code=303)


@app.post("/schedule/cancel/{job_id}")
async def schedule_cancel(job_id: str):
    async with schedule_lock:
        scheduled_jobs.pop(job_id, None)
    return RedirectResponse(url="/", status_code=303)

# ---------------------------------------------------------------------------
# Routes — misc
# ---------------------------------------------------------------------------
@app.get("/play/{input_id:path}")
async def play_vlc(input_id: str, request: Request):
    lbl     = _label(input_id)
    content = f"#EXTM3U\n#EXTINF:-1,{lbl}\n{str(request.base_url).rstrip('/')}/stream/{input_id}"
    return Response(content=content, media_type="application/x-mpegurl",
                    headers={"Content-Disposition": f"attachment; filename=stream_{input_id}.m3u"})


# ---------------------------------------------------------------------------
# Routes — filesystem browser (used by driver path picker)
# ---------------------------------------------------------------------------

@app.get("/admin/browse")
async def browse_filesystem(path: str = "/"):
    """Return directory listing for the folder browser UI.

    Returns:
      {
        "path":    "/current/absolute/path",
        "parent":  "/parent/path"  or null if at root,
        "dirs":    [{"name": str, "has_install_sh": bool}, ...],
        "has_install_sh": bool   # true if install.sh is in this dir
      }
    """
    # Normalise and clamp to absolute path
    try:
        resolved = os.path.realpath(os.path.normpath(path))
    except Exception:
        resolved = "/"

    if not os.path.isdir(resolved):
        resolved = os.path.dirname(resolved)
    if not os.path.isdir(resolved):
        resolved = "/"

    parent = os.path.dirname(resolved) if resolved != "/" else None

    dirs = []
    try:
        for entry in sorted(os.scandir(resolved), key=lambda e: e.name.lower()):
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name.startswith("."):
                continue
            try:
                has_sh = os.path.isfile(os.path.join(entry.path, "install.sh"))
                dirs.append({"name": entry.name, "path": entry.path, "has_install_sh": has_sh})
            except PermissionError:
                pass
    except PermissionError:
        pass

    has_install_sh = os.path.isfile(os.path.join(resolved, "install.sh"))

    return JSONResponse({
        "path":           resolved,
        "parent":         parent,
        "dirs":           dirs,
        "has_install_sh": has_install_sh,
    })


# ---------------------------------------------------------------------------
# Routes — Magewell driver reinstall
# ---------------------------------------------------------------------------

@app.post("/admin/driver/set-path")
async def driver_set_path(installer_path: str = Form(...)):
    """Save the Magewell installer path to config."""
    installer_path = installer_path.strip().rstrip("/")
    if installer_path and not os.path.isfile(os.path.join(installer_path, "install.sh")):
        return JSONResponse(
            {"ok": False, "error": f"install.sh not found in: {installer_path}"},
            status_code=400,
        )
    _save_installer_path(installer_path)
    log.info("Magewell installer path updated to: %s", installer_path)
    return JSONResponse({"ok": True, "path": installer_path})


@app.get("/admin/driver/reinstall-stream")
async def driver_reinstall_stream(request: Request):
    """SSE endpoint that runs install.sh and streams output line-by-line.

    The client (dashboard modal) connects here after the user confirms.
    Lines are sent as SSE 'line' events.  A final 'done' event carries
    {"ok": true/false, "reboot_required": true/false}.
    """
    global DRIVER_MISSING

    installer = _get_installer_path()
    if not installer:
        async def _no_path():
            yield "event: line\ndata: ERROR: No installer path configured.\n\n"
            yield 'event: done\ndata: {"ok":false}\n\n'
        return StreamingResponse(_no_path(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    install_script = os.path.join(installer, "install.sh")

    async def _run():
        global DRIVER_MISSING
        loop = asyncio.get_running_loop()

        yield f"event: line\ndata: Running {install_script}\n\n"
        yield f"event: line\ndata: ─────────────────────────────────\n\n"

        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", install_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=installer,
        )

        # Stream output line by line
        while True:
            if await request.is_disconnected():
                proc.kill()
                return
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=30.0)
            except asyncio.TimeoutError:
                yield "event: line\ndata: (waiting for installer…)\n\n"
                continue
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip()
            log.info("Magewell install: %s", line)
            yield f"event: line\ndata: {line}\n\n"

        returncode = await proc.wait()

        yield f"event: line\ndata: ─────────────────────────────────\n\n"

        # sudo -n exits 1 with "password is required" if sudoers rule is missing
        if returncode != 0:
            output_so_far = ""
            try:
                remaining = await asyncio.wait_for(proc.stdout.read(), timeout=2.0)
                output_so_far = remaining.decode(errors="replace")
            except Exception:
                pass
            if "password is required" in output_so_far or returncode == 1:
                yield "event: line\ndata: ✗ sudo requires a password.\n\n"
                yield "event: line\ndata: Run 'sudo ./setup.sh' once to configure the\n\n"
                yield "event: line\ndata: passwordless sudoers rule, then try again.\n\n"
                yield 'event: done\ndata: {"ok":false,"reboot_required":false}\n\n'
                return

        if returncode == 0:
            yield "event: line\ndata: install.sh completed successfully.\n\n"
            # Try to load the module immediately
            try:
                mod_result = await asyncio.create_subprocess_exec(
                    "sudo", "-n", "modprobe", "ProCapture",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                await mod_result.wait()
            except Exception:
                pass

            if _magewell_module_loaded():
                DRIVER_MISSING = False
                yield "event: line\ndata: ✓ ProCapture module loaded — no reboot needed.\n\n"
                yield 'event: done\ndata: {"ok":true,"reboot_required":false}\n\n'
            else:
                yield "event: line\ndata: Module not yet active — a reboot is required.\n\n"
                yield 'event: done\ndata: {"ok":true,"reboot_required":true}\n\n'
        else:
            yield f"event: line\ndata: ✗ install.sh exited with code {returncode}.\n\n"
            yield 'event: done\ndata: {"ok":false,"reboot_required":false}\n\n'

    return StreamingResponse(
        _run(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Routes — real-time log viewer
# ---------------------------------------------------------------------------

_LOG_PAGE_HTML = """<!DOCTYPE html>
<html data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Log — Broadcast Hub</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;900&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  /* ── Exact same theme variables as dashboard ── */
  :root, [data-theme="dark"] {
    --bg:           #090d1a;
    --bg-topbar:    rgba(9,13,26,.97);
    --surface:      #0b0f22;
    --border:       #1c2540;
    --border-hi:    #2a3560;
    --text:         #c0cce8;
    --muted:        #3a4870;
    --dim:          #1c2540;
    --accent:       #00e5ff;
    --accent-dim:   rgba(0,229,255,.18);
    --accent-bdr:   rgba(0,229,255,.4);
    --live:         #ff0066;
    --c-info:       #0070ff;
    --c-warning:    #ff8800;
    --c-error:      #ff0066;
    --c-debug:      #3a4870;
    --c-critical:   #ff0066;
  }
  [data-theme="mono"] {
    --bg:           #100e06;
    --bg-topbar:    rgba(12,10,4,.98);
    --surface:      #0c0a04;
    --border:       #2a1e08;
    --border-hi:    #3a2810;
    --text:         #e8d0a0;
    --muted:        #5a3818;
    --dim:          #2a1e08;
    --accent:       #ff6600;
    --accent-dim:   rgba(255,102,0,.18);
    --accent-bdr:   rgba(255,102,0,.45);
    --live:         #ff2200;
    --c-info:       #ffaa00;
    --c-warning:    #ff6600;
    --c-error:      #ff2200;
    --c-debug:      #5a3818;
    --c-critical:   #ff2200;
  }
  [data-theme="light"] {
    --bg:           #f3f5fa;
    --bg-topbar:    rgba(26,31,56,.98);
    --surface:      #ffffff;
    --border:       #c8cedd;
    --border-hi:    #b0b8d0;
    --text:         #1a1f38;
    --muted:        #6878a8;
    --dim:          #e4e8f4;
    --accent:       #4d9fff;
    --accent-dim:   rgba(77,159,255,.15);
    --accent-bdr:   rgba(77,159,255,.45);
    --live:         #dc2626;
    --c-info:       #4d9fff;
    --c-warning:    #d97706;
    --c-error:      #dc2626;
    --c-debug:      #6878a8;
    --c-critical:   #dc2626;
  }

  body {
    background: var(--bg); color: var(--text);
    font-family: 'Inter', sans-serif;
    display: flex; flex-direction: column; height: 100vh; overflow: hidden;
  }

  /* ── Topbar ── */
  .topbar {
    padding: 12px 20px; border-bottom: 1px solid var(--border);
    background: var(--bg-topbar); backdrop-filter: blur(14px);
    display: flex; align-items: center; gap: 14px; flex-shrink: 0; z-index: 10;
  }
  .logo { font-weight: 900; font-style: italic; font-size: 19px;
          text-transform: uppercase; color: var(--text); text-decoration: none; }
  .logo span { color: var(--accent); }
  .page-title { font-size: 10px; font-weight: 700; text-transform: uppercase;
                letter-spacing: .16em; color: var(--muted); }
  .spacer { flex: 1; }
  .sse-pill {
    display: flex; align-items: center; gap: 5px;
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .12em; color: var(--dim); transition: color .3s;
  }
  .sse-pill.live { color: var(--accent); }
  .sse-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--accent); animation: blink 1.4s ease-in-out infinite;
    display: none;
  }
  .sse-pill.live .sse-dot { display: block; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.15} }

  .btn-top {
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 10px;
    text-transform: uppercase; letter-spacing: .1em;
    padding: 6px 13px; border-radius: 4px; border: 1px solid var(--border);
    cursor: pointer; transition: opacity .15s; background: var(--surface);
    color: var(--muted);
  }
  .btn-top:hover { opacity: .8; }
  .btn-pause  { background: var(--accent-dim); color: var(--accent); border-color: var(--accent-bdr); }
  .btn-pause.paused { background: var(--dim); color: var(--muted); border-color: var(--border); }

  .theme-toggle {
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 9px;
    text-transform: uppercase; letter-spacing: .12em;
    background: var(--accent-dim); border: 1px solid var(--accent-bdr);
    color: var(--text); padding: 5px 10px; border-radius: 4px;
    cursor: pointer; white-space: nowrap;
  }
  .back-link {
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 10px;
    text-transform: uppercase; letter-spacing: .1em;
    color: var(--muted); text-decoration: none; padding: 6px 0;
    transition: color .15s;
  }
  .back-link:hover { color: var(--text); }

  .filter-row {
    display: flex; gap: 6px; align-items: center;
    padding: 8px 20px; border-bottom: 1px solid var(--border);
    background: var(--surface); flex-shrink: 0; flex-wrap: wrap;
  }
  .filter-lbl { font-size: 9px; font-weight: 700; text-transform: uppercase;
                letter-spacing: .14em; color: var(--muted); margin-right: 4px; }
  .level-btn {
    font-family: 'Inter', sans-serif; font-size: 9px; font-weight: 900;
    text-transform: uppercase; letter-spacing: .1em;
    padding: 3px 10px; border-radius: 3px; border: 1px solid transparent;
    cursor: pointer; transition: opacity .15s; opacity: .35;
  }
  .level-btn.on { opacity: 1; }
  .level-btn[data-level="DEBUG"]    { color: var(--c-debug);    border-color: var(--border); background: var(--dim); }
  .level-btn[data-level="INFO"]     { color: var(--c-info);     border-color: var(--accent-bdr); background: var(--accent-dim); }
  .level-btn[data-level="WARNING"]  { color: var(--c-warning);  border-color: rgba(255,140,0,.3); background: rgba(255,140,0,.1); }
  .level-btn[data-level="ERROR"]    { color: var(--c-error);    border-color: rgba(255,68,68,.3); background: rgba(255,68,68,.1); }
  .level-btn[data-level="CRITICAL"] { color: var(--c-critical); border-color: rgba(255,59,59,.4); background: rgba(255,59,59,.12); }
  .search-box {
    margin-left: auto; background: var(--surface); border: 1px solid var(--border);
    color: var(--text); font-family: 'Inter', sans-serif; font-size: 11px;
    padding: 4px 10px; border-radius: 4px; outline: none; width: 200px;
  }
  .search-box:focus { border-color: var(--accent); }
  .count-lbl { font-size: 9px; color: var(--muted); font-weight: 700;
               letter-spacing: .1em; text-transform: uppercase; white-space: nowrap; }

  /* ── Log pane ── */
  #log-pane {
    flex: 1; overflow-y: auto; overflow-x: hidden;
    padding: 6px 0; scroll-behavior: smooth;
  }
  .log-row {
    display: grid;
    grid-template-columns: 180px 68px 160px 1fr;
    gap: 0 10px;
    padding: 3px 20px; border-bottom: 1px solid var(--border);
    font-size: 11px; line-height: 1.55; font-family: 'Courier New', monospace;
    transition: background .1s;
  }
  .log-row:hover { background: var(--dim); }
  .log-row.hidden { display: none; }
  .col-ts    { color: var(--muted); white-space: nowrap; }
  .col-level { font-weight: 900; text-transform: uppercase; letter-spacing: .05em; white-space: nowrap; }
  .col-name  { color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .col-msg   { color: var(--text); word-break: break-word; }

  .level-DEBUG    { color: var(--c-debug); }
  .level-INFO     { color: var(--c-info); }
  .level-WARNING  { color: var(--c-warning); }
  .level-ERROR    { color: var(--c-error); }
  .level-CRITICAL { color: var(--c-critical); }

  #scroll-btn {
    position: fixed; bottom: 20px; right: 24px;
    background: var(--accent-dim); color: var(--accent);
    border: 1px solid var(--accent-bdr);
    font-family: 'Inter', sans-serif; font-weight: 900; font-size: 10px;
    text-transform: uppercase; letter-spacing: .1em;
    padding: 7px 14px; border-radius: 4px; cursor: pointer;
    display: none; transition: opacity .2s;
  }
  #scroll-btn:hover { opacity: .8; }
</style>
</head>
<body>

<div class="topbar">
  <a href="/" class="logo">Broadcast<span>Hub</span></a>
  <span class="page-title">/ Log</span>
  <div class="spacer"></div>
  <div class="sse-pill" id="sse-pill"><div class="sse-dot"></div><span id="sse-label">Connecting…</span></div>
  <button class="theme-toggle" id="theme-toggle" onclick="cycleTheme()">● Neon Ops</button>
  <button class="btn-top btn-pause" id="pause-btn" onclick="togglePause()">⏸ Pause</button>
  <button class="btn-top" onclick="clearLog()">✕ Clear</button>
  <a href="/" class="back-link">← Dashboard</a>
</div>

<div class="filter-row">
  <span class="filter-lbl">Level</span>
  <button class="level-btn on" data-level="DEBUG"    onclick="toggleLevel(this)">Debug</button>
  <button class="level-btn on" data-level="INFO"     onclick="toggleLevel(this)">Info</button>
  <button class="level-btn on" data-level="WARNING"  onclick="toggleLevel(this)">Warning</button>
  <button class="level-btn on" data-level="ERROR"    onclick="toggleLevel(this)">Error</button>
  <button class="level-btn on" data-level="CRITICAL" onclick="toggleLevel(this)">Critical</button>
  <input  class="search-box" id="search-box" type="text" placeholder="Filter message…" oninput="applyFilters()">
  <span class="count-lbl" id="count-lbl">0 entries</span>
</div>

<div id="log-pane"></div>
<button id="scroll-btn" onclick="scrollToBottom()">▼ Jump to Bottom</button>

<script>
  const pane      = document.getElementById('log-pane');
  const ssePill   = document.getElementById('sse-pill');
  const sseLabel  = document.getElementById('sse-label');
  const pauseBtn  = document.getElementById('pause-btn');
  const scrollBtn = document.getElementById('scroll-btn');
  const countLbl  = document.getElementById('count-lbl');

  let paused      = false;
  let autoScroll  = true;

  const THEMES       = ['dark', 'mono', 'light'];
  const THEME_LABELS = { dark: '● Neon Ops', mono: '◐ Broadcast', light: '○ Studio Pro' };

  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('bh-theme', t); } catch(e) {}
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = THEME_LABELS[t] || t;
  }

  function cycleTheme() {
    const cur  = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
    applyTheme(next);
  }

  // Restore saved theme on load — syncs with dashboard
  (function() {
    try {
      const saved = localStorage.getItem('bh-theme');
      if (saved && THEMES.includes(saved)) applyTheme(saved);
    } catch(e) {}
  })();

  // ── Scroll tracking ─────────────────────────────────────────────────────
  pane.addEventListener('scroll', () => {
    const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 60;
    autoScroll = atBottom;
    scrollBtn.style.display = atBottom ? 'none' : 'block';
  });

  function scrollToBottom() {
    pane.scrollTop = pane.scrollHeight;
    autoScroll = true;
    scrollBtn.style.display = 'none';
  }

  // ── Row builder ─────────────────────────────────────────────────────────
  function rowVisible(level, msg) {
    if (!activeLevel.has(level)) return false;
    if (searchStr && !msg.toLowerCase().includes(searchStr)) return false;
    return true;
  }

  function makeRow(e) {
    const div = document.createElement('div');
    div.className = 'log-row' + (rowVisible(e.level, e.msg) ? '' : ' hidden');
    div.dataset.level = e.level;
    div.dataset.msg   = e.msg.toLowerCase();
    div.innerHTML =
      `<span class="col-ts">${e.ts}<span style="opacity:.4">.${e.ms}</span></span>` +
      `<span class="col-level level-${e.level}">${e.level}</span>` +
      `<span class="col-name">${escHtml(e.name)}</span>` +
      `<span class="col-msg">${escHtml(e.msg)}</span>`;
    return div;
  }

  function escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function updateCount() {
    const visible = pane.querySelectorAll('.log-row:not(.hidden)').length;
    const total   = pane.querySelectorAll('.log-row').length;
    countLbl.textContent = visible === total
      ? `${total} entries`
      : `${visible} / ${total} entries`;
  }

  const MAX_ROWS = 1000;

  function addEntry(e) {
    if (paused) return;
    const row = makeRow(e);
    pane.appendChild(row);
    const rows = pane.querySelectorAll('.log-row');
    if (rows.length > MAX_ROWS) {
      for (let i = 0; i < rows.length - MAX_ROWS; i++) rows[i].remove();
    }
    updateCount();
    if (autoScroll) pane.scrollTop = pane.scrollHeight;
  }

  const activeLevel = new Set(['DEBUG','INFO','WARNING','ERROR','CRITICAL']);
  let searchStr = '';

  function toggleLevel(btn) {
    const lv = btn.dataset.level;
    if (activeLevel.has(lv)) { activeLevel.delete(lv); btn.classList.remove('on'); }
    else                      { activeLevel.add(lv);    btn.classList.add('on');    }
    applyFilters();
  }

  function applyFilters() {
    searchStr = document.getElementById('search-box').value.toLowerCase();
    for (const row of pane.querySelectorAll('.log-row')) {
      row.classList.toggle('hidden', !rowVisible(row.dataset.level, row.dataset.msg));
    }
    updateCount();
  }

  function togglePause() {
    paused = !paused;
    pauseBtn.textContent = paused ? '▶ Resume' : '⏸ Pause';
    pauseBtn.classList.toggle('paused', paused);
  }

  function clearLog() {
    pane.innerHTML = '';
    updateCount();
  }

  function connect() {
    const es = new EventSource('/logs/stream');
    es.addEventListener('history', ev => {
      for (const e of JSON.parse(ev.data)) addEntry(e);
    });
    es.addEventListener('log', ev => {
      addEntry(JSON.parse(ev.data));
    });
    es.onopen = () => {
      ssePill.classList.add('live');
      sseLabel.textContent = 'Live';
    };
    es.onerror = () => {
      ssePill.classList.remove('live');
      sseLabel.textContent = 'Reconnecting…';
      es.close();
      setTimeout(connect, 3000);
    };
  }

  connect();
</script>
</body>
</html>"""


@app.get("/logs", response_class=HTMLResponse)
async def log_viewer():
    return HTMLResponse(_LOG_PAGE_HTML)


@app.get("/logs/stream")
async def log_stream(request: Request):
    """SSE endpoint — sends buffered history then tails new log entries."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    with _log_lock:
        history = list(_log_buffer)
        _log_subscribers.append(queue)

    async def event_generator():
        try:
            # 1. Send buffered history as a single 'history' event
            yield f"event: history\ndata: {json.dumps(history)}\n\n"

            # 2. Tail new entries
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: log\ndata: {json.dumps(entry)}\n\n"
                except asyncio.TimeoutError:
                    # Send a keep-alive comment to prevent proxy timeouts
                    yield ": keepalive\n\n"
        finally:
            with _log_lock:
                try:
                    _log_subscribers.remove(queue)
                except ValueError:
                    pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Routes — authentication
# ---------------------------------------------------------------------------

@app.get("/setup", response_class=HTMLResponse)
async def get_setup():
    if _auth.is_configured():
        return RedirectResponse(url="/", status_code=302)
    return _auth.setup_page()


@app.post("/setup")
async def post_setup(password: str = Form(...), confirm: str = Form(...)):
    if _auth.is_configured():
        return RedirectResponse(url="/", status_code=302)
    if password != confirm:
        return _auth.setup_page(error="Passwords do not match.")
    try:
        _auth.setup(password)
    except ValueError as exc:
        return _auth.setup_page(error=str(exc))
    response = RedirectResponse(url="/", status_code=303)
    _auth.make_session_cookie(response)
    return response


@app.get("/login", response_class=HTMLResponse)
async def get_login(request: Request, next: str = "/"):
    if _auth.is_authenticated(request):
        return RedirectResponse(url=next, status_code=302)
    return _auth.login_page(next_url=next)


@app.post("/login")
async def post_login(
    request: Request,
    password: str = Form(...),
    next:     str = Form("/"),
):
    if not _auth.verify_password(password):
        log.warning("Auth: failed login attempt from %s",
                    request.client.host if request.client else "unknown")
        return _auth.login_page(error="Incorrect password.", next_url=next)
    log.info("Auth: successful login from %s",
             request.client.host if request.client else "unknown")
    response = RedirectResponse(url=next if next.startswith("/") else "/", status_code=303)
    _auth.make_session_cookie(response)
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    _auth.clear_session_cookie(response)
    return response


@app.get("/settings/password", response_class=HTMLResponse)
async def get_change_password():
    return _auth.change_password_page()


@app.post("/settings/password")
async def post_change_password(
    request:      Request,
    current:      str = Form(...),
    new_password: str = Form(...),
    confirm:      str = Form(...),
):
    if not _auth.verify_password(current):
        return _auth.change_password_page(error="Current password is incorrect.")
    if new_password != confirm:
        return _auth.change_password_page(error="New passwords do not match.")
    try:
        _auth.change_password(new_password)
    except ValueError as exc:
        return _auth.change_password_page(error=str(exc))
    # Re-issue a fresh cookie for this session since the secret rotated
    response = _auth.change_password_page(success=True)
    _auth.make_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Routes — health / misc
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Readiness probe for process supervisors and reverse proxies."""
    async with input_config_lock:
        n = len(INPUT_IDS)
    if n == 0:
        return JSONResponse({"status": "starting", "inputs": 0}, status_code=503)
    return JSONResponse({"status": "ok", "inputs": n})


@app.get("/debug/state")
async def debug_state():
    """Dump internal state for memory leak diagnosis."""
    import tracemalloc, gc
    gc.collect()
    import resource
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024

    async with inputs_lock:
        inputs_snapshot = {
            k: {
                "viewer_count": v.get("viewer_count", 0),
                "viewers": len(v.get("viewers", [])),
                "queue_sizes": [
                    viewer["queue"].qsize()
                    for viewer in v.get("viewers", [])
                ],
            }
            for k, v in active_inputs.items()
        }

    import threading
    return JSONResponse({
        "rss_mb":        rss_mb,
        "thread_count":  threading.active_count(),
        "active_inputs": inputs_snapshot,
        "log_buffer":    len(_log_buffer),
        "log_subscribers": len(_log_subscribers),
        "active_hls":    list(active_hls.keys()),
        "active_records": len(active_records),
    })


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

# ---------------------------------------------------------------------------
# Helpers — viewer drawer HTML
# ---------------------------------------------------------------------------

def _fmt_elapsed(s: int) -> str:
    h, m = divmod(s, 3600); m, sec = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _viewers_cell_html(input_id: str, viewer_count: int, viewer_list: list, is_live: bool) -> str:
    if not is_live:
        return '<span class="text-gray-700 font-bold text-sm">—</span>'

    safe_id = input_id.replace("-", "_")
    count_label = f'{viewer_count} Viewer{"s" if viewer_count != 1 else ""}'

    rows_html = ""
    for vw in viewer_list:
        rows_html += f"""
                  <div class="vd-row">
                    <span class="vd-ip">{vw["ip"]}</span>
                    <span class="vd-dur">{_fmt_elapsed(vw["elapsed"])}</span>
                  </div>"""

    if not rows_html:
        rows_html = '<div class="vd-empty">No direct stream clients</div>'

    return f"""
        <div>
          <span class="viewers-chip" id="vchip-{safe_id}"
                onclick="toggleViewerDrawer('{safe_id}')"
                title="Click to see connected IPs">
            <span>{count_label}</span>
            <span class="vchip-caret" id="vcaret-{safe_id}">&#9660;</span>
          </span>
          <div class="viewer-drawer" id="vdrawer-{safe_id}">
            <div class="vd-inner">
              <div class="vd-header">
                <span>IP Address</span>
                <span>Duration</span>
              </div>{rows_html}
            </div>
          </div>
        </div>"""


# ---------------------------------------------------------------------------
# Dashboard — desktop
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    async with inputs_lock:
        live_inputs = dict(active_inputs)
    async with input_config_lock:
        cfg               = dict(input_config)
        current_input_ids = list(INPUT_IDS)
    async with records_lock:
        recordings = [
            {
                "id":       rid,
                "label":    r["label"] or _label(r["input_id"]),
                "input_id": r["input_id"],
                "fmt":      r["fmt"].upper(),
                "path":     r["output_path"],
                "elapsed":  int(time.time() - r["started_at"]),
                "duration": r["duration"],
            }
            for rid, r in active_records.items()
        ]
    async with schedule_lock:
        scheduled = [
            {
                "id":       jid,
                "label":    j["label"] or _label(j["input_id"]),
                "input_id": j["input_id"],
                "fmt":      j["fmt"].upper(),
                "path":     j["output_path"],
                "start":    datetime.fromtimestamp(j["start_ts"]).strftime("%Y-%m-%d %H:%M"),
                "duration": j["duration"],
            }
            for jid, j in scheduled_jobs.items()
        ]
    async with hls_lock:
        hls_active = dict(active_hls)

    base_url = str(request.base_url).rstrip("/")

    # Pre-compute correct per-board channel labels so templates.py
    # doesn't have to re-derive them from the key string alone.
    all_keys = set(current_input_ids) | set(hls_active.keys())
    labels = {k: _label(k) for k in all_keys}

    return render_dashboard(
        live_inputs=live_inputs,
        cfg=cfg,
        current_input_ids=current_input_ids,
        recordings=recordings,
        scheduled=scheduled,
        hls_active=hls_active,
        base_url=base_url,
        should_be_live=dict(SHOULD_BE_LIVE),
        format_ext=FORMAT_EXT,
        labels=labels,
        available_encoders=[
            {"value": e, "label": ENCODER_LABELS.get(e, e)}
            for e in AVAILABLE_ENCODERS
        ],
        available_audio_codecs=AVAILABLE_AUDIO_CODECS,
        channel_layouts=CHANNEL_LAYOUTS,
        encoder_presets=ENCODER_PRESETS,
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=6502)

