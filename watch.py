#!/usr/bin/env python3
"""Find Observador's "Podcast Plus" series and publish them as one RSS feed.

Signals (either one makes a series Plus):
  * site:      its slug is in the "Podcast Plus" section of observador.pt/podcasts/
  * omny_text: its Omny description says "Podcast Plus" (new series appear here
               weeks before the site section lists them)

The combined feed mirrors the series' public Omny feeds; audio stays on Omny.
Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

OMNY_ORG = "b04d3ae5-22c4-41b6-b20a-aa54000ba759"
OMNY_API = f"https://api.omny.fm/orgs/{OMNY_ORG}"
SITE_PODCASTS = "https://observador.pt/podcasts/"
GIST_FILE = "observador-podcast-plus.xml"
# observador.pt answers 403 to the default Python-urllib agent.
USER_AGENT = "observador-podcast-plus-watch/1.0 (personal feed aggregator)"
SITE_MISS_LIMIT = 3  # consecutive runs without the site section before failing
DESCRIPTION_LIMIT = 1200

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
}
ITUNES = "{%s}" % NS["itunes"]
ATOM = "{%s}" % NS["atom"]
PLUS_TEXT = re.compile(r"podcast\s+plus", re.I)

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"


class FetchError(RuntimeError):
    pass


# --------------------------------------------------------------------------- HTTP

def http_get(url: str, headers: dict | None = None, attempts: int = 3,
             timeout: int = 60) -> tuple[int, bytes, object]:
    """GET with retries. Returns (status, body, headers); 304 is a valid answer."""
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), resp.headers  # case-insensitive
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 304, b"", exc.headers
            last = exc
            if exc.code < 500 and exc.code != 429:
                break  # client errors will not fix themselves
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt + 1 < attempts:
            time.sleep(3 * (attempt + 1))
    raise FetchError(f"GET {url} failed: {last}")


# --------------------------------------------------------------------------- sources

def fetch_programs() -> list[dict]:
    """All Observador programs on Omny (the API answers in one page today)."""
    programs: list[dict] = []
    cursor = ""
    for _ in range(20):
        _, body, _ = http_get(f"{OMNY_API}/programs?cursor={cursor}")
        data = json.loads(body)
        programs += data.get("Programs") or []
        cursor = data.get("Cursor") or data.get("NextCursor") or ""
        if not cursor:
            break
    if not programs:
        raise FetchError("Omny returned no programs")
    return programs


def fetch_public_playlist(program_id: str) -> dict | None:
    """Fallback for programs without a DefaultPlaylist."""
    _, body, _ = http_get(f"{OMNY_API}/programs/{program_id}/playlists")
    for pl in json.loads(body).get("Playlists") or []:
        if pl.get("Visibility") == "Public" and pl.get("RssFeedUrl"):
            return pl
    return None


def parse_site_plus_slugs(page: str) -> list[str] | None:
    """Slugs in the "Podcast Plus" section, or None when the section is gone."""
    m = re.search(r"<h3[^>]*>\s*Podcast Plus\s*</h3>(.*?)(?=<h3[^>]*>|\Z)", page, re.S | re.I)
    if not m:
        return None
    slugs = re.findall(r'href="https?://observador\.pt/programas/([^/"?#]+)/?"', m.group(1))
    slugs = list(dict.fromkeys(s.lower() for s in slugs))
    return slugs or None


def fetch_site_plus_slugs() -> list[str] | None:
    try:
        _, body, _ = http_get(SITE_PODCASTS)
    except FetchError as exc:
        print(f"::warning::site fetch failed: {exc}")
        return None
    return parse_site_plus_slugs(body.decode("utf-8", "replace"))


# --------------------------------------------------------------------------- classify

def classify(programs: list[dict], site_slugs: list[str] | None,
             playlist_lookup=fetch_public_playlist) -> list[dict]:
    site = set(site_slugs or [])
    found = []
    for p in programs:
        if p.get("Hidden"):
            continue
        playlist = p.get("DefaultPlaylist") or {}
        signals = []
        if (p.get("Slug") or "").lower() in site:
            signals.append("site")
        if PLUS_TEXT.search(f"{p.get('Description') or ''} {playlist.get('Description') or ''}"):
            signals.append("omny_text")
        if not signals:
            continue
        if not playlist.get("RssFeedUrl") or playlist.get("Visibility", "Public") != "Public":
            playlist = playlist_lookup(p["Id"]) or {}
        if not playlist.get("RssFeedUrl"):
            print(f"::warning::Plus series {p.get('Name')!r} has no public feed yet")
            continue
        found.append({
            "id": p["Id"],
            "slug": p.get("Slug") or "",
            "name": (p.get("Name") or "").strip(),
            "feed": playlist["RssFeedUrl"],
            "artwork": large_artwork(playlist.get("ArtworkUrl") or p.get("ArtworkUrl") or ""),
            "signals": signals,
        })
    return found


def large_artwork(url: str) -> str:
    return re.sub(r"size=\w+", "size=Large", url) if url else url


# --------------------------------------------------------------------------- feeds

def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", html.unescape(text or "")).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def plain_text(fragment: str, limit: int = DESCRIPTION_LIMIT) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", fragment or "")
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [l for l in lines if l and "omnystudio.com/listener" not in l]
    text = "\n\n".join(lines)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def pub_ts(value: str) -> float:
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        return 0.0


def parse_feed(xml_bytes: bytes) -> tuple[dict, list[dict], str | None]:
    """Returns (channel info, items, next-page URL)."""
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("not an RSS feed")
    img = channel.find(f"{ITUNES}image")
    info = {"image": img.get("href") if img is not None else ""}
    nxt = None
    for link in channel.findall(f"{ATOM}link"):
        if link.get("rel") == "next":
            nxt = link.get("href")
    items = []
    for it in channel.findall("item"):
        enc = it.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        guid_el = it.find("guid")
        item_img = it.find(f"{ITUNES}image")
        body = it.findtext(f"{{{NS['content']}}}encoded") or it.findtext("description") or ""
        items.append({
            "title": (it.findtext("title") or "").strip(),
            "guid": (guid_el.text or "").strip() if guid_el is not None and guid_el.text else enc.get("url"),
            "pubDate": (it.findtext("pubDate") or "").strip(),
            "enclosure": {"url": enc.get("url"), "length": enc.get("length") or "0",
                          "type": enc.get("type") or "audio/mpeg"},
            "duration": (it.findtext(f"{ITUNES}duration") or "").strip(),
            "episodeType": (it.findtext(f"{ITUNES}episodeType") or "").strip(),
            "image": item_img.get("href") if item_img is not None else "",
            "link": (it.findtext("link") or "").strip(),
            "description": plain_text(body),
        })
    return info, items, nxt


def fetch_feed(url: str, cache: dict | None) -> dict:
    """Fresh feed entry {etag, last_modified, image, items}; reuses cache on 304."""
    cache = cache or {}
    headers = {}
    if cache.get("items"):
        if cache.get("etag"):
            headers["If-None-Match"] = cache["etag"]
        if cache.get("last_modified"):
            headers["If-Modified-Since"] = cache["last_modified"]
    status, body, resp_headers = http_get(url, headers)
    if status == 304:
        return cache
    info, items, nxt = parse_feed(body)
    for _ in range(10):  # Omny paginates long feeds
        if not nxt:
            break
        _, more, _ = http_get(nxt)
        _, extra, nxt = parse_feed(more)
        items += extra
    return {"etag": resp_headers.get("ETag") or "",
            "last_modified": resp_headers.get("Last-Modified") or "",
            "image": info["image"], "items": items}


# --------------------------------------------------------------------------- merge + render

def merge(series: list[dict], feeds: dict[str, dict]) -> list[dict]:
    """Combined items, newest first, without cross-posts of other Plus series."""
    names = {s["id"]: norm(s["name"]) for s in series}
    seen_guid, seen_audio, merged = set(), set(), []
    for s in series:
        entry = feeds.get(s["id"]) or {}
        others = [n for sid, n in names.items() if sid != s["id"] and n and n != names[s["id"]]]
        for it in entry.get("items") or []:
            title = norm(it["title"])
            if any(o in title for o in others):
                continue  # promo copy of another series' episode
            audio = it["enclosure"]["url"].split("?")[0]
            if it["guid"] in seen_guid or audio in seen_audio:
                continue
            seen_guid.add(it["guid"])
            seen_audio.add(audio)
            merged.append({**it, "series": s["name"],
                           "image": it.get("image") or entry.get("image") or s["artwork"]})
    merged.sort(key=lambda it: (pub_ts(it["pubDate"]), it["guid"]), reverse=True)
    return merged


def render_rss(items: list[dict], series: list[dict], feeds: dict[str, dict]) -> str:
    for prefix, uri in NS.items():
        ET.register_namespace(prefix, uri)
    rss = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(rss, "channel")

    def sub(parent, tag, text=None, **attrs):
        el = ET.SubElement(parent, tag, attrs)
        if text is not None:
            el.text = text
        return el

    newest = items[0] if items else None
    art = (newest or {}).get("image") or (series[0]["artwork"] if series else "")
    sub(ch, "title", "Observador Podcast Plus (todas as séries)")
    sub(ch, "link", SITE_PODCASTS)
    sub(ch, "description",
        "Todas as séries Podcast Plus do Observador num só feed. Agregador pessoal, "
        "não oficial: os episódios e o áudio vêm dos feeds públicos do Observador (Omny).")
    sub(ch, "language", "pt-PT")
    if newest:
        sub(ch, "lastBuildDate", newest["pubDate"])
    sub(ch, f"{ITUNES}author", "Observador")
    sub(ch, f"{ITUNES}block", "yes")
    sub(ch, f"{ITUNES}explicit", "false")
    ET.SubElement(ch, f"{ITUNES}category", {"text": "History"})
    if art:
        sub(ch, f"{ITUNES}image", href=art)
        img = sub(ch, "image")
        sub(img, "url", art)
        sub(img, "title", "Observador Podcast Plus (todas as séries)")
        sub(img, "link", SITE_PODCASTS)
    for it in items:
        el = sub(ch, "item")
        sub(el, "title", f"{it['series']} — {it['title']}")
        sub(el, "description", f"{it['series']}\n\n{it['description']}".strip())
        sub(el, "guid", it["guid"], isPermaLink="false")
        sub(el, "pubDate", it["pubDate"])
        if it.get("link"):
            sub(el, "link", it["link"])
        sub(el, "enclosure", url=it["enclosure"]["url"], length=str(it["enclosure"]["length"]),
            type=it["enclosure"]["type"])
        if it.get("duration"):
            sub(el, f"{ITUNES}duration", it["duration"])
        if it.get("episodeType"):
            sub(el, f"{ITUNES}episodeType", it["episodeType"])
        if it.get("image"):
            sub(el, f"{ITUNES}image", href=it["image"])
    ET.indent(rss, space=" ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n"


# --------------------------------------------------------------------------- state + publish

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def publish_gist(gist_id: str, token: str, content: str) -> None:
    body = json.dumps({"files": {GIST_FILE: {"content": content}}}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}", data=body, method="PATCH",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"gist update failed: HTTP {exc.code} {exc.read()[:300]!r}") from exc


def run(state: dict, cache: dict, programs: list[dict], site_slugs: list[str] | None,
        today: str, fetch=fetch_feed, playlist_lookup=fetch_public_playlist) -> dict:
    """Pure-ish core: updates state/cache in place, returns a report."""
    state.setdefault("series", {})
    first_run = not state["series"]
    state["site_misses"] = 0 if site_slugs else state.get("site_misses", 0) + 1

    excluded = set(state.setdefault("excluded_slugs", []))
    classified = [s for s in classify(programs, site_slugs, playlist_lookup)
                  if s["slug"] not in excluded]
    by_id = {s["id"]: s for s in classified}
    live_ids = {p["Id"] for p in programs if not p.get("Hidden")}
    new = []
    for s in classified:
        known = state["series"].get(s["id"])
        if known is None:
            state["series"][s["id"]] = {**s, "first_seen": today, "seeded": first_run}
            if not first_run:
                new.append(s["name"])
        else:
            known.update({k: s[k] for k in ("slug", "name", "feed", "artwork")})
            known["signals"] = sorted(set(known.get("signals", [])) | set(s["signals"]))
    # Keep known series that are still on Omny even if a signal broke today.
    series = [by_id.get(sid) or dict(known)
              for sid, known in state["series"].items()
              if sid in live_ids and known.get("slug") not in excluded]
    series.sort(key=lambda s: s["name"])

    for stale in set(cache) - {s["id"] for s in series}:
        del cache[stale]
    errors = []
    for s in series:
        try:
            cache[s["id"]] = fetch(s["feed"], cache.get(s["id"]))
        except (FetchError, ET.ParseError, ValueError) as exc:
            errors.append(f"{s['name']}: {exc}")
            print(f"::warning::feed failed, keeping cached items: {s['name']}: {exc}")
    items = merge(series, cache)
    xml = render_rss(items, series, cache)
    return {"series": series, "new": new, "first_run": first_run, "errors": errors,
            "items": items, "xml": xml, "sha256": hashlib.sha256(xml.encode()).hexdigest()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="do not publish the gist")
    ap.add_argument("--out", type=Path, help="also write the combined feed here")
    ap.add_argument("--state-dir", type=Path, default=STATE_DIR)
    args = ap.parse_args(argv)

    series_path, items_path = args.state_dir / "series.json", args.state_dir / "items.json"
    state = load_json(series_path, {"version": 1, "series": {}})
    cache = load_json(items_path, {})

    programs = fetch_programs()
    site_slugs = fetch_site_plus_slugs()
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    report = run(state, cache, programs, site_slugs, today)
    # Changes monthly, so the state commit keeps the public repo "active" and
    # GitHub never disables the schedule (60 days without activity).
    state["checked_month"] = today[:7]

    if args.out:
        args.out.write_text(report["xml"])
    published = False
    if report["series"] and len(report["errors"]) == len(report["series"]):
        print("::error::every series feed failed; not publishing")
        save_json(series_path, state)
        return 1
    if not args.dry_run and report["sha256"] != state.get("feed_sha256"):
        gist_id, token = os.environ.get("GIST_ID"), os.environ.get("GIST_TOKEN")
        if not gist_id or not token:
            print("::error::GIST_ID and GIST_TOKEN must be set (or use --dry-run)")
            return 1
        publish_gist(gist_id, token, report["xml"])
        state["feed_sha256"] = report["sha256"]
        published = True
    save_json(series_path, state)
    save_json(items_path, cache)

    lines = [f"Podcast Plus series: {len(report['series'])}",
             f"Items in combined feed: {len(report['items'])} ({len(report['xml']) // 1024} KB)",
             f"Site section: {'ok' if site_slugs else 'MISSING'} ({len(site_slugs or [])} slugs)",
             f"Feed changed and published: {published}",
             f"New series: {', '.join(report['new']) or 'none'}"]
    if report["first_run"]:
        lines.append("First run: current series seeded as known.")
    print("\n".join(lines))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write("\n".join(f"- {l}" for l in lines) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"new_series={'; '.join(report['new'])}\n")
    if state["site_misses"] >= SITE_MISS_LIMIT:
        print(f"::error::Podcast Plus section missing from {SITE_PODCASTS} for "
              f"{state['site_misses']} runs; the site parser needs a look")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
