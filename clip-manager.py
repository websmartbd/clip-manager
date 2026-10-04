#!/usr/bin/env python3
"""
clip-manager.py — a Windows-11-style clipboard history manager for Linux/GTK.
Now with image support and a redesigned UI.

USAGE
  python3 clip-manager.py run     # start the background daemon (monitors clipboard)
  python3 clip-manager.py show    # show the popup (auto-starts the daemon if needed)

Bind `python3 /path/to/clip-manager.py show` to Super+V in
Settings > Keyboard > Custom Shortcuts.

Storage: ~/.local/share/clip-manager/history.db   (SQLite)
Images:  ~/.local/share/clip-manager/images/*.png
Socket:  ~/.cache/clip-manager/clip.sock
"""

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf, Pango

import os
import sys
import time
import uuid
import base64
import fcntl
import signal
import socket
import struct
import hashlib
import sqlite3
import traceback
import threading
import subprocess
from datetime import datetime

APP_DIR = os.path.join(GLib.get_user_data_dir(), "clip-manager")
IMG_DIR = os.path.join(APP_DIR, "images")
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "clip-manager")
DB_PATH = os.path.join(APP_DIR, "history.db")
SOCK_PATH = os.path.join(CACHE_DIR, "clip.sock")
PID_PATH = os.path.join(CACHE_DIR, "daemon.pid")
LOG_PATH = os.path.join(CACHE_DIR, "daemon.log")

_MAX_FRAME_SIZE = 20 * 1024 * 1024  # 20MB, generous for even large clipboard text


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _recv_framed(sock):
    """Read one length-prefixed message: 4-byte big-endian length, then that
    many bytes of UTF-8 payload. Returns None on a malformed/short read."""
    header = _recv_exact(sock, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length <= 0 or length > _MAX_FRAME_SIZE:
        return None
    payload = _recv_exact(sock, length)
    if payload is None:
        return None
    return payload.decode("utf-8", "ignore")


def _send_framed(sock, message):
    payload = message.encode("utf-8")
    sock.sendall(struct.pack(">I", len(payload)) + payload)
MAX_UNPINNED = 300
POLL_INTERVAL_MS = 300
THUMB_SIZE = 64
# Bumped whenever a fix changes daemon behavior, so a running (old) daemon
# process can be told apart from the current code on disk -- editing this
# file has no effect on a daemon that's already running in the background.
SCRIPT_BUILD = "2026-10-02-clear-no-confirm"


def log_error(context):
    """Append a timestamped traceback to the daemon log so failures are visible."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"\n--- {datetime.now().isoformat()} [{context}] ---\n")
            f.write(traceback.format_exc())
    except Exception:
        pass

CSS = b"""
window.clip-window { background-color: transparent; }
window.clip-window.solid-bg { background-color: #f7f8fa; }

.card {
    background-color: alpha(#ffffff, 0.99);
    border-radius: 18px;
    border: 1px solid #d5d7db;
    box-shadow: 0 10px 30px alpha(#000000, 0.18);
}
.card.solid-fallback {
    background-color: #f7f8fa;
    border: 1px solid #d5d7db;
}

.titlebar-row {
    padding: 14px 16px 8px 16px;
}
.drag-handle {
    background-color: transparent;
}
.title-text {
    color: #1f2124;
    font-weight: 700;
    font-size: 13.5px;
}
.count-badge {
    color: #6b6e74;
    font-size: 11px;
}
.close-btn {
    background: alpha(#ffffff, 0.4);
    border: none;
    color: #6b6e74;
    border-radius: 999px;
    padding: 2px 6px;
}
.close-btn:hover { background-color: alpha(#ffffff, 0.8); color: #1f2124; }

.search-wrap { padding: 0 16px 8px 16px; }
entry.clip-search {
    background-color: alpha(#ffffff, 0.97);
    color: #1f2124;
    border: 1px solid alpha(#000000, 0.09);
    border-radius: 12px;
    padding: 7px 10px;
    caret-color: #2f7ff0;
}
entry.clip-search:focus { border-color: #2f7ff0; background-color: #ffffff; }

.filter-bar { padding: 0 16px 10px 16px; }
togglebutton.filter-chip {
    background-color: alpha(#000000, 0.06);
    color: #55585e;
    border: 1px solid alpha(#000000, 0.09);
    border-radius: 999px;
    padding: 3px 12px;
    font-size: 11.5px;
    margin-right: 6px;
}
togglebutton.filter-chip:hover { background-color: alpha(#000000, 0.11); }
togglebutton.filter-chip:checked {
    background-color: #2f7ff0;
    color: #ffffff;
    border-color: #2f7ff0;
}

scrolledwindow.clip-scroll { padding: 0 10px 8px 10px; }

list.clip-list { background-color: transparent; }
list.clip-list row {
    background-color: transparent;
    padding: 0;
    margin: 3px 4px;
}
list.clip-list row.clip-row-box {
    background-color: alpha(#000000, 0.055);
    border: 1px solid alpha(#000000, 0.08);
    border-radius: 12px;
}
list.clip-list row:hover.clip-row-box { background-color: alpha(#000000, 0.10); }
list.clip-list row:selected { background-color: transparent; }
list.clip-list row:selected .row-inner { background-color: alpha(#2f7ff0, 0.12); border: 1px solid #2f7ff0; }

.row-inner { padding: 9px 9px; border-radius: 11px; border: 1px solid transparent; }
.row-text { color: #26282c; font-size: 12.5px; }
.row-time { color: #8a8d93; font-size: 10px; }

button.icon-btn {
    background: transparent;
    border: none;
    border-radius: 999px;
    padding: 4px;
    color: #8a8d93;
    min-width: 22px;
    min-height: 22px;
}
button.icon-btn:hover { background-color: alpha(#ffffff, 0.9); color: #26282c; }
button.icon-btn.pinned { color: #d9a400; }
button.icon-btn.delete-btn:hover { background-color: alpha(#e04b4b, 0.15); color: #d33; }

.hint-bar {
    padding: 7px 16px 12px 16px;
    color: #8a8d93;
    font-size: 10.5px;
    border-top: 1px solid alpha(#000000, 0.07);
}

.empty-state { color: #9a9da3; padding: 40px 0; font-size: 12.5px; }

image.thumb {
    border-radius: 8px;
}
"""


def ensure_dirs():
    os.makedirs(APP_DIR, exist_ok=True)
    os.makedirs(IMG_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)


def get_db():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL DEFAULT 'text',
            content TEXT NOT NULL,
            hash TEXT NOT NULL UNIQUE,
            pinned INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )"""
    )
    conn.commit()
    _migrate_schema(conn)
    return conn


def _migrate_schema(conn):
    """Add columns that older versions of this script's DB may be missing,
    and merge any duplicate-content rows so the same clipboard item never
    ends up saved more than once."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(history)").fetchall()}
    if "type" not in cols:
        conn.execute("ALTER TABLE history ADD COLUMN type TEXT NOT NULL DEFAULT 'text'")
    if "hash" not in cols:
        conn.execute("ALTER TABLE history ADD COLUMN hash TEXT")
        # backfill hashes for pre-existing rows so the UNIQUE-by-hash logic works
        for row_id, content in conn.execute(
            "SELECT id, content FROM history WHERE hash IS NULL"
        ).fetchall():
            h = hashlib.sha256(str(content).encode("utf-8", "ignore")).hexdigest()
            conn.execute("UPDATE history SET hash = ? WHERE id = ?", (h, row_id))
        conn.commit()

    # Merge any rows that share the same content hash (leftover duplicates
    # from before de-duplication existed, or from any gap in it) into a
    # single row, keeping pinned status and the most recent timestamp.
    dupe_hashes = [
        row[0]
        for row in conn.execute(
            "SELECT hash FROM history WHERE hash IS NOT NULL "
            "GROUP BY hash HAVING COUNT(*) > 1"
        ).fetchall()
    ]
    for h in dupe_hashes:
        rows = conn.execute(
            "SELECT id, type, content, pinned, created_at FROM history WHERE hash = ? "
            "ORDER BY pinned DESC, created_at DESC",
            (h,),
        ).fetchall()
        keep_id = rows[0][0]
        keep_content = rows[0][2]
        any_pinned = any(r[3] for r in rows)
        newest_created_at = max(r[4] for r in rows)
        for row_id, row_type, content, _pinned, _created_at in rows[1:]:
            if row_type == "image" and content != keep_content and os.path.exists(content):
                try:
                    os.remove(content)
                except OSError:
                    pass
            conn.execute("DELETE FROM history WHERE id = ?", (row_id,))
        conn.execute(
            "UPDATE history SET pinned = ?, created_at = ? WHERE id = ?",
            (1 if any_pinned else 0, newest_created_at, keep_id),
        )

    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_history_hash ON history(hash)"
        )
    except sqlite3.IntegrityError:
        pass
    conn.commit()


class ClipboardDaemon:
    def __init__(self):
        self.db = get_db()
        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.last_text_hash = None
        self.last_image_hash = None
        self.window = None
        # Primary: react the instant any app takes clipboard ownership (i.e.
        # the moment of every copy/cut), instead of waiting for the next poll
        # tick. This avoids the race where a copy happens and gets read
        # before the source app finishes rendering it, or two rapid copies
        # land in the same poll window and one is lost.
        self.clipboard.connect("owner-change", self.on_owner_change)
        # Fallback: some apps/environments don't reliably emit owner-change,
        # so keep a slow poll as a safety net.
        GLib.timeout_add(POLL_INTERVAL_MS, self.poll_clipboard)

    def on_owner_change(self, clipboard, event):
        # Read right away, then re-check a couple more times shortly after:
        # some apps briefly claim clipboard ownership before their data is
        # fully ready to serve, so the very first read can occasionally come
        # back stale or empty. These follow-ups catch that without adding a
        # noticeable delay for the common case where the app is ready
        # immediately.
        self.poll_clipboard()
        GLib.timeout_add(120, self._recheck_clipboard)
        GLib.timeout_add(400, self._recheck_clipboard)
        return False

    def _recheck_clipboard(self):
        self.poll_clipboard()
        return False

    def poll_clipboard(self):
        try:
            text = self.clipboard.wait_for_text()
            if text and text.strip():
                h = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
                if h != self.last_text_hash:
                    self.last_text_hash = h
                    self.last_image_hash = None
                    self.save_text(text, h)
                return True

            if self.clipboard.wait_is_image_available():
                pixbuf = self.clipboard.wait_for_image()
                if pixbuf is not None:
                    h = hashlib.sha256(pixbuf.get_pixels()).hexdigest()
                    if h != self.last_image_hash:
                        self.last_image_hash = h
                        self.last_text_hash = None
                        self.save_image(pixbuf, h)
        except Exception:
            pass
        return True

    def save_text(self, text, h):
        try:
            existing = self.db.execute(
                "SELECT id FROM history WHERE hash = ?", (h,)
            ).fetchone()
            if existing:
                self.db.execute(
                    "UPDATE history SET created_at = ? WHERE id = ?",
                    (time.time(), existing[0]),
                )
            else:
                self.db.execute(
                    "INSERT INTO history (type, content, hash, pinned, created_at) "
                    "VALUES ('text', ?, ?, 0, ?)",
                    (text, h, time.time()),
                )
            self.db.commit()
            self.trim_history()
        except sqlite3.Error:
            pass

    def save_image(self, pixbuf, h):
        try:
            existing = self.db.execute(
                "SELECT content FROM history WHERE hash = ?", (h,)
            ).fetchone()
            if existing:
                self.db.execute(
                    "UPDATE history SET created_at = ? WHERE hash = ?", (time.time(), h)
                )
            else:
                fname = f"{uuid.uuid4().hex}.png"
                path = os.path.join(IMG_DIR, fname)
                pixbuf.savev(path, "png", [], [])
                self.db.execute(
                    "INSERT INTO history (type, content, hash, pinned, created_at) "
                    "VALUES ('image', ?, ?, 0, ?)",
                    (path, h, time.time()),
                )
            self.db.commit()
            self.trim_history()
        except Exception:
            pass

    def save_image_bytes(self, data, ext, h):
        try:
            existing = self.db.execute(
                "SELECT content FROM history WHERE hash = ?", (h,)
            ).fetchone()
            if existing:
                self.db.execute(
                    "UPDATE history SET created_at = ? WHERE hash = ?", (time.time(), h)
                )
            else:
                fname = f"{uuid.uuid4().hex}.{ext}"
                path = os.path.join(IMG_DIR, fname)
                with open(path, "wb") as f:
                    f.write(data)
                self.db.execute(
                    "INSERT INTO history (type, content, hash, pinned, created_at) "
                    "VALUES ('image', ?, ?, 0, ?)",
                    (path, h, time.time()),
                )
            self.db.commit()
            self.trim_history()
        except Exception:
            pass

    def trim_history(self):
        stale = self.db.execute(
            """SELECT id, type, content FROM history WHERE pinned = 0 AND id NOT IN (
                SELECT id FROM history WHERE pinned = 0
                ORDER BY created_at DESC LIMIT ?
            )""",
            (MAX_UNPINNED,),
        ).fetchall()
        for row_id, row_type, content in stale:
            if row_type == "image" and os.path.exists(content):
                try:
                    os.remove(content)
                except OSError:
                    pass
            self.db.execute("DELETE FROM history WHERE id = ?", (row_id,))
        self.db.commit()

    def start_socket_server(self):
        if os.path.exists(SOCK_PATH):
            os.remove(SOCK_PATH)
        # Store this on self (not just a local variable) so it's anchored to
        # the long-lived daemon object and can never be garbage-collected
        # out from under the accept loop. A local variable here was getting
        # closed immediately after listen() succeeded, silently turning the
        # daemon into one that looked alive but refused every connection.
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(SOCK_PATH)
        self.srv.listen(5)
        srv = self.srv

        def loop():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError as e:
                    # Don't let a single transient error permanently kill
                    # the accept loop -- log it and keep going. Previously
                    # an unhandled exception here silently ended this
                    # thread forever while the rest of the daemon (and the
                    # process) kept running, which looked like a healthy
                    # daemon that simply refused every connection.
                    try:
                        with open(LOG_PATH, "a") as f:
                            f.write(
                                f"{datetime.now().isoformat()} "
                                f"[socket accept error] {type(e).__name__}: {e}\n"
                            )
                    except Exception:
                        pass
                    time.sleep(0.5)
                    continue
                try:
                    data = _recv_framed(conn)
                except OSError:
                    conn.close()
                    continue
                if data is None:
                    conn.close()
                    continue

                if data == "show":
                    conn.close()
                    try:
                        with open(LOG_PATH, "a") as f:
                            f.write(f"{datetime.now().isoformat()} [socket] received 'show', scheduling idle_add\n")
                    except Exception:
                        pass
                    GLib.idle_add(self.show_popup)
                elif data == "version":
                    try:
                        conn.sendall(SCRIPT_BUILD.encode())
                    except OSError:
                        pass
                    conn.close()
                elif data.startswith("text:"):
                    conn.close()
                    GLib.idle_add(self._capture_external_text, data[len("text:"):])
                elif data.startswith("image:"):
                    conn.close()
                    GLib.idle_add(self._capture_external_image, data[len("image:"):])
                else:
                    conn.close()

        threading.Thread(target=loop, daemon=True).start()

    def _capture_external_text(self, text):
        # Fed by the GNOME Shell extension, which can see clipboard changes
        # even when this daemon has no focused window (Wayland normally
        # blocks that for ordinary background apps). Reuses the same
        # hash-dedup path as the regular poller.
        if not text or not text.strip():
            return False
        h = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
        if h != self.last_text_hash:
            self.last_text_hash = h
            self.last_image_hash = None
            self.save_text(text, h)
        return False

    def _capture_external_image(self, payload):
        # payload is "<mimetype>:<base64-encoded-bytes>", fed by the GNOME
        # Shell extension the same way _capture_external_text is.
        try:
            mimetype, b64data = payload.split(":", 1)
            raw = base64.b64decode(b64data)
        except Exception:
            return False
        if not raw:
            return False
        ext = mimetype.split("/")[-1].lower() if "/" in mimetype else "png"
        if ext not in ("png", "jpeg", "jpg", "gif", "bmp", "webp"):
            ext = "png"
        h = hashlib.sha256(raw).hexdigest()
        if h != self.last_image_hash:
            self.last_image_hash = h
            self.last_text_hash = None
            self.save_image_bytes(raw, ext, h)
        return False

    def show_popup(self):
        try:
            with open(LOG_PATH, "a") as f:
                f.write(f"{datetime.now().isoformat()} [show_popup] called, window is None: {self.window is None}\n")
        except Exception:
            pass
        try:
            if self.window is None:
                self.window = HistoryWindow(self.db, self.clipboard)
            self.window.refresh_and_present()
        except Exception:
            log_error("show_popup")
        return False


class HistoryWindow(Gtk.Window):
    def __init__(self, db, clipboard):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.db = db
        self.clipboard = clipboard
        self.current_filter = "all"
        self._drag_offset = None
        self._dragging = False
        self._shown_at = 0
        self._dialog_open = False

        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_default_size(400, 520)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.get_style_context().add_class("clip-window")

        self._setup_transparency()
        self._apply_css()

        self.connect("focus-out-event", self.on_focus_out)
        self.connect("key-press-event", self.on_key_press)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.get_style_context().add_class("card")
        self.card_box = card
        self.add(card)

        # ---- title bar ----
        titlebar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        titlebar.get_style_context().add_class("titlebar-row")

        # Everything except the close button lives inside this EventBox so the
        # window can be dragged to a new position by clicking and holding here.
        # (The window is undecorated, so without this there is no way to move it.)
        drag_handle = Gtk.EventBox()
        drag_handle.get_style_context().add_class("drag-handle")
        drag_handle.set_above_child(True)
        drag_handle.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.BUTTON1_MOTION_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        drag_handle.connect("button-press-event", self.on_drag_press)
        drag_handle.connect("motion-notify-event", self.on_drag_motion)
        drag_handle.connect("button-release-event", self.on_drag_release)
        drag_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        drag_handle.add(drag_inner)

        title = Gtk.Label(label="Clipboard History")
        title.get_style_context().add_class("title-text")
        title.set_halign(Gtk.Align.START)
        drag_inner.pack_start(title, False, False, 0)

        self.count_label = Gtk.Label(label="")
        self.count_label.get_style_context().add_class("count-badge")
        self.count_label.set_margin_start(8)
        drag_inner.pack_start(self.count_label, False, False, 0)

        spacer = Gtk.Box()
        drag_inner.pack_start(spacer, True, True, 0)

        titlebar.pack_start(drag_handle, True, True, 0)

        clear_btn = Gtk.Button()
        clear_btn.set_image(Gtk.Image.new_from_icon_name("edit-clear-all-symbolic", Gtk.IconSize.BUTTON))
        clear_btn.set_tooltip_text("Clear history (keeps pinned items)")
        clear_btn.get_style_context().add_class("close-btn")
        clear_btn.set_relief(Gtk.ReliefStyle.NONE)
        clear_btn.set_margin_end(4)
        clear_btn.connect("clicked", self.on_clear_clicked)
        titlebar.pack_start(clear_btn, False, False, 0)

        close_btn = Gtk.Button(label="✕")
        close_btn.get_style_context().add_class("close-btn")
        close_btn.set_relief(Gtk.ReliefStyle.NONE)
        close_btn.connect("clicked", lambda b: self.hide_window())
        titlebar.pack_start(close_btn, False, False, 0)
        card.pack_start(titlebar, False, False, 0)

        # ---- search ----
        search_wrap = Gtk.Box()
        search_wrap.get_style_context().add_class("search-wrap")
        self.search = Gtk.SearchEntry()
        self.search.get_style_context().add_class("clip-search")
        self.search.set_placeholder_text("Search clipboard…")
        self.search.connect("search-changed", lambda e: self.listbox.invalidate_filter())
        self.search.connect("activate", self.on_search_activate)
        search_wrap.pack_start(self.search, True, True, 0)
        card.pack_start(search_wrap, False, False, 0)

        # ---- filter chips (created, wired up after listbox exists) ----
        filter_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        filter_bar.get_style_context().add_class("filter-bar")
        self.chip_all = Gtk.ToggleButton(label="All")
        self.chip_text = Gtk.ToggleButton(label="Text")
        self.chip_image = Gtk.ToggleButton(label="Images")
        for chip in (self.chip_all, self.chip_text, self.chip_image):
            chip.get_style_context().add_class("filter-chip")
            chip.set_relief(Gtk.ReliefStyle.NONE)
            filter_bar.pack_start(chip, False, False, 0)
        card.pack_start(filter_bar, False, False, 0)

        # ---- list ----
        scroller = Gtk.ScrolledWindow()
        scroller.get_style_context().add_class("clip-scroll")
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        card.pack_start(scroller, True, True, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.get_style_context().add_class("clip-list")
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_filter_func(self.filter_func)
        self.listbox.connect("row-activated", self.on_row_activated)
        scroller.add(self.listbox)

        # now that self.listbox exists, it's safe to wire up + activate the chips
        self.chip_all.connect("toggled", self.on_filter_toggled, "all")
        self.chip_text.connect("toggled", self.on_filter_toggled, "text")
        self.chip_image.connect("toggled", self.on_filter_toggled, "image")
        self.chip_all.set_active(True)

        hint = Gtk.Label(label="Enter: copy    Esc: close    📌 pin    🗑 delete    🧹 clear")
        hint.get_style_context().add_class("hint-bar")
        hint.set_halign(Gtk.Align.START)
        card.pack_start(hint, False, False, 0)

    def _setup_transparency(self):
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        composited = bool(visual and screen.is_composited())
        if composited:
            self.set_visual(visual)
        else:
            self.get_style_context().add_class("solid-bg")
            GLib.idle_add(self._mark_card_fallback)

    def _mark_card_fallback(self):
        if hasattr(self, "card_box"):
            self.card_box.get_style_context().add_class("solid-fallback")
        return False

    def _apply_css(self):
        try:
            provider = Gtk.CssProvider()
            provider.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        except Exception:
            log_error("apply_css")

    def on_filter_toggled(self, btn, key):
        if not btn.get_active():
            if not any(c.get_active() for c in (self.chip_all, self.chip_text, self.chip_image)):
                btn.set_active(True)
            return
        for chip in (self.chip_all, self.chip_text, self.chip_image):
            if chip is not btn:
                chip.handler_block_by_func(self.on_filter_toggled)
                chip.set_active(False)
                chip.handler_unblock_by_func(self.on_filter_toggled)
        self.current_filter = key
        self.listbox.invalidate_filter()

    def filter_func(self, row):
        if getattr(row, "is_placeholder", False):
            return True
        if self.current_filter != "all" and row.row_type != self.current_filter:
            return False
        query = self.search.get_text().lower().strip()
        if not query or row.row_type == "image":
            return True
        return query in row.full_text.lower()

    def load_rows(self):
        for child in self.listbox.get_children():
            self.listbox.remove(child)

        cur = self.db.execute(
            "SELECT id, type, content, pinned, created_at FROM history "
            "ORDER BY pinned DESC, created_at DESC LIMIT 500"
        )
        rows = cur.fetchall()
        self.count_label.set_text(f"{len(rows)} items" if rows else "")

        if not rows:
            empty = Gtk.Label(label="Nothing copied yet")
            empty.get_style_context().add_class("empty-state")
            placeholder_row = Gtk.ListBoxRow(selectable=False, activatable=False)
            placeholder_row.row_type = "all"
            placeholder_row.full_text = ""
            placeholder_row.is_placeholder = True
            placeholder_row.add(empty)
            self.listbox.add(placeholder_row)
        else:
            for row_id, row_type, content, pinned, created_at in rows:
                self.listbox.add(self._build_row(row_id, row_type, content, pinned, created_at))
            first_row = None
            for child in self.listbox.get_children():
                if getattr(child, "row_id", None) is not None:
                    first_row = child
                    break
            if first_row is not None:
                self.listbox.select_row(first_row)
        self.listbox.show_all()

    def _build_row(self, row_id, row_type, content, pinned, created_at):
        row = Gtk.ListBoxRow()
        row.get_style_context().add_class("clip-row-box")
        row.row_id = row_id
        row.row_type = row_type
        row.full_text = content if row_type == "text" else "[image]"

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        inner.get_style_context().add_class("row-inner")
        row.add(inner)

        if row_type == "image":
            img = Gtk.Image()
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    content, THUMB_SIZE, THUMB_SIZE, True
                )
                img.set_from_pixbuf(pixbuf)
            except Exception:
                img.set_from_icon_name("image-x-generic-symbolic", Gtk.IconSize.DIALOG)
            img.get_style_context().add_class("thumb")
            inner.pack_start(img, False, False, 0)
        else:
            icon = Gtk.Image.new_from_icon_name("edit-paste-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
            inner.pack_start(icon, False, False, 0)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.set_valign(Gtk.Align.CENTER)

        if row_type == "image":
            label = Gtk.Label(label="Image")
        else:
            preview = content.replace("\n", " ⏎ ").strip()
            if len(preview) > 80:
                preview = preview[:80] + "…"
            label = Gtk.Label(label=preview)
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.get_style_context().add_class("row-text")
        vbox.pack_start(label, False, False, 0)

        ts = Gtk.Label(label=datetime.fromtimestamp(created_at).strftime("%b %d, %H:%M"))
        ts.set_halign(Gtk.Align.START)
        ts.get_style_context().add_class("row-time")
        vbox.pack_start(ts, False, False, 0)

        inner.pack_start(vbox, True, True, 0)

        pin_btn = Gtk.ToggleButton()
        pin_btn.set_image(Gtk.Image.new_from_icon_name("starred-symbolic", Gtk.IconSize.BUTTON))
        pin_btn.get_style_context().add_class("icon-btn")
        if pinned:
            pin_btn.get_style_context().add_class("pinned")
        pin_btn.set_active(bool(pinned))
        pin_btn.set_relief(Gtk.ReliefStyle.NONE)
        pin_btn.connect("toggled", self.on_pin_toggled, row_id)
        inner.pack_start(pin_btn, False, False, 0)

        del_btn = Gtk.Button()
        del_btn.set_image(Gtk.Image.new_from_icon_name("user-trash-symbolic", Gtk.IconSize.BUTTON))
        del_btn.get_style_context().add_class("icon-btn")
        del_btn.get_style_context().add_class("delete-btn")
        del_btn.set_relief(Gtk.ReliefStyle.NONE)
        if pinned:
            del_btn.set_sensitive(False)
            del_btn.set_tooltip_text("Unpin to delete")
        del_btn.connect("clicked", self.on_delete_clicked, row_id)
        inner.pack_start(del_btn, False, False, 0)

        return row

    def on_pin_toggled(self, btn, row_id):
        active = btn.get_active()
        self.db.execute("UPDATE history SET pinned = ? WHERE id = ?", (1 if active else 0, row_id))
        self.db.commit()
        self.load_rows()

    def on_delete_clicked(self, btn, row_id):
        row = self.db.execute(
            "SELECT type, content, pinned FROM history WHERE id = ?", (row_id,)
        ).fetchone()
        if not row:
            return
        row_type, content, pinned = row
        if pinned:
            # Pinned items must be unpinned first; this is a safety net in
            # case the button was ever enabled while still pinned.
            return
        if row_type == "image" and os.path.exists(content):
            try:
                os.remove(content)
            except OSError:
                pass
        self.db.execute("DELETE FROM history WHERE id = ?", (row_id,))
        self.db.commit()
        self.load_rows()

    def on_clear_clicked(self, btn):
        self.clear_history()

    def clear_history(self):
        rows = self.db.execute(
            "SELECT type, content FROM history WHERE pinned = 0"
        ).fetchall()
        for row_type, content in rows:
            if row_type == "image" and os.path.exists(content):
                try:
                    os.remove(content)
                except OSError:
                    pass
        self.db.execute("DELETE FROM history WHERE pinned = 0")
        self.db.commit()
        self.load_rows()

    def on_search_activate(self, entry):
        row = self.listbox.get_selected_row()
        if row is None or not row.get_child_visible():
            row = None
            for child in self.listbox.get_children():
                if getattr(child, "row_id", None) is not None and child.get_child_visible():
                    row = child
                    break
        if row is not None and getattr(row, "row_id", None) is not None:
            self.on_row_activated(self.listbox, row)

    def on_row_activated(self, listbox, row):
        if row.row_type == "image":
            content_path = self.db.execute(
                "SELECT content FROM history WHERE id = ?", (row.row_id,)
            ).fetchone()
            pixbuf = None
            if content_path:
                try:
                    pixbuf = GdkPixbuf.Pixbuf.new_from_file(content_path[0])
                except Exception:
                    pixbuf = None
            if pixbuf is not None:
                self.clipboard.set_image(pixbuf)
                self.clipboard.store()
                self.hide_window()
        else:
            self.clipboard.set_text(row.full_text, -1)
            self.clipboard.store()
            self.hide_window()

    def _is_wayland(self):
        display = self.get_display()
        return bool(display) and "Wayland" in type(display).__name__

    def on_drag_press(self, widget, event):
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button != 1:
            return False
        if self._is_wayland():
            # Wayland doesn't let clients set their own window position directly,
            # so this is the one case where the WM-driven interactive move is used.
            # The compositor owns the grab from here, so our own button-release
            # handler below may never fire -- clear the flag shortly after
            # instead of waiting for a release we might not get.
            self._dragging = True
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
            GLib.timeout_add(400, self._clear_dragging_flag)
            return True
        # X11: track the pointer ourselves and move the window on every motion
        # event. This doesn't depend on the window manager supporting any
        # particular "interactive move" protocol, which is more reliable across
        # different window managers than begin_move_drag alone.
        self._dragging = True
        win_x, win_y = self.get_position()
        self._drag_offset = (event.x_root - win_x, event.y_root - win_y)
        return True

    def _clear_dragging_flag(self):
        self._dragging = False
        return False

    def on_drag_motion(self, widget, event):
        if self._drag_offset is None:
            return False
        offset_x, offset_y = self._drag_offset
        target_x, target_y = int(event.x_root - offset_x), int(event.y_root - offset_y)
        cur_x, cur_y = self.get_position()
        # Ignore sub-pixel jitter so a plain click (no real drag) never moves
        # the window at all.
        if abs(target_x - cur_x) < 2 and abs(target_y - cur_y) < 2:
            return True
        self.move(target_x, target_y)
        return True

    def on_drag_release(self, widget, event):
        self._drag_offset = None
        self._dragging = False
        return False

    def on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide_window()
            return True
        return False

    def hide_window(self):
        self.hide()

    def on_focus_out(self, widget, event):
        # Don't hide immediately: a plain click inside the popup (a button,
        # the search entry, our own drag handling grabbing the pointer) can
        # trigger a brief, spurious focus-out on some window managers even
        # though the popup never actually stopped being the active window.
        # Re-check shortly after, once things have settled.
        GLib.timeout_add(80, self._hide_if_still_inactive)
        return False

    def _hide_if_still_inactive(self):
        if self._dragging:
            return False
        if self._dialog_open:
            return False
        if time.time() - self._shown_at < 1.0:
            # Just shown -- give it a moment to actually receive focus
            # before trusting is_active() at all.
            return False
        if not self.is_active():
            try:
                with open(LOG_PATH, "a") as f:
                    f.write(f"{datetime.now().isoformat()} [auto-hide] hiding due to focus loss\n")
            except Exception:
                pass
            self.hide_window()
        return False

    def refresh_and_present(self):
        self.load_rows()
        self.search.set_text("")
        self._shown_at = time.time()
        self.show_all()
        self.present()
        GLib.idle_add(self.search.grab_focus)
        try:
            with open(LOG_PATH, "a") as f:
                f.write(
                    f"{datetime.now().isoformat()} [refresh_and_present] "
                    f"visible={self.get_visible()} mapped={self.get_mapped()} "
                    f"active={self.is_active()} pos={self.get_position()}\n"
                )
        except Exception:
            pass


def send_show_command():
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(SOCK_PATH)
        _send_framed(s, "show")
        s.close()
        return True
    except OSError:
        return False


def query_daemon_version():
    """Ask a running daemon what build it is. Returns None if nothing is
    listening, or if it's an old daemon that predates this command (it will
    just close the connection without replying)."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(SOCK_PATH)
        _send_framed(s, "version")
        data = s.recv(256).decode(errors="ignore").strip()
        s.close()
        return data or None
    except OSError:
        return None


def kill_stale_daemon():
    """Stop an already-running daemon so a fresh one (with the current code)
    can take its place. A running daemon keeps whatever code was loaded when
    it started -- editing this file on disk does nothing to it until it's
    restarted."""
    try:
        if os.path.exists(PID_PATH):
            with open(PID_PATH) as f:
                pid = int(f.read().strip())
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            else:
                for _ in range(30):
                    time.sleep(0.1)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
    except Exception:
        pass
    try:
        if os.path.exists(SOCK_PATH):
            os.remove(SOCK_PATH)
    except OSError:
        pass


def spawn_daemon():
    ensure_dirs()
    script = os.path.abspath(__file__)
    log_file = open(LOG_PATH, "a")
    log_file.write(f"\n--- {datetime.now().isoformat()} [spawn] starting daemon ---\n")
    log_file.flush()
    subprocess.Popen(
        [sys.executable, script, "run"],
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )


SPAWN_LOCK_PATH = os.path.join(CACHE_DIR, "daemon-spawn.lock")


def cmd_show():
    ensure_dirs()
    if send_show_command():
        return
    # No daemon answered. Take an exclusive lock before spawning one, so
    # that if 'show' was invoked several times in quick succession (a
    # double keypress, a stuck key, etc.) only one of them actually spawns
    # a daemon -- the rest just find it already running once they get the
    # lock and connect normally instead of each spawning their own.
    lock_fd = open(SPAWN_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if send_show_command():
            return
        spawn_daemon()
        connected = False
        for _ in range(50):
            time.sleep(0.1)
            if send_show_command():
                connected = True
                break
        if not connected:
            print(
                "Could not start the clipboard daemon.\n"
                f"Check the log for details:  cat {LOG_PATH}"
            )
            sys.exit(1)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def cmd_restart():
    """Explicitly stop any running daemon (whatever build it is, including
    any leftover/zombie processes) and start a fresh one. Run this by hand
    after updating clip-manager.py -- 'show' itself no longer tries to
    detect or auto-restart a stale daemon, since that logic turned out to
    cause more problems (runaway respawning) than it solved."""
    ensure_dirs()
    print("Stopping any running clip-manager daemon...")
    try:
        subprocess.run(["pkill", "-f", "clip-manager.py run"], check=False)
    except Exception:
        pass
    try:
        if os.path.exists(SOCK_PATH):
            os.remove(SOCK_PATH)
    except OSError:
        pass
    time.sleep(0.3)
    spawn_daemon()
    connected = False
    for _ in range(50):
        time.sleep(0.1)
        if send_show_command():
            connected = True
            break
    if connected:
        print("Daemon restarted.")
    else:
        print(
            "Could not start the clipboard daemon.\n"
            f"Check the log for details:  cat {LOG_PATH}"
        )
        sys.exit(1)


def cmd_run():
    ensure_dirs()
    try:
        init_result = Gtk.init_check()
        ok = init_result[0] if isinstance(init_result, tuple) else init_result
        if not ok:
            log_error("gtk_init_failed (no display connection)")
            sys.exit(1)
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(LOG_PATH, "a") as f:
                f.write(
                    f"\n--- {datetime.now().isoformat()} [run] daemon starting, "
                    f"pid={os.getpid()}, build={SCRIPT_BUILD}, poll={POLL_INTERVAL_MS}ms ---\n"
                )
            with open(PID_PATH, "w") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass
        daemon = ClipboardDaemon()
        daemon.start_socket_server()
        Gtk.main()
    except Exception:
        log_error("cmd_run")
        raise


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("run", "show", "restart"):
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "run":
        cmd_run()
    elif sys.argv[1] == "restart":
        cmd_restart()
    else:
        cmd_show()


if __name__ == "__main__":
    main()
