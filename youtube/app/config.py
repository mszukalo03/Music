"""
Configuration management for the YouTube downloader.
Handles environment variables, type conversion, and central settings.
"""
from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

def _to_bool(value: Optional[str], default: bool = False) -> bool:
    """
    Convert a string environment variable to a boolean.
    
    Supports various truthy/falsy strings like 'true', '1', 'yes', 'on', etc.
    """
    if value is None:
        return default
    v = value.strip().lower()
    if v in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default

def _to_int(value: Optional[str], default: int) -> int:
    """
    Convert a string environment variable to an integer.
    Returns the default value if conversion fails or if the value is missing.
    """
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default

def _to_float(value: Optional[str], default: float) -> float:
    """
    Convert a string environment variable to a float.
    Returns the default value if conversion fails or if the value is missing.
    """
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default

def _expand_path(value: str) -> Path:
    """
    Expand environment variables and user home directory in a path string,
    then return a resolved absolute Path object.
    """
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()

@dataclass(frozen=True)
class Settings:
    """
    Central configuration container for the application.
    Stores all settings loaded from environment variables.
    """

    # Core paths
    songs_json_path: Path
    download_dir: Path
    state_dir: Path

    # Loop behavior
    poll_interval_seconds: int
    retry_backoff_seconds: int
    max_items_per_cycle: int

    # YouTube fallback (yt-dlp)
    yt_search_max_results: int
    yt_match_min_score: float
    duration_match_tolerance: float
    yt_cookies_file: Optional[Path]
    yt_proxy: Optional[str]
    yt_output_format: str
    yt_audio_quality: str
    yt_download_archive: bool

    # Metadata & APIs
    genius_api_key: Optional[str]
    acoustid_api_key: Optional[str]

    # Archiving (found in library)
    found_in_library_archive_enabled: bool
    found_in_library_archive_json_path: Path

    # Logging
    log_level: str
    log_json: bool

    # Database
    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str
    postgres_db: str

def load_dotenv(dotenv_path: Optional[Path] = None) -> None:
    """
    Load environment variables from a .env file if the python-dotenv package is installed.
    By default, it looks for a '.env' file in the current working directory.
    """
    try:
        from dotenv import load_dotenv as _load_dotenv
    except Exception:
        # python-dotenv not installed, skipping
        return

    if dotenv_path is None:
        dotenv_path = Path(".env")
    _load_dotenv(dotenv_path=dotenv_path, override=False)

def get_settings() -> Settings:
    """
    Load and validate application settings from environment variables.
    Provides reasonable defaults for most configuration options.
    """
    load_dotenv()

    songs_json_path_raw = os.getenv("SONGS_JSON_PATH", "").strip() or "songs.json"
    download_dir_raw = os.getenv("DOWNLOAD_DIR", "").strip() or "./downloads"
    state_dir_raw = os.getenv("STATE_DIR", "").strip() or "./state"

    songs_json_path = _expand_path(songs_json_path_raw)
    download_dir = _expand_path(download_dir_raw)
    state_dir = _expand_path(state_dir_raw)

    poll_interval_seconds = _to_int(os.getenv("POLL_INTERVAL_SECONDS"), default=60)
    retry_backoff_seconds = _to_int(os.getenv("RETRY_BACKOFF_SECONDS"), default=5)
    max_items_per_cycle = _to_int(os.getenv("MAX_ITEMS_PER_CYCLE"), default=50)

    yt_search_max_results = _to_int(os.getenv("YT_SEARCH_MAX_RESULTS"), default=5)
    yt_match_min_score = _to_float(os.getenv("YT_MATCH_MIN_SCORE"), default=0.65)
    duration_match_tolerance = _to_float(os.getenv("DURATION_MATCH_TOLERANCE"), default=5.0)

    yt_cookies_file_raw = (os.getenv("YT_COOKIES_FILE", "") or "").strip()
    yt_cookies_file = _expand_path(yt_cookies_file_raw) if yt_cookies_file_raw else None

    yt_proxy = (os.getenv("YT_PROXY", "") or "").strip() or None
    yt_output_format = (os.getenv("YT_OUTPUT_FORMAT", "") or "m4a").strip().lower()
    yt_audio_quality = (os.getenv("YT_AUDIO_QUALITY", "") or "0").strip()
    yt_download_archive = _to_bool(os.getenv("YT_DOWNLOAD_ARCHIVE"), default=True)

    genius_api_key = (os.getenv("GENIUS_API_KEY", "") or "").strip() or None
    acoustid_api_key = (os.getenv("ACOUSTID_API_KEY", "") or "").strip() or None

    found_in_library_archive_enabled = _to_bool(
        os.getenv("FOUND_IN_LIBRARY_ARCHIVE_ENABLED"), default=False
    )
    found_in_library_archive_json_path_raw = (
        os.getenv("FOUND_IN_LIBRARY_ARCHIVE_JSON_PATH", "") or ""
    ).strip()
    if found_in_library_archive_json_path_raw:
        found_in_library_archive_json_path = _expand_path(
            found_in_library_archive_json_path_raw
        )
    else:
        found_in_library_archive_json_path = (
            state_dir / "found_in_library.json"
        ).resolve()

    log_level = (os.getenv("LOG_LEVEL", "") or "INFO").strip().upper()
    log_json = _to_bool(os.getenv("LOG_JSON"), default=False)

    postgres_host = os.getenv("POSTGRES_HOST", "postgres")
    postgres_port = _to_int(os.getenv("POSTGRES_PORT"), default=5432)
    postgres_user = os.getenv("POSTGRES_USER", "user")
    postgres_password = os.getenv("POSTGRES_PASSWORD", "password")
    postgres_db = os.getenv("POSTGRES_DB", "youtubedb")

    return Settings(
        songs_json_path=songs_json_path,
        download_dir=download_dir,
        state_dir=state_dir,
        poll_interval_seconds=poll_interval_seconds,
        retry_backoff_seconds=retry_backoff_seconds,
        max_items_per_cycle=max_items_per_cycle,
        yt_search_max_results=yt_search_max_results,
        yt_match_min_score=yt_match_min_score,
        duration_match_tolerance=duration_match_tolerance,
        yt_cookies_file=yt_cookies_file,
        yt_proxy=yt_proxy,
        yt_output_format=yt_output_format,
        yt_audio_quality=yt_audio_quality,
        yt_download_archive=yt_download_archive,
        genius_api_key=genius_api_key,
        acoustid_api_key=acoustid_api_key,
        found_in_library_archive_enabled=found_in_library_archive_enabled,
        found_in_library_archive_json_path=found_in_library_archive_json_path,
        log_level=log_level,
        log_json=log_json,
        postgres_host=postgres_host,
        postgres_port=postgres_port,
        postgres_user=postgres_user,
        postgres_password=postgres_password,
        postgres_db=postgres_db,
    )
