"""Whole runs of `tidal-disco sync` against the fake TIDAL server."""

import json
import threading
import urllib.request

from tidal_disco import login
from tidal_disco.state import State

P1 = "Master Discography 01"


def catalog(fake):
    """Three followed artists with the kinds of release that used to go wrong."""
    a = fake.artist("a", "Aurora Fields")
    b = fake.artist("b", "The Blue Hour")
    c = fake.artist("c", "Cassette Club")
    other = fake.artist("x", "Somebody Else", follow=False)
    va = fake.artist("va", "Various Artists", follow=False)
    t = {}
    # Aurora: a 25-track album (two pages), a single, a clean/explicit pair, and junk
    t["a_album"] = fake.album("a1", "Long Player", [a], [(f"LP {n:02d}", [a]) for n in range(1, 26)], date="2021-04-02")
    t["a_single"] = fake.album("a2", "Early Single", [a], [("Early Single", [a])], kind="SINGLE", date="2019-02-01")
    t["a_clean"] = fake.album("a3", "Night", [a], [("Dusk", [a]), ("Dawn", [a])], kind="EP", date="2020-06-01")
    t["a_explicit"] = fake.album("a4", "Night", [a], [("Dusk", [a]), ("Dawn", [a])], kind="EP", date="2020-06-01", explicit=True)
    fake.album("a5", "Aurora Fields presents: Summer Sessions (DJ Mix)", [a],
               [(f"Mix {n} (Mixed)", [other]) for n in range(12)], date="2022-01-01")
    fake.album("a6", "Label Sampler Vol. 3", [va], [("Guest Spot", [a])] + [(f"Filler {n}", [other]) for n in range(9)],
               listed_under=[a], date="2018-01-01")
    fake.album("a7", "Aurora's Selections", [a], [("Own One", [a])] + [(f"Pick {n}", [other]) for n in range(7)],
               date="2023-01-01")
    fake.album("a8", "Somebody Else LP", [other], [("Feature", [other, a])] + [(f"Solo {n}", [other]) for n in range(5)],
               listed_under=[a], date="2017-01-01")
    # The Blue Hour: sorted under B, two releases
    t["b_ep"] = fake.album("b1", "Blue EP", [b], [("Blue One", [b]), ("Blue Two", [b])], kind="EP", date="2018-03-03")
    t["b_album"] = fake.album("b2", "Hour", [b], [(f"Hour {n}", [b]) for n in range(1, 4)], date="2022-09-09")
    # Cassette Club: 45 singles, so the release list itself needs three pages
    t["c"] = []
    for n in range(45):
        t["c"] += fake.album(f"c{n:02d}", f"Tape {n:02d}", [c], [(f"Tape {n:02d}", [c])], kind="SINGLE",
                             date=f"20{10 + n // 12:02d}-{n % 12 + 1:02d}-01")
    return t


def expected(t):
    return t["a_single"] + t["a_explicit"] + t["a_album"] + t["b_ep"] + t["b_album"] + t["c"]


def test_first_run_builds_the_real_discography(fake, run):
    t = catalog(fake)
    code, out = run("sync")
    assert code == 0, out
    assert fake.playlist_tracks(P1) == expected(t)
    assert "3 artists, 50 releases, 78 tracks in 1 playlist part(s)." in out
    assert "added 78, removed 0." in out
    playlist = next(iter(fake.playlists.values()))
    assert playlist["access"] == "UNLISTED"
    assert playlist["description"].startswith("Aurora Fields to Cassette Club · 3 artists.")

    report = json.loads(State(run.state_path).get("last_report"))
    reasons = {e["album_id"]: e["reason"] for e in report["excluded"]}
    assert reasons["a3"] == "duplicate edition of a4"
    assert reasons["a5"] == "DJ mix"
    assert reasons["a6"] == "appears on (not a main artist)"
    assert reasons["a7"].startswith("compilation (1 of 8")
    assert reasons["a8"] == "appears on (not a main artist)"
    assert len(reasons) == 5


def test_second_run_changes_nothing_and_is_cheap(fake, run):
    catalog(fake)
    run("sync")
    first_calls = len(fake.calls)
    code, out = run("sync")
    assert code == 0 and "added 0, removed 0." in out
    second = fake.calls[first_calls:]
    assert not [c for c in second if c[0] != "GET" and c[1] != "/token"]
    assert not [c for c in second if c[1].endswith("/relationships/items") and "/playlists/" in c[1]]
    assert not [c for c in second if c[1] == "/v2/albums"]  # album details come from the cache
    assert len(second) < first_calls / 2


def test_new_release_lands_inside_its_artist(fake, run):
    t = catalog(fake)
    run("sync")
    new = fake.album("a9", "Brand New", ["a"], [("New One", ["a"]), ("New Two", ["a"])], kind="EP", date="2026-11-06")
    code, out = run("sync")
    assert code == 0 and "added 2, removed 0." in out
    tracks = fake.playlist_tracks(P1)
    at = tracks.index(new[0])
    assert tracks[at - 1] == t["a_album"][-1] and tracks[at + 2] == t["b_ep"][0]
    assert sorted(tracks) == sorted(expected(t) + new)


def test_unfollow_removes_only_what_the_tool_added(fake, run):
    t = catalog(fake)
    run("sync")
    by_hand = fake.album("h1", "Hand Picked", ["x"], [("By Hand", ["x"])])[0]
    playlist = next(iter(fake.playlists.values()))
    playlist["items"].insert(3, {"track": by_hand, "item": "hand-item"})
    fake._touch(next(iter(fake.playlists)))
    fake.followed.remove("b")
    code, out = run("sync")
    assert code == 0 and "added 0, removed 5." in out
    tracks = fake.playlist_tracks(P1)
    assert by_hand in tracks
    assert not set(t["b_ep"] + t["b_album"]) & set(tracks)
    assert [x for x in tracks if x != by_hand] == t["a_single"] + t["a_explicit"] + t["a_album"] + t["c"]


def test_several_parts_and_stable_assignment(fake, run):
    t = catalog(fake)
    code, out = run("sync", part_target=40, part_cap=45)
    assert code == 0 and "in 3 playlist part(s)" in out
    names = sorted(p["name"] for p in fake.playlists.values())
    assert names == [P1, "Master Discography 02", "Master Discography 03"]
    assert fake.playlist_tracks(P1) == t["a_single"] + t["a_explicit"] + t["a_album"] + t["b_ep"] + t["b_album"]
    assert fake.playlist_tracks("Master Discography 02") == t["c"][:40]
    assert fake.playlist_tracks("Master Discography 03") == t["c"][40:]
    new = fake.album("b3", "Late", ["b"], [("Late", ["b"])], kind="SINGLE", date="2026-01-01")
    run("sync", part_target=40, part_cap=45)
    assert fake.playlist_tracks(P1)[-1] == new[0]
    assert fake.playlist_tracks("Master Discography 02") == t["c"][:40]


def test_dry_run_and_preview_change_nothing(fake, run):
    catalog(fake)
    code, out = run("sync", "--dry-run")
    assert code == 0 and "would add 78" in out and not fake.playlists
    code, out = run("sync", "--only", "aurora")
    assert code == 0 and not fake.playlists
    assert "+ 2021-04-02  ALBUM  Long Player  [25 tracks]" in out
    assert "left out: DJ mix" in out and "The Blue Hour" not in out


def test_rate_limits_token_rotation_and_no_batch_support(fake, run):
    t = catalog(fake)
    fake.rate_limit_every = 7
    fake.batch_disabled = True
    code, out = run("sync")
    assert code == 0, out
    assert fake.playlist_tracks(P1) == expected(t)
    # the rotated refresh token was saved, so the stale one in the environment still works next time
    assert State(run.state_path).get("refresh_token") == fake.refresh_token != "rt-0"
    assert run("sync")[0] == 0


def test_a_failing_artist_keeps_their_tracks(fake, run):
    t = catalog(fake)
    run("sync")
    fake.fail_artists.add("b")
    code, out = run("sync")
    assert code == 0 and "used the last good result" in out
    assert fake.playlist_tracks(P1) == expected(t)


def test_suspicious_follow_list_stops_the_run(fake, run):
    t = catalog(fake)
    run("sync")
    fake.followed[:] = ["a"]
    code, _ = run("sync")
    assert code == 2 and fake.playlist_tracks(P1) == expected(t)
    fake.followed[:] = []
    assert run("sync")[0] == 2


def test_large_removal_needs_force(fake, run):
    t = catalog(fake)
    run("sync")
    fake.artist_albums["c"] = []  # TIDAL suddenly lists far fewer releases
    fake.artist_albums["a"].remove("a1")
    code, out = run("sync")
    assert code == 0 and "removals were skipped" in out
    assert fake.playlist_tracks(P1) == expected(t)
    code, out = run("sync", "--force")
    assert "removed 70" in out and len(fake.playlist_tracks(P1)) == 8


def test_refused_track_is_isolated(fake, run):
    t = catalog(fake)
    bad = t["a_album"][10]
    fake.bad_tracks.add(bad)
    code, out = run("sync")
    assert code == 0 and "TIDAL refused 1 track(s)" in out
    assert fake.playlist_tracks(P1) == [x for x in expected(t) if x != bad]
    code, out = run("sync")  # not retried every night
    assert "added 0, removed 0." in out


def test_all_adds_refused_fails_without_blacklisting(fake, run):
    catalog(fake)
    fake.reject_all_adds = True
    code, out = run("sync")
    assert code == 1 and "refuses every addition" in out
    assert State(run.state_path).unaddable() == set()
    fake.reject_all_adds = False
    code, out = run("sync")
    assert code == 0 and "added 78" in out


def test_on_duplicates_is_dropped_if_tidal_refuses_it(fake, run):
    t = catalog(fake)
    fake.reject_on_duplicates = True
    code, _ = run("sync")
    assert code == 0 and fake.playlist_tracks(P1) == expected(t)


def test_silent_drop_at_the_cap_is_noticed_and_repaired(fake, run):
    t = catalog(fake)
    fake.cap = 60
    code, out = run("sync")
    assert "TIDAL reports 60 items, expected 78" in out
    fake.cap = 10000
    code, out = run("sync")
    assert code == 0 and "added 18" in out
    assert fake.playlist_tracks(P1) == expected(t)


def test_state_loss_adopts_playlists_by_name(fake, run, tmp_path):
    t = catalog(fake)
    run("sync")
    for f in tmp_path.glob("state.db*"):
        f.unlink()
    fake.refresh_token = "rt-0"
    code, out = run("sync")
    assert code == 0 and "added 0, removed 0." in out
    assert len(fake.playlists) == 1 and fake.playlist_tracks(P1) == expected(t)


def test_time_budget_resumes(fake, run):
    t = catalog(fake)
    code, out = run("sync", max_minutes=-1)
    assert code == 0 and "Time budget reached" in out and not fake.playlists
    code, out = run("sync")
    assert code == 0 and fake.playlist_tracks(P1) == expected(t)


def test_rebuild_restores_order(fake, run):
    t = catalog(fake)
    run("sync")
    pid = next(iter(fake.playlists))
    fake.playlists[pid]["items"].reverse()
    fake._touch(pid)
    code, _ = run("sync", "--rebuild")
    assert code == 0 and fake.playlist_tracks(P1) == expected(t)


def test_report_command(fake, run):
    catalog(fake)
    run("sync")
    code, out = run("report", "--artist", "aurora")
    assert code == 0 and "Left out (5 releases)" in out and "Label Sampler Vol. 3" in out


def test_login_exchange(fake):
    url = login.authorize_url("cid", login.REDIRECT, login.SCOPES, "chal", "st")
    assert "code_challenge_method=S256" in url and "collection.read" in url and "client_id=cid" in url
    tokens = login.exchange("cid", "", "good-code", "verifier", login.REDIRECT, fake.url + "/token")
    assert tokens["refresh_token"] == "rt-1"


def test_login_callback_listener():
    got = {}
    thread = threading.Thread(target=lambda: got.update(url=login._wait_for_callback("http://127.0.0.1:18765/tidal")))
    thread.start()
    for _ in range(50):
        try:
            urllib.request.urlopen("http://127.0.0.1:18765/tidal?code=abc&state=s", timeout=2).read()
            break
        except OSError:
            continue
    thread.join(5)
    assert got["url"] == "/tidal?code=abc&state=s"


def test_missing_track_credits_are_looked_up(fake, run):
    t = catalog(fake)
    fake.no_item_credits = True
    code, out = run("sync")
    assert code == 0 and fake.playlist_tracks(P1) == expected(t)
    assert "without a credit check" not in out
    assert ("GET", "/v2/tracks") in fake.calls


def test_named_collections_are_left_out_even_without_credits(fake, run):
    t = catalog(fake)
    fake.album("a10", "A State of Trance Top 20 - November 2025", ["a"],
               [(f"Trance {n}", ["x"]) for n in range(12)], date="2025-11-01")
    fake.no_item_credits = True
    fake.no_track_lookup = True
    code, out = run("sync")
    assert code == 0, out
    report = json.loads(State(run.state_path).get("last_report"))
    reasons = {e["album_id"]: e["reason"] for e in report["excluded"]}
    assert reasons["a10"] == "collection (a state of trance)"
    assert reasons["a5"] == "DJ mix" and reasons["a6"] == "appears on (not a main artist)"
    # an unnamed sampler cannot be told apart without credits, and the run says so
    assert "a7" not in reasons
    assert "2 multi-track releases were kept without a credit check" in out
    assert sorted(e["album_id"] for e in report["unchecked"]) == ["a1", "a7"]


def test_growth_moves_an_artist_to_another_part_without_duplicates(fake, run):
    t = catalog(fake)
    run("sync", part_target=40, part_cap=42)   # 01: Aurora + Blue Hour, 02 and 03: Cassette Club
    new = fake.album("a9", "Big New", ["a"], [(f"New {n}", ["a"]) for n in range(12)], date="2026-11-01")
    code, out = run("sync", part_target=40, part_cap=42)
    assert code == 0 and "skipped" not in out, out
    blue = t["b_ep"] + t["b_album"]
    assert fake.playlist_tracks(P1) == t["a_single"] + t["a_explicit"] + t["a_album"] + new
    assert fake.playlist_tracks("Master Discography 03") == blue + t["c"][40:]
    everything = [x for p in fake.playlists.values() for x in (i["track"] for i in p["items"])]
    assert len(everything) == len(set(everything)) == 90


def test_unfollowing_big_artists_is_not_blocked_by_the_removal_limit(fake, run):
    t = catalog(fake)
    for n in range(3):
        fake.artist(f"z{n}", f"Zed {n}")
        fake.album(f"z{n}a", f"Zed {n}", [f"z{n}"], [(f"Zed {n}", [f"z{n}"])], kind="SINGLE")
    run("sync")
    fake.followed.remove("a")
    fake.followed.remove("c")
    code, out = run("sync")
    assert code == 0 and "removed 73" in out and "skipped" not in out
    assert fake.playlist_tracks(P1)[:5] == t["b_ep"] + t["b_album"]
