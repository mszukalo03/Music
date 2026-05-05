
import logging
import shutil
import acoustid
import musicbrainzngs
import mutagen
import threading
import time
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TYER, TCON, TRCK, USLT, APIC, encoding
from mutagen.mp4 import MP4, MP4Tags, MP4Cover
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
import requests
import lyricsgenius


log = logging.getLogger(__name__)

# Initialize MusicBrainz
# TODO: Move user agent to config
musicbrainzngs.set_useragent("HomeLabYoutubeDownloader", "0.1", "contact@example.com")
# Set rate limit to 1 request per second to be safe
musicbrainzngs.set_rate_limit(1.0)

class MetadataError(RuntimeError):
    pass

_acoustid_lock = threading.Lock()
_last_acoustid_call = 0.0

def generate_fingerprint(path: str) -> Tuple[float, str]:
    """
    Generate an AcoustID fingerprint for the given audio file using fpcalc.
    Returns (duration, fingerprint).
    """
    if not shutil.which("fpcalc"):
        raise MetadataError("fpcalc not found on PATH. Install libchromaprint-tools.")

    try:
        duration, fingerprint = acoustid.fingerprint_file(path)
        return duration, fingerprint
    except acoustid.FingerprintGenerationError as e:
        raise MetadataError(f"Failed to generate fingerprint: {e}")

def lookup_track(fingerprint: str, duration: float, meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Lookup metadata using AcoustID and MusicBrainz.
    API Key should be in environment ACOUSTID_API_KEY.
    """
    # TODO: Get API key from settings/env
    import os
    apikey = os.getenv("ACOUSTID_API_KEY")
    if not apikey:
        log.warning("No ACOUSTID_API_KEY set. Skipping lookup.")
        return {}

    global _last_acoustid_call
    with _acoustid_lock:
        # Rate limit: 3 calls per second max (approx 0.33s delay)
        now = time.time()
        diff = now - _last_acoustid_call
        if diff < 0.33:
            time.sleep(0.33 - diff)
        _last_acoustid_call = time.time()

    try:
        # Initial lookup
        # meta parameter hints: recordingids, sources, releases, tracks, compress, usermeta, recordings
        resp = acoustid.lookup(apikey, fingerprint, duration, meta=["recordings", "releases", "tracks"])
    except acoustid.WebServiceError as e:
        log.warning(f"AcoustID lookup failed: {e}")
        return {}

    if not resp or "results" not in resp or not resp["results"]:
        return {}

    # Best match logic can be complex. For now, take the highest score.
    best = resp["results"][0]
    score = best.get("score", 0)
    
    # Extract details
    result = {
        "acoustid_fingerprint": fingerprint,
        "acoustid_score": score,
        "acoustid_id": best.get("id"),
    }

    if best.get("recordings"):
        rec = best["recordings"][0]
        result["mb_track_id"] = rec.get("id")
        result["title"] = rec.get("title")
        
        if rec.get("artists"):
            # Artists list
            artists = [a.get("name") for a in rec["artists"]]
            result["artist"] = ", ".join(artists)
            if rec["artists"][0].get("id"):
                result["mb_artist_id"] = rec["artists"][0]["id"]

        if rec.get("releases"):
            rel = rec["releases"][0]
            result["album"] = rel.get("title")
            # attempt date (sometimes partial)
            if rel.get("date"):
                 result["date"] = rel["date"]

    return result

def apply_tags(path: str, metadata: Dict[str, Any]) -> None:
    """
    Write tags to the file. Supports key fields, including lyrics and album art.
    """
    if not Path(path).exists():
        raise MetadataError(f"File not found: {path}")

    # Determine format
    ext = Path(path).suffix.lower()
    
    try:
        # First use EasyID3/EasyMP4 for common text tags
        f = mutagen.File(path, easy=True)
        if f is None:
            if ext == ".mp3":
                f = EasyID3(path)
            else:
                f = mutagen.File(path)
        
        if f is None:
            log.warning(f"Could not open file for tagging: {path}")
            return

        if metadata.get("title"):
            f["title"] = metadata["title"]
        if metadata.get("artist"):
            f["artist"] = metadata["artist"]
        if metadata.get("album"):
            f["album"] = metadata["album"]
        if metadata.get("date"):
            f["date"] = metadata["date"]
        if metadata.get("genre"):
            f["genre"] = metadata["genre"]
            
        f.save()
        
        # Now apply advanced tags (Lyrics, Cover Art) using specific format handlers
        if ext == ".mp3":
            audio = ID3(path)
            if metadata.get("lyrics"):
                audio.add(USLT(encoding=encoding.UTF8, lang='eng', desc='', text=metadata["lyrics"]))
            if metadata.get("album_art_data"):
                # album_art_data should be raw bytes
                audio.add(APIC(
                    encoding=encoding.LATIN1, 
                    mime='image/jpeg', 
                    type=3, # 3 is for the cover(front) image
                    desc=u'Cover',
                    data=metadata["album_art_data"]
                ))
            audio.save()
            
        elif ext in [".m4a", ".mp4"]:
            audio = MP4(path)
            if metadata.get("lyrics"):
                audio["\xa9lyr"] = metadata["lyrics"]
            if metadata.get("album_art_data"):
                # MP4Cover expects a specific format
                audio["covr"] = [MP4Cover(metadata["album_art_data"], imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()

        log.info(f"Tags saved for {path}")

    except Exception as e:
        log.error(f"Failed to apply tags to {path}: {e}")

def fetch_lyrics(artist: str, title: str, api_key: Optional[str] = None) -> Optional[str]:
    """
    Fetch lyrics for the track.
    If API key is provided, use Genius API. Otherwise return None.
    """
    if not api_key:
        return None
    try:
        genius = lyricsgenius.Genius(api_key, verbose=False, remove_section_headers=False)
        song = genius.search_song(title, artist)
        if song and song.lyrics:
            return song.lyrics
    except Exception as e:
        log.warning(f"Failed to fetch lyrics for {artist} - {title}: {e}")
    return None

def fetch_album_art(mb_release_id: str) -> Optional[bytes]:
    """
    Fetch album art from Cover Art Archive using MusicBrainz Release ID.
    Returns the raw image bytes (JPEG).
    """
    if not mb_release_id:
        return None
    try:
        # Rate limit friendly URL
        url = f"http://coverartarchive.org/release/{mb_release_id}/front"
        headers = {"User-Agent": "HomeLabYoutubeDownloader/0.1 ( contact@example.com )"}
        resp = requests.get(url, headers=headers, allow_redirects=True, timeout=10)
        if resp.status_code == 200:
            return resp.content
    except Exception as e:
        log.warning(f"Failed to fetch album art for release {mb_release_id}: {e}")
    return None


def search_recording(artist: str, title: str, album: Optional[str] = None) -> Dict[str, Any]:
    """
    Search MusicBrainz for a recording.
    Returns the best match with mb_track_id, duration, etc.
    """
    if not artist or not title:
        return {}

    query = f'artist:"{artist}" AND recording:"{title}"'
    if album:
        query += f' AND release:"{album}"'
        
    try:
        # musicbrainzngs should already be configured (useragent/ratelimit) at module level
        res = musicbrainzngs.search_recordings(query=query, limit=5)
    except Exception as e:
        log.warning(f"MusicBrainz search failed: {e}")
        return {}
        
    if not res.get("recording-list"):
        return {}
        
    # Pick the best match. 
    # For now, just take the first one, or maybe prefer one with an ISRC or duration?
    # The search score is usually decent.
    best = res["recording-list"][0]
    
    result = {
        "mb_track_id": best.get("id"),
        "title": best.get("title"),
        "score": best.get("ext:score", 0),
    }
    
    # Duration (milliseconds in MB)
    if best.get("length"):
        try:
            result["duration"] = int(best["length"]) / 1000.0
        except:
            pass
            
    # Artist credit
    if best.get("artist-credit"):
        # simple join
        names = [ac.get("name") or ac.get("artist", {}).get("name") for ac in best["artist-credit"] if isinstance(ac, dict)]
        # sometimes it's a string?
        # musicbrainzngs returns list of dicts or strings
        clean_names = []
        for ac in best["artist-credit"]:
            if isinstance(ac, dict):
                 if "artist" in ac:
                     clean_names.append(ac["artist"]["name"])
                 elif "name" in ac:
                     clean_names.append(ac["name"])
            elif isinstance(ac, str):
                clean_names.append(ac)
                
        if clean_names:
            result["artist"] = ", ".join(clean_names)
            # Take first artist ID
            if isinstance(best["artist-credit"][0], dict) and "artist" in best["artist-credit"][0]:
                 result["mb_artist_id"] = best["artist-credit"][0]["artist"]["id"]

    # Release (Album)
    if best.get("release-list"):
        # Prefer the one matching our album query if possible, otherwise first
        chosen_release = best["release-list"][0]
        if album:
            for r in best["release-list"]:
                 if r.get("title", "").lower() == album.lower():
                     chosen_release = r
                     break
        
        result["album"] = chosen_release.get("title")
        if chosen_release.get("date"):
            result["date"] = chosen_release.get("date")
        if chosen_release.get("id"):
            result["mb_release_id"] = chosen_release.get("id")
            
    return result
