# YouTube Music Downloader with Postgres

This project monitors a JSON file or listens for DB requests to download tracks/albums from YouTube. It manages a personal song library using a **PostgreSQL** database for improved state tracking and management.

## Goals

- **Primary Source**: YouTube (via `yt-dlp`).
- **State Management**: PostgreSQL database to track request status (pending, processing, completed, failed).
- **Library Integration**: Checks existing filesystem library before downloading.
- **Production Ready**: Dockerized with Postgres, configurable via `.env`.

---

## Input Methods

### 1. JSON Import
The app monitors `songs.json` (mapped to `/app/songs.json`). When you add items to this file, they are automatically imported into the database and cleared from the file.

**Format**:
```json
{
  "tracks": [
    { "title": "Lose Yourself", "artist": "Eminem" },
    { "title": "Billie Jean", "artist": "Michael Jackson", "album": "Thriller" }
  ],
  "albums": [
    { "artist": "Daft Punk", "title": "Discovery", "year": 2001 }
  ]
}
```

### 2. Direct Database Insertion
You can insert rows into the `requests` table directly:
```sql
INSERT INTO requests (type, title, artist, status, input_key) 
VALUES ('track', 'Song Title', 'Artist', 'pending', 'unique-key-123');
```

## Database Management (CRUD)

There are three primary ways to interact with the database:

### 1. Management CLI (Inside Container)

You can use the provided `manage_db.py` script inside the running container to perform common tasks:

**List Requests**:
```bash
docker exec -it youtube-downloader python3 manage_db.py list --status pending
```

**Add a Song**:
```bash
docker exec -it youtube-downloader python3 manage_db.py add --type track --title "Song Title" --artist "Artist"
```

**Delete a Request**:
```bash
docker exec -it youtube-downloader python3 manage_db.py delete 15
```

**Reset Failed**:
```bash
docker exec -it youtube-downloader python3 manage_db.py reset
```

### 2. Direct SQL (psql)

For full control, access the Postgres CLI directly:

```bash
docker exec -it youtube-db psql -U user -d youtubedb
```

**Useful SQL commands**:
- `SELECT * FROM requests WHERE status = 'failed';`
- `UPDATE requests SET status = 'pending' WHERE id = 12;`
- `DELETE FROM requests WHERE status = 'completed';`

### 3. JSON Import (Legacy Support)

The app still monitors `songs.json`. Adding items there will trigger an automatic one-time import into the database. After import, the file is cleared.

---

## How it Works

1. **Import**: JSON file contents are imported into the Postgres `requests` table.
2. **Library Check**: The system checks if the requested song/album already exists in your local library (configured via `LIBRARY_DIR`). If found, it marks the request as `completed`.
3. **Download**: If not found locally, it searches YouTube and downloads the best audio using `yt-dlp`.
4. **State Update**: The database is updated with the result (download path, youtube metadata) or error status.

---

## Requirements

- Docker & Docker Compose
- valid `songs.json` (or empty one to start)

## Configuration

Configuration is done via environment variables or `.env` file.

### Required
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
- `DOWNLOAD_DIR`: Where to save downloads.
- `SONGS_JSON_PATH`: Path to input JSON.

### Optional
- `LIBRARY_DIR`: Path to existing music library for checking.
- `POLL_INTERVAL_SECONDS`: How often to check for new work (default 60).
- `YT_PROXY`: Proxy for YouTube.

---

## Running

```bash
docker-compose up -d --build
```
This starts the `youtube-downloader` app and a `postgres` database container.

## Database Schema

Table `requests`:
- `id`: Serial Primary Key
- `type`: 'track' or 'album'
- `status`: 'pending', 'processing', 'completed', 'failed'
- `title`, `artist`, `album`: Metadata
- `metadata`: JSONB field for storing YouTube/Filesystem details.