import hashlib
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response

RNZ_URL = "https://www.rnz.de/anzeigen-immobilien_rubrik,4240.html"
BASE_URL = "https://www.rnz.de"

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "600"))
DB_PATH = os.environ.get("DB_PATH", "data/rnz.db")
PORT = int(os.environ.get("PORT", "8080"))

USER_AGENT = (
    "Mozilla/5.0 (compatible; RNZ-RSS/1.0; "
    "+https://github.com/example/rnz-rss)"
)

app = Flask(__name__)

db_lock = threading.Lock()


def get_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS adverts (
            advert_id INTEGER PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT,
            first_seen TEXT NOT NULL
        )
        """
    )

    conn.commit()
    return conn


def fetch_page():
    response = requests.get(
        RNZ_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    return response.text


def extract_adverts(html):
    soup = BeautifulSoup(html, "html.parser")

    adverts = {}

    # The RNZ page contains links such as:
    #
    # /anzeige-detail_advertid,49097.html
    #
    pattern = re.compile(r"/anzeige-detail_advertid,(\d+)\.html")

    for link in soup.find_all("a", href=True):
        match = pattern.search(link["href"])

        if not match:
            continue

        advert_id = int(match.group(1))
        url = urljoin(BASE_URL, match.group(0))

        # Try to find a useful title.
        title = link.get_text(" ", strip=True)

        if not title:
            img = link.find("img")
            if img:
                title = img.get("alt", "").strip()

        if not title:
            title = f"RNZ Immobilienanzeige {advert_id}"

        adverts[advert_id] = {
            "advert_id": advert_id,
            "url": url,
            "title": title,
        }

    return list(adverts.values())


def get_existing_ids():
    conn = get_db()

    try:
        rows = conn.execute(
            "SELECT advert_id FROM adverts"
        ).fetchall()

        return {row["advert_id"] for row in rows}
    finally:
        conn.close()


def save_new_adverts(adverts):
    now = datetime.now(timezone.utc).isoformat()

    new_adverts = []

    with db_lock:
        conn = get_db()

        try:
            for advert in adverts:
                existing = conn.execute(
                    """
                    SELECT advert_id
                    FROM adverts
                    WHERE advert_id = ?
                    """,
                    (advert["advert_id"],),
                ).fetchone()

                if existing:
                    continue

                conn.execute(
                    """
                    INSERT INTO adverts (
                        advert_id,
                        url,
                        title,
                        first_seen
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        advert["advert_id"],
                        advert["url"],
                        advert["title"],
                        now,
                    ),
                )

                advert["first_seen"] = now
                new_adverts.append(advert)

            conn.commit()

        finally:
            conn.close()

    return new_adverts


def poll_once():
    print("Checking RNZ...")

    try:
        html = fetch_page()
        adverts = extract_adverts(html)

        print(f"Found {len(adverts)} adverts")

        if not adverts:
            print(
                "WARNING: zero adverts found. "
                "The RNZ page structure may have changed."
            )
            return

        new_adverts = save_new_adverts(adverts)

        if new_adverts:
            print(
                "New adverts:",
                ", ".join(str(a["advert_id"]) for a in new_adverts),
            )
        else:
            print("No new adverts.")

    except Exception as exc:
        print(f"ERROR while polling RNZ: {exc}")


def polling_loop():
    # Do the first check immediately.
    poll_once()

    while True:
        time.sleep(POLL_INTERVAL)
        poll_once()


def escape_xml(value):
    if value is None:
        return ""

    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def make_guid(advert):
    raw = f"rnz-immobilien:{advert['advert_id']}"
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_rss():
    conn = get_db()

    try:
        rows = conn.execute(
            """
            SELECT advert_id, url, title, first_seen
            FROM adverts
            ORDER BY datetime(first_seen) DESC
            LIMIT 100
            """
        ).fetchall()
    finally:
        conn.close()

    now = datetime.now(timezone.utc)

    items = []

    for row in rows:
        try:
            published = datetime.fromisoformat(
                row["first_seen"]
            ).astimezone(timezone.utc)
        except Exception:
            published = now

        advert_id = row["advert_id"]
        title = row["title"] or f"RNZ Immobilienanzeige {advert_id}"
        url = row["url"]

        items.append(
            f"""
        <item>
            <title>{escape_xml(title)}</title>
            <link>{escape_xml(url)}</link>
            <guid isPermaLink="false">{make_guid(row)}</guid>
            <pubDate>{format_datetime(published)}</pubDate>
            <description>
                Neue Immobilienanzeige auf RNZ:
                {escape_xml(url)}
            </description>
        </item>
        """
        )

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
    <channel>
        <title>RNZ Immobilien – neue Anzeigen</title>
        <link>{RNZ_URL}</link>
        <description>
            Neue Immobilienanzeigen auf RNZ.de
        </description>
        <language>de-DE</language>
        <lastBuildDate>{format_datetime(now)}</lastBuildDate>
        {''.join(items)}
    </channel>
</rss>
"""

    return rss


@app.route("/")
def index():
    return """
    <html>
        <head>
            <title>RNZ Immobilien RSS</title>
        </head>
        <body>
            <h1>RNZ Immobilien RSS</h1>

            <p>
                <a href="/feed.xml">RSS Feed</a>
            </p>

            <p>
                Source:
                <a href="https://www.rnz.de/anzeigen-immobilien_rubrik,4240.html">
                    RNZ Immobilien
                </a>
            </p>
        </body>
    </html>
    """


@app.route("/feed.xml")
def feed():
    rss = generate_rss()

    return Response(
        rss,
        mimetype="application/rss+xml",
        headers={
            "Cache-Control": "no-cache",
        },
    )


@app.route("/health")
def health():
    return {
        "status": "ok",
        "source": RNZ_URL,
        "poll_interval_seconds": POLL_INTERVAL,
    }


if __name__ == "__main__":
    # Start background polling.
    thread = threading.Thread(
        target=polling_loop,
        daemon=True,
    )
    thread.start()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
    )
