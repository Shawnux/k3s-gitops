import tidalapi
import json
import os
import time
import re
from collections import Counter

# --- Configuration ---
PLAYLIST_PREFIX = "Master Discography"
MAX_TRACKS_PER_VOL = 9500 
CHUNK_SIZE = 50           
SERIES_THRESHOLD = 15     

# The "Merciless" Blocklist - Fuzzy matches against these phrases.
STATIC_BLOCKLIST = [
    "group therapy", "a state of trance", "asot", "abgt", "corstens countdown", 
    "wake your mind", "electric for life", "purified", "club life", "satisfaction"
]

QUALITY_PRIORITY = {
    "super deluxe": 11, "deluxe": 10, "complete": 9, "expanded": 8,
    "special": 7, "bonus": 6, "remaster": 5
}

def load_session():
    session = tidalapi.Session()
    path = os.getenv('SESSION_PATH', 'session.json') 
    with open(path, 'r') as f:
        data = json.load(f)
    session.load_oauth_session(data['token_type'], data['access_token'], data['refresh_token'])
    return session

def get_base_pattern(title):
    if not title: return ""
    t = str(title).lower()
    t = re.sub(r'[\(\[].*?[\)\]]', '', t)
    t = re.sub(r'\d+', '', t)
    t = re.sub(r'[^\w\s]', '', t)
    return ' '.join(t.split())

def is_spam(title, blocklist):
    """Fuzzy matching: Returns True if any blocklist phrase is INSIDE the title."""
    pattern = get_base_pattern(title)
    for b in blocklist:
        # If the blocked phrase is at least 4 letters and exists inside the title
        if len(b) > 3 and b in pattern:
            return True
    return False

def get_quality_score(title):
    score = 0
    t = str(title).lower()
    for keyword, value in QUALITY_PRIORITY.items():
        if keyword in t: score = max(score, value)
    return score

def add_chunk_with_fallback(session, playlist, chunk):
    """Adds tracks and returns the number of successfully added tracks."""
    retries = 3
    added_count = 0
    while retries > 0:
        try:
            playlist = session.playlist(playlist.id)
            playlist.add(chunk)
            return len(chunk)
        except Exception as e:
            if "412" in str(e):
                time.sleep(2)
                retries -= 1
            elif "400" in str(e):
                print("    [!] 400 Bad Request. Isolating bad tracks...")
                for tid in chunk:
                    try:
                        playlist = session.playlist(playlist.id)
                        playlist.add([tid])
                        added_count += 1
                    except: pass
                return added_count
            else:
                print(f"    [!] Error: {e}")
                break
    return added_count

def sync_library():
    print("--- Starting Tidal Sync ---")
    session = load_session()
    if not session.check_login(): raise Exception("Session invalid.")
        
    # --- Bootup & Volume Discovery ---
    playlists = session.user.playlists()
    all_vols = sorted([p for p in playlists if p.name.startswith(PLAYLIST_PREFIX)], key=lambda x: x.name)
    
    if not all_vols:
        print("No volumes found. Creating Volume 1...")
        target_playlist = session.user.create_playlist(f"{PLAYLIST_PREFIX} - Vol 1", "Automated GitOps Sync")
        all_vols.append(target_playlist)
    else:
        target_playlist = all_vols[-1]

    existing_track_ids = set()
    print("Building multi-volume cache...")
    for vol in all_vols:
        try: vol_tracks = vol.tracks(limit=10000)
        except: vol_tracks = vol.tracks()
        existing_track_ids.update({t.id for t in vol_tracks})
        if vol.id == target_playlist.id:
            current_vol_track_count = len(vol_tracks)

    print(f"Active Target: {target_playlist.name} (Starting at {current_vol_track_count} tracks)\n")
    
    favorite_artists = session.user.favorites.artists()

    for artist in favorite_artists:
        print(f"Fetching: {artist.name}")
        
        raw_discography = []
        for method in ['get_albums', 'albums', 'get_singles', 'singles', 'get_ep_singles', 'eps']:
            if hasattr(artist, method):
                try:
                    releases = getattr(artist, method)()
                    if releases: raw_discography.extend(releases)
                except: pass
        
        if not raw_discography: continue

        # 1. Build Master Blocklist
        base_titles = [get_base_pattern(getattr(r, 'name', '')) for r in raw_discography]
        title_counts = Counter(base_titles)
        dynamic_blocklist = {base for base, count in title_counts.items() if count >= SERIES_THRESHOLD and len(base) > 2}
        master_blocklist = set(STATIC_BLOCKLIST).union(dynamic_blocklist)
        
        deduped_dict = {}

        # 2. Filter & Deduplicate
        for release in raw_discography:
            title = getattr(release, 'name', '')
            base_pattern = get_base_pattern(title)
            
            # Fuzzy match against the master blocklist
            if is_spam(title, master_blocklist):
                continue

            score = get_quality_score(title)
            try: t_count = len(release.tracks())
            except: t_count = 0

            if base_pattern not in deduped_dict or score > deduped_dict[base_pattern][0]:
                deduped_dict[base_pattern] = (score, t_count, release)
            elif score == deduped_dict[base_pattern][0] and t_count > deduped_dict[base_pattern][1]:
                deduped_dict[base_pattern] = (score, t_count, release)

        # 3. Extract Unique Tracks
        final_releases = [v[2] for v in deduped_dict.values()]
        track_ids_to_add = []
        for release in final_releases:
            try:
                for track in release.tracks():
                    # Double check track title against blocklist just in case
                    if not is_spam(track.name, master_blocklist) and track.id not in existing_track_ids:
                        track_ids_to_add.append(track.id)
            except: continue
                
        # 4. Dynamic Addition & Rollover
        if track_ids_to_add:
            print(f"  -> Queued {len(track_ids_to_add)} pristine tracks...")
            
            for i in range(0, len(track_ids_to_add), CHUNK_SIZE):
                chunk = track_ids_to_add[i:i + CHUNK_SIZE]
                
                # Check Rollover BEFORE adding
                if current_vol_track_count + len(chunk) > MAX_TRACKS_PER_VOL:
                    new_vol_num = len(all_vols) + 1
                    print(f"  [!] Capacity Reached. Rolling over to Volume {new_vol_num}...")
                    target_playlist = session.user.create_playlist(f"{PLAYLIST_PREFIX} - Vol {new_vol_num}", "Automated GitOps Sync")
                    all_vols.append(target_playlist)
                    current_vol_track_count = 0
                
                # Add and track state
                added = add_chunk_with_fallback(session, target_playlist, chunk)
                current_vol_track_count += added
                existing_track_ids.update(chunk)
                time.sleep(1)
        else:
            print("  -> Up to date.")

    print("\n--- Sync Complete ---")

if __name__ == "__main__":
    sync_library()