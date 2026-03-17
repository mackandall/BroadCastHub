# BroadcastHub

**BroadcastHub** is a self-hosted web application for managing video magewell and decklink capture hardware. It provides a browser-based dashboard for monitoring live inputs, recording to disk, scheduling recordings, streaming via HLS to mobile devices, and controlling Android TV source devices via ADB if needed.

---

## Features

- **Multi-input dashboard** — monitor all Magewell and Decklink capture inputs in real time via SSE
- **Live streaming** — stream any input as MPEG-TS directly to VLC or any compatible player
- **HLS broadcasting** — one-click HLS stream for mobile/browser viewing at `/mobile`
- **Recording** — record to TS, MP4, MKV, or MOV
- **Scheduler** — queue recordings with a future start time if needed 
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

- Ubuntu 22.04 LTS or 24.04 LTS (Have not tested with any other flavors of Linux)
- Python 3.10+
- ffmpeg 4.4+
- Magewell ProCapture driver (for Magewell cards)
- magewell2ts binary (for Magewell cards) — see below
- ADB (optional, for Android TV control)

---

## Quick Install

### 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-pip ffmpeg adb build-essential cmake libv4l-dev libudev-dev
```

### 2. Install Python dependencies

```bash
pip3 install -r requirements.txt --break-system-packages
```

### 3. Install the Magewell driver and magewell2ts

BroadcastHub uses the [magewell2ts](https://github.com/jpoet/Magewell2TS) application by jpoet to interface with Magewell Pro Capture cards. This requires both the Magewell ProCapture driver and the magewell2ts binary to be installed.

#### 3a. Install the Magewell ProCapture driver

Download the Linux driver from the Magewell website:

- **Pro Capture:** https://www.magewell.com/downloads/pro-capture#/driver/linux-x86
- **Eco Capture:** https://www.magewell.com/downloads/eco-capture#/driver/linux-x86

Extract and install to a **permanent location** — BroadcastHub needs this path for automatic reinstallation after kernel updates:

```bash
mkdir -p ~/src/Magewell
tar -xzf ProCaptureForLinux_*.tar.gz -C ~/src/Magewell
cd ~/src/Magewell/ProCaptureForLinux_x.x.xxxx
sudo ./install.sh
```

Verify the driver loaded:

```bash
lsmod | grep ProCapture
```

> **Note:** On newer kernels it may be necessary to add `ibt=off` to kernel parameters:
> ```bash
> sudo grubby --update-kernel=ALL --args="ibt=off"
> ```

#### 3b. Install magewell2ts

magewell2ts reads directly from the Magewell API and outputs MPEG-TS to stdout. Full build instructions are at https://github.com/jpoet/Magewell2TS — a summary for Ubuntu follows.

Download the Magewell Capture SDK from https://www.magewell.com/sdk, then:

```bash
mkdir -p ~/src/Magewell
cd ~/src/Magewell
tar -xzf Magewell_Capture_SDK_Linux_*.tar.gz
git clone https://github.com/jpoet/Magewell2TS.git
cd Magewell2TS
mkdir build && cd build
cmake ..
make
sudo make install
```

Verify the install:

```bash
magewell2ts --list
```

You should see your capture cards listed.

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

## Decklink Support

Decklink cards are supported via ffmpeg's decklink input device. No additional binaries are required beyond ffmpeg compiled with Decklink support. Decklink inputs are auto-detected at startup.

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

## Related Projects

- [magewell2ts](https://github.com/jpoet/Magewell2TS) by jpoet — the capture binary BroadcastHub uses to interface with Magewell Pro Capture cards
- [ah4c](https://github.com/sullrich/ah4c) — Android HDMI for Channels, recommended companion for tuning Android TV streaming devices

---

## Acknowledgements

Built with assistance from [Claude](https://claude.ai) (Anthropic) — AI pair programmer for architecture, implementation, and debugging.
Conceptualized initially by @istwok on Chanels DVR forums. 

---

## License

MIT License — see [LICENSE](LICENSE) for details.
