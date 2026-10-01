import asyncio
import hashlib
import html
import http.client
import io
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError

# ================================ CONFIG ================================
# Edit the values below, OR set them as environment variables
# (environment variables win - the safer option if you publish this repo).


def _ids(name: str) -> set:
    """Parse a comma-separated list of Telegram user IDs from an environment variable."""
    return {int(x) for x in os.environ.get(name, "").replace(" ", "").split(",") if x}


API_ID = int(os.environ.get("API_ID", "1234567"))                    # https://my.telegram.org
API_HASH = os.environ.get("API_HASH", "PUT_YOUR_API_HASH_HERE")      # https://my.telegram.org
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")   # @BotFather

# Admin Telegram user IDs (get yours from @userinfobot). Admins can use /ban, /unban,
# /banned and /broadcast. To hardcode: ADMINS = {123456789}
ADMINS = _ids("ADMINS")

# Users allowed to use the bot. Empty = everyone (except banned users).
# To hardcode: ALLOWED = {123456789, 987654321}
ALLOWED = _ids("ALLOWED_USERS")

# Optional GoFile account token. Needed for the "Delete from GoFile" button
# (without it, files are uploaded as a guest and can't be deleted by the bot).
GOFILE_TOKEN = os.environ.get("GOFILE_TOKEN", "")

MAX_URL_SIZE = int(os.environ.get("MAX_URL_SIZE_MB", "2048")) * 1024 ** 2  # direct-link limit
BATCH_MAX = int(os.environ.get("BATCH_MAX", "20"))                        # files per /batch
RETRIES = int(os.environ.get("RETRIES", "3"))                             # upload/download attempts
# ========================================================================

UPLOAD_HOST = "upload.gofile.io"
UPLOAD_PATH = "/uploadfile"
API_HOST = "api.gofile.io"
CHUNK_SIZE = 256 * 1024
EDIT_INTERVAL = 2.5  # seconds between progress-bar edits
UA = "Mozilla/5.0 (Linux; Android 13) GoFileBot/2.0"

# Where data.json, the Telegram session and temporary downloads live.
DATA_DIR = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(DATA_DIR, "downloads")
DATA_FILE = os.path.join(DATA_DIR, "data.json")
OLD_LANG_FILE = os.path.join(DATA_DIR, "languages.json")  # migrated automatically if present
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

client = TelegramClient(os.path.join(DATA_DIR, "bot_session"), API_ID, API_HASH)
sem = asyncio.Semaphore(1)  # one upload job at a time

PENDING = {}        # token -> request (single file / batch)
AWAIT_RENAME = {}   # user_id -> token
BATCH = {}          # user_id -> token of the open batch
TASKS = set()       # keep references to background tasks


# ---------------------------------------------------------------- texts
LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
DEFAULT_LANG = "en"


def load_locales() -> dict:
    """Load every locales/<code>.json. Drop a new JSON file there to add a language."""
    out = {}
    for fn in sorted(os.listdir(LOCALES_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(LOCALES_DIR, fn), "r", encoding="utf-8") as f:
                out[fn[:-5]] = json.load(f)
    if DEFAULT_LANG not in out:
        raise SystemExit(f"locales/{DEFAULT_LANG}.json is missing")
    return out


STR = load_locales()


# ================================================================ STORE
def load_store() -> dict:
    store = {"langs": {}, "users": {}, "banned": [], "history": [],
             "stats": {"count": 0, "bytes": 0}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            store.update(json.load(f))
    except FileNotFoundError:
        try:  # migrate languages from the old version
            with open(OLD_LANG_FILE, "r", encoding="utf-8") as f:
                store["langs"] = json.load(f)
        except Exception:
            pass
    except Exception as e:
        print("could not read data.json:", e)
    return store


STORE = load_store()


def save_store():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STORE, f, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


def lang_of(uid):
    return STORE["langs"].get(str(uid))


def tr(uid, key, /, **kw) -> str:
    lang = STORE["langs"].get(str(uid), DEFAULT_LANG)
    text = STR.get(lang, {}).get(key) or STR[DEFAULT_LANG][key]
    return text.format(**kw)


def user_rec(uid) -> dict:
    return STORE["users"].setdefault(
        str(uid), {"first_seen": now_str(), "count": 0, "bytes": 0, "last_upload": ""})


def record_upload(uid, name, size, link, kind, count, delete_ids, sha256="") -> str:
    hid = uuid.uuid4().hex[:8]
    STORE["history"].append({
        "id": hid, "uid": uid, "name": name, "size": size, "link": link, "kind": kind,
        "count": count, "sha256": sha256, "date": now_str(),
        "delete_ids": delete_ids, "deleted": False,
    })
    del STORE["history"][:-1000]
    STORE["stats"]["count"] += count
    STORE["stats"]["bytes"] += size
    u = user_rec(uid)
    u["count"] += count
    u["bytes"] += size
    u["last_upload"] = now_str()
    save_store()
    return hid


def find_history(hid):
    for h in STORE["history"]:
        if h["id"] == hid:
            return h
    return None


# ================================================================ LOGIC
class Cancelled(Exception):
    pass


class TooBig(Exception):
    pass


def human_size(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def short(name: str, n: int = 40) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


def bar(pct: int, width: int = 12) -> str:
    filled = int(width * pct / 100)
    return "▓" * filled + "░" * (width - filled)


def progress_text(uid, key, done, total, t0, head="", attempt=1) -> str:
    if total:
        p = min(100, int(done * 100 / total))
        b, pct, tot = bar(p), f"{p}%", human_size(total)
    else:
        b, pct, tot = bar(0), "…", "?"
    elapsed = max(time.monotonic() - t0, 0.001)
    speed = f"{human_size(done / elapsed)}/s"
    extra = tr(uid, "retry_note", n=attempt, max=RETRIES) if attempt > 1 else ""
    return head + tr(uid, key, bar=b, pct=pct, done=human_size(done), total=tot,
                     speed=speed, extra=extra)


def clean_name(name: str, orig: str):
    name = os.path.basename(name.strip().replace("\\", "/"))
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "", name).strip(" .")
    if not name:
        return None
    if not os.path.splitext(name)[1]:
        name += os.path.splitext(orig)[1]
    return name[:200]


def fmt_of(name: str, fallback: str = "?") -> str:
    return os.path.splitext(name)[1].lstrip(".").upper() or fallback


# ---- connectivity check
async def _can_connect(host: str, port: int, timeout: float = 5) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def check_connection() -> str:
    """Returns 'ok' | 'no_internet' | 'gofile_unreachable'."""
    results = await asyncio.gather(_can_connect("1.1.1.1", 443), _can_connect("8.8.8.8", 443))
    if not any(results):
        return "no_internet"
    if not await _can_connect(UPLOAD_HOST, 443):
        return "gofile_unreachable"
    return "ok"


# ---- retry wrapper (runs inside worker threads)
def with_retry(fn, state):
    last = None
    for attempt in range(1, RETRIES + 1):
        state["attempt"] = attempt
        state["done"] = 0
        try:
            return fn()
        except (Cancelled, PermissionError, TooBig):
            raise
        except Exception as e:
            last = e
            if attempt < RETRIES:
                for _ in range(attempt * 4):  # back off, but stay cancellable
                    if state["flag"]["cancel"]:
                        raise Cancelled()
                    time.sleep(0.5)
    raise last


# ---- GoFile upload with real progress (worker thread)
def _multipart_upload(path: str, filename: str, state: dict, fields: dict) -> dict:
    size = os.path.getsize(path)
    boundary = uuid.uuid4().hex
    parts = b""
    for k, v in fields.items():
        parts += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode("utf-8")
    safe = filename.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")
    head = parts + (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="file"; filename="{safe}"\r\n'
        f'Content-Type: application/octet-stream\r\n\r\n'
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode()

    conn = http.client.HTTPSConnection(UPLOAD_HOST, timeout=300)
    try:
        conn.putrequest("POST", UPLOAD_PATH)
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(len(head) + size + len(tail)))
        if GOFILE_TOKEN:
            conn.putheader("Authorization", f"Bearer {GOFILE_TOKEN}")
        conn.endheaders()

        conn.send(head)
        with open(path, "rb") as f:
            while True:
                if state["flag"]["cancel"]:
                    raise Cancelled()
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                conn.send(chunk)
                state["done"] += len(chunk)
        conn.send(tail)

        resp = conn.getresponse()
        raw = resp.read().decode(errors="ignore").strip()
    finally:
        conn.close()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Bad response ({resp.status}): {raw[:300]}")

    d = data.get("data") or {}
    if data.get("status") != "ok" or not d.get("downloadPage"):
        raise RuntimeError(f"Upload failed: {raw[:300]}")
    return d


def _gofile_delete(ids: list):
    body = json.dumps({"contentsId": ",".join(ids)}).encode()
    conn = http.client.HTTPSConnection(API_HOST, timeout=30)
    try:
        conn.request("DELETE", "/contents", body=body, headers={
            "Authorization": f"Bearer {GOFILE_TOKEN}",
            "Content-Type": "application/json",
        })
        raw = conn.getresponse().read().decode(errors="ignore").strip()
    finally:
        conn.close()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Bad response: {raw[:300]}")
    if data.get("status") != "ok":
        raise RuntimeError(raw[:300])


def _hash_file(path: str, flag: dict):
    md5, sha = hashlib.md5(), hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            if flag["cancel"]:
                raise Cancelled()
            md5.update(chunk)
            sha.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


# ---- direct links
def assert_public_url(url: str):
    """Block non-http(s) URLs and anything that resolves to a private/internal address."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("invalid url")
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(u.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError("cannot resolve host")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise PermissionError("blocked address")


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def name_from_headers(url: str, headers) -> str:
    cd = headers.get("Content-Disposition", "") or ""
    name = ""
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", cd)
    if m:
        name = urllib.parse.unquote(m.group(1))
    else:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd)
        if m:
            name = m.group(1)
    if not name:
        name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
    if not name:
        name = "download"
    mime = headers.get_content_type()
    if not os.path.splitext(name)[1] and mime not in ("application/octet-stream", "binary/octet-stream"):
        name += mimetypes.guess_extension(mime) or ""
    return clean_name(name, name) or "download"


def _probe_url(url: str) -> dict:
    assert_public_url(url)
    opener = urllib.request.build_opener(SafeRedirect())
    last = None
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
            with opener.open(req, timeout=15) as r:
                headers, final = r.headers, r.geturl()
            break
        except PermissionError:
            raise
        except Exception as e:
            last = e
    else:
        raise last
    length = headers.get("Content-Length")
    return {
        "name": name_from_headers(final, headers),
        "size": int(length) if length and length.isdigit() else None,
        "mime": headers.get_content_type(),
    }


def _download_url(url: str, dest: str, state: dict):
    assert_public_url(url)
    opener = urllib.request.build_opener(SafeRedirect())
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with opener.open(req, timeout=30) as r:
        length = r.headers.get("Content-Length")
        if length and length.isdigit():
            state["total"] = int(length)
            if state["total"] > MAX_URL_SIZE:
                raise TooBig("size limit exceeded")
        with open(dest, "wb") as f:
            while True:
                if state["flag"]["cancel"]:
                    raise Cancelled()
                chunk = r.read(CHUNK_SIZE)
                if not chunk:
                    break
                f.write(chunk)
                state["done"] += len(chunk)
                if state["done"] > MAX_URL_SIZE:
                    raise TooBig("size limit exceeded")


# ================================================================ access control
def is_admin(uid) -> bool:
    return uid in ADMINS


def is_banned(uid) -> bool:
    return uid in STORE["banned"]


def allowed(event) -> bool:
    uid = event.sender_id
    if is_admin(uid):
        return True
    if is_banned(uid):
        return False
    return not ALLOWED or uid in ALLOWED


async def touch_user(event):
    u = user_rec(event.sender_id)
    try:
        s = await event.get_sender()
        u["name"] = " ".join(x for x in (s.first_name, s.last_name) if x)[:60]
        u["username"] = s.username or ""
    except Exception:
        pass
    u["last_seen"] = now_str()
    save_store()


# ================================================================ UI helpers
def new_request(uid, mode, token) -> dict:
    return {"uid": uid, "mode": mode, "token": token, "items": [], "card": None,
            "busy": False, "flag": {"cancel": False}, "task": None, "lock": asyncio.Lock()}


def card_text(uid, p) -> str:
    it = p["items"][0]
    return tr(uid, "card", name=html.escape(it["name"]), size=human_size(it["size"]),
              fmt=it["fmt"], date=now_str())


def batch_text(uid, p) -> str:
    items = p["items"]
    if items:
        lst = "\n".join(
            f"{i}. <code>{html.escape(short(it['name']))}</code> • {human_size(it['size'])}"
            for i, it in enumerate(items, 1))
    else:
        lst = tr(uid, "batch_empty_list")
    total = sum(it["size"] or 0 for it in items)
    return tr(uid, "batch_card", list=lst, count=len(items), total=human_size(total))


def idle_text(uid, p) -> str:
    return batch_text(uid, p) if p["mode"] == "batch" else card_text(uid, p)


def idle_buttons(uid, p):
    token = p["token"]
    if p["mode"] == "batch":
        return [
            [Button.inline(tr(uid, "btn_upload_all", n=len(p["items"])), f"up:{token}".encode())],
            [Button.inline(tr(uid, "btn_cancel"), f"cx:{token}".encode())],
        ]
    return [
        [Button.inline(tr(uid, "btn_upload"), f"up:{token}".encode()),
         Button.inline(tr(uid, "btn_rename"), f"rn:{token}".encode())],
        [Button.inline(tr(uid, "btn_cancel"), f"cx:{token}".encode())],
    ]


async def safe_edit(msg, text, buttons=None):
    try:
        await msg.edit(text, buttons=buttons, parse_mode="html")
    except MessageNotModifiedError:
        pass
    except Exception as e:
        print("edit error:", e)


async def ask_language(event, need=False):
    key = "need_lang" if need else "pick_lang"
    codes = sorted(STR)
    text = "\n".join(STR[c][key] for c in codes if key in STR[c])
    await event.respond(
        text,
        buttons=[[Button.inline(STR[c].get("lang_button", c), f"lang:{c}".encode()) for c in codes]],
    )


def item_from_event(event) -> dict:
    f = event.file
    ext = f.ext or ""
    name = f.name
    if not name:
        kind = ("photo" if event.photo else "video" if event.video else
                "voice" if event.voice else "audio" if event.audio else "file")
        name = f"{kind}_{datetime.now():%Y%m%d_%H%M%S}{ext}"
    name = clean_name(name, name) or f"file_{uuid.uuid4().hex[:6]}{ext}"
    return {"kind": "tg", "msg": event.message, "url": None, "name": name, "size": f.size,
            "fmt": fmt_of(name, (f.mime_type or "?").split("/")[-1].upper())}


async def create_single(event, uid, item, status=None):
    token = uuid.uuid4().hex[:10]
    p = new_request(uid, "single", token)
    p["items"].append(item)
    PENDING[token] = p
    text, btns = card_text(uid, p), idle_buttons(uid, p)
    if status is not None:
        await safe_edit(status, text, buttons=btns)
        p["card"] = status
    else:
        p["card"] = await event.reply(text, buttons=btns, parse_mode="html")


async def batch_add(event, uid, item):
    p = PENDING.get(BATCH.get(uid))
    if not p:
        BATCH.pop(uid, None)
        return await create_single(event, uid, item)
    async with p["lock"]:
        if len(p["items"]) >= BATCH_MAX:
            await event.reply(tr(uid, "batch_max", n=BATCH_MAX))
            return
        p["items"].append(item)
        old = p["card"]
        p["card"] = await event.respond(batch_text(uid, p), buttons=idle_buttons(uid, p),
                                        parse_mode="html")
        try:
            await old.delete()
        except Exception:
            pass


URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


async def handle_url(event, uid, url):
    status = await event.reply(tr(uid, "url_probing"), parse_mode="html")
    try:
        info = await asyncio.to_thread(_probe_url, url)
    except PermissionError:
        await safe_edit(status, tr(uid, "url_blocked"))
        return
    except Exception as e:
        await safe_edit(status, tr(uid, "url_bad", err=html.escape(str(e)[:200])))
        return
    if info["size"] and info["size"] > MAX_URL_SIZE:
        await safe_edit(status, tr(uid, "url_too_big", limit=human_size(MAX_URL_SIZE)))
        return
    item = {"kind": "url", "msg": None, "url": url, "name": info["name"], "size": info["size"],
            "fmt": fmt_of(info["name"], info["mime"].split("/")[-1].upper())}
    if uid in BATCH:
        await batch_add(event, uid, item)
        try:
            await status.delete()
        except Exception:
            pass
    else:
        await create_single(event, uid, item, status=status)


# ================================================================ upload job
def spawn_thread(fn, *args):
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    task.add_done_callback(lambda t: t.cancelled() or t.exception())  # silence "never retrieved"
    return task


async def poll(uid, card, task, state, key, head, buttons, finalize=False):
    t0, attempt = time.monotonic(), 1
    while True:
        await asyncio.wait({task}, timeout=EDIT_INTERVAL)
        if task.done():
            return task.result()
        if state["attempt"] != attempt:
            attempt, t0 = state["attempt"], time.monotonic()
        if finalize and state["total"] and state["done"] >= state["total"]:
            text = head + tr(uid, "finalizing")
        else:
            text = progress_text(uid, key, state["done"], state["total"], t0, head, attempt)
        await safe_edit(card, text, buttons)


async def process_item(token, p, item, i, n, work_dir, fields) -> dict:
    uid, card, flag = p["uid"], p["card"], p["flag"]
    head = (tr(uid, "batch_head", i=i, n=n, name=html.escape(short(item["name"]))) + "\n\n") if n > 1 else ""
    stop = [[Button.inline(tr(uid, "btn_stop"), f"xc:{token}".encode())]]
    item_dir = os.path.join(work_dir, str(i))
    os.makedirs(item_dir, exist_ok=True)
    path = os.path.join(item_dir, item["name"])

    # 1) download
    if item["kind"] == "tg":
        t0, last = time.monotonic(), [0.0]
        await safe_edit(card, progress_text(uid, "downloading", 0, item["size"] or 0, t0, head), stop)

        async def dl_progress(cur, total):
            now = time.monotonic()
            if now - last[0] < EDIT_INTERVAL:
                return
            last[0] = now
            await safe_edit(card, progress_text(uid, "downloading", cur, total, t0, head), stop)

        got = await item["msg"].download_media(file=path, progress_callback=dl_progress)
        if not got:
            raise RuntimeError(tr(uid, "dl_fail"))
    else:
        state = {"done": 0, "total": item["size"] or 0, "attempt": 1, "flag": flag}
        task = spawn_thread(with_retry, lambda: _download_url(item["url"], path, state), state)
        await safe_edit(card, progress_text(uid, "downloading_url", 0, state["total"], time.monotonic(), head), stop)
        await poll(uid, card, task, state, "downloading_url", head, stop)
        got = path

    # 2) checksum
    await safe_edit(card, head + tr(uid, "hashing"), stop)
    sha, md5 = await asyncio.to_thread(_hash_file, got, flag)
    real = os.path.getsize(got)

    # 3) upload (real progress = bytes actually sent)
    state = {"done": 0, "total": real, "attempt": 1, "flag": flag}
    task = spawn_thread(with_retry, lambda: _multipart_upload(got, item["name"], state, fields), state)
    await safe_edit(card, progress_text(uid, "uploading", 0, real, time.monotonic(), head), stop)
    data = await poll(uid, card, task, state, "uploading", head, stop, finalize=True)
    return {"ok": True, "name": item["name"], "fmt": item["fmt"], "size": real,
            "sha256": sha, "md5": md5, "data": data}


async def run_job(token, p, work_dir) -> list:
    uid, items = p["uid"], p["items"]
    results, folder, gtoken = [], None, None
    if sem.locked():
        await safe_edit(p["card"], tr(uid, "queued"))
    async with sem:
        for i, item in enumerate(items, 1):
            fields = {}
            if folder:  # 2nd+ file of a batch goes into the same GoFile folder
                fields["folderId"] = folder
                tk = GOFILE_TOKEN or gtoken
                if tk:
                    fields["token"] = tk
            try:
                r = await process_item(token, p, item, i, len(items), work_dir, fields)
                results.append(r)
                if folder is None:
                    folder = r["data"].get("parentFolder")
                    gtoken = r["data"].get("guestToken")
            except Cancelled:
                raise
            except Exception as e:
                results.append({"ok": False, "name": item["name"], "err": str(e)[:300]})
    return results


async def present_results(p, results):
    uid, card = p["uid"], p["card"]
    ok = [r for r in results if r["ok"]]
    if not ok:
        err = "\n".join(f"{short(r['name'], 30)}: {r['err']}" for r in results)
        await safe_edit(card, tr(uid, "error", err=html.escape(err[:1500])))
        return

    date = now_str()
    if len(p["items"]) == 1:
        r = ok[0]
        d = r["data"]
        link = d["downloadPage"]
        verified = " ✔️" if str(d.get("md5", "")).lower() == r["md5"] else ""
        hashes = tr(uid, "hashes", sha=r["sha256"], md5=r["md5"], ok=verified)
        ids = [d["id"]] if d.get("id") else []
        hid = record_upload(uid, r["name"], r["size"], link, "single", 1, ids, r["sha256"])
        text = tr(uid, "done", name=html.escape(r["name"]), size=human_size(r["size"]),
                  fmt=r["fmt"], date=date, hashes=hashes, link=link)
    else:
        first = ok[0]["data"]
        link = first["downloadPage"]
        ids = [first["parentFolder"]] if first.get("parentFolder") else []
        lines = [
            (f"✅ <code>{html.escape(short(r['name']))}</code> • {human_size(r['size'])}" if r["ok"]
             else f"❌ <code>{html.escape(short(r['name']))}</code>")
            for r in results
        ]
        total = sum(r["size"] for r in ok)
        hid = record_upload(uid, f"{len(ok)} files", total, link, "batch", len(ok), ids)
        text = tr(uid, "batch_done", ok=len(ok), n=len(results), list="\n".join(lines),
                  total=human_size(total), date=date, link=link)

    btns = [[Button.url(tr(uid, "btn_open"), link)]]
    if GOFILE_TOKEN and ids:
        btns.append([Button.inline(tr(uid, "btn_delete"), f"dl:{hid}".encode())])
    await safe_edit(card, text, buttons=btns)

    if len(p["items"]) > 1:
        try:
            sums = "".join(f"{r['sha256']}  {r['name']}\n" for r in ok)
            bio = io.BytesIO(sums.encode())
            bio.name = "SHA256SUMS.txt"
            await client.send_file(uid, bio, caption=tr(uid, "sums_caption"), force_document=True)
        except Exception as e:
            print("could not send checksums file:", e)


async def do_upload(token, p):
    uid, card = p["uid"], p["card"]
    p["busy"] = True
    p["flag"]["cancel"] = False
    if BATCH.get(uid) == token:
        BATCH.pop(uid, None)
    keep = False
    work_dir = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex)
    try:
        # 0) connectivity check (before anything heavy)
        await safe_edit(card, tr(uid, "checking"))
        status = await check_connection()
        if status != "ok":
            keep = True
            p["busy"] = False
            if p["mode"] == "batch":
                BATCH[uid] = token
            await safe_edit(card, tr(uid, status) + "\n\n" + idle_text(uid, p),
                            buttons=idle_buttons(uid, p))
            return

        os.makedirs(work_dir, exist_ok=True)
        job = asyncio.create_task(run_job(token, p, work_dir))
        p["task"] = job
        results = await job
        await present_results(p, results)
    except (asyncio.CancelledError, Cancelled):
        if p["flag"]["cancel"]:
            await safe_edit(card, tr(uid, "cancelled"))
        else:
            raise
    except Exception as e:
        await safe_edit(card, tr(uid, "error", err=html.escape(str(e)[:1500])))
    finally:
        if not keep:
            PENDING.pop(token, None)
        shutil.rmtree(work_dir, ignore_errors=True)


# ================================================================ commands
async def cmd_start(event, uid, args):
    await touch_user(event)
    if lang_of(uid):
        await event.respond(tr(uid, "welcome"), parse_mode="html")
    else:
        await ask_language(event)


async def cmd_language(event, uid, args):
    await ask_language(event)


async def cmd_help(event, uid, args):
    text = tr(uid, "help") + (tr(uid, "help_admin") if is_admin(uid) else "")
    await event.respond(text, parse_mode="html")


async def cmd_batch(event, uid, args):
    old = BATCH.pop(uid, None)
    if old:
        op = PENDING.pop(old, None)
        if op:
            await safe_edit(op["card"], tr(uid, "cancelled"))
    token = uuid.uuid4().hex[:10]
    p = new_request(uid, "batch", token)
    PENDING[token] = p
    BATCH[uid] = token
    p["card"] = await event.respond(batch_text(uid, p), buttons=idle_buttons(uid, p),
                                    parse_mode="html")


async def cmd_history(event, uid, args):
    hs = [h for h in reversed(STORE["history"]) if h["uid"] == uid and not h.get("deleted")][:10]
    if not hs:
        await event.respond(tr(uid, "hist_empty"))
        return
    lines = [
        f'{i}. <a href="{html.escape(h["link"])}">{html.escape(short(h["name"], 35))}</a>'
        f' • {human_size(h["size"])} • {h["date"]}'
        for i, h in enumerate(hs, 1)
    ]
    await event.respond(tr(uid, "hist_title") + "\n\n" + "\n".join(lines),
                        parse_mode="html", link_preview=False)


async def cmd_stats(event, uid, args):
    u = user_rec(uid)
    text = tr(uid, "stats_user", count=u["count"], total=human_size(u["bytes"]),
              last=u.get("last_upload") or tr(uid, "never"))
    if is_admin(uid):
        text += tr(uid, "stats_global", users=len(STORE["users"]), count=STORE["stats"]["count"],
                   total=human_size(STORE["stats"]["bytes"]), banned=len(STORE["banned"]))
    await event.respond(text, parse_mode="html")


def parse_id(args: str):
    try:
        return int(args.split()[0])
    except (ValueError, IndexError):
        return None


async def cmd_ban(event, uid, args):
    if not is_admin(uid):
        return await event.respond(tr(uid, "admin_only"))
    target = parse_id(args)
    if target is None:
        return await event.respond(tr(uid, "ban_usage"), parse_mode="html")
    if is_admin(target):
        return await event.respond(tr(uid, "cant_ban_admin"))
    if target not in STORE["banned"]:
        STORE["banned"].append(target)
        save_store()
    await event.respond(tr(uid, "banned_ok", uid=target), parse_mode="html")


async def cmd_unban(event, uid, args):
    if not is_admin(uid):
        return await event.respond(tr(uid, "admin_only"))
    target = parse_id(args)
    if target is None:
        return await event.respond(tr(uid, "unban_usage"), parse_mode="html")
    if target in STORE["banned"]:
        STORE["banned"].remove(target)
        save_store()
    await event.respond(tr(uid, "unbanned_ok", uid=target), parse_mode="html")


async def cmd_banned(event, uid, args):
    if not is_admin(uid):
        return await event.respond(tr(uid, "admin_only"))
    if not STORE["banned"]:
        return await event.respond(tr(uid, "banned_none"))
    lst = "\n".join(f"• <code>{b}</code>" for b in STORE["banned"])
    await event.respond(tr(uid, "banned_list", list=lst), parse_mode="html")


async def cmd_broadcast(event, uid, args):
    if not is_admin(uid):
        return await event.respond(tr(uid, "admin_only"))
    if not args:
        return await event.respond(tr(uid, "broadcast_usage"), parse_mode="html")
    ok = fail = 0
    for target in list(STORE["users"]):
        if int(target) in STORE["banned"]:
            continue
        try:
            await client.send_message(int(target), args)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.1)
    await event.respond(tr(uid, "broadcast_done", ok=ok, fail=fail))


COMMANDS = {
    "start": cmd_start, "language": cmd_language, "help": cmd_help, "batch": cmd_batch,
    "history": cmd_history, "stats": cmd_stats, "ban": cmd_ban, "unban": cmd_unban,
    "banned": cmd_banned, "broadcast": cmd_broadcast,
}


# ================================================================ handlers
@client.on(events.NewMessage(pattern=r"(?s)^/(\w+)(?:@\w+)?(?:\s+(.*))?$",
                             func=lambda e: e.is_private))
async def on_command(event):
    if not allowed(event):
        return
    uid = event.sender_id
    cmd = event.pattern_match.group(1).lower()
    args = (event.pattern_match.group(2) or "").strip()
    handler = COMMANDS.get(cmd)
    if not handler:
        return
    if cmd not in ("start", "language") and not lang_of(uid):
        await ask_language(event, need=True)
        return
    await handler(event, uid, args)


@client.on(events.CallbackQuery(pattern=rb"^lang:([a-z_]+)$"))
async def on_lang(event):
    if not allowed(event):
        return
    uid = event.sender_id
    code = event.data_match.group(1).decode()
    if code not in STR:
        await event.answer()
        return
    STORE["langs"][str(uid)] = code
    save_store()
    await event.edit(tr(uid, "lang_set") + "\n\n" + tr(uid, "welcome"), parse_mode="html")


@client.on(events.NewMessage(func=lambda e: e.is_private and e.file is not None))
async def on_file(event):
    if not allowed(event):
        return
    uid = event.sender_id
    if not lang_of(uid):
        await ask_language(event, need=True)
        return
    await touch_user(event)
    item = item_from_event(event)
    if uid in BATCH:
        await batch_add(event, uid, item)
    else:
        await create_single(event, uid, item)


@client.on(events.NewMessage(func=lambda e: e.is_private and e.file is None
                             and bool(e.raw_text) and not e.raw_text.startswith("/")))
async def on_text(event):
    if not allowed(event):
        return
    uid = event.sender_id

    # rename flow
    token = AWAIT_RENAME.get(uid)
    if token:
        p = PENDING.get(token)
        if not p or p["busy"]:
            AWAIT_RENAME.pop(uid, None)
            return
        item = p["items"][0]
        new = clean_name(event.raw_text, item["name"])
        if not new:
            await event.reply(tr(uid, "invalid_name"))
            return
        item["name"] = new
        item["fmt"] = fmt_of(new, item["fmt"])
        AWAIT_RENAME.pop(uid, None)
        await safe_edit(p["card"], card_text(uid, p), buttons=idle_buttons(uid, p))
        try:
            await event.delete()
        except Exception:
            pass
        return

    # direct link
    m = URL_RE.search(event.raw_text)
    if m:
        if not lang_of(uid):
            await ask_language(event, need=True)
            return
        await touch_user(event)
        await handle_url(event, uid, m.group(0))


async def handle_delete_action(event, uid, action, hid):
    h = find_history(hid)
    if not h or h["uid"] != uid:
        await event.answer(tr(uid, "expired"), alert=True)
        return
    if h.get("deleted"):
        await event.answer(tr(uid, "already_deleted"), alert=True)
        return
    if action == "dl":
        await event.answer()
        await event.respond(
            tr(uid, "confirm_delete", name=html.escape(short(h["name"]))),
            buttons=[[Button.inline(tr(uid, "btn_yes"), f"dy:{hid}".encode()),
                      Button.inline(tr(uid, "btn_no"), f"dn:{hid}".encode())]],
            parse_mode="html", reply_to=event.message_id)
    elif action == "dn":
        await event.answer()
        await event.delete()
    else:  # dy
        await event.answer()
        try:
            await asyncio.to_thread(_gofile_delete, h["delete_ids"])
            h["deleted"] = True
            save_store()
            await event.edit(tr(uid, "deleted"), parse_mode="html")
        except Exception as e:
            await event.edit(tr(uid, "delete_fail", err=html.escape(str(e)[:300])), parse_mode="html")


@client.on(events.CallbackQuery(pattern=rb"^(up|rn|cx|bk|xc|dl|dy|dn):(\w+)$"))
async def on_action(event):
    if not allowed(event):
        return
    uid = event.sender_id
    action = event.data_match.group(1).decode()
    token = event.data_match.group(2).decode()

    if action in ("dl", "dy", "dn"):
        await handle_delete_action(event, uid, action, token)
        return

    p = PENDING.get(token)
    if not p:
        await event.answer(tr(uid, "expired"), alert=True)
        return
    if p["uid"] != uid:
        await event.answer("⛔", alert=True)
        return

    if action == "xc":  # stop a running job
        p["flag"]["cancel"] = True
        await event.answer("⏹")
        if p["task"] and not p["task"].done():
            p["task"].cancel()
        return

    if p["busy"]:
        await event.answer()
        return

    if action == "cx":
        PENDING.pop(token, None)
        if BATCH.get(uid) == token:
            BATCH.pop(uid, None)
        if AWAIT_RENAME.get(uid) == token:
            AWAIT_RENAME.pop(uid, None)
        await event.answer()
        await safe_edit(p["card"], tr(uid, "cancelled"))

    elif action == "rn":
        AWAIT_RENAME[uid] = token
        await event.answer()
        await safe_edit(
            p["card"],
            tr(uid, "ask_rename", name=html.escape(p["items"][0]["name"])),
            buttons=[[Button.inline(tr(uid, "btn_back"), f"bk:{token}".encode())]],
        )

    elif action == "bk":
        AWAIT_RENAME.pop(uid, None)
        await event.answer()
        await safe_edit(p["card"], card_text(uid, p), buttons=idle_buttons(uid, p))

    elif action == "up":
        if not p["items"]:
            await event.answer(tr(uid, "batch_empty"), alert=True)
            return
        AWAIT_RENAME.pop(uid, None)
        await event.answer()
        t = asyncio.create_task(do_upload(token, p))
        TASKS.add(t)
        t.add_done_callback(TASKS.discard)


async def main():
    if API_ID == 1234567 or "PUT_YOUR" in API_HASH or "PUT_YOUR" in BOT_TOKEN:
        raise SystemExit("Please set API_ID, API_HASH and BOT_TOKEN (see README.md).")
    await client.start(bot_token=BOT_TOKEN)
    print("Bot is running...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
