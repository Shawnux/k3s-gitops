import re
from types import SimpleNamespace

from tidal_disco.config import Config
from tidal_disco.plan import Block, base_pattern, build_plan, classify, pack, simplify, sort_name
from tidal_disco.playlists import plan_changes, plan_runs

A = SimpleNamespace(id="a1", name="Alpha")
B = SimpleNamespace(id="b1", name="The Beta")


def track(tid, title="Song", artists=("a1",), isrc=None, version=""):
    return {"id": tid, "title": title, "version": version, "isrc": isrc or f"I{tid}",
            "explicit": False, "vol": 1, "num": 1, "artist_ids": list(artists)}


def album(aid, title="Album", kind="ALBUM", date="2020-01-01", artists=("a1",), tracks=None,
          explicit=False, names=("Alpha",), version="", tags=()):
    return {"id": aid, "title": title, "version": version, "type": kind, "date": date,
            "explicit": explicit, "artist_ids": list(artists) if artists is not None else None,
            "artist_names": list(names), "tracks": tracks if tracks is not None else [track(f"{aid}-1")],
            "popularity": 0, "media_tags": list(tags)}


def why(al, cfg=None, series=frozenset()):
    cfg = cfg or Config()
    names = [simplify(n) for n in cfg.collection_names + cfg.extra_collection_names]
    return classify(al, A, cfg, [re.compile(p, re.I) for p in cfg.exclude_title_regex], names, series)


def test_keeps_an_ordinary_release():
    assert why(album("1")) is None


def test_leaves_out_appears_on_and_various_artists():
    assert "appears on" in why(album("1", artists=("zz",)))
    assert "various" in why(album("1", artists=("a1", "va"), names=("Alpha", "Various Artists")))


def test_leaves_out_compilations_by_track_credits():
    tracks = [track("1"), track("2", artists=("x",)), track("3", artists=("y",)),
              track("4", artists=("z",)), track("5", artists=("w",))]
    assert "compilation (1 of 5 tracks are the artist's)" == why(album("1", tracks=tracks))
    # a short single is never judged by credits
    assert why(album("1", kind="SINGLE", tracks=[track("1", artists=("x",))])) is None
    # unknown credits are not held against a release
    unknown = [dict(track(str(n)), artist_ids=None) for n in range(6)]
    assert why(album("1", tracks=unknown)) is None


def test_leaves_out_dj_mixes_and_title_patterns():
    assert why(album("1", title="Night Drive (DJ Mix)")) == "DJ mix"
    mixed = [track(str(n), title=f"Song {n} (Mixed)") for n in range(6)]
    assert why(album("1", title="Live at the Warehouse", tracks=mixed)) == "DJ mix"
    assert why(album("1", title="Remixed")) is None
    assert "title matches" in why(album("1", title="Karaoke Hits"))
    assert why(album("1", title="Night Drive (DJ Mix)"), Config(exclude_dj_mixes=False)) is None


def test_types_and_config_overrides():
    assert "type SINGLE" in why(album("1", kind="SINGLE"), Config(types=["ALBUM", "EP"]))
    assert why(album("1", artists=("zz",)), Config(force_include_album_ids=["1"])) is None
    assert why(album("1"), Config(force_exclude_album_ids=["1"])) == "excluded in config"
    assert why(album("1", tracks=[])) == "no playable tracks"


def test_order_is_artist_then_date_then_track():
    albums = {
        "a1": [album("late", date="2022-05-01", tracks=[track("L1"), track("L2")]),
               album("early", date="2019-03-01", tracks=[track("E1")])],
        "b1": [album("b", artists=("b1",), tracks=[track("B1", artists=("b1",))])],
    }
    plan = build_plan([B, A], albums, Config())
    assert [b.artist_name for b in plan.blocks] == ["Alpha", "The Beta"]
    assert plan.blocks[0].track_ids == ["E1", "L1", "L2"]
    assert sort_name("The Beta") == "beta"


def test_clean_and_explicit_copies_collapse_to_one():
    clean = album("clean", tracks=[track("c1", "One"), track("c2", "Two")])
    dirty = album("dirty", explicit=True, tracks=[track("d1", "One"), track("d2", "Two")])
    deluxe = album("deluxe", title="Album (Deluxe)", tracks=[track("x1", "One"), track("x3", "Three")])
    plan = build_plan([A], {"a1": [clean, dirty, deluxe]}, Config())
    assert plan.blocks[0].track_ids == ["d1", "d2", "x1", "x3"]
    assert [e["reason"] for e in plan.excluded] == ["duplicate edition of dirty"]
    assert build_plan([A], {"a1": [clean, dirty]}, Config(prefer_explicit=False)).blocks[0].track_ids == ["c1", "c2"]
    assert len(build_plan([A], {"a1": [clean, dirty]}, Config(dedupe="none")).blocks[0].track_ids) == 4


def test_recordings_mode_keeps_the_album_copy():
    single = album("s", title="Hit", kind="SINGLE", date="2019-01-01", tracks=[track("s1", "Hit", isrc="X1")])
    full = album("f", date="2019-06-01", tracks=[track("f1", "Hit", isrc="X1"), track("f2", "Other", isrc="X2")])
    editions = build_plan([A], {"a1": [single, full]}, Config())
    assert editions.blocks[0].track_ids == ["s1", "f1", "f2"] and editions.repeat_recordings == 1
    recordings = build_plan([A], {"a1": [single, full]}, Config(dedupe="recordings"))
    assert recordings.blocks[0].track_ids == ["f1", "f2"]


def test_recordings_mode_keeps_the_original_on_its_first_release():
    hit = track("s1", "Acrobatic", isrc="AC")
    single = album("s", title="Acrobatic", kind="SINGLE", date="2021-04-30", tracks=[hit])
    remix1 = album("r1", title="Acrobatic (Biscits Remix)", kind="SINGLE", date="2021-09-08",
                   tracks=[track("r1a", "Acrobatic", isrc="AC"), track("r1b", "Acrobatic", isrc="B1", version="Biscits Remix")])
    pack_ = album("ep", title="Acrobatic (Remixes)", kind="EP", date="2021-10-01",
                  tracks=[track("e1", "Acrobatic", isrc="AC"), track("e2", "Acrobatic", isrc="B1", version="Biscits Remix"),
                          track("e3", "Acrobatic", isrc="W1", version="Worra Remix")])
    plan = build_plan([A], {"a1": [pack_, remix1, single]}, Config(dedupe="recordings"))
    assert plan.blocks[0].track_ids == ["s1", "r1b", "e3"]
    assert [(r["album_id"], r["tracks"], r["of"]) for r in plan.blocks[0].releases] == [
        ("s", 1, 1), ("r1", 1, 2), ("ep", 1, 3)]


def test_version_already_in_the_title_is_one_release():
    a = album("a", title="Temptation (Lee Foss Remix)", version="Lee Foss Remix", kind="SINGLE",
              tracks=[track("a1", "Temptation", version="Lee Foss Remix")])
    b = album("b", title="Temptation (Lee Foss Remix)", kind="SINGLE",
              tracks=[track("b1", "Temptation (Lee Foss Remix)")])
    plan = build_plan([A], {"a1": [a, b]}, Config())
    assert len(plan.blocks[0].releases) == 1
    assert plan.blocks[0].releases[0]["title"] == "Temptation (Lee Foss Remix)"
    assert plan.excluded[0]["reason"].startswith("duplicate edition of")


def test_same_recordings_under_another_name_is_a_copy():
    a = album("a", title="Temptation (Lee Foss Remix)", version="Lee Foss Remix (Extended)", kind="SINGLE",
              tracks=[track("a1", "Temptation", isrc="T1", version="Lee Foss Remix")])
    b = album("b", title="Temptation (Lee Foss Remix)", kind="SINGLE",
              tracks=[track("b1", "Temptation - Lee Foss Remix (Radio)", isrc="T1")])
    extended = album("c", title="Temptation (Lee Foss Remix)", kind="SINGLE",
                     tracks=[track("c1", "Temptation", isrc="T2", version="Lee Foss Extended Remix")])
    remix_single = album("d", title="Acrobatic (Biscits Remix)", kind="SINGLE",
                         tracks=[track("d1", "Acrobatic", isrc="A0"), track("d2", "Acrobatic", isrc="A1")])
    original = album("e", title="Acrobatic", kind="SINGLE", tracks=[track("e1", "Acrobatic", isrc="A0")])
    plan = build_plan([A], {"a1": [a, b, extended, remix_single, original]}, Config())
    kept = sorted(r["album_id"] for r in plan.blocks[0].releases)
    assert len(kept) == 4 and "c" in kept and "d" in kept and "e" in kept
    assert [e["reason"] for e in plan.excluded] == [f"duplicate edition of {({'a', 'b'} & set(kept)).pop()}"]
    # every official release stays whole: the original is on its remix single too
    assert plan.blocks[0].track_ids.count("d1") == 1 and "e1" in plan.blocks[0].track_ids


def test_shared_release_is_listed_once():
    shared = album("s", artists=("a1", "b1"), tracks=[track("s1", artists=("a1", "b1"))])
    plan = build_plan([A, B], {"a1": [shared], "b1": [shared]}, Config())
    assert plan.blocks[0].track_ids == ["s1"] and plan.blocks[1].track_ids == []


def blocks(*sizes):
    return [Block(f"id{n}", f"Artist {n:02d}", [f"{n}-{i}" for i in range(size)]) for n, size in enumerate(sizes)]


def others(n, title="Guest {} (ASOT 1000)"):
    return [track(f"o{i}", title.format(i), artists=("x",)) for i in range(n)]


def test_a_state_of_trance_and_similar_collections_are_left_out():
    # credits known: a show full of other people's tracks is a compilation whatever it is called
    episode = album("1", title="A State of Trance Episode 1000", tracks=[track("1")] + others(9))
    assert why(episode).startswith("compilation (1 of 10")
    # credits missing: the name alone is enough
    blind = [dict(t, artist_ids=None) for t in others(10)]
    assert why(album("1", title="A State of Trance 2024", tracks=blind)) == "collection (a state of trance)"
    assert why(album("1", title="Corsten\u2019s Countdown 800", tracks=blind)) == "collection (corstens countdown)"
    assert why(album("1", title="Group Therapy 500", tracks=blind)) == "collection (group therapy)"
    assert why(album("1", title="Trance Top 20 (Selected by Alpha)", tracks=blind)) == "collection (selected by)"
    assert why(album("1", title="Year Mix 2025", tracks=blind)) == "collection (year mix)"
    # host credited on a bit over half the tracks: still a collection by name
    half = [track(str(i)) for i in range(6)] + others(4)
    assert why(album("1", title="ABGT 550", tracks=half)) == "collection (abgt)"
    assert why(album("1", title="Night Songs", tracks=half)) is None
    # your own additions
    cfg = Config(extra_collection_names=["Sunset Sessions"])
    assert why(album("1", title="Sunset Sessions Vol. 4", tracks=blind), cfg) == "collection (sunset sessions)"


def test_the_artists_own_work_is_not_mistaken_for_a_collection():
    own = [track(str(i)) for i in range(5)]
    # an anthem bundle named after the show, every track the artist's
    assert why(album("1", title="Turn The World (ASOT 1000 Anthem)", tracks=own)) is None
    # a one-track single is never judged by its name
    assert why(album("1", kind="SINGLE", title="A State of Trance", tracks=[dict(track("1"), artist_ids=None)])) is None
    # words inside other words do not count
    assert why(album("1", title="Alive at Night", tracks=[dict(t, artist_ids=None) for t in own])) is None
    # a remix collection: credited to the original artists, but named as the artist's remixes
    remixes = [track(str(i), f"Their Song {i} (Alpha Remix)", artists=("x",)) for i in range(6)]
    assert why(album("1", title="The Remixes", tracks=remixes)) is None
    assert why(album("1", title="A State of Trance 2024", tracks=others(10)),
               Config(exclude_collections=False)).startswith("compilation")


def test_a_title_repeated_many_times_is_a_series():
    assert base_pattern("Night Drive Radio 042 [Part 2]") == "night drive radio"
    shows = [album(f"s{i}", title=f"Night Drive Radio {i:03d}",
                   tracks=[dict(track(f"s{i}-{n}", artists=("x",)), artist_ids=None) for n in range(8)])
             for i in range(15)]
    real = album("r", title="Real Album", tracks=[track("r1")])
    plan = build_plan([A], {"a1": shows + [real]}, Config())
    assert [r["album_id"] for r in plan.blocks[0].releases] == ["r"]
    assert {e["reason"] for e in plan.excluded} == {"series (many releases share this title)"}
    # fourteen is not enough, and those are reported as kept without a credit check
    plan = build_plan([A], {"a1": shows[:14] + [real]}, Config())
    assert len(plan.blocks[0].releases) == 15 and len(plan.unchecked) == 14


def test_pack_first_run_fills_parts_in_order():
    tracks, labels, mapping = pack(blocks(40, 40, 40, 40), {}, target=100, cap=110)
    assert [len(tracks[k]) for k in sorted(tracks)] == [80, 80]
    assert labels[1] == ("Artist 00", "Artist 01")
    assert mapping == {"id0": 1, "id1": 1, "id2": 2, "id3": 2}


def test_pack_keeps_artists_where_they_were():
    prev = {"id0": 1, "id1": 1, "id2": 2, "id3": 2}
    # artist 0 grew a little, a new artist arrived: nobody moves, the newcomer fills a gap
    grown = blocks(55, 40, 40, 40, 10)
    tracks, _, mapping = pack(grown, prev, target=100, cap=110)
    assert {k: v for k, v in mapping.items() if k in prev} == prev
    assert mapping["id4"] == 2 and len(tracks[1]) == 95


def test_pack_puts_a_new_artist_near_its_alphabetical_neighbours():
    prev = {"id0": 1, "id1": 2, "id3": 3}
    # id2 is new and sorts between id1 (part 2) and id3 (part 3): part 2 has room, part 1 is not tried first
    tracks, _, mapping = pack(blocks(10, 10, 5, 10), prev, target=100, cap=110)
    assert mapping["id2"] == 2
    # no room next door: the nearest part with room wins
    tracks, _, mapping = pack(blocks(10, 98, 5, 99), prev, target=100, cap=110)
    assert mapping["id2"] == 1


def test_pack_spills_when_a_part_would_pass_the_cap():
    prev = {"id0": 1, "id1": 1}
    tracks, _, mapping = pack(blocks(80, 40), prev, target=100, cap=110)
    assert mapping == {"id0": 1, "id1": 2} and len(tracks[1]) == 80


def test_pack_splits_one_huge_artist_and_keeps_emptied_parts():
    tracks, _, mapping = pack(blocks(250), {}, target=100, cap=110)
    assert [len(tracks[k]) for k in sorted(tracks)] == [100, 100, 50]
    assert mapping == {"id0": 1, "id0#2": 2, "id0#3": 3}
    tracks, _, _ = pack(blocks(10), {"id0": 1, "gone": 3}, target=100, cap=110)
    assert sorted(tracks) == [1, 2, 3] and tracks[3] == []


def item(track_id, n):
    return {"track": track_id, "item": f"i{n}", "type": "tracks"}


def test_changes_only_touch_managed_tracks():
    current = [item("a", 1), item("hand", 2), item("old", 3), item("a", 4)]
    remove, keep = plan_changes(current, ["a", "b"], managed={"a", "old"}, prune_unmanaged=False)
    assert [i["item"] for i in remove] == ["i3", "i4"]
    assert [i["track"] for i in keep] == ["a", "hand"]
    remove, _ = plan_changes(current, ["a", "b"], managed=set(), prune_unmanaged=True)
    assert [i["item"] for i in remove] == ["i2", "i3", "i4"]


def test_default_parts_hold_two_thousand():
    cfg = Config()
    assert (cfg.part_target, cfg.part_cap) == (1850, 2000)
    tracks, _, _ = pack(blocks(900, 900, 900, 2500), {}, cfg.part_target, cfg.part_cap)
    assert [len(tracks[k]) for k in sorted(tracks)] == [1800, 900, 1850, 650]
    assert max(len(v) for v in tracks.values()) <= 2000


def test_runs_insert_before_the_next_present_track():
    keep = [item("b", 1), item("d", 2)]
    runs = plan_runs(keep, ["a", "b", "c", "skipme", "d", "e", "f"], skip={"skipme"})
    assert runs == [("i1", ["a"]), ("i2", ["c"]), (None, ["e", "f"])]
