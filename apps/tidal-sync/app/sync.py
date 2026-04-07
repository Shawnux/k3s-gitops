import tidalapi
import json
import os
import time

# --- Configuration ---
PLAYLIST_PREFIX = "Master Discography"
MAX_TRACKS_PER_VOL = 9500 # Leave a 500-track buffer below Tidal's 10k limit
CHUNK_SIZE = 50           # How many tracks to add in a single API request

def load_session():
    """Loads the headless session from our saved JSON file."""
    session = tidalapi.Session()
    # When deployed to K8s, this path will point to our mounted Secret
    path = os.getenv('SESSION_PATH', 'session.json') 
    
    with open(path, 'r') as f:
        data = json.load(f)
        
    # Re-hydrate the session using the refresh token
    session.load_oauth_session(
        data['token_type'], 
        data['access_token'], 
        data['refresh_token']
    )
    return session

def get_active_playlist(session):
    """Finds the current volume to add to, or creates the next one if full."""
    playlists = session.user.playlists()
    
    # Find all playlists that match our prefix and sort them by name (Vol 1, Vol 2...)
    vols = sorted([p for p in playlists if p.name.startswith(PLAYLIST_PREFIX)], key=lambda x: x.name)
    
    if not vols:
        print("No volumes found. Creating Volume 1...")
        return session.user.create_playlist(f"{PLAYLIST_PREFIX} - Vol 1", "Automated GitOps Sync")
    
    active_vol = vols[-1]
    current_track_count = len(active_vol.tracks())
    
    print(f"Current active volume: {active_vol.name} ({current_track_count}/{MAX_TRACKS_PER_VOL} tracks)")
    
    # Rollover logic
    if current_track_count >= MAX_TRACKS_PER_VOL:
        new_vol_num = len(vols) + 1
        print(f"Volume full! Rolling over to Volume {new_vol_num}...")
        return session.user.create_playlist(f"{PLAYLIST_PREFIX} - Vol {new_vol_num}", "Automated GitOps Sync")
    
    return active_vol

def sync_library():
    print("--- Starting Tidal Sync ---")
    session = load_session()
    
    if not session.check_login():
        raise Exception("Session invalid. The refresh token may have expired or been revoked.")
        
    target_playlist = get_active_playlist(session)
    
    # Cache the IDs of tracks already in the ACTIVE volume so we don't re-add them tonight
    existing_track_ids = {track.id for track in target_playlist.tracks()}
    
    favorite_artists = session.user.favorites.artists()
    print(f"Found {len(favorite_artists)} favorite artists. Processing...\n")

    for artist in favorite_artists:
        print(f"Fetching: {artist.name}")
        
        full_discography = []
        
        # Dynamically check and call available discography methods
        # to ensure compatibility across different versions of the tidalapi
        for method in ['get_albums', 'albums', 'get_singles', 'singles', 'get_ep_singles', 'eps']:
            if hasattr(artist, method):
                try:
                    releases = getattr(artist, method)()
                    if releases:
                        full_discography.extend(releases)
                except Exception as e:
                    pass
        
        if not full_discography:
            print(f"  [!] No releases found or failed to fetch for {artist.name}")
            continue
            
        track_ids_to_add = []
        
        # Extract track IDs from every release
        for release in full_discography:
            try:
                for track in release.tracks():
                    # Only queue the track if it's not already in tonight's target volume
                    if track.id not in existing_track_ids:
                        track_ids_to_add.append(track.id)
            except Exception as e:
                # Catch edge cases where a track might be region-locked or unavailable
                continue
                
        if track_ids_to_add:
            print(f"  -> Adding {len(track_ids_to_add)} new tracks...")
            
            # Add tracks in chunks to respect API rate limits
            for i in range(0, len(track_ids_to_add), CHUNK_SIZE):
                chunk = track_ids_to_add[i:i + CHUNK_SIZE]
                target_playlist.add(chunk)
                
                # Update our local cache so we don't double-add if an artist has identical tracks on different releases
                existing_track_ids.update(chunk) 
                time.sleep(1) # Tiny sleep to be polite to Tidal's servers
        else:
            print("  -> Up to date.")

    print("\n--- Sync Complete ---")

if __name__ == "__main__":
    sync_library()