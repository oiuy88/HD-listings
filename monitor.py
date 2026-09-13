import html
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
import pytesseract
from PIL import Image
from io import BytesIO
from playwright.sync_api import sync_playwright


TARGET_URL = (
    "https://www.rnz.de/"
    "anzeigen-immobilien_rubrik,4240.html"
)

STATE_FILE = Path("state/listings.json")
FEED_FILE = Path("feed.xml")

MAX_ITEMS = 100


def clean_text(text):
    text = text or ""
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def xml_escape(text):
    return html.escape(
        str(text or ""),
        quote=True,
    )


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


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def get_image_id(image_url):
    """
    RNZ image URLs look approximately like:

    2612265_1_advertbig_....jpg

    The numeric ID is much more useful as a stable
    listing identifier than a hash of the surrounding HTML.
    """

    filename = Path(
        urlparse(image_url).path
    ).name

    match = re.match(
        r"(\d+)_\d+_advertbig",
        filename,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    return hashlib.sha256(
        image_url.encode("utf-8")
    ).hexdigest()[:24]


def get_image_url(src):
    return urljoin(
        TARGET_URL,
        src,
    )


def ocr_image(image_url):
    """
    OCR the advertisement image.

    German + English are used because property ads
    can contain abbreviations and English terms.
    """

    try:
        response = requests.get(
            image_url,
            timeout=30,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(compatible; RNZ-RSS-Monitor/1.0)"
                )
            },
        )

        response.raise_for_status()

        image = Image.open(
            BytesIO(response.content)
        )

        # Upscale small advertisements before OCR.
        width, height = image.size

        if width < 1000:
            scale = 1000 / width
            image = image.resize(
                (
                    int(width * scale),
                    int(height * scale),
                )
            )

        text = pytesseract.image_to_string(
            image,
            lang="deu+eng",
            config="--psm 6",
        )

        return clean_text(text)

    except Exception as exc:
        print(
            f"OCR failed for {image_url}: {exc}"
        )

        return ""


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
            return clean_text(
                match.group(0)
            )

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
            return clean_text(
                match.group(0)
            )

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
            return clean_text(
                match.group(0)
            )

    return ""


def make_title(ocr_text, image_id):
    """
    Build a useful Feedly title from OCR.

    We deliberately don't pretend that OCR is perfect.
    If we cannot confidently extract structured values,
    we use the first useful OCR phrase instead.
    """

    price = extract_price(ocr_text)
    size = extract_size(ocr_text)
    rooms = extract_rooms(ocr_text)

    parts = []

    if rooms:
        parts.append(rooms)

    if size:
        parts.append(size)

    if price:
        parts.append(price)

    # Look for a likely location.
    location = ""

    known_places = [
        "Heidelberg",
        "Mannheim",
        "Schwetzingen",
        "Leimen",
        "Eppelheim",
        "Dossenheim",
        "Neckargemünd",
        "Wiesloch",
        "Walldorf",
        "Weinheim",
        "Ladenburg",
        "Sandhausen",
        "Nußloch",
    ]

    for place in known_places:
        if re.search(
            rf"\b{re.escape(place)}\b",
            ocr_text,
            re.IGNORECASE,
        ):
            location = place
            break

    if location:
        parts.insert(0, location)

    if parts:
        return "🏠 " + " · ".join(parts)

    # Fallback: use first useful OCR words.
    words = ocr_text.split()

    if words:
        fallback = " ".join(words[:12])

        return (
            f"🏠 {fallback} "
            f"(RNZ #{image_id})"
        )

    return (
        f"🏠 Neue Immobilienanzeige "
        f"(RNZ #{image_id})"
    )


def make_description(
    ocr_text,
    image_url,
    listing_url,
):
    safe_text = xml_escape(
        ocr_text
    )

    return f"""
<p>
  <img
    src="{xml_escape(image_url)}"
    alt="RNZ Immobilienanzeige"
    style="max-width:100%;height:auto;"
  >
</p>

<p>
  <strong>OCR text:</strong>
</p>

<p>
  {safe_text}
</p>

<p>
  <a href="{xml_escape(listing_url)}">
    Original RNZ Immobilienanzeige öffnen
  </a>
</p>
""".strip()


def extract_listing_url(img):
    """
    Try to find the RNZ page associated with the image.
    """

    return img.evaluate(
        """
        (img) => {
            const anchor = img.closest("a");

            if (anchor && anchor.href) {
                return anchor.href;
            }

            return "";
        }
        """
    )


def extract_image_listings(page):
    """
    Find RNZ Immobilien advertisement images.

    We specifically target images containing 'advertbig'
    rather than arbitrary images on the page.
    """

    images = page.locator(
        "img[src*='advertbig']"
    )

    print(
        f"Found {images.count()} advertisement images."
    )

    listings = {}

    for i in range(images.count()):
        img = images.nth(i)

        try:
            src = img.get_attribute("src")

            if not src:
                continue

            image_url = get_image_url(src)

            image_id = get_image_id(
                image_url
            )

            listing_url = (
                extract_listing_url(img)
            )

            if not listing_url:
                listing_url = TARGET_URL

            listing_url = canonical_url(
                listing_url
            )

            print(
                f"OCR #{image_id}: "
                f"{image_url}"
            )

            ocr_text = ocr_image(
                image_url
            )

            title = make_title(
                ocr_text,
                image_id,
            )

            listings[image_id] = {
                "id": image_id,
                "title": title,
                "image_url": image_url,
                "listing_url": listing_url,
                "ocr": ocr_text,
                "first_seen": (
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                ),
            }

        except Exception as exc:
            print(
                f"Could not process image "
                f"{i}: {exc}"
            )

    return listings


def rfc822(iso_date):
    try:
        dt = datetime.fromisoformat(
            iso_date.replace(
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


def generate_feed(listings):
    items = sorted(
        listings.values(),
        key=lambda item: item.get(
            "first_seen",
            "",
        ),
        reverse=True,
    )

    rss_items = []

    for item in items[:MAX_ITEMS]:
        description = make_description(
            item["ocr"],
            item["image_url"],
            item["listing_url"],
        )

        rss_items.append(
            f"""
    <item>
      <title>
        {xml_escape(item["title"])}
      </title>

      <link>
        {xml_escape(item["listing_url"])}
      </link>

      <guid isPermaLink="false">
        rnz-immobilien-{xml_escape(item["id"])}
      </guid>

      <pubDate>
        {rfc822(item["first_seen"])}
      </pubDate>

      <description><![CDATA[
        {description}
      ]]></description>

      <enclosure
        url="{xml_escape(item["image_url"])}"
        type="image/jpeg"
      />
    </item>
"""
        )

    now = datetime.now(
        timezone.utc
    ).strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>

    <title>RNZ Immobilien</title>

    <link>
      {xml_escape(TARGET_URL)}
    </link>

    <description>
      Neue Immobilienanzeigen der Rhein-Neckar-Zeitung
    </description>

    <language>de-DE</language>

    <lastBuildDate>
      {now}
    </lastBuildDate>

    <ttl>10</ttl>

    <generator>
      RNZ Immobilien RSS Monitor
    </generator>

    {"".join(rss_items)}

  </channel>
</rss>
"""

    FEED_FILE.write_text(
        feed,
        encoding="utf-8",
    )


def main():
    previous = load_state()

    print(
        f"Checking RNZ: {TARGET_URL}"
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
                "height": 1200,
            },
            user_agent=(
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
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
                f"RNZ returned HTTP "
                f"{response.status}"
            )

        # Wait for lazy-loaded content.
        page.wait_for_timeout(
            7000
        )

        # Scroll through the page so
        # lazy-loaded advertisement images appear.
        for _ in range(10):
            page.mouse.wheel(
                0,
                1500,
            )
            page.wait_for_timeout(
                600
            )

        current = extract_image_listings(
            page
        )

        browser.close()

    if not current:
        raise RuntimeError(
            "No advertbig images were found. "
            "The RNZ page structure may have changed. "
            "Existing state was NOT overwritten."
        )

    print(
        f"Detected {len(current)} listings."
    )

    # First run = baseline.
    if not previous:

        print(
            "Initial run: establishing baseline."
        )

        save_state(current)
        generate_feed(current)

        return

    # Preserve original first_seen.
    for item_id, item in current.items():

        if item_id in previous:

            item["first_seen"] = (
                previous[item_id]
                .get(
                    "first_seen",
                    item["first_seen"],
                )
            )

    new_ids = (
        set(current.keys())
        - set(previous.keys())
    )

    print(
        f"New advertisements: "
        f"{len(new_ids)}"
    )

    for item_id in new_ids:
        print(
            "NEW:",
            current[item_id]["title"],
        )

    save_state(current)
    generate_feed(current)


if __name__ == "__main__":
    main()
