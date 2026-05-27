"""SportsEngine Tourney tournament schedule fetcher and HTML parser."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import quote

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    SE_TOURNEY_DIVISION_PAGE_URL,
    SE_TOURNEY_SEARCH_API_URL,
    SE_TOURNEY_TEAM_PAGE_URL,
    SE_TOURNEY_TOURNAMENT_PAGE_URL,
)
from .models import ParsedEvent

_LOGGER = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}

_DATE_FORMATS = [
    "%A, %B %d, %Y",   # Saturday, June 7, 2025
    "%A - %B %d, %Y",  # Saturday - June 7, 2025
    "%B %d, %Y",        # June 7, 2025
    "%m/%d/%Y",         # 6/7/2025
    "%A, %B %d",        # Saturday, June 7 (year inferred)
    "%A - %B %d",       # Saturday - June 7 (year inferred)
    "%B %d",            # June 7 (year inferred)
]

_TIME_FORMATS = [
    "%I:%M %p",    # 9:00 AM
    "%I:%M%p",     # 9:00AM
    "%H:%M",       # 13:00
]


# ── Public API ────────────────────────────────────────────────────────────────

async def async_search_tournaments(hass: HomeAssistant, query: str) -> list[dict]:
    """Search SportsEngine Tourney for tournaments.

    Fires the full query plus each individual word (3+ chars) in parallel,
    merges results, and sorts by how many query words appear in the name
    so the most relevant matches come first.

    Returns list of dicts: {id, name, location, dates}.
    """
    session = async_get_clientsession(hass)
    words = [w for w in query.strip().split() if len(w) >= 3]

    # Build unique search terms: full phrase + individual words
    terms = [query]
    for w in words:
        if w.lower() not in query.lower().replace(query, ""):  # avoid exact dup
            if w not in terms:
                terms.append(w)

    # Fire all searches in parallel
    batches = await asyncio.gather(
        *[_search_once(session, t) for t in terms], return_exceptions=True
    )

    seen_ids: set[str] = set()
    all_results: list[dict] = []
    for batch in batches:
        if isinstance(batch, Exception):
            continue
        for item in batch:
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                all_results.append(item)

    # Sort: most query words present in the tournament name = highest rank
    query_words = {w.lower() for w in words}
    all_results.sort(
        key=lambda r: sum(1 for w in query_words if w in r["name"].lower()),
        reverse=True,
    )
    return all_results


async def _search_once(session: aiohttp.ClientSession, term: str) -> list[dict]:
    """Call the SE Tourney search API for a single term. Returns parsed result list."""
    url = SE_TOURNEY_SEARCH_API_URL.format(query=quote(term, safe=""))
    try:
        async with session.get(
            url,
            headers={"Accept": "application/json", "User-Agent": _BROWSER_HEADERS["User-Agent"]},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug("SE Tourney search term %r returned HTTP %d", term, resp.status)
                return []
            data = await resp.json(content_type=None)
    except Exception as err:
        _LOGGER.debug("SE Tourney search term %r failed: %s", term, err)
        return []

    if not isinstance(data, list):
        _LOGGER.warning("SE Tourney search %r: unexpected response type %s", term, type(data))
        return []
    _LOGGER.debug("SE Tourney search %r: %d raw items; first-item keys: %s",
                  term, len(data), list(data[0].keys()) if data else "[]")
    results = []
    for item in data:
        link = item.get("link", "")
        t_id = _id_from_qs(link, "IDTournament")
        if t_id:
            results.append({
                "id": t_id,
                "name": (item.get("title") or item.get("name") or "").strip(),
                "location": (item.get("location") or "").strip(),
                "dates": (item.get("date") or item.get("dates") or "").strip(),
            })
    return results


async def async_fetch_divisions(hass: HomeAssistant, tournament_id: str) -> list[dict]:
    """Fetch divisions for a tournament from Tournament.aspx.

    Returns list of dicts: {id, name}.
    """
    url = f"{SE_TOURNEY_TOURNAMENT_PAGE_URL}?IDTournament={tournament_id}"
    html = await _fetch_html(async_get_clientsession(hass), url)
    if not html:
        _LOGGER.warning("SE Tourney: no HTML returned for tournament %s", tournament_id)
        return []
    div_count = html.lower().count("division.aspx")
    _LOGGER.debug(
        "SE Tourney: tournament %s page: %d bytes, %d Division.aspx hrefs found",
        tournament_id, len(html), div_count,
    )
    if div_count == 0:
        _LOGGER.warning(
            "SE Tourney: no Division.aspx links in HTML for tournament %s — page may be JS-rendered. Snippet: %r",
            tournament_id, html[:500],
        )
    results = _parse_division_links(html, tournament_id)
    _LOGGER.debug("SE Tourney: parsed %d divisions for tournament %s", len(results), tournament_id)
    return results


async def async_fetch_teams(
    hass: HomeAssistant, tournament_id: str, division_id: str
) -> list[dict]:
    """Fetch teams for a division from Division.aspx.

    Returns list of dicts: {id, name}.
    """
    html = await _fetch_html(
        async_get_clientsession(hass),
        f"{SE_TOURNEY_DIVISION_PAGE_URL}?IDTournament={tournament_id}&IDDivision={division_id}",
    )
    if not html:
        return []
    return _parse_team_links(html, division_id)


async def async_fetch_games(
    hass: HomeAssistant,
    tournament_id: str,
    division_id: str,
    team_id: str,
    prefix: str = "",
    color_id: str = "",
    game_duration_minutes: int = 90,
) -> list[ParsedEvent]:
    """Fetch and parse a team's game schedule.

    Fetches both the Team.aspx and Division.aspx pages in parallel.  The
    division page provides authoritative dates for all games (including
    bracket/conditional ones) via the ``date_YYYYMMDD`` CSS class on each row.
    The team page provides the confirmed-vs-conditional split and specific
    field sub-names (e.g. "Field 1").
    """
    session = async_get_clientsession(hass)
    team_url = (
        f"{SE_TOURNEY_TEAM_PAGE_URL}?IDTournament={tournament_id}"
        f"&IDDivision={division_id}&IDTeam={team_id}"
    )
    div_url = (
        f"{SE_TOURNEY_DIVISION_PAGE_URL}?IDTournament={tournament_id}"
        f"&IDDivision={division_id}"
    )

    team_html, div_html = await asyncio.gather(
        _fetch_html(session, team_url),
        _fetch_html(session, div_url),
    )

    div_schedule: dict[str, dict] = {}
    if div_html:
        div_parser = _DivisionScheduleParser()
        div_parser.feed(div_html)
        div_schedule = div_parser.schedule
        _LOGGER.debug(
            "SE Tourney: division schedule has %d games for %s/%s",
            len(div_schedule), tournament_id, division_id,
        )

    if not team_html:
        return []

    events = _parse_games(
        team_html, div_schedule,
        tournament_id, division_id, team_id,
        prefix, color_id, game_duration_minutes,
    )
    _LOGGER.debug(
        "SE Tourney: parsed %d games for team %s/%s/%s",
        len(events), tournament_id, division_id, team_id,
    )
    return events


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def _fetch_html(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, headers=_BROWSER_HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                _LOGGER.warning("SE Tourney page returned HTTP %d for %s", resp.status, url)
                return None
            return await resp.text()
    except Exception as err:
        _LOGGER.error("SE Tourney page fetch failed for %s: %s", url, err)
        return None


def _id_from_qs(url_fragment: str, param: str) -> str | None:
    """Extract a query-string parameter value from a URL or URL fragment."""
    if "?" not in url_fragment:
        return None
    query = url_fragment.split("?", 1)[1].split("#")[0]
    for part in query.split("&"):
        k, _, v = part.partition("=")
        if k.lower() == param.lower() and v:
            return v
    return None


# ── HTML parsers ──────────────────────────────────────────────────────────────

def _parse_division_links(html: str, tournament_id: str) -> list[dict]:
    """Extract {id, name} pairs from Division.aspx links on Tournament.aspx."""
    parser = _AnchorTextParser("IDDivision", filter_param="IDTournament", filter_value=tournament_id)
    parser.feed(html)
    return [{"id": k, "name": v} for k, v in parser.results.items()]


def _parse_team_links(html: str, division_id: str) -> list[dict]:
    """Extract {id, name} pairs from Team.aspx links on Division.aspx."""
    parser = _AnchorTextParser("IDTeam", filter_param="IDDivision", filter_value=division_id)
    parser.feed(html)
    return [{"id": k, "name": v} for k, v in parser.results.items()]


class _AnchorTextParser(HTMLParser):
    """Extract {param_id → display name} from anchor tags whose href contains a given query param.

    Division/team names are inside nested elements within the <a> tag, so we
    collect all innerText and take the first non-empty line before "Last Updated".
    """

    def __init__(self, id_param: str, filter_param: str | None = None, filter_value: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self._id_param = id_param
        self._filter_param = filter_param
        self._filter_value = filter_value
        self._in_link = False
        self._current_id: str | None = None
        self._text_buf = ""
        self.results: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        extracted_id = _id_from_qs(href, self._id_param)
        if not extracted_id:
            return
        if self._filter_param and self._filter_value:
            if _id_from_qs(href, self._filter_param) != self._filter_value:
                return
        self._in_link = True
        self._current_id = extracted_id
        self._text_buf = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_link:
            text = re.split(r"Last [Uu]pdated", self._text_buf)[0]
            name = next((line.strip() for line in text.splitlines() if line.strip()), "")
            if self._current_id and name and self._current_id not in self.results:
                self.results[self._current_id] = name
            self._in_link = False
            self._current_id = None
            self._text_buf = ""

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._text_buf += data


class _DivisionScheduleParser(HTMLParser):
    """Parse Division.aspx schedule table to build game_label → schedule lookup.

    Each game row has a CSS class ``schedule_row date_YYYYMMDD`` which gives the
    authoritative date for that game.  Cells: [game_label, time, location, team1,
    score1, score2, team2, buttons].

    Only rows with a parseable time and a simple game label (letter + digits,
    e.g. "B3", "B11") are collected.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.schedule: dict[str, dict] = {}  # game_label → {date_str, time_str, location}
        self._current_date: str = ""
        self._in_row: bool = False
        self._cells: list[str] = []
        self._cell_buf: str = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            attr = dict(attrs)
            cls = attr.get("class", "")
            m = re.search(r'\bdate_(\d{8})\b', cls)
            if m and "schedule_row" in cls:
                d = m.group(1)
                self._current_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                self._in_row = True
                self._cells = []
            else:
                self._in_row = False
        elif tag in ("td", "th") and self._in_row:
            self._cell_buf = ""

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_row:
            self._cells.append(self._cell_buf.strip())
            self._cell_buf = ""
        elif tag == "tr" and self._in_row:
            self._flush_row()
            self._in_row = False
            self._cells = []

    def handle_data(self, data: str) -> None:
        if self._in_row:
            self._cell_buf += data

    def _flush_row(self) -> None:
        if len(self._cells) < 3 or not self._current_date:
            return
        game_label = self._cells[0].strip()
        time_raw = self._cells[1].strip()
        location = self._cells[2].strip() or None
        if not game_label or not time_raw:
            return
        if not re.match(r'^[A-Za-z]\d+$', game_label):
            return
        m = re.search(r'\b(\d{1,2}:\d{2}\s*[AP]M)\b', time_raw, re.IGNORECASE)
        if not m:
            return
        team1 = re.sub(r'\s+', ' ', self._cells[3]).strip() if len(self._cells) > 3 else ""
        team2 = re.sub(r'\s+', ' ', self._cells[6]).strip() if len(self._cells) > 6 else ""
        if game_label not in self.schedule:
            self.schedule[game_label] = {
                "date_str": self._current_date,
                "time_str": m.group(1),
                "location": location,
                "team1": team1,
                "team2": team2,
            }


class _TeamPageParser(HTMLParser):
    """Stateful parser for Team.aspx.

    Wide <th> rows (colspan >= 4) are treated as section headers.  When one
    parses as a date the parser enters a confirmed-games section.  When one
    does NOT parse as a date (e.g. "Next Bracket Game If Team Wins") the
    parser enters a conditional section and skips all rows until the next
    real date header resets it.  h2–h5 headings are also checked for dates.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.games: list[dict] = []
        self.conditional_sections: list[dict] = []  # [{label, follows_game_id, games:[{game_id,time_str,location}]}]
        self._current_date: str = ""
        self._in_conditional_section: bool = False
        self._last_confirmed_game_id: str = ""
        self._in_heading = False
        self._heading_buf = ""
        self._in_row = False
        self._cells: list[str] = []
        self._cell_buf = ""
        self._in_wide_th = False

    def handle_starttag(self, tag: str, attrs) -> None:
        attr = dict(attrs)
        if tag in ("h2", "h3", "h4", "h5"):
            self._in_heading = True
            self._heading_buf = ""
        elif tag == "tr":
            self._in_row = True
            self._cells = []
        elif tag in ("td", "th"):
            self._cell_buf = ""
            if tag == "th":
                try:
                    colspan = int(attr.get("colspan") or "1")
                except ValueError:
                    colspan = 1
                if colspan >= 4:
                    self._in_wide_th = True
                    self._in_heading = True
                    self._heading_buf = ""

    def handle_endtag(self, tag: str) -> None:
        if tag in ("h2", "h3", "h4", "h5") and self._in_heading and not self._in_wide_th:
            self._try_set_date(self._heading_buf)
            self._in_heading = False
            self._heading_buf = ""
        elif tag == "th":
            if self._in_wide_th:
                old_date = self._current_date
                self._try_set_date(self._heading_buf)
                if self._current_date != old_date:
                    self._in_conditional_section = False
                else:
                    self._in_conditional_section = True
                    label = re.sub(r'^Next Bracket Game\s*', '', self._heading_buf.strip(), flags=re.IGNORECASE).strip()
                    self.conditional_sections.append({"label": label, "follows_game_id": self._last_confirmed_game_id, "games": []})
                self._in_heading = False
                self._in_wide_th = False
                self._heading_buf = ""
            else:
                self._cells.append(self._cell_buf.strip())
                self._cell_buf = ""
        elif tag == "td":
            self._cells.append(self._cell_buf.strip())
            self._cell_buf = ""
        elif tag == "tr":
            self._flush_row()
            self._in_row = False
            self._cells = []

    def handle_data(self, data: str) -> None:
        if self._in_heading:
            self._heading_buf += data
        elif self._in_row:
            self._cell_buf += data

    def _try_set_date(self, text: str) -> None:
        parsed = _parse_date_text(text.strip())
        if parsed:
            self._current_date = parsed

    def _flush_row(self) -> None:
        cells = self._cells
        if len(cells) < 7 or not self._current_date:
            return
        game_id = cells[0].strip()
        time_str = cells[1].strip()
        location = cells[2].strip() or None
        if not game_id or not time_str:
            return

        if self._in_conditional_section:
            m = re.search(r'\b(\d{1,2}:\d{2}\s*[AP]M)\b', time_str, re.IGNORECASE)
            if m and self.conditional_sections:
                self.conditional_sections[-1]["games"].append({
                    "game_id": game_id,
                    "date_str": self._current_date,
                    "display_time": m.group(1),
                    "location": location,
                })
            return

        team1 = re.sub(r'\s+', ' ', cells[3]).strip()
        team2 = re.sub(r'\s+', ' ', cells[6]).strip()
        if not (team1 or team2):
            return
        self.games.append({
            "game_id": game_id,
            "date_str": self._current_date,
            "time_str": time_str,
            "location": location,
            "team1": team1,
            "team2": team2,
        })
        self._last_confirmed_game_id = game_id


def _parse_date_text(text: str) -> str | None:
    """Try multiple date formats; return ISO YYYY-MM-DD or None."""
    cleaned = re.sub(r"^[A-Za-z]{3,9}[\s,\-–]+", "", text).strip()
    for candidate in (text, cleaned):
        for fmt in _DATE_FORMATS:
            try:
                dt = datetime.strptime(candidate, fmt)
                if dt.year == 1900:
                    now = datetime.now()
                    dt = dt.replace(year=now.year)
                    if (dt.date() - now.date()).days < -7:
                        dt = dt.replace(year=now.year + 1)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def _parse_time_text(text: str) -> tuple[int, int] | None:
    """Parse a time string into (hour24, minute) or None.

    SE Tourney pages embed a hidden date before the time in each cell, e.g.
    "Sun 02/22/26 11:00 AM".  Extract just the HH:MM AM/PM portion.
    """
    text = text.strip()
    m = re.search(r'\b(\d{1,2}:\d{2}\s*[AP]M)\b', text, re.IGNORECASE)
    if m:
        text = m.group(1).strip()
    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.hour, dt.minute
        except ValueError:
            continue
    return None


def _parse_games(
    html: str,
    div_schedule: dict[str, dict],
    tournament_id: str,
    division_id: str,
    team_id: str,
    prefix: str,
    color_id: str,
    game_duration_minutes: int,
) -> list[ParsedEvent]:
    parser = _TeamPageParser()
    parser.feed(html)

    # Build description lines from conditional sections.
    # Use division schedule for authoritative dates; keep team-page location
    # (it's often more specific, e.g. "Rosselli Park - Field 1" vs "Rosselli Park").
    # e.g. "If Team Wins: B11 - Sat May 30 - 7:45 AM - Rosselli Park - Field 1"
    description_lines: list[str] = []
    for section in parser.conditional_sections:
        follows_id = section.get("follows_game_id", "")
        label_lower = section["label"].lower()
        if follows_id:
            if "win" in label_lower:
                expected_slot = f"Bracket Winner {follows_id}"
            elif "los" in label_lower:
                expected_slot = f"Bracket Loser {follows_id}"
            else:
                expected_slot = ""
        else:
            expected_slot = ""

        # If we know the expected bracket slot, find the authoritative game from
        # the division schedule — this is ground-truth bracket data and immune to
        # data errors on the team page (e.g. TM listing the wrong conditional game).
        if expected_slot and div_schedule:
            slot_lower = expected_slot.lower()
            sched_game_id = next(
                (gid for gid, info in div_schedule.items()
                 if slot_lower in info.get("team1", "").lower()
                 or slot_lower in info.get("team2", "").lower()),
                None,
            )
            if sched_game_id:
                div_info = div_schedule[sched_game_id]
                date_str = div_info.get("date_str", "")
                display_time = div_info.get("time_str", "")
                location = div_info.get("location")
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                    date_display = f"{dt.strftime('%a %b')} {dt.day}"
                except (ValueError, KeyError):
                    date_display = date_str
                parts = [sched_game_id, date_display, display_time]
                if location:
                    parts.append(location)
                description_lines.append(f"{section['label']}: {' - '.join(parts)}")
                continue  # division schedule found — skip team-page fallback

        # Fallback: division schedule empty or slot not found — use team-page data
        for g in section["games"]:
            game_id = g["game_id"]
            div_info = div_schedule.get(game_id, {})
            date_str = div_info.get("date_str") or g.get("date_str", "")
            location = g.get("location") or div_info.get("location")
            display_time = div_info.get("time_str") or g.get("display_time", "")
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                date_display = f"{dt.strftime('%a %b')} {dt.day}"
            except (ValueError, KeyError):
                date_display = date_str
            parts = [game_id, date_display, display_time]
            if location:
                parts.append(location)
            description_lines.append(f"{section['label']}: {' - '.join(parts)}")
    description = "\n".join(description_lines) or None

    events: list[ParsedEvent] = []
    seen: set[str] = set()

    for game in parser.games:
        time_parts = _parse_time_text(game["time_str"])
        if not time_parts:
            _LOGGER.debug(
                "SE Tourney: skipping game %r — unparseable time %r",
                game["game_id"], game["time_str"],
            )
            continue
        hour, minute = time_parts
        try:
            start_dt = datetime.strptime(game["date_str"], "%Y-%m-%d").replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
        except ValueError:
            continue
        end_dt = start_dt + timedelta(minutes=game_duration_minutes)

        game_id = game["game_id"]
        uid = f"tm_{tournament_id}_{division_id}_{game_id}"
        composite_id = f"{uid}_{prefix}" if prefix else uid

        if composite_id in seen:
            continue
        seen.add(composite_id)

        team1 = game["team1"]
        team2 = game["team2"]
        summary = f"{team1} vs {team2}"
        location = game["location"]

        raw = f"UID:{uid}\nSUMMARY:{summary}\nDTSTART:{start_dt.isoformat()}\nLOCATION:{location or ''}"
        if description:
            raw += f"\nDESCRIPTION:{description}"
        md5 = hashlib.md5(raw.encode()).hexdigest()

        events.append(
            ParsedEvent(
                uid=uid,
                composite_id=composite_id,
                prefix=prefix,
                summary=summary,
                start=start_dt,
                end=end_dt,
                is_all_day=False,
                location=location,
                description=description,
                recurrence=[],
                color_id=color_id or None,
                status=None,
                url=None,
                md5=md5,
                raw_ical_no_dtstamp=raw,
                enrichment_suffix="",
                skip_title_case=True,
            )
        )

    return events
