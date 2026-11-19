"""Offline tests on fixtures captured from the live sources on 2026-11-19."""
import copy
import json
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watch  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
PROGRAMS = json.loads((FIX / "omny_programs.json").read_text())["Programs"]
SITE = (FIX / "podcasts_section.html").read_text()
FEEDS = {
    "os-escndalos-de-uma-revolucionria": (FIX / "feed_escandalos.xml").read_bytes(),
    "as-sombras-de-duarte-lima": (FIX / "feed_sombras.xml").read_bytes(),
}
SLUG_BY_FEED = {p["DefaultPlaylist"]["RssFeedUrl"]: p["Slug"]
                for p in PROGRAMS if p.get("DefaultPlaylist")}
EMPTY_FEED = b'<rss version="2.0"><channel><title>x</title></channel></rss>'


def fake_fetch(url, cache):
    slug = SLUG_BY_FEED.get(url)
    info, items, _ = watch.parse_feed(FEEDS.get(slug, EMPTY_FEED))
    return {"etag": "", "last_modified": "", "image": info["image"], "items": items}


def no_playlist(_program_id):
    return None


def run(state, programs=PROGRAMS, site=SITE, today="2026-11-19", fetch=fake_fetch):
    cache = {}
    slugs = watch.parse_site_plus_slugs(site)
    return watch.run(state, cache, programs, slugs, today, fetch=fetch,
                     playlist_lookup=no_playlist), cache


class SiteSection(unittest.TestCase):
    def test_reads_only_the_plus_section(self):
        slugs = watch.parse_site_plus_slugs(SITE)
        self.assertEqual(len(slugs), 16)
        self.assertNotIn("em-40-minutos", slugs)   # "Em destaque" section before it
        self.assertNotIn("caf-espanha", slugs)     # section after it
        self.assertIn("os-escndalos-de-uma-revolucionria", slugs)

    def test_missing_section(self):
        self.assertIsNone(watch.parse_site_plus_slugs("<h3 class='title'>Podcasts</h3>"))


class Classify(unittest.TestCase):
    def test_both_signals_give_all_17(self):
        found = watch.classify(PROGRAMS, watch.parse_site_plus_slugs(SITE), no_playlist)
        by_slug = {s["slug"]: s for s in found}
        self.assertEqual(len(found), 17)
        self.assertEqual(by_slug["as-sombras-de-duarte-lima"]["signals"], ["omny_text"])
        self.assertEqual(by_slug["matar-o-papa"]["signals"], ["site"])
        self.assertNotIn("em-40-minutos", by_slug)
        self.assertTrue(all(s["feed"].endswith("/podcast.rss") for s in found))

    def test_hidden_programs_are_ignored(self):
        programs = copy.deepcopy(PROGRAMS)
        for p in programs:
            if p["Slug"] == "as-sombras-de-duarte-lima":
                p["Hidden"] = True
        found = watch.classify(programs, watch.parse_site_plus_slugs(SITE), no_playlist)
        self.assertNotIn("as-sombras-de-duarte-lima", {s["slug"] for s in found})


class Merge(unittest.TestCase):
    def test_cross_posts_dropped(self):
        report, _ = run({})
        titles = [it["title"] for it in report["items"]]
        esc = [it for it in report["items"] if it["series"].startswith("Os Escândalos")]
        # 12 in the feed: 4 Sombras promo copies and a soundtrack bonus go, the trailer stays
        self.assertEqual(len(esc), 7)
        self.assertFalse(any("Duarte Lima" in it["title"] for it in esc))
        self.assertEqual([it["title"] for it in esc if not it["title"].startswith("Episódio")],
                         ['Trailer "Os Escândalos de uma Revolucionária". Estreia a 15 de setembro'])
        self.assertEqual(sum(1 for it in report["items"] if it["series"] == "As Sombras de Duarte Lima"), 5)
        self.assertEqual(len(titles), 12)

    def test_keep_extras_switch(self):
        report, _ = run({"keep_extras": True})
        self.assertEqual(len(report["items"]), 13)
        self.assertTrue(any(it["episodeType"] == "bonus" for it in report["items"]))

    def test_rss_is_valid_and_ordered(self):
        report, _ = run({})
        root = ET.fromstring(report["xml"])
        items = root.findall("channel/item")
        self.assertEqual(len(items), 12)
        self.assertEqual(items[0].findtext("title"),
                         "As Sombras de Duarte Lima — Episódio 5. Um informador chamado Apache")
        stamps = [watch.pub_ts(i.findtext("pubDate")) for i in items]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertTrue(all(i.find(watch.ITUNES + "image") is not None for i in items))
        self.assertEqual(root.findtext(f"channel/{watch.ITUNES}block"), "yes")
        self.assertNotIn("omnystudio.com/listener", report["xml"])

    def test_output_is_deterministic(self):
        a, _ = run({})
        b, _ = run({})
        self.assertEqual(a["sha256"], b["sha256"])


class Detection(unittest.TestCase):
    def test_first_run_seeds_without_reporting_new(self):
        state = {}
        report, _ = run(state)
        self.assertTrue(report["first_run"])
        self.assertEqual(report["new"], [])
        self.assertEqual(len(state["series"]), 17)
        self.assertTrue(all(s["seeded"] for s in state["series"].values()))

    def test_new_series_detected_and_merged(self):
        state = {}
        run(state)
        programs = copy.deepcopy(PROGRAMS)
        fake = copy.deepcopy(next(p for p in programs if p["Slug"] == "as-sombras-de-duarte-lima"))
        fake.update({"Id": "00000000-0000-0000-0000-000000000001", "Slug": "o-caso-novo",
                     "Name": "O Caso Novo",
                     "Description": '"O Caso Novo" é o novo Podcast Plus do Observador.'})
        fake["DefaultPlaylist"] = {**fake["DefaultPlaylist"], "Description": "",
                                   "RssFeedUrl": "https://example.invalid/new/podcast.rss"}
        programs.append(fake)
        new_feed = (b'<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
                    b'<channel><title>O Caso Novo</title>'
                    b'<item><title>Epis\xc3\xb3dio 1. O come\xc3\xa7o</title>'
                    b'<guid>new-1</guid><pubDate>Thu, 03 Dec 2026 10:00:00 +0000</pubDate>'
                    b'<itunes:duration>2400</itunes:duration>'
                    b'<enclosure url="https://example.invalid/new/1.mp3" length="1" type="audio/mpeg"/>'
                    b'</item><item><title>Trailer "O Caso Novo"</title><guid>new-0</guid>'
                    b'<pubDate>Thu, 26 Nov 2026 10:00:00 +0000</pubDate>'
                    b'<itunes:episodeType>trailer</itunes:episodeType>'
                    b'<enclosure url="https://example.invalid/new/0.mp3" length="1" type="audio/mpeg"/>'
                    b'</item></channel></rss>')

        def fetch(url, cache):
            if url.startswith("https://example.invalid/"):
                info, items, _ = watch.parse_feed(new_feed)
                return {"etag": "", "last_modified": "", "image": "", "items": items}
            return fake_fetch(url, cache)

        report, _ = run(state, programs=programs, today="2026-11-26", fetch=fetch)
        self.assertEqual(report["new"], ["O Caso Novo"])
        entry = state["series"][fake["Id"]]
        self.assertEqual((entry["first_seen"], entry["seeded"]), ("2026-11-26", False))
        self.assertEqual(report["items"][0]["title"], "Episódio 1. O começo")
        self.assertEqual(report["items"][0]["series"], "O Caso Novo")
        self.assertIn("new-0", {it["guid"] for it in report["items"]})  # its trailer

    def test_site_section_missing_keeps_known_series(self):
        state = {}
        run(state)
        report, _ = run(state, site="<html>redesigned</html>")
        self.assertEqual(state["site_misses"], 1)
        self.assertEqual(len(report["series"]), 17)  # old site-only series stay
        run(state, site="<html>redesigned</html>")
        self.assertEqual(state["site_misses"], 2)
        run(state)
        self.assertEqual(state["site_misses"], 0)

    def test_excluded_slug_leaves_the_feed(self):
        state = {}
        run(state)
        state["excluded_slugs"] = ["as-sombras-de-duarte-lima"]
        report, _ = run(state)
        self.assertEqual(len(report["series"]), 16)
        self.assertFalse(any(it["series"] == "As Sombras de Duarte Lima" for it in report["items"]))

    def test_failed_feed_keeps_cached_items(self):
        state = {}
        report, cache = run(state)

        def failing(url, previous):
            if SLUG_BY_FEED.get(url) == "as-sombras-de-duarte-lima":
                raise watch.FetchError("boom")
            return fake_fetch(url, previous)

        report2 = watch.run(state, cache, PROGRAMS, watch.parse_site_plus_slugs(SITE),
                            "2026-11-20", fetch=failing, playlist_lookup=no_playlist)
        self.assertEqual(len(report2["errors"]), 1)
        self.assertEqual(report2["sha256"], report["sha256"])


class Extras(unittest.TestCase):
    # Real titles from the feeds on 2026-11-19 (episodeType, seconds).
    CASES = [
        ("trailer", 240, 'Trailer "Matar o Papa". Estreia a 13 de maio', False),
        ("trailer", 60, "O regresso de “A Caça ao Estripador de Lisboa”", False),
        ("full", 200, "Trailer: “A Caça ao Estripador de Lisboa”. Estreia a 17 de dezembro", False),
        ("bonus", 180, 'A música de Frankie Chavez para "Os Escândalos de uma Revolucionária"', True),
        ("full", 120, 'Extra. A música dos Linda Martini para "1983: Portugal à Queima-Roupa"', True),
        ("full", 120, 'A música dos HMB para "O Misterioso Engenheiro Jardim"', True),
        ("full", 360, 'O piano de Júlio Resende em "Os Ficheiros do Caso Carlos Castro"', True),
        ("full", 2820, "Episódio especial: Porque é que o Estripador de Lisboa nunca foi apanhado?", False),
        ("full", 2040, 'Estreia. "A Caça ao Estripador de Lisboa". Episódio 1: Foi a última vez que a viu', False),
        ("full", 2295, "Episódio 5. Um informador chamado Apache", False),
        ("", 0, "Um título sem duração", False),
    ]

    def test_real_titles(self):
        for etype, secs, title, extra in self.CASES:
            with self.subTest(title=title):
                item = {"episodeType": etype, "duration": str(secs), "title": title}
                self.assertEqual(watch.is_extra(item), extra)

    def test_parse_duration(self):
        self.assertEqual(watch.parse_duration("2295"), 2295)
        self.assertEqual(watch.parse_duration("38:15"), 2295)
        self.assertEqual(watch.parse_duration("1:02:03"), 3723)
        self.assertEqual(watch.parse_duration(""), 0)
        self.assertEqual(watch.parse_duration("n/a"), 0)


class Text(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(watch.plain_text("<p>Ol&aacute;</p><p>See omnystudio.com/listener x</p>"), "Olá")
        self.assertTrue(watch.plain_text("a" * 5000).endswith("…"))

    def test_norm(self):
        self.assertEqual(watch.norm('“Os Escândalos de uma Revolucionária”'),
                         "os escandalos de uma revolucionaria")


if __name__ == "__main__":
    unittest.main()
