import html
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.sync_api import sync_playwright


TARGET_URL = "https://www.rnz.de/anzeigen-immobilien_rubrik,4240.html"

STATE_FILE = Path("state/listings.json")
FEED_FILE = Path("feed.xml")

FEED_TITLE = "RNZ Immobilienanzeigen"
FEED_DESCRIPTION = "Neue Immobilienanzeigen auf RNZ"
FEED_LINK = TARGET_URL


def clean_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def canonical_url(url):
    parsed = urlparse(url)

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            "",
        )
    )


def make_id(url):
    return hashlib.sha256(
        canonical_url(url).encode("utf-8")
    ).hexdigest()


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def xml_escape(value):
    return html.escape(
        str(value or ""),
        quote=True,
    )


def extract_container_text(anchor):
    return anchor.evaluate(
        """
        (a) => {
            const selectors = [
                "article",
                "li",
                "[class*='card']",
                "[class*='item']",
                "[class*='result']",
                "[class*='anzeige']"
            ];

            for (const selector of selectors) {
                const el = a.closest(selector);

                if (el) {
                    const text = el.innerText || "";

                    if (text.trim().length >= 20) {
                        return text;
                    }
                }
            }

            let el = a;

            for (let i = 0; i < 6 && el; i++) {
                el = el.parentElement;

                if (!el) break;

                const text = el.innerText || "";

                if (text.trim().length >= 20) {
                    return text;
                }
            }

            return a.innerText || "";
        }
        """
    )


def extract_title(anchor, text):
    title = clean_text(anchor.inner_text())

    if len(title) >= 5:
        return title

    lines = [
        clean_text(line)
        for line in text.splitlines()
        if clean_text(line)
    ]

    if lines:
        return lines[0]

    return "Neue Immobilienanzeige"


def extract_price(text):
    patterns = [
        r"\b\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})?\s*€",
        r"€\s*\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})?",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:
            return clean_text(match.group(0))

    return ""


def extract_size(text):
    patterns = [
        r"\b\d+(?:[.,]\d+)?\s*m²\b",
        r"\b\d+(?:[.,]\d+)?\s*qm\b",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:
            return clean_text(match.group(0))

    return ""


def extract_rooms(text):
    patterns = [
        r"\b\d+(?:[.,]\d+)?\s*[- ]?Zimmer\b",
        r"\b\d+(?:[.,]\d+)?\s*Zi\.\b",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:
            return clean_text(match.group(0))

    return ""


def make_rss_title(title, text):
    price = extract_price(text)
    size = extract_size(text)
    rooms = extract_rooms(text)

    extras = []

    if price:
        extras.append(price)

    if size:
        extras.append(size)

    if rooms:
        extras.append(rooms)

    if extras:
        return f"{title} – {' · '.join(extras)}"

    return title


def make_description(title, text, url):
    return f"""
<p><strong>{xml_escape(title)}</strong></p>

<p>{xml_escape(text)}</p>

<p>
<a href="{xml_escape(url)}">
Immobilienanzeige bei RNZ öffnen
</a>
</p>
""".strip()


def extract_listings(page):
    anchors = page.locator(
        "a[href*='anzeige-detail']"
    )

    listings = {}

    for i in range(anchors.count()):
        anchor = anchors.nth(i)

        try:
            href = anchor.get_attribute("href")

            if not href:
                continue

            url = canonical_url(
                urljoin(TARGET_URL, href)
            )

            parsed = urlparse(url)

            # Only accept links on rnz.de.
            if parsed.netloc and parsed.netloc != "www.rnz.de":
                continue

            text = clean_text(
                extract_container_text(anchor)
            )

            if len(text) < 10:
                continue

            title = extract_title(
                anchor,
                text,
            )

            item_id = make_id(url)

            listings[item_id] = {
                "id": item_id,
                "title": title,
                "rss_title": make_rss_title(
                    title,
                    text,
                ),
                "url": url,
                "description": make_description(
                    title,
                    text,
                    url,
                ),
                "text": text,
                "first_seen": datetime.now(
                    timezone.utc
                ).isoformat(),
            }

        except Exception as exc:
            print(
                f"Could not process listing {i}: {exc}"
            )

    return listings


def generate_feed(listings):
    # Newest first.
    items = sorted(
        listings.values(),
        key=lambda x: x.get(
            "first_seen",
            "",
        ),
        reverse=True,
    )

    now = datetime.now(
        timezone.utc
    ).strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )

    rss_items = []

    # Keep the feed reasonably sized.
    for listing in items[:100]:
        rss_items.append(
            f"""
    <item>
      <title>{xml_escape(listing["rss_title"])}</title>
      <link>{xml_escape(listing["url"])}</link>
      <guid isPermaLink="true">{xml_escape(listing["url"])}</guid>
      <pubDate>{datetime_to_rfc822(listing["first_seen"])}</pubDate>
      <description><![CDATA[
        {listing["description"]}
      ]]></description>
    </item>
"""
        )

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{xml_escape(FEED_TITLE)}</title>
    <link>{xml_escape(FEED_LINK)}</link>
    <description>{xml_escape(FEED_DESCRIPTION)}</description>
    <language>de-DE</language>
    <lastBuildDate>{now}</lastBuildDate>
    <ttl>10</ttl>
    <generator>RNZ Immobilien RSS Monitor</generator>

    {"".join(rss_items)}
  </channel>
</rss>
"""

    FEED_FILE.write_text(
        feed,
        encoding="utf-8",
    )


def datetime_to_rfc822(value):
    try:
        dt = datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00",
            )
        )

        return dt.strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )

    except Exception:
        return datetime.now(
            timezone.utc
        ).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )


def main():
    previous = load_state()

    print(
        f"Checking {TARGET_URL}"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={
                "width": 1440,
                "height": 1000,
            },
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        response = page.goto(
            TARGET_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

        if response is None:
            raise RuntimeError(
                "RNZ returned no response."
            )

        if not response.ok:
            raise RuntimeError(
                f"RNZ returned HTTP {response.status}"
            )

        # Allow dynamic content to load.
        page.wait_for_timeout(7000)

        # Trigger lazy-loaded listings.
        for _ in range(8):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(500)

        current = extract_listings(page)

        browser.close()

    print(
        f"Found {len(current)} listings."
    )

    if not current:
        raise RuntimeError(
            "No listings detected. "
            "The state and feed were NOT changed."
        )

    # First successful run:
    # establish baseline.
    if not previous:
        print(
            "Creating initial baseline."
        )

        save_state(current)
        generate_feed(current)

        return

    # Preserve first_seen for existing listings.
    for item_id, listing in current.items():
        if item_id in previous:
            listing["first_seen"] = previous[
                item_id
            ].get(
                "first_seen",
                listing["first_seen"],
            )

    new_ids = (
        set(current.keys())
        - set(previous.keys())
    )

    print(
        f"New listings: {len(new_ids)}"
    )

    for item_id in new_ids:
        print(
            "NEW:",
            current[item_id]["rss_title"],
        )
        print(
            current[item_id]["url"]
        )

    save_state(current)
    generate_feed(current)

    print(
        f"RSS feed generated: {FEED_FILE}"
    )


if __name__ == "__main__":
    main()
