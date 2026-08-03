"""HLS playlist parsing, track selection, and the walk down from a master.

The parser is a single pass that classifies as it goes: a master playlist names
other playlists and no segments, a media playlist names segments. Counting both
is cheaper than two parsers and it lets a mislabelled file (segments *and*
variant URIs) be treated as media, which is the safe reading — a playlist with
segments can be played.

Encryption tags need the same care in both places. A master carries
``EXT-X-SESSION-KEY``, a media playlist carries ``EXT-X-KEY``, and the union of
the two is the real protection picture: the master frequently declares fewer
KIDs than the stream uses, so stopping at the master hands back a key request
for the video track and nothing for audio.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urljoin

from . import netio, protection
from .errors import SourceError
from .protection import Protection

VIDEO = "video"
AUDIO = "audio"
SUBTITLE = "subtitle"

# Matched as a substring of the whole attribute list rather than an exact
# KEYFORMAT comparison: servers spell the UUID with and without a urn prefix,
# and the system ID itself is the part that identifies Widevine.
_WIDEVINE_MARK = "edef8ba9"
_FAIRPLAY_MARK = "streamingkeydelivery"

_KEY_LINE = re.compile(r"\A#EXT-X-(?:SESSION-)?KEY:(?P<attrs>.+)\Z")
_DATA_URI = re.compile(r'URI="data:[^;,"]*(?:;base64)?,(?P<payload>[^"]+)"')
_FAIRPLAY_KID = re.compile(r"keyId=([0-9a-fA-F]+)", re.I)
_TARGET_DURATION = re.compile(r"^#EXT-X-TARGETDURATION:(\d+)", re.M)
_MEDIA_SEQUENCE = re.compile(r"^#EXT-X-MEDIA-SEQUENCE:(\d+)", re.M)

_SEGMENT_SUFFIXES = (".ts", ".mp4", ".m4s", ".m4a", ".aac", ".vtt")


def _attribute(attrs: str, name: str) -> str:
    """Read one attribute value, quoted or bare.

    Anchored on a comma or start-of-list so that ``BANDWIDTH`` does not also
    match inside ``AVERAGE-BANDWIDTH``; an unanchored search reads the average
    as the peak and silently reorders variants by the wrong number.
    """
    pattern = rf'(?:\A|,)\s*{re.escape(name)}=(?:"([^"]*)"|([^,]*))'
    found = re.search(pattern, attrs)
    if not found:
        return ""
    return (found.group(1) if found.group(1) is not None else found.group(2) or "").strip()


def _path_of(line: str) -> str:
    return line.split("?", 1)[0].split("#", 1)[0].lower()


def _is_segment(line: str) -> bool:
    if not line or line.startswith("#"):
        return False
    return _path_of(line).endswith(_SEGMENT_SUFFIXES)


def _is_playlist(line: str) -> bool:
    if not line or line.startswith("#"):
        return False
    return ".m3u8" in _path_of(line)


@dataclass(frozen=True, slots=True)
class Rendition:
    """One selectable track in a master playlist."""

    url: str
    kind: str = VIDEO
    bandwidth: int = 0
    resolution: str = ""
    group_id: str = ""
    name: str = ""
    language: str = ""
    default: bool = False

    def describe(self) -> str:
        if self.kind == VIDEO:
            detail = self.resolution or f"{self.bandwidth // 1000} kbps"
            return f"video {detail}"
        label = self.language or self.name or self.group_id or "-"
        return f"{self.kind} {label}"


@dataclass(frozen=True, slots=True)
class Playlist:
    url: str
    is_master: bool
    live: bool
    segment_count: int
    renditions: tuple[Rendition, ...]
    protection: Protection
    target_duration: int = 0
    media_sequence: int = 0

    @property
    def audio_renditions(self) -> tuple[Rendition, ...]:
        return tuple(r for r in self.renditions if r.kind == AUDIO)

    @property
    def video_renditions(self) -> tuple[Rendition, ...]:
        return tuple(r for r in self.renditions if r.kind == VIDEO)


def parse(text: str, url: str = "") -> Playlist:
    """Parse playlist text. Pure: never fetches anything."""
    if "#EXTM3U" not in text:
        where = f" from {url}" if url else ""
        raise SourceError(
            f"the response{where} is not an HLS playlist",
            remedy="Check the URL — a login page or an API error page also returns 200.",
        )

    lines = [line.strip() for line in text.splitlines()]
    renditions: list[Rendition] = []
    payloads: list[str] = []
    kids: set[str] = set()
    fairplay: set[str] = set()
    unreadable = 0
    segment_count = 0
    pending_stream_inf: str | None = None

    for index, line in enumerate(lines):
        if not line:
            continue

        if _is_segment(line):
            segment_count += 1
            continue

        key_line = _KEY_LINE.match(line)
        if key_line:
            attrs = key_line.group("attrs")
            if _WIDEVINE_MARK in attrs.lower():
                found = _DATA_URI.search(attrs)
                if not found:
                    unreadable += 1
                    continue
                payload = found.group("payload")
                read = protection.read_kids_quietly(payload)
                if not read:
                    unreadable += 1
                    continue
                if payload not in payloads:
                    payloads.append(payload)
                kids.update(read)
            elif _FAIRPLAY_MARK in attrs.lower():
                # Recorded for diagnosis only. FairPlay KIDs never take part in
                # coverage: no Widevine key answers to them, and counting them
                # turns a healthy key set into a permanent failure.
                fairplay.update(m.lower() for m in _FAIRPLAY_KID.findall(attrs))
            continue

        if line.startswith("#EXT-X-MEDIA:"):
            attrs = line[len("#EXT-X-MEDIA:") :]
            target = _attribute(attrs, "URI")
            if not target:
                continue
            media_type = _attribute(attrs, "TYPE").upper()
            renditions.append(
                Rendition(
                    url=urljoin(url, target) if url else target,
                    kind=AUDIO if media_type == "AUDIO" else SUBTITLE,
                    group_id=_attribute(attrs, "GROUP-ID"),
                    name=_attribute(attrs, "NAME"),
                    language=_attribute(attrs, "LANGUAGE"),
                    default=_attribute(attrs, "DEFAULT").upper() == "YES",
                )
            )
            continue

        if line.startswith("#EXT-X-STREAM-INF:"):
            pending_stream_inf = line[len("#EXT-X-STREAM-INF:") :]
            continue

        if pending_stream_inf is not None and _is_playlist(line):
            attrs = pending_stream_inf
            pending_stream_inf = None
            average = _attribute(attrs, "AVERAGE-BANDWIDTH")
            peak = _attribute(attrs, "BANDWIDTH")
            chosen = average or peak
            renditions.append(
                Rendition(
                    url=urljoin(url, line) if url else line,
                    kind=VIDEO,
                    bandwidth=int(chosen) if chosen.isdigit() else 0,
                    resolution=_attribute(attrs, "RESOLUTION"),
                )
            )
            continue

    if unreadable and not payloads:
        raise SourceError(
            f"{url or 'the playlist'} advertises Widevine but no PSSH could be read",
            remedy="The manifest may be truncated. Re-fetch it.",
        )

    variant_count = sum(1 for line in lines if _is_playlist(line))
    is_master = variant_count > 0 and segment_count == 0

    target = _TARGET_DURATION.search(text)
    sequence = _MEDIA_SEQUENCE.search(text)

    return Playlist(
        url=url,
        is_master=is_master,
        live="#EXT-X-ENDLIST" not in text,
        segment_count=segment_count,
        renditions=tuple(renditions),
        protection=Protection(
            payloads=tuple(payloads),
            advertised=frozenset(kids),
            fairplay_kids=frozenset(fairplay),
        ),
        target_duration=int(target.group(1)) if target else 0,
        media_sequence=int(sequence.group(1)) if sequence else 0,
    )


def fetch(url: str, *, timeout: float = netio.DEFAULT_TIMEOUT) -> Playlist:
    try:
        reply = netio.get(url, timeout=timeout)
    except netio.TransportError as exc:
        raise SourceError(
            f"could not reach {url}: {exc}",
            remedy="Check connectivity, and HTTPS_PROXY if you are behind one.",
        ) from exc
    if not reply.ok:
        raise SourceError(
            f"{url} returned HTTP {reply.status}",
            remedy="A live manifest expires. Re-copy the URL from the player.",
        )
    return parse(reply.text, url)


def pick_tracks(playlist: Playlist) -> tuple[Rendition, ...]:
    """Predict which renditions the downloader's automatic selection will take.

    Highest-bandwidth video plus one audio track. Subtitles are left out: they
    are unencrypted and contribute no KID, so including them can only widen the
    required set with KIDs no license will ever return.
    """
    chosen: list[Rendition] = []
    videos = playlist.video_renditions
    audios = playlist.audio_renditions
    if videos:
        chosen.append(max(videos, key=lambda r: (r.bandwidth, r.resolution)))
    if audios:
        chosen.append(next((a for a in audios if a.default), audios[0]))
    return tuple(chosen)


SELECTION_FILE = "meta_selected.json"

_AUDIO_FIELDS = (("group_id", "GroupId"), ("name", "Name"), ("language", "Language"))
_VIDEO_FIELDS = (("bandwidth", "Bandwidth"), ("resolution", "Resolution"))
_UNPROTECTED_MEDIA = frozenset({"SUBTITLES", "CLOSED-CAPTIONS"})


def read_reported_selection(shard_root: Path) -> list[dict] | None:
    """The downloader's own record of what it chose, if it has written one.

    Written with a BOM, which ``json.loads`` rejects, hence ``utf-8-sig``.
    """
    path = shard_root / SELECTION_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, list) else None


def _match_score(rendition: Rendition, entry: dict) -> int | None:
    media_type = str(entry.get("MediaType") or "").upper()
    if media_type in _UNPROTECTED_MEDIA:
        return None
    if media_type == "AUDIO":
        if rendition.kind != AUDIO:
            return None
        fields = _AUDIO_FIELDS
    elif media_type in ("", "VIDEO"):
        if rendition.kind != VIDEO:
            return None
        fields = _VIDEO_FIELDS
    else:
        return None

    score = 0
    for attribute, reported_key in fields:
        reported = entry.get(reported_key)
        mine = getattr(rendition, attribute)
        if reported in (None, "", 0) or mine in ("", 0):
            continue  # absent on either side is no evidence, not a mismatch
        if str(reported) != str(mine):
            return None
        score += 1
    return score or None


def align_selection(playlist: Playlist, reported: list[dict] | None) -> tuple[Rendition, ...] | None:
    """Map the downloader's reported tracks back onto manifest renditions.

    All or nothing. A partial mapping produces a *narrower* required set than
    the truth, which lets the coverage gate pass a key set missing the audio
    key — strictly worse than not consulting the file at all. So one unmatched
    entry discards the whole mapping and the caller falls back to prediction.
    """
    if not reported:
        return None
    matched: list[Rendition] = []
    for entry in reported:
        if str(entry.get("MediaType") or "").upper() in _UNPROTECTED_MEDIA:
            continue
        scored = [
            (score, r)
            for r in playlist.renditions
            if (score := _match_score(r, entry)) is not None
        ]
        if not scored:
            return None
        matched.append(max(scored, key=lambda pair: pair[0])[1])
    return tuple(matched) or None


@dataclass(frozen=True, slots=True)
class Source:
    """A resolved stream: the playlist plus the protection picture behind it."""

    playlist: Playlist
    protection: Protection
    tracks: tuple[Rendition, ...]

    @property
    def url(self) -> str:
        return self.playlist.url

    @property
    def live(self) -> bool:
        return self.playlist.live

    @property
    def segment_ms(self) -> int:
        """Declared segment duration in milliseconds, 0 when unknown."""
        return self.playlist.target_duration * 1000


def resolve(
    url: str,
    *,
    selection: tuple[Rendition, ...] | None = None,
    timeout: float = netio.DEFAULT_TIMEOUT,
) -> Source:
    """Fetch a playlist and, when it is a master, the tracks below it.

    The walk down is what makes the protection picture complete, and it is also
    where ``EXT-X-TARGETDURATION`` comes from: that tag only ever appears in a
    media playlist, so a master on its own reports no segment duration and every
    consumer of that number would fall back to a guess.
    """
    root = fetch(url, timeout=timeout)
    combined = root.protection
    needed: set[str] = set()

    if root.is_master:
        tracks = selection or pick_tracks(root)
        for rendition in tracks:
            try:
                child = fetch(rendition.url, timeout=timeout)
            except SourceError:
                # One unreachable variant is not a reason to abandon the run;
                # the gate below still refuses to proceed on an empty picture.
                continue
            combined = combined.merge(child.protection)
            needed |= child.protection.advertised
            if not root.target_duration and child.target_duration:
                root = replace(root, target_duration=child.target_duration)
    else:
        tracks = ()
        needed |= root.protection.advertised

    resolved = Protection(
        payloads=combined.payloads,
        advertised=combined.advertised,
        needed=frozenset(needed),
        fairplay_kids=combined.fairplay_kids,
    )
    return Source(playlist=root, protection=resolved, tracks=tracks)
