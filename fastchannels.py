"""FastChannels helpers for Broadcast Hub's "fastchannels" tuning method.

FastChannels (https://github.com/kineticman/fastchannels) can resolve a
provider channel to a playable manifest plus a Widevine licence URL, and
ships a tiny Android player (com.fastchannels.player) that plays exactly
that when started over adb. FastChannels itself can only drive ONE fixed
device, so Broadcast Hub does the adb part itself — one player per tuner in
its own pool — and only asks FastChannels for the two URLs.

Everything here is plain blocking HTTP with no Broadcast Hub state, so it
can be imported and used on its own (e.g. to fill channels.json offline).
Callers on the event loop run these through run_in_executor.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

PLAYER_PACKAGE   = "com.fastchannels.player"
PLAYER_COMPONENT = PLAYER_PACKAGE + "/.PlaybackActivity"

HTTP_TIMEOUT_SECS = 10


class FastChannelsError(RuntimeError):
    """FastChannels couldn't be reached or didn't know the channel."""


def _get_json(base_url: str, path: str, timeout: int = HTTP_TIMEOUT_SECS):
    url = base_url.rstrip("/") + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise FastChannelsError(f"FastChannels answered HTTP {exc.code} for {path}") from exc
    except Exception as exc:
        raise FastChannelsError(f"FastChannels at {base_url} is unreachable: {exc}") from exc


def list_source_channels(base_url: str, source: str) -> list:
    """Every channel FastChannels has for one source (enabled or not)."""
    out, page = [], 1
    while True:
        data = _get_json(base_url, f"/api/channels?source={urllib.parse.quote(source)}&per_page=200&page={page}")
        out += [c for c in data.get("channels", []) if c.get("source_name") == source]
        if page >= int(data.get("pages") or 1):
            return out
        page += 1


def _playback_from_preview(base_url: str, preview: dict) -> dict:
    ch = preview.get("channel") or {}
    # Same choice FastChannels makes when it triggers its own player: the
    # source's dedicated manifest route when there is one, else the play URL.
    manifest = preview.get("preview_url") or preview.get("play_url") or ""
    if manifest.startswith("/"):
        manifest = base_url.rstrip("/") + manifest
    return {
        "fc_id":        ch.get("id"),
        "fc_source":    ch.get("source_name") or "",
        "fc_channel_id": str(ch.get("source_channel_id") or ""),
        "name":         ch.get("name") or "",
        "manifest_url": manifest,
        "license_url":  ch.get("license_url") or "",
    }


def playback_by_id(base_url: str, fc_id) -> dict:
    """Manifest + licence URL for FastChannels' own numeric channel id.

    base_url must be an address the DEVICE can reach (not 127.0.0.1):
    FastChannels builds the licence URL from the host it was asked on.
    """
    return _playback_from_preview(base_url, _get_json(base_url, f"/api/channels/{int(fc_id)}/preview?detect=0"))


def resolve_playback(base_url: str, fc_source: str, fc_channel_id: str,
                     fc_id=None, name_hint: str = "") -> dict:
    """Manifest + licence URL for a channel known by its stable identity
    (source name + the source's own channel id — the pair FastChannels uses
    in its /play/... URLs). fc_id is FastChannels' numeric row id, used as a
    shortcut; it can change if FastChannels' database is rebuilt, so it's
    verified and, when stale, found again by name.
    """
    fc_channel_id = str(fc_channel_id)

    def _matches(info: dict) -> bool:
        return info["fc_source"] == fc_source and info["fc_channel_id"] == fc_channel_id

    if fc_id:
        try:
            info = playback_by_id(base_url, fc_id)
            if _matches(info):
                return info
        except FastChannelsError:
            pass   # stale id, or FastChannels down — the search below tells which

    query = f"/api/channels?source={urllib.parse.quote(fc_source)}&slim=1&per_page=50"
    if name_hint:
        query += "&search=" + urllib.parse.quote(name_hint)
    for cand in _get_json(base_url, query).get("channels", []):
        info = playback_by_id(base_url, cand["id"])
        if _matches(info):
            return info
    raise FastChannelsError(
        f"FastChannels has no {fc_source} channel with id {fc_channel_id}"
        + (f" ({name_hint})" if name_hint else "")
    )


_FEED_PLAY_URL_RE = re.compile(r"^https?://[^/]+/play/fc-player/([^/]+)/(.+)\.m3u8\s*$")

# FastChannels publishes its player ("DRM bridge") channels as two playlists:
# the ones whose guide comes from its own XMLTV, and the ones that carry a
# Gracenote station id instead.
FEED_VARIANTS = {"xmltv": "", "gracenote": "/gracenote"}


def feed_entries(base_url: str, feed: str = "default", guide: str = "all") -> list:
    """FastChannels' player playlist(s) for one feed, parsed into
    [{extinf, source, channel_id, name}] — channel_id decoded, i.e. the
    source's own channel id as FastChannels stores it. guide is "xmltv",
    "gracenote" or "all".
    """
    variants = [guide] if guide in FEED_VARIANTS else list(FEED_VARIANTS)
    entries = []
    for variant in variants:
        url = f"{base_url.rstrip('/')}/feeds/{urllib.parse.quote(feed)}/m3u/fc-player{FEED_VARIANTS[variant]}"
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECS) as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception as exc:
            raise FastChannelsError(f"FastChannels playlist {url} could not be read: {exc}") from exc
        extinf = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#EXTINF"):
                extinf = line
            elif extinf and line and not line.startswith("#"):
                m = _FEED_PLAY_URL_RE.match(line)
                if m:
                    entries.append({
                        "extinf":     extinf,
                        "source":     m.group(1),
                        "channel_id": urllib.parse.unquote(m.group(2)),
                        "name":       extinf.rsplit(",", 1)[-1].strip(),
                    })
                extinf = None
    return entries


def check_manifest(manifest_url: str, timeout: int = 8) -> None:
    """Raise FastChannelsError unless the manifest can be fetched right now.

    FastChannels answers its manifest routes with an HTTP error when it has
    no stream to give (e.g. an Amazon channel with no playback envelope
    until its next scrape). The player would just sit on a black screen, so
    this is checked from here first — it takes milliseconds, where waiting
    for a player that will never play takes the whole readiness timeout.
    """
    try:
        with urllib.request.urlopen(manifest_url, timeout=timeout) as resp:
            if not resp.read(64):
                raise FastChannelsError("FastChannels returned an empty stream manifest")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(200).decode("utf-8", "replace").strip()
        except Exception:
            pass
        raise FastChannelsError(
            f"FastChannels has no stream for this channel right now (HTTP {exc.code}"
            + (f": {detail}" if detail else "") + ")"
        ) from exc
    except FastChannelsError:
        raise
    except Exception as exc:
        raise FastChannelsError(f"FastChannels stream manifest could not be fetched: {exc}") from exc


def refresh_source(base_url: str, source: str, timeout: int = 45) -> bool:
    """Ask FastChannels to scrape one source again now, and wait for it.

    Some sources hand out playback access that lapses long before the next
    scheduled scrape (Amazon's free channels: FastChannels then answers "no
    playback envelope" until it scrapes again), and a fresh scrape brings it
    back. True once a newer scrape has completed, False if it didn't within
    [timeout] seconds or the source isn't there or is disabled.
    """
    def _source():
        return next((s for s in _get_json(base_url, "/api/sources") if s.get("name") == source), None)

    before = _source()
    if not before or not before.get("is_enabled"):
        return False
    request = urllib.request.Request(f"{base_url.rstrip('/')}/api/sources/{before['id']}/run", data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECS):
            pass
    except Exception as exc:
        raise FastChannelsError(f"FastChannels would not refresh {source}: {exc}") from exc
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.5)
        now = _source()
        if now and now.get("last_scraped_at") and now.get("last_scraped_at") != before.get("last_scraped_at"):
            return True
    return False


def _norm_name(name: str) -> str:
    name = (name or "").lower().replace("&", " and ")
    name = re.sub(r"\b(hd|channel|network|tv|the|east|us)\b", " ", name)
    return re.sub(r"[^a-z0-9+]", "", name)


def match_channels(bh_channels: dict, fc_channels: list) -> tuple:
    """Pair Broadcast Hub channels with a FastChannels source's channels.

    Guide station id (Gracenote) first when both sides have one and it is
    unambiguous, else the exact name, else the name with HD/Channel/TV-style
    noise words dropped. Returns ({channel_id: fc_channel}, [unmatched ids]).
    """
    by_station, by_exact, by_norm = {}, {}, {}
    for fc in fc_channels:
        if fc.get("gracenote_id"):
            by_station.setdefault(str(fc["gracenote_id"]), []).append(fc)
        by_exact.setdefault((fc.get("name") or "").strip().lower(), []).append(fc)
        by_norm.setdefault(_norm_name(fc.get("name")), []).append(fc)

    matched, unmatched = {}, []
    for channel_id, ch in bh_channels.items():
        name = ch.get("display_name", "")
        candidates = (
            by_exact.get(name.strip().lower()),
            by_station.get(str(ch.get("guide_station_id") or "")),
            by_norm.get(_norm_name(name)),
        )
        hit = next((hits[0] for hits in candidates if hits and len(hits) == 1), None)
        if hit:
            matched[channel_id] = hit
        else:
            unmatched.append(channel_id)
    return matched, unmatched


def match_and_resolve(base_url: str, source: str, bh_channels: dict) -> tuple:
    """match_channels plus each match's stable identity, ready to store on
    the channel: {channel_id: {fc_source, fc_channel_id, fc_id}}."""
    matched, unmatched = match_channels(bh_channels, list_source_channels(base_url, source))
    resolved = {}
    for channel_id, fc in matched.items():
        try:
            info = playback_by_id(base_url, fc["id"])
        except FastChannelsError:
            unmatched.append(channel_id)
            continue
        if not info["fc_channel_id"]:
            unmatched.append(channel_id)
            continue
        resolved[channel_id] = {
            "fc_source":     info["fc_source"],
            "fc_channel_id": info["fc_channel_id"],
            "fc_id":         info["fc_id"],
        }
    return resolved, unmatched
