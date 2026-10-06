"""Configuration defaults shared by the scraper and the GUI."""
from __future__ import annotations

from typing import Any, Dict

# Every key can be overridden in config.json; older config files keep working
# because missing keys fall back to these values.
DEFAULT_CONFIG: Dict[str, Any] = {
    "url": "",
    "telegramAPIKEY": "",
    "telegramCHATID": "",
    "databaseFile": "database.db",
    "sleep": 60,             # seconds between searches
    "sessionMode": "auto",    # auto | browser | requests
    "headless": True,         # False shows the browser window (looks more human)
    "blockResources": True,   # skip images/media/fonts
    "proxy": "",              # http://user:pass@host:port or socks5://host:port
    "userAgent": "",          # empty = use the real browser UA
    "minDelay": 1.5,          # "thinking" time before a request (seconds)
    "maxDelay": 4.0,          # "thinking" time after scrolling (seconds)
    "checkInterval": 5.0,     # how often the live html is inspected while waiting
    "jitter": 45,             # random extra seconds added to "sleep"
    "pages": 1,               # result pages per cycle
    "maxItemsPerPage": 60,    # results actually loaded per page
}

GUI_DEFAULTS: Dict[str, Any] = {
    "url": "https://www.ebay.es/sch/i.html?_nkw=ps5&_sacat=0&_sop=10",
    "databaseFile": "database.db",
    "sleep": "60",
    "jitter": "45",
    "minDelay": "1.5",
    "maxDelay": "4.0",
    "checkInterval": "5",
    "pages": "1",
    "maxItemsPerPage": "60",
    "sessionMode": "auto",
    "proxy": "",
    "userAgent": "",
}