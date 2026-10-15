"""Plex's protected free live channels, for Broadcast Hub.

Plex's free "Live TV" has a handful of channels (Crunchyroll, CINEVAULT, ...)
that are Widevine-protected. Plex only lists and serves them to a client that
declares DRM support (X-Plex-Drm), so playlist tools that take Plex's plain
HLS channels never see them. A player with Widevine can play them directly:
Plex hands out an HLS manifest and a licence endpoint, both usable with the
same anonymous token any visitor of watch.plex.tv gets.

This module only talks to Plex (blocking HTTP, no Broadcast Hub state):
which channels are protected, the two URLs for one of them, and their guide
as XMLTV. Broadcast Hub starts the player on a tuner with those URLs — the
same player and tune path as its FastChannels method.
"""
import datetime
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

EPG_HOST   = "https://epg.provider.plex.tv"
TOKEN_URL  = "https://plex.tv/api/v2/users/anonymous"
HTTP_TIMEOUT_SECS = 20

# An anonymous token is cheap to get and is asked for again well before any
# plausible expiry, and at once when Plex refuses it.
TOKEN_MAX_AGE_SECS = 6 * 3600

# Plex pads the far end of a protected channel's guide with placeholder
# entries named after the channel; real listings reach about a week.
GUIDE_DAYS = 7

_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/145.0.0.0 Safari/537.36")
# How this client presents itself: Plex's web player. Sent as query
# parameters, which Plex accepts in place of headers — that matters because
# the player app on the box can only be given URLs, not headers.
_IDENTITY = {
    "X-Plex-Client-Identifier": str(uuid.uuid4()),
    "X-Plex-Device":            "Linux",
    "X-Plex-Language":          "en",
    "X-Plex-Platform":          "Chrome",
    "X-Plex-Platform-Version":  "145.0.0.0",
    "X-Plex-Product":           "Plex Mediaverse",
    "X-Plex-Provider-Version":  "6.5.0",
}
_DRM = {"X-Plex-Drm": "widevine:video"}

_token = {"value": None, "at": 0.0}
_token_lock = threading.Lock()


class PlexLiveError(RuntimeError):
    """Plex couldn't be reached, or wouldn't serve the channel."""


def _request(url: str, data: bytes | None = None, accept: str = "application/json") -> bytes:
    request = urllib.request.Request(url, data=data, headers={"User-Agent": _USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECS) as resp:
        return resp.read()


def _get_token(force: bool = False) -> str:
    with _token_lock:
        if force or not _token["value"] or time.time() - _token["at"] > TOKEN_MAX_AGE_SECS:
            try:
                body = _request(f"{TOKEN_URL}?{urllib.parse.urlencode(_IDENTITY)}", data=b"")
                _token["value"] = json.loads(body)["authToken"]
                _token["at"] = time.time()
            except Exception as exc:
                raise PlexLiveError(f"Plex gave no anonymous token: {exc}") from exc
        return _token["value"]


def _query(drm: bool, token: str) -> str:
    return urllib.parse.urlencode({**_IDENTITY, **(_DRM if drm else {}), "X-Plex-Token": token})


def _plex(path: str, drm: bool = True, data: bytes | None = None, accept: str = "application/json",
          extra: dict | None = None) -> bytes:
    """One call to Plex's guide provider, with one retry on a refused token."""
    for attempt in (0, 1):
        token = _get_token(force=attempt == 1)
        url = f"{EPG_HOST}{path}?{_query(drm, token)}"
        if extra:
            url += "&" + urllib.parse.urlencode(extra)
        try:
            return _request(url, data=data, accept=accept)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and attempt == 0:
                continue
            detail = ""
            try:
                detail = exc.read(200).decode("utf-8", "replace").strip()
            except Exception:
                pass
            raise PlexLiveError(f"Plex answered HTTP {exc.code} for {path}" + (f": {detail}" if detail else "")) from exc
        except Exception as exc:
            raise PlexLiveError(f"Plex is unreachable: {exc}") from exc
    raise PlexLiveError("Plex refused the request")


def _lineup(drm: bool) -> dict:
    root = ET.fromstring(_plex("/lineups/plex/channels", drm=drm, accept="application/xml"))
    return {c.attrib["id"]: c.attrib for c in root.iter("Channel") if c.attrib.get("id")}


def protected_channels() -> list:
    """The channels Plex lists only for a DRM-capable client: what its plain
    lineup lacks. [{id, grid_key, name, logo, summary}], by name."""
    plain = _lineup(drm=False)
    channels = [
        {
            "id":       cid,
            "grid_key": attrs.get("gridKey") or cid.rsplit("-", 1)[-1],
            "name":     attrs.get("title") or cid,
            "logo":     attrs.get("thumb") or "",
            "summary":  attrs.get("summary") or "",
        }
        for cid, attrs in _lineup(drm=True).items() if cid not in plain
    ]
    return sorted(channels, key=lambda c: c["name"].lower())


def playback(channel_id: str) -> dict:
    """Manifest + licence URL for one protected channel, ready to hand to a
    Widevine player. Tunes the channel first (Plex starts serving it on that)
    and confirms the manifest really comes back.
    """
    if not channel_id or not all(ch.isalnum() or ch == "-" for ch in channel_id):
        raise PlexLiveError(f"Not a Plex channel id: {channel_id}")
    _plex(f"/channels/{channel_id}/tune", data=b"")
    token = _get_token()
    query = _query(True, token)
    manifest = f"{EPG_HOST}/library/parts/{channel_id}.m3u8?includeAllStreams=1&{query}"
    try:
        if b"#EXTM3U" not in _request(manifest, accept="*/*")[:200]:
            raise PlexLiveError("Plex returned something that isn't a stream manifest")
    except urllib.error.HTTPError as exc:
        raise PlexLiveError(f"Plex has no stream for this channel right now (HTTP {exc.code})") from exc
    except PlexLiveError:
        raise
    except Exception as exc:
        raise PlexLiveError(f"Plex's stream manifest could not be fetched: {exc}") from exc
    return {
        "manifest_url": manifest,
        "license_url":  f"{EPG_HOST}/library/parts/{channel_id}/license?{query}",
        "name":         "",
        "fc_id":        None,
    }


def _xmltv_time(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def _programmes(channel: dict, xml_id: str, days: int) -> list:
    out, seen = [], set()
    today = datetime.date.today()
    for offset in range(days):
        date = (today + datetime.timedelta(days=offset)).isoformat()
        try:
            data = json.loads(_plex("/grid", extra={"channelGridKey": channel["grid_key"], "date": date}))
        except PlexLiveError:
            continue   # one missing day shouldn't cost the whole guide
        for item in (data.get("MediaContainer") or {}).get("Metadata") or []:
            show = item.get("grandparentTitle")
            title = show or item.get("title") or ""
            # The placeholder Plex pads the schedule with: the channel's own name.
            if not show and title.strip().lower() == channel["name"].strip().lower():
                continue
            for media in item.get("Media") or []:
                begins, ends = media.get("beginsAt"), media.get("endsAt")
                if not begins or not ends or (begins, ends) in seen:
                    continue
                seen.add((begins, ends))
                lines = [f'  <programme start="{_xmltv_time(begins)}" stop="{_xmltv_time(ends)}" channel={quoteattr(xml_id)}>']
                lines.append(f"    <title>{escape(title)}</title>")
                if show and item.get("title"):
                    lines.append(f"    <sub-title>{escape(item['title'])}</sub-title>")
                if item.get("summary"):
                    lines.append(f"    <desc>{escape(item['summary'])}</desc>")
                if item.get("year"):
                    lines.append(f"    <date>{int(item['year'])}</date>")
                if item.get("type") == "movie":
                    lines.append("    <category>Movie</category>")
                for genre in item.get("Genre") or []:
                    if genre.get("tag"):
                        lines.append(f"    <category>{escape(genre['tag'])}</category>")
                image = item.get("grandparentThumb") or item.get("thumb")
                if image:
                    lines.append(f"    <icon src={quoteattr(image)} />")
                season, episode = item.get("parentIndex"), item.get("index")
                if show and isinstance(season, int) and isinstance(episode, int):
                    lines.append(f'    <episode-num system="xmltv_ns">{season - 1}.{episode - 1}.</episode-num>')
                    lines.append(f'    <episode-num system="onscreen">S{season:02d}E{episode:02d}</episode-num>')
                if item.get("contentRating"):
                    lines.append(f"    <rating><value>{escape(item['contentRating'])}</value></rating>")
                lines.append("  </programme>")
                out.append((begins, "\n".join(lines)))
    return [text for _, text in sorted(out)]


def xml_id(channel: dict) -> str:
    """The id a channel has in both the playlist (tvg-id) and the guide."""
    return f"plexlive.{channel['grid_key']}"


def guide_xmltv(days: int = GUIDE_DAYS) -> str:
    """XMLTV for every protected channel, [days] days from today."""
    channels = protected_channels()
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="Broadcast Hub (Plex live)">']
    for ch in channels:
        lines.append(f"  <channel id={quoteattr(xml_id(ch))}>")
        lines.append(f"    <display-name>{escape(ch['name'])}</display-name>")
        if ch["logo"]:
            lines.append(f"    <icon src={quoteattr(ch['logo'])} />")
        lines.append("  </channel>")
    for ch in channels:
        lines += _programmes(ch, xml_id(ch), days)
    lines.append("</tv>")
    return "\n".join(lines) + "\n"
