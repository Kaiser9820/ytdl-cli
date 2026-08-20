"""
YouTube to MP3 Converter CLI
Downloads YouTube videos as MP3, adds enriched metadata via external APIs (Deezer / TVmaze),
and embeds high-res album artwork.

Requirements:
    pip install yt-dlp mutagen pillow requests
    FFmpeg must be installed and in your PATH
"""

import os
import re
from io import BytesIO
import requests
from PIL import Image
import yt_dlp
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, ID3NoHeaderError

# ────────────────────────────────────────────────
# CONFIGURATION
# ────────────────────────────────────────────────

DOWNLOAD_DIR = os.path.expanduser("~/Downloads/ConvertedAudio")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ────────────────────────────────────────────────
# HELPER & METADATA ENRICHMENT APIs
# ────────────────────────────────────────────────

def clean_song_title(title: str) -> str:
    """Remove video clutter like (Official Video), [HD], ft. Artist, etc."""
    cleaned = re.sub(r'[\(\[\{].*?[\)\]\}]', '', title)
    cleaned = re.sub(r'(?i)\b(official video|lyric video|official audio|audio|hd|4k|remastered)\b', '', cleaned)
    return cleaned.strip()

def classify_content(title: str, categories: list) -> str:
    """Determine if the video is a TV Show, Music, or General Video."""
    categories = [c.lower() for c in (categories or [])]
    title_lower = title.lower()

    # Check for TV show indicators (S01E01, Season 1 Ep 2, etc.)
    tv_pattern = re.search(r'(s\d{1,2}\s*e\d{1,2}|season\s*\d+\s*episode\s*\d+)', title_lower)
    if tv_pattern or 'shows' in categories or 'film & animation' in categories:
        return 'tv_show'

    # If title has " - " or category is Music, classify as music
    if ' - ' in title or 'music' in categories or 'official music video' in title_lower:
        return 'music'

    return 'general'

def enrich_music(artist: str, title: str) -> dict:
    """Fetch track, album, release date, genre, and high-res artwork from Deezer."""
    cleaned_title = clean_song_title(title)
    
    # Try exact match, combined query, and track title alone
    queries = [
        f'artist:"{artist}" track:"{cleaned_title}"',
        f'{artist} {cleaned_title}',
        f'track:"{cleaned_title}"'
    ]

    for query in queries:
        url = f"https://api.deezer.com/search/track?q={query}&limit=1"
        try:
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            data = resp.json()

            if data.get('data'):
                track = data['data'][0]
                album_info = track.get('album', {})
                album_id = album_info.get('id')

                release_date = ""
                genre_name = "Music"

                # Query the album endpoint for release date and specific genre
                if album_id:
                    album_resp = requests.get(f"https://api.deezer.com/album/{album_id}", timeout=5)
                    if album_resp.status_code == 200:
                        album_data = album_resp.json()
                        release_date = album_data.get('release_date', '')[:4]
                        
                        genres = album_data.get('genres', {}).get('data', [])
                        if genres and len(genres) > 0:
                            genre_name = genres[0].get('name', 'Music')

                return {
                    'title': track.get('title_short', title),
                    'artist': track.get('artist', {}).get('name', artist),
                    'album': album_info.get('title', f"{title} - Single"),
                    'date': release_date,
                    'genre': genre_name,
                    'cover_url': album_info.get('cover_xl') or album_info.get('cover_big')
                }
        except requests.RequestException as e:
            print(f"  [!] Deezer query failed: {e}")

    # Dynamic fallback if no Deezer match is found
    return {
        'album': f"{title} - Single",
        'genre': 'Music'
    }

def enrich_tv(title: str, uploader: str) -> dict:
    """Extract show name and fetch metadata from TVmaze."""
    show_name = uploader 
    match = re.split(r'(?i)(s\d{1,2}\s*e\d{1,2}|-)', title)
    if match and match[0].strip():
        show_name = match[0].strip()

    url = f"https://api.tvmaze.com/search/shows?q={show_name}"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        
        if data:
            show = data[0]['show']
            genres = ", ".join(show.get('genres', []))
            network = show.get('network', {}).get('name') or show.get('webChannel', {}).get('name') or uploader
            return {
                'artist': network,
                'album': show.get('name'),
                'genre': genres if genres else 'TV Show',
                'date': show.get('premiered', '')[:4]
            }
    except requests.RequestException as e:
        print(f"  [!] TVmaze lookup failed: {e}")
        
    return {}


# ────────────────────────────────────────────────
# CORE PROCESSING
# ────────────────────────────────────────────────

def download_and_process(url: str) -> None:
    print("Downloading audio...")

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': os.path.join(DOWNLOAD_DIR, '%(uploader)s - %(title)s.%(ext)s'),
        'continuedl': True,
        'noplaylist': True,
        'quiet': False,
        'no_warnings': True,

        # ────── Fix HTTP 403 Forbidden ──────
        'extractor_args': {
            'youtube': {
                'player_client': ['mweb', 'ios', 'web']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        }
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
        except Exception as e:
            print(f"\nFailed to download video: {e}")
            return

    # ────── Base Metadata Extraction ──────
    yt_title = info.get('title', 'Unknown Title')
    yt_uploader = info.get('uploader') or info.get('channel') or 'Unknown Artist'
    yt_date = info.get('upload_date', '')[:4] if info.get('upload_date') else ''
    categories = info.get('categories', [])

    # Base metadata structure
    meta = {
        'title': yt_title,
        'artist': yt_uploader,
        'album': f"{yt_title} - Single",
        'date': yt_date,
        'genre': 'Music'
    }

    # Split "Artist - Title" format if present
    if ' - ' in yt_title:
        parts = yt_title.split(' - ', 1)
        if len(parts[0]) < 40 and parts[0].strip():
            meta['artist'] = parts[0].strip()
            meta['title'] = parts[1].strip()

    # ────── Content Classification & API Routing ──────
    content_type = classify_content(yt_title, categories)
    deezer_cover_url = None

    if content_type == 'music':
        print("\n  🎵 Querying Deezer API...")
        enriched = enrich_music(meta['artist'], meta['title'])
        deezer_cover_url = enriched.pop('cover_url', None)
        meta.update({k: v for k, v in enriched.items() if v})

    elif content_type == 'tv_show':
        print("\n  📺 Querying TVmaze...")
        enriched = enrich_tv(yt_title, yt_uploader)
        meta.update({k: v for k, v in enriched.items() if v})
    else:
        print("\n  🎬 Detected General Video. Using default metadata.")

    print(f"  Title:  {meta['title']}")
    print(f"  Artist: {meta['artist']}")
    print(f"  Album:  {meta['album']}")
    print(f"  Genre:  {meta['genre']}")
    print(f"  Year:   {meta['date']}")

    # ────── Locate the MP3 file ──────
    mp3_path = None
    latest_mtime = 0
    for filename in os.listdir(DOWNLOAD_DIR):
        if filename.lower().endswith('.mp3'):
            path = os.path.join(DOWNLOAD_DIR, filename)
            mtime = os.path.getmtime(path)
            if mtime > latest_mtime:
                latest_mtime = mtime
                mp3_path = path

    if not mp3_path or os.path.getsize(mp3_path) < 100_000:
        print("Could not locate MP3 file. Check download folder manually.")
        return

    # ────── Write ID3 Metadata ──────
    try:
        tags = EasyID3(mp3_path)
    except ID3NoHeaderError:
        tags = EasyID3()
        tags.save(mp3_path)

    for key, value in meta.items():
        if value:
            tags[key] = value
    tags.save(mp3_path)
    print("  ✓ Text metadata tags updated.")

    # ────── Embed High-Res Artwork ──────
    artwork_url = deezer_cover_url or info.get('thumbnail')
    if artwork_url:
        try:
            resp = requests.get(artwork_url, timeout=10)
            if resp.status_code == 200:
                img = Image.open(BytesIO(resp.content))
                img_buffer = BytesIO()
                img.convert('RGB').save(img_buffer, format='JPEG')
                
                id3_tags = ID3(mp3_path)
                id3_tags.add(APIC(
                    encoding=3, mime='image/jpeg', type=3, desc='Cover',
                    data=img_buffer.getvalue()
                ))
                id3_tags.save(mp3_path, v2_version=3)
                print("  ✓ Album artwork embedded.")
        except Exception as e:
            print(f"  [!] Could not add artwork: {e}")

    # Success output since we are no longer moving it
    print(f"\nSuccess! File ready and saved in: {mp3_path}")


def main():
    print("YouTube to MP3 Tool (Enriched Version)")
    print("-" * 60)
    while True:
        url = input("\nPaste YouTube URL (or 'q' to quit): ").strip()
        if url.lower() in ['quit', 'q', 'exit']:
            break
        if not url:
            continue
        if "youtube.com" not in url and "youtu.be" not in url:
            print("Not a valid YouTube URL.")
            continue

        download_and_process(url)
        print("-" * 60)

if __name__ == "__main__":
    main()
