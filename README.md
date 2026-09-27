# Revisit

Revisit is a private, personal library for saving videos and links to watch
later. Save YouTube, Instagram, TikTok, and other URLs; add tags, collections,
and durations; then find something that fits the time you have. The app is built
with Flask and MongoDB and can be installed as a Progressive Web App (PWA).

## Features

- **Save links:** quick-save from the Library or use the full form to set a title, duration, tags, and one or more collections.
- **Import inbox:** stage up to 100 URLs at once, review previews, dismiss unwanted links, and save all or selected links into a collection. Duplicate, invalid, and already-saved URLs are reported.
- **Video previews:** fetch titles and thumbnails from YouTube oEmbed or public Open Graph metadata when available. Saving still works when a platform blocks preview requests.
- **Time to watch:** filter saved videos by an approximate duration and optionally by collection. Items without a known duration appear separately.
- **Search and organize:** search titles, platforms, URLs, tags, and collection names. Rename collections or convert a tag into a same-named collection without removing the tag.
- **Per-account libraries:** account data is scoped to its owner in MongoDB.
- **Install and share:** Android Chrome can share a video directly to Revisit; iOS uses a clipboard handoff (see [PWA installation and sharing](#pwa-installation-and-sharing)).

## Requirements

- Python 3.9 or newer
- A reachable MongoDB deployment, such as MongoDB Atlas or a local MongoDB server
- Git, if you are cloning the repository

## Local installation

**1. Clone the repository** and enter its directory:

```sh
git clone <repository-url>
cd Revisit
```

**2. Create and activate a virtual environment:**

```sh
python3 -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell, activate it with `.\.venv\Scripts\Activate.ps1`.

**3. Install the application dependencies:**

```sh
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

**4. Configure MongoDB and a stable session secret** as described below.

**5. Start the development server:**

```sh
python app.py
```

Open <http://127.0.0.1:5000>, create an account, and sign in. The health
endpoint is <http://127.0.0.1:5000/healthz>.

## Configuration

The app reads `MONGODB_URI` from the process environment or from
`atlas-credentials.env` beside `app.py`. That local file is ignored by Git.
Create it with your own values:

```dotenv
MONGODB_URI=mongodb+srv://<db-user>:<db-password>@<cluster-host>/<database>?retryWrites=true&w=majority
MONGODB_DATABASE=revisit
SECRET_KEY=<long-random-secret>
SESSION_COOKIE_SECURE=false
```

Replace the placeholders; do not commit real credentials. For MongoDB Atlas,
create a database user and allow the development machine's IP in the Atlas
network access list. A local MongoDB server can be used by setting
`MONGODB_URI` to its connection string instead.

Generate a secret with `python -c 'import secrets; print(secrets.token_hex(32))'`.
Keep it stable between restarts so sessions remain valid. In local HTTP
development, `SESSION_COOKIE_SECURE=false` allows the browser to send the login
cookie. Use `true` behind HTTPS in production. `MONGODB_DATABASE` is optional
and defaults to `revisit`.

The app checks its MongoDB connection and creates required indexes when it
starts. `/healthz` reports whether MongoDB is reachable.

## Using Revisit

### Library

Paste a URL into the quick-save field or open the full save form. Titles,
thumbnails, and some durations are fetched automatically where the source
allows it. You can override the title and duration, add comma-separated tags,
and assign a link to multiple collections. Open a saved item to edit its
metadata or remove it from the library.

### Import inbox

Paste one URL per line and submit the batch. Revisit accepts up to 100 links per
submission, identifies duplicates and links already in the library, and stages
the rest in the inbox. Preview details load as you browse. Move individual
links, selected links, or the whole batch into a collection; tags and collection
choices can be set for selected items. Clearing or dismissing inbox entries
does not delete items already saved in the Library.

### Collections and tags

Create collections from the Collections page or while saving a link. Rename a
collection to update its memberships. Convert a tag into a collection to add
all items with that tag; the original tag remains on those items. Empty
collections are removed when items are moved or deleted.

### Time to watch

Enter a time limit to find saved videos whose known duration fits. The page can
be limited to one collection. Videos without detected durations appear under
**Duration unknown** and are not included among confirmed fits. Duration can be
entered or corrected in the save/edit forms; accepted values range from 1 to
1,440 minutes.

### PWA installation and sharing

The site must be served over HTTPS for service workers, installation, and
clipboard access to work reliably. `localhost` is also treated as a secure
context during development.

**Android:** open Revisit in Chrome, install it from the browser menu, then use
a video's Share action and select Revisit. The shared URL is placed in the
Import inbox. If signed out, Revisit retains the URL through sign-in or account
creation.

**iPhone/iPad:** open Revisit in Safari and choose **Share → Add to Home Screen**.
iOS does not currently allow web apps to register as Share sheet destinations.
From a video's Share menu, copy its link, open Revisit, and tap **Paste from
clipboard** in the Import inbox.

The service worker provides a small offline page only. Library data and saving
links require a network connection; private pages are not cached on the device.

## Tests

Install the test dependency and run the test suite:

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

The account tests use `mongomock`; they do not require a running MongoDB server.

## Deployment

Deploy behind HTTPS and configure these environment variables in the hosting
provider's secret settings:

| Variable | Required | Purpose |
| --- | --- | --- |
| `MONGODB_URI` | Yes | MongoDB connection string |
| `SECRET_KEY` | Yes | Stable, random Flask session/signing key |
| `MONGODB_DATABASE` | No | Database name; defaults to `revisit` |
| `SESSION_COOKIE_SECURE` | Recommended | Set to `true` when served over HTTPS |

Generate `SECRET_KEY` with `openssl rand -hex 32` or Python's `secrets` module.
Use the same value across restarts and all app instances. Do not upload
`atlas-credentials.env`; deployment services do not receive that ignored local
file. In MongoDB Atlas, allow network access from the host and restrict the IP
access list where the provider supports stable outbound addresses.

### Railway

`railway.json` configures the build, health check, and start command. Set
`MONGODB_URI` and `SECRET_KEY` in Railway Variables; set
`SESSION_COOKIE_SECURE=true` when HTTPS is active. The start command is:

```sh
gunicorn --bind 0.0.0.0:$PORT app:app
```

If you set a custom Railway start command, use the command above. The Flask
application is `app:app` (not `main:app`).

### Render

Use the repository's `render.yaml` Blueprint. Supply `MONGODB_URI` and
`SECRET_KEY` when prompted. The Blueprint sets `MONGODB_DATABASE=revisit`,
enables secure session cookies, and configures the Gunicorn start command and
`/healthz` health check.

### Other hosts

Install `requirements.txt`, configure the variables above, and run
`gunicorn --bind 0.0.0.0:$PORT app:app` behind an HTTPS reverse proxy or the
host's managed TLS endpoint.

## Accounts and security

Usernames and email addresses are unique without regard to case. The current
registration flow uses a four-digit PIN, stored as a salted password hash.
Login and signup attempts are throttled using MongoDB records. State-changing
forms use CSRF protection, and library, collection, and import queries are
scoped to the signed-in account.

A four-digit PIN has only 10,000 possible values and is weak protection for a
publicly exposed service. Restrict registration or replace the PIN flow with a
stronger password before opening the app broadly. Email addresses are not
verified. Password/PIN recovery, account deletion, and multi-factor
authentication are not implemented.

## Preview limitations

YouTube titles and thumbnails use YouTube's oEmbed endpoint; duration is read
from the public video page when available. Other sites use public Open Graph
metadata. Instagram and TikTok may block automated requests, so previews can be
missing or incomplete. A preview failure does not prevent saving the URL.

## Existing data

MongoDB records created before account ownership was added remain unassigned.
After creating the account that should own them, run:

```sh
python migrate_legacy_data.py <username>
```

The script reports the unassigned record counts and asks for confirmation before
assigning them. It migrates existing unowned MongoDB records; it does not import
the old SQLite `reelbox.db` file.
