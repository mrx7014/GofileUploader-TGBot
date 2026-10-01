# GoFile Telegram Bot

A single-file Telegram bot that uploads whatever you send it to [GoFile](https://gofile.io)
and replies with the download link. Built with [Telethon](https://github.com/LonamiWebs/Telethon)
and the Python standard library, so it runs happily on a phone (Termux) or a small server.

Inspired by [Sushrut1101/GoFile-Upload](https://github.com/Sushrut1101/GoFile-Upload); the upload
logic has been re-implemented in Python so the bot needs no `jq`, no shell script and no `curl`.

## Features

- **Two languages** - English and Arabic, chosen per user with a button picker (`/language`).
  More languages can be added by dropping a JSON file into `locales/` (see [Languages](#languages)).
- **Big files** - uses the MTProto API (not the 20 MB Bot API limit), so files up to Telegram's
  own limit can be received.
- **File info card** before every upload: name, size, format and upload date, with
  **Upload / Rename / Cancel** buttons.
- **Real progress bars** for downloading from Telegram, downloading from a link and
  uploading to GoFile (bytes actually sent, speed included).
- **Stop button** while a transfer is running.
- **Direct links** - send an `http(s)` link and the bot fetches it and uploads it to GoFile.
  Private / internal addresses are blocked (SSRF protection).
- **Batch mode** (`/batch`) - collect several files or links and get **one GoFile folder link**.
  A `SHA256SUMS.txt` file is sent along with the result.
- **Checksums** - SHA-256 and MD5 of every file, cross-checked with the MD5 GoFile returns.
- **Connection check** before each upload (internet + GoFile reachable).
- **Automatic retries** (default 3 attempts) for failed uploads and link downloads.
- **History & stats** - `/history` and `/stats`.
- **Delete from GoFile** button (requires your GoFile account token).
- **Admin tools** - `/ban`, `/unban`, `/banned`, `/broadcast`, global `/stats`.

## Requirements

- Python 3.9+
- `pip install -r requirements.txt` (only `telethon`)

## Getting your credentials

| Value | Where to get it |
|-------|-----------------|
| `BOT_TOKEN` | Talk to [@BotFather](https://t.me/BotFather), send `/newbot`. |
| `API_ID` / `API_HASH` | Log in at [my.telegram.org](https://my.telegram.org) -> **API development tools** -> create an app. |
| Your user ID (for `ADMINS`) | Send any message to [@userinfobot](https://t.me/userinfobot). |
| `GOFILE_TOKEN` (optional) | Your account token on your GoFile profile page. |

## Installation

```bash
git clone https://github.com/MRX7014/gofile-telegram-bot.git
cd gofile-telegram-bot
pip install -r requirements.txt
```

### Termux (Android)

```bash
pkg update && pkg install python git
git clone https://github.com/MRX7014/gofile-telegram-bot.git
cd gofile-telegram-bot
pip install -r requirements.txt
```

## Configuration

Set the values as **environment variables** (recommended - nothing secret ends up in the repo):

```bash
cp .env.example .env      # then edit .env
source .env
python gofile_bot.py
```

...or edit the `CONFIG` block at the top of `gofile_bot.py` (environment variables take priority).
If you edit the file, **do not commit your tokens**.

| Variable | Required | Description |
|----------|:--------:|-------------|
| `API_ID` | yes | Telegram API ID. |
| `API_HASH` | yes | Telegram API hash. |
| `BOT_TOKEN` | yes | Bot token from @BotFather. |
| `ADMINS` | no | Comma-separated user IDs allowed to use admin commands. |
| `ALLOWED_USERS` | no | Comma-separated user IDs allowed to use the bot. Empty = everyone (except banned). |
| `GOFILE_TOKEN` | no | GoFile account token; enables the *Delete from GoFile* button and uploads into your account. |
| `MAX_URL_SIZE_MB` | no | Size limit for files fetched from direct links (default `2048`). |
| `BATCH_MAX` | no | Max files per `/batch` (default `20`). |
| `RETRIES` | no | Upload / link-download attempts (default `3`). |
| `DATA_DIR` | no | Where `data.json`, the session file and temporary downloads are stored (default: next to the script). |

## Usage

Send the bot a file or a direct link, check the info card, and press **Upload**.

| Command | Description |
|---------|-------------|
| `/start` | Welcome message (asks for a language the first time). |
| `/help` | List of commands. |
| `/language` | Change the bot's language. |
| `/batch` | Start a batch: send several files / links, then press **Upload all** to get one folder link. |
| `/history` | Your 10 latest uploads. |
| `/stats` | Your upload statistics (admins also see global stats). |
| `/ban <id>` `/unban <id>` `/banned` | Admin: manage banned users. |
| `/broadcast <text>` | Admin: send a message to every user of the bot. |

Tip: register the commands with @BotFather (`/setcommands`) so they show up in the menu.

## Languages

All user-facing text lives in `locales/<code>.json`, so `gofile_bot.py` itself stays English-only.
The bot ships with `en.json` (English, also the fallback) and `ar.json` (Arabic). Every user picks a
language the first time they use the bot and can change it later with `/language`.

To add a language, copy `locales/en.json` to e.g. `locales/fr.json` and translate the values:

- keep the `{placeholders}` (like `{name}` or `{link}`) and the HTML tags (`<b>`, `<code>`) unchanged;
- set `lang_button` (the button label, e.g. `"🇫🇷 Français"`), `pick_lang` and `need_lang`;
- restart the bot - a new button appears automatically.

`python tests/test_offline.py` checks that every locale has the same keys and placeholders as `en.json`.

## Keeping it running

**Termux:** acquire a wake lock and run the bot inside `tmux` so Android doesn't kill it.

```bash
pkg install tmux
termux-wake-lock
tmux new -s gofilebot
source .env && python gofile_bot.py     # detach with Ctrl+B then D
```

**Linux server (systemd):**

```ini
[Unit]
Description=GoFile Telegram Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/gofile-telegram-bot
EnvironmentFile=/opt/gofile-telegram-bot/.env.systemd
ExecStart=/usr/bin/python3 gofile_bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

(`.env.systemd` uses plain `KEY=value` lines, without `export`.)

## Project layout

```
gofile-telegram-bot/
├── gofile_bot.py        # the bot (single file)
├── locales/
│   ├── en.json          # English texts (fallback)
│   └── ar.json          # Arabic texts
├── tests/test_offline.py
├── requirements.txt
├── .env.example
├── .gitignore
├── LICENSE
└── README.md
```

## How it works

1. You send a file (or a link). The bot shows an info card and waits.
2. On **Upload** it checks that the internet and GoFile are reachable.
3. It downloads the file (Telegram or link), computes SHA-256 / MD5, then streams it to
   `https://upload.gofile.io/uploadfile` in 256 KB chunks, updating the progress bar from the
   bytes actually sent.
4. In batch mode the first file creates a GoFile folder; the following files are uploaded into it
   using the returned `folderId` and guest token.
5. The result message contains the link, checksums and (with a GoFile token) a delete button.

Only one job runs at a time; other requests wait in a queue. Temporary files are removed when a
job ends or is cancelled.

## Tests

The test-suite runs the real upload, batch, retry, cancel, delete and command logic against a
local fake GoFile server and validates the locale files - no Telegram account or internet access required:

```bash
python tests/test_offline.py
```

## Notes & limitations

- GoFile's API has changed in the past. The endpoints used are `POST upload.gofile.io/uploadfile`
  and `DELETE api.gofile.io/contents`; if GoFile changes them, update `UPLOAD_HOST`, `UPLOAD_PATH`
  and `_gofile_delete()` in `gofile_bot.py`.
- Guest uploads cannot be deleted through the bot; set `GOFILE_TOKEN` for that.
- Telegram limits how often a message can be edited, so progress updates every ~2.5 seconds.
- Downloads from links follow redirects but refuse any address that is not publicly routable.

## Credits

- [Sushrut1101/GoFile-Upload](https://github.com/Sushrut1101/GoFile-Upload) - the original shell script that inspired this project.
- [Telethon](https://github.com/LonamiWebs/Telethon) - the Telegram client library.

## License

[MIT](LICENSE)
