"""Human-like browsing session used to fetch eBay pages without being flagged.

eBay fingerprints every request: TLS/JA3, HTTP header order, the ``navigator``
properties, the cookie jar and the request rhythm.  A bare ``requests.get(url)``
gets an instant HTTP 403 (see the ``Error Page | eBay`` response), so this
module drives a real Chromium through Playwright, patches the automation
footprints, keeps a persistent profile (returning "visitor") and behaves like a
person: warm-up navigation, consent handling, random mouse movement, scrolling
and randomised waits.

Usage::

    from human_session import HumanSession

    with HumanSession("https://www.ebay.es/sch/i.html?_nkw=ps5") as session:
        html = session.fetch()

A lighter :class:`RequestsSession` fallback (real headers + browser cookies) is
used only when Playwright or its browser binaries are unavailable.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import random
import re
import time
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional

import requests

__all__ = [
    "BotDetected",
    "HumanSession",
    "RequestsSession",
    "build_session",
    "normalise_ua",
    "profile_for_url",
]


class BotDetected(Exception):
    """Raised when eBay answers with a bot check / block page instead of HTML."""


class _RendererGone(Exception):
    """The browser tab died (renderer crash); a new one can be opened."""


# --------------------------------------------------------------------------- #
# Per-marketplace browser identity
# --------------------------------------------------------------------------- #

# A real visitor of ebay.es has a Spanish browser, a Spanish clock and a Spanish
# IP.  Mixing an American locale/timezone with a Spanish marketplace is one of
# the easiest things for eBay to spot.
_MARKETPLACES: Dict[str, Dict[str, str]] = {
    "ebay.com": {"locale": "en-US", "timezone": "America/New_York", "accept_language": "en-US,en;q=0.9"},
    "ebay.ca": {"locale": "en-CA", "timezone": "America/Toronto", "accept_language": "en-CA,en;q=0.9,fr-CA;q=0.8"},
    "ebay.co.uk": {"locale": "en-GB", "timezone": "Europe/London", "accept_language": "en-GB,en;q=0.9"},
    "ebay.ie": {"locale": "en-IE", "timezone": "Europe/Dublin", "accept_language": "en-IE,en;q=0.9"},
    "ebay.es": {"locale": "es-ES", "timezone": "Europe/Madrid", "accept_language": "es-ES,es;q=0.9,en;q=0.8"},
    "ebay.de": {"locale": "de-DE", "timezone": "Europe/Berlin", "accept_language": "de-DE,de;q=0.9,en;q=0.8"},
    "ebay.at": {"locale": "de-AT", "timezone": "Europe/Vienna", "accept_language": "de-AT,de;q=0.9,en;q=0.8"},
    "ebay.ch": {"locale": "de-CH", "timezone": "Europe/Zurich", "accept_language": "de-CH,de;q=0.9,en;q=0.8"},
    "ebay.fr": {"locale": "fr-FR", "timezone": "Europe/Paris", "accept_language": "fr-FR,fr;q=0.9,en;q=0.8"},
    "ebay.be": {"locale": "fr-BE", "timezone": "Europe/Brussels", "accept_language": "fr-BE,fr;q=0.9,en;q=0.8"},
    "ebay.nl": {"locale": "nl-NL", "timezone": "Europe/Amsterdam", "accept_language": "nl-NL,nl;q=0.9,en;q=0.8"},
    "ebay.be/nl": {"locale": "nl-BE", "timezone": "Europe/Brussels", "accept_language": "nl-BE,nl;q=0.9,fr;q=0.8"},
    "ebay.it": {"locale": "it-IT", "timezone": "Europe/Rome", "accept_language": "it-IT,it;q=0.9,en;q=0.8"},
    "ebay.pl": {"locale": "pl-PL", "timezone": "Europe/Warsaw", "accept_language": "pl-PL,pl;q=0.9,en;q=0.8"},
    "ebay.se": {"locale": "sv-SE", "timezone": "Europe/Stockholm", "accept_language": "sv-SE,sv;q=0.9,en;q=0.8"},
    "ebay.dk": {"locale": "da-DK", "timezone": "Europe/Copenhagen", "accept_language": "da-DK,da;q=0.9,en;q=0.8"},
    "ebay.no": {"locale": "nb-NO", "timezone": "Europe/Oslo", "accept_language": "nb-NO,nb;q=0.9,en;q=0.8"},
    "ebay.fi": {"locale": "fi-FI", "timezone": "Europe/Helsinki", "accept_language": "fi-FI,fi;q=0.9,en;q=0.8"},
    "ebay.com.au": {"locale": "en-AU", "timezone": "Australia/Sydney", "accept_language": "en-AU,en;q=0.9"},
    "ebay.co.nz": {"locale": "en-NZ", "timezone": "Pacific/Auckland", "accept_language": "en-NZ,en;q=0.9"},
    "ebay.com.sg": {"locale": "en-SG", "timezone": "Asia/Singapore", "accept_language": "en-SG,en;q=0.9"},
    "ebay.com.my": {"locale": "en-MY", "timezone": "Asia/Kuala_Lumpur", "accept_language": "en-MY,en;q=0.9"},
    "ebay.ph": {"locale": "en-PH", "timezone": "Asia/Manila", "accept_language": "en-PH,en;q=0.9"},
    "ebay.com.hk": {"locale": "zh-HK", "timezone": "Asia/Hong_Kong", "accept_language": "zh-HK,zh;q=0.9,en;q=0.8"},
    "ebay.co.jp": {"locale": "ja-JP", "timezone": "Asia/Tokyo", "accept_language": "ja-JP,ja;q=0.9,en;q=0.8"},
    "ebay.com.tw": {"locale": "zh-TW", "timezone": "Asia/Taipei", "accept_language": "zh-TW,zh;q=0.9,en;q=0.8"},
    "ebay.fr/be": {"locale": "fr-BE", "timezone": "Europe/Brussels", "accept_language": "fr-BE,fr;q=0.9,en;q=0.8"},
}

_DEFAULT_PROFILE = _MARKETPLACES["ebay.com"]

# Viewports seen on real desktops.  A single fixed 1920x1080 is unusual.
_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1600, "height": 900},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 2560, "height": 1440},
    {"width": 1680, "height": 1050},
    {"width": 1280, "height": 800},
]

# Sub-resources that are safe to skip: they cost bandwidth but carry no
# information about the page content.
_BLOCKABLE = ("image", "media", "font")

# Note: a normal eBay page ships eBay's own captcha SDK, so a bare "captcha"
# match cannot be used as a block signal.
_BOT_CHECK_MARKERS = (
    "access denied",
    "security measure",
    "unusual traffic",
    "you have been blocked",
    "your request has been blocked",
    "verify you are a human",
    "are you a robot",
    "we just need to make sure you're not a robot",
    "please enable javascript and cookies to continue",
    "challenges.cloudflare.com",
    "datadome",
    "error page | ebay",
    "something went wrong on our end",
)


def profile_for_url(url: str) -> Dict[str, str]:
    """Return the locale/timezone/language profile matching the eBay host."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host.endswith(".com"):
        host = host[: -len(".com")]
    for key in sorted(_MARKETPLACES, key=len, reverse=True):
        if host == key or host.endswith("." + key):
            return dict(_MARKETPLACES[key])
    if host.startswith("www."):
        host = host[4:]
    # Compare against the marketplace part of the hostname ("www.ebay.es" -> "ebay.es").
    parts = host.split(".")
    for i in range(len(parts) - 2):
        candidate = ".".join(parts[i:])
        if candidate in _MARKETPLACES:
            return dict(_MARKETPLACES[candidate])
    return dict(_DEFAULT_PROFILE)


def normalise_ua(user_agent: Optional[str]) -> Optional[str]:
    """Turn Playwright's ``HeadlessChrome`` UA into a plain desktop Chrome UA.

    Keeping the *real* browser version is important: Chromium also sends
    ``Sec-CH-UA-Platform``/``Sec-CH-UA`` client hints built from that version, so
    a hand written UA string that disagrees with them is an instant red flag.
    """
    if not user_agent:
        return None
    if "HeadlessChrome" in user_agent:
        user_agent = user_agent.replace("HeadlessChrome/", "Chrome/")
    return user_agent


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _is_device_check(url: str) -> bool:
    """eBay's device assurance / browser check endpoints."""
    url = (url or "").lower()
    return "devicebind.ebay.es" in url or "splashui/challenge" in url


def _looks_blocked(text: str, status: Optional[int] = None) -> Optional[str]:
    if status is not None and status >= 400:
        return "HTTP %s" % status
    lowered = text[:20000].lower()
    for marker in _BOT_CHECK_MARKERS:
        if marker in lowered:
            return marker
    if len(text) < 5000:
        return None
    return None


# --------------------------------------------------------------------------- #
# Fingerprint patches
# --------------------------------------------------------------------------- #

_STATE_JS = r"""
() => {
  const html = document.documentElement.outerHTML;
  const text = (document.body && document.body.innerText || '').replace(/\s+/g, ' ').trim();
  const doc = document;
  return {
    url: location.href,
    title: doc.title || '',
    text: text.slice(0, 200),
    total: html.length,
    cards: doc.querySelectorAll('li.s-card, li.s-item, li.sresult').length,
    scroll: Math.round((window.scrollY / Math.max(1, doc.body.scrollHeight - window.innerHeight)) * 100),
    challenge: /splashui\/challenge|devicebind|Sorry for the interruption|Disculpa la interrupci/i.test(location.href + ' ' + doc.title),
    blocked: /Error Page|Something went wrong on our end|SORRY/i.test((doc.title || '') + ' ' + text.slice(0, 400)),
    consent: !!doc.querySelector('#gdpr-banner, [data-testid*="consent" i], [class*="cookie-consent" i]'),
  };
}
"""

_FINGERPRINT_JS = r"""
async () => {
  const info = { ua: navigator.userAgent, brands: null, fullVersionList: null, platformVersion: null };
  const uad = navigator.userAgentData;
  if (uad) {
    try { info.brands = uad.brands; } catch (e) {}
    try {
      const high = await uad.getHighEntropyValues(['fullVersionList', 'platformVersion']);
      info.fullVersionList = high.fullVersionList;
      info.platformVersion = high.platformVersion;
    } catch (e) {}
  }
  return info;
}
"""


_STEALTH_JS = r"""
(() => {
  const ua = __UA__;
  const languages = __LANGUAGES__;
  const platform = __PLATFORM__;
  const screenInfo = __SCREEN__;
  const hardwareConcurrency = __CORES__;
  const deviceMemory = __MEMORY__;

  const define = (obj, prop, getter) => {
    try {
      Object.defineProperty(obj, prop, { get: getter, configurable: true });
    } catch (e) {}
  };

  // 1. navigator.webdriver is the single most famous automation flag.
  define(Navigator.prototype, 'webdriver', () => undefined);
  try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}
  define(navigator, 'webdriver', () => undefined);

  // 2. Headless UA in the JS world even if the header was rewritten.
  const patchNavigator = (nav) => {
    define(nav, 'userAgent', () => ua);
    define(nav, 'appVersion', () => ua.replace(/^Mozilla\//, ''));
    define(nav, 'platform', () => platform);
    define(nav, 'vendor', () => 'Google Inc.');
    define(nav, 'product', () => 'Gecko');
    define(nav, 'productSub', () => '20030107');
    define(nav, 'hardwareConcurrency', () => hardwareConcurrency);
    define(nav, 'deviceMemory', () => deviceMemory);
    define(nav, 'maxTouchPoints', () => 0);
    define(nav, 'languages', () => languages);
    define(nav, 'language', () => languages[0]);
    define(nav, 'doNotTrack', () => null);
    define(nav, 'webdriver', () => undefined);
    if (!nav.userAgentData) {
      Object.defineProperty(nav, 'userAgentData', {
        configurable: true,
        get: () => ({
          brands: __BRANDS__,
          mobile: false,
          platform: platform,
          getHighEntropyValues: (hints) => Promise.resolve({
            architecture: 'x86',
            bitness: '64',
            model: '',
            platform: platform,
            platformVersion: '10.0.0',
            uaFullVersion: __FULL_VERSION__,
            fullVersionList: __BRANDS__,
            wow64: false,
          }),
          toJSON: () => ({ brands: __BRANDS__, mobile: false, platform: platform }),
        }),
      });
    }
  };
  patchNavigator(navigator);

  // 3. Same properties on same-origin/about:blank iframes, which otherwise leak
  //    the unpatched values.
  const patchFrames = () => {
    try {
      const frames = document.querySelectorAll('iframe, frame');
      for (const frame of frames) {
        try {
          const win = frame.contentWindow;
          if (win && win.navigator && win.navigator !== navigator) {
            patchNavigator(Object.create(win.navigator));
          }
        } catch (e) {}
      }
    } catch (e) {}
  };
  patchFrames();
  document.addEventListener('DOMContentLoaded', patchFrames);

  // 4. window.chrome is present in every Chrome install.
  if (!window.chrome) {
    Object.defineProperty(window, 'chrome', {
      configurable: true,
      writable: true,
      value: {
        app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
        runtime: { OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' }, PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_64: 'x86_64' }, PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' }, RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' }, id: undefined, connect: () => {}, sendMessage: () => {} },
        csi: () => ({}), loadTimes: () => ({}),
      },
    });
  }

  // 5. Plugins / mimeTypes are non-empty in real Chrome (PDF viewer).
  if (navigator.plugins && navigator.plugins.length === 0) {
    const makePlugins = () => {
      const plugins = [
        { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      ];
      const items = plugins.map((p, i) => Object.assign(Object.create(Plugin.prototype), p, { length: plugins.length, item: (n) => plugins[n] || null, namedItem: (n) => plugins.find((x) => x.name === n) || null, [Symbol.iterator]: function* () { yield* plugins; } }));
      const list = Object.create(PluginArray.prototype);
      Object.defineProperty(list, 'length', { get: () => plugins.length });
      for (let i = 0; i < items.length; i++) Object.defineProperty(list, i, { get: () => items[i] });
      return list;
    };
    try {
      define(navigator, 'plugins', () => makePlugins());
      define(navigator, 'mimeTypes', () => {
        const types = [
          { type: 'application/pdf', suffixes: 'pdf', description: '' },
          { type: 'text/pdf', suffixes: 'pdf', description: '' },
        ];
        const items = types.map((t, i) => Object.assign(Object.create(MimeType.prototype), t, { length: types.length, item: (n) => types[n] || null, namedItem: (n) => types.find((x) => x.type === n) || null, [Symbol.iterator]: function* () { yield* types; } }));
        const list = Object.create(MimeTypeArray.prototype);
        Object.defineProperty(list, 'length', { get: () => types.length });
        for (let i = 0; i < items.length; i++) Object.defineProperty(list, i, { get: () => items[i] });
        return list;
      });
    } catch (e) {}
  }

  // 6. permissions.query must agree with Notification.permission.
  const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (originalQuery) {
    window.navigator.permissions.query = (params) => {
      const name = params && params.name;
      if (name === 'notifications') {
        return Promise.resolve({ state: Notification.permission, onchange: null, addEventListener() {}, removeEventListener() {} });
      }
      return originalQuery.call(window.navigator.permissions, params);
    };
  }

  // 7. Screen / device pixel ratio consistency.
  define(window.screen, 'width', () => screenInfo.width);
  define(window.screen, 'height', () => screenInfo.height);
  define(window.screen, 'availWidth', () => screenInfo.availWidth);
  define(window.screen, 'availHeight', () => screenInfo.availHeight);
  define(window.screen, 'colorDepth', () => 24);
  define(window.screen, 'pixelDepth', () => 24);
  if (!window.devicePixelRatio || Math.abs(window.devicePixelRatio - screenInfo.scale) > 0.01) {
    define(window, 'devicePixelRatio', () => screenInfo.scale);
  }

  // 8. WebGL vendor strings (headless Chromium reports SwiftShader).
  const patchWebGL = () => {
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (parameter) {
      if (parameter === 37445) return 'Intel Inc.';
      if (parameter === 37446) return 'Intel Iris OpenGL Engine';
      return getParameter.apply(this, [parameter]);
    };
    const getParameter2 = WebGL2RenderingContext && WebGL2RenderingContext.prototype.getParameter;
    if (getParameter2) {
      WebGL2RenderingContext.prototype.getParameter = function (parameter) {
        if (parameter === 37445) return 'Intel Inc.';
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';
        return getParameter2.apply(this, [parameter]);
      };
    }
  };
  try { patchWebGL(); } catch (e) {}

  // 9. Chrome exposes these helpers even in automation mode.
  if (!window.chrome || typeof window.chrome.app === 'undefined') {
    // already handled in step 4
  }
  try {
    if (!window.chrome.runtime) {
      Object.defineProperty(window.chrome, 'runtime', { value: { id: undefined, connect: () => {}, sendMessage: () => {} }, configurable: true });
    }
  } catch (e) {}

  // 10. Automation queries: keep everything visible/focused.
  define(document, 'hidden', () => false);
  define(document, 'visibilityState', () => 'visible');
  if (!document.hasFocus) document.hasFocus = () => true;

  // 11. No leaked CDP detection surface.
  try { delete window.__playwright__binding__; } catch (e) {}
  try { delete window.__pwInitScripts; } catch (e) {}
  try { delete window.__playwright; } catch (e) {}
  try { delete window.__pw_manual; } catch (e) {}
  try { delete window.__playwright_target__; } catch (e) {}
  try { delete window.cdc_; } catch (e) {}
})();
"""


def _stealth_script(user_agent: str, languages: Iterable[str], platform_name: str,
                    screen_info: Dict[str, int], cores: int, memory: int) -> str:
    match = re.search(r"Chrome/(\d+)\.(\d+)\.(\d+)\.(\d+)", user_agent)
    if match:
        major, minor, build, patch = (int(g) for g in match.groups())
        full_version = f"{major}.{minor}.{build}.{patch}"
        brands = [
            {"brand": "Not/A)Brand", "version": "8"},
            {"brand": "Chromium", "version": str(major)},
            {"brand": "Google Chrome", "version": str(major)},
        ]
    else:
        full_version = "131.0.0.0"
        brands = [
            {"brand": "Not/A)Brand", "version": "8"},
            {"brand": "Chromium", "version": "131"},
            {"brand": "Google Chrome", "version": "131"},
        ]
    replacements = {
        "__UA__": json.dumps(user_agent),
        "__LANGUAGES__": json.dumps(list(languages)),
        "__PLATFORM__": json.dumps(platform_name),
        "__SCREEN__": json.dumps(screen_info),
        "__CORES__": str(cores),
        "__MEMORY__": str(memory),
        "__BRANDS__": json.dumps(brands),
        "__FULL_VERSION__": json.dumps(full_version),
    }
    script = _STEALTH_JS
    for key, value in replacements.items():
        script = script.replace(key, value)
    return script


def _platform_identity() -> Dict[str, str]:
    """Guess the visitor's OS from the host OS so UA and platform agree."""
    import platform as _platform

    system = _platform.system()
    if system == "Windows":
        return {"platform": "Win32", "client_hint": "Windows", "platform_version": "15.0.0"}
    if system == "Darwin":
        return {"platform": "MacIntel", "client_hint": "macOS", "platform_version": "15.0.0"}
    # Linux Chrome reports the kernel version.
    return {"platform": "Linux x86_64", "client_hint": "Linux", "platform_version": "6.8.0"}


# --------------------------------------------------------------------------- #
# Browser backed session
# --------------------------------------------------------------------------- #


class HumanSession:
    """A Chromium session that behaves like a human visitor."""

    def __init__(
        self,
        url: str,
        *,
        proxy: Optional[str] = None,
        headless: bool = True,
        profile_dir: Optional[str] = None,
        block_resources: bool = True,
        locale: Optional[str] = None,
        timezone_id: Optional[str] = None,
        user_agent: Optional[str] = None,
        min_delay: float = 1.2,
        max_delay: float = 3.5,
        check_interval: float = 5.0,
        timeout: float = 45.0,
        debug: bool = False,
        dump_html: bool = False,
        dump_dir: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.target_url = url
        self.profile = profile_for_url(url)
        self.host = urllib.parse.urlparse(url).hostname or "www.ebay.com"
        self.scheme = urllib.parse.urlparse(url).scheme or "https"
        self.proxy = proxy or None
        self.headless = headless
        self.block_resources = block_resources
        self.locale = locale or self.profile["locale"]
        self.timezone_id = timezone_id or self.profile["timezone"]
        self.accept_language = self.profile["accept_language"]
        self.configured_ua = normalise_ua(user_agent)
        self.min_delay = max(0.0, float(min_delay))
        self.max_delay = max(self.min_delay, float(max_delay))
        # How often the live HTML is inspected while the session is waiting.
        self.check_interval = max(1.0, float(check_interval))
        self.timeout = timeout * 1000
        self.debug = debug
        # -debug saves the HTML of every navigation into dump_dir so the exact
        # page eBay served can be inspected afterwards.
        self.dump_html = dump_html or debug
        self.dump_dir = dump_dir or os.path.join(os.getcwd(), "ebay_debug")
        self.extra_headers = dict(extra_headers or {})

        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None
        self._user_agent = self.configured_ua or ""
        # Extra headers are replaced (not merged) by set_extra_http_headers, so
        # every change has to go through this accumulator.
        self._extra: Dict[str, str] = {
            "Accept-Language": self.accept_language,
            "Upgrade-Insecure-Requests": "1",
            **self.extra_headers,
        }
        self._languages = [self.locale, self.locale.split("-")[0], "en"]
        self._viewport = dict(random.choice(_VIEWPORTS))
        self._screen = {
            "width": self._viewport["width"],
            "height": self._viewport["height"],
            "availWidth": self._viewport["width"],
            "availHeight": self._viewport["height"] - random.choice([0, 0, 40, 48]),
            "scale": random.choice([1, 1, 1, 1.25]),
        }
        self._cores = random.choice([4, 8, 8, 12, 16])
        self._memory = random.choice([4, 8, 8, 16])
        self._warmed_domains: set[str] = set()
        self._closed = False
        self._step = 0
        self._crashed = False
        self._pending_checks: set[str] = set()
        self._device_checks: List[tuple] = []

    # -- logging ------------------------------------------------------------ #

    def _log(self, message: str, force: bool = False) -> None:
        """Log a step. Verbose output stays behind ``-debug``; problems and
        blocks are always printed."""
        if not (self.debug or force):
            return
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        print("    [%s] %s" % (stamp, message), flush=True)

    def _dump(self, url: str, page_html: str) -> Optional[str]:
        """Save the HTML of one navigation and report what it contains."""
        self._step += 1
        try:
            os.makedirs(self.dump_dir, exist_ok=True)
            name = "%02d_%s.html" % (self._step, re.sub(r"[^a-zA-Z0-9]+", "_", url)[:70])
            path = os.path.join(self.dump_dir, name)
            with open(path, "w", encoding="utf-8", errors="replace") as handle:
                handle.write(page_html)
        except OSError as exc:
            self._log("could not save html: %s" % exc)
            return None
        return path

    def _describe(self, page_html: str, url: str) -> str:
        """One line summary of a page: which eBay markup it contains."""
        parts = ["len=%d" % len(page_html)]
        for label, needle in (("srp-results(ul)", '<ul class="srp-results'),
                              ("s-card", "s-card--horizontal"),
                              ("s-item", "s-item__info"),
                              ("sresult", "sresult lvresult"),
                              ("captcha-sdk", "captcha"),
                              ("block-page", "Error Page | eBay")):
            if needle in page_html:
                parts.append(label)
        match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.S | re.I)
        if match:
            parts.append("title=%r" % _clean_text(match.group(1))[:60])
        items = len(re.findall(r'data-listingid="', page_html))
        if items:
            parts.append("listingid=%d" % items)
        return " ".join(parts)

    def _state(self, action: str) -> Dict[str, Any]:
        """Inspect the live DOM after an action and log what the page looks like.

        This is the "check the html after every step" hook: it reports the page
        identity, how many result cards are present, whether eBay is showing a
        challenge, and how far the visitor has scrolled.
        """
        page = self.page
        if page is None or page.is_closed():
            return {}
        try:
            state = page.evaluate(_STATE_JS) or {}
        except Exception as exc:
            self._log("%s: could not read the page (%s)" % (action, type(exc).__name__))
            return {}

        flags = []
        if state.get("challenge"):
            flags.append("CHALLENGE")
        if state.get("blocked"):
            flags.append("BLOCKED")
        if state.get("consent"):
            flags.append("consent-banner")
        if state.get("cards"):
            flags.append("cards=%d" % state["cards"])
        if state.get("total"):
            flags.append("html=%dkB" % (state["total"] // 1024))
        if state.get("scroll"):
            flags.append("scroll=%d%%" % state["scroll"])
        if state.get("title"):
            flags.append("title=%r" % state["title"][:50])
        if state.get("text"):
            flags.append("text=%r" % state["text"][:70])
        self._log("after %-22s %s" % (action, " | ".join(flags) or "-"))
        return state

    def _is_challenge(self, state: Optional[Dict[str, Any]] = None) -> bool:
        """True when eBay shows its device/browser check instead of content."""
        state = state if state is not None else self._state("challenge check")
        url = (state.get("url") or "").lower()
        if "splashui/challenge" in url or "devicebind" in url:
            return True
        title = (state.get("title") or "").lower()
        if "disculpa la interrupci" in title or "sorry for the interruption" in title:
            return True
        text = (state.get("text") or "").lower()
        return "comprobaci" in text and "navegador" in text

    def _wait_for_device_check(self, timeout: float = 20.0) -> None:
        """Stay on the page until eBay's device verification finished.

        eBay fires a ``devicebind.ebay.es`` beacon on every page and only serves
        search results to a device it managed to verify.  Navigating away while
        that beacon is still running is a good way to get a 403 on the next
        request, so the page is kept open until the check completes.
        """
        deadline = time.time() + timeout
        while self._pending_checks and time.time() < deadline:
            self._watch(min(deadline - time.time(), self.check_interval),
                        "device check (%d pending)" % len(self._pending_checks))
        if self._pending_checks:
            self._log("device check still running after %.0fs (%d pending)"
                      % (timeout, len(self._pending_checks)))
        else:
            self._log("device check completed: %s"
                      % (str(self._device_checks[-1]) if self._device_checks else "nothing to do",))

    def _wait_for_challenge(self, timeout: float = 45.0) -> bool:
        """eBay's browser check redirects back on its own; just wait for it."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._crashed or self.page is None or self.page.is_closed():
                self._log("the browser check cannot finish, the renderer is gone")
                return False
            remaining = deadline - time.time()
            state = self._watch(min(remaining, self.check_interval), "browser check")
            if not self._is_challenge(state):
                return True
        self._log("the browser check did not finish in %.0fs" % timeout)
        return False

    def _install_console(self) -> None:
        """Log every network response so blocks can be traced request by request."""
        page = self.page
        if page is None:
            return

        def on_request(request):
            url = request.url
            if _is_device_check(url):
                self._pending_checks.add(url)
                self._log("device check started: %s" % url[:100])

        def on_response(response):
            request = response.request
            self._pending_checks.discard(request.url)
            if request.resource_type not in ("document", "xhr", "fetch"):
                return
            try:
                headers = response.headers
                server = headers.get("server", "-")
                refresh = headers.get("refresh", "")
                cookies = [name.split("=")[0] for name in
                           (headers.get("set-cookie", "") or "").split(",") if "=" in name]
                extra = ""
                if refresh:
                    extra = " refresh=%r" % refresh[:80]
                if cookies:
                    extra += " set-cookie=%s" % ",".join(cookies[:6])
                self._log("%-3s %-4s %s%s" % (response.status, request.resource_type[:4],
                                               request.url[:110], extra))
                if _is_device_check(request.url):
                    self._device_checks.append((response.status, request.url))
                    self._log("device check finished: %s %s"
                              % (response.status, request.url[:90]))
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> "HumanSession":
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()

        proxy_settings = None
        if self.proxy:
            proxy_settings = {"server": self.proxy}

        profile_dir = self.profile_dir
        os.makedirs(profile_dir, exist_ok=True)

        launch_kwargs: Dict[str, Any] = {
            "user_data_dir": profile_dir,
            "headless": self.headless,
            "proxy": proxy_settings,
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "viewport": {"width": self._viewport["width"], "height": self._viewport["height"]},
            "device_scale_factor": self._screen["scale"],
            "is_mobile": False,
            "has_touch": False,
            "color_scheme": random.choice(["light", "light", "dark"]),
            "reduced_motion": "no-preference",
            "service_workers": "block",
            "accept_downloads": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
                "--disable-infobars",
                "--disable-notifications",
                "--disable-broadcasting-initialization",
                "--password-store=basic",
                "--use-mock-keychain",
                # Renderers crash on small /dev/shm (containers) and on the
                # argon2.wasm proof-of-work eBay's browser check runs.
                "--disable-dev-shm-usage",
                "--renderer-process-limit=1",
                "--window-size=%d,%d" % (self._viewport["width"], self._viewport["height"]),
            ],
            # Removing --enable-automation drops the "controlled by automation"
            # infobar and the CDP banner.
            "ignore_default_args": ["--enable-automation", "--headless"],
            "extra_http_headers": dict(self._extra),
        }

        self._launch_context(launch_kwargs, channel="chromium")
        self._prepare_page()
        return self

    def _launch_context(self, launch_kwargs: Dict[str, Any], channel: Optional[str]) -> None:
        errors = []
        channels = [channel, None] if channel else [None]
        for ch in channels:
            kwargs = dict(launch_kwargs)
            if ch:
                kwargs["channel"] = ch
            try:
                self._context = self._playwright.chromium.launch_persistent_context(**kwargs)
                return
            except Exception as exc:  # channel may be missing
                errors.append("%s: %s" % (ch or "bundled", exc))
        raise RuntimeError("Could not launch Chromium (%s)" % "; ".join(errors))

    def _prepare_page(self) -> None:
        context = self._context
        context.set_default_navigation_timeout(self.timeout)
        context.set_default_timeout(self.timeout)

        # Learn the real UA (and its version/client hints) from the browser itself so
        # the header, navigator and Sec-CH-UA family never disagree.  This runs
        # before the stealth script is installed, so the values are genuine.
        probe = context.pages[0] if context.pages else context.new_page()
        identity_info: Dict[str, Any] = {}
        try:
            probe.goto("about:blank")
            identity_info = probe.evaluate(_FINGERPRINT_JS) or {}
        except Exception:
            identity_info = {}
        raw_ua = identity_info.get("ua") or ""
        fixed_ua = normalise_ua(self.configured_ua or raw_ua)
        if not fixed_ua:
            fixed_ua = raw_ua
        self._user_agent = fixed_ua
        if fixed_ua and fixed_ua != raw_ua:
            # Rewrite the outgoing header too, otherwise the request still
            # advertises HeadlessChrome.
            self._extra["User-Agent"] = fixed_ua

        identity = _platform_identity()
        language_list = [self.locale]
        for extra in self.accept_language.split(","):
            tag = extra.split(";")[0].strip()
            if tag and tag not in language_list:
                language_list.append(tag)
        if "en" not in language_list:
            language_list.append("en")
        self._languages = language_list

        context.add_init_script(
            script=_stealth_script(
                self._user_agent or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                self._languages,
                identity["platform"],
                self._screen,
                self._cores,
                self._memory,
            )
        )
        # Match the client hints with the reported platform.  The bundled
        # Chromium only advertises the "Chromium" brand; a "Chrome/151" UA with
        # a Chromium-only brand list is a contradiction Akamai's bot scoring
        # picks up, so the Google Chrome brand is added here.  Versions come
        # from the browser, never from the (possibly stale) UA string.
        self._extra["Sec-CH-UA-Platform"] = '"%s"' % identity["client_hint"]
        self._extra["Sec-CH-UA-Mobile"] = "?0"
        self._extra["Sec-CH-UA-Platform-Version"] = '"%s"' % (
            identity_info.get("platformVersion") or identity["platform_version"]
        )
        self._extra["Sec-CH-UA"] = self._client_hints_brands(identity_info, fixed_ua)
        full_version = self._full_version(identity_info, fixed_ua)
        self._extra["Sec-CH-UA-Full-Version"] = full_version
        self._extra["Accept-Language"] = self.accept_language
        context.set_extra_http_headers(dict(self._extra))

        page = context.pages[0] if context.pages else context.new_page()
        self.page = page
        if self.block_resources:
            self._install_resource_filter(page)
        page.set_default_timeout(self.timeout)
        page.on("crash", lambda: self._on_crash())
        self._install_console()
        self._log("browser ready: ua=%s" % self._user_agent)
        self._log("locale=%s tz=%s viewport=%sx%s dpr=%s"
                  % (self.locale, self.timezone_id, self._viewport["width"],
                     self._viewport["height"], self._screen["scale"]))
        self._log("headers: %s" % ", ".join("%s: %s" % (k, v) for k, v in self._extra.items()))

    def _on_crash(self) -> None:
        """A renderer crash means the tab is gone; the next fetch opens a new one."""
        self._crashed = True
        self._log("RENDERER CRASHED - the tab is gone, a new one will be opened", force=True)

    @staticmethod
    def _full_version(identity_info: Dict[str, Any], user_agent: str) -> str:
        """The UA build wins: Sec-CH-UA-Full-Version has to match the UA string."""
        match = re.search(r"Chrome/(\d+\.\d+\.\d+\.\d+)", user_agent or "")
        if match:
            return match.group(1)
        for brand in identity_info.get("fullVersionList") or []:
            if brand.get("brand") in ("Google Chrome", "Chromium") and brand.get("version"):
                return brand["version"]
        return "131.0.0.0"

    @classmethod
    def _client_hints_brands(cls, identity_info: Dict[str, Any], user_agent: str) -> str:
        brands = list(identity_info.get("brands") or [])
        if not any(brand.get("brand") == "Google Chrome" for brand in brands):
            match = re.search(r"Chrome/(\d+)\.", user_agent or "")
            version = match.group(1) if match else "131"
            brands.insert(0, {"brand": "Google Chrome", "version": version})
        return ", ".join('"%s";v="%s"' % (brand["brand"], brand["version"]) for brand in brands
                         if brand.get("brand") and brand.get("version"))

    def _install_resource_filter(self, page) -> None:
        """Skip images/media/fonts (kept configurable: they are a soft signal)."""

        def handler(route):
            request = route.request
            if request.resource_type in _BLOCKABLE:
                with contextlib.suppress(Exception):
                    route.abort()
                    return
            with contextlib.suppress(Exception):
                route.continue_()

        with contextlib.suppress(Exception):
            page.route("**/*", handler)

    # -- human behaviour ---------------------------------------------------- #

    def _pause(self, low: float = 0.25, high: float = 1.1) -> None:
        time.sleep(random.uniform(low, high))

    def _watch(self, seconds: float, reason: str, step: Optional[float] = None) -> Dict[str, Any]:
        """Inspect the live HTML every ``check_interval`` seconds for a while.

        Every wait in this class goes through here, so the log always shows what
        eBay is serving (challenge, results, block page) while nothing happens.
        """
        step = self.check_interval if step is None else max(0.5, float(step))
        deadline = time.time() + max(0.0, seconds)
        state: Dict[str, Any] = {}
        while True:
            state = self._state(reason)
            if self._crashed:
                self._log("stopped watching (%s): the renderer crashed" % reason, force=True)
                break
            if state.get("blocked"):
                self._log("stopped watching (%s): eBay is serving a block page" % reason,
                          force=True)
                break
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(step, remaining))
        return state

    def _human_mouse_move(self, steps: int = 3) -> None:
        """Move the cursor along a small curve, like a hand would."""
        page = self.page
        if page is None or page.is_closed():
            return
        try:
            width = self._viewport["width"]
            height = self._viewport["height"]
            x, y = random.uniform(0, width), random.uniform(0, height)
            page.mouse.move(x, y, steps=random.randint(8, 25))
            self._pause(0.05, 0.25)
            page.mouse.move(
                random.uniform(0, width),
                random.uniform(0, height),
                steps=random.randint(5, 18),
            )
        except Exception:
            pass

    def _human_scroll(self, rounds: Optional[int] = None) -> None:
        """Scroll in uneven increments so lazy content loads like a real visit."""
        page = self.page
        if page is None or page.is_closed():
            return
        if rounds is None:
            rounds = random.randint(3, 7)
        try:
            for index in range(rounds):
                delta = random.randint(120, 900)
                page.mouse.wheel(0, delta)
                self._pause(0.3, 1.4)
                if random.random() < 0.25:
                    page.mouse.wheel(0, -random.randint(80, 400))
                    self._pause(0.2, 0.8)
                if random.random() < 0.4:
                    self._human_mouse_move()
                self._state("scroll %d/%d (+%dpx)" % (index + 1, rounds, delta))
        except Exception as exc:
            self._log("scroll interrupted: %s" % type(exc).__name__)

    def _dismiss_consent(self) -> None:
        """Accept cookie banners: eBay stores the choice in a cookie."""
        page = self.page
        if page is None or page.is_closed():
            return
        selectors = (
            "button:has-text('Accept all')",
            "button:has-text('Accept All')",
            "button:has-text('Aceptar todo')",
            "button:has-text('Aceptar')",
            "button:has-text('Allow all')",
            "button:has-text('Alle akzeptieren')",
            "button:has-text('Tout accepter')",
            "button:has-text('Accept')",
            "#gdpr-banner button:has-text('Accept')",
            "[data-testid='consent-banner'] button",
        )
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible(timeout=400):
                    locator.click(timeout=2500)
                    self._pause(0.6, 1.6)
                    self._log("consent accepted via %r" % selector)
                    self._state("consent click")
                    return
            except Exception:
                continue
        self._state("consent scan")

    def _hover_first_listings(self, count: int = 2) -> None:
        """Hover a few results, like a shopper comparing items."""
        page = self.page
        if page is None or page.is_closed():
            return
        for selector in ("li.s-item a.s-item__link", "li.s-card a.s-card__link",
                         "li.sresult h3 a", ".s-item__title a"):
            try:
                locator = page.locator(selector)
                total = locator.count()
            except Exception:
                continue
            if total:
                self._log("%d result links found to hover" % total)
                for index in range(min(count, total)):
                    try:
                        locator.nth(random.randint(0, total - 1)).hover(timeout=2500)
                        self._pause(0.4, 1.5)
                        self._state("hover %d/%d" % (index + 1, count))
                    except Exception:
                        continue
                return
            self._state("hover scan")

    def _wait_for_results(self) -> None:
        page = self.page
        if page is None:
            return
        for selector in ("ul.srp-results li.s-card", "li.s-item", "li.sresult",
                         "ul.srp-results", "[data-viewport]", ".srp-controls"):
            try:
                page.wait_for_selector(selector, timeout=self.check_interval * 1000,
                                       state="attached")
                self._log("results matched %r" % selector)
                self._state("results loaded")
                return
            except Exception:
                continue
        self._log("no result markup appeared")
        self._watch(self.check_interval, "results missing")

    def warmup(self, url: Optional[str] = None) -> None:
        """Land on the site entry page (and a category page) before searching."""
        page = self.page
        if page is None or page.is_closed():
            return
        target = urllib.parse.urlparse(url or self.target_url)
        origin = "%s://%s" % (target.scheme or "https", target.hostname or self.host)

        if self.host not in self._warmed_domains:
            # Two *different*, real pages: navigating to the same url twice in a
            # row is what triggers eBay's device/browser check, and a dead url
            # (404) looks nothing like a shopper either.
            second = random.choice(["deals/", "sch/i.html?_nkw=electronics&_sacat=0",
                                    "sch/i.html?_sacat=0&_nkw=consolas",
                                    "sch/i.html?_nkw=moviles&_sacat=0"])
            pages = [origin + "/", origin + "/" + second]
            self._log("warmup for %s: %s" % (self.host, ", ".join(pages)))
            referer = origin + "/"
            for candidate in pages:
                try:
                    started = time.time()
                    # Every navigation carries a referrer, exactly like a click
                    # through the site; without it eBay answers 403 straight away.
                    response = page.goto(candidate, wait_until="domcontentloaded",
                                         timeout=self.timeout, referer=referer)
                    referer = candidate
                    status = response.status if response else 200
                    html = page.content()
                    if self.dump_html:
                        path = self._dump(candidate, html)
                        if path:
                            self._log("html saved: %s" % path)
                    self._log("warmup %s -> %s %s (%.1fs) %s"
                              % (candidate[:80], status, response.headers.get("server", "-")
                                 if response else "-", time.time() - started,
                                 self._describe(html, candidate)))
                    state = self._state("warmup load")
                    if self._is_challenge(state):
                        self._log("eBay showed its browser check, waiting it out", force=True)
                        self._wait_for_challenge()
                        state = self._state("after browser check")
                    self._wait_for_device_check()
                    if status == 404:
                        self._log("warmup page %s is gone, skipping it" % candidate)
                    if status >= 400 and candidate.rstrip("/") == origin:
                        # The entry page is what issues the bm_sv/dp1 cookies; if
                        # even that is refused the whole IP/session is blocked.
                        raise BotDetected("eBay answered HTTP %s on %s"
                                          % (status, candidate))
                    self._watch(random.uniform(1.0, 2.5), "warmup settling")
                    self._dismiss_consent()
                    self._human_mouse_move()
                    if random.random() < 0.6:
                        self._human_scroll(rounds=random.randint(2, 4))
                except BotDetected:
                    raise
                except Exception as exc:
                    self._log("warmup %s failed: %s" % (candidate, exc))
            self._warmed_domains.add(self.host)
            self._log("cookies: %s" % ", ".join(sorted(c["name"] for c in self.cookies())))

        # Every request gets a mouse movement and a random pause beforehand.
        self._human_mouse_move()
        self._pause(self.min_delay, self.max_delay)
        self._log("paused %.1fs before the search request" % self.max_delay)

    # -- fetching ----------------------------------------------------------- #

    def _ensure_page(self):
        """Return a usable page, recreating it if the tab/browser died."""
        if self.page is not None and not self.page.is_closed():
            return self.page
        if self._context is None:
            raise RuntimeError("browser context is gone")
        self._log("page was closed, opening a new tab")
        self.page = self._context.new_page()
        self.page.set_default_timeout(self.timeout)
        if self.block_resources:
            self._install_resource_filter(self.page)
        self._install_console()
        return self.page

    def fetch(self, url: Optional[str] = None, *, warmup: bool = True) -> str:
        """Load ``url`` like a human and return the rendered HTML.

        The renderer occasionally dies on eBay's proof-of-work page; one retry
        with a fresh tab is attempted before giving up.
        """
        try:
            return self._fetch_once(url, warmup=warmup)
        except _RendererGone:
            self._log("retrying once with a new tab")
            time.sleep(3)
            return self._fetch_once(url, warmup=warmup)

    def _fetch_once(self, url: Optional[str] = None, *, warmup: bool = True) -> str:
        if self._closed:
            raise RuntimeError("session is closed")
        page = self._ensure_page()
        target = url or self.target_url
        if warmup:
            self.warmup(target)
            page = self._ensure_page()

        parsed = urllib.parse.urlparse(target)
        origin = "%s://%s" % (parsed.scheme or "https", parsed.hostname or self.host)

        # A shopper clicks through from the homepage, so the navigation carries a
        # referrer and the same-origin cookie chain.
        started = time.time()
        self._crashed = False
        response = page.goto(target, wait_until="domcontentloaded", timeout=self.timeout,
                             referer=origin + "/")
        status = response.status if response else None
        headers = response.headers if response else {}
        self._log("GET %s (referer %s/)" % (target[:120], origin))

        if self._crashed:
            raise _RendererGone("the renderer crashed while loading the search page")

        if status and status >= 400:
            # Nothing to wait for: eBay refused the page. Report it right away.
            html = page.content()
            if self.dump_html:
                path = self._dump(target, html)
                if path:
                    self._log("html saved: %s" % path)
            self._log("search -> %s server=%s %s"
                      % (status, headers.get("server", "-"), self._describe(html, target)),
                      force=True)
            raise BotDetected("eBay answered HTTP %s (server %s)"
                              % (status, headers.get("server", "?")))

        self._watch(random.uniform(0.8, 2.2), "search settling")
        self._dismiss_consent()
        state = self._state("search loaded")
        if self._is_challenge(state):
            self._log("eBay is running its browser check, waiting it out", force=True)
            self._wait_for_challenge()
            state = self._state("after browser check")
        if self._crashed:
            raise _RendererGone("the renderer crashed during the browser check")

        self._wait_for_device_check()
        self._wait_for_results()
        self._human_scroll()
        self._human_mouse_move()
        self._hover_first_listings()

        if self._crashed:
            raise _RendererGone("the renderer crashed while reading the results")
        html = page.content()
        if self.dump_html:
            path = self._dump(target, html)
            if path:
                self._log("html saved: %s" % path)
        self._log("search -> %s server=%s (%.1fs) %s"
                  % (status, headers.get("server", "-"), time.time() - started,
                     self._describe(html, target)))

        if status and status >= 400:
            raise BotDetected("eBay answered HTTP %s (server %s)"
                              % (status, headers.get("server", "?")))
        reason = _looks_blocked(html, status)
        if reason:
            raise BotDetected("eBay did not return the search page (%s)" % reason)
        return html

    def cookies(self, domain: Optional[str] = None) -> List[Dict[str, Any]]:
        if self._context is None:
            return []
        try:
            cookies = self._context.cookies()
        except Exception:
            return []
        if domain:
            cookies = [c for c in cookies if domain in (c.get("domain") or "")]
        return cookies

    @property
    def profile_dir(self) -> str:
        base = os.environ.get("EBAY_PROFILE_DIR")
        if not base:
            base = os.path.join(os.path.expanduser("~"), ".ebayautosearch", "browser-profile")
        return base

    def close(self) -> None:
        self._closed = True
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
            getattr(self._playwright, "stop", None),
        ):
            if closer is None:
                continue
            with contextlib.suppress(Exception):
                closer()
        self._context = None
        self._browser = None
        self._playwright = None
        self.page = None

    def __enter__(self) -> "HumanSession":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# requests fallback
# --------------------------------------------------------------------------- #


class RequestsSession:
    """Fallback for machines where Playwright cannot run.

    It replays a browser's cookie jar and header set.  eBay may still refuse
    requests-only traffic, so :func:`build_session` only uses it as a last
    resort.
    """

    def __init__(self, url: str, *, proxy: Optional[str] = None, user_agent: Optional[str] = None,
                 min_delay: float = 1.2, max_delay: float = 3.5, timeout: float = 30.0,
                 cookies: Optional[List[Dict[str, Any]]] = None, debug: bool = False) -> None:
        self.target_url = url
        self.profile = profile_for_url(url)
        parsed = urllib.parse.urlparse(url)
        self.host = parsed.hostname or "www.ebay.com"
        self.origin = "%s://%s" % (parsed.scheme or "https", self.host)
        self.proxy = proxy or None
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.debug = debug
        self._user_agent = user_agent or (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
        self.session = requests.Session()
        self.session.proxies.update({"http": self.proxy, "https": self.proxy} if self.proxy else {})
        self.session.headers.update(self._headers())
        self._warmed = False
        if cookies:
            self._apply_cookies(cookies)

    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": self.profile["accept_language"],
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "max-age=0",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "DNT": "1",
        }

    def _apply_cookies(self, cookies: List[Dict[str, Any]]) -> None:
        for cookie in cookies:
            try:
                self.session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/"),
                )
            except Exception:
                continue

    def warmup(self) -> None:
        if self._warmed:
            return
        time.sleep(random.uniform(self.min_delay, self.max_delay))
        try:
            self.session.get(self.origin + "/", timeout=self.timeout,
                             headers={**self.session.headers, "Sec-Fetch-Site": "none"})
            time.sleep(random.uniform(0.8, 2.2))
        except requests.RequestException as exc:
            if self.debug:
                print("[warmup] failed: %s" % exc)
        self._warmed = True

    def fetch(self, url: Optional[str] = None) -> str:
        target = url or self.target_url
        self.warmup()
        headers = dict(self.session.headers)
        headers["Referer"] = self.origin + "/"
        response = self.session.get(target, timeout=self.timeout, headers=headers)
        if self.debug:
            print("    [%s] requests GET %s -> %s server=%s %s"
                  % (datetime.datetime.now().strftime("%H:%M:%S"), target[:110],
                     response.status_code, response.headers.get("server", "-"),
                     _clean_text(response.text[:200])[:80]))
        reason = _looks_blocked(response.text, response.status_code)
        if reason:
            raise BotDetected("eBay blocked the request (%s)" % reason)
        time.sleep(random.uniform(self.min_delay, self.max_delay))
        return response.text

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.session.close()

    def __enter__(self) -> "RequestsSession":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def build_session(url: str, *, mode: str = "auto", debug: bool = False, **kwargs: Any):
    """Create the best session available.

    ``mode`` is ``"browser"``, ``"requests"`` or ``"auto"`` (default): the real
    browser is used and the requests fallback only kicks in when Playwright is
    missing or refuses to start.
    """
    mode = (mode or "auto").lower()
    requests_kwargs = {
        key: value for key, value in kwargs.items()
        if key in {"proxy", "user_agent", "min_delay", "max_delay", "timeout"}
    }
    if mode == "requests":
        return RequestsSession(url, debug=debug, **requests_kwargs)
    try:
        return HumanSession(url, debug=debug, **kwargs).start()
    except Exception as exc:
        if mode == "browser":
            raise
        print("! Browser mode unavailable (%s), falling back to plain requests" % exc)
        return RequestsSession(url, debug=debug, **requests_kwargs)
