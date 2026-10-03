"""Headless screenshots of Home Assistant views, with a render report.

Auth: HA's frontend keeps OAuth tokens in localStorage["hassTokens"], not a
cookie. An init script plants the long-lived token there before the frontend
boots. Every run uses a fresh browser context, so the token never reaches a
persistent profile.
"""
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hactl import output
from hactl.errors import HactlError

VIEWPORTS = {
    "phone": {"width": 412, "height": 915, "device_scale_factor": 2, "is_mobile": True, "has_touch": True},
    "desktop": {"width": 1440, "height": 900, "device_scale_factor": 1, "is_mobile": False, "has_touch": False},
}
THEME_VAR, THEME_VALUE = "--ha-card-border-radius", "22px"  # warm-minimal sets this in both modes

# Unhandled promise rejections reach Playwright as an opaque "Object" page error;
# log the actual reason so the report can say what failed.
REJECTION_JS = (
    "window.addEventListener('unhandledrejection', ev => { let r; "
    "try { r = JSON.stringify(ev.reason); } catch (e) { r = String(ev.reason); } "
    "console.error('unhandled rejection: ' + String(r).slice(0, 300)); });"
)
# Console noise HA's own frontend makes on every load; not a dashboard problem.
BENIGN_CONSOLE = (
    "Subscription not found",  # unsubscribing from a websocket subscription the server already dropped
)

PROBE_JS = """() => {
  const all = [];
  const walk = (root) => { for (const el of root.querySelectorAll('*')) { all.push(el); if (el.shadowRoot) walk(el.shadowRoot); } };
  walk(document);
  const deepText = (node) => {
    let s = '';
    const visit = (n) => {
      if (n.nodeType === 3) s += n.textContent + ' ';
      if (n.shadowRoot) visit(n.shadowRoot);
      for (const c of n.childNodes) visit(c);
    };
    visit(node);
    return s.replace(/\\s+/g, ' ').trim();
  };
  const cards = all.filter(e => e.tagName === 'HA-CARD');
  const errors = all.filter(e => e.tagName === 'HUI-ERROR-CARD').map(e => deepText(e).slice(0, 200));
  const unavailable = [...new Set(cards.map(deepText)
    .filter(t => /\\b(unavailable|unknown)\\b/i.test(t)).map(t => t.slice(0, 120)))];
  const host = document.querySelector('home-assistant');
  const theme = [document.documentElement, host].filter(Boolean)
    .map(el => getComputedStyle(el).getPropertyValue('THEME_VAR').trim()).find(v => v) || '';
  const view = all.find(e => e.tagName === 'HUI-VIEW' || e.tagName === 'HUI-VIEW-CONTAINER');
  return {cards: cards.length, errors, unavailable, theme,
          height: Math.max(document.documentElement.scrollHeight, view ? view.scrollHeight : 0)};
}""".replace("THEME_VAR", THEME_VAR)


@dataclass(frozen=True)
class ShotSpec:
    path: str
    viewport: str
    scheme: str

    @property
    def filename(self) -> str:
        return f"{slug(self.path)}_{self.viewport}_{self.scheme}.png"


def slug(path: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-") or "root"


def parse_viewports(text: str) -> list:
    names = [v.strip() for v in text.split(",") if v.strip()]
    bad = [n for n in names if n not in VIEWPORTS]
    if bad or not names:
        raise HactlError(f"unknown viewport(s) {', '.join(bad) or '(none)'}; choose from {', '.join(VIEWPORTS)}")
    return names


def plan_shots(paths, viewports, schemes) -> list:
    return [ShotSpec(p, v, s) for s in schemes for v in viewports for p in paths]


def default_out_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "hactl" / "shots" / datetime.now().strftime("%Y%m%d-%H%M%S")


def auth_script(url: str, token: str) -> str:
    tokens = {"hassUrl": url, "clientId": url + "/", "access_token": token, "token_type": "Bearer",
              "refresh_token": "", "expires_in": 315360000, "expires": int(time.time() * 1000) + 315360000 * 1000}
    return (f"localStorage.setItem('hassTokens', {json.dumps(json.dumps(tokens))});"
            "localStorage.setItem('dockedSidebar', JSON.stringify('always_hidden'));")


def summarize_console(raw) -> list:
    """Drop benign noise and the opaque 'Object' duplicates of rejections; count repeats."""
    kept = [m for m in raw if m != "pageerror: Object" and not any(b in m for b in BENIGN_CONSOLE)]
    return [f"{m} (x{n})" if n > 1 else m for m, n in Counter(kept).items()]


def response_problem(status):
    """Why a page load is unusable, or None. HA restarting shows up as 502/503 from Traefik."""
    if status is None:
        return "no response from HA"
    if status >= 400:
        return f"HA answered HTTP {status}: restarting, or the path doesn't exist?"
    return None


def _check_login(page) -> None:
    if "/auth/authorize" in page.url:
        raise HactlError("HA showed its login page: the token was rejected (see docs/ha.md)")


def _settle(page, timeout_s=20.0) -> dict:
    """Wait until the ha-card count is stable for 1.5 s."""
    deadline = time.monotonic() + timeout_s
    last, since = None, time.monotonic()
    _check_login(page)
    info = page.evaluate(PROBE_JS)
    while time.monotonic() < deadline:
        _check_login(page)
        info = page.evaluate(PROBE_JS)
        if info["cards"] != last:
            last, since = info["cards"], time.monotonic()
        elif info["cards"] > 0 and time.monotonic() - since > 1.5:
            break
        page.wait_for_timeout(300)
    return info


def _shoot(ctx, base, spec, out_dir) -> dict:
    page = ctx.new_page()
    console = []
    page.on("console", lambda m: console.append(f"console: {m.text[:200]}") if m.type == "error" else None)
    page.on("pageerror", lambda e: console.append(f"pageerror: {str(e)[:200]}"))
    t0 = time.monotonic()
    resp = page.goto(base + spec.path, wait_until="domcontentloaded", timeout=30000)
    problem = response_problem(resp.status if resp else None)
    if problem:
        raise HactlError(f"{spec.path}: {problem}")
    info = _settle(page)
    _check_login(page)
    vp = VIEWPORTS[spec.viewport]
    if info["height"] > vp["height"]:  # HA scrolls inside its view; grow the viewport to hold it all
        page.set_viewport_size({"width": vp["width"], "height": min(info["height"] + 40, 8000)})
        page.wait_for_timeout(800)
    f = out_dir / spec.filename
    page.screenshot(path=str(f), full_page=True)
    page.close()
    return {"path": spec.path, "viewport": spec.viewport, "scheme": spec.scheme, "file": str(f),
            "seconds": round(time.monotonic() - t0, 1), "cards": info["cards"], "error_cards": info["errors"],
            "unavailable": info["unavailable"], "theme_ok": info["theme"] == THEME_VALUE,
            "console": summarize_console(console)[:15]}


def take(client, specs, out_dir: Path) -> list:
    from playwright.sync_api import Error as PlaywrightError

    try:
        return _take(client, specs, out_dir)
    except PlaywrightError as e:  # unreachable HA, timeouts, a page torn down mid-load
        first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        raise HactlError(f"browser could not load HA at {client.url}: {first}") from None


def _take(client, specs, out_dir: Path) -> list:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    results, contexts = [], {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for spec in specs:
                key = (spec.viewport, spec.scheme)
                if key not in contexts:
                    vp = VIEWPORTS[spec.viewport]
                    ctx = browser.new_context(
                        viewport={"width": vp["width"], "height": vp["height"]},
                        device_scale_factor=vp["device_scale_factor"], is_mobile=vp["is_mobile"],
                        has_touch=vp["has_touch"], color_scheme=spec.scheme,
                    )
                    ctx.add_init_script(auth_script(client.url, client.bearer()))
                    ctx.add_init_script(REJECTION_JS)
                    contexts[key] = ctx
                results.append(_shoot(contexts[key], client.url, spec, out_dir))
        finally:
            browser.close()
    return results


def report(results) -> tuple:
    lines, bad = [], False
    for r in results:
        flags = []
        if r["error_cards"]:
            flags.append(f"{len(r['error_cards'])} error card(s)")
        if not r["theme_ok"]:
            flags.append("theme is NOT warm-minimal")
        if r["console"]:
            flags.append(f"{len(r['console'])} console error(s)")
        if r["unavailable"]:
            flags.append(f"{len(r['unavailable'])} card(s) show unavailable/unknown")
        bad |= bool(r["error_cards"]) or not r["theme_ok"]
        lines.append(f"{r['file']}  {r['cards']} cards  {r['seconds']}s  {'; '.join(flags) or 'clean'}")
        lines += [f"    error card: {x}" for x in r["error_cards"]]
        lines += [f"    unavailable: {x}" for x in r["unavailable"]]
        lines += [f"    {x}" for x in r["console"]]
    return lines, bad


def _run(args) -> int:
    from hactl.client import Client

    paths = [p if p.startswith("/") else "/" + p for p in args.paths]
    specs = plan_shots(paths, parse_viewports(args.viewport), args.scheme or ["dark"])
    results = take(Client(), specs, Path(args.out) if args.out else default_out_dir())
    lines, bad = report(results)
    output.emit(args, results, lines)
    return 1 if bad else 0


def register(sub) -> None:
    p = sub.add_parser("shot", parents=[output.COMMON], help="screenshot HA views at phone/desktop size")
    p.add_argument("paths", nargs="+", help="e.g. /home-ops/0")
    p.add_argument("--viewport", default="phone,desktop", help="comma list of: " + ", ".join(VIEWPORTS))
    p.add_argument("--scheme", action="append", choices=["dark", "light"], help="repeatable; default dark")
    p.add_argument("--out", help="output dir (default $XDG_CACHE_HOME/hactl/shots/<time>)")
    p.set_defaults(func=_run)
