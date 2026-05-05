from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app.config import Settings
from app.database import Database
from app.core import metadata

LOG = logging.getLogger(__name__)

_AUDIO_EXTS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".wav",
    ".alac",
    ".aiff",
    ".ogg",
    ".opus",
    ".wma",
    ".mp4",  # sometimes music files are in mp4/m4b containers
    ".mkv",  # rare, but some users keep audio in mkv
}

def _normalize_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _looks_like_audio_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in _AUDIO_EXTS

def _scan_subtree(path: str) -> List[str]:
    """
    Worker function for ProcessPoolExecutor.
    Scans a directory recursively for audio files.
    argument should be a string to ensure pickling works smoothly across simple executor contexts.
    """
    results = []
    p_obj = Path(path)
    if not p_obj.exists():
        return results
    
    try:
        for p in p_obj.rglob("*"):
            if p.is_file() and p.suffix.lower() in _AUDIO_EXTS:
                results.append(str(p))
    except Exception:
        # Swallow errors in workers to avoid crashing the pool
        pass
    return results

class LibraryScanner:
    def __init__(self, settings: Settings, library_dir: Optional[Path]):
        self.settings = settings
        self.library_dir = library_dir
        self.lock = threading.RLock()
        self._scanned = False
        self.last_scanned = 0.0

    def scan(self) -> None:
        """
        Builds the in-memory index of the library.
        Uses ProcessPoolExecutor for parallel scanning of subdirectories.
        """
        if not self.library_dir or not self.library_dir.exists():
            LOG.warning("Library directory not valid, skipping scan.")
            return

        LOG.info(f"Scanning library: {self.library_dir}")

        all_paths: List[str] = []
        
        # 1. Identify top-level subdirectories and root files
        try:
            subdirs = [str(x) for x in self.library_dir.iterdir() if x.is_dir()]
            # Root files we handle directly in this process
            root_files = [x for x in self.library_dir.iterdir() if x.is_file() and _looks_like_audio_file(x)]
        except OSError as e:
            LOG.error(f"Failed to access library root: {e}")
            return

        all_paths.extend(str(p) for p in root_files)

        # 2. Parallel scan
        if subdirs:
            # Adjust max_workers as needed, default is usually CPU count
            with ProcessPoolExecutor() as executor:
                # Map returns an iterator of results in order
                nested_results = executor.map(_scan_subtree, subdirs)
                for res_list in nested_results:
                    all_paths.extend(res_list)

        # 3. DB Sync
        active_paths = []
        db = Database(self.settings)
        db.connect()
        try:
            existing_tracks = db.get_all_library_tracks()
            db_map = {row['file_path']: row['file_mtime'] for row in existing_tracks}
            
            new_or_modified = []
            
            for p in all_paths:
                path_obj = Path(p)
                try:
                    mtime = path_obj.stat().st_mtime
                except Exception:
                    continue
                
                active_paths.append(p)
                db_mtime = db_map.get(p)
                if db_mtime is None or mtime > db_mtime:
                    new_or_modified.append((p, mtime))
                    
            # Prune deleted files
            deleted_count = db.delete_library_tracks_not_in(active_paths)
            if deleted_count > 0:
                LOG.info(f"Removed {deleted_count} deleted tracks from database.")
                
            # Process new or modified files
            if new_or_modified:
                LOG.info(f"Processing {len(new_or_modified)} new or modified tracks for library sync...")
                for p, mtime in new_or_modified:
                    self._process_and_upsert_file(db, p, mtime)
                    
        finally:
            db.close()
            
        import time 
        now = time.time()
        with self.lock:
            self._scanned = True
            self.last_scanned = now
        
        LOG.info(f"Library DB sync complete. Checked {len(active_paths)} active files.")

    def _process_and_upsert_file(self, db: Database, path: str, mtime: float):
        try:
            # Generate fingerprint and get basic info
            duration, fingerprint = metadata.generate_fingerprint(path)
            
            # Lookup metadata via AcoustID/MusicBrainz
            lookup_meta = {}
            mb_data = metadata.lookup_track(fingerprint, duration, lookup_meta)
            
            # We also might want to read existing ID3 tags just in case
            import mutagen
            from mutagen.easyid3 import EasyID3
            f = mutagen.File(path, easy=True)
            if f is None and Path(path).suffix.lower() == ".mp3":
                f = EasyID3(path)
                
            file_title = f.get('title', [None])[0] if f else None
            file_artist = f.get('artist', [None])[0] if f else None
            file_album = f.get('album', [None])[0] if f else None
            file_date = f.get('date', [None])[0] if f else None
            
            # Fallback to filename if all else fails
            if not file_title and not mb_data.get('title'):
                file_title = Path(path).stem

            track_data = {
                "file_path": path,
                "title": mb_data.get('title') or file_title,
                "artist": mb_data.get('artist') or file_artist,
                "album": mb_data.get('album') or file_album,
                "genre": None, # Could extract if needed
                "release_date": mb_data.get('date') or file_date,
                "duration": duration,
                "mb_track_id": mb_data.get('mb_track_id'),
                "mb_artist_id": mb_data.get('mb_artist_id'),
                "acoustid_fingerprint": fingerprint,
                "lyrics": None, # We don't fetch lyrics proactively for full lib scan to save time, unless requested
                "file_mtime": mtime
            }
            
            db.upsert_library_track(track_data)
            
        except Exception as e:
            LOG.warning(f"Failed to process library file {path}: {e}")
            # Insert a basic record so we don't retry every time
            basic_data = {
                "file_path": path,
                "title": Path(path).stem,
                "artist": None, "album": None, "genre": None, "release_date": None,
                "duration": 0.0, "mb_track_id": None, "mb_artist_id": None,
                "acoustid_fingerprint": None, "lyrics": None, "file_mtime": mtime
            }
            db.upsert_library_track(basic_data)

    def scan_if_needed(self, interval_seconds: int = 300) -> None:
        """
        Triggers a scan if the last scan was older than interval_seconds.
        """
        import time
        if not self._scanned or (time.time() - self.last_scanned > interval_seconds):
            self.scan()

    def _ensure_scanned(self):
        if not self._scanned:
             self.scan()

    def has_track(self, db: Database, artist: Optional[str], title: str) -> Tuple[bool, Dict[str, Any]]:
        """
        Check for a track in the library database.
        """
        self._ensure_scanned()
        
        title_n = _normalize_text(title)
        artist_n = _normalize_text(artist or "")

        if not title_n:
            return (False, {"reason": "missing_title"})

        title_tokens = set(title_n.split(" "))
        artist_tokens = set(artist_n.split(" ")) if artist_n else set()

        try:
            with db.conn.cursor() as cur:
                cur.execute("SELECT file_path, title, artist FROM library_tracks")
                tracks = cur.fetchall()
                
            for file_path, t_title, t_artist in tracks:
                blob = _normalize_text(f"{t_title} {t_artist} {Path(file_path).stem}")
                blob_tokens = set(blob.split(" "))
                
                if not title_tokens.issubset(blob_tokens):
                    continue
                
                if artist_tokens:
                    if len(artist_tokens) == 1:
                        if not artist_tokens.issubset(blob_tokens):
                            continue
                    else:
                        if len(artist_tokens & blob_tokens) == 0:
                            continue
                            
                return (
                    True,
                    {
                        "reason": "db_track_match",
                        "matched_path": file_path,
                    },
                )
        except Exception as e:
            LOG.error(f"Error checking has_track: {e}")
            
        return (False, {"reason": "db_track_not_found"})

    def has_album(self, db: Database, artist: str, album: str) -> Tuple[bool, Dict[str, Any]]:
        """
        Check for an album in the library database.
        """
        self._ensure_scanned()

        artist_n = _normalize_text(artist)
        album_n = _normalize_text(album)
        if not artist_n or not album_n:
            return (False, {"reason": "missing_artist_or_album"})

        artist_tokens = set(artist_n.split(" "))
        album_tokens = set(album_n.split(" "))

        try:
            with db.conn.cursor() as cur:
                cur.execute("SELECT file_path, album, artist FROM library_tracks")
                tracks = cur.fetchall()
                
            for file_path, t_album, t_artist in tracks:
                blob = _normalize_text(f"{t_album} {t_artist} {Path(file_path).parent.name}")
                blob_tokens = set(blob.split(" "))
                
                if not artist_tokens.issubset(blob_tokens):
                    continue
                if not album_tokens.issubset(blob_tokens):
                    continue
                    
                return (
                    True,
                    {
                        "reason": "db_album_match",
                        "matched_dir": str(Path(file_path).parent),
                    },
                )
        except Exception as e:
            LOG.error(f"Error checking has_album: {e}")

        return (False, {"reason": "db_album_not_found"})

