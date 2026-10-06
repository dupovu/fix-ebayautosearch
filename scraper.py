"""eBay auto searcher.

Loads an eBay search page with a human-like browser session, stores every new
listing id in SQLite and (optionally) pushes it to Telegram.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import random
import re
import sqlite3
import sys

# Keep prints visible immediately even when the output is piped to a file or a
# log collector (e.g. `nohup python3 scraper.py > live.log`).
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:  # pragma: no cover
    pass
import time
import urllib.parse
from signal import SIGINT, signal
from typing import Any, Dict, List, Optional, Tuple

import requests

import human_session
from human_session import BotDetected
from lxml import html as lxml_html

import config_gui
from settings import DEFAULT_CONFIG

MAX_RETRIES = 5
RESTART_TIME = 10

# After a block eBay raises a cooldown; the next attempt gets a fresh session and
# these seconds of quiet time (multiplied by the number of consecutive blocks).
COOLDOWN_MIN = 180
COOLDOWN_MAX = 600

con: Optional[sqlite3.Connection] = None


class TooManyConnectionRetries(Exception):
    pass


def load_config(filename_path: str) -> Dict[str, Any]:
    """Read the config file, filling in defaults for missing keys."""
    with open(filename_path, encoding="utf-8") as config_file:
        raw = json.load(config_file)
    config = dict(DEFAULT_CONFIG)
    for key, value in raw.items():
        config[key] = value

    for key in ("sleep", "minDelay", "maxDelay", "jitter", "checkInterval"):
        config[key] = _to_number(config[key], DEFAULT_CONFIG[key])
    for key in ("pages", "maxItemsPerPage"):
        config[key] = max(1, int(_to_number(config[key], DEFAULT_CONFIG[key])))
    config["telegramAPIKEY"] = str(config.get("telegramAPIKEY") or "")
    config["telegramCHATID"] = str(config.get("telegramCHATID") or "")
    config["databaseFile"] = str(config.get("databaseFile") or "database.db")
    config["proxy"] = str(config.get("proxy") or "")
    config["userAgent"] = str(config.get("userAgent") or "")
    config["sessionMode"] = str(config.get("sessionMode") or "auto").lower()
    config["headless"] = _to_bool(config.get("headless"), True)
    config["blockResources"] = _to_bool(config.get("blockResources"), True)

    if not config["url"]:
        raise ValueError("No eBay search url configured in " + filename_path)
    if config["maxDelay"] < config["minDelay"]:
        config["maxDelay"] = config["minDelay"]
    return config


def _to_number(value: Any, fallback: float) -> float:
    try:
        if isinstance(value, str):
            value = value.strip()
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if number <= 0:
        return float(fallback)
    return number


def _to_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return fallback


def exit_handler(signal_received, frame):
    print("CTRL-C Pressed, exiting...")
    if con is not None:
        with contextlib.suppress(Exception):
            con.close()
    sys.exit(0)


def sql_connection(file_name):
    directory = os.path.dirname(os.path.abspath(file_name))
    with contextlib.suppress(Exception):
        os.makedirs(directory, exist_ok=True)
    try:
        return sqlite3.connect(file_name)
    except sqlite3.Error as error:
        print("Database error: %s" % error)
        raise


def send_telegram(msg: str, idchat: str, token: str) -> None:
    """Send one text message. The payload is form-encoded, not glued to the URL."""
    response = requests.post(
        "https://api.telegram.org/bot%s/sendMessage" % token,
        data={"chat_id": idchat, "text": msg, "disable_web_page_preview": "true"},
        timeout=20,
    )
    if response.status_code >= 400:
        print("Telegram error %s: %s" % (response.status_code, response.text[:200]))


# --------------------------------------------------------------------------- #
# Listing parsing
# --------------------------------------------------------------------------- #

_LISTING_ID = re.compile(r"/itm/(?:[^/?#]*?/)?(\d{9,15})")
_PRICE_XPATHS = (
    './/*[contains(@class,"s-card__price")]',
    './/*[contains(@class,"s-item__price")]',
    './/*[@itemprop="price"]/@content',
    './/*[contains(@class,"lvprice")]',
    './/*[contains(@class,"prc")]',
)
_TITLE_XPATHS = (
    './/*[contains(@class,"s-card__title")]',
    './/*[contains(@class,"s-item__title")]',
    './/h3',
    './/h2',
)
# Result containers used by the current (Marko) search UI, the classic
# "s-item" UI and the legacy ListView UI.
_RESULT_NODES = (
    '//li[contains(@class,"s-item") or contains(@class,"s-card") or contains(@class,"sresult")]'
)
_TITLE_BADGES = (
    "Anuncio nuevo", "Nuevo", "Oferta especial", "Recogido", "Envío gratis",
    "Sponsor", "Paid ad", "eBay Refurbished",
)
# Screen-reader only text that eBay appends to links opened in a new tab.
_TITLE_SUFFIXES = (
    "Se abre en una nueva ventana o pestaña",
    "Abre en una nueva ventana o pestaña",
    "Opens in a new window or tab",
)


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    # eBay prefixes card titles with status badges ("Anuncio nuevo", "Oferta
    # especial", "Recogido", ...).
    for badge in _TITLE_BADGES:
        if text.startswith(badge):
            text = text[len(badge):].strip()
    for suffix in _TITLE_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return text


def _first(node, xpaths: Tuple[str, ...]) -> str:
    for xpath in xpaths:
        with contextlib.suppress(Exception):
            found = node.xpath(xpath)
            if found:
                element = found[0]
                text = _clean(element if isinstance(element, str) else element.text_content())
                if text:
                    return text
    return ""


def parse_listings(page_html: str, url: str) -> List[Tuple[str, str, str]]:
    """Extract ``(listing_id, price, title)`` for every result on the page.

    eBay serves several result markups depending on the marketplace, the A/B
    bucket and whether the page was rendered by the browser, so every known
    layout is attempted and anything unparseable is skipped instead of raising.
    """
    if not page_html:
        return []
    try:
        tree = lxml_html.fromstring(page_html)
    except Exception as exc:
        print("Could not parse the eBay response: %s" % exc)
        return []

    results: Dict[str, Tuple[str, str]] = {}

    def add(listing_id: Optional[str], price: str, title: str) -> None:
        if not listing_id:
            return
        listing_id = listing_id.strip()
        # Real item ids are 9-15 digits; 16+ digit values are internal
        # "listingid"s used by eBay's placeholder/ad cards.
        if not listing_id.isdigit() or not 9 <= len(listing_id) <= 15:
            return
        if listing_id not in results or (not results[listing_id][0] and price):
            results[listing_id] = (_clean(price), _clean(title))

    for node in tree.xpath(_RESULT_NODES):
        # Skeleton cards are pre-rendered inside aria-hidden containers and point
        # at a placeholder url.
        if node.xpath('ancestor::div[contains(@class,"s-clipped")][@aria-hidden="true"]'):
            continue

        hrefs = node.xpath('.//a[contains(@class,"s-item__link")]/@href'
                           ' | .//a[contains(@class,"s-card__link")]/@href'
                           ' | .//a[contains(@class,"s-item__title")]/@href'
                           ' | .//h3/a/@href'
                           ' | .//a[contains(@class,"s-item__image")]/@href')
        listing_id = ""
        for href in hrefs:
            match = _LISTING_ID.search(href or "")
            if match:
                listing_id = match.group(1)
                break
        if not listing_id:
            candidate = node.get("listingid") or node.get("data-listingid") or ""
            if candidate.isdigit() and 9 <= len(candidate) <= 15:
                listing_id = candidate

        add(listing_id, _first(node, _PRICE_XPATHS), _first(node, _TITLE_XPATHS))

    # Fallback: any /itm/ link on the page (some layouts wrap results in
    # different containers).
    if not results:
        for href in tree.xpath('//a[contains(@href,"/itm/")]/@href'):
            match = _LISTING_ID.search(href or "")
            if match:
                add(match.group(1), "", "")

    if not results:
        print("No listings found in the response. If eBay keeps returning a challenge "
              "page, use sessionMode=browser with headless=false and a residential proxy.")
    return [(listing_id, price, title) for listing_id, (price, title) in results.items()]


def item_url(url: str, listing_id: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return "%s://%s/itm/%s" % (parsed.scheme or "https", parsed.hostname or "www.ebay.com", listing_id)


def build_page_url(url: str, page: int, limit: Optional[int]) -> str:
    """Add ``_pgn``/``_ipg`` parameters so more than one result page is read."""
    if page <= 1 and not limit:
        return url
    parts = urllib.parse.urlsplit(url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not key.startswith("_pgn") and not key.startswith("_ipg")
    ]
    if limit:
        query.append(("_ipg", str(limit)))
    if page > 1:
        query.append(("_pgn", str(page)))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #


def make_session(config: Dict[str, Any], debug: bool = False):
    """Build a session for one cycle according to the configured mode."""
    kwargs: Dict[str, Any] = {
        "proxy": config["proxy"] or None,
        "user_agent": config["userAgent"] or None,
        "min_delay": config["minDelay"],
        "max_delay": config["maxDelay"],
        "check_interval": config["checkInterval"],
        "debug": debug,
    }
    mode = config["sessionMode"]
    if mode != "requests":
        kwargs["headless"] = config["headless"]
        kwargs["block_resources"] = config["blockResources"]
    return human_session.build_session(config["url"], mode=mode, **kwargs)


def scrape_once(session, config: Dict[str, Any], verbose: bool = False) -> List[Tuple[str, str, str]]:
    """One full cycle: read every configured page and return the listings."""
    listings: List[Tuple[str, str, str]] = []
    for page in range(1, config["pages"] + 1):
        target = build_page_url(config["url"], page, config["maxItemsPerPage"])
        html = session.fetch(target)
        listings.extend(parse_listings(html, config["url"]))
        if verbose:
            print("page %d: %d listings" % (page, len(listings)))
    return listings


def _now() -> str:
    """Timestamp for the database (a plain string, no deprecated adapter)."""
    return datetime.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def publish(config: Dict[str, Any], cursor, listings: List[Tuple[str, str, str]]) -> int:
    """Store every listing id and report the new ones. Returns how many were new."""
    sent = 0
    for listing_id, price, title in listings:
        try:
            cursor.execute("INSERT INTO identifiers(id,listingDate) VALUES(?,?)",
                           (listing_id, _now()))
        except sqlite3.IntegrityError:
            # Already reported on an earlier cycle.
            continue
        link = item_url(config["url"], listing_id)
        print(link)
        if price:
            print(price)
        if title:
            print(title)
        sent += 1
        if config["telegramAPIKEY"] and config["telegramCHATID"]:
            message = "\n".join(part for part in (title, price, link) if part)
            try:
                send_telegram(message, config["telegramCHATID"], config["telegramAPIKEY"])
                # Telegram limits messages per second.
                time.sleep(random.uniform(0.5, 1.2))
            except Exception as exc:
                print("Telegram: %s" % exc)
    return sent


def wait_before_next_cycle(config: Dict[str, Any]) -> None:
    total = config["sleep"] + random.uniform(0, config["jitter"])
    print("Next search in %.0f seconds" % total)
    time.sleep(total)


def scraper(config: Dict[str, Any], debug: bool = False) -> None:
    """Infinite loop; a fresh human session is built for every cycle.

    Recreating the browser (and reusing the persistent profile) means eBay keeps
    seeing a returning visitor with a normal cookie jar instead of one process
    hammering the same endpoint.
    """
    global con
    cursordb = con.cursor()
    url = config["url"]
    consecutive_blocks = 0

    while True:
        for attempt in range(MAX_RETRIES):
            session = None
            try:
                session = make_session(config, debug=debug)
                listings = scrape_once(session, config, verbose=debug)
                consecutive_blocks = 0
                break
            except BotDetected as exc:
                consecutive_blocks += 1
                print("Blocked by eBay (%s)" % exc)
                cooldown = random.uniform(COOLDOWN_MIN, COOLDOWN_MAX) * min(consecutive_blocks, 4)
                print("Waiting %.0f seconds before trying again with a fresh session" % cooldown)
                with contextlib.suppress(KeyboardInterrupt):
                    time.sleep(cooldown)
                if attempt == MAX_RETRIES - 1:
                    raise
            except Exception as exc:
                retryable = isinstance(exc, (human_session._RendererGone, requests.RequestException,
                                             TimeoutError, OSError))
                print("Request failed (%s: %s), retry %d/%d in %ds"
                      % (type(exc).__name__, exc, attempt + 1, MAX_RETRIES, RESTART_TIME))
                if not retryable:
                    raise
                with contextlib.suppress(KeyboardInterrupt):
                    time.sleep(RESTART_TIME)
            finally:
                if session is not None:
                    with contextlib.suppress(Exception):
                        session.close()
        else:
            raise TooManyConnectionRetries("Could not load the search page after %d attempts" % MAX_RETRIES)

        new = publish(config, cursordb, listings)
        print("%d new listings" % new)
        con.commit()
        wait_before_next_cycle(config)


def startup(filename_path: str, debug: bool = False, once: bool = False) -> None:
    global con
    config = load_config(filename_path)
    print("Target: %s" % config["url"])

    con = sql_connection(config["databaseFile"])
    cursor = con.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS identifiers(id VARCHAR(20) PRIMARY KEY, listingDate timestamp)")
    con.commit()

    if config["telegramAPIKEY"] and config["telegramCHATID"]:
        # Imported lazily so the scraper still runs when aiogram is absent.
        try:
            import telegram_sender

            telegram_sender.start_bot_in_thread(config["telegramAPIKEY"], config["telegramCHATID"],
                                                config["databaseFile"])
        except ImportError as exc:
            print("Telegram /getfile command unavailable: %s (pip install aiogram)" % exc)

    signal(SIGINT, exit_handler)

    if once:
        last_error: Optional[Exception] = None
        for attempt in range(2):
            session = make_session(config, debug=debug)
            try:
                listings = scrape_once(session, config, verbose=True)
                new = publish(config, cursor, listings)
                con.commit()
                print("%d listings found, %d new" % (len(listings), new))
                return
            except BotDetected as exc:
                last_error = exc
                print("Blocked by eBay (%s), retrying once in 90s" % exc)
                with contextlib.suppress(KeyboardInterrupt):
                    time.sleep(90)
            finally:
                with contextlib.suppress(Exception):
                    session.close()
        raise last_error if last_error else RuntimeError("could not load the search page")

    scraper(config, debug=debug)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Search eBay for new listings and notify Telegram")
    parser.add_argument("-nogui", "--nogui", action="store_true",
                        help="skip the configuration GUI")
    parser.add_argument("-path", metavar="--path", type=str, default="config.json", required=False,
                        help="the path to the config file (defaults to config.json)")
    parser.add_argument("-once", "--once", action="store_true",
                        help="run a single search cycle and exit")
    parser.add_argument("-debug", "--debug", action="store_true", help="verbose output")

    options = parser.parse_args()
    filename = options.path

    if not options.nogui:
        if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            print("No display detected, using %s as is" % filename)
        else:
            try:
                config_gui.GUI(filename)
            except Exception as exc:
                print("Could not open the GUI (%s), using %s as is" % (exc, filename))

    while True:
        try:
            startup(filename, debug=options.debug, once=options.once)
            break
        except KeyboardInterrupt:
            exit_handler(None, None)
        except Exception as exc:
            print(exc)
            if options.once:
                sys.exit(1)
            print("Restarting the application in " + str(RESTART_TIME) + " seconds")
            time.sleep(RESTART_TIME)
            if con is not None:
                with contextlib.suppress(Exception):
                    con.close()