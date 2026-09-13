import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.sync_api import sync_playwright


TARGET_URL = os.environ.get(
    "TARGET_URL",
    "https://www.rnz.de/anzeigen-immobilien_rubrik,4240.html",
)

STATE_FILE = Path("state/listings.json")


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "")
    return text.strip()


def canonical_url(url: str) -> str:
    """
    Remove fragments and tracking parameters so the same listing
    doesn't appear as a new listing because of a changing URL.
    """
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


def listing_id(url: str) -> str:
    return hashlib.sha256(
        canonical_url(url).encode("utf-8")
    ).hexdigest()


def load_previous_listings():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def save_listings(listings):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    STATE_FILE.write_text(
        json.dumps(
            listings,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def extract_listing_text(anchor):
    """
    Try to find the container belonging to the listing.

    RNZ can change CSS class names, so this deliberately uses
    several structural fallbacks rather than relying on one
    fragile CSS selector.
    """

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

            for (let i = 0; i < 5 && el; i++) {
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


def extract_title(anchor, container_text):
    title = clean_text(anchor.inner_text())

    if title and len(title) >= 5:
        return title

    lines = [
        clean_text(x)
        for x in container_text.splitlines()
        if clean_text(x)
    ]

    if lines:
        return lines[0]

    return "Neue Immobilienanzeige"


def extract_listings(page):
    """
    Find links pointing to RNZ advertisement detail pages.

    The exact listing-card HTML can change, so we identify
    advertisements primarily by their detail URL.
    """

    candidates = page.locator(
        "a[href*='anzeige-detail']"
    )

    listings = {}

    for i in range(candidates.count()):
        anchor = candidates.nth(i)

        try:
            href = anchor.get_attribute("href")
            if not href:
                continue

            url = canonical_url(
                urljoin(TARGET_URL, href)
            )

            parsed = urlparse(url)

            if parsed.netloc and parsed.netloc != urlparse(
                TARGET_URL
            ).netloc:
                continue

            container_text = extract_listing_text(anchor)
            container_text = clean_text(container_text)

            if len(container_text) < 10:
                continue

            title = extract_title(
                anchor,
                container_text,
            )

            item_id = listing_id(url)

            listings[item_id] = {
                "url": url,
                "title": title,
                "text": container_text,
            }

        except Exception as exc:
            print(
                f"Could not process listing {i}: {exc}"
            )

    return listings


def main():
    print(f"Checking: {TARGET_URL}")

    previous = load_previous_listings()

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
                "No response received from RNZ."
            )

        if not response.ok:
            raise RuntimeError(
                f"RNZ returned HTTP {response.status}"
            )

        # Allow dynamically loaded advertisements to appear.
        page.wait_for_timeout(7_000)

        # Scroll down so lazy-loaded listings are triggered.
        for _ in range(5):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(700)

        listings = extract_listings(page)

        browser.close()

    print(
        f"Found {len(listings)} individual listings."
    )

    if not listings:
        raise RuntimeError(
            "No Immobilien listings were detected. "
            "RNZ may have changed its page structure or "
            "the page may have returned a bot/challenge page. "
            "The previous state was NOT overwritten."
        )

    new_ids = set(listings) - set(previous)

    print(
        f"New listings detected: {len(new_ids)}"
    )

    # First successful run establishes the baseline.
    if not previous:
        print(
            "No previous database found. "
            "Creating initial baseline without notifications."
        )

        save_listings(listings)

        with open(
            os.environ["GITHUB_OUTPUT"],
            "a",
            encoding="utf-8",
        ) as output:
            output.write("new_count=0\n")
            output.write("baseline=true\n")

        return

    # Save the complete current database.
    save_listings(listings)

    # Prepare GitHub Actions output.
    with open(
        os.environ["GITHUB_OUTPUT"],
        "a",
        encoding="utf-8",
    ) as output:
        output.write(
            f"new_count={len(new_ids)}\n"
        )
        output.write("baseline=false\n")

    if not new_ids:
        print("No new advertisements.")
        return

    print("\nNEW LISTINGS:")

    for item_id in new_ids:
        listing = listings[item_id]

        print(
            f"\n{listing['title']}"
            f"\n{listing['url']}"
            f"\n{listing['text'][:1000]}"
        )


if __name__ == "__main__":
    main()
