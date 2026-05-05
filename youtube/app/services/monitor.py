import logging
import time
import json
import os
import concurrent.futures
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple, List, Optional

from app.config import Settings
from app.core import models
from app.database import Database
from app.core import metadata
from app.core import metadata
from app.core.metadata import search_recording
from app.core.library import LibraryScanner
from app.core.downloader import TrackQuery, YouTubeDownloaderConfig, YouTubeDownloadError, download_best_audio, safe_filename

LOG = logging.getLogger("app.services.monitor")

def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    tmp.write_text(data, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError as e:
        # Fallback for bind mounts (Errno 16: Device or resource busy, or EXDEV)
        if hasattr(e, "errno") and e.errno in (16, 18):  # EBUSY, EXDEV
            path.write_text(data, encoding="utf-8")
            try:
                os.remove(tmp)
            except OSError:
                pass
        else:
            raise

def _stable_key(kind: str, *, artist: Optional[str], title: str, album: Optional[str]) -> str:
    a = (artist or "").strip().lower()
    t = (title or "").strip().lower()
    al = (album or "").strip().lower()
    return f"{kind}|artist={a}|title={t}|album={al}"

def _build_ytdlp_cfg(settings: Settings) -> YouTubeDownloaderConfig:
    extra_args: List[str] = []
    if settings.yt_proxy:
        extra_args += ["--proxy", settings.yt_proxy]

    if settings.yt_download_archive:
        archive_path = settings.state_dir / "yt-dlp-archive.txt"
        extra_args += ["--download-archive", str(archive_path)]

    return YouTubeDownloaderConfig(
        download_dir=settings.download_dir,
        audio_format=settings.yt_output_format,
        max_results=settings.yt_search_max_results,
        strict_matching=False,
        strict_min_score=settings.yt_match_min_score,
        strict_min_score=settings.yt_match_min_score,
        cookies_file=settings.yt_cookies_file,
        duration_match_tolerance=settings.duration_match_tolerance,
        extra_args=tuple(extra_args),
    )

class MonitorService:
    def __init__(self, settings: Settings):
        self.settings = settings
        # Main thread DB connection for sequential tasks
        self.db = Database(settings)
        try:
            self.db.init_schema()
        except Exception as e:
            LOG.error(f"Failed to init DB schema: {e}")
            
        self.ytdlp_cfg = _build_ytdlp_cfg(settings)
        
        library_dir_raw = (os.getenv("LIBRARY_DIR", "") or "").strip()
        self.library_dir = (
            Path(library_dir_raw).expanduser().resolve() if library_dir_raw else None
        )
        
        # Initialize Library Scanner (Thread Safe)
        self.scanner = LibraryScanner(self.settings, self.library_dir)
        # Initial scan
        self.scanner.scan()
        
        # Thread Pool for concurrent processing
        # Adjust max_workers as needed. 4 is a reasonable default for mixed I/O and CPU.
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    def _ensure_dirs(self) -> None:
        self.settings.download_dir.mkdir(parents=True, exist_ok=True)
        # self.settings.state_dir.mkdir(parents=True, exist_ok=True) 

    def _import_from_json(self):
        if not self.settings.songs_json_path.exists():
             return

        try:
            # Check if file is empty
            if self.settings.songs_json_path.stat().st_size == 0:
                return

            payload = _read_json(self.settings.songs_json_path)
            try:
                doc = models.parse_input_document(payload)
            except Exception as e:
                LOG.error(f"Failed to parse JSON: {e}")
                return

            imported_count = 0
            # Import Tracks
            for t in doc.tracks:
                key = _stable_key("track", artist=t.artist, title=t.title, album=t.album)
                self.db.upsert_request(
                    key=key,
                    type="track",
                    title=t.title,
                    artist=t.artist,
                    album=t.album,
                    year=None,
                    genre=t.genre,
                    force_youtube=t.force_youtube
                )
                imported_count += 1
            
            # Import Albums
            for a in doc.albums:
                key = _stable_key("album", artist=a.artist, title=a.title, album=a.title)
                self.db.upsert_request(
                    key=key,
                    type="album",
                    title=a.title,
                    artist=a.artist,
                    album=a.title, 
                    year=a.year,
                    genre=a.genre,
                    force_youtube=a.force_youtube
                )
                imported_count += 1
            
            if imported_count > 0:
                LOG.info(f"Imported {imported_count} items from JSON to DB.")
                # Clear JSON file but keep structure
                _atomic_write_json(self.settings.songs_json_path, {"tracks": [], "albums": []})
                
        except json.JSONDecodeError:
            LOG.warning("songs.json is not valid json, skipping import")
        except Exception as e:
            LOG.error(f"Error importing JSON: {e}")

    def shutdown(self):
        LOG.info("Shutting down monitor service... Waiting for pending tasks.")
        self.executor.shutdown(wait=True)
        LOG.info("Monitor service shutdown complete.")

    def run_once(self) -> int:
        self._ensure_dirs()
        self._import_from_json()
        
        # Periodic library rescan (every 5 mins check)
        self.scanner.scan_if_needed()

        pending = self.db.get_pending_requests(limit=self.settings.max_items_per_cycle)
        if not pending:
            return 0
            
        processed_count = 0
        futures = []
        
        LOG.info(f"Processing {len(pending)} requests with {self.executor._max_workers} threads...")
        
        for item in pending:
            futures.append(self.executor.submit(self._process_single_item_safe, item))
            
        for f in concurrent.futures.as_completed(futures):
            try:
                if f.result():
                    processed_count += 1
            except Exception as e:
                 LOG.error(f"Unexpected error in worker thread: {e}", exc_info=True)

        LOG.info(f"run_once complete processed={processed_count}")
        return processed_count

    def _process_single_item_safe(self, item: Dict[str, Any]) -> bool:
        """Helper to run _process_db_item and handle connection/errors safely"""
        try:
            self._process_db_item(item)
            return True
        except Exception as e:
            LOG.error(f"Error processing item {item.get('id')}: {e}", exc_info=True)
            return False

    def _process_db_item(self, item: Dict[str, Any]):
        """
        Runs in a worker thread. 
        MUST use its own DB connection.
        """
        req_id = item["id"]
        
        # Thread-local DB connection
        with Database(self.settings) as db:
            # Mark processing
            db.update_request_status(req_id, "processing")
            
            kind = item["type"]
            artist = item["artist"]
            title = item["title"]
            album = item["album"]
            
            # 1. Library Check using Scanner (Thread Safe)
            if self.library_dir:
                found = False
                meta = {}
                
                if kind == "track":
                    found, meta = self.scanner.has_track(db, artist=artist, title=title)
                elif kind == "album":
                    found, meta = self.scanner.has_album(db, artist=artist, album=album)
                
                if found:
                    LOG.info(f"Found in library: {title} ({artist})")
                    db.update_request_status(req_id, "completed", meta={"source": "filesystem", "details": meta})
                    return

            # 2. Pre-Download Metadata Enrichment
            mb_pre_data = {}
            if kind == "track":
                try:
                    mb_pre_data = search_recording(artist, title, album)
                except Exception as e:
                    LOG.warning(f"Pre-download metadata search failed: {e}")
            
            # Deduplication check by MBID
            if mb_pre_data.get("mb_track_id"):
                mbid = mb_pre_data["mb_track_id"]
                existing = db.get_request_by_mb_track_id(mbid)
                if existing and existing["id"] != req_id and existing["status"] == "completed":
                    LOG.info(f"Duplicate found by MBID {mbid}: {title} ({artist}). Skipping.")
                    db.update_request_status(
                        req_id, 
                        "skipped", 
                        meta={"reason": "duplicate_mbid", "original_id": existing["id"]}
                    )
                    return

            # 3. Download
            try:
                if kind == "track":
                    q = TrackQuery(
                        title=title, 
                        artist=artist, 
                        album=album, 
                        year=item.get("year"),
                        duration=mb_pre_data.get("duration")
                    )
                else:
                    q = TrackQuery(title=f"{title} full album", artist=artist, album=album, year=item.get("year"))

                res = download_best_audio(q, self.ytdlp_cfg)
                
                # --- Metadata & Tagging Workflow ---
                final_path = res.output_path
                mb_data = {}
                
                try:
                    # 1. Fingerprint & AcoustID Lookup
                    duration, fingerprint = metadata.generate_fingerprint(str(final_path))
                    
                    # 2. Lookup
                    lookup_meta = {}
                    mb_data = metadata.lookup_track(fingerprint, duration, lookup_meta)
                    
                    # Merge pre-download data if AcoustID failed or gave less info?
                    # Priority: AcoustID/MB result > Pre-download MB result > Input Data
                    if not mb_data.get("mb_track_id") and mb_pre_data.get("mb_track_id"):
                        mb_data.update(mb_pre_data)

                    tag_data = {
                        "title": mb_data.get("title") or title,
                        "artist": mb_data.get("artist") or artist,
                        "album": mb_data.get("album") or album,
                        "date": mb_data.get("date"),
                        "genre": item.get("genre")
                    }
                    
                    # Fetch Lyrics
                    lyrics = metadata.fetch_lyrics(tag_data["artist"] or artist, tag_data["title"] or title, self.settings.genius_api_key)
                    if lyrics:
                        tag_data["lyrics"] = lyrics
                        mb_data["lyrics"] = lyrics

                    # Fetch Album Art
                    album_art_bytes = None
                    if mb_data.get("mb_release_id"):
                        album_art_bytes = metadata.fetch_album_art(mb_data["mb_release_id"])
                    if album_art_bytes:
                        tag_data["album_art_data"] = album_art_bytes
                    
                    # 3. Apply Tags
                    metadata.apply_tags(str(final_path), tag_data)
                    
                    # 4. Rename File
                    # Format: Artist - Title [mbid].ext
                    # If no MBID, fallback to [video_id] (or keep simple?)
                    
                    raw_artist = tag_data["artist"] or "Unknown"
                    raw_title = tag_data["title"] or "Unknown"
                    
                    safe_artist = safe_filename(raw_artist)
                    safe_title = safe_filename(raw_title)
                    
                    unique_id_component = mb_data.get("mb_track_id")
                    if not unique_id_component:
                         unique_id_component = res.video_id
                    
                    new_filename = f"{safe_artist} - {safe_title} [{unique_id_component}]{final_path.suffix}"
                    new_path = final_path.parent / new_filename
                    
                    # Handle basic collision if same MBID (shouldn't happen due to DB check, but maybe concurrent?)
                    if new_path.exists() and new_path != final_path:
                        # If content allows, we overwrite or skip? 
                        # Safe to overwrite if it's the same song? Maybe not.
                        # Let's add a random suffix if collision
                         for i in range(1, 10):
                             variant = f"{safe_artist} - {safe_title} [{unique_id_component}] ({i}){final_path.suffix}"
                             v_path = final_path.parent / variant
                             if not v_path.exists():
                                 new_path = v_path
                                 break

                    final_path.rename(new_path)
                    final_path = new_path
                    
                    LOG.info(f"Processed: {final_path}")
                    
                    # 5. Insert into library_tracks
                    db.upsert_library_track({
                        "file_path": str(final_path),
                        "title": tag_data["title"],
                        "artist": tag_data["artist"],
                        "album": tag_data["album"],
                        "genre": tag_data["genre"],
                        "release_date": tag_data["date"],
                        "duration": mb_data.get("duration") or res.duration,
                        "mb_track_id": mb_data.get("mb_track_id"),
                        "mb_artist_id": mb_data.get("mb_artist_id"),
                        "acoustid_fingerprint": mb_data.get("acoustid_fingerprint"),
                        "lyrics": mb_data.get("lyrics"),
                        "file_mtime": os.path.getmtime(str(final_path))
                    })
                    
                except Exception as e:
                    LOG.error(f"Metadata/Tagging error for {req_id}: {e}")
                    # Continue completion even if tagging fails
                
                # -----------------------------------
                
                yt_meta = {
                    "source": res.source,
                    "video_id": res.video_id,
                    "webpage_url": res.webpage_url,
                    "title": res.title,
                    "uploader": res.uploader,
                    "duration": res.duration,
                    "output_path": str(final_path),
                    "match_score": res.match_score,
                    "original_query": res.matched_query,
                }
                
                db.update_request_status(
                    req_id, 
                    "completed", 
                    meta={"source": "youtube", "details": yt_meta},
                    mb_track_id=mb_data.get("mb_track_id"),
                    mb_artist_id=mb_data.get("mb_artist_id"),
                    acoustid_fingerprint=mb_data.get("acoustid_fingerprint"),
                    acoustid_score=mb_data.get("acoustid_score")
                )
                
            except YouTubeDownloadError as e:
                LOG.warning(f"Download failed: {e}")
                db.update_request_status(req_id, "failed", error=str(e))
            except Exception as e:
                LOG.error(f"Unexpected error: {e}")
                db.update_request_status(req_id, "failed", error=str(e))
