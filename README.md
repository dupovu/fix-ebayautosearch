# Basic eBay auto searcher

Scrapes an eBay search page, stores every listing it has already seen in a SQLite
database and sends the new ones to Telegram (console output works too).

The scraper is **not** a plain `requests` loop. eBay fronts its search with
Akamai/Radware bot management and answers automated HTTP clients with an
instant `403` ("Error Page | eBay"). So the pages are loaded by a real Chromium
through Playwright, with the automation fingerprints removed and a human-like
browsing rhythm (see [How the anti-bot part works](#how-the-anti-bot-part-works)).

## Requirements

```
pip install -r requirements.txt
playwright install chromium
```

`playwright install chromium` is mandatory: without a real browser binary the
program can only fall back to plain HTTP requests, which eBay blocks.

## Usage

```
python scraper.py            # GUI configuration, then start
python scraper.py -nogui     # read config.json and start immediately
python scraper.py -once      # a single search cycle (handy to test a config)
```

Flags:

| flag | meaning |
|---|---|
| `-path <file>` | config file location (default `config.json`) |
| `-nogui` | skip the configuration GUI |
| `-once` | run one cycle and exit |
| `-debug` | verbose output |

## Debugging

Run the scraper with `-debug` to see every step:

```bash
python3 scraper.py -nogui -path config.json -once -debug
```

* the live HTML is re-inspected every `checkInterval` seconds (5 s by default)
  while the session is waiting for a page, a browser check or results;
* every response is logged with its status, so `403 AkamaiGHost` is visible
  immediately instead of after a minute of scrolling a block page;
* the full HTML of every navigation is written to `ebay_debug/` - open
  `ebay_debug/*.html` in a browser to see exactly what eBay served.

Block pages, crashed renderers and browser checks are printed even without
`-debug`; everything else is silent, so normal runs stay quiet.

## Configuration

The GUI writes `config.json`; the same keys can be edited by hand. Old config
files keep working — every new key has a default.

| key | default | meaning |
|---|---|---|
| `url` | – | eBay search URL (copy it from the browser) |
| `sleep` | 60 | seconds between searches |
| `jitter` | 45 | extra random seconds added to `sleep`, so the rhythm is not machine-like |
| `minDelay` / `maxDelay` | 1.5 / 4.0 | random "thinking" time around each request |
| `checkInterval` | 5.0 | how often the live HTML is inspected while waiting |
| `pages` | 1 | result pages per cycle (`_pgn`) |
| `maxItemsPerPage` | 60 | results loaded per page (`_ipg`) |
| `databaseFile` | `database.db` | SQLite file of already reported listings |
| `telegramAPIKEY` / `telegramCHATID` | – | optional; with `aiogram` installed the bot also answers `/getfile` |
| `sessionMode` | `auto` | `auto` (browser, fallback to requests), `browser`, `requests` |
| `headless` | true | `false` opens the real browser window (looks even more human) |
| `blockResources` | true | skip images/media/fonts; faster, slightly less human |
| `proxy` | – | `http://user:pass@host:port` or `socks5://host:port` |
| `userAgent` | – | blank = use the real browser UA (recommended) |

## How the anti-bot part works

`human_session.py` opens a Chromium with a **persistent profile**
(`~/.ebayautosearch/browser-profile`), so eBay sees the same returning visitor
with a normal cookie jar (`dp1`, `bm_sv`, `s`, …) instead of a new client on
every request. Do not delete that directory: eBay refuses search pages from
profiles without those cookies.

On top of that:

- **Identity per marketplace** — `ebay.es` gets `es-ES` + `Europe/Madrid`,
  `ebay.de` gets `de-DE` + `Europe/Berlin`, and so on. A German browser clock on
  the Spanish site is an instant anomaly.
- **Fingerprint patching** — `navigator.webdriver`, `window.chrome`,
  `navigator.plugins`/`mimeTypes`, `permissions.query`, WebGL vendor strings,
  screen metrics and the `userAgentData` brands are all replaced with the values
  a real Chrome reports. `Sec-CH-UA` is rewritten to include the
  `Google Chrome` brand, because the bundled Chromium only advertises
  `Chromium` while the UA string says `Chrome/…` — a contradiction bot scoring
  looks for. Chromium is started with `--disable-blink-features=AutomationControlled`
  and without `--enable-automation`.
- **Header hygiene** — the `User-Agent` keeps the *real* browser version (so it
  never disagrees with `Sec-CH-UA-Full-Version`), navigations carry a `Referer`
  like a click from the homepage, and images/media/fonts are skipped only if you
  want speed over realism.
- **Human rhythm** — before every request the session lands on the site entry
  page, accepts the consent banner if it appears, moves the mouse along a curve,
  scrolls in uneven steps, hovers a few results and waits a random amount of
  time. Search pages are never hit twice in a row without this prelude.
- **Backoff** — an `HTTP 403`/challenge page raises `BotDetected`, and the
  scraper then waits 3–10 minutes (growing with consecutive blocks) before
  building a new session, instead of hammering the endpoint.

## About blocks

eBay blocks datacenter IP ranges (AWS/Azure/GCloud and most VPS providers) at
the edge, no matter how human the browser looks. The simulation is what gets you
*through* the bot check; a **residential proxy in the `proxy` field** is what
keeps you out of the block. For the lowest block rate use `headless: false`
together with a residential proxy and a `sleep` of several minutes.

## Issues

If a cycle prints "No listings found in the response" the page layout changed or
eBay served a challenge page. Run with `-debug` and check the reported status
code; `-once` is the fastest way to test a new configuration.

