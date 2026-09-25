# Revisit

Paste-a-link organizer for saved YouTube/Instagram/TikTok content — preview
thumbnails, multi-tags, collections, and time-based discovery. Flask + SQLite,
one file of backend logic, no JS framework.

## Run locally

```
pip install -r requirements.txt
python app.py
```
Visit http://127.0.0.1:5000

## Deploy for free (get a shareable URL) — PythonAnywhere

PythonAnywhere's free tier: no credit card, persistent disk (your SQLite file
survives restarts), always-on, gives you `https://<yourname>.pythonanywhere.com`.

1. Create a free account at pythonanywhere.com
2. **Files** tab → upload this whole `revisit` folder (or use a Bash console
   there and `git clone` if you push this to GitHub first)
3. Open a **Bash console** in PythonAnywhere and run:
   ```
   cd revisit
   pip install --user -r requirements.txt
   ```
4. **Web** tab → "Add a new web app" → choose **Flask** → point it at
   `revisit/app.py`
5. In the web app's WSGI config file, make sure it imports your `app` object
   from `app.py` (PythonAnywhere's wizard sets this up for you when you pick
   Flask + the right file path)
6. Hit **Reload**, then open the URL it gives you — that's what you share

## Alternative — Render.com

Also free, but the free tier's disk is wiped on every redeploy (fine if you
rarely change the code, annoying if you don't want to lose saved links on a
future update). Steps: push this folder to a GitHub repo → New Web Service on
Render → connect the repo → build command `pip install -r requirements.txt`,
start command `gunicorn app:app`.

## Time-based discovery

When saving a link, add its approximate duration in minutes. The **Time to
watch** page then finds saved links that fit inside a selected time window, with
an optional collection filter. Revisit automatically tries to read duration
metadata from YouTube and public OG metadata from other platforms; the manual
duration remains available when a platform blocks that request. Links without
a duration stay in the library but are not included in timed results.

## Preview behavior

Instagram/TikTok previews come from scraping public `og:title` / `og:image`
tags — no API key needed, but both platforms sometimes block automated
requests. A blocked preview no longer prevents saving: Revisit keeps the link,
uses the URL as its fallback title, and shows a platform placeholder when no
thumbnail is available. Expired thumbnail URLs also fall back to the platform
placeholder in the browser. YouTube previews are reliable (uses YouTube's own
oEmbed endpoint).

## Not built (say the word if you want these next)

- Editing tags/collections on an existing item (currently: delete + re-add)
- Multi-user accounts / login
- Browser extension or share-sheet (you'd paste the link manually)
