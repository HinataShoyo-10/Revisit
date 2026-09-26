import os
import re
import secrets
import uuid
from html import unescape
from math import ceil
from datetime import datetime
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request, url_for
from pymongo import MongoClient, DESCENDING, ASCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, "atlas-credentials.env"))
MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI must be set in the environment or atlas-credentials.env")
APP_USERNAME = os.environ.get("APP_USERNAME", "revisit")
APP_PASSWORD = os.environ.get("APP_PASSWORD")
if not APP_PASSWORD:
    raise RuntimeError("APP_PASSWORD must be set in the environment or atlas-credentials.env")

app = Flask(__name__)
mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
database = mongo_client[os.environ.get("MONGODB_DATABASE", "revisit")]
items_collection = database["items"]
collections_collection = database["collections"]
imports_collection = database["imports"]


@app.before_request
def require_authentication():
    if request.endpoint == "healthcheck":
        return None

    credentials = request.authorization
    if (
        credentials
        and credentials.type.lower() == "basic"
        and secrets.compare_digest((credentials.username or "").encode(), APP_USERNAME.encode())
        and secrets.compare_digest((credentials.password or "").encode(), APP_PASSWORD.encode())
    ):
        return None

    return Response(
        "Authentication required", 401,
        {"WWW-Authenticate": 'Basic realm="Revisit"'},
    )


def init_db():
    mongo_client.admin.command("ping")
    items_collection.create_index([("added_at", DESCENDING)])
    items_collection.create_index([("duration_minutes", ASCENDING)])
    items_collection.create_index([("tags", ASCENDING)])
    items_collection.create_index([("collections", ASCENDING)])
    collections_collection.create_index("name", unique=True)
    imports_collection.create_index("url", unique=True)
    imports_collection.create_index([("queued_at", DESCENDING)])


def item_for_template(document):
    return {**document, "id": str(document["_id"])}


def collection_names():
    return [
        row["name"]
        for row in collections_collection.find({}, {"_id": 0, "name": 1}).sort("name", ASCENDING)
    ]


@app.context_processor
def inject_library_options():
    return {
        "all_tags": sorted(
            tag
            for tag in items_collection.distinct("tags")
            if isinstance(tag, str) and tag
        ),
        "all_collections": collection_names(),
    }


@app.route("/healthz")
def healthcheck():
    try:
        mongo_client.admin.command("ping")
    except PyMongoError:
        return jsonify(status="unhealthy"), 503
    return jsonify(status="healthy")


def save_collection(name):
    collections_collection.update_one(
        {"name": name},
        {"$setOnInsert": {"name": name, "created_at": datetime.utcnow().isoformat()}},
        upsert=True,
    )


def delete_empty_collections(names):
    for name in set(names):
        if name and items_collection.count_documents({"collections": name}) == 0:
            collections_collection.delete_one({"name": name})


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if host in ("youtube.com", "youtu.be") or host.endswith(".youtube.com"):
        return "youtube"
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "instagram"
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
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


def create_library_item(url, form, preview=None):
    platform = detect_platform(url)
    fetched_title, thumbnail, fetched_duration = preview or fetch_preview(url, platform)
    title = form.get("title", "").strip() or fetched_title or url
    raw_duration = form.get("duration_minutes", "").strip()
    try:
        duration_minutes = int(raw_duration) if raw_duration else fetched_duration
        if duration_minutes is not None and not 1 <= duration_minutes <= 1440:
            raise ValueError
    except ValueError:
        duration_minutes = None

    tag_names = list(dict.fromkeys(
        tag.strip() for tag in form.get("tags", "").split(",") if tag.strip()
    ))
    selected_collections = form.getlist("collections")
    new_collection = form.get("new_collection", "").strip()
    if new_collection:
        selected_collections.append(new_collection)
    selected_collections = list(dict.fromkeys(
        name.strip() for name in selected_collections if name.strip()
    ))
    for name in selected_collections:
        save_collection(name)

    document = {
        "_id": uuid.uuid4().hex,
        "url": url,
        "platform": platform,
        "title": title,
        "thumbnail": thumbnail,
        "duration_minutes": duration_minutes,
        "added_at": datetime.utcnow().isoformat(),
        "tags": tag_names,
        "collections": selected_collections,
    }
    items_collection.insert_one(document)
    return document


@app.route("/")
def index():
    tag_filter = request.args.get("tag", "").strip()
    collection_filter = request.args.get("collection", "").strip()
    q = request.args.get("q", "").strip()

    query = {}
    if tag_filter:
        query["tags"] = tag_filter
    if collection_filter:
        query["collections"] = collection_filter
    if q:
        search_pattern = {"$regex": re.escape(q), "$options": "i"}
        matching_collections = collections_collection.distinct("name", {"name": search_pattern})
        search_fields = [
            {"title": search_pattern},
            {"url": search_pattern},
            {"platform": search_pattern},
            {"tags": search_pattern},
            {"collections": search_pattern},
        ]
        if matching_collections:
            search_fields.append({"collections": {"$in": matching_collections}})
        query["$or"] = search_fields

    items = [
        item_for_template(document)
        for document in items_collection.find(query).sort("added_at", DESCENDING)
    ]
    return render_template(
        "index.html",
        items=items,
        tag_filter=tag_filter,
        collection_filter=collection_filter,
        q=q,
    )


@app.route("/discover")
def discover():
    collection_filter = request.args.get("collection", "").strip()
    raw_minutes = request.args.get("minutes", "30").strip()
    try:
        minutes = max(1, min(int(raw_minutes), 1440))
    except ValueError:
        minutes = 30

    query = {"duration_minutes": {"$lte": minutes}}
    unknown_query = {"duration_minutes": None}
    if collection_filter:
        query["collections"] = collection_filter
        unknown_query["collections"] = collection_filter
    items = [
        item_for_template(document)
        for document in items_collection.find(query).sort(
            [("duration_minutes", ASCENDING), ("added_at", DESCENDING)]
        )
    ]
    unknown_items = [
        item_for_template(document)
        for document in items_collection.find(unknown_query).sort("added_at", DESCENDING)
    ]
    return render_template(
        "discover.html",
        items=items,
        unknown_items=unknown_items,
        collection_filter=collection_filter,
        minutes=minutes,
    )


@app.route("/add", methods=["GET", "POST"])
def add():
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        if not url:
            return redirect(url_for("add"))
        create_library_item(url, request.form)
        return redirect(url_for("index"))

    return render_template("add.html")


@app.route("/imports", methods=["GET", "POST"])
def imports():
    if request.method == "POST":
        lines = [line.strip() for line in request.form.get("urls", "").splitlines() if line.strip()]
        queued = already_saved = duplicate = invalid = 0
        for url in lines:
            parsed = urlparse(url)
            if len(lines) > 100 and queued + already_saved + duplicate + invalid >= 100:
                invalid += 1
                continue
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                invalid += 1
                continue
            if items_collection.find_one({"url": url}, {"_id": 1}):
                already_saved += 1
                continue
            try:
                imports_collection.insert_one(
                    {
                        "_id": uuid.uuid4().hex,
                        "url": url,
                        "platform": detect_platform(url),
                        "queued_at": datetime.utcnow().isoformat(),
                    }
                )
                queued += 1
            except DuplicateKeyError:
                duplicate += 1
        return redirect(url_for(
            "imports",
            queued=queued,
            already_saved=already_saved,
            duplicate=duplicate,
            invalid=invalid,
        ))

    pending = [
        item_for_template(document)
        for document in imports_collection.find().sort("queued_at", DESCENDING)
    ]
    return render_template(
        "imports.html",
        pending=pending,
        queued=request.args.get("queued", 0, type=int),
        already_saved=request.args.get("already_saved", 0, type=int),
        duplicate=request.args.get("duplicate", 0, type=int),
        invalid=request.args.get("invalid", 0, type=int),
        moved=request.args.get("moved", 0, type=int),
        collection=request.args.get("collection", "Imported"),
        selected_moved=request.args.get("selected_moved", 0, type=int),
        skipped_existing=request.args.get("skipped_existing", 0, type=int),
        selection_error=request.args.get("selection_error", 0, type=int),
        cleared=request.args.get("cleared", 0, type=int),
    )


@app.route("/imports/<import_id>/preview", methods=["POST"])
def import_preview(import_id):
    imported = imports_collection.find_one({"_id": import_id})
    if not imported:
        return jsonify({"error": "Import not found"}), 404
    if imported.get("preview_checked"):
        return jsonify({
            "title": imported.get("title"),
            "thumbnail": imported.get("thumbnail"),
            "duration_minutes": imported.get("duration_minutes"),
        })

    title = thumbnail = duration = None
    if imported["platform"] in {"youtube", "instagram", "tiktok"}:
        title, thumbnail, duration = fetch_preview(imported["url"], imported["platform"])
    if thumbnail and urlparse(thumbnail).scheme not in {"http", "https"}:
        thumbnail = None

    imports_collection.update_one(
        {"_id": import_id},
        {"$set": {
            "title": title,
            "thumbnail": thumbnail,
            "duration_minutes": duration,
            "preview_checked": True,
        }},
    )
    return jsonify({
        "title": title,
        "thumbnail": thumbnail,
        "duration_minutes": duration,
    })


@app.route("/imports/<import_id>/dismiss", methods=["POST"])
def dismiss_import(import_id):
    imports_collection.delete_one({"_id": import_id})
    return redirect(url_for("imports"))


@app.route("/imports/clear", methods=["POST"])
def clear_imports():
    result = imports_collection.delete_many({})
    return redirect(url_for("imports", cleared=result.deleted_count))


def promote_import(imported, collection, tags):
    form = request.form.copy()
    form.setlist("collections", [collection])
    form.setlist("new_collection", [])
    form.setlist("tags", [tags])
    preview = None
    if imported.get("preview_checked"):
        preview = (
            imported.get("title"),
            imported.get("thumbnail"),
            imported.get("duration_minutes"),
        )
    return create_library_item(imported["url"], form, preview=preview)


@app.route("/imports/save-selected", methods=["POST"])
def save_selected_imports():
    import_ids = request.form.getlist("import_ids")
    if not import_ids:
        return redirect(url_for("imports"))

    moved = skipped_existing = selection_error = 0
    for import_id in import_ids:
        imported = imports_collection.find_one({"_id": import_id})
        if not imported:
            continue
        collection = request.form.get(f"collection_{import_id}", "").strip()
        if not collection:
            selection_error += 1
            continue
        if items_collection.find_one({"url": imported["url"]}, {"_id": 1}):
            imports_collection.delete_one({"_id": import_id})
            skipped_existing += 1
            continue
        promote_import(
            imported,
            collection,
            request.form.get(f"tags_{import_id}", ""),
        )
        imports_collection.delete_one({"_id": import_id})
        moved += 1

    return redirect(url_for(
        "imports",
        selected_moved=moved,
        skipped_existing=skipped_existing,
        selection_error=selection_error,
    ))


@app.route("/imports/save-all", methods=["POST"])
def save_all_imports():
    collection = request.form.get("collection", "").strip() or "Imported"
    selected_ids = request.form.getlist("import_ids")
    query = {"_id": {"$in": selected_ids}} if selected_ids else {}
    pending = list(imports_collection.find(query))
    if not pending:
        return redirect(url_for("imports"))

    save_collection(collection)
    urls = [item["url"] for item in pending]
    saved_urls = {
        item["url"]
        for item in items_collection.find({"url": {"$in": urls}}, {"url": 1})
    }
    added_at = datetime.utcnow().isoformat()
    new_items = [
        {
            "_id": uuid.uuid4().hex,
            "url": item["url"],
            "platform": item["platform"],
            "title": item["url"],
            "thumbnail": None,
            "duration_minutes": None,
            "added_at": added_at,
            "tags": [],
            "collections": [collection],
            "source": "import_inbox",
        }
        for item in pending
        if item["url"] not in saved_urls
    ]
    if new_items:
        items_collection.insert_many(new_items)
    imports_collection.delete_many({"_id": {"$in": [item["_id"] for item in pending]}})
    return redirect(url_for(
        "imports",
        moved=len(new_items),
        already_saved=len(saved_urls),
        collection=collection,
    ))


@app.route("/item/<item_id>/edit", methods=["GET", "POST"])
def edit_item(item_id):
    item = items_collection.find_one({"_id": item_id})
    if not item:
        return redirect(url_for("index"))

    if request.method == "POST":
        title = request.form.get("title", "").strip() or item["title"]
        raw_duration = request.form.get("duration_minutes", "").strip()
        try:
            duration_minutes = int(raw_duration) if raw_duration else item.get("duration_minutes")
            if duration_minutes is not None and not 1 <= duration_minutes <= 1440:
                raise ValueError
        except ValueError:
            duration_minutes = item.get("duration_minutes")

        tags = list(dict.fromkeys(
            tag.strip() for tag in request.form.get("tags", "").split(",") if tag.strip()
        ))
        selected_collections = request.form.getlist("collections")
        new_collection = request.form.get("new_collection", "").strip()
        if new_collection:
            selected_collections.append(new_collection)
        selected_collections = list(dict.fromkeys(
            name.strip() for name in selected_collections if name.strip()
        ))
        for name in selected_collections:
            save_collection(name)

        previous_collections = item.get("collections", [])
        items_collection.update_one(
            {"_id": item_id},
            {"$set": {
                "title": title,
                "duration_minutes": duration_minutes,
                "tags": tags,
                "collections": selected_collections,
            }},
        )
        delete_empty_collections(set(previous_collections) - set(selected_collections))
        return redirect(url_for("index", collection=selected_collections[0]) if selected_collections else url_for("index"))

    return render_template("edit.html", item=item_for_template(item))


@app.route("/item/<item_id>/delete", methods=["POST"])
def delete_item(item_id):
    item = items_collection.find_one({"_id": item_id}, {"collections": 1})
    items_collection.delete_one({"_id": item_id})
    if item:
        delete_empty_collections(item.get("collections", []))
    return redirect(url_for("index"))


@app.route("/collections", methods=["GET", "POST"])
def collections():
    if request.method == "POST":
        action = request.form.get("action", "create")
        if action == "rename":
            old_name = request.form.get("old_name", "").strip()
            new_name = request.form.get("new_name", "").strip()
            if old_name and new_name and old_name != new_name:
                try:
                    result = collections_collection.update_one(
                        {"name": old_name}, {"$set": {"name": new_name}}
                    )
                except DuplicateKeyError:
                    return redirect(url_for("collections", error="name_exists"))
                if result.modified_count:
                    items_collection.update_many(
                        {"collections": old_name},
                        {"$addToSet": {"collections": new_name}},
                    )
                    items_collection.update_many(
                        {"collections": old_name},
                        {"$pull": {"collections": old_name}},
                    )
            return redirect(url_for("collections", renamed=new_name or old_name))

        if action == "convert_tag":
            tag = request.form.get("tag", "").strip()
            if tag:
                save_collection(tag)
                items_collection.update_many(
                    {"tags": tag}, {"$addToSet": {"collections": tag}}
                )
            return redirect(url_for("collections", converted=tag))

        name = request.form.get("name", "").strip()
        if name:
            save_collection(name)
        return redirect(url_for("collections"))

    rows = [
        {
            "name": document["name"],
            "count": items_collection.count_documents(
                {"collections": document["name"]}
            ),
        }
        for document in collections_collection.find({}, {"_id": 0, "name": 1}).sort(
            "name", ASCENDING
        )
    ]
    existing_collections = set(collection_names())
    convertible_tags = sorted(
        tag
        for tag in items_collection.distinct("tags")
        if isinstance(tag, str) and tag and tag not in existing_collections
    )
    return render_template(
        "collections.html",
        collections=rows,
        convertible_tags=convertible_tags,
        renamed=request.args.get("renamed", ""),
        converted=request.args.get("converted", ""),
        collection_error=request.args.get("error", ""),
    )


init_db()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
