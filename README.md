# Observador Podcast Plus watch

Twice a day (06:23 and 18:23 UTC) a GitHub Action finds every Observador
"Podcast Plus" series and republishes all their episodes as **one RSS feed**
in a secret gist. Subscribe to that feed once in AntennaPod; new series show up
by themselves, starting with their trailer.

Feed: see the `GIST_ID` secret →
`https://gist.githubusercontent.com/Batestinha/<GIST_ID>/raw/observador-podcast-plus.xml`

## How a series counts as Podcast Plus

Programs come from Observador's Omny organisation
(`api.omny.fm/orgs/b04d3ae5-…/programs`, public, no key). A visible program is Plus when either:

- `site`: its slug is in the "Podcast Plus" section of <https://observador.pt/podcasts/>, or
- `omny_text`: its Omny description says "Podcast Plus" (e.g. "…é o novo Podcast Plus do
  Observador"). New series appear here weeks before the site section lists them.

Once known, a series stays in the feed for as long as it is on Omny, even if a signal breaks.

Observador pushes each new episode into the older Plus feeds as a promo copy
("Episódio 5 … | As Sombras de Duarte Lima", "Antestreia …"). Items whose title names a
different Plus series are dropped, so every episode appears once, under its own series.

## Files

- `watch.py`: the whole job (standard library only). `python3 watch.py --dry-run --out feed.xml`
  runs it locally without publishing.
- `state/series.json`: known series (`first_seen`, `seeded` for the 17 present on 2026-11-19,
  signals), the last published feed hash, and the count of consecutive runs without the site section.
- `state/items.json`: last good items per series, used when a feed fails.
- `tests/`: offline tests on fixtures captured 2026-11-19 (`python3 -m unittest discover -s tests`).

Detection shows up as a commit "New Podcast Plus series: …".

## Failures (GitHub emails you on a failed scheduled run)

- Omny unreachable or empty: the run fails.
- Site section missing for 3 runs in a row: the run fails (the parser needs updating). Until
  then it carries on with the Omny signal.
- A feed failing: its cached items are kept. All feeds failing: the run fails without publishing.
- Gist token rejected (expired): the run fails. Create a new fine-grained token with
  Gists read/write and run `gh secret set GIST_TOKEN -R Batestinha/observador-podcast-plus`.

## Adding or removing a series by hand

- Remove one: add its slug to `"excluded_slugs"` in `state/series.json` and commit.
- Add one the signals miss: add an entry under its Omny program id in `state/series.json`
  with the same fields as the others. It stays as long as the program is visible on Omny.

The combined feed mirrors the public feeds only. Subscribers' early access stays on observador.pt.
