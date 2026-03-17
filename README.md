# BroadcastHub

**BroadcastHub** is a self-hosted web application for managing professional video capture hardware. It provides a browser-based dashboard for monitoring live inputs, recording to disk, scheduling recordings, streaming via HLS to mobile devices, and controlling Android TV source devices via ADB.

---

## Features

- **Multi-input dashboard** — monitor all Magewell and Decklink capture inputs in real time via SSE
- **Live streaming** — stream any input as MPEG-TS directly to VLC or any compatible player
- **HLS broadcasting** — one-click HLS stream for mobile/browser viewing at `/mobile`
- **Recording** — record to TS, MP4, MKV, or MOV with optional timed duration
- **Scheduler** — queue recordings with a future start time
- **Hardware encoding** — supports Intel QSV, NVIDIA NVENC, AMD AMF, VAAPI, and software x264/x265
- **Android TV control** — built-in ADB remote for navigating source devices
- **Real-time log viewer** — timestamped, filterable log stream at `/logs`
- **Automatic driver reinstall** — detects missing Magewell kernel module after kernel updates and reinstalls automatically
- **Authentication** — bcrypt password hashing, signed HttpOnly session cookies
- **Dark / Mono / Light themes**

---

## Supported Hardware

| Type | Cards |
|------|-------|
| Magewell Pro Capture | Quad HDMI, Dual HDMI, HDMI 4K, Quad SDI, and others |
| Magewell Eco Capture | HDMI 4K M.2 (single channel, up to 4K30) |
| Blackmagic Decklink | Any card supported by ffmpeg's decklink input device |

---

## Requirements

- Ubuntu 22.04 LTS or 24.04 LTS
- Python 3.10+
- ffmpeg 4.4+
- Magewell ProCapture driver (for Magewell cards)
- ADB (optional, for Android TV control)

---

## Quick Install

### 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-pip ffmpeg adb
```

### 2. Install Python dependencies

```bash
pip3 install -r requirements.txt --break-system-packages
```

### 3. Install the Magewell driver

Download the ProCapture driver from [magewell.com/downloads/pro-capture](https://www.magewell.com/downloads/pro-capture), extract it to a permanent location, then run:

```bash
cd ~/src/Magewell/ProCaptureForLinux_x.x.xxxx
sudo ./install.sh
```

### 4. Run one-time setup

```bash
sudo ./setup.sh
```

This configures the passwordless sudo rule for automatic driver reinstallation after kernel updates, saves the installer path, and optionally installs a systemd service.

### 5. Start BroadcastHub

```bash
python3 m2tsweb_fastapi.py
```

Open your browser at `http://<your-machine-ip>:6502`

On first visit you will be prompted to set a password.

---

## Automatic Driver Reinstallation

The Magewell ProCapture driver is a kernel module that must be rebuilt after every kernel update. BroadcastHub handles this automatically:

- On startup, it checks whether the ProCapture module is loaded via `lsmod`
- If missing and an installer path is configured, it runs `sudo install.sh` automatically
- If the automatic reinstall fails, an orange banner appears on the dashboard with a guided reinstall panel including a folder browser and live console output

Run `sudo ./setup.sh` once to configure the passwordless sudo rule that makes this possible.

---

## File Reference

| File | Purpose |
|------|---------|
| `m2tsweb_fastapi.py` | Main application — FastAPI server, all routes, capture management, recording, scheduling |
| `templates.py` | HTML rendering — dashboard, mobile page, all UI templates |
| `auth.py` | Authentication — password hashing, session cookies, login pages |
| `setup.sh` | One-time setup — sudoers rule, installer path, optional systemd service |
| `requirements.txt` | Python dependencies |

> **Note:** `auth.json` (credentials) and `input_config.json` (runtime config) are created automatically on first run and are excluded from this repository via `.gitignore`.

---

## Acknowledgements

Built with assistance from [Claude](https://claude.ai) (Anthropic) — AI pair programmer for architecture, implementation, and debugging.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
