import tidalapi
import musicbrainzngs
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

STATIC_BLOCKLIST = [
    "group therapy", "a state of trance", "asot", "abgt", "corstens countdown", 
    "wake your mind", "electric for life", "purified", "club life", "satisfaction",
    "live at", "live from", "live set", "tomorrowland", "ultra music", 
    "electric daisy carnival", "edc", "awakenings", "creamfields", "defqon"
]

QUALITY_PRIORITY = {
    "super deluxe": 11, "deluxe": 10, "complete": 9, "expanded": 8,
    "special": 7, "bonus": 6, "remaster": 5
}

# --- MusicBrainz Public API Setup ---
musicbrainzngs.set_useragent("TidalGitOpsSync", "1.3", "homelab-automation")

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
    pattern = get_base_pattern(title)
    for b in blocklist:
        if len(b) >= 3 and b in pattern:
            return True
    return False

def get_quality_score(title):
    score = 0
    t = str(title).lower()
    for keyword, value in QUALITY_PRIORITY.items():
        if keyword in t: score = max(score, value)
    return score

def get_official_albums(tidal_artist):
    """Smart Entity Resolution: Cross-references Tidal albums with MusicBrainz."""
    print(f"  [MB] Resolving entity for {tidal_artist.name}...")
    mbid = None
    
    try:
        albums = tidal_artist.get_albums()
        if albums:
            ref_album = albums[0].name
            clean_ref = re.sub(r'[^\w\s]', '', ref_album).strip()
            
            time.sleep(1.5)
            rg_search = musicbrainzngs.search_release_groups(
                query=f'artist:"{tidal_artist.name}" AND releasegroup:"{clean_ref}"', 
                limit=1
            )
            if rg_search.get('release-group-list'):
                mbid = rg_search['release-group-list'][0]['artist-credit'][0]['artist']['id']
                print("    -> Entity resolved via Album cross-reference.")
    except Exception:
        pass

    if not mbid:
        try:
            time.sleep(1.5)
            search = musicbrainzngs.search_artists(artist=tidal_artist.name, limit=3)
            if search.get('artist-list'):
                mbid = search['artist-list'][0]['id']
                print("    -> Entity resolved via Top Relevance search.")
        except Exception:
            return set()

    if not mbid:
        return set()

    try:
        time.sleep(1.5)
        # UPGRADE: Now includes 'single' to catch EDM producers who don't drop albums
        releases = musicbrainzngs.browse_release_groups(
            artist=mbid, 
            release_type=['album', 'ep', 'single'], 
            limit=100
        )
        
        canonical_titles = set()
        for rg in releases.get('release-group-list', []):
            secondary = rg.get('secondary-type-list', [])
            if any(bad in secondary for bad in ['Live', 'Compilation', 'Mixtape/Street', 'Broadcast', 'Remix']):
                continue
            canonical_titles.add(get_base_pattern(rg['title']))
            
        return canonical_titles
    except Exception:
        return set()

def add_chunk_with_fallback(session, playlist, chunk):
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
                for tid in chunk:
                    try:
                        playlist = session.playlist(playlist.id)
                        playlist.add([tid])
                        added_count += 1
                    except: pass
                return added_count
            else:
                break
    return added_count

def sync_library():
    print("--- Starting Hybrid Source-of-Truth Sync ---")
    session = load_session()
    if not session.check_login(): raise Exception("Session invalid.")
        
    playlists = session.user.playlists()
    all_vols = sorted([p for p in playlists if p.name.startswith(PLAYLIST_PREFIX)], key=lambda x: x.name)
    
    if not all_vols:
        target_playlist = session.user.create_playlist(f"{PLAYLIST_PREFIX} - Vol 1", "Automated GitOps Sync")
        all_vols.append(target_playlist)
    else:
        target_playlist = all_vols[-1]

    existing_track_ids = set()
    for vol in all_vols:
        try: vol_tracks = vol.tracks(limit=10000)
        except: vol_tracks = vol.tracks()
        existing_track_ids.update({t.id for t in vol_tracks})
        if vol.id == target_playlist.id:
            current_vol_track_count = len(vol_tracks)
    
    print("Fetching full artist list from Tidal...")
    try:
        favorite_artists = session.user.favorites.artists(limit=1000)
    except TypeError:
        favorite_artists = list(session.user.favorites.artists())
        
    print(f"Found {len(favorite_artists)} favorite artists. Processing...\n")

    for artist in favorite_artists:
        print(f"\nFetching: {artist.name}")
        
        # --- The Hybrid Logic Gateway ---
        use_mb_filter = True
        canonical_titles = get_official_albums(artist)
        
        if not canonical_titles:
            print("  [!] Authority check failed/empty. Degading to Fuzzy Filter fallback...")
            use_mb_filter = False

        raw_discography = []
        for method in ['get_albums', 'albums', 'get_singles', 'singles', 'get_ep_singles', 'eps']:
            if hasattr(artist, method):
                try:
                    releases = getattr(artist, method)()
                    if releases: raw_discography.extend(releases)
                except: pass
        
        if not raw_discography: continue

        # If MB failed, dynamically generate the frequency blocklist for this specific artist
        master_blocklist = set(STATIC_BLOCKLIST)
        if not use_mb_filter:
            base_titles = [get_base_pattern(getattr(r, 'name', '')) for r in raw_discography]
            title_counts = Counter(base_titles)
            dynamic_blocklist = {base for base, count in title_counts.items() if count >= SERIES_THRESHOLD and len(base) > 2}
            master_blocklist = master_blocklist.union(dynamic_blocklist)
        
        deduped_dict = {}

        for release in raw_discography:
            title = getattr(release, 'name', '')
            base_pattern = get_base_pattern(title)
            
            # Apply the appropriate filter engine
            if use_mb_filter:
                if base_pattern not in canonical_titles:
                    continue
            else:
                if is_spam(title, master_blocklist):
                    continue

            score = get_quality_score(title)
            try: t_count = len(release.tracks())
            except: t_count = 0

            if base_pattern not in deduped_dict or score > deduped_dict[base_pattern][0]:
                deduped_dict[base_pattern] = (score, t_count, release)
            elif score == deduped_dict[base_pattern][0] and t_count > deduped_dict[base_pattern][1]:
                deduped_dict[base_pattern] = (score, t_count, release)

        final_releases = [v[2] for v in deduped_dict.values()]
        track_ids_to_add = []
        for release in final_releases:
            try:
                for track in release.tracks():
                    # Double check tracks against the blocklist just in case
                    if not is_spam(track.name, set(STATIC_BLOCKLIST)) and track.id not in existing_track_ids:
                        track_ids_to_add.append(track.id)
            except: continue
                
        if track_ids_to_add:
            status = "verified studio tracks" if use_mb_filter else "fuzzy-filtered tracks"
            print(f"  -> Queued {len(track_ids_to_add)} {status}...")
            
            for i in range(0, len(track_ids_to_add), CHUNK_SIZE):
                chunk = track_ids_to_add[i:i + CHUNK_SIZE]
                
                if current_vol_track_count + len(chunk) > MAX_TRACKS_PER_VOL:
                    new_vol_num = len(all_vols) + 1
                    print(f"  [!] Capacity Reached. Rolling over to Volume {new_vol_num}...")
                    target_playlist = session.user.create_playlist(f"{PLAYLIST_PREFIX} - Vol {new_vol_num}", "Automated GitOps Sync")
                    all_vols.append(target_playlist)
                    current_vol_track_count = 0
                
                added = add_chunk_with_fallback(session, target_playlist, chunk)
                current_vol_track_count += added
                existing_track_ids.update(chunk)
                time.sleep(1)
        else:
            print("  -> Up to date.")

if __name__ == "__main__":
    sync_library()