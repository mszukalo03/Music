"""
Database abstraction layer for the YouTube downloader.
Handles Postgres connection, schema initialization, and CRUD operations for requests.
"""
import psycopg2
import logging
import time
import json
from app.config import Settings

LOG = logging.getLogger("app.database")

class Database:
    """
    Manages the PostgreSQL database connection and operations.
    """
    def __init__(self, settings: Settings):
        """Initialize the Database with application settings."""
        self.settings = settings
        self.conn = None

    def __enter__(self):
        """Context manager entry - establishes connection."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - closes connection."""
        self.close()

    def close(self):
        """Close the database connection if it exists."""
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def connect(self):
        """
        Establish a connection to the PostgreSQL database.
        Retries up to 10 times with a 3-second delay between attempts.
        """
        retries = 10
        while retries > 0:
            try:
                self.conn = psycopg2.connect(
                    host=self.settings.postgres_host,
                    port=self.settings.postgres_port,
                    user=self.settings.postgres_user,
                    password=self.settings.postgres_password,
                    dbname=self.settings.postgres_db
                )
                LOG.info("Connected to Postgres")
                return
            except Exception as e:
                LOG.error(f"Failed to connect to DB: {e}. Retrying in 3s... ({retries} left)")
                retries -= 1
                time.sleep(3)
        raise Exception("Could not connect to Database after multiple retries")

    def apply_migrations(self):
        """
        Apply schema migrations safely.
        """
        if not self.conn:
            self.connect()
            
        try:
            with self.conn.cursor() as cur:
                # 1. Ensure columns exist (idempotent)
                cur.execute("ALTER TABLE requests ADD COLUMN IF NOT EXISTS mb_track_id VARCHAR(50);")
                cur.execute("ALTER TABLE requests ADD COLUMN IF NOT EXISTS mb_artist_id VARCHAR(50);")
                cur.execute("ALTER TABLE requests ADD COLUMN IF NOT EXISTS acoustid_fingerprint TEXT;")
                cur.execute("ALTER TABLE requests ADD COLUMN IF NOT EXISTS acoustid_score FLOAT;")
                
                # 2. Add Unique Constraint on mb_track_id
                # First, check if constraint exists
                cur.execute("""
                    SELECT conname FROM pg_constraint WHERE conname = 'requests_mb_track_id_key';
                """)
                if not cur.fetchone():
                    LOG.info("Applying UNIQUE constraint on mb_track_id...")
                    # Cleanup duplicates: Keep the one with 'completed' status, or most recent.
                    # Strategy:
                    # Identify mb_track_ids that appear > 1 time (where not null).
                    # For each, keep one ID and delete others.
                    
                    cur.execute("""
                        DELETE FROM requests
                        WHERE id IN (
                            SELECT id
                            FROM (
                                SELECT id,
                                       ROW_NUMBER() OVER (
                                           PARTITION BY mb_track_id 
                                           ORDER BY 
                                               CASE WHEN status='completed' THEN 1 ELSE 2 END, 
                                               updated_at DESC
                                       ) as rnum
                                FROM requests
                                WHERE mb_track_id IS NOT NULL
                            ) t
                            WHERE t.rnum > 1
                        );
                    """)
                    
                    # Now safe to add constraint
                    cur.execute("ALTER TABLE requests ADD CONSTRAINT requests_mb_track_id_key UNIQUE (mb_track_id);")
                    LOG.info("UNIQUE constraint added to mb_track_id.")
                
                self.conn.commit()
                
        except Exception as e:
            LOG.error(f"Migration failed: {e}")
            self.conn.rollback()

    def init_schema(self):
        """
        Initialize the database schema if it doesn't exist.
        Creates the 'requests' table and necessary indices.
        """
        if not self.conn:
            self.connect()
        
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS requests (
                        id SERIAL PRIMARY KEY,
                        type VARCHAR(20) NOT NULL, -- 'track' or 'album'
                        title VARCHAR(255) NOT NULL,
                        artist VARCHAR(255),
                        album VARCHAR(255),
                        year INT,
                        genre VARCHAR(100),
                        force_youtube BOOLEAN DEFAULT FALSE,
                        status VARCHAR(20) DEFAULT 'pending', -- pending, processing, completed, failed, skipped
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_attempt_at TIMESTAMP,
                        attempt_count INT DEFAULT 0,
                        last_error TEXT,
                        metadata JSONB,
                        mb_track_id VARCHAR(50),      -- MusicBrainz Track ID
                        mb_artist_id VARCHAR(50),     -- MusicBrainz Artist ID
                        acoustid_fingerprint TEXT,    -- AcoustID Fingerprint
                        acoustid_score FLOAT,         -- Match Score
                        input_key VARCHAR(512) UNIQUE -- To prevent duplicates from JSON import
                    );
                """)
                
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS library_tracks (
                        id SERIAL PRIMARY KEY,
                        file_path TEXT UNIQUE NOT NULL,
                        title VARCHAR(255),
                        artist VARCHAR(255),
                        album VARCHAR(255),
                        genre VARCHAR(100),
                        release_date VARCHAR(50),
                        duration FLOAT,
                        mb_track_id VARCHAR(50),
                        mb_artist_id VARCHAR(50),
                        acoustid_fingerprint TEXT,
                        lyrics TEXT,
                        file_mtime FLOAT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                
                # Index for fast lookup
                cur.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_requests_input_key ON requests(input_key);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_library_mbid ON library_tracks(mb_track_id);")
                
                self.conn.commit()
                LOG.info("Schema initialized")
                
            self.apply_migrations()
            
        except Exception as e:
            LOG.error(f"Schema init failed: {e}")
            self.conn.rollback()
            raise

    def get_request_by_mb_track_id(self, mb_track_id: str) -> Optional[dict[str, Any]]:
        """
        Retrieve a request from the database by its MusicBrainz Track ID.
        """
        if not mb_track_id:
            return None
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT * FROM requests WHERE mb_track_id = %s LIMIT 1", (mb_track_id,))
                col_names = [desc[0] for desc in cur.description]
                row = cur.fetchone()
                if row:
                    return dict(zip(col_names, row))
                return None
        except Exception as e:
            LOG.error(f"DB Read Error (by MBID): {e}")
            return None

    def get_pending_requests(self, limit=10):
        """
        Fetch requests that are 'pending' or 'failed' (with retries left).
        Ordered by creation time (FIFO).
        """
        # Determine retry policy
        # If status is failed, only retry if it hasn't exceeded max attempts (e.g. 3)
        # If status is pending, take it.
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    SELECT id, type, title, artist, album, year, genre, force_youtube, attempt_count
                    FROM requests
                    WHERE status = 'pending' 
                       OR (status = 'failed' AND attempt_count < 3)
                    ORDER BY created_at ASC
                    LIMIT %s
                """, (limit,))
                columns = [col[0] for col in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
        except Exception as e:
             LOG.error(f"DB Read Error: {e}")
             self.conn.rollback() # Ensure transaction is cleared
             return []

    def update_request_status(self, request_id, status, error=None, meta=None, 
                              mb_track_id=None, mb_artist_id=None, acoustid_fingerprint=None, acoustid_score=None):
        """
        Update the status and metadata of a specific request.
        Handles status changes to 'processing', 'completed', 'skipped', or 'failed'.
        """
        try:
            with self.conn.cursor() as cur:
                if status == 'processing':
                    cur.execute("""
                        UPDATE requests 
                        SET status = %s, last_attempt_at = NOW(), attempt_count = attempt_count + 1, updated_at = NOW()
                        WHERE id = %s
                    """, (status, request_id))
                elif status in ('completed', 'skipped'):
                     # Dynamic update construction
                     updates = [
                         "status = %s",
                         "updated_at = NOW()",
                         "metadata = coalesce(metadata, '{}'::jsonb) || %s::jsonb"
                     ]
                     params = [status, json.dumps(meta) if meta else '{}']
                     
                     if mb_track_id:
                         updates.append("mb_track_id = %s")
                         params.append(mb_track_id)
                     if mb_artist_id:
                         updates.append("mb_artist_id = %s")
                         params.append(mb_artist_id)
                     if acoustid_fingerprint:
                         updates.append("acoustid_fingerprint = %s")
                         params.append(acoustid_fingerprint)
                     if acoustid_score is not None:
                         updates.append("acoustid_score = %s")
                         params.append(acoustid_score)
                         
                     params.append(request_id)
                     
                     query = f"UPDATE requests SET {', '.join(updates)} WHERE id = %s"
                     cur.execute(query, tuple(params))
                     
                elif status == 'failed':
                     cur.execute("""
                        UPDATE requests 
                        SET status = %s, last_error = %s, updated_at = NOW(), metadata = coalesce(metadata, '{}'::jsonb) || %s::jsonb
                        WHERE id = %s
                    """, (status, error, json.dumps(meta) if meta else '{}', request_id))
                
                self.conn.commit()
        except Exception as e:
            LOG.error(f"DB Update Error: {e}")
            self.conn.rollback()

    def upsert_request(self, key, type, title, artist, album, year=None, genre=None, force_youtube=False):
        """
        Insert a new request or ignore if the input_key already exists.
        Commonly used during JSON import.
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO requests (input_key, type, title, artist, album, year, genre, force_youtube)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (input_key) DO NOTHING
                """, (key, type, title, artist, album, year, genre, force_youtube))
                self.conn.commit()
        except Exception as e:
            LOG.error(f"DB Upsert Error: {e}")
            self.conn.rollback()

    def list_requests(self, status=None, limit=100):
        """
        List requests with optional status filtering.
        """
        try:
            with self.conn.cursor() as cur:
                query = "SELECT id, type, title, artist, status, created_at FROM requests"
                params = []
                if status:
                    query += " WHERE status = %s"
                    params.append(status)
                query += " ORDER BY created_at DESC LIMIT %s"
                params.append(limit)
                
                cur.execute(query, tuple(params))
                columns = [col[0] for col in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
        except Exception as e:
            LOG.error(f"DB List Error: {e}")
            self.conn.rollback()
            return []

    def delete_request(self, request_id):
        """Delete a specific request by ID."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM requests WHERE id = %s", (request_id,))
                self.conn.commit()
                return True
        except Exception as e:
            LOG.error(f"DB Delete Error: {e}")
            self.conn.rollback()
            return False

    def reset_failed_requests(self):
        """Reset all 'failed' requests back to 'pending' and clear attempt counts."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("UPDATE requests SET status = 'pending', attempt_count = 0 WHERE status = 'failed'")
                self.conn.commit()
                return True
        except Exception as e:
            LOG.error(f"DB Reset Error: {e}")
            self.conn.rollback()
            return False

    # --- Library Tracks Operations ---

    def upsert_library_track(self, track_data: dict):
        """
        Insert or update a library track based on its file_path.
        track_data should be a dict matching the library_tracks schema.
        """
        try:
            with self.conn.cursor() as cur:
                query = """
                    INSERT INTO library_tracks (
                        file_path, title, artist, album, genre, release_date, duration,
                        mb_track_id, mb_artist_id, acoustid_fingerprint, lyrics, file_mtime, updated_at
                    ) VALUES (
                        %(file_path)s, %(title)s, %(artist)s, %(album)s, %(genre)s, %(release_date)s, %(duration)s,
                        %(mb_track_id)s, %(mb_artist_id)s, %(acoustid_fingerprint)s, %(lyrics)s, %(file_mtime)s, CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (file_path) DO UPDATE SET
                        title = EXCLUDED.title,
                        artist = EXCLUDED.artist,
                        album = EXCLUDED.album,
                        genre = EXCLUDED.genre,
                        release_date = EXCLUDED.release_date,
                        duration = EXCLUDED.duration,
                        mb_track_id = EXCLUDED.mb_track_id,
                        mb_artist_id = EXCLUDED.mb_artist_id,
                        acoustid_fingerprint = EXCLUDED.acoustid_fingerprint,
                        lyrics = EXCLUDED.lyrics,
                        file_mtime = EXCLUDED.file_mtime,
                        updated_at = CURRENT_TIMESTAMP;
                """
                cur.execute(query, track_data)
                self.conn.commit()
        except Exception as e:
            LOG.error(f"DB Upsert Library Track Error: {e}")
            self.conn.rollback()

    def get_all_library_tracks(self) -> list[dict]:
        """Fetch all library tracks with their paths and mtimes for sync comparison."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT file_path, file_mtime, title, artist, album, mb_track_id FROM library_tracks")
                columns = [col[0] for col in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
        except Exception as e:
            LOG.error(f"DB Read Error (get_all_library_tracks): {e}")
            return []

    def delete_library_tracks_not_in(self, active_paths: list[str]) -> int:
        """
        Delete library tracks whose file_paths are NOT in the active_paths list.
        Useful for pruning records of files deleted from disk.
        Returns the number of deleted rows.
        """
        try:
            with self.conn.cursor() as cur:
                if not active_paths:
                    # If empty list passed, delete all.
                    cur.execute("DELETE FROM library_tracks")
                else:
                    cur.execute("DELETE FROM library_tracks WHERE file_path != ALL(%s)", (active_paths,))
                deleted_count = cur.rowcount
                self.conn.commit()
                return deleted_count
        except Exception as e:
            LOG.error(f"DB Delete Library Tracks Error: {e}")
            self.conn.rollback()
            return 0


