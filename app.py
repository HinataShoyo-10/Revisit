import os
import re
import sqlite3
from html import unescape
from math import ceil
from datetime import datetime
from urllib.parse import urlparse

import requests
from flask import Flask, g, redirect, render_template, request, url_for

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reelbox.db")

app = Flask(__name__)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            platform TEXT NOT NULL,
            title TEXT,
            thumbnail TEXT,
            duration_minutes INTEGER,
            added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS item_tags (
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (item_id, tag_id)
        );
        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS item_collections (
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            PRIMARY KEY (item_id, collection_id)
        );
        """
    )
    item_columns = {
        row[1] for row in db.execute("PRAGMA table_info(items)").fetchall()
    }
    if "duration_minutes" not in item_columns:
        db.execute("ALTER TABLE items ADD COLUMN duration_minutes INTEGER")
    db.commit()
    db.close()


def detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "instagram.com" in host:
        return "instagram"
    if "tiktok.com" in host:
        return "tiktok"
    return "other"


def fetch_youtube_preview(url: str):
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=6,
        )
        if r.ok:
            data = r.json()
            return data.get("title"), data.get("thumbnail_url")
    except requests.RequestException:
        pass
    return None, None


def fetch_youtube_duration(url: str):
    try:
        response = requests.get(
            url,
            timeout=6,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Revisit/1.0)"},
        )
        match = re.search(r'"lengthSeconds":"(\d+)"', response.text)
        if response.ok and match:
            return ceil(int(match.group(1)) / 60)
    except requests.RequestException:
        pass
    return None


def fetch_og_preview(url: str):
    # ponytail: regex OG-tag scrape instead of an HTML-parser dependency.
    # Works for most sites; Instagram/TikTok sometimes block bot requests,
    # in which case both come back None and the item just saves with no
    # thumbnail. Upgrade path: swap in a headless-browser fetch if that
    # matters more than staying dependency-light.
    try:
        r = requests.get(
            url,
            timeout=6,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Revisit/1.0)"},
        )
        if not r.ok:
            return None, None
        html = r.text
        title_match = re.search(
            r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]+content=["\']([^"\']+)', html,
            re.IGNORECASE,
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:title["\']', html,
            re.IGNORECASE,
        )
        image_match = re.search(
            r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)', html,
            re.IGNORECASE,
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']', html,
            re.IGNORECASE,
        )
        duration_match = re.search(
            r'<meta[^>]+(?:property|name)=["\'](?:video:duration|og:video:duration)["\'][^>]+content=["\'](\d+)',
            html,
            re.IGNORECASE,
        ) or re.search(
            r'<meta[^>]+content=["\'](\d+)["\'][^>]+(?:property|name)=["\'](?:video:duration|og:video:duration)["\']',
            html,
            re.IGNORECASE,
        )
        title = unescape(title_match.group(1)).strip() if title_match else None
        image = unescape(image_match.group(1)).strip() if image_match else None
        duration = ceil(int(duration_match.group(1)) / 60) if duration_match else None
        return title, image, duration
    except requests.RequestException:
        return None, None, None


def fetch_preview(url: str, platform: str):
    if platform == "youtube":
        title, thumb = fetch_youtube_preview(url)
        if title:
            return title, thumb, fetch_youtube_duration(url)
    return fetch_og_preview(url)


@app.route("/")
def index():
    db = get_db()
    tag_filter = request.args.get("tag", "").strip()
    collection_filter = request.args.get("collection", "").strip()
    q = request.args.get("q", "").strip()

    sql = "SELECT DISTINCT items.* FROM items"
    joins = []
    where = []
    params = []

    if tag_filter:
        joins.append(
            "JOIN item_tags it ON it.item_id = items.id JOIN tags t ON t.id = it.tag_id"
        )
        where.append("t.name = ?")
        params.append(tag_filter)
    if collection_filter:
        joins.append(
            "JOIN item_collections ic ON ic.item_id = items.id "
            "JOIN collections c ON c.id = ic.collection_id"
        )
        where.append("c.name = ?")
        params.append(collection_filter)
    if q:
        where.append("items.title LIKE ?")
        params.append(f"%{q}%")

    sql += " " + " ".join(joins)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY items.added_at DESC"

    items = db.execute(sql, params).fetchall()

    items_with_meta = []
    for item in items:
        tags = db.execute(
            "SELECT tags.name FROM tags JOIN item_tags ON item_tags.tag_id = tags.id "
            "WHERE item_tags.item_id = ?",
            (item["id"],),
        ).fetchall()
        colls = db.execute(
            "SELECT collections.name FROM collections "
            "JOIN item_collections ON item_collections.collection_id = collections.id "
            "WHERE item_collections.item_id = ?",
            (item["id"],),
        ).fetchall()
        items_with_meta.append(
            {
                **dict(item),
                "tags": [t["name"] for t in tags],
                "collections": [c["name"] for c in colls],
            }
        )

    all_tags = [r["name"] for r in db.execute("SELECT name FROM tags ORDER BY name")]
    all_collections = [
        r["name"] for r in db.execute("SELECT name FROM collections ORDER BY name")
    ]

    return render_template(
        "index.html",
        items=items_with_meta,
        all_tags=all_tags,
        all_collections=all_collections,
        tag_filter=tag_filter,
        collection_filter=collection_filter,
        q=q,
    )


@app.route("/discover")
def discover():
    db = get_db()
    collection_filter = request.args.get("collection", "").strip()
    raw_minutes = request.args.get("minutes", "30").strip()
    try:
        minutes = max(1, min(int(raw_minutes), 1440))
    except ValueError:
        minutes = 30

    sql = "SELECT DISTINCT items.* FROM items"
    params = [minutes]
    where = ["items.duration_minutes IS NOT NULL", "items.duration_minutes <= ?"]
    if collection_filter:
        sql += (
            " JOIN item_collections ic ON ic.item_id = items.id "
            "JOIN collections c ON c.id = ic.collection_id"
        )
        where.append("c.name = ?")
        params.append(collection_filter)
    sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY items.duration_minutes ASC, items.added_at DESC"
    items = db.execute(sql, params).fetchall()
    all_collections = [
        row["name"] for row in db.execute("SELECT name FROM collections ORDER BY name")
    ]
    return render_template(
        "discover.html",
        items=items,
        all_collections=all_collections,
        collection_filter=collection_filter,
        minutes=minutes,
    )


@app.route("/add", methods=["GET", "POST"])
def add():
    db = get_db()
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        if not url:
            return redirect(url_for("add"))

        platform = detect_platform(url)
        fetched_title, thumbnail, fetched_duration = fetch_preview(url, platform)
        title = request.form.get("title", "").strip() or fetched_title or url
        raw_duration = request.form.get("duration_minutes", "").strip()
        try:
            duration_minutes = (
                int(raw_duration) if raw_duration else fetched_duration
            )
            if duration_minutes is not None and not 1 <= duration_minutes <= 1440:
                raise ValueError
        except ValueError:
            duration_minutes = None

        cur = db.execute(
            "INSERT INTO items (url, platform, title, thumbnail, duration_minutes, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (url, platform, title, thumbnail, duration_minutes, datetime.utcnow().isoformat()),
        )
        item_id = cur.lastrowid

        tag_names = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]
        for name in tag_names:
            db.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
            tag_id = db.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
            db.execute(
                "INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?)",
                (item_id, tag_id),
            )

        collection_names = request.form.getlist("collections")
        new_collection = request.form.get("new_collection", "").strip()
        if new_collection:
            collection_names.append(new_collection)
        for name in collection_names:
            db.execute("INSERT OR IGNORE INTO collections (name) VALUES (?)", (name,))
            coll_id = db.execute(
                "SELECT id FROM collections WHERE name = ?", (name,)
            ).fetchone()["id"]
            db.execute(
                "INSERT OR IGNORE INTO item_collections (item_id, collection_id) VALUES (?, ?)",
                (item_id, coll_id),
            )

        db.commit()
        return redirect(url_for("index"))

    all_collections = [
        r["name"] for r in db.execute("SELECT name FROM collections ORDER BY name")
    ]
    return render_template("add.html", all_collections=all_collections)


@app.route("/item/<int:item_id>/delete", methods=["POST"])
def delete_item(item_id):
    db = get_db()
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    return redirect(url_for("index"))


@app.route("/collections", methods=["GET", "POST"])
def collections():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            db.execute("INSERT OR IGNORE INTO collections (name) VALUES (?)", (name,))
            db.commit()
        return redirect(url_for("collections"))

    rows = db.execute(
        """
        SELECT collections.name, COUNT(item_collections.item_id) AS count
        FROM collections
        LEFT JOIN item_collections ON item_collections.collection_id = collections.id
        GROUP BY collections.id
        ORDER BY collections.name
        """
    ).fetchall()
    return render_template("collections.html", collections=rows)


init_db()

if __name__ == "__main__":
    app.run(debug=True)
