import os, asyncio, subprocess, json, shutil, psutil, uuid, time, re, sys, termios, tempfile
import logging
import auth as _auth
from datetime import datetime
from fastapi import FastAPI, Request, Response, Form
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse, JSONResponse
from contextlib import asynccontextmanager
import uvicorn
from templates import render_dashboard, render_mobile

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
_ring_handler.setLevel(logging.DEBUG)
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
# Global state
# ---------------------------------------------------------------------------
active_inputs  = {}   # live hardware streams        key: "B-I" e.g. "0-0"
active_records = {}   # active recordings             key: record_id
scheduled_jobs = {}   # pending scheduled jobs        key: job_id
active_hls     = {}   # HLS segment writers           key: "B-I"
SHOULD_BE_LIVE = {}   # inputs that should stay alive key: "B-I" → {restart_count, last_restart, faulted}
inputs_lock    = asyncio.Lock()
records_lock   = asyncio.Lock()
schedule_lock  = asyncio.Lock()
hls_lock       = asyncio.Lock()
input_config_lock = asyncio.Lock()
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

# ---------------------------------------------------------------------------
# Encoder configuration
# ---------------------------------------------------------------------------

# Quality flag builder per encoder.
# For magewell2ts the -c / -q flags are handled by the binary itself;
# these are used only for the decklink/ffmpeg path.
ENCODER_QUALITY_ARGS: dict[str, callable] = {
    # H.264
    "h264_qsv":   lambda q: ["-q",    str(q)],
    "h264_nvenc": lambda q: ["-cq",   str(q)],
    "h264_amf":   lambda q: ["-qp_i", str(q), "-qp_p", str(q)],
    "h264_vaapi": lambda q: ["-qp",   str(q)],
    "libx264":    lambda q: ["-crf",  str(q)],
    # H.265 / HEVC — same quality flag conventions per vendor
    "hevc_qsv":   lambda q: ["-q",    str(q)],
    "hevc_nvenc": lambda q: ["-cq",   str(q)],
    "hevc_amf":   lambda q: ["-qp_i", str(q), "-qp_p", str(q)],
    "hevc_vaapi": lambda q: ["-qp",   str(q)],
    "libx265":    lambda q: ["-crf",  str(q)],
}

# Extra encoder-specific flags appended after quality args.
# NOTE: preset is now handled dynamically from per-input config,
# so only non-preset extras live here.
ENCODER_EXTRA_ARGS: dict[str, list] = {
    "h264_nvenc": ["-tune", "ll"],
    "hevc_nvenc": ["-tune", "ll"],
    "h264_qsv":   [],
    "hevc_qsv":   [],
    "libx264":    ["-tune", "zerolatency"],
    "libx265":    ["-tune", "zerolatency"],
    "h264_amf":   [],
    "hevc_amf":   [],
    "h264_vaapi": [],
    "hevc_vaapi": [],
}

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
}

# Encoder presets per codec family.
# These are passed as -preset to magewell2ts (-p flag) and to ffmpeg directly
# for Decklink. Only shown in UI when the selected codec supports presets.
ENCODER_PRESETS: dict[str, list[str]] = {
    "h264_qsv":   ["veryfast", "faster", "fast", "medium", "slow", "veryslow"],
    "hevc_qsv":   ["veryfast", "faster", "fast", "medium", "slow", "veryslow"],
    "h264_nvenc": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
    "hevc_nvenc": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
    "h264_amf":   ["speed", "balanced", "quality"],
    "hevc_amf":   ["speed", "balanced", "quality"],
    "h264_vaapi": [],   # no preset flag for vaapi
    "hevc_vaapi": [],
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
# Audio codec configuration (Decklink only — magewell2ts handles audio itself)
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
# (decklink input must be 2, 8, or 16).  "out_ch" is the -ac value for
# the encoder.  "layout" is the ffmpeg channel layout name used in filters.
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

    The process must write raw MPEG-TS to stdout.
    Supports two drivers:
      "magewell"  — delegates to magewell2ts (handles encoding internally)
      "decklink"  — wraps ffmpeg with the decklink input device
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

    elif driver == "decklink":
        quality_mode = cfg.get("quality_mode", "cqp")
        vf           = cfg.get("video_filter", "yadif=1,scale=1920:1080")

        if quality_mode == "cbr":
            bv = cfg.get("video_bitrate", "50M")
            quality_flags = [
                "-b:v",     bv,
                "-maxrate", bv,
                "-bufsize", cfg.get("bufsize", "200M"),
            ]
        else:
            quality_flags = ENCODER_QUALITY_ARGS.get(
                encoder, ENCODER_QUALITY_ARGS["libx264"]
            )(q)

        # ── Audio ────────────────────────────────────────────────────────────
        ch_layout_key = cfg.get("channel_layout", "stereo")
        cl            = CHANNEL_LAYOUT_MAP.get(ch_layout_key, CHANNEL_LAYOUT_MAP["stereo"])
        capture_ch    = cl["capture_ch"]   # passed to decklink -channels (must be 2/8/16)
        out_ch        = cl["out_ch"]       # passed to ffmpeg -ac
        layout_name   = cl["layout"]       # ffmpeg channel layout name

        audio_codec   = cfg.get("audio_codec", "aac")
        audio_bitrate = cfg.get("audio_bitrate", "128k")

        # Clamp out_ch to the codec's maximum
        max_ch = AUDIO_CODEC_MAX_CH.get(audio_codec, 2)
        out_ch = min(out_ch, max_ch)

        # Build audio filter chain
        # 1. Resample to 48 kHz (decklink native; ensures consistent input)
        # 2. Optionally fix the BMD LFE/Center channel swap present on some
        #    Intensity cards: CE standard is FL,FR,FC,LFE,BL,BR but BMD
        #    firmware sends FL,FR,LFE,FC,BL,BR — swapping ch 3 and 4.
        af_parts = ["aresample=48000"]
        if cfg.get("fix_lfe_swap", False) and out_ch >= 6:
            # pan filter: reassign channel positions 2 and 3 (0-indexed)
            af_parts.append(
                "pan=5.1|FL=c0|FR=c1|FC=c3|LFE=c2|BL=c4|BR=c5"
            )
        # Set the output channel layout so the encoder knows what it's getting
        if out_ch > 2:
            af_parts.append(f"aformat=channel_layouts={layout_name}")

        audio_filter = ",".join(af_parts)

        # ac3 / dca / eac3 benefit from explicit bitrate scaling with channel count
        # AAC default of 128k for stereo → scale up for surround if not overridden
        if audio_bitrate == "128k" and out_ch > 2:
            audio_bitrate = f"{64 * out_ch}k"   # 64 kbps per channel heuristic

        # Build preset flag dynamically from per-input config
        dl_preset     = cfg.get("preset", "")
        preset_flags  = ["-preset", dl_preset] if dl_preset and ENCODER_PRESETS.get(encoder) else []

        return [
            "ffmpeg",
            "-hide_banner", "-loglevel", "error",
            "-channels", str(capture_ch),
            "-format_code", cfg.get("format_code", "hp50"),
            "-f", "decklink",
            "-i", cfg.get("device_name", ""),
            "-vf", vf,
            "-c:v", encoder,
            *quality_flags,
            *preset_flags,
            *ENCODER_EXTRA_ARGS.get(encoder, []),
            "-g",   str(cfg.get("gop", 90)),
            "-c:a", audio_codec,
            "-ac",  str(out_ch),
            "-b:a", audio_bitrate,
            "-af",  audio_filter,
            "-f",   "mpegts",
            "pipe:1",
        ]

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

    Example for your hardware:
      Board 1: [1] → channel 1  (only input on DVI card)
      Board 0: [2] → channel 1  (first input on quad)
               [3] → channel 2
               [4] → channel 3
               [5] → channel 4
    """
    # First pass: collect all entries in order
    raw = []
    current_board = 0
    for line in text.splitlines():
        board_m = re.match(r"Board:\s*(\d+)", line)
        if board_m:
            current_board = int(board_m.group(1))
            continue
        input_m = re.match(r"\s*\[(\d+)\]\s+Video Signal\s+(\w+)", line)
        if input_m:
            raw.append({
                "board":  current_board,
                "input":  int(input_m.group(1)),
                "signal": input_m.group(2),
                "desc":   "",
            })
            continue
        nosig_m = re.match(r"\s*\[(\d+)\]\s+(No\s+\w+|Unlocked)", line, re.IGNORECASE)
        if nosig_m:
            raw.append({
                "board":  current_board,
                "input":  int(nosig_m.group(1)),
                "signal": "NONE",
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


def _run_decklink_list() -> list:
    """Return a list of Decklink devices visible to ffmpeg.

    Each entry:
      {"driver": "decklink", "key": "dl-N", "device_name": str, "signal": "UNKNOWN"}

    Signal status cannot be probed without opening the device, so it is
    always reported as UNKNOWN at scan time.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "decklink",
             "-list_devices", "1", "-i", "dummy"],
            capture_output=True, text=True, timeout=10,
        )
        # Device names appear in stderr as lines like:
        #   [decklink @ 0x55f1234] 'Intensity Extreme'
        devices = re.findall(r"\[decklink[^\]]*\] '(.+?)'", result.stderr)
        return [
            {
                "driver":      "decklink",
                "key":         f"dl-{i}",
                "device_name": name,
                "signal":      "UNKNOWN",
                "desc":        "",
            }
            for i, name in enumerate(devices)
        ]
    except FileNotFoundError:
        return []   # ffmpeg not installed
    except Exception as e:
        log.warning("Decklink device scan failed: %s", e)
        return []


def _run_all_hardware() -> tuple[list, list]:
    """Return (magewell_entries, decklink_entries)."""
    return _run_list(), _run_decklink_list()


def _input_key(board: int, inp: int, channel: int = None) -> str:
    # Use per-board channel number if provided, otherwise fall back to global index
    return f"{board}-{channel if channel is not None else inp}"


def _split_key(key: str):
    """Split a key into (board_or_prefix, index).

    Magewell keys: "0-1"  → (0, 1)  both ints
    Decklink keys: "dl-0" → ("dl", 0)
    """
    prefix, idx = key.split("-", 1)
    if prefix == "dl":
        return "dl", int(idx)
    return int(prefix), int(idx)


def _label(key: str, channel: int = None) -> str:
    if key.startswith("dl-"):
        idx = key.split("-", 1)[1]
        return f"Decklink · Device {idx}"
    b, i = _split_key(key)
    # Key is now board-channel, so i is already the per-board input number
    return f"Board {b} · Input {i}"


def _sort_ids(ids):
    """Sort input-ID strings: magewell keys (board-input) first, then decklink (dl-N)."""
    def sort_key(k):
        if k.startswith("dl-"):
            return (1, 0, int(k.split("-", 1)[1]))
        parts = k.split("-", 1)
        return (0, int(parts[0]), int(parts[1]))
    return sorted(ids, key=sort_key)

# ---------------------------------------------------------------------------
# Per-input configuration
# ---------------------------------------------------------------------------
CONFIG_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "input_config.json")


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
                "q":       v["q"],
                "adb_ip":  v.get("adb_ip", ""),
                "driver":  v.get("driver", "magewell"),
                "encoder": v.get("encoder", AVAILABLE_ENCODERS[0]),
            }
            driver = entry["driver"]
            if driver == "magewell":
                entry.update({
                    "board":        v.get("board", 0),
                    "input":        v.get("input", 0),
                    "channel":      v.get("channel", 1),
                    "preset":       v.get("preset", ""),
                    "lookahead":    v.get("lookahead", 35),
                    "p010":         v.get("p010", False),
                    "no_audio":     v.get("no_audio", False),
                    "vaapi_device": v.get("vaapi_device", ""),
                })
            elif driver == "decklink":
                entry.update({
                    "device_name":    v.get("device_name", ""),
                    "format_code":    v.get("format_code", "hp50"),
                    "quality_mode":   v.get("quality_mode", "cqp"),
                    "video_bitrate":  v.get("video_bitrate", "50M"),
                    "audio_bitrate":  v.get("audio_bitrate", "128k"),
                    "video_filter":   v.get("video_filter", "yadif=1,scale=1920:1080"),
                    "gop":            v.get("gop", 90),
                    "bufsize":        v.get("bufsize", "200M"),
                    "audio_codec":    v.get("audio_codec", "aac"),
                    "channel_layout": v.get("channel_layout", "stereo"),
                    "fix_lfe_swap":   v.get("fix_lfe_swap", False),
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
                  driver: str = "magewell", device_name: str = "",
                  extra: dict = None) -> dict:
    base = {
        "q":       saved.get(key, {}).get("q", 25) if isinstance(saved.get(key), dict) else saved.get(key, 25),
        "adb_ip":  saved_adb.get(key, ""),
        "driver":  driver,
        "encoder": saved.get(key, {}).get("encoder", AVAILABLE_ENCODERS[0]) if isinstance(saved.get(key), dict) else AVAILABLE_ENCODERS[0],
        "signal":  signal,
        "desc":    desc,
    }
    if driver == "magewell":
        saved_mw = saved.get(key, {}) if isinstance(saved.get(key), dict) else {}
        base.update({
            "board":        board_or_dl,
            "input":        inp,
            "channel":      channel,
            "preset":       saved_mw.get("preset",       ""),
            "lookahead":    saved_mw.get("lookahead",    35),
            "p010":         saved_mw.get("p010",         False),
            "no_audio":     saved_mw.get("no_audio",     False),
            "vaapi_device": saved_mw.get("vaapi_device", ""),
        })
    elif driver == "decklink":
        saved_entry = saved.get(key, {}) if isinstance(saved.get(key), dict) else {}
        base.update({
            "device_name":    device_name,
            "format_code":    saved_entry.get("format_code",    "hp50"),
            "quality_mode":   saved_entry.get("quality_mode",   "cqp"),
            "video_bitrate":  saved_entry.get("video_bitrate",  "50M"),
            "audio_bitrate":  saved_entry.get("audio_bitrate",  "128k"),
            "video_filter":   saved_entry.get("video_filter",   "yadif=1,scale=1920:1080"),
            "gop":            saved_entry.get("gop",            90),
            "bufsize":        saved_entry.get("bufsize",        "200M"),
            "audio_codec":    saved_entry.get("audio_codec",    "aac"),
            "channel_layout": saved_entry.get("channel_layout", "stereo"),
            "fix_lfe_swap":   saved_entry.get("fix_lfe_swap",   False),
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

    # Scan both card types
    mw_entries = _run_list()
    dl_entries = _run_decklink_list()

    hw_by_key: dict = {}
    for e in mw_entries:
        key = _input_key(e["board"], e["input"], e["channel"])
        hw_by_key[key] = e
    for e in dl_entries:
        hw_by_key[e["key"]] = e

    def _hw_or_saved(key):
        """Return best-effort config tuple for a key not in current hw scan."""
        if key in hw_by_key:
            return hw_by_key[key]
        if key in saved_entries:
            return saved_entries[key]
        # Absolute fallback
        if key.startswith("dl-"):
            return {"driver": "decklink", "device_name": "", "signal": "NONE", "desc": ""}
        try:
            b, i = int(key.split("-")[0]), int(key.split("-")[1])
        except Exception:
            b, i = 0, 0
        # i is the channel number — _input_key encodes "board-channel" in the key,
        # so preserve it here rather than hardcoding 1, which would silently
        # capture the wrong input whenever the hardware scan misses a card.
        return {"driver": "magewell", "board": b, "input": i, "channel": i, "signal": "NONE", "desc": ""}

    def _make_entry(key, hw_data):
        driver = hw_data.get("driver", "magewell")
        if driver == "decklink":
            return _build_entry(
                key, None, 0, 0,
                hw_data.get("signal", "UNKNOWN"), hw_data.get("desc", ""),
                saved_entries, saved_adb,
                driver="decklink", device_name=hw_data.get("device_name", ""),
            )
        else:
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
_save_config(input_config, INPUT_IDS, HIDDEN_IDS)

# Per-user recording directory preferences  key: IP string → rec_dir string
_raw_cfg = _load_config_file()
USER_PREFS: dict = _raw_cfg.get("__user_prefs__", {})
user_prefs_lock = asyncio.Lock()

os.makedirs(HLS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Magewell driver state
# ---------------------------------------------------------------------------

def _magewell_module_loaded() -> bool:
    """Return True if the ProCapture kernel module is currently loaded."""
    try:
        result = subprocess.run(
            ["lsmod"], capture_output=True, text=True, timeout=5
        )
        return "ProCapture" in result.stdout
    except Exception:
        return False


def _get_installer_path() -> str:
    """Return saved Magewell installer path from config, or empty string."""
    cfg = _load_config_file()
    return cfg.get("__user_prefs__", {}).get("magewell_installer", "").strip()


def _save_installer_path(path: str) -> None:
    """Persist Magewell installer path into __user_prefs__ in config."""
    cfg = _load_config_file()
    cfg.setdefault("__user_prefs__", {})["magewell_installer"] = path
    config_dir = os.path.dirname(CONFIG_FILE)
    fd, tmp = tempfile.mkstemp(dir=config_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_FILE)
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
_PUBLIC_PATHS = {"/health", "/login", "/setup", "/logout", "/favicon.ico"}
_PUBLIC_PREFIXES = ("/hls/", "/stream/")

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
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    asyncio.create_task(_drain_stderr(f"hls/{input_id}", proc))
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


async def ensure_hls(input_id: str):
    async with hls_lock:
        if input_id in active_hls:
            return
    asyncio.create_task(hls_writer(input_id))


async def stop_hls(input_id: str):
    async with hls_lock:
        entry = active_hls.get(input_id)
    if entry:
        try: entry["process"].kill()
        except Exception: pass

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
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
    try:
        yield
    finally:
        stats_task.cancel()
        watchdog_task.cancel()
        schedule_task.cancel()
        await asyncio.gather(
            stats_task, watchdog_task, schedule_task,
            return_exceptions=True,
        )

        for rec in list(active_records.values()):
            try: rec["process"].kill()
            except Exception: pass
            try: rec["process"].wait(timeout=1)
            except Exception: pass

        for entry in list(active_hls.values()):
            try: entry["process"].kill()
            except Exception: pass
            try: entry["process"].wait(timeout=1)
            except Exception: pass

        for entry in list(active_inputs.values()):
            try:
                os.killpg(os.getpgid(entry["process"].pid), 9)
            except Exception:
                pass
            try:
                entry["process"].stdout.close()
            except Exception:
                pass
            try:
                entry["process"].wait(timeout=1)
            except Exception:
                pass

        if _tty_fd is not None and _tty_state is not None:
            try:
                termios.tcsetattr(_tty_fd, termios.TCSADRAIN, _tty_state)
            except Exception:
                pass

app = FastAPI(lifespan=lifespan)
app.middleware("http")(_auth_middleware)

# ---------------------------------------------------------------------------
# Stderr drain helper
# ---------------------------------------------------------------------------
async def _drain_stderr(label: str, proc: subprocess.Popen) -> None:
    """Read stderr from *proc* line-by-line and forward to the logger.

    Must be run as an asyncio task so it never blocks the event loop.
    ffmpeg writes diagnostics to stderr; without draining, the pipe buffer
    fills up and the process stalls — especially visible on preview/HLS.
    """
    if proc.stderr is None:
        return
    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, proc.stderr.readline)
            if not line:
                break
            decoded = line.decode(errors="replace").rstrip()
            if decoded:
                log.debug("%s stderr: %s", label, decoded)
    except Exception:
        pass
    finally:
        try:
            proc.stderr.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Stream distributor
# ---------------------------------------------------------------------------
async def distributor(input_id, process):
    loop = asyncio.get_running_loop()
    idle_since = None
    try:
        while True:
            async with inputs_lock:
                if input_id not in active_inputs:
                    break
                if len(active_inputs[input_id].get("viewers", [])) == 0:
                    if idle_since is None:
                        idle_since = loop.time()
                    elif loop.time() - idle_since > 10:
                        break
                else:
                    idle_since = None
            try:
                chunk = await asyncio.wait_for(
                    loop.run_in_executor(None, process.stdout.read, 16384),
                    timeout=2.0
                )
            except asyncio.TimeoutError:
                if process.poll() is not None: break
                continue
            if not chunk: break
            async with inputs_lock:
                if input_id in active_inputs:
                    for viewer in list(active_inputs[input_id]["viewers"]):
                        try: viewer["queue"].put_nowait(chunk)
                        except asyncio.QueueFull: pass
    finally:
        exit_code = process.poll()
        log.info("Distributor: input %s process exited (code=%s)", input_id, exit_code)
        try:
            os.killpg(os.getpgid(process.pid), 9)
            process.stdout.close()
            process.wait(timeout=0.5)
        except: pass
        async with inputs_lock:
            if input_id in active_inputs:
                del active_inputs[input_id]
        log.info("Distributor: removed %s from active_inputs", input_id)

# ---------------------------------------------------------------------------
# Recording worker
# ---------------------------------------------------------------------------
ADB_HOME_DELAY = 60  # seconds after recording ends before sending Home

async def recording_worker(record_id: str, input_id: str, output_path: str,
                            fmt: str, duration: int, adb_home: bool = False):
    ff_fmt, _ = FORMAT_EXT[fmt]
    duration_args = ["-t", str(duration)] if duration > 0 else []
    proc = subprocess.Popen(
        [
            "ffmpeg", "-y",
            "-i", f"http://127.0.0.1:6502/stream/{input_id}",
            "-c:v", "copy", "-c:a", "copy",
            *duration_args, "-f", ff_fmt, output_path,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    asyncio.create_task(_drain_stderr(f"record/{record_id}", proc))
    async with records_lock:
        if record_id in active_records:
            active_records[record_id]["process"] = proc
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, proc.wait)
    async with records_lock:
        if record_id in active_records:
            del active_records[record_id]

    # Optionally send ADB Home keypress after a short delay
    if adb_home:
        async with input_config_lock:
            adb_ip = input_config.get(input_id, {}).get("adb_ip", "").strip()
        if adb_ip:
            await asyncio.sleep(ADB_HOME_DELAY)
            try:
                await loop.run_in_executor(
                    None,
                    lambda: subprocess.run(
                        ["adb", "-s", adb_ip, "shell", "input", "keyevent",
                         str(ADB_KEYCODES["home"])],
                        capture_output=True, timeout=5
                    )
                )
                log.info("ADB home sent to %s after recording %s", adb_ip, record_id)
            except Exception as e:
                log.warning("ADB home failed for %s: %s", record_id, e)

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
    queue = asyncio.Queue(maxsize=50)
    viewer = {"queue": queue, "ip": client_ip, "connected_at": time.time()}

    log.info("Stream: new connection to %s from %s", input_id, client_ip)
    async with inputs_lock:
        await _ensure_input(input_id, viewer)
        new_count = active_inputs[input_id].get("viewer_count", 0)
        log.debug("Stream: %s now has %d viewer(s)", input_id, new_count)
        if new_count == 1 and input_id not in SHOULD_BE_LIVE:
            SHOULD_BE_LIVE[input_id] = {"restart_count": 0, "last_restart": 0, "faulted": False}

    async def stream_from_queue():
        # Do NOT use request.is_disconnected() — it fires immediately on clients
        # like Channels DVR that use Connection:close or don't send a request body.
        # Instead we detect a gone client by catching GeneratorExit (the ASGI
        # framework cancels the generator when the client TCP connection drops)
        # or by a queue timeout after we know data should be flowing.
        consecutive_timeouts = 0
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=5.0)
                    consecutive_timeouts = 0
                except asyncio.TimeoutError:
                    consecutive_timeouts += 1
                    # After 3 consecutive timeouts (15s) with no data, the
                    # upstream process has probably stalled — bail out
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
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
    )
    asyncio.create_task(_drain_stderr(f"preview/{input_id}", ffmpeg_proc))

    async def stream_preview():
        loop = asyncio.get_running_loop()
        try:
            while not await request.is_disconnected():
                try:
                    chunk = await asyncio.wait_for(
                        loop.run_in_executor(None, ffmpeg_proc.stdout.read, 16384),
                        timeout=2.0
                    )
                except asyncio.TimeoutError:
                    if ffmpeg_proc.poll() is not None: break
                    continue
                if not chunk: break
                yield chunk
        finally:
            try: ffmpeg_proc.kill()
            except: pass
            try: ffmpeg_proc.stdout.close()
            except: pass
            try: ffmpeg_proc.wait(timeout=0.5)
            except: pass

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
            try:
                os.killpg(os.getpgid(active_inputs[input_id]["process"].pid), 9)
                active_inputs[input_id]["process"].wait(timeout=0.5)
                restarted = True
            except: pass
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
            try:
                os.killpg(os.getpgid(active_inputs[input_id]["process"].pid), 9)
                active_inputs[input_id]["process"].wait(timeout=0.5)
                restarted = True
            except: pass
    return JSONResponse({"ok": True, "encoder": encoder, "restarted": restarted})


# ---------------------------------------------------------------------------
# Route — set Magewell-specific config (preset, lookahead, p010, no_audio, device)
# ---------------------------------------------------------------------------
@app.post("/input/{input_id:path}/set_magewell_cfg")
async def set_magewell_cfg(
    input_id:     str,
    preset:       str  = Form(""),
    lookahead:    int  = Form(35),
    p010:         str  = Form("0"),    # checkbox sends "1"/"0"
    no_audio:     str  = Form("0"),
    vaapi_device: str  = Form(""),
):
    async with input_config_lock:
        if input_id not in INPUT_IDS:
            return JSONResponse({"ok": False, "error": "Input not found"}, status_code=404)
        if input_config[input_id].get("driver") != "magewell":
            return JSONResponse({"ok": False, "error": "Input is not a Magewell device"}, status_code=400)
        encoder = input_config[input_id].get("encoder", AVAILABLE_ENCODERS[0])
        # Validate preset if provided
        if preset and preset not in ENCODER_PRESETS.get(encoder, []):
            # Silently clear invalid preset rather than erroring — codec may have changed
            preset = ""
        try:
            safe_device = _safe_vaapi_device(vaapi_device)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        input_config[input_id].update({
            "preset":       preset.strip(),
            "lookahead":    max(0, min(lookahead, 120)),
            "p010":         p010 in ("1", "true", "on"),
            "no_audio":     no_audio in ("1", "true", "on"),
            "vaapi_device": safe_device,
        })
        _save_config(input_config, INPUT_IDS, HIDDEN_IDS)
    restarted = False
    async with inputs_lock:
        if input_id in active_inputs:
            try:
                os.killpg(os.getpgid(active_inputs[input_id]["process"].pid), 9)
                active_inputs[input_id]["process"].wait(timeout=0.5)
                restarted = True
            except: pass
    return JSONResponse({"ok": True, "restarted": restarted})
# ---------------------------------------------------------------------------
# Route — list available Decklink format codes for a specific device
# ---------------------------------------------------------------------------
@app.get("/input/{input_id:path}/decklink_formats")
async def decklink_formats(input_id: str):
    """Query ffmpeg for the format codes supported by this Decklink device.

    ffmpeg must briefly open the device to enumerate formats, so this is
    called lazily (when the user expands the Decklink config panel) rather
    than at page load time.

    Returns a list like:
      [{"code": "hp50", "label": "1080p 50", "mode": "bmdModeHD1080p50"}, ...]
    """
    async with input_config_lock:
        cfg = input_config.get(input_id, {})

    if cfg.get("driver") != "decklink":
        return JSONResponse(
            {"ok": False, "error": "Not a Decklink input"}, status_code=400
        )

    device       = cfg.get("device_name", "")
    current_code = cfg.get("format_code", "")

    if not device:
        return JSONResponse(
            {"ok": False, "error": "No device name configured"}, status_code=400
        )

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["ffmpeg", "-hide_banner", "-f", "decklink",
                 "-list_formats", "1", "-i", device],
                capture_output=True, text=True, timeout=15,
            )
        )
        # ffmpeg writes format lines to stderr, e.g.:
        #   [decklink @ 0x563f1a2b4c80]   11    hp50    1080p 50
        #   [decklink @ 0x563f1a2b4c80]   12    hp59    1080p 59.94
        #   [decklink @ 0x563f1a2b4c80]   13    hp60    1080p 60
        # Older ffmpeg versions use a slightly different format:
        #   [decklink @ ...] 	23	1080p 23.98	 (bmdModeHD1080p2398)
        # We try both patterns.
        formats = []

        # Pattern A — newer ffmpeg: index  code  label
        for m in re.finditer(
            r"\[decklink[^\]]*\]\s+\d+\s+(\w+)\s+(.+?)(?:\s*\((\w+)\))?\s*$",
            result.stderr, re.MULTILINE
        ):
            code, label, mode = m.group(1), m.group(2).strip(), m.group(3) or ""
            # Skip lines that are clearly not format codes (headers, etc.)
            if re.match(r"^[a-z0-9]+$", code):
                formats.append({"code": code, "label": label, "mode": mode})

        # Pattern B — older ffmpeg: index  label  (bmdMode...)
        if not formats:
            for m in re.finditer(
                r"\[decklink[^\]]*\]\s+\d+\s+(.+?)\s+\((\w+)\)\s*$",
                result.stderr, re.MULTILINE
            ):
                label, mode = m.group(1).strip(), m.group(2)
                # Derive a short code from the mode name where possible
                code = mode
                formats.append({"code": code, "label": label, "mode": mode})

        if not formats:
            # Return stderr so the caller can show a useful error message
            return JSONResponse({
                "ok":     False,
                "error":  "No formats found — device may not be connected or signal absent",
                "stderr": result.stderr[-800:],   # last 800 chars is usually enough
            })

        return JSONResponse({
            "ok":      True,
            "device":  device,
            "current": current_code,
            "formats": formats,
        })

    except subprocess.TimeoutExpired:
        return JSONResponse(
            {"ok": False, "error": "Timed out querying device formats (15s)"}, status_code=504
        )
    except FileNotFoundError:
        return JSONResponse(
            {"ok": False, "error": "ffmpeg not found in PATH"}, status_code=500
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/input/{input_id:path}/set_decklink_cfg")
async def set_decklink_cfg(
    input_id:       str,
    format_code:    str  = Form("hp50"),
    quality_mode:   str  = Form("cqp"),
    video_bitrate:  str  = Form("50M"),
    audio_bitrate:  str  = Form("128k"),
    video_filter:   str  = Form("yadif=1,scale=1920:1080"),
    gop:            int  = Form(90),
    audio_codec:    str  = Form("aac"),
    channel_layout: str  = Form("stereo"),
    fix_lfe_swap:   str  = Form("0"),   # "1" / "0" — checkboxes send strings
):
    if audio_codec not in AUDIO_CODEC_MAX_CH:
        return JSONResponse({"ok": False, "error": f"Unknown audio codec: {audio_codec}"}, status_code=400)
    if channel_layout not in CHANNEL_LAYOUT_MAP:
        return JSONResponse({"ok": False, "error": f"Unknown channel layout: {channel_layout}"}, status_code=400)
    try:
        safe_vf = _safe_video_filter(video_filter)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    async with input_config_lock:
        if input_id not in INPUT_IDS:
            return JSONResponse({"ok": False, "error": "Input not found"}, status_code=404)
        if input_config[input_id].get("driver") != "decklink":
            return JSONResponse({"ok": False, "error": "Input is not a Decklink device"}, status_code=400)
        input_config[input_id].update({
            "format_code":    format_code.strip(),
            "quality_mode":   quality_mode,
            "video_bitrate":  video_bitrate.strip(),
            "audio_bitrate":  audio_bitrate.strip(),
            "video_filter":   safe_vf,
            "gop":            max(1, gop),
            "audio_codec":    audio_codec,
            "channel_layout": channel_layout,
            "fix_lfe_swap":   fix_lfe_swap in ("1", "true", "on"),
        })
        _save_config(input_config, INPUT_IDS, HIDDEN_IDS)
    restarted = False
    async with inputs_lock:
        if input_id in active_inputs:
            try:
                os.killpg(os.getpgid(active_inputs[input_id]["process"].pid), 9)
                active_inputs[input_id]["process"].wait(timeout=0.5)
                restarted = True
            except: pass
    return JSONResponse({"ok": True, "restarted": restarted})

# ---------------------------------------------------------------------------
# Route — set ADB IP
# ---------------------------------------------------------------------------
@app.post("/input/{input_id:path}/set_adb_ip")
async def set_adb_ip(input_id: str, adb_ip: str = Form(...)):
    adb_ip = adb_ip.strip()
    async with input_config_lock:
        if input_id not in INPUT_IDS:
            return JSONResponse({"ok": False, "error": "Input not found"}, status_code=404)
        input_config[input_id]["adb_ip"] = adb_ip
        _save_config(input_config, INPUT_IDS, HIDDEN_IDS)

    # Attempt adb connect so keypress commands work immediately
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
        except subprocess.TimeoutExpired:
            connect_msg = "adb connect timed out"
        except FileNotFoundError:
            connect_msg = "adb not found in PATH"
        except Exception as e:
            connect_msg = str(e)

    return JSONResponse({
        "ok": True,
        "adb_ip": adb_ip,
        "connected": connected,
        "connect_msg": connect_msg,
    })

# ---------------------------------------------------------------------------
# Route — ADB keypress
# ---------------------------------------------------------------------------
@app.post("/input/{input_id:path}/adb_key")
async def adb_key(input_id: str, key: str = Form(...)):
    keycode = ADB_KEYCODES.get(key)
    if keycode is None:
        return JSONResponse({"ok": False, "error": f"Unknown key: {key}"}, status_code=400)

    async with input_config_lock:
        adb_ip = input_config.get(input_id, {}).get("adb_ip", "").strip()

    if not adb_ip:
        return JSONResponse({"ok": False, "error": "No ADB IP configured for this input"}, status_code=400)

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["adb", "-s", adb_ip, "shell", "input", "keyevent", str(keycode)],
                capture_output=True, text=True, timeout=5
            )
        )
        ok = result.returncode == 0
        return JSONResponse({"ok": ok, "key": key, "keycode": keycode,
                             "error": result.stderr.strip() if not ok else ""})
    except subprocess.TimeoutExpired:
        return JSONResponse({"ok": False, "error": "ADB command timed out"}, status_code=504)
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "adb not found in PATH"}, status_code=500)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
    mw_entries, dl_entries = await loop.run_in_executor(None, _run_all_hardware)

    async with input_config_lock:
        active_set = set(INPUT_IDS)
        hidden_set = set(HIDDEN_IDS)
        cfg_snap   = {k: v for k, v in input_config.items()}

    hw_by_key = {}
    for e in mw_entries:
        key = _input_key(e["board"], e["input"], e["channel"])
        hw_by_key[key] = e
    for e in dl_entries:
        hw_by_key[e["key"]] = e

    rows = []
    for key, e in hw_by_key.items():
        saved = cfg_snap.get(key, {})
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
    for key in INPUT_IDS:
        if key not in hw_by_key:
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
                try:
                    os.killpg(os.getpgid(active_inputs[key]["process"].pid), 9)
                    active_inputs[key]["process"].wait(timeout=0.5)
                except: pass
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
    mw_entries, dl_entries = await loop.run_in_executor(None, _run_all_hardware)

    hw_by_key = {}
    for e in mw_entries:
        key = _input_key(e["board"], e["input"], e["channel"])
        hw_by_key[key] = e
    for e in dl_entries:
        hw_by_key[e["key"]] = e

    if not hw_by_key:
        return JSONResponse({"ok": False, "error": "No capture devices found during rescan"})

    async with input_config_lock:
        saved_entries = {k: v for k, v in input_config.items()}

        added = []
        for key, e in hw_by_key.items():
            driver = e.get("driver", "magewell")
            if key not in INPUT_IDS and key not in HIDDEN_IDS:
                if driver == "decklink":
                    input_config[key] = _build_entry(
                        key, None, 0, 0,
                        e["signal"], e.get("desc", ""),
                        saved_entries, {k: v.get("adb_ip", "") for k, v in saved_entries.items()},
                        driver="decklink", device_name=e["device_name"],
                    )
                else:
                    input_config[key] = _build_entry(
                        key, e["board"], e["input"], e["channel"],
                        e["signal"], e["desc"],
                        saved_entries, {k: v.get("adb_ip", "") for k, v in saved_entries.items()},
                        driver="magewell",
                    )
                INPUT_IDS.append(key)
                added.append(key)
            elif key in INPUT_IDS:
                input_config[key]["signal"] = e["signal"]
                input_config[key].setdefault("driver", driver)

        _save_config(input_config, INPUT_IDS, HIDDEN_IDS)

    return JSONResponse({"ok": True, "inputs": list(INPUT_IDS), "added": added, "removed": []})


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
    asyncio.create_task(
        recording_worker(record_id, input_id, output_path, fmt, duration, do_adb_home)
    )
    return RedirectResponse(url="/", status_code=303)


@app.post("/record/stop/{record_id}")
async def record_stop(record_id: str):
    async with records_lock:
        if record_id in active_records:
            try: active_records[record_id]["process"].kill()
            except: pass
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
  :root {
    --bg: #080808; --surface: #0e0e0e; --border: #1e1e1e;
    --text: #e8e8e8; --muted: #444; --dim: #2a2a2a;
    --accent: #e8ff47;
    --c-info:    #4a9eff;
    --c-warning: #ff8c00;
    --c-error:   #ff4444;
    --c-debug:   #888;
    --c-critical:#ff3b3b;
  }
  body {
    background: var(--bg); color: var(--text);
    font-family: 'Inter', sans-serif;
    display: flex; flex-direction: column; height: 100vh; overflow: hidden;
  }

  /* ── Topbar ── */
  .topbar {
    padding: 12px 20px; border-bottom: 1px solid var(--border);
    background: rgba(8,8,8,.97); backdrop-filter: blur(14px);
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
    padding: 6px 13px; border-radius: 4px; border: none; cursor: pointer;
    transition: opacity .15s;
  }
  .btn-top:hover { opacity: .8; }
  .btn-clear  { background: rgba(255,255,255,.06); color: var(--muted); border: 1px solid var(--border); }
  .btn-pause  { background: rgba(232,255,71,.08); color: var(--accent); border: 1px solid rgba(232,255,71,.2); }
  .btn-pause.paused { background: rgba(255,140,0,.1); color: #ff8c00; border-color: rgba(255,140,0,.3); }
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
  .level-btn[data-level="DEBUG"]    { color: var(--c-debug);    border-color: #333; background: rgba(136,136,136,.07); }
  .level-btn[data-level="INFO"]     { color: var(--c-info);     border-color: rgba(74,158,255,.25); background: rgba(74,158,255,.07); }
  .level-btn[data-level="WARNING"]  { color: var(--c-warning);  border-color: rgba(255,140,0,.25);  background: rgba(255,140,0,.07); }
  .level-btn[data-level="ERROR"]    { color: var(--c-error);    border-color: rgba(255,68,68,.25);  background: rgba(255,68,68,.07); }
  .level-btn[data-level="CRITICAL"] { color: var(--c-critical); border-color: rgba(255,59,59,.35);  background: rgba(255,59,59,.1); }
  .search-box {
    margin-left: auto; background: #111; border: 1px solid var(--border);
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
    padding: 3px 20px; border-bottom: 1px solid rgba(255,255,255,.025);
    font-size: 11px; line-height: 1.55; font-family: 'Courier New', monospace;
    transition: background .1s;
  }
  .log-row:hover { background: rgba(255,255,255,.03); }
  .log-row.hidden { display: none; }
  .col-ts    { color: #3a3a3a; white-space: nowrap; }
  .col-level { font-weight: 900; text-transform: uppercase; letter-spacing: .05em; white-space: nowrap; }
  .col-name  { color: #555; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .col-msg   { color: var(--text); word-break: break-word; }

  .level-DEBUG    { color: var(--c-debug); }
  .level-INFO     { color: var(--c-info); }
  .level-WARNING  { color: var(--c-warning); }
  .level-ERROR    { color: var(--c-error); }
  .level-CRITICAL { color: var(--c-critical); }

  /* scroll-to-bottom button */
  #scroll-btn {
    position: fixed; bottom: 20px; right: 24px;
    background: rgba(232,255,71,.12); color: var(--accent);
    border: 1px solid rgba(232,255,71,.3);
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
  <button class="btn-top btn-pause" id="pause-btn" onclick="togglePause()">⏸ Pause</button>
  <button class="btn-top btn-clear" onclick="clearLog()">✕ Clear</button>
  <a href="/" style="font-family:'Inter',sans-serif;font-weight:900;font-size:10px;text-transform:uppercase;
     letter-spacing:.1em;color:var(--muted);text-decoration:none;padding:6px 0">← Dashboard</a>
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
  let totalShown  = 0;

  // Level filter state
  const activeLevel = new Set(['DEBUG','INFO','WARNING','ERROR','CRITICAL']);

  // Search string
  let searchStr = '';

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
      `<span class="col-ts">${e.ts}<span style="color:#222">.${e.ms}</span></span>` +
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

  // ── Add entry ────────────────────────────────────────────────────────────
  const MAX_ROWS = 1000;   // cap DOM rows to keep memory sane

  function addEntry(e) {
    if (paused) return;
    const row = makeRow(e);
    pane.appendChild(row);

    // Trim oldest rows if over cap
    const rows = pane.querySelectorAll('.log-row');
    if (rows.length > MAX_ROWS) {
      for (let i = 0; i < rows.length - MAX_ROWS; i++) rows[i].remove();
    }

    updateCount();
    if (autoScroll) pane.scrollTop = pane.scrollHeight;
  }

  // ── Level toggle ─────────────────────────────────────────────────────────
  function toggleLevel(btn) {
    const lv = btn.dataset.level;
    if (activeLevel.has(lv)) { activeLevel.delete(lv); btn.classList.remove('on'); }
    else                      { activeLevel.add(lv);    btn.classList.add('on');    }
    applyFilters();
  }

  // ── Search + filter ──────────────────────────────────────────────────────
  function applyFilters() {
    searchStr = document.getElementById('search-box').value.toLowerCase();
    for (const row of pane.querySelectorAll('.log-row')) {
      const vis = rowVisible(row.dataset.level, row.dataset.msg);
      row.classList.toggle('hidden', !vis);
    }
    updateCount();
  }

  // ── Pause / clear ─────────────────────────────────────────────────────────
  function togglePause() {
    paused = !paused;
    pauseBtn.textContent = paused ? '▶ Resume' : '⏸ Pause';
    pauseBtn.classList.toggle('paused', paused);
  }

  function clearLog() {
    pane.innerHTML = '';
    updateCount();
  }

  // ── SSE connection ────────────────────────────────────────────────────────
  function connect() {
    const es = new EventSource('/logs/stream');

    es.addEventListener('history', ev => {
      const entries = JSON.parse(ev.data);
      for (const e of entries) addEntry(e);
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
    """Readiness probe for process supervisors and reverse proxies.

    Returns 200 with a JSON body when the server is up and has at least
    one configured input. Returns 503 if no inputs are configured yet
    (e.g. still bootstrapping).
    """
    async with input_config_lock:
        n = len(INPUT_IDS)
    if n == 0:
        return JSONResponse({"status": "starting", "inputs": 0}, status_code=503)
    return JSONResponse({"status": "ok", "inputs": n})


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

