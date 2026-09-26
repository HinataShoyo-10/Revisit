# Revisit

Paste-a-link organizer for saved YouTube/Instagram/TikTok content — preview
thumbnails, multi-tags, collections, and time-based discovery. Flask + MongoDB,
one file of backend logic, no JS framework.

## Run locally

Set `APP_PASSWORD` in `atlas-credentials.env` before starting the app. The
optional `APP_USERNAME` defaults to `revisit`.

```
pip install -r requirements.txt
python app.py
```
Visit http://127.0.0.1:5000

## Database

Revisit connects to MongoDB Atlas using `MONGODB_URI`. For local development,
put the connection string in `atlas-credentials.env` beside `app.py` (the file
is ignored by Git), or provide it as an environment variable. The optional
`MONGODB_DATABASE` variable selects the database; it defaults to `revisit`.

The database uses three collections:

- `items`: one document per saved link, including preview fields, duration,
   tags, and collection names embedded as arrays.
- `collections`: one document per collection name, including collections with
   no saved links.
- `imports`: pending links waiting for review and categorization.

Use **Import inbox** to paste up to 100 URLs per batch. Save a large batch into
one staging collection (default `Imported`), then use **Organize** on library
items to assign their final titles, tags, durations, and collections. Duplicate,
invalid, and already-saved URLs are reported and skipped.

Indexes are created automatically on startup. Existing `reelbox.db` data is not
imported automatically; export or migrate it before removing the local file.

## Deployment

For Railway, this repository includes `railway.json`, which starts the app with
`gunicorn --bind 0.0.0.0:$PORT app:app` and checks `/healthz`. If a custom start
command is configured in the Railway service settings, set it to that command;
`main:app` will fail because this project defines the Flask app in `app.py`.

Set `MONGODB_URI` and a strong `APP_PASSWORD` in the Railway service variables.
`APP_USERNAME` defaults to `revisit` and `MONGODB_DATABASE` defaults to
`revisit`. The ignored `atlas-credentials.env` file is for local development;
Railway does not receive it from the repository. The app uses HTTP Basic Auth.

For Render, use the Blueprint in `render.yaml` and provide `MONGODB_URI` and
`APP_PASSWORD` when prompted.

In MongoDB Atlas, allow network access from the deployed service before
deploying. For production, use a restricted IP access list where your hosting
plan supports stable outbound IPs; avoid leaving Atlas open to `0.0.0.0/0`.
Keep the connection string in the host's secret environment settings, not in
the repository. Since the library is stored in MongoDB Atlas, it does not
depend on the host's local disk.

For other hosts, install dependencies with `pip install -r requirements.txt`
and start with `gunicorn --bind 0.0.0.0:$PORT app:app`. Configure `MONGODB_URI`
and `APP_PASSWORD` as secrets, plus optional `APP_USERNAME` and
`MONGODB_DATABASE`, in the hosting provider's environment. Serve behind HTTPS.

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

- Multi-user accounts / login
- Browser extension or share-sheet (you'd paste the link manually)
