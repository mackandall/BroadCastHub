# Broadcast Hub — Changelog

Session covering: channel-number tuning, EDID refresh, several confirmed
race-condition/reliability fixes, stray-device auto-sleep, M3U format
support, and rebuilding HDHomeRun/DLNA discovery around the real channel
system instead of raw physical inputs.

---

## Channel Tuning

### Fixed: SiliconDust's HDHomeRun app lost ALL tuners while Broadcast Hub was on
- 2026-11-18: with discovery on, the HDHomeRun app (macOS) said "No
  HDHomeRun tuners found" and the real QUATRO became unusable too. Two
  causes, found in the app's own code:
  - No `Access-Control-Allow-Origin: *` header. The app is a web app, so
    WebKit hid Broadcast Hub's answers ("TypeError: Load failed at
    .../discover.json"). Now sent on the HDHomeRun paths (discover.json,
    lineup.json/.xml, lineup_status.json, status.json, /auto, /tuner), like
    a real HDHomeRun; the login-protected pages don't get it.
  - The made-up `DeviceAuth` "broadcasthub". The app joins every tuner's
    DeviceAuth and sends it to SiliconDust's cloud; the bad value spoiled
    the whole list, so every tuner was dropped as "no guide access". Now
    empty in discover.json and left out of the UDP discovery reply: the
    real tuners are approved and only Broadcast Hub shows "no guide".
- Also added: `/status.json` (per-tuner status like a real HDHomeRun) and
  VideoCodec/AudioCodec/HD on each lineup.json channel.

### Added: HDHomeRun stream port 5004 (`/auto/v<number>`, `/tuner<n>/v<number>`)
- 2026-11-18: NextPVR found Broadcast Hub as an HDHomeRun (after the real
  device id) but said "No tuner was available": it streams from
  `http://<ip>:5004/tuner3/v204` like it would from a real unit, and nothing
  answered on port 5004.
- With "Let apps find Broadcast Hub automatically" on, port 5004 is served
  too, with both routes. They find the channel by its number and use the
  same tuning, sharing and 805 refusal as `/channel/<id>/stream`; Broadcast
  Hub still picks the box itself for `/tuner<n>`. lineup.json/.xml then
  point at `http://<ip>:5004/auto/v<number>`, as a real HDHomeRun's do;
  with the setting off they keep the `/channel/<id>/stream` URLs.
- Two enabled channels can no longer share a number: saving or enabling one
  is refused with the name of the channel that has it.

### Added: "All Tuners In Use" refusal, like a real HDHomeRun
- 2026-11-18: when every tuner a channel may use was busy on other
  channels, the stream still answered 200 at once and then ended empty, so
  Jellyfin waited and failed with a vague "zero bytes" error.
- A channel stream now checks first: if all of the channel's tuners are
  busy and none is already on that channel, it answers 503 with
  `X-HDHomeRun-Error: 805 All Tuners In Use` straight away. Sharing a
  channel that is already tuned, and the instant 200 for normal tunes, are
  unchanged. Logged as "Channel stream: … refused … busy".

### Added: FastChannels Player watch — restart a player that stopped mid-stream
- 2026-10-14: a 30 s DirecTV CDN timeout left the player on "Source error"
  for the rest of the night. The capture card kept encoding the black
  screen, so the stream, the tuner and the recording all looked healthy.
- While a FastChannels channel is tuned, the heartbeat now also checks the
  player's media session every 30 s. Errored, stopped or gone on two checks
  running, or buffering/paused for over 60 s, means the channel is resolved
  again and the player restarted on the box; the capture keeps running, so
  the recording continues in the same file. Logged as "Player watch: …".
- After three failed restarts it retries every 2 minutes, and emails
  (alert_config.json) once when two in a row didn't help.
- Channels page, "Tuning Method": checkbox to turn it off (on by default).

### Added: FastChannels tuning method, switchable with the provider-app method
- New `fastchannels` provider: FastChannels resolves a channel to a
  manifest + Widevine licence URL and Broadcast Hub starts FastChannels
  Player (`com.fastchannels.player`) on whichever pool device is free —
  no provider app, no channel numbers. Measured on onn 4K boxes: ~5 s
  from request to picture, two channels started at once, 1080p + AC-3 5.1.
- Channels page, "Tuning Method": one switch between the provider app on
  the DirecTV boxes and FastChannels Player on the onn boxes. Each method
  keeps its own device address per input, and channels keep both their
  provider fields and their FastChannels mapping, so switching back
  restores the other setup exactly. Refused while a tuner is in use.
- "Match channels to FastChannels" fills each channel's mapping
  (`fc_source` + `fc_channel_id`) by guide station id, then name.
- Ready means the player's own media session reports PLAYING; a player
  that never starts playing fails the tune instead of recording black.
  Release force-stops the player before sleeping the box.
- `/fastchannels/playlist.m3u` (public, `?feed=` and `?guide=xmltv|gracenote|all`):
  FastChannels' own player playlist with the stream URLs pointed at this
  server, so every channel FastChannels publishes for its player (Amazon,
  DirecTV, ...) tunes through the pool here instead of FastChannels' single
  fixed device. Those channels need no channels.json entry — their id,
  `fc:<source>:<channel id>`, says what to play.

### Added: Plex's protected free channels (`plexlive.py`)
- Plex lists and serves a few of its free live channels (Crunchyroll,
  CINEVAULT, CINEVAULT: Classics, CINEVAULT: Westerns, GET Comedy, Stories
  by AMC) only to a DRM-capable client. `/plexlive/playlist.m3u` offers them
  as channels tuned here (FastChannels Player on a pool box, stream and
  licence URLs straight from Plex with an anonymous token), and
  `/plexlive/epg.xml` is their guide from Plex's own listings (7 days,
  rebuilt every 3 hours). Both public, like the other playlists.
- FastChannels: when it has no stream for a channel, ask it to scrape that
  source again and retry once (setting on the Tuning Method card).
- Fixed: `/fans/config` had lost its route decorator, so the dashboard
  showed no CoolerControl profiles after a page reload.

### Added: network encoder inputs and reserved tuners
- New input driver `network`: an HDMI port of a network encoder that
  already serves MPEG-TS over HTTP (ffmpeg copies it; nothing is encoded
  here). Added from the Channels page ("Add a network tuner") or
  `POST /inputs/network`; the key comes from the URL
  (`http://192.168.1.168/8.ts` -> `168-8`).
- Inputs can be reserved with a `pool`: tuners marked "Plex channels only"
  serve the `/plexlive` channels and nothing else, and the general pool is
  left to the provider channels. A setting lets Plex channels borrow a
  general-pool tuner when theirs are busy (off by default).
- A network encoder shows its own NO SIGNAL card while a box wakes from
  sleep (~7 s on an onn stick), so a tune that had to wake the box waits
  that out before the capture starts.

### Added: channel-number tuning (`directv_number` provider)
- New selectable provider alongside the existing deep-link method
  (`directv_now`), chosen per-channel from the same Provider dropdown.
- Tunes via `input text {guide_number}` — literal keypad entry, matching
  real remote-control use — instead of `am start` with a deep-link URI.
  Confirmed against real `bmitune.sh`/`prebmitune.sh` scripts and a
  direct community explanation of why: deep-links are delivered to the
  app as an external Intent (`onNewIntent`), a different and
  less-exercised code path than ordinary remote input, and can
  occasionally land on the wrong channel or fail silently.
- No `force-stop` before tuning — `input text` requires the app already
  open and focused; killing it first defeats the mechanism entirely.
  This makes it a warm-tune-only method; it cannot cold-start the app.
- Readiness gate runs **before** `input text`, not after (fixed after a
  live test showed the channel number being typed before the app had
  focus, landing nowhere, with the app resuming its previous channel
  once it finished loading). A failed gate now aborts the tune outright
  so a different tuner can be tried, rather than firing input blind.
- Readiness gate gained a second, required check for this method: the
  DirecTV app's window must actually have input focus
  (`dumpsys window` / `mCurrentFocus`), not just report an active
  audio/media session — confirmed necessary via real `prebmitune.sh`
  comments ("mCurrentFocus is the only signal that says a keystroke
  will land; mFocusedApp stays set while asleep").
- Requires each channel's `guide_number` field to be set; returns a
  clear, specific error at tune time if it's missing rather than
  failing silently.

### M3U import: added support for a second URL format
- Original format: `CALLSIGN~CONTENTID` in the URL
  (`/play/tuner/METV~83321f4e-...`) — deep-link tuning.
- New format: a bare channel number in the URL with no tilde
  (`/play/tuner/77`) — number-tuning. A real-world M3U export in this
  format was previously parsed as zero channels (`400 Bad Request`)
  since the parser required a tilde to match anything.
- Parser now tries the tilde format first, falls back to treating the
  trailing path segment as the channel number/ID directly. Verified
  against decimal channel numbers (`305.1`, `557.2`, etc.) from a real
  150+ channel lineup.

---

## EDID Refresh

### Added: automatic EDID refresh on every tune
- A live test showed forcing an EDID rewrite while watching a stream
  with distorted/garbled audio caused a visible resync and cleared the
  audio — confirming stale EDID negotiation, not tuning timing, as a
  real cause of intermittent audio corruption.
- EDID was previously only ever written once, at server startup. It is
  now also re-applied at the start of every tune, before the ADB
  sequence begins, using the same underlying mechanism as the existing
  manual "write EDID now" action.
- Toggleable — see Streaming Behavior settings below.

### Fixed: EDID write success was not actually being verified
- Confirmed directly from `magewell2ts`'s own source
  (`Magewell::WriteEDID`): the function logs whether the underlying SDK
  call succeeded, but **returns `true` unconditionally regardless** —
  meaning the process exit code alone could not be trusted to mean the
  write actually worked.
- Fixed to check the real signal instead: the printed text ("EDID
  written successfully" vs "Failed to write EDID!"). The tune log now
  also includes the raw command output for full visibility.

---

## Reliability / Race-Condition Fixes

### Fixed: proactive eviction left orphaned generators running
- When a same-client channel switch evicted a viewer, the old
  generator (still `await`ing its queue) was never actually woken —
  just removed from a list. It would eventually hit its own 15s
  timeout and release whatever `input_id` it remembered, which could
  by then belong to a brand-new, still-in-progress tune (`was_tuned_
  to=None` observed in a real log).
- Fixed with a `stop_event` per viewer: eviction now wakes the
  generator immediately, and a generator woken this way skips its own
  release logic entirely (eviction already released the correct input
  at the correct moment).

### Fixed: heartbeat was skipped when the readiness gate timed out
- Heartbeat previously only started if the readiness gate reported
  `ready=True`. Capture proceeds either way, and the app's own
  5-minute inactivity timer doesn't care whether our own readiness
  check happened to succeed. A real four-input test showed two inputs
  with a timed-out gate getting zero heartbeat protection for the rest
  of the session.
- Heartbeat now starts unconditionally once a tune attempt completes
  without error; the ready/not-ready outcome is still logged.

### Fixed: device release could be cancelled mid-command
- The stream generator's own cleanup used to `await
  _release_tuner(...)` directly inside a `finally:` block. When a
  client disconnects, the ASGI framework cancels the task driving that
  generator — and Python's asyncio can deliver that cancellation into
  code already running inside `finally:`, interrupting whatever await
  is in flight. A real log showed the sleep-keyevent step never
  logging at all (success or failure) after a real disconnect — the
  device was never actually told to sleep.
- Fixed by dispatching release via `asyncio.create_task(...)` instead,
  matching every other release call site in the codebase, so it
  survives the generator's own cancellation.

### Fixed: same-channel reconnect could race against an in-flight release
- `input_tuning_state` used to only get cleared *after* the slow
  device-sleep ADB command completed. A same-channel reconnect arriving
  in that window (e.g. Channels DVR moving between two recordings on
  the same channel) would see stale "still active" state, skip
  re-tuning, and then find the device going to sleep underneath it —
  turning what should be a seamless handoff into a failed reuse attempt
  followed by a second, real tune ~15s later.
- Fixed by clearing tuning state immediately, before the slow sleep
  command runs, so a fast reconnect always sees accurate state.

---

## Streaming Behavior Settings

New settings section on the Channels page (`stream_settings.json`),
each independently toggleable, collapsible cards for provider timing.

### Same-client channel-switch eviction (default: on)
- On: correct for a single interactive viewer (e.g. VLC) switching
  channels — the previous channel is stopped the moment a new one is
  requested.
- Off: required for a DVR doing concurrent multi-channel recording
  (e.g. Channels DVR) — every recording shares one server IP, and with
  this on, starting a new recording would incorrectly stop every other
  recording already in progress from that same DVR. Confirmed causing
  exactly this during real four-way recording tests.

### EDID refresh on tune (default: on)
- See EDID Refresh section above.

### Stray device sleep (default: on)
- Every existing device-sleep mechanism is reactive, scoped to devices
  the app itself tuned. A device that becomes awake outside that
  lifecycle (e.g. a firmware update triggers a restart, and the box
  wakes up showing whatever channel it defaults to) was previously
  invisible to the app entirely and would run awake indefinitely.
- A periodic sweep (interval configurable, default 60s, floor 15s)
  checks every tuner-pool device with no active tune of ours behind it,
  via `dumpsys power`, and puts it back to sleep if it's genuinely
  awake.
- Fixed a real race in the first version: the sweep read tuning state
  with no lock, so it could catch a device mid-tune (woken as part of a
  genuine, still-in-progress tune, but not yet marked active) and
  incorrectly sleep it. Fixed by having the sweep acquire the same lock
  real tuning uses, so it can never act on a device mid-tune — at the
  cost of a small, bounded delay to any new tune request that happens
  to overlap with a sweep pass.

---

## HDHomeRun / DLNA Discovery

### Added: "Let apps find Broadcast Hub automatically" (port 80) + a real device id
- 2026-11-18: discovery alone wasn't enough. Jellyfin, Channels DVR and
  Emby ignore the reply's `BaseURL` and fetch `http://<ip>/discover.json`
  on port 80, like a real HDHomeRun.
- New checkbox on the Channels page, **Network Discovery → Let apps find
  Broadcast Hub automatically**: Broadcast Hub also serves the whole app
  on port 80 (a second uvicorn server on the same app, so client
  addresses stay real), switchable without a restart, remembered across
  restarts. While it's on, discovery requests from this machine are
  answered too. The page shows green when serving, or why it can't (no
  permission, port in use).
- Opening port 80 without root needs `CAP_NET_BIND_SERVICE`: `setup.sh`
  now adds `AmbientCapabilities=CAP_NET_BIND_SERVICE` to the service.
- The fixed device id `10000001` is gone. It sits in the range
  libhdhomerun treats as first-generation "legacy" tuners with no web
  server, so Channels DVR added Broadcast Hub with no lineup and never
  fetched anything; it also failed HDHomeRun's id checksum. Each install
  now generates its own id once (prefix `105`, valid checksum, kept in
  `hdhr_device_id.txt`); a saved legacy id is replaced. Apps that knew
  the old id see Broadcast Hub as a new device once.
- Verified 2026-11-18: Channels DVR (restarted) added Broadcast Hub as a
  regular HDHomeRun with 90 channels; Jellyfin's Detect lists
  "Broadcast Hub · http://192.168.1.188" with no errors.

### Added: HDHomeRun network discovery (UDP 65001)
- 2026-11-18: apps that "find tuners" (Plex, Channels DVR, the HDHomeRun
  app, libhdhomerun's `hdhomerun_config discover`) broadcast a discover
  request to UDP 65001. Broadcast Hub now answers it like a real
  HDHomeRun: device id, tuner count (the same number `/discover.json`
  gives) and `BaseURL`/`LineupURL` with the `:6502` port, on whichever of
  this machine's addresses the asker can reach. Malformed packets and
  requests for other device types or ids are ignored. If the port is
  taken, this logs a warning and nothing else is affected.
- Requests from this machine itself are not answered. Jellyfin's
  discovery ignores the reply's `BaseURL` and fetches
  `http://<ip>/discover.json` on port 80; that is refused, Jellyfin logs
  an error and stops listening for other tuners' replies. So Jellyfin
  (running on the same machine) still adds Broadcast Hub by address,
  `192.168.1.x:6502`.
- Verified 2026-11-18: a broadcast from another machine gets Broadcast
  Hub's reply next to a real HDHomeRun QUATRO's, in the same format; a
  broadcast from this machine gets only the QUATRO's.
- The device id (`10000001`) doesn't pass the checksum real HDHomeRun ids
  carry. It was kept so existing setups don't see a new device; if a
  libhdhomerun-based app skips Broadcast Hub in discovery, that's why.

### Replaced: HDHomeRun lineup now reflects real channels, not raw inputs
- `/discover.json`, `/lineup.json`, `/lineup.xml` previously listed the
  raw physical capture inputs (`Board 0 · Input 1`, etc.), with URLs
  pointing at `/stream/{input_id}` — whatever happened to be manually
  showing on that board at that moment.
- Now built from `channels.json` — the same source `/channels/
  export.m3u` already uses — with real guide numbers/names and URLs
  pointing at `/channel/{id}/stream`, going through the full tuning
  pipeline. `TunerCount` in `/discover.json` now reflects the real
  tuner pool size, not the raw input count — matching how a real cable
  HDHomeRun device advertises a lineup far larger than its simultaneous
  tuner count.
- Verified working end-to-end via Emby (native HDHomeRun "add by IP,"
  after discovering Emby hardcodes port 80 for that field — Broadcast
  Hub must be reached with an explicit port, e.g. `192.168.1.x:6502`).

### Replaced: DLNA "Channels" folder (VLC's Universal Plug'n'Play) — same change
- Previously listed raw inputs via `/stream/{input_id}`; a board only
  ever appeared here if it happened to already be awake, and showed
  nothing while asleep (its normal state between tunes).
- Now built from `channels.json`, identical source/logic to the
  HDHomeRun lineup above. Verified working directly in VLC: channel
  list displays correctly, and channels tune and play on selection.

---

## Known Gaps / Tabled

- **Readiness gate has no real per-channel verification** — it confirms
  the app is playing *something* with focus, not that it's specifically
  the newly-requested channel. A device already open/focused before a
  tune could report `ready=True` without the tune's own input ever
  having been necessary or confirmed to land.
- **Video motion-gated tuning** (the `ah4c`-fork approach) — evaluated
  in depth; concluded to be the largest, most specialized undertaking
  in the project (real MPEG-TS demuxing, decode-cost tradeoffs, a
  nontrivial "seam" handoff problem). Current settle-delay + readiness
  gates approach is already producing clean results in real four-way
  tests; motion detection would mainly buy adaptiveness over the fixed
  delay, not fix something currently broken.
- **On-device native tuning app** (matching `FastChannels Player`) —
  would explain their faster tune times and invisible loading
  transitions (control + detection happening inside the same process
  instead of via external ADB polling), but is real Android app
  development — a different category of work than anything in this
  project so far. Confirmed it still fundamentally depends on the real
  DirecTV app under the hood for DRM'd content; it's a controller, not
  a replacement.
- **Interactive device control (ws-scrcpy)** — D-pad overlay, keyboard
  passthrough, mouse click-to-navigate. Groundwork exists (`remote_key`
  already handles ADB and Roku keycodes); UI layer never built.
- **Client silence during the tune wait** — periodic NULL TS packets
  during the tuning gap, in case a client times out on total silence.
  Never confirmed as a real problem; tabled pending observing it.
- **Closed captions via local speech-to-text** — the `ah4c` approach of
  generating captions from audio rather than recovering broadcast
  captions (confirmed unavailable at the SDK level for this HDMI
  hardware). Large scope: real speech-engine integration plus low-level
  TS bitstream manipulation. Needs a design pass before any code.
- **`asyncio.create_subprocess_exec` migration** and **Docker
  containerization** — cleanup/modernization items, never urgent.
