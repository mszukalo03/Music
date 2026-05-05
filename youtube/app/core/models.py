from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence


class InputFormatError(ValueError):
    """Raised when the input JSON does not follow the expected structure."""


def _as_mapping(value: Any, *, where: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise InputFormatError(
        f"{where}: expected an object/dict, got {type(value).__name__}"
    )


def _as_sequence(value: Any, *, where: str) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    raise InputFormatError(
        f"{where}: expected an array/list, got {type(value).__name__}"
    )


def _get_str(
    obj: Mapping[str, Any],
    key: str,
    *,
    where: str,
    required: bool = False,
    default: Optional[str] = None,
) -> Optional[str]:
    if key not in obj:
        if required:
            raise InputFormatError(f"{where}: missing required field '{key}'")
        return default
    val = obj.get(key)
    if val is None:
        if required:
            raise InputFormatError(f"{where}: field '{key}' cannot be null")
        return default
    if isinstance(val, str):
        s = val.strip()
        return s if s else (None if not required else "")
    # allow numbers/bools to be coerced to string? usually not in production inputs
    raise InputFormatError(
        f"{where}: field '{key}' must be a string, got {type(val).__name__}"
    )


def _truthy_flag(obj: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    if key not in obj:
        return default
    val = obj.get(key)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "y", "on"}
    raise InputFormatError(f"field '{key}' must be a boolean-ish value")


@dataclass(frozen=True)
class TrackItem:
    """
    Represents a single track request (preferred input type).

    Fields are intentionally minimal and tolerant:
    - `title` is required.
    - `artist`, `album`, `genre` are optional hints used for searching/matching.
    """

    title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    genre: Optional[str] = None

    # Optional stable id from your JSON, if you want to control dedupe externally.
    external_id: Optional[str] = None

    # If true, skip Lidarr and go straight to YouTube (rare but useful).
    force_youtube: bool = False

    def display_name(self) -> str:
        if self.artist:
            return f"{self.artist} - {self.title}"
        return self.title

    def search_query(self) -> str:
        """
        Best-effort query string for Lidarr/YouTube.
        Keep it simple; downstream clients can refine further.
        """
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title


@dataclass(frozen=True)
class AlbumItem:
    """
    Represents an album request.

    - `title` and `artist` are required to search reliably.
    - `year` is optional and can help disambiguate.
    """

    title: str
    artist: str
    year: Optional[int] = None
    genre: Optional[str] = None

    external_id: Optional[str] = None
    force_youtube: bool = False

    def display_name(self) -> str:
        if self.year is not None:
            return f"{self.artist} - {self.title} ({self.year})"
        return f"{self.artist} - {self.title}"

    def search_query(self) -> str:
        if self.year is not None:
            return f"{self.artist} {self.title} {self.year}"
        return f"{self.artist} {self.title}"


@dataclass(frozen=True)
class InputDocument:
    """
    Parsed input file.
    """

    tracks: List[TrackItem]
    albums: List[AlbumItem]

    # Any additional top-level metadata
    meta: Dict[str, Any]

    def all_items_count(self) -> int:
        return len(self.tracks) + len(self.albums)


def _parse_int(value: Any, *, where: str, key: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise InputFormatError(f"{where}: field '{key}' must be an integer, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError as e:
            raise InputFormatError(f"{where}: field '{key}' must be an integer") from e
    raise InputFormatError(
        f"{where}: field '{key}' must be an integer, got {type(value).__name__}"
    )


import logging

LOG = logging.getLogger(__name__)

def parse_input_document(data: Any) -> InputDocument:
    """
    Parse the JSON content (already loaded) into typed items.
    
    Tolerates individual item failures by logging them and proceeding.

    Supported top-level shapes:
    1) {"items":[...]}  (legacy/compact)
    2) {"tracks":[...], "albums":[...]} (recommended)
    3) Direct list: [...] (treated as items)
    """
    meta: Dict[str, Any] = {}

    if isinstance(data, list):
        items = data
        root_where = "$"
        root_obj: Mapping[str, Any] = {}
    else:
        root_obj = _as_mapping(data, where="$")
        root_where = "$"
        # capture meta, excluding known keys
        meta = dict(root_obj)
        meta.pop("items", None)
        meta.pop("tracks", None)
        meta.pop("albums", None)
        if "items" in root_obj:
            items = list(_as_sequence(root_obj["items"], where="$.items"))
        else:
            items = []
    tracks: List[TrackItem] = []
    albums: List[AlbumItem] = []

    # Recommended split format
    if not isinstance(data, list) and ("tracks" in root_obj or "albums" in root_obj):
        raw_tracks = root_obj.get("tracks", [])
        raw_albums = root_obj.get("albums", [])
        
        for idx, raw in enumerate(_as_sequence(raw_tracks, where="$.tracks")):
            try:
                tracks.append(_parse_track_item(raw, where=f"$.tracks[{idx}]"))
            except InputFormatError as e:
                LOG.warning(f"Skipping invalid track at index {idx}: {e}")

        for idx, raw in enumerate(_as_sequence(raw_albums, where="$.albums")):
            try:
                albums.append(_parse_album_item(raw, where=f"$.albums[{idx}]"))
            except InputFormatError as e:
                LOG.warning(f"Skipping invalid album at index {idx}: {e}")

        return InputDocument(tracks=tracks, albums=albums, meta=meta)

    # Legacy/unified items format
    for idx, raw in enumerate(items):
        where = f"{root_where}.items[{idx}]"
        try:
            obj = _as_mapping(raw, where=where)

            # Heuristic:
            # - If it has "song"/"track" -> track
            # - Else if it has both "artist" and ("album" or "title") and does NOT have song -> album
            song = _get_str(obj, "song", where=where, required=False)
            track_title = _get_str(obj, "track", where=where, required=False)
            title = _get_str(obj, "title", where=where, required=False)
            artist = _get_str(obj, "artist", where=where, required=False)
            album = _get_str(obj, "album", where=where, required=False)

            is_track = bool(song or track_title) or (
                title is not None and (album is None or artist is None)
            )
            # If title+artist+album and no song/track, treat as album
            is_album = (
                (song is None and track_title is None)
                and bool(artist)
                and bool(album or title)
            )

            if is_album and not is_track:
                albums.append(_parse_album_item(obj, where=where))
            else:
                tracks.append(_parse_track_item(obj, where=where))
        except InputFormatError as e:
            LOG.warning(f"Skipping invalid item at index {idx}: {e}")

    return InputDocument(tracks=tracks, albums=albums, meta=meta)


def _parse_track_item(raw: Any, *, where: str) -> TrackItem:
    obj = _as_mapping(raw, where=where)
    title = (
        _get_str(obj, "song", where=where, required=False)
        or _get_str(obj, "track", where=where, required=False)
        or _get_str(obj, "title", where=where, required=True)
    )
    artist = _get_str(obj, "artist", where=where, required=False)
    album = _get_str(obj, "album", where=where, required=False)
    genre = _get_str(obj, "genre", where=where, required=False)

    external_id = _get_str(
        obj, "external_id", where=where, required=False, default=None
    ) or _get_str(obj, "id", where=where, required=False, default=None)

    force_youtube = _truthy_flag(obj, "force_youtube", default=False)

    return TrackItem(
        title=title,
        artist=artist,
        album=album,
        genre=genre,
        external_id=external_id,
        force_youtube=force_youtube,
    )


def _parse_album_item(raw: Any, *, where: str) -> AlbumItem:
    obj = _as_mapping(raw, where=where)

    # Accept both "album" and "title" for album name
    title = _get_str(obj, "album", where=where, required=False) or _get_str(
        obj, "title", where=where, required=True
    )
    artist = _get_str(obj, "artist", where=where, required=True)
    genre = _get_str(obj, "genre", where=where, required=False)

    year = _parse_int(obj.get("year"), where=where, key="year")

    external_id = _get_str(
        obj, "external_id", where=where, required=False, default=None
    ) or _get_str(obj, "id", where=where, required=False, default=None)

    force_youtube = _truthy_flag(obj, "force_youtube", default=False)

    return AlbumItem(
        title=title,
        artist=artist,
        year=year,
        genre=genre,
        external_id=external_id,
        force_youtube=force_youtube,
    )
