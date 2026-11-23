# Nederlands oefenen

A Dutch practice app for beginners (A1–A2). Everything is in Dutch, with pictures instead of translations,
and a 🔊 button on every word and sentence. It uses the same engine as
[frans-oefenen](https://github.com/attie707/frans-oefenen): pure Python, no dependencies, SQLite.

| Part | Exercises |
|---|---|
| 🍎 Woorden | *Wat is dit?* (picture → word), *De of het?*, *Schrijf het woord*. 158 everyday words in 9 themes. |
| 🏃 Werkwoorden | 6 levels: zijn/hebben → regular present → irregular (gaan, kunnen, mogen…) → past tense regular (werkte) → past tense irregular (liep) → perfect tense (heb gewerkt / ben gegaan). Choose or type. |
| 🧩 Zinnen | Tap the words in the right order: questions, inversion (*Morgen ga ik…*), niet/geen, modal verbs, perfect tense, subordinate clauses (*omdat…*). Other correct word orders are accepted too. |
| 🎧 Luisteren | Hear a word → pick the picture · word dictation · hear a sentence → build it. |

There's also repetition of mistakes (Leitner boxes), a daily goal, a streak, points, scores per part, and a CSV export.

## Install into the existing LXC (next to frans-oefenen)

On the **Proxmox host** (replace 108 with your container ID):

```bash
pct set 108 --memory 1024
pct exec 108 -- bash -c "apt-get install -y -qq git >/dev/null; git clone -q https://github.com/attie707/nederlands-oefenen /opt/nederlands-oefenen && bash /opt/nederlands-oefenen/deploy/setup.sh"
```

The app runs on port **8001**. `pct exec 108 -- update` updates all apps in the container, and
`pct exec 108 -- backup` backs up all their databases.

## Speech
- **Piper** (installed automatically, free and local): voice `nl_BE-nathalie-medium`. This is a Flemish voice,
  because Piper's Netherlands-Dutch voices are not good.
- **Azure neural voices** (recommended for Dutch: `nl-NL-FennaNeural`, real Netherlands Dutch): put your key in
  `/etc/oefenen-tts.env` inside the container and run `update`. The free tier is plenty, because every sound is
  generated only once and then cached.
- If neither is available, the browser's own voice is used.

## Nginx Proxy Manager
Add a proxy host, e.g. `nederlands.example.nl` → `http://<container-ip>:8001`, and enable SSL. If it's reachable
from the internet, add `COOKIE_SECURE=1` to `/etc/oefenen-tts.env` and run `update`.
