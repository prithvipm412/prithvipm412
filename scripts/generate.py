#!/usr/bin/env python3
"""Fetch live GitHub data and render the profile dashboard SVGs.

Run locally with: GH_TOKEN=xxx python scripts/generate.py
"""

import bisect
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

COLORS = {
    "bg": "#0b1426",
    "border": "#1c2b4a",
    "text": "#e6f1ff",
    "muted": "#7d8bb0",
    "accent": "#7c9cff",
    "accent_soft": "#3d5291",
}
HEAT = ["#121e36", "#24386a", "#3d5aa8", "#6584e0", "#a5b8ff"]
HEAT_LIGHT = ["#e7edf9", "#c3d0f2", "#8ea6e8", "#5878d6", "#2f4fb5"]

ICON_SLUG_OVERRIDES = {
    "Godot": "godotengine",
    "Next.js": "nextdotjs",
    "Tailwind": "tailwindcss",
}

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
# Fetch
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_github_data(username, token):
    body = json.dumps({"query": GRAPHQL_QUERY, "variables": {"login": username}}).encode("utf-8")
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as e:
        raise FetchError(f"GitHub API request failed: {e}") from e

    if payload.get("errors"):
        raise FetchError(f"GitHub API returned errors: {payload['errors']}")
    data = payload.get("data", {}).get("user")
    if not data:
        raise FetchError("GitHub API returned no user data")
    return data


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


# ---------------------------------------------------------------------------
# Render helpers
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

    return f"""<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{tagline}">
<style>
  .card{{fill:{COLORS['bg']};stroke:{COLORS['border']};stroke-width:1;}}
  .tagline{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:15px;fill:{COLORS['text']};}}
  .label{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:11px;fill:{COLORS['muted']};}}
  .total{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;fill:{COLORS['muted']};}}
  .line{{fill:none;stroke:{COLORS['accent']};stroke-width:1.5;stroke-dasharray:1600;stroke-dashoffset:1600;}}
  .area{{fill:url(#sparkGrad);}}
  .dot{{fill:{COLORS['accent']};}}
  @media (prefers-color-scheme: light) {{
    .card{{fill:#f5f8ff;stroke:#d6e0f5;}}
    .tagline{{fill:#0b1426;}}
    .label,.total{{fill:#5a6785;}}
    .line{{stroke:#3b5bdb;}}
    .dot{{fill:#3b5bdb;}}
  }}
  @media (prefers-reduced-motion: no-preference) {{
    .line{{animation:draw 2.2s ease-out forwards;}}
    .dot{{animation:pulse 2.4s ease-in-out infinite;}}
  }}
  @keyframes draw{{to{{stroke-dashoffset:0;}}}}
  @keyframes pulse{{0%,100%{{opacity:1;}}50%{{opacity:0.4;}}}}
</style>
<defs>
  <linearGradient id="sparkGrad" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="{COLORS['accent']}" stop-opacity="0.18"/>
    <stop offset="100%" stop-color="{COLORS['accent']}" stop-opacity="0"/>
  </linearGradient>
</defs>
<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="10" class="card"/>
<text x="{pad}" y="30" class="tagline">{tagline}</text>
<text x="{pad}" y="{h - 8}" class="label">Contributions, last 52 weeks</text>
<text x="{w - pad}" y="{h - 8}" text-anchor="end" class="total">{total_contributions} total</text>
<path d="{area_path}" class="area"/>
<path d="{line_path}" class="line"/>
<circle cx="{last_x:.2f}" cy="{last_y:.2f}" r="3" class="dot"/>
</svg>"""


# ---------------------------------------------------------------------------
# Render: stack.svg
# ---------------------------------------------------------------------------

def render_stack(config):
    items = config["stack"]
    gap, icon_size, font_size = 18, 12, 11

    chips = []
    for name in items:
        slug = ICON_SLUG_OVERRIDES.get(name, name.lower().replace(" ", "").replace(".", ""))
        path_d = fetch_icon_path(slug)
        text_w = len(name) * 6.2
        chips.append({"name": name, "path_d": path_d, "chip_w": icon_size + 6 + text_w})

    total_w = sum(c["chip_w"] for c in chips) + gap * (len(chips) - 1)
    x = (WIDTH - total_w) / 2

    elements = []
    for c in chips:
        icon_x, icon_y = x, 22 - icon_size / 2
        if c["path_d"]:
            scale = icon_size / 24
            elements.append(
                f'<g transform="translate({icon_x:.2f},{icon_y:.2f}) scale({scale:.4f})">'
                f'<path d="{c["path_d"]}" class="icon"/></g>'
            )
        else:
            elements.append(f'<circle cx="{icon_x + icon_size / 2:.2f}" cy="22" r="3" class="icon-dot"/>')
        text_x = icon_x + icon_size + 6
        elements.append(f'<text x="{text_x:.2f}" y="26" class="chip-label">{escape_xml(c["name"])}</text>')
        x += c["chip_w"] + gap

    return f"""<svg width="{WIDTH}" height="44" viewBox="0 0 {WIDTH} 44" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Tech stack">
<style>
  .icon,.icon-dot{{fill:{COLORS['muted']};}}
  .chip-label{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:{font_size}px;fill:{COLORS['muted']};}}
  @media (prefers-color-scheme: light) {{
    .icon,.icon-dot,.chip-label{{fill:#5a6785;}}
  }}
</style>
{"".join(elements)}
</svg>"""


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

    # Drift/twinkle use SMIL so each point gets an independent random phase and
    # duration; CSS keyframes can't express that without one @keyframes per point.
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

    return f"""<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Particle field generated from contribution data">
<style>
  .card{{fill:{COLORS['bg']};stroke:{COLORS['border']};stroke-width:1;}}
  .field-line{{stroke:{COLORS['accent_soft']};stroke-width:0.6;}}
  .pt-bright{{fill:{COLORS['accent']};}}
  .pt-soft{{fill:{COLORS['accent_soft']};opacity:0.7;}}
  .caption{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:10px;fill:{COLORS['muted']};}}
  @media (prefers-color-scheme: light) {{
    .card{{fill:#f5f8ff;stroke:#d6e0f5;}}
    .field-line{{stroke:#9fb3f0;}}
    .pt-bright{{fill:#3b5bdb;}}
    .pt-soft{{fill:#9fb3f0;}}
    .caption{{fill:#5a6785;}}
  }}
</style>
<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="10" class="card"/>
<g>{"".join(lines)}</g>
<g>{"".join(dots)}</g>
<text x="{pad}" y="{h - 14}" class="caption">{caption}</text>
</svg>"""


# ---------------------------------------------------------------------------
# Render: stats.svg
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
        overview_rows.append(f'<text x="{pad}" y="{y}" class="row-label">{escape_xml(label)}</text>')
        overview_rows.append(f'<text x="{card_w - pad}" y="{y}" text-anchor="end" class="row-value">{value}</text>')

    lang_rows = []
    bar_w = card_w - pad * 2
    for i, lang in enumerate(languages[:5]):
        y = 52 + i * 26
        bar_y = y + 6
        fill_w = bar_w * lang["pct"] / 100
        delay = i * 0.12
        lang_rows.append(f'<text x="{pad}" y="{y}" class="row-label">{escape_xml(lang["name"])}</text>')
        lang_rows.append(f'<text x="{card_w - pad}" y="{y}" text-anchor="end" class="row-value-sm">{lang["pct"]:.1f}%</text>')
        lang_rows.append(f'<rect x="{pad}" y="{bar_y}" width="{bar_w}" height="2" rx="1" class="bar-track"/>')
        lang_rows.append(
            f'<rect x="{pad}" y="{bar_y}" width="{fill_w:.2f}" height="2" rx="1" class="bar-fill" '
            f'style="animation-delay:{delay:.2f}s"/>'
        )

    streak_body = f"""
<text x="{card_w / 2}" y="92" text-anchor="middle" class="streak-num">{streaks['current']}</text>
<text x="{card_w / 2}" y="112" text-anchor="middle" class="row-label">days in a row</text>
<text x="{pad}" y="{h - 30}" class="row-label-sm">Longest {streaks['longest']}</text>
<text x="{card_w - pad}" y="{h - 30}" text-anchor="end" class="row-label-sm">Peak {streaks['peak']} a day</text>
"""

    def wrap_card(x, title, body):
        return (
            f'<g transform="translate({x},0)">'
            f'<rect x="0.5" y="0.5" width="{card_w - 1}" height="{h - 1}" rx="10" class="card"/>'
            f'<text x="{pad}" y="32" class="title">{title}</text>{body}</g>'
        )

    cards = (
        wrap_card(card_x(0), "Overview", "".join(overview_rows))
        + wrap_card(card_x(1), "Languages", "".join(lang_rows))
        + wrap_card(card_x(2), "Streak", streak_body)
    )

    return f"""<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Repository, language and streak stats">
<style>
  .card{{fill:{COLORS['bg']};stroke:{COLORS['border']};stroke-width:1;}}
  .title{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:13px;font-weight:600;fill:{COLORS['text']};}}
  .row-label{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:11px;fill:{COLORS['muted']};}}
  .row-label-sm{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:10px;fill:{COLORS['muted']};}}
  .row-value{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:18px;fill:{COLORS['text']};}}
  .row-value-sm{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;fill:{COLORS['text']};}}
  .sep{{stroke:{COLORS['border']};stroke-width:1;}}
  .bar-track{{fill:{COLORS['border']};}}
  .bar-fill{{fill:{COLORS['accent']};transform-box:fill-box;transform-origin:left;}}
  .streak-num{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:40px;font-weight:300;fill:{COLORS['text']};}}
  @media (prefers-color-scheme: light) {{
    .card{{fill:#f5f8ff;stroke:#d6e0f5;}}
    .title{{fill:#0b1426;}}
    .row-label,.row-label-sm{{fill:#5a6785;}}
    .row-value,.row-value-sm,.streak-num{{fill:#0b1426;}}
    .sep{{stroke:#d6e0f5;}}
    .bar-track{{fill:#d6e0f5;}}
    .bar-fill{{fill:#3b5bdb;}}
  }}
  @media (prefers-reduced-motion: no-preference) {{
    .bar-fill{{animation:grow 0.6s ease-out forwards;}}
  }}
  @keyframes grow{{from{{transform:scaleX(0);}}to{{transform:scaleX(1);}}}}
</style>
{cards}
</svg>"""


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
                month_labels.append(f'<text x="{x}" y="{grid_top - 10}" class="month">{month}</text>')
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
        f'<text x="{pad}" y="{grid_top + 1 * (cell + gap) + 8}" class="daylabel">Mon</text>'
        f'<text x="{pad}" y="{grid_top + 3 * (cell + gap) + 8}" class="daylabel">Wed</text>'
        f'<text x="{pad}" y="{grid_top + 5 * (cell + gap) + 8}" class="daylabel">Fri</text>'
    )

    legend_x, legend_y = w - pad - 120, h - 20
    legend_cells = "".join(
        f'<rect x="{legend_x + 36 + i * 12}" y="{legend_y - 8}" width="8" height="8" rx="2" class="heat-{i}"/>'
        for i in range(5)
    )

    return f"""<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Contribution heatmap">
<style>
  .card{{fill:{COLORS['bg']};stroke:{COLORS['border']};stroke-width:1;}}
  .title{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:13px;font-weight:600;fill:{COLORS['text']};}}
  .meta{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:11px;fill:{COLORS['muted']};}}
  .month,.daylabel,.legend-label{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:10px;fill:{COLORS['muted']};}}
  .heat-0{{fill:{HEAT[0]};}} .heat-1{{fill:{HEAT[1]};}} .heat-2{{fill:{HEAT[2]};}} .heat-3{{fill:{HEAT[3]};}} .heat-4{{fill:{HEAT[4]};}}
  @media (prefers-color-scheme: light) {{
    .card{{fill:#f5f8ff;stroke:#d6e0f5;}}
    .title{{fill:#0b1426;}}
    .meta,.month,.daylabel,.legend-label{{fill:#5a6785;}}
    .heat-0{{fill:{HEAT_LIGHT[0]};}} .heat-1{{fill:{HEAT_LIGHT[1]};}} .heat-2{{fill:{HEAT_LIGHT[2]};}} .heat-3{{fill:{HEAT_LIGHT[3]};}} .heat-4{{fill:{HEAT_LIGHT[4]};}}
  }}
  @media (prefers-reduced-motion: no-preference) {{
    .fade{{animation:fadein 0.25s ease-out both;}}
  }}
  @keyframes fadein{{from{{opacity:0;}}to{{opacity:1;}}}}
</style>
<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="10" class="card"/>
<text x="{pad}" y="32" class="title">Activity</text>
<text x="{w - pad}" y="32" text-anchor="end" class="meta">Longest streak {streaks['longest']} days, peak {streaks['peak']} in a day</text>
{"".join(month_labels)}
{day_labels}
{"".join(cells)}
<text x="{legend_x}" y="{legend_y}" class="legend-label">Less</text>
{legend_cells}
<text x="{legend_x + 36 + 5 * 12 + 4}" y="{legend_y}" class="legend-label">More</text>
</svg>"""


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
        lang_color = (lang_edges[0]["node"].get("color") if lang_edges else None) or COLORS["muted"]

        if lang_name:
            lang_bit = (
                f'<circle cx="{x + 2}" cy="{y + 46}" r="3" fill="{lang_color}"/>'
                f'<text x="{x + 10}" y="{y + 49}" class="meta">{escape_xml(lang_name)} · updated {when}</text>'
            )
        else:
            lang_bit = f'<text x="{x}" y="{y + 49}" class="meta">updated {when}</text>'

        cells.append(
            f'<text x="{x}" y="{y + 16}" class="repo-name">{escape_xml(repo["name"])}</text>'
            f'<text x="{x}" y="{y + 32}" class="desc">{escape_xml(desc)}</text>'
            f"{lang_bit}"
        )

    return f"""<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Recently shipped repositories">
<style>
  .card{{fill:{COLORS['bg']};stroke:{COLORS['border']};stroke-width:1;}}
  .title{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:13px;font-weight:600;fill:{COLORS['text']};}}
  .repo-name{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:13px;fill:{COLORS['accent']};}}
  .desc,.meta{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:11px;fill:{COLORS['muted']};}}
  @media (prefers-color-scheme: light) {{
    .card{{fill:#f5f8ff;stroke:#d6e0f5;}}
    .title{{fill:#0b1426;}}
    .repo-name{{fill:#3b5bdb;}}
    .desc,.meta{{fill:#5a6785;}}
  }}
</style>
<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="10" class="card"/>
<text x="{pad}" y="32" class="title">Recently shipped</text>
{"".join(cells)}
</svg>"""


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
        languages = derive_languages(repos, exclude)
        overview = derive_overview(repos, exclude, user_data["followers"]["totalCount"])
        builds = derive_builds(repos, exclude, config.get("builds_count", 4))
        active_days_60 = sum(1 for d in days[-60:] if d["count"] > 0)

        svgs = {
            "header": render_header(config, weekly_totals, total_contributions),
            "stack": render_stack(config),
            "field": render_field(active_days_60, total_contributions),
            "stats": render_stats(overview, languages, streaks),
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
