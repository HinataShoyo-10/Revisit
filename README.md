# Revisit

Paste-a-link organizer for saved YouTube/Instagram/TikTok content — preview
thumbnails, multi-tags, collections, and time-based discovery. Flask + MongoDB,
one file of backend logic, no JS framework.

## Run locally

```
pip install -r requirements.txt
python app.py
```
Visit http://127.0.0.1:5000

## Tests

Install test dependencies with `pip install -r requirements-dev.txt`, then run
`python -m unittest discover -s tests -v`.

## Database

Revisit connects to MongoDB Atlas using `MONGODB_URI`. For local development,
put the connection string in `atlas-credentials.env` beside `app.py` (the file
is ignored by Git), or provide it as an environment variable. The optional
`MONGODB_DATABASE` variable selects the database; it defaults to `revisit`.

The database uses these collections:

- `users`: unique normalized username and email keys plus a salted PIN hash.
- `items`, `collections`, and `imports`: every document is scoped by `user_id`.
- `login_attempts`: short-lived MongoDB-backed login throttling records.

Use **Import inbox** to paste up to 100 URLs per batch. Save a large batch into
one staging collection (default `Imported`), then use **Organize** on library
items to assign their final titles, tags, durations, and collections. Duplicate,
invalid, and already-saved URLs are reported and skipped.

Indexes are created automatically on startup. Existing MongoDB records created
before accounts were added remain unowned until assigned. After creating the
account that should own the existing library, run
`python migrate_legacy_data.py <username>` and confirm the transfer. This does
not import the old `reelbox.db` SQLite database.

## Deployment

For Railway, `railway.json` starts `gunicorn --bind 0.0.0.0:$PORT app:app` and
checks `/healthz`. If a custom start command is configured in Railway settings,
replace it with that command; `main:app` does not exist in this project.

Set `MONGODB_URI` and a stable, random `SECRET_KEY` in Railway Variables. Also
set `SESSION_COOKIE_SECURE=true` so login cookies are sent only over HTTPS.
`MONGODB_DATABASE` defaults to `revisit`. Railway does not receive the ignored
local `atlas-credentials.env` file.

Generate a session key with `openssl rand -hex 32`; keep the same value across
deployments and replicas. Railway startup fails if `SECRET_KEY` is missing.

For Render, use the Blueprint in `render.yaml` and provide `MONGODB_URI` and
`SECRET_KEY` when prompted. Set `SESSION_COOKIE_SECURE=true` there as well.

In MongoDB Atlas, allow network access from the deployed service before
deploying. For production, use a restricted IP access list where your hosting
plan supports stable outbound IPs; avoid leaving Atlas open to `0.0.0.0/0`.
Keep the connection string in the host's secret environment settings, not in
the repository. Since the library is stored in MongoDB Atlas, it does not
depend on the host's local disk.

For other hosts, install dependencies with `pip install -r requirements.txt`
and start with `gunicorn --bind 0.0.0.0:$PORT app:app`. Configure `MONGODB_URI`,
a stable `SECRET_KEY`, and `SESSION_COOKIE_SECURE=true` in the host environment.
Serve behind HTTPS.

## Accounts and security

Each account has a unique username and email. The requested four-digit PIN is
salted and hashed, and login failures and signup attempts are throttled using
MongoDB so the limits are shared across app workers. All state-changing forms
use CSRF protection, and every library query is scoped to its owner.

A four-digit PIN has only 10,000 possible values and is weaker than a normal
password. A longer password or passphrase is strongly recommended before
opening registration publicly. Email verification, password/PIN recovery,
account deletion, and multi-factor authentication are not implemented yet;
email addresses are unique but not verified.

## Time-based discovery

When saving a link, add its approximate duration in minutes. The **Time to
watch** page then finds saved links that fit inside a selected time window, with
an optional collection filter. Revisit automatically tries to read duration
metadata from YouTube and public OG metadata from other platforms; the manual
duration remains available when a platform blocks that request. Links without
a detected duration also appear in a separate **Duration unknown** group and
are not counted as confirmed fits.

## Search and organization

Library search matches titles, source platforms, URLs, tags, and collection
names. Collection names can be edited from the Collections page. Converting a
tag creates a same-named collection and adds every matching item without
removing the tag. Collections are removed automatically when moving or deleting
items leaves them empty.

## Preview behavior

Instagram/TikTok previews come from scraping public `og:title` / `og:image`
tags — no API key needed, but both platforms sometimes block automated
requests. A blocked preview no longer prevents saving: Revisit keeps the link,
uses the URL as its fallback title, and shows a platform placeholder when no
thumbnail is available. Expired thumbnail URLs also fall back to the platform
placeholder in the browser. YouTube previews are reliable (uses YouTube's own
oEmbed endpoint).

## Not built

- Browser extension or share-sheet (you'd paste the link manually)
