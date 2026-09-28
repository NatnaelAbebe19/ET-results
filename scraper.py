"""
Scraper for Ethiopian Airlines careers results page.
Fetches and parses result announcements from the corporate website.

Page structure:
  .card[id="accordion_N"]
    .card-header > a.collapselink
      <strong>Postion :</strong> VALUE
      <strong>Location :</strong> VALUE
      <strong>Announcement :</strong> VALUE
    .collapse#collapse_N > .panel-body
      .Mylead elements with position, location, description, candidate list
"""

import asyncio
import hashlib
import logging
import re
import aiohttp
from bs4 import BeautifulSoup, Tag

from config import ET_RESULTS_URL

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def _generate_id(position: str, announcement: str) -> str:
    """Generate a unique ID for a result entry based on its content."""
    raw = f"{position.strip()}|{announcement.strip()}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _extract_label_value(container: Tag, label: str) -> str:
    """
    Extract the text value following a <strong>Label :</strong> tag.
    The value is the next sibling text node after the strong tag.
    """
    strong_tags = container.find_all("strong")
    for strong in strong_tags:
        strong_text = strong.get_text(strip=True)
        if label.lower() in strong_text.lower():
            next_sibling = strong.next_sibling
            if next_sibling and isinstance(next_sibling, str):
                return next_sibling.strip().lstrip("\xa0").strip()
            parent_text = strong.parent.get_text(strip=True) if strong.parent else ""
            if parent_text:
                parts = parent_text.split(strong_text, 1)
                if len(parts) > 1:
                    return parts[1].strip().lstrip("\xa0").strip()
    return ""


async def fetch_results() -> list[dict]:
    """
    Async fetch and parse all result announcements from the ET careers page.
    Uses aiohttp to avoid blocking the event loop during HTTP I/O.

    Returns:
        A list of dicts, each with keys:
        - id: unique hash fingerprint
        - position: job position title
        - location: exam/interview venue
        - announcement: announcement type
        - description: full description text
        - date_time: exam/interview date and time
        - candidates: list of candidate dicts {no, name}
    """
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
            async with session.get(ET_RESULTS_URL) as response:
                response.raise_for_status()
                html = await response.text()
    except Exception as e:
        logger.error(f"Failed to fetch results page: {e}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []

    accordion_cards = soup.select("div.card[id^='accordion_']")

    if not accordion_cards:
        logger.warning("No accordion cards found on the page. Structure may have changed.")
        return []

    for card in accordion_cards:
        result = _parse_accordion_card(card)
        if result:
            results.append(result)

    logger.info(f"Scraped {len(results)} result entries")
    return results


def _parse_accordion_card(card: Tag) -> dict | None:
    """Parse a single accordion card element."""
    try:
        header = card.select_one(".card-header")
        if not header:
            return None

        link = header.select_one("a.collapselink")
        source = link if link else header

        position = _extract_label_value(source, "Postion")
        if not position:
            position = _extract_label_value(source, "Position")
        location = _extract_label_value(source, "Location")
        announcement = _extract_label_value(source, "Announcement")

        if not position:
            return None

        description = ""
        panel_body = card.select_one(".panel-body")
        if panel_body:
            desc_leads = panel_body.select(".Mylead")
            for lead in desc_leads:
                text = lead.get_text(strip=True)
                if "Description" in text:
                    desc_text = text.split(":", 1)[-1].strip() if ":" in text else text
                    description = desc_text[:500]
                    break

            if not description:
                body_text = panel_body.get_text(" ", strip=True)
                if len(body_text) > 50:
                    description = body_text[:500]

        date_time = ""
        if panel_body:
            panel_text = panel_body.get_text(" ", strip=True)
            dt_match = re.search(
                r"DATE\s*&\s*TIME\s*:\s*(.+?)(?=\s+AND,?\s+IF\s+YOUR\s+NAME|\s+PLEASE\s+NOTE|\s+CANDIDATE[_\s]*LIST|$)",
                panel_text,
                re.IGNORECASE,
            )
            if dt_match:
                date_time = dt_match.group(1).strip()

        candidates = []
        if panel_body:
            table = panel_body.find("table")
            if table:
                for row in table.find_all("tr"):
                    cols = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                    if cols and len(cols) >= 2:
                        no_str = cols[0]
                        name_str = cols[1]
                        if no_str.lower() in ("no", "no.", "s.n", "s.no") or "name" in name_str.lower():
                            continue
                        if re.match(r"^\d+$", no_str):
                            candidates.append({"no": no_str, "name": name_str})

            if not candidates:
                panel_text = panel_body.get_text("\n", strip=True)
                in_list = False
                for line in panel_text.splitlines():
                    line = line.strip()
                    if re.search(r"candidate[_\s]*list", line, re.IGNORECASE):
                        in_list = True
                        continue
                    if in_list:
                        match = re.match(r"^(\d+)\s+(.+)", line)
                        if match:
                            candidates.append({"no": match.group(1), "name": match.group(2).strip()})

        return {
            "id": _generate_id(position, announcement),
            "position": position,
            "location": location,
            "announcement": announcement,
            "description": description,
            "date_time": date_time,
            "candidates": candidates,
        }
    except Exception as e:
        logger.warning(f"Failed to parse accordion card: {e}")
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = asyncio.run(fetch_results())
    print(f"\n{'='*60}")
    print(f"Found {len(results)} results")
    print(f"{'='*60}\n")
    for i, r in enumerate(results[:5], 1):
        print(f"#{i}")
        print(f"  Position:     {r['position'][:80]}")
        print(f"  Location:     {r['location'][:80]}")
        print(f"  Announcement: {r['announcement'][:80]}")
        print(f"  Description:  {r['description'][:120]}...")
        print(f"  ID:           {r['id']}")
        print()
