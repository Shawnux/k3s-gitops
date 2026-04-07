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
SERIES_THRESHOLD = 15     # Flag as repetitive if pattern appears this many times

# Keywords to prioritize "better" versions of the same album
QUALITY_PRIORITY = {
    "super deluxe": 11,
    "deluxe": 10,
    "complete": 9,
    "expanded": 8,
    "special": 7,
    "bonus": 6,
    "remaster": 5
}

def load_session():
    session = tidalapi.Session()
    path = os.getenv('SESSION_PATH', 'session.json') 
    with open(path, 'r') as f:
        data = json.load(f)
    session.load_oauth_session(data['token_type'], data['access_token'], data['refresh_token'])
    return session

def get_active_playlist(session):
    playlists = session.user.playlists()
    vols = sorted([p for p in playlists if p.name.startswith(PLAYLIST_PREFIX)], key=lambda x: x.name)
    if not vols:
        print("No volumes found. Creating Volume 1...")
        return session.user.create_playlist(f"{PLAYLIST_PREFIX} - Vol 1", "Automated GitOps Sync")
    active_vol = vols[-1]
    current_track_count = len(active_vol.tracks())
    print(f"Current active volume: {active_vol.name} ({current_track_count}/{MAX_TRACKS_PER_VOL} tracks)")
    if current_track_count >= MAX_TRACKS_PER_VOL:
        new_vol_num = len(vols) + 1
        print(f"Volume full! Rolling over to Volume {new_vol_num}...")
        return session.user.create_playlist(f"{PLAYLIST_PREFIX} - Vol {new_vol_num}", "Automated GitOps Sync")
    return active_vol

def get_base_pattern(title):
    if not title: return ""
    t = str(title).lower()
    t = re.sub(r'[\(\[].*?[\)\]]', '', t)
    t = re.sub(r'\d+', '', t)
    t = re.sub(r'[^\w\s]', '', t)
    return ' '.join(t.split())

def get_quality_score(title):
    score = 0
    t = str(title).lower()
    for keyword, value in QUALITY_PRIORITY.items():
        if keyword in t:
            score = max(score, value)
    return score

def modify_playlist(session, playlist, track_ids, mode="add"):
    """Handles adding or removing tracks with ETag/412 protection."""
    verb = "Adding" if mode == "add" else "Removing"
    print(f"  -> {verb} {len(track_ids)} tracks...")
    
    for i in range(0, len(track_ids), CHUNK_SIZE):
        chunk = track_ids[i:i + CHUNK_SIZE]
        retries = 3
        while retries > 0:
            try:
                # Refresh playlist object to get latest ETag
                playlist = session.playlist(playlist.id)
                if mode == "add":
                    playlist.add(chunk)
                else:
                    # In tidalapi, remove usually requires index or ID list
                    playlist.remove_by_id(chunk)
                break 
            except Exception as e:
                if "412" in str(e):
                    time.sleep(2)
                    retries -= 1
                else:
                    print(f"    [!] Error during {mode}: {e}")
                    break 
        time.sleep(2)
    return playlist

def sync_library():
    print("--- Starting Tidal Sync ---")
    session = load_session()
    if not session.check_login():
        raise Exception("Session invalid. The refresh token may have expired or been revoked.")
        
    target_playlist = get_active_playlist(session)
    # Cache existing tracks as objects to check titles for the purge
    current_tracks = target_playlist.tracks()
    existing_track_ids = {track.id for track in current_tracks}
    
    favorite_artists = session.user.favorites.artists()
    print(f"Found {len(favorite_artists)} favorite artists. Processing...\n")

    for artist in favorite_artists:
        print(f"Fetching: {artist.name}")
        
        raw_discography = []
        for method in ['get_albums', 'albums', 'get_singles', 'singles', 'get_ep_singles', 'eps']:
            if hasattr(artist, method):
                try:
                    releases = getattr(artist, method)()
                    if releases: raw_discography.extend(releases)
                except: pass
        
        if not raw_discography:
            continue

        # 1. Dynamic Spam Detection (Frequency Analysis)
        base_titles = [get_base_pattern(getattr(r, 'name', '')) for r in raw_discography]
        title_counts = Counter(base_titles)
        dynamic_blocklist = {base for base, count in title_counts.items() if count >= SERIES_THRESHOLD and len(base) > 2}
        
        # 2. Deluxe Deduplication & Purge Logic
        deduped_dict = {}
        track_ids_to_purge = []

        # Identify tracks ALREADY in the playlist that match the new dynamic blocklist
        if dynamic_blocklist:
            print(f"  [i] Series detected: {', '.join(dynamic_blocklist)}")
            for track in current_tracks:
                # If the track title or album title matches a blocked series, marked for purge
                # Note: This is an expensive check, but essential for cleaning 'polluted' playlists
                if get_base_pattern(track.name) in dynamic_blocklist:
                    track_ids_to_purge.append(track.id)

        for release in raw_discography:
            title = getattr(release, 'name', '')
            base_pattern = get_base_pattern(title)
            
            # If release is in blocklist, collect its IDs for purging if they exist in the playlist
            if base_pattern in dynamic_blocklist:
                try:
                    for t in release.tracks():
                        if t.id in existing_track_ids:
                            track_ids_to_purge.append(t.id)
                except: pass
                continue

            # Standard Deluxe Logic
            score = get_quality_score(title)
            try: t_count = len(release.tracks())
            except: t_count = 0

            if base_pattern not in deduped_dict or score > deduped_dict[base_pattern][0]:
                deduped_dict[base_pattern] = (score, t_count, release)
            elif score == deduped_dict[base_pattern][0] and t_count > deduped_dict[base_pattern][1]:
                deduped_dict[base_pattern] = (score, t_count, release)

        # 3. Execute Purge (Remove Repetitive Content)
        track_ids_to_purge = list(set(track_ids_to_purge)) # Unique IDs only
        if track_ids_to_purge:
            print(f"  [!] Cleaning up {len(track_ids_to_purge)} repetitive/spam tracks from playlist...")
            target_playlist = modify_playlist(session, target_playlist, track_ids_to_purge, mode="remove")
            # Update cache so we don't think they are still there
            existing_track_ids = existing_track_ids - set(track_ids_to_purge)

        # 4. Extract and Add New Unique Tracks
        final_releases = [v[2] for v in deduped_dict.values()]
        track_ids_to_add = []
        for release in final_releases:
            try:
                for track in release.tracks():
                    if track.id not in existing_track_ids:
                        track_ids_to_add.append(track.id)
            except: continue
                
        if track_ids_to_add:
            target_playlist = modify_playlist(session, target_playlist, track_ids_to_add, mode="add")
            existing_track_ids.update(track_ids_to_add) 
        else:
            print("  -> Up to date.")

    print("\n--- Sync Complete ---")

if __name__ == "__main__":
    sync_library()