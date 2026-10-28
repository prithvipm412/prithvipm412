#!/usr/bin/env python3
"""Fetch live GitHub data and render the profile dashboard SVGs.

Run locally with: GH_TOKEN=xxx python scripts/generate.py
"""

import bisect
import colorsys
import json
import math
import os
import random
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape as escape_xml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
ASSETS_DIR = os.path.join(ROOT, "assets")
README_PATH = os.path.join(ROOT, "README.md")
ICON_CACHE_PATH = os.path.join(ROOT, "scripts", "icon_cache.json")

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
SIMPLE_ICONS_VERSION = "13.21.0"
IST = timezone(timedelta(hours=5, minutes=30))
WIDTH = 840
MONO_CHAR_W_RATIO = 0.6

ICON_SLUG_OVERRIDES = {
    "Godot": "godotengine",
    "Next.js": "nextdotjs",
    "Tailwind": "tailwindcss",
}

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

GRAPHQL_QUERY = """
query($login: String!) {
  user(login: $login) {
    followers { totalCount }
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
    repositories(ownerAffiliations: OWNER, isFork: false, first: 100, orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes {
        name
        url
        description
        isPrivate
        stargazerCount
        forkCount
        pushedAt
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""


class FetchError(Exception):
    pass


# ---------------------------------------------------------------------------
# Shared SVG scaffolding. The whole palette lives here as CSS custom
# properties (light overrides via @media), so every card is themed from one
# place instead of duplicating hex literals per file.
# ---------------------------------------------------------------------------

CSS_ROOT = """
:root {
  --bg: #0a0a0a; --card: #111111; --border: #232323; --text: #ededed; --muted: #7a7a7a;
  --accent: #f5f5f5; --accent-soft: #4a4a4a;
  --heat0: #151515; --heat1: #2b2b2b; --heat2: #4d4d4d; --heat3: #8c8c8c; --heat4: #e8e8e8;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #fafafa; --card: #ffffff; --border: #e4e4e4; --text: #111111; --muted: #6b6b6b;
    --accent: #111111; --accent-soft: #b5b5b5;
    --heat0: #f0f0f0; --heat1: #d4d4d4; --heat2: #a3a3a3; --heat3: #5c5c5c; --heat4: #1a1a1a;
  }
}
.sans { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.card { fill: var(--card); stroke: var(--border); stroke-width: 1; }
.title { font-size: 13px; font-weight: 600; fill: var(--text); }
.label { font-size: 11px; fill: var(--muted); }
.label-sm { font-size: 10px; fill: var(--muted); }
.value { fill: var(--text); }
.value-muted { fill: var(--muted); }
.sep { stroke: var(--border); stroke-width: 1; }
"""

GRAIN_FILTER = """<filter id="grain" x="-5%" y="-5%" width="110%" height="110%">
  <feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="2" seed="7" stitchTiles="stitch" result="n"/>
  <feColorMatrix in="n" type="matrix" values="0 0 0 0 0.5  0 0 0 0 0.5  0 0 0 0 0.5  0 0 0 0.06 0"/>
</filter>"""


def card_rect(w, h, rx=10, x=0, y=0):
    return (
        f'<rect x="{x + 0.5}" y="{y + 0.5}" width="{w - 1}" height="{h - 1}" rx="{rx}" class="card"/>'
        f'<rect x="{x + 0.5}" y="{y + 0.5}" width="{w - 1}" height="{h - 1}" rx="{rx}" filter="url(#grain)"/>'
    )


def svg_doc(width, height, aria_label, style_extra, body, defs_extra="", card=True):
    card_markup = card_rect(width, height) if card else ""
    return f"""<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{escape_xml(aria_label)}">
<style>
{CSS_ROOT}
{style_extra}
</style>
<defs>
{GRAIN_FILTER}
{defs_extra}
</defs>
{card_markup}
{body}
</svg>"""


def desaturate_hex(hex_color, factor=0.4):
    if not hex_color:
        return None
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return None
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    s *= factor
    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return "#{:02x}{:02x}{:02x}".format(round(r2 * 255), round(g2 * 255), round(b2 * 255))


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _graphql_request(query, token, username, timeout=30):
    body = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        GITHUB_GRAPHQL_URL,
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"{username}-profile-generator",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as e:
        raise FetchError(f"GitHub API request failed: {e}") from e


def fetch_github_data(username, token):
    query = GRAPHQL_QUERY.replace("query($login: String!)", "query").replace(
        "user(login: $login)", f"user(login: {json.dumps(username)})"
    )
    payload = _graphql_request(query, token, username)
    if payload.get("errors"):
        raise FetchError(f"GitHub API returned errors: {payload['errors']}")
    data = payload.get("data", {}).get("user")
    if not data:
        raise FetchError("GitHub API returned no user data")
    return data


def fetch_commit_history(username, token, repo_names):
    if not repo_names:
        return {}
    parts = []
    for i, name in enumerate(repo_names):
        parts.append(f"""
  r{i}: repository(owner: {json.dumps(username)}, name: {json.dumps(name)}) {{
    defaultBranchRef {{
      target {{
        ... on Commit {{
          history(first: 100) {{
            nodes {{ committedDate author {{ user {{ login }} }} }}
          }}
        }}
      }}
    }}
  }}""")
    query = "query {" + "".join(parts) + "\n}"
    payload = _graphql_request(query, token, username)
    if payload.get("errors") and not payload.get("data"):
        raise FetchError(f"GitHub API returned errors: {payload['errors']}")
    return payload.get("data") or {}


def fetch_icon_path(slug):
    cache = {}
    if os.path.exists(ICON_CACHE_PATH):
        try:
            with open(ICON_CACHE_PATH, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            cache = {}
    if slug in cache:
        return cache[slug]

    url = f"https://cdn.jsdelivr.net/npm/simple-icons@{SIMPLE_ICONS_VERSION}/icons/{slug}.svg"
    path_d = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "profile-generator"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            svg_text = resp.read().decode("utf-8")
        match = re.search(r'<path\s+d="([^"]+)"', svg_text)
        if match:
            path_d = match.group(1)
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError):
        path_d = None

    cache[slug] = path_d
    try:
        os.makedirs(os.path.dirname(ICON_CACHE_PATH), exist_ok=True)
        with open(ICON_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
    except OSError:
        pass
    return path_d


# ---------------------------------------------------------------------------
# Derive
# ---------------------------------------------------------------------------

def derive_contribution_days(contrib_collection):
    weeks = contrib_collection["contributionCalendar"]["weeks"]
    days = [
        {"date": d["date"], "count": d["contributionCount"]}
        for w in weeks
        for d in w["contributionDays"]
    ]
    days.sort(key=lambda d: d["date"])
    return days


def derive_weekly_totals(contrib_collection):
    weeks = contrib_collection["contributionCalendar"]["weeks"]
    return [sum(d["contributionCount"] for d in w["contributionDays"]) for w in weeks]


def derive_streaks(days):
    if not days:
        return {"current": 0, "longest": 0, "peak": 0}

    by_date = {datetime.strptime(d["date"], "%Y-%m-%d").date(): d["count"] for d in days}
    today = datetime.now(IST).date()

    cursor = today if by_date.get(today, 0) > 0 else today - timedelta(days=1)
    current = 0
    while by_date.get(cursor, 0) > 0:
        current += 1
        cursor -= timedelta(days=1)

    longest = 0
    running = 0
    for d in days:
        if d["count"] > 0:
            running += 1
            longest = max(longest, running)
        else:
            running = 0

    peak = max(d["count"] for d in days)
    return {"current": current, "longest": longest, "peak": peak}


def derive_weekday_totals(days):
    totals = [0] * 7
    for d in days:
        dt = datetime.strptime(d["date"], "%Y-%m-%d").date()
        totals[dt.weekday()] += d["count"]
    return totals


def derive_languages(repos, exclude_names):
    totals = {}
    for repo in repos:
        if repo["name"] in exclude_names:
            continue
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            totals[name] = totals.get(name, 0) + edge["size"]

    total_size = sum(totals.values())
    if total_size == 0:
        return []
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return [{"name": name, "pct": round(size / total_size * 100, 1)} for name, size in ranked]


def derive_overview(repos, exclude_names, followers_total):
    counted = [r for r in repos if r["name"] not in exclude_names]
    return {
        "repos": len(counted),
        "stars": sum(r["stargazerCount"] for r in counted),
        "forks": sum(r["forkCount"] for r in counted),
        "followers": followers_total,
    }


def derive_builds(repos, exclude_names, count):
    eligible = [r for r in repos if r["name"] not in exclude_names and not r["isPrivate"]]
    return eligible[:count]


def derive_commit_hours(history_data, username):
    hours = [0] * 24
    for repo in (history_data or {}).values():
        if not repo or not repo.get("defaultBranchRef"):
            continue
        target = repo["defaultBranchRef"].get("target") or {}
        nodes = (target.get("history") or {}).get("nodes") or []
        for commit in nodes:
            author = (commit.get("author") or {}).get("user") or {}
            if author.get("login") != username:
                continue
            dt = datetime.strptime(commit["committedDate"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            hours[dt.astimezone(IST).hour] += 1
    return hours


# ---------------------------------------------------------------------------
# Small render helpers
# ---------------------------------------------------------------------------

def catmull_rom_to_bezier_path(points):
    if len(points) < 2:
        return ""
    p = points
    d = f"M {p[0][0]:.2f},{p[0][1]:.2f} "
    for i in range(len(p) - 1):
        p0 = p[i - 1] if i - 1 >= 0 else p[i]
        p1 = p[i]
        p2 = p[i + 1]
        p3 = p[i + 2] if i + 2 < len(p) else p2
        c1x = p1[0] + (p2[0] - p0[0]) / 6
        c1y = p1[1] + (p2[1] - p0[1]) / 6
        c2x = p2[0] - (p3[0] - p1[0]) / 6
        c2y = p2[1] - (p3[1] - p1[1]) / 6
        d += f"C {c1x:.2f},{c1y:.2f} {c2x:.2f},{c2y:.2f} {p2[0]:.2f},{p2[1]:.2f} "
    return d.strip()


def fmt_hour_12(h):
    period = "AM" if h < 12 else "PM"
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    return f"{h12} {period}"


def peak_label(hour):
    if 0 <= hour <= 4 or hour == 23:
        return "night owl"
    if 5 <= hour <= 8:
        return "early bird"
    if 9 <= hour <= 16:
        return "daytime builder"
    return "evening coder"


# ---------------------------------------------------------------------------
# Render: terminal.svg
# ---------------------------------------------------------------------------

def render_terminal(config, building_line):
    w, h = WIDTH, 170
    pad = 24
    first_name = config["name"].split()[0].lower() if config.get("name") else config["username"]

    lines = [
        ("prompt", "$ whoami"),
        ("output", f"{first_name} — {config['tagline']}"),
        ("prompt", "$ cat now.txt"),
        ("output", building_line),
        ("prompt", "$ ls hackathons/ | wc -l"),
        ("output", str(config.get("hackathon_count", 10))),
    ]

    max_chars = int((w - pad * 2) / (12 * MONO_CHAR_W_RATIO))
    lines = [(kind, (text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…")) for kind, text in lines]

    font_size = 12
    char_w = font_size * MONO_CHAR_W_RATIO
    line_h = 19
    top = 48

    char_dur = 0.045
    line_gap = 0.25
    cum = 0.0
    timings = []
    for _, text in lines:
        dur = max(len(text), 1) * char_dur
        timings.append({"delay": cum, "dur": dur, "end": cum + dur})
        cum += dur + line_gap
    total_dur = timings[-1]["end"] + 1.0

    defs = []
    body = []
    for i, ((kind, text), t) in enumerate(zip(lines, timings)):
        y = top + i * line_h
        cls = "term-prompt" if kind == "prompt" else "term-output"
        full_w = max(len(text), 1) * char_w
        defs.append(
            f"@keyframes type{i}{{from{{width:0;}}to{{width:{full_w:.2f}px;}}}}"
        )
        body.append(
            f'<clipPath id="clip{i}"><rect class="typeclip" id="rect{i}" x="{pad}" y="{y - font_size}" '
            f'width="{full_w:.2f}" height="{line_h}" style="animation-delay:{t["delay"]:.3f}s"/></clipPath>'
            f'<g clip-path="url(#clip{i})"><text x="{pad}" y="{y}" class="{cls} mono">{escape_xml(text)}</text></g>'
        )

    # Per-line reveal animation, gated behind reduced-motion.
    reveal_rules = "\n".join(
        f'#rect{i}{{animation:type{i} {t["dur"]:.3f}s steps({max(len(text), 1)}) {t["delay"]:.3f}s forwards;}}'
        for i, ((kind, text), t) in enumerate(zip(lines, timings))
    )

    cursor_x = pad + max(len(lines[-1][1]), 1) * char_w
    cursor_y_top = top + (len(lines) - 1) * line_h - font_size + 2
    cursor_positions = []
    for i, t in enumerate(timings):
        pct = min(99.9, (t["end"] / total_dur) * 100)
        end_x = pad + max(len(lines[i][1]), 1) * char_w
        end_y = top + i * line_h - font_size + 2
        cursor_positions.append(f"{pct:.2f}% {{ transform: translate({end_x:.2f}px,{end_y:.2f}px); }}")
    cursor_positions.append(f"100% {{ transform: translate({cursor_x:.2f}px,{cursor_y_top:.2f}px); }}")

    style_extra = f"""
.term-dot {{ fill: var(--muted); }}
.term-dot.d2 {{ fill: var(--accent-soft); }}
.term-dot.d3 {{ fill: var(--border); }}
.term-bar {{ stroke: var(--border); stroke-width: 1; }}
.term-prompt {{ font-size: {font_size}px; fill: var(--muted); }}
.term-output {{ font-size: {font_size}px; fill: var(--text); }}
.cursor {{ fill: var(--text); opacity: 0; }}
{"".join(defs)}
@media (prefers-reduced-motion: no-preference) {{
  .typeclip {{ width: 0; }}
  {reveal_rules}
  .cursor {{
    animation-name: cursor-pos, cursor-blink;
    animation-duration: {total_dur:.3f}s, 1s;
    animation-timing-function: steps(1), steps(2);
    animation-iteration-count: 1, infinite;
    animation-fill-mode: forwards, none;
    animation-delay: 0s, {timings[0]["end"]:.3f}s;
  }}
}}
@keyframes cursor-pos {{ {" ".join(cursor_positions)} }}
@keyframes cursor-blink {{ 0% {{ opacity: 1; }} 50% {{ opacity: 0; }} 100% {{ opacity: 1; }} }}
"""

    chrome = (
        f'<line x1="0.5" y1="32.5" x2="{w - 0.5}" y2="32.5" class="term-bar"/>'
        f'<circle cx="{pad - 8}" cy="16" r="4" class="term-dot d1"/>'
        f'<circle cx="{pad + 8}" cy="16" r="4" class="term-dot d2"/>'
        f'<circle cx="{pad + 24}" cy="16" r="4" class="term-dot d3"/>'
    )
    cursor = f'<rect class="cursor" x="0" y="0" width="7" height="{font_size + 2}" transform="translate({pad:.2f},{top - font_size + 2:.2f})"/>'

    return svg_doc(w, h, "Terminal: a quick introduction", style_extra, chrome + "".join(body) + cursor)


# ---------------------------------------------------------------------------
# Render: header.svg
# ---------------------------------------------------------------------------

def render_header(config, weekly_totals, total_contributions):
    w, h = WIDTH, 110
    pad = 24
    chart_top, chart_bottom = 46, h - 20
    chart_left, chart_right = pad, w - pad

    values = weekly_totals or [0]
    vmax = max(values) or 1
    n = len(values)
    points = [
        (
            chart_left + (chart_right - chart_left) * (i / max(n - 1, 1)),
            chart_bottom - (chart_bottom - chart_top) * (v / vmax),
        )
        for i, v in enumerate(values)
    ]

    line_path = catmull_rom_to_bezier_path(points)
    area_path = (
        f"{line_path} L {points[-1][0]:.2f},{chart_bottom:.2f} "
        f"L {points[0][0]:.2f},{chart_bottom:.2f} Z"
    )
    last_x, last_y = points[-1]
    tagline = escape_xml(config["tagline"])

    style_extra = """
.tagline { font-size: 15px; fill: var(--text); }
.total { font-size: 11px; fill: var(--muted); }
.line { fill: none; stroke: var(--accent); stroke-width: 1.5; stroke-dasharray: 1600; stroke-dashoffset: 1600; }
.area { fill: url(#sparkGrad); }
.dot { fill: var(--accent); }
@media (prefers-reduced-motion: no-preference) {
  .line { animation: draw 2.2s ease-out forwards; }
  .dot { animation: pulse 2.4s ease-in-out infinite; }
}
@keyframes draw { to { stroke-dashoffset: 0; } }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
"""
    defs_extra = """<linearGradient id="sparkGrad" x1="0" y1="0" x2="0" y2="1">
  <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.18"/>
  <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
</linearGradient>"""

    body = (
        f'<text x="{pad}" y="30" class="tagline sans">{tagline}</text>'
        f'<text x="{pad}" y="{h - 8}" class="label sans">Contributions, last 52 weeks</text>'
        f'<text x="{w - pad}" y="{h - 8}" text-anchor="end" class="total mono">{total_contributions} total</text>'
        f'<path d="{area_path}" class="area"/>'
        f'<path d="{line_path}" class="line"/>'
        f'<circle cx="{last_x:.2f}" cy="{last_y:.2f}" r="3" class="dot"/>'
    )

    return svg_doc(w, h, config["tagline"], style_extra, body, defs_extra)


# ---------------------------------------------------------------------------
# Render: stack.svg
# ---------------------------------------------------------------------------

def render_stack(config):
    items = config["stack"]
    pad = 24
    base_gap, base_icon, base_font, base_char_w = 18, 12, 11, 6.2
    available = WIDTH - pad * 2

    def row_width(gap, icon_size, char_w):
        widths = [icon_size + 6 + len(name) * char_w for name in items]
        return sum(widths) + gap * (len(items) - 1), widths

    total_w, _ = row_width(base_gap, base_icon, base_char_w)
    scale = min(1.0, available / total_w) if total_w else 1.0

    gap, icon_size, font_size, char_w = (
        base_gap * scale,
        base_icon * scale,
        base_font * scale,
        base_char_w * scale,
    )
    total_w, widths = row_width(gap, icon_size, char_w)
    x = (WIDTH - total_w) / 2

    elements = []
    for name, chip_w in zip(items, widths):
        slug = ICON_SLUG_OVERRIDES.get(name, name.lower().replace(" ", "").replace(".", ""))
        path_d = fetch_icon_path(slug)
        icon_x, icon_y = x, 22 - icon_size / 2
        if path_d:
            icon_scale = icon_size / 24
            elements.append(
                f'<g transform="translate({icon_x:.2f},{icon_y:.2f}) scale({icon_scale:.4f})">'
                f'<path d="{path_d}" class="icon"/></g>'
            )
        else:
            elements.append(f'<circle cx="{icon_x + icon_size / 2:.2f}" cy="22" r="3" class="icon-dot"/>')
        text_x = icon_x + icon_size + 6
        elements.append(f'<text x="{text_x:.2f}" y="26" class="chip-label sans">{escape_xml(name)}</text>')
        x += chip_w + gap

    style_extra = f"""
.icon, .icon-dot {{ fill: var(--muted); }}
.chip-label {{ font-size: {font_size:.2f}px; fill: var(--muted); }}
"""
    return svg_doc(WIDTH, 44, "Tech stack", style_extra, "".join(elements), card=False)


# ---------------------------------------------------------------------------
# Render: field.svg
# ---------------------------------------------------------------------------

def render_field(active_days_60, total_contributions):
    w, h = WIDTH, 260
    pad = 24
    n = max(24, min(80, active_days_60))
    rng = random.Random(total_contributions or 1)

    points = [
        {
            "x": rng.uniform(pad, w - pad),
            "y": rng.uniform(pad, h - pad - 24),
            "r": rng.uniform(1, 3.5),
        }
        for _ in range(n)
    ]
    bright_count = max(1, round(n * 0.15))
    bright_idx = set(rng.sample(range(n), bright_count))

    lines = []
    for i in range(n):
        for j in range(i + 1, n):
            dx = points[i]["x"] - points[j]["x"]
            dy = points[i]["y"] - points[j]["y"]
            dist = math.hypot(dx, dy)
            if dist < 110:
                opacity = max(0.04, 0.22 * (1 - dist / 110))
                lines.append(
                    f'<line x1="{points[i]["x"]:.2f}" y1="{points[i]["y"]:.2f}" '
                    f'x2="{points[j]["x"]:.2f}" y2="{points[j]["y"]:.2f}" '
                    f'class="field-line" opacity="{opacity:.3f}"/>'
                )

    dots = []
    for idx, p in enumerate(points):
        cls = "pt-bright" if idx in bright_idx else "pt-soft"
        dur = rng.uniform(12, 30)
        delay = rng.uniform(0, dur)
        dx1, dy1 = rng.uniform(-6, 6), rng.uniform(-6, 6)
        twinkle_anim = ""
        if rng.random() < 0.2:
            tdur = rng.uniform(3, 6)
            twinkle_anim = (
                f'<animate attributeName="opacity" values="1;0.3;1" '
                f'dur="{tdur:.1f}s" begin="-{rng.uniform(0, tdur):.1f}s" repeatCount="indefinite"/>'
            )
        dots.append(
            f'<g><circle cx="{p["x"]:.2f}" cy="{p["y"]:.2f}" r="{p["r"]:.2f}" class="{cls}">{twinkle_anim}</circle>'
            f'<animateTransform attributeName="transform" type="translate" '
            f'values="0,0; {dx1:.2f},{dy1:.2f}; 0,0" dur="{dur:.1f}s" begin="-{delay:.1f}s" '
            f'repeatCount="indefinite"/></g>'
        )

    caption = f"particle field · {n} points · seeded by {total_contributions} contributions"

    style_extra = """
.field-line { stroke: var(--accent-soft); stroke-width: 0.6; }
.pt-bright { fill: var(--accent); }
.pt-soft { fill: var(--accent-soft); opacity: 0.7; }
.caption { font-size: 10px; fill: var(--muted); }
"""
    body = (
        f'<g>{"".join(lines)}</g>'
        f'<g>{"".join(dots)}</g>'
        f'<text x="{pad}" y="{h - 14}" class="caption sans">{caption}</text>'
    )
    return svg_doc(w, h, "Particle field generated from contribution data", style_extra, body)


# ---------------------------------------------------------------------------
# Render: stats.svg (donut language ring)
# ---------------------------------------------------------------------------

def render_stats(overview, languages, streaks):
    w, h = WIDTH, 190
    card_w, gap, pad = 272, 12, 20

    def card_x(i):
        return i * (card_w + gap)

    rows1 = [
        ("Repositories", overview["repos"]),
        ("Stars", overview["stars"]),
        ("Forks", overview["forks"]),
        ("Followers", overview["followers"]),
    ]
    row_h, row_start = 30, 56
    overview_rows = []
    for i, (label, value) in enumerate(rows1):
        y = row_start + i * row_h
        if i > 0:
            overview_rows.append(f'<line x1="{pad}" y1="{y - row_h + 12}" x2="{card_w - pad}" y2="{y - row_h + 12}" class="sep"/>')
        overview_rows.append(f'<text x="{pad}" y="{y}" class="label sans">{escape_xml(label)}</text>')
        overview_rows.append(f'<text x="{card_w - pad}" y="{y}" text-anchor="end" class="value mono row-value">{value}</text>')

    # Donut ring: one stroke segment per language, drawn via a dedicated
    # @keyframes per segment since each segment's dasharray target differs.
    cx, cy, r = 70, 118, 42
    stroke_w = 12
    circumference = 2 * math.pi * r
    opacities = [0.92, 0.72, 0.55, 0.4, 0.26]
    donut_defs = []
    donut_segs = []
    legend_rows = []
    cum = 0.0
    for i, lang in enumerate(languages[:5]):
        seg_len = circumference * lang["pct"] / 100
        donut_defs.append(
            f"@keyframes donutseg{i}{{from{{stroke-dasharray:0 {circumference:.2f};}}"
            f"to{{stroke-dasharray:{seg_len:.2f} {circumference - seg_len:.2f};}}}}"
        )
        donut_segs.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" class="donut-seg donut-seg-{i}" '
            f'stroke-width="{stroke_w}" fill="none" stroke="var(--text)" opacity="{opacities[i]}" '
            f'stroke-dasharray="{seg_len:.2f} {circumference - seg_len:.2f}" '
            f'stroke-dashoffset="{-cum:.2f}" style="animation-delay:{i * 0.1:.2f}s"/>'
        )
        cum += seg_len

        ly = 46 + i * 24
        lang_name = lang["name"]
        if len(lang_name) > 8:
            lang_name = lang_name[:7].rstrip() + "…"
        legend_rows.append(
            f'<circle cx="{150}" cy="{ly - 4}" r="4" fill="var(--text)" opacity="{opacities[i]}"/>'
            f'<text x="162" y="{ly}" class="label sans">{escape_xml(lang_name)}</text>'
            f'<text x="{card_w - pad}" y="{ly}" text-anchor="end" class="value-muted mono" style="font-size:11px">{lang["pct"]:.1f}%</text>'
        )

    donut_rules = "\n".join(
        f'.donut-seg-{i}{{animation:donutseg{i} 0.8s ease-out both;}}' for i in range(len(languages[:5]))
    )

    streak_body = f"""
<text x="{card_w / 2}" y="92" text-anchor="middle" class="value mono" style="font-size:40px;font-weight:300">{streaks['current']}</text>
<text x="{card_w / 2}" y="112" text-anchor="middle" class="label sans">days in a row</text>
<text x="{pad}" y="{h - 30}" class="label-sm sans">Longest {streaks['longest']}</text>
<text x="{card_w - pad}" y="{h - 30}" text-anchor="end" class="label-sm sans">Peak {streaks['peak']} a day</text>
"""

    def wrap_card(x, title, body):
        return (
            f'<g transform="translate({x},0)">'
            f'{card_rect(card_w, h)}'
            f'<text x="{pad}" y="32" class="title sans">{title}</text>{body}</g>'
        )

    donut_body = f'<g transform="rotate(-90 {cx} {cy})">{"".join(donut_segs)}</g>' + "".join(legend_rows)

    cards = (
        wrap_card(card_x(0), "Overview", "".join(overview_rows))
        + wrap_card(card_x(1), "Languages", donut_body)
        + wrap_card(card_x(2), "Streak", streak_body)
    )

    style_extra = f"""
.row-value {{ font-size: 18px; }}
@media (prefers-reduced-motion: no-preference) {{
  {donut_rules}
}}
{"".join(donut_defs)}
"""

    return svg_doc(w, h, "Repository, language and streak stats", style_extra, cards, card=False)


# ---------------------------------------------------------------------------
# Render: clock.svg
# ---------------------------------------------------------------------------

def render_clock(hours, weekday_totals):
    w, h = WIDTH, 260
    pad_outer, gap, pad = 24, 20, 20
    card_w = (w - pad_outer * 2 - gap) / 2

    # Left: 24-hour radial chart of commit times.
    total_commits = sum(hours)
    peak_hour = hours.index(max(hours)) if total_commits else None
    cx, cy = card_w / 2, 132
    r_inner, r_max = 16, 78
    maxv = max(hours) or 1

    spokes = []
    for hh in range(24):
        angle = (hh / 24) * 2 * math.pi - math.pi / 2
        frac = hours[hh] / maxv
        r1, r2 = r_inner, r_inner + frac * (r_max - r_inner)
        x1, y1 = cx + r1 * math.cos(angle), cy + r1 * math.sin(angle)
        x2, y2 = cx + r2 * math.cos(angle), cy + r2 * math.sin(angle)
        seg_len = r2 - r1
        cls = "spoke-peak" if hh == peak_hour else "spoke"
        delay = hh * 0.02
        spokes.append(
            f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" class="{cls} grow-spoke" '
            f'style="--len:{seg_len:.2f};stroke-dasharray:{seg_len:.2f};animation-delay:{delay:.2f}s"/>'
        )

    ticks = []
    for label, hr in [("12A", 0), ("6A", 6), ("12P", 12), ("6P", 18)]:
        angle = (hr / 24) * 2 * math.pi - math.pi / 2
        tx = cx + (r_max + 16) * math.cos(angle)
        ty = cy + (r_max + 16) * math.sin(angle) + 3
        ticks.append(f'<text x="{tx:.2f}" y="{ty:.2f}" text-anchor="middle" class="label-sm sans">{label}</text>')

    if total_commits:
        clock_caption = f"{peak_label(peak_hour)} — peak at {fmt_hour_12(peak_hour)}"
    else:
        clock_caption = "not enough commit data yet"

    left_body = (
        f'<text x="{pad}" y="32" class="title sans">When I commit</text>'
        f'<circle cx="{cx}" cy="{cy}" r="{r_max}" class="ring" fill="none"/>'
        f'{"".join(spokes)}{"".join(ticks)}'
        f'<text x="{card_w / 2}" y="{h - 10}" text-anchor="middle" class="label sans">{escape_xml(clock_caption)}</text>'
    )

    # Right: Mon-Sun bar chart, busiest day highlighted.
    busiest_idx = weekday_totals.index(max(weekday_totals)) if any(weekday_totals) else None
    bar_pad = 20
    bar_area_w = card_w - bar_pad * 2
    bar_gap = 10
    bar_w = (bar_area_w - bar_gap * 6) / 7
    baseline = 200
    max_bar_h = 130
    wk_max = max(weekday_totals) or 1

    bars = []
    for i, total in enumerate(weekday_totals):
        bh = (total / wk_max) * max_bar_h
        x = bar_pad + i * (bar_w + bar_gap)
        y = baseline - bh
        cls = "wk-bar-peak" if i == busiest_idx else "wk-bar"
        delay = i * 0.06
        bars.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" height="{max(bh, 1):.2f}" rx="2" '
            f'class="{cls} grow-bar" style="animation-delay:{delay:.2f}s"/>'
        )
        bars.append(
            f'<text x="{x + bar_w / 2:.2f}" y="{baseline + 16}" text-anchor="middle" class="label-sm sans">{WEEKDAY_NAMES[i]}</text>'
        )
        if i == busiest_idx:
            bars.append(
                f'<text x="{x + bar_w / 2:.2f}" y="{y - 6:.2f}" text-anchor="middle" class="label sans">{total}</text>'
            )

    busiest_label = f"busiest: {WEEKDAY_NAMES[busiest_idx]}" if busiest_idx is not None else ""
    right_body = (
        f'<text x="{pad}" y="32" class="title sans">Weekday rhythm</text>'
        f'<text x="{card_w - pad}" y="32" text-anchor="end" class="label sans">{escape_xml(busiest_label)}</text>'
        f'{"".join(bars)}'
    )

    cards = (
        f'<g transform="translate(0,0)">{card_rect(card_w, h)}{left_body}</g>'
        f'<g transform="translate({card_w + gap},0)">{card_rect(card_w, h)}{right_body}</g>'
    )

    style_extra = """
.ring { stroke: var(--border); stroke-width: 1; }
.spoke { stroke: var(--accent-soft); stroke-width: 2.5; stroke-linecap: round; }
.spoke-peak { stroke: var(--text); stroke-width: 2.5; stroke-linecap: round; }
.wk-bar { fill: var(--accent-soft); }
.wk-bar-peak { fill: var(--text); }
@media (prefers-reduced-motion: no-preference) {
  .grow-spoke { stroke-dashoffset: var(--len); animation: spokegrow 0.5s ease-out forwards; }
  .grow-bar { transform-box: fill-box; transform-origin: bottom; animation: bargrow 0.5s ease-out both; }
}
@keyframes spokegrow { from { stroke-dashoffset: 0; } to { stroke-dashoffset: var(--len); } }
@keyframes bargrow { from { transform: scaleY(0); } to { transform: scaleY(1); } }
"""
    return svg_doc(w, h, "When and how often I commit", style_extra, cards, card=False)


# ---------------------------------------------------------------------------
# Render: skyline.svg
# ---------------------------------------------------------------------------

def render_skyline(weekly_totals, year):
    w, h = WIDTH, 220
    pad = 24
    baseline = 170
    max_tower_h = 120
    ddx, ddy = 5, -3

    values = weekly_totals or [0]
    vmax = max(values) or 1
    n = len(values)
    avail_w = w - pad * 2
    spacing = avail_w / n
    tower_w = max(2, spacing * 0.6)

    towers = []
    for i, v in enumerate(values):
        bx = pad + i * spacing
        th = max(2, (v / vmax) * max_tower_h)
        top_y = baseline - th
        delay = i * 0.012
        front = f'<rect x="{bx:.2f}" y="{top_y:.2f}" width="{tower_w:.2f}" height="{th:.2f}" class="tower-front"/>'
        right = (
            f'<polygon points="{bx + tower_w:.2f},{top_y:.2f} {bx + tower_w + ddx:.2f},{top_y + ddy:.2f} '
            f'{bx + tower_w + ddx:.2f},{baseline + ddy:.2f} {bx + tower_w:.2f},{baseline:.2f}" class="tower-right"/>'
        )
        top = (
            f'<polygon points="{bx:.2f},{top_y:.2f} {bx + tower_w:.2f},{top_y:.2f} '
            f'{bx + tower_w + ddx:.2f},{top_y + ddy:.2f} {bx + ddx:.2f},{top_y + ddy:.2f}" class="tower-top"/>'
        )
        towers.append(
            f'<g class="tower" style="animation-delay:{delay:.3f}s">{front}{right}{top}</g>'
        )

    caption = f"{year} in commits"
    body = (
        f'<text x="{pad}" y="32" class="title sans">Skyline</text>'
        f'{"".join(towers)}'
        f'<text x="{pad}" y="{h - 14}" class="label sans">{escape_xml(caption)}</text>'
    )
    style_extra = """
.tower-top { fill: var(--text); opacity: 0.85; }
.tower-right { fill: var(--text); opacity: 0.5; }
.tower-front { fill: var(--text); opacity: 0.28; }
@media (prefers-reduced-motion: no-preference) {
  .tower { transform-box: fill-box; transform-origin: bottom; animation: rise 0.5s ease-out both; }
}
@keyframes rise { from { transform: scaleY(0); } to { transform: scaleY(1); } }
"""
    return svg_doc(w, h, f"Isometric skyline of weekly contributions, {year}", style_extra, body)


# ---------------------------------------------------------------------------
# Render: activity.svg
# ---------------------------------------------------------------------------

def render_activity(days, streaks):
    w, h = WIDTH, 190
    pad = 24
    cell, gap = 10, 3
    grid_top, grid_left = 56, pad + 24

    weeks, chunk = [], []
    for d in days:
        chunk.append(d)
        if len(chunk) == 7:
            weeks.append(chunk)
            chunk = []
    if chunk:
        weeks.append(chunk)
    weeks = weeks[-53:]

    nonzero = sorted(d["count"] for week in weeks for d in week if d["count"] > 0)

    def level_for(count):
        if count == 0 or not nonzero:
            return 0
        frac = bisect.bisect_right(nonzero, count) / len(nonzero)
        if frac <= 0.25:
            return 1
        if frac <= 0.5:
            return 2
        if frac <= 0.75:
            return 3
        return 4

    cells, month_labels, last_month, last_label_x = [], [], None, -999
    for col, week in enumerate(weeks):
        x = grid_left + col * (cell + gap)
        if week:
            month = datetime.strptime(week[0]["date"], "%Y-%m-%d").strftime("%b")
            if month != last_month and x - last_label_x >= 24:
                month_labels.append(f'<text x="{x}" y="{grid_top - 10}" class="label-sm sans">{month}</text>')
                last_month = month
                last_label_x = x
            elif month != last_month:
                last_month = month
        delay = col * 8
        for row, d in enumerate(week):
            y = grid_top + row * (cell + gap)
            lvl = level_for(d["count"])
            cells.append(
                f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2" class="heat-{lvl} fade" '
                f'style="animation-delay:{delay}ms"/>'
            )

    day_labels = (
        f'<text x="{pad}" y="{grid_top + 1 * (cell + gap) + 8}" class="label-sm sans">Mon</text>'
        f'<text x="{pad}" y="{grid_top + 3 * (cell + gap) + 8}" class="label-sm sans">Wed</text>'
        f'<text x="{pad}" y="{grid_top + 5 * (cell + gap) + 8}" class="label-sm sans">Fri</text>'
    )

    legend_x, legend_y = w - pad - 120, h - 20
    legend_cells = "".join(
        f'<rect x="{legend_x + 36 + i * 12}" y="{legend_y - 8}" width="8" height="8" rx="2" class="heat-{i}"/>'
        for i in range(5)
    )

    style_extra = """
.heat-0 { fill: var(--heat0); } .heat-1 { fill: var(--heat1); } .heat-2 { fill: var(--heat2); }
.heat-3 { fill: var(--heat3); } .heat-4 { fill: var(--heat4); }
@media (prefers-reduced-motion: no-preference) {
  .fade { animation: fadein 0.25s ease-out both; }
}
@keyframes fadein { from { opacity: 0; } to { opacity: 1; } }
"""
    body = (
        f'<text x="{pad}" y="32" class="title sans">Activity</text>'
        f'<text x="{w - pad}" y="32" text-anchor="end" class="label sans">Longest streak {streaks["longest"]} days, peak {streaks["peak"]} in a day</text>'
        f'{"".join(month_labels)}{day_labels}{"".join(cells)}'
        f'<text x="{legend_x}" y="{legend_y}" class="label-sm sans">Less</text>{legend_cells}'
        f'<text x="{legend_x + 36 + 5 * 12 + 4}" y="{legend_y}" class="label-sm sans">More</text>'
    )
    return svg_doc(w, h, "Contribution heatmap", style_extra, body)


# ---------------------------------------------------------------------------
# Render: builds.svg
# ---------------------------------------------------------------------------

def render_builds(repos):
    w, pad = WIDTH, 24
    n = len(repos)
    cols = 2
    rows = max(1, math.ceil(n / cols))
    row_h, header_h = 64, 50
    h = header_h + rows * row_h + 16
    col_w = (w - pad * 2 - 16) / cols

    now = datetime.now(timezone.utc)
    cells = []
    for i, repo in enumerate(repos):
        col, row = i % cols, i // cols
        x = pad + col * (col_w + 16)
        y = header_h + row * row_h

        desc = repo.get("description") or "No description yet"
        if len(desc) > 70:
            desc = desc[:69].rstrip() + "…"

        pushed = datetime.strptime(repo["pushedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        days_ago = max(0, (now - pushed).days)
        when = "today" if days_ago == 0 else ("yesterday" if days_ago == 1 else f"{days_ago} days ago")

        lang_edges = repo["languages"]["edges"]
        lang_name = lang_edges[0]["node"]["name"] if lang_edges else None
        raw_color = lang_edges[0]["node"].get("color") if lang_edges else None
        lang_color = desaturate_hex(raw_color) or "var(--muted)"

        if lang_name:
            lang_bit = (
                f'<circle cx="{x + 2}" cy="{y + 46}" r="3" fill="{lang_color}"/>'
                f'<text x="{x + 10}" y="{y + 49}" class="label sans">{escape_xml(lang_name)} · updated {when}</text>'
            )
        else:
            lang_bit = f'<text x="{x}" y="{y + 49}" class="label sans">updated {when}</text>'

        cells.append(
            f'<text x="{x}" y="{y + 16}" class="repo-name sans">{escape_xml(repo["name"])}</text>'
            f'<text x="{x}" y="{y + 32}" class="desc sans">{escape_xml(desc)}</text>'
            f"{lang_bit}"
        )

    style_extra = """
.repo-name { font-size: 13px; fill: var(--text); }
.desc { font-size: 11px; fill: var(--muted); }
"""
    body = (
        f'<text x="{pad}" y="32" class="title sans">Recently shipped</text>'
        f'{"".join(cells)}'
    )
    return svg_doc(w, h, "Recently shipped repositories", style_extra, body)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def write(svgs):
    os.makedirs(ASSETS_DIR, exist_ok=True)
    for name, content in svgs.items():
        with open(os.path.join(ASSETS_DIR, f"{name}.svg"), "w", encoding="utf-8") as f:
            f.write(content)

    if os.path.exists(README_PATH):
        with open(README_PATH, "r", encoding="utf-8") as f:
            readme = f.read()
        today = datetime.now(IST).strftime("%Y-%m-%d")
        readme = re.sub(r"(<!--UPDATED-->).*?(<!--/UPDATED-->)", rf"\g<1>{today}\g<2>", readme)
        with open(README_PATH, "w", encoding="utf-8") as f:
            f.write(readme)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_config()
    token = os.environ.get("GH_TOKEN")
    if not token:
        print("ERROR: GH_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    try:
        user_data = fetch_github_data(config["username"], token)
    except FetchError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    exclude = set(config.get("exclude_repos", []))
    repos = user_data["repositories"]["nodes"]
    contrib = user_data["contributionsCollection"]

    try:
        days = derive_contribution_days(contrib)
        weekly_totals = derive_weekly_totals(contrib)
        total_contributions = contrib["contributionCalendar"]["totalContributions"]
        streaks = derive_streaks(days)
        weekday_totals = derive_weekday_totals(days)
        languages = derive_languages(repos, exclude)
        overview = derive_overview(repos, exclude, user_data["followers"]["totalCount"])
        builds = derive_builds(repos, exclude, config.get("builds_count", 4))
        active_days_60 = sum(1 for d in days[-60:] if d["count"] > 0)

        history_repo_names = [r["name"] for r in builds[:10]] or [r["name"] for r in repos[:10] if r["name"] not in exclude]
        try:
            history_data = fetch_commit_history(config["username"], token, history_repo_names)
        except FetchError as e:
            print(f"WARNING: commit history fetch failed, clock.svg will show no data: {e}", file=sys.stderr)
            history_data = {}
        hours = derive_commit_hours(history_data, config["username"])

        if builds:
            top = builds[0]
            desc = top.get("description") or "no description yet"
            building_line = f"building: {top['name']} — {desc}"
        else:
            building_line = "building: nothing public yet"

        year = datetime.now(IST).year

        svgs = {
            "terminal": render_terminal(config, building_line),
            "header": render_header(config, weekly_totals, total_contributions),
            "stack": render_stack(config),
            "field": render_field(active_days_60, total_contributions),
            "stats": render_stats(overview, languages, streaks),
            "clock": render_clock(hours, weekday_totals),
            "skyline": render_skyline(weekly_totals, year),
            "activity": render_activity(days, streaks),
            "builds": render_builds(builds),
        }
    except Exception as e:
        print(f"ERROR: failed to build SVGs: {e}", file=sys.stderr)
        sys.exit(1)

    write(svgs)
    print(f"Wrote {len(svgs)} SVGs to {ASSETS_DIR}")


if __name__ == "__main__":
    main()
