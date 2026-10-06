"""Tkinter configuration window."""
from __future__ import annotations

import json
import os
import sys
import tkinter as tk
from typing import Any, Dict

from settings import DEFAULT_CONFIG, GUI_DEFAULTS

FIELDS = (
    ("url", "eBay search url"),
    ("sleep", "seconds between searches"),
    ("jitter", "extra random seconds"),
    ("minDelay", "browser min think time (s)"),
    ("maxDelay", "browser max think time (s)"),
    ("checkInterval", "html check interval (s)"),
    ("pages", "result pages per cycle"),
    ("maxItemsPerPage", "results loaded per page"),
    ("databaseFile", "database file"),
    ("telegramAPIKEY", "telegram API key"),
    ("telegramCHATID", "telegram chat id"),
    ("sessionMode", "session mode (auto/browser/requests)"),
    ("proxy", "proxy (http://host:port)"),
    ("userAgent", "user agent (blank = real browser)"),
)

DEFAULTS = dict(GUI_DEFAULTS)


def cancelclick():
    sys.exit(0)


class GUI:
    def okclick(self):
        data: Dict[str, Any] = {}
        for key, _label in FIELDS:
            value = self.entries[key].get()
            if key in ("sleep", "jitter", "minDelay", "maxDelay", "pages", "maxItemsPerPage"):
                try:
                    value = float(value) if "." in value else int(value)
                except ValueError:
                    pass
            data[key] = value
        data["headless"] = bool(self.headless_var.get())
        data["blockResources"] = bool(self.block_var.get())
        for key, value in DEFAULT_CONFIG.items():
            data.setdefault(key, value)

        with open(self.file_name, 'w', encoding='utf-8') as config_file:
            json.dump(data, config_file, indent=2, ensure_ascii=False)
        self.window.destroy()

    def __init__(self, file_name: str):
        self.file_name = file_name
        self.entries: Dict[str, tk.Entry] = {}

        config: Dict[str, Any] = dict(DEFAULT_CONFIG)
        if os.path.isfile(file_name):
            try:
                with open(file_name, encoding='utf-8') as config_file:
                    config.update(json.load(config_file))
            except Exception as exc:
                print("Could not read %s (%s), starting from the defaults" % (file_name, exc))

        self.window = tk.Tk()
        self.window.title("eBay auto search")
        self.window.resizable(True, True)

        frame = tk.Frame(self.window, padx=10, pady=10)
        frame.grid(column=0, row=0, sticky="nsew")
        self.window.columnconfigure(0, weight=1)

        row = 0
        for key, label in FIELDS:
            tk.Label(frame, text=label, anchor="w").grid(column=0, row=row, sticky="w")
            entry = tk.Entry(frame, width=70)
            value = config.get(key, DEFAULTS.get(key, ""))
            entry.insert(0, "" if value is None else str(value))
            entry.grid(column=1, row=row, sticky="ew", pady=1)
            self.entries[key] = entry
            row += 1

        self.headless_var = tk.BooleanVar(value=bool(config.get("headless", True)))
        tk.Checkbutton(frame, text="run the browser hidden (headless)",
                       variable=self.headless_var).grid(column=0, row=row, sticky="w")
        row += 1
        self.block_var = tk.BooleanVar(value=bool(config.get("blockResources", True)))
        tk.Checkbutton(frame, text="skip images/media/fonts (faster, slightly less human)",
                       variable=self.block_var).grid(column=0, row=row, sticky="w")
        row += 1

        tk.Label(frame, text="A residential proxy in 'proxy' plus headless=false is the most "
                             "reliable combination against eBay blocks.",
                 wraplength=560, justify="left", fg="#555").grid(column=0, row=row, columnspan=2,
                                                                  sticky="w", pady=(8, 0))
        row += 1

        buttons = tk.Frame(frame)
        buttons.grid(column=0, row=row, columnspan=2, pady=10)
        tk.Button(buttons, text="OK", command=self.okclick).pack(side="left", padx=5)
        tk.Button(buttons, text="Cancel", command=cancelclick).pack(side="left", padx=5)

        self.window.mainloop()