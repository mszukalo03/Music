"""
YouTube fallback downloader using yt-dlp.

Responsibilities:
- Build a reasonable YouTube search query from track metadata.
- Search YouTube for candidate videos.
- Pick best match (basic scoring).
- Download best available audio source and convert/remux to a desired format.

Notes:
- This module intentionally does not depend on the rest of the app.
- Prefer audio output (e.g., m4a/mp3) rather than MP4 video.
- Requires: yt-dlp.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from rapidfuzz import fuzz

log = logging.getLogger(__name__)


class YouTubeDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrackQuery:
    """
    Minimal track metadata for YouTube search + naming.

    `title` is required. Others are optional but improve matching.
    """

    title: str
    artist: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    year: Optional[int] = None
    duration: Optional[float] = None

    def search_query(self) -> str:
        """
        Build a YouTube query string.

        Strategy:
        - Prefer "Artist - Title"
        - Add "audio" to bias to official / audio sources
        - Add album/year when present to disambiguate
        """
        parts: List[str] = []
        if self.artist:
            parts.append(self.artist.strip())
        parts.append(self.title.strip())

        # Join as "Artist - Title" when possible
        base = " - ".join([p for p in parts if p])

        extra: List[str] = ["audio"]
        if self.album:
            extra.append(self.album.strip())
        if self.year:
            extra.append(str(self.year))

        return f"{base} {' '.join(extra)}".strip()


@dataclass(frozen=True)
class YouTubeDownloaderConfig:
    """
    Configuration knobs. Keep them environment-driven in the caller.
    """

    download_dir: Path
    # Audio output format. Typical: "m4a" (best with YouTube), or "mp3".
    audio_format: str = "m4a"
    # Limit candidates retrieved from ytsearch.
    max_results: int = 8
    # If True, require some minimum score heuristic to accept a candidate.
    strict_matching: bool = False
    # If strict, minimum score (0..1) to accept.
    strict_min_score: float = 0.55
    # Optional cookies file to improve reliability (age-gated, rate limiting).
    cookies_file: Optional[Path] = None
    # Optional extra yt-dlp args (advanced usage).
    extra_args: Tuple[str, ...] = ()
    # Network retries at yt-dlp level
    retries: int = 3
    # Socket timeout seconds (yt-dlp arg)
    socket_timeout: int = 20
    # Add sleep to avoid hammering YouTube
    inter_download_sleep_seconds: float = 0.0
    # If True, keep the downloaded intermediate file(s)
    keep_intermediates: bool = False
    # If True, prefer "ytsearchdate" to bias for newer uploads (usually worse for music). Default False.
    prefer_newest: bool = False
    # Duration filtering tolerance in seconds
    duration_match_tolerance: float = 5.0


@dataclass(frozen=True)
class DownloadResult:
    """
    Result of a download attempt.
    """

    source: str  # "youtube"
    video_id: str
    webpage_url: str
    title: str
    uploader: Optional[str]
    duration: Optional[int]
    output_path: Path
    match_score: float
    matched_query: str


def _require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise YouTubeDownloadError(
            f"Required executable '{name}' not found on PATH. "
            f"Install it or adjust PATH."
        )


def _yt_dlp_cmd_base(cfg: YouTubeDownloaderConfig) -> List[str]:
    _require_binary("yt-dlp")

    cmd = [
        "yt-dlp",
        "--no-progress",
        "--newline",
        "--no-playlist",
        "--ignore-errors",
        "--no-warnings",
        "--retries",
        str(cfg.retries),
        "--socket-timeout",
        str(cfg.socket_timeout),
        "--print-json",
        "--no-simulate",
    ]

    # Post-processing & output handling
    # We want best audio, then convert to target format.
    # yt-dlp will use ffmpeg for extraction/conversion.
    cmd += [
        "-f",
        "ba[vcodec=none]/bestaudio", # prefer audio-only
        "--extract-audio",
        "--audio-format",
        cfg.audio_format,
        "--audio-quality",
        "0",  # best
    ]

    if cfg.keep_intermediates:
        cmd.append("-k")

    if cfg.cookies_file:
        cmd += ["--cookies", str(cfg.cookies_file)]

    if cfg.extra_args:
        cmd += list(cfg.extra_args)

    return cmd


_SAFE_CHARS_RE = re.compile(r"[^a-zA-Z0-9\.\-\_\(\)\[\]\,\s]+")



def safe_filename(s: str, max_len: int = 180) -> str:
    s = s.strip()
    s = s.replace("/", "-").replace("\\", "-").replace(":", " - ")
    s = _SAFE_CHARS_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s



def _normalize(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\(.*?\)", " ", s)  # remove parentheses
    s = re.sub(r"\[.*?\]", " ", s)  # remove brackets
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _score_entry(q: TrackQuery, entry: Dict[str, Any]) -> float:
    """
    Score a yt-dlp entry in [0, 1] using RapidFuzz.
    """
    cand_title = entry.get("title") or ""
    cand_uploader = entry.get("uploader") or entry.get("channel") or ""

    # 1. Title Similarity (Token Sort Ratio is good for out-of-order words)
    # We compare query title vs candidate title.
    # If artist is known, we prepend it to query for better matching against "Artist - Title" videos.
    query_text = q.title
    if q.artist:
        query_text = f"{q.artist} - {q.title}"

    # partial_token_sort_ratio helps if candidate has extra junk
    s_title = fuzz.token_sort_ratio(query_text, cand_title) / 100.0

    # 2. Uploader checks (bonus)
    # If we have an artist, check if uploader matches artist
    s_uploader = 0.0
    if q.artist and cand_uploader:
         s_uploader = fuzz.token_set_ratio(q.artist, cand_uploader) / 100.0

    # 3. Penalties
    penalty = 0.0
    cand_norm = cand_title.lower()
    query_norm = query_text.lower()
    
    # Penalize "live", "cover", "remix" if not requested
    for bad in ("live", "cover", "remix", "karaoke", "instrumental"):
        if bad in cand_norm and bad not in query_norm:
            penalty += 0.15

    # Weighted score
    # Title match is dominant
    score = (0.85 * s_title) + (0.15 * s_uploader) - penalty
    return max(0.0, min(1.0, score))


def _run_yt_dlp_capture_json(cmd: List[str]) -> List[Dict[str, Any]]:
    """
    Run yt-dlp printing JSON per line. Return parsed objects.
    """
    log.debug("Running yt-dlp: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    out = proc.stdout or ""
    if proc.returncode != 0:
        # yt-dlp failed. It might have printed some JSON before failing, 
        # but we shouldn't trust it to have produced a valid file.
        err_msg = f"yt-dlp failed with code={proc.returncode}"
        # Capture last few lines of output/error for context
        tail = out[-500:].replace("\n", " ").strip()
        if tail:
            err_msg += f" | Output: {tail}"
        
        log.warning(err_msg)
        raise YouTubeDownloadError(err_msg)

    objs: List[Dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # yt-dlp prints logs too; JSON lines start with "{"
        if not line.startswith("{"):
            continue
        try:
            objs.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not objs:
        raise YouTubeDownloadError(
            "yt-dlp produced no JSON output. Check connectivity, cookies, or query."
        )
    return objs


def search_youtube(q: TrackQuery, cfg: YouTubeDownloaderConfig) -> List[Dict[str, Any]]:
    """
    Search YouTube via yt-dlp and return raw entries.
    """
    search_kind = "ytsearchdate" if cfg.prefer_newest else "ytsearch"
    query = q.search_query()
    target = f"{search_kind}{cfg.max_results}:{query}"

    cmd = _yt_dlp_cmd_base(cfg)
    # For search we don't want to download; simulate and dump JSON.
    # We'll do actual download on chosen entry.
    cmd_search = [c for c in cmd if c not in ("--no-simulate",)]
    cmd_search += ["--simulate", "--skip-download", target]

    objs = _run_yt_dlp_capture_json(cmd_search)

    # Entries may include a top-level "entries" list; yt-dlp differs by extractor.
    entries: List[Dict[str, Any]] = []
    for obj in objs:
        if "entries" in obj and isinstance(obj["entries"], list):
            for e in obj["entries"]:
                if isinstance(e, dict):
                    entries.append(e)
        else:
            # Sometimes individual entries are printed directly
            if obj.get("id") and obj.get("webpage_url"):
                entries.append(obj)

    # Filter out None / incomplete
    entries = [
        e
        for e in entries
        if isinstance(e, dict) and e.get("webpage_url") and e.get("id")
    ]
    return entries


def pick_best_match(
    q: TrackQuery, entries: Iterable[Dict[str, Any]], cfg: YouTubeDownloaderConfig
) -> Tuple[Dict[str, Any], float]:
    best: Optional[Dict[str, Any]] = None
    best_score = -1.0

    for e in entries:
        s = _score_entry(q, e)
        if s > best_score:
            best = e
            best_score = s

            best = e
            best_score = s

    if best is None:
        raise YouTubeDownloadError("No YouTube entries to choose from.")

    # Duration Filtering
    if q.duration and best:
        cand_duration = best.get("duration")
        if isinstance(cand_duration, (int, float)):
             diff = abs(cand_duration - q.duration)
             if diff > cfg.duration_match_tolerance:
                 # If the best match fails duration check, we could check others,
                 # but for now let's just reject/warn.
                 # Better strategy: Filter entries *before* picking max score?
                 # Let's re-eval loop to pick best score *that satisfies duration*.
                 
                 valid_best = None
                 valid_best_score = -1.0
                 
                 for e in entries:
                     dur = e.get("duration")
                     if isinstance(dur, (int, float)):
                         if abs(dur - q.duration) <= cfg.duration_match_tolerance:
                             s = _score_entry(q, e)
                             if s > valid_best_score:
                                 valid_best = e
                                 valid_best_score = s
                 
                 if valid_best:
                     best = valid_best
                     best_score = valid_best_score
                 else:
                      # If strict matching is on, we might want to fail. 
                      # But if not, maybe we fall back to the score-best?
                      # User asked to "Reject any candidate...", implying strictness.
                      # We'll log a warning and if strict matching is high, it might naturally fail?
                      # Let's enforce it if we have a duration.
                      raise YouTubeDownloadError(
                          f"Best match duration {cand_duration}s differs from expected {q.duration}s by > {cfg.duration_match_tolerance}s. No valid matches found."
                      )

    if cfg.strict_matching and best_score < cfg.strict_min_score:
        raise YouTubeDownloadError(
            f"Strict matching enabled: best score {best_score:.2f} < min {cfg.strict_min_score:.2f}."
        )

    return best, best_score


def _build_output_template(q: TrackQuery, cfg: YouTubeDownloaderConfig) -> str:
    """
    Build an yt-dlp output template that produces stable file names.

    We include uploader/title to minimize collisions, but also incorporate artist/title
    from the JSON to preserve intent.
    """
    # yt-dlp will replace %(ext)s after post-processing to desired audio_format.
    # We use a simple ID-based filename to avoid character encoding issues and make finding the file deterministic.
    out_dir = cfg.download_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    return str(out_dir / "%(id)s.%(ext)s")


def download_best_audio(q: TrackQuery, cfg: YouTubeDownloaderConfig) -> DownloadResult:
    """
    Search YouTube and download best audio for the best match.

    Returns path to the final converted audio file.
    """
    cfg.download_dir.mkdir(parents=True, exist_ok=True)

    entries = search_youtube(q, cfg)
    best_entry, score = pick_best_match(q, entries, cfg)

    url = best_entry.get("webpage_url")
    if not url:
        raise YouTubeDownloadError("Chosen entry missing webpage_url.")

    outtmpl = _build_output_template(q, cfg)

    cmd = _yt_dlp_cmd_base(cfg)
    cmd += ["-o", outtmpl, url]

    # yt-dlp prints JSON lines; last JSON line usually is final info.
    objs = _run_yt_dlp_capture_json(cmd)

    # Find the last object that looks like a video info dict and has requested_downloads / filepath.
    info: Optional[Dict[str, Any]] = None
    for obj in reversed(objs):
        if obj.get("id") and (
            obj.get("_filename")
            or obj.get("requested_downloads")
            or obj.get("filepath")
        ):
            info = obj
            break
    if info is None:
        info = objs[-1]

    # Determine output file path:
    # - yt-dlp may put final path in requested_downloads[0]["filepath"]
    # - or in _filename
    output_path: Optional[str] = None
    rd = info.get("requested_downloads")
    if isinstance(rd, list) and rd:
        fp = rd[0].get("filepath") or rd[0].get("filename")
        if isinstance(fp, str):
            output_path = fp

    if not output_path:
        fn = info.get("_filename") or info.get("filepath")
        if isinstance(fn, str):
            output_path = fn

    if not output_path:
        # Fallback: glob by the id in download_dir.
        vid = best_entry.get("id")
        if vid:
            candidates = sorted(cfg.download_dir.glob(f"*[{vid}].*"))
            if candidates:
                output_path = str(candidates[-1])

    if not output_path:
        raise YouTubeDownloadError(
            "Could not determine downloaded output path from yt-dlp output."
        )

    final_path = Path(output_path)
    if not final_path.exists():
        # 1. Check if it exists with the configured audio extension (conversion happened)
        alt_path = final_path.with_suffix("." + cfg.audio_format)
        if alt_path.exists():
            final_path = alt_path
        else:
            # 2. Fallback search by ID (handling glob escaping for brackets)
            vid = best_entry.get("id")
            if vid:
                # Naive glob using brackets is dangerous as [] are glob patterns.
                # Search for *vid* and filter manually.
                candidates = []
                for p in cfg.download_dir.glob(f"*{vid}*"):
                     if f"[{vid}]" in p.name:
                         candidates.append(p)
                candidates.sort()
                
                if candidates:
                    final_path = candidates[-1]
                else:
                    raise YouTubeDownloadError(
                        f"Downloaded file not found on disk: {final_path} (checked extensions and fallback search)"
                    )
            else:
                 raise YouTubeDownloadError(f"Downloaded file not found on disk: {final_path}")

    if cfg.inter_download_sleep_seconds > 0:
        time.sleep(cfg.inter_download_sleep_seconds)

    return DownloadResult(
        source="youtube",
        video_id=str(best_entry.get("id")),
        webpage_url=str(url),
        title=str(best_entry.get("title") or ""),
        uploader=(best_entry.get("uploader") or best_entry.get("channel")),
        duration=best_entry.get("duration")
        if isinstance(best_entry.get("duration"), int)
        else None,
        output_path=final_path,
        match_score=score,
        matched_query=q.search_query(),
    )
