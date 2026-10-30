# Maktaba

A minimal, Apple-style reader for a [Kiwix](https://kiwix.org) library, packaged as an
[Umbrel](https://umbrel.com) community app.

- One search box across every book, with instant title suggestions
- Full-text results with per-book filters
- Reading view with contents, text size and automatic dark mode
- Right-to-left books (e.g. Urdu Wikipedia) shown right-to-left
- Continue reading and saved articles (stored in the browser)

Plain HTML/CSS/JS, served by nginx, which also proxies the Kiwix API on the same origin.
No build step and no internet access at runtime.

## Install on Umbrel

1. Install **Kiwix** from the Umbrel App Store and add some ZIM files.
2. Umbrel → App Store → ⋯ → **Community App Stores** → add `https://github.com/anaszaman/maktaba`.
3. Install **Maktaba**. It opens on port 8899.

## Layout

```
umbrel-app-store.yml           store id "anaszaman"
anaszaman-maktaba/
  umbrel-app.yml               app manifest
  docker-compose.yml           nginx serving data/www, proxying to kiwix_web_1:8080
  data/nginx.conf
  data/www/                    the app
dev_server.py                  local preview (see file header)
```
