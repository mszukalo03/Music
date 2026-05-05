import logging
import os
import sys
from pathlib import Path

# Setup path
sys.path.append(os.getcwd())

from app.config import Settings, get_settings
from app.database import Database
from app.core import metadata
from app.core import downloader
from app.core.downloader import TrackQuery

# Configure logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("verify")

def test_metadata_search():
    log.info("Testing Metadata Search...")
    # Known track: "Never Gonna Give You Up" by Rick Astley
    res = metadata.search_recording("Rick Astley", "Never Gonna Give You Up")
    if res and res.get("title") == "Never Gonna Give You Up":
        log.info(f"✅ Metadata Found: {res}")
    else:
        log.error(f"❌ Metadata Search Failed: {res}")

def test_database_migration():
    log.info("Testing Database Migration...")
    s = get_settings()
    # Use a test DB if possible, or just safe migration on current (it's idempotent)
    try:
        db = Database(s)
        db.init_schema() # Should call apply_migrations
        log.info("✅ Database Schema Initialized & Migrated")
        
        # Verify column existence
        with db.conn.cursor() as cur:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='requests' AND column_name='mb_track_id'")
            if cur.fetchone():
                log.info("✅ Column mb_track_id exists")
            else:
                log.error("❌ Column mb_track_id missing")
    except Exception as e:
        log.error(f"❌ Database Test Failed: {e}")

def test_fuzzy_matching():
    log.info("Testing Fuzzy Matching...")
    q = TrackQuery(title="Never Gonna Give You Up", artist="Rick Astley")
    
    # Mock entries
    entries = [
        {"title": "Rick Astley - Never Gonna Give You Up (Official Music Video)", "uploader": "Rick Astley", "duration": 213},
        {"title": "Never Gonna Give You Up", "uploader": "RandomUser", "duration": 213},
        {"title": "Rick Astley - Never Gonna Give You Up (Live)", "uploader": "Rick Astley", "duration": 300}, # Should have penalty
        {"title": "Totally Different Song", "uploader": "Someone", "duration": 100}
    ]
    
    # Score them
    log.info("Scoring entries:")
    for e in entries:
        score = downloader._score_entry(q, e)
        log.info(f"  '{e['title']}': {score:.2f}")
        
    # Check if correct one wins
    best, score = downloader.pick_best_match(q, entries, downloader.YouTubeDownloaderConfig(download_dir=Path("."), strict_matching=False))
    if best["title"] == "Rick Astley - Never Gonna Give You Up (Official Music Video)":
        log.info("✅ Fuzzy Matching Logic Correct (Best match selected)")
    else:
        log.error(f"❌ Fuzzy Matching Failed. Selected: {best['title']}")

def test_duration_filtering():
    log.info("Testing Duration Filtering...")
    q = TrackQuery(title="Song", artist="Artist", duration=213.0) # Matches 213s
    cfg = downloader.YouTubeDownloaderConfig(
        download_dir=Path("."), 
        strict_matching=True,
        duration_match_tolerance=5.0
    )
    
    # Mock entries
    entries = [
        {"title": "Correct Duration", "duration": 213, "webpage_url": "http://test"},
        {"title": "Too Long", "duration": 300, "webpage_url": "http://test"},
        {"title": "Too Short", "duration": 100, "webpage_url": "http://test"}
    ]
    
    # Test
    try:
        best, score = downloader.pick_best_match(q, entries, cfg)
        if best["title"] == "Correct Duration":
             log.info("✅ Duration Filtering Passed")
        else:
             log.error(f"❌ Duration Filtering Failed. Got {best['title']}")
    except Exception as e:
         log.error(f"❌ Duration Filtering Error: {e}")

if __name__ == "__main__":
    test_metadata_search()
    test_database_migration()
    test_fuzzy_matching()
