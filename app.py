import os
import re
import secrets
import hashlib
import uuid
from html import unescape
from math import ceil
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFProtect
from pymongo import MongoClient, DESCENDING, ASCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError
from pymongo import ReturnDocument
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, "atlas-credentials.env"))
MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI must be set in the environment or atlas-credentials.env")

app = Flask(__name__)
DEPLOYED = bool(
    os.environ.get("RAILWAY_ENVIRONMENT")
    or os.environ.get("RAILWAY_ENVIRONMENT_NAME")
    or os.environ.get("RENDER")
)
SECRET_KEY = os.environ.get("SECRET_KEY")
if DEPLOYED and not SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set to a stable random value in the hosting environment")
app.config.update(
    SECRET_KEY=SECRET_KEY or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get(
        "SESSION_COOKIE_SECURE", "true" if DEPLOYED else "false"
    ).lower() == "true",
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Sign in to access your library."
mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
database = mongo_client[os.environ.get("MONGODB_DATABASE", "revisit")]
users_collection = database["users"]
login_attempts_collection = database["login_attempts"]
items_collection = database["items"]
collections_collection = database["collections"]
imports_collection = database["imports"]
DUMMY_PIN_HASH = generate_password_hash(secrets.token_urlsafe(16), method="scrypt")


class User(UserMixin):
    def __init__(self, document):
        self.id = str(document["_id"])
        self.username = document["username"]
        self.email = document["email"]


@login_manager.user_loader
def load_user(user_id):
    document = users_collection.find_one({"_id": user_id})
    return User(document) if document else None


def current_user_id():
    return current_user.get_id()


def login_attempt_key(username):
    window = int(datetime.now(timezone.utc).timestamp()) // 900
    remote_address = request.remote_addr or "unknown"
    return hashlib.sha256(f"{remote_address}:{username}:{window}".encode()).hexdigest()


def registration_attempt_key():
    window = int(datetime.now(timezone.utc).timestamp()) // 900
    remote_address = request.remote_addr or "unknown"
    return hashlib.sha256(f"register:{remote_address}:{window}".encode()).hexdigest()


def record_rate_limit_attempt(key):
    return login_attempts_collection.find_one_and_update(
        {"_id": key},
        {
            "$inc": {"attempts": 1},
            "$setOnInsert": {
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30)
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


def owned(query=None):
    return {**(query or {}), "user_id": current_user_id()}


def safe_next_url():
    target = request.args.get("next", "")
    parsed = urlparse(target)
    if (
        parsed.scheme
        or parsed.netloc
        or "\\" in target
        or not target.startswith("/")
        or target.startswith("//")
    ):
        return url_for("index")
    return target


def init_db():
    mongo_client.admin.command("ping")
    users_collection.create_index("username_key", unique=True, name="users_username_unique")
    users_collection.create_index("email_key", unique=True, name="users_email_unique")
    login_attempts_collection.create_index("expires_at", expireAfterSeconds=0)
    items_collection.create_index([("user_id", ASCENDING), ("added_at", DESCENDING)])
    items_collection.create_index([("user_id", ASCENDING), ("duration_minutes", ASCENDING)])
    items_collection.create_index([("user_id", ASCENDING), ("tags", ASCENDING)])
    items_collection.create_index([("user_id", ASCENDING), ("collections", ASCENDING)])
    drop_legacy_index(collections_collection, "name_1")
    collections_collection.create_index(
        [("user_id", ASCENDING), ("name", ASCENDING)],
        unique=True,
        name="collections_owner_name_unique",
    )
    drop_legacy_index(imports_collection, "url_1")
    imports_collection.create_index(
        [("user_id", ASCENDING), ("url", ASCENDING)],
        unique=True,
        name="imports_owner_url_unique",
    )
    imports_collection.create_index([("user_id", ASCENDING), ("queued_at", DESCENDING)])


def drop_legacy_index(collection, index_name):
    if index_name not in collection.index_information():
        return
    try:
        collection.drop_index(index_name)
    except OperationFailure as error:
        if error.code != 27 and "index not found" not in str(error).lower():
            raise


def item_for_template(document):
    return {**document, "id": str(document["_id"])}


def collection_names():
    return [
        row["name"]
        for row in collections_collection.find(
            owned(), {"_id": 0, "name": 1}
        ).sort("name", ASCENDING)
    ]


@app.context_processor
def inject_library_options():
    if not current_user.is_authenticated:
        return {"all_tags": [], "all_collections": []}
    return {
        "all_tags": sorted(
            tag
            for tag in items_collection.distinct("tags", {"user_id": current_user_id()})
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


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        attempt_key = registration_attempt_key()
        previous_attempts = login_attempts_collection.find_one({"_id": attempt_key})
        if previous_attempts and previous_attempts.get("attempts", 0) >= 10:
            return render_template(
                "register.html",
                errors=["Too many attempts. Try again in 15 minutes."],
                username=request.form.get("username", "").strip(),
                email=request.form.get("email", "").strip(),
            ), 429
        record_rate_limit_attempt(attempt_key)

        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        pin = request.form.get("password", "")
        username_key = username.casefold()
        email_key = email.casefold()
        errors = []
        if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", username):
            errors.append("Username must be 3–30 letters, numbers, or underscores.")
        if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            errors.append("Enter a valid email address.")
        if not re.fullmatch(r"[0-9]{4}", pin):
            errors.append("PIN must be exactly four digits.")
        if errors:
            return render_template("register.html", errors=errors, username=username, email=email), 400

        document = {
            "_id": uuid.uuid4().hex,
            "username": username,
            "username_key": username_key,
            "email": email,
            "email_key": email_key,
            "pin_hash": generate_password_hash(pin, method="scrypt"),
            "created_at": datetime.now(timezone.utc),
        }
        try:
            users_collection.insert_one(document)
        except DuplicateKeyError:
            return render_template(
                "register.html",
                errors=["That username or email is already registered."],
                username=username,
                email=email,
            ), 409
        login_user(User(document))
        return redirect(url_for("index"))
    return render_template("register.html", errors=[], username="", email="")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username_key = request.form.get("username", "").strip().casefold()
        pin = request.form.get("password", "")
        attempt_key = login_attempt_key(username_key)
        previous_attempts = login_attempts_collection.find_one({"_id": attempt_key})
        if previous_attempts and previous_attempts.get("attempts", 0) >= 5:
            return render_template(
                "login.html", error="Too many attempts. Try again in 15 minutes.", username=username_key
            ), 429

        document = users_collection.find_one({"username_key": username_key})
        candidate_pin = pin if re.fullmatch(r"[0-9]{4}", pin) else "0000"
        pin_hash = document["pin_hash"] if document else DUMMY_PIN_HASH
        valid_pin = bool(document) and check_password_hash(pin_hash, candidate_pin)
        if not valid_pin:
            record_rate_limit_attempt(attempt_key)
            return render_template(
                "login.html", error="Username or PIN is incorrect.", username=username_key
            ), 401

        login_attempts_collection.delete_one({"_id": attempt_key})
        login_user(User(document))
        return redirect(safe_next_url())
    return render_template("login.html", error="", username="")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


def save_collection(name):
    collections_collection.update_one(
        owned({"name": name}),
        {"$setOnInsert": {
            "user_id": current_user_id(),
            "name": name,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )


def delete_empty_collections(names):
    for name in set(names):
        if name and items_collection.count_documents(owned({"collections": name})) == 0:
            collections_collection.delete_one(owned({"name": name}))


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
        "user_id": current_user_id(),
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
@login_required
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
        matching_collections = collections_collection.distinct(
            "name", owned({"name": search_pattern})
        )
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
        for document in items_collection.find(owned(query)).sort("added_at", DESCENDING)
    ]
    return render_template(
        "index.html",
        items=items,
        tag_filter=tag_filter,
        collection_filter=collection_filter,
        q=q,
    )


@app.route("/discover")
@login_required
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
        for document in items_collection.find(owned(query)).sort(
            [("duration_minutes", ASCENDING), ("added_at", DESCENDING)]
        )
    ]
    unknown_items = [
        item_for_template(document)
        for document in items_collection.find(owned(unknown_query)).sort("added_at", DESCENDING)
    ]
    return render_template(
        "discover.html",
        items=items,
        unknown_items=unknown_items,
        collection_filter=collection_filter,
        minutes=minutes,
    )


@app.route("/add", methods=["GET", "POST"])
@login_required
def add():
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        if not url:
            return redirect(url_for("add"))
        create_library_item(url, request.form)
        return redirect(url_for("index"))

    return render_template("add.html")


@app.route("/imports", methods=["GET", "POST"])
@login_required
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
            if items_collection.find_one(owned({"url": url}), {"_id": 1}):
                already_saved += 1
                continue
            try:
                imports_collection.insert_one(
                    {
                        "_id": uuid.uuid4().hex,
                        "user_id": current_user_id(),
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
        for document in imports_collection.find(owned()).sort("queued_at", DESCENDING)
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
@login_required
def import_preview(import_id):
    imported = imports_collection.find_one(owned({"_id": import_id}))
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
        owned({"_id": import_id}),
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
@login_required
def dismiss_import(import_id):
    imports_collection.delete_one(owned({"_id": import_id}))
    return redirect(url_for("imports"))


@app.route("/imports/clear", methods=["POST"])
@login_required
def clear_imports():
    result = imports_collection.delete_many(owned())
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
@login_required
def save_selected_imports():
    import_ids = request.form.getlist("import_ids")
    if not import_ids:
        return redirect(url_for("imports"))

    moved = skipped_existing = selection_error = 0
    for import_id in import_ids:
        imported = imports_collection.find_one(owned({"_id": import_id}))
        if not imported:
            continue
        collection = request.form.get(f"collection_{import_id}", "").strip()
        if not collection:
            selection_error += 1
            continue
        if items_collection.find_one(owned({"url": imported["url"]}), {"_id": 1}):
            imports_collection.delete_one(owned({"_id": import_id}))
            skipped_existing += 1
            continue
        promote_import(
            imported,
            collection,
            request.form.get(f"tags_{import_id}", ""),
        )
        imports_collection.delete_one(owned({"_id": import_id}))
        moved += 1

    return redirect(url_for(
        "imports",
        selected_moved=moved,
        skipped_existing=skipped_existing,
        selection_error=selection_error,
    ))


@app.route("/imports/save-all", methods=["POST"])
@login_required
def save_all_imports():
    collection = request.form.get("collection", "").strip() or "Imported"
    selected_ids = request.form.getlist("import_ids")
    query = {"_id": {"$in": selected_ids}} if selected_ids else {}
    pending = list(imports_collection.find(owned(query)))
    if not pending:
        return redirect(url_for("imports"))

    save_collection(collection)
    urls = [item["url"] for item in pending]
    saved_urls = {
        item["url"]
        for item in items_collection.find(owned({"url": {"$in": urls}}), {"url": 1})
    }
    added_at = datetime.utcnow().isoformat()
    new_items = [
        {
            "_id": uuid.uuid4().hex,
            "user_id": current_user_id(),
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
    imports_collection.delete_many(
        owned({"_id": {"$in": [item["_id"] for item in pending]}})
    )
    return redirect(url_for(
        "imports",
        moved=len(new_items),
        already_saved=len(saved_urls),
        collection=collection,
    ))


@app.route("/item/<item_id>/edit", methods=["GET", "POST"])
@login_required
def edit_item(item_id):
    item = items_collection.find_one(owned({"_id": item_id}))
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
            owned({"_id": item_id}),
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
@login_required
def delete_item(item_id):
    item = items_collection.find_one(owned({"_id": item_id}), {"collections": 1})
    items_collection.delete_one(owned({"_id": item_id}))
    if item:
        delete_empty_collections(item.get("collections", []))
    return redirect(url_for("index"))


@app.route("/collections", methods=["GET", "POST"])
@login_required
def collections():
    if request.method == "POST":
        action = request.form.get("action", "create")
        if action == "rename":
            old_name = request.form.get("old_name", "").strip()
            new_name = request.form.get("new_name", "").strip()
            if old_name and new_name and old_name != new_name:
                try:
                    result = collections_collection.update_one(
                        owned({"name": old_name}), {"$set": {"name": new_name}}
                    )
                except DuplicateKeyError:
                    return redirect(url_for("collections", error="name_exists"))
                if result.modified_count:
                    items_collection.update_many(
                        owned({"collections": old_name}),
                        {"$addToSet": {"collections": new_name}},
                    )
                    items_collection.update_many(
                        owned({"collections": old_name}),
                        {"$pull": {"collections": old_name}},
                    )
            return redirect(url_for("collections", renamed=new_name or old_name))

        if action == "convert_tag":
            tag = request.form.get("tag", "").strip()
            if tag:
                save_collection(tag)
                items_collection.update_many(
                    owned({"tags": tag}), {"$addToSet": {"collections": tag}}
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
                owned({"collections": document["name"]})
            ),
        }
        for document in collections_collection.find(
            owned(), {"_id": 0, "name": 1}
        ).sort(
            "name", ASCENDING
        )
    ]
    existing_collections = set(collection_names())
    convertible_tags = sorted(
        tag
        for tag in items_collection.distinct("tags", {"user_id": current_user_id()})
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
