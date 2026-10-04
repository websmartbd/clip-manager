# Clip Manager

A lightweight clipboard history manager for Linux (GNOME / Ubuntu), built with Python and GTK3.

Press **Super+V** to pop up your recent clipboard history, search through it, pin items you want to keep, and copy anything back to the clipboard with one click — the same Windows-style "clipboard history" experience, on Linux.

## Backstory

I switched from Windows to Linux, and one of the things I used constantly on Windows was the built-in clipboard history (Win+V) — copy a few things, then pick whichever one you need later instead of re-copying over and over.

On Linux, I tried several existing clipboard manager extensions and apps looking for the same thing, but none of them really felt right — some were clunky, some didn't handle images well, some didn't integrate cleanly with GNOME, some just didn't feel reliable. So I ended up building my own, exactly the way I wanted it to work.

Once it was working well for me, it didn't make sense to keep it just for myself. If you also moved to Linux from Windows and miss this one specific feature, this is for you.

## How it works

Clip Manager runs as a small background daemon plus a GTK3 popup window:

- A **daemon process** (`clip-manager.py run`) watches the system clipboard and saves new text/image content into a local SQLite database (`~/.local/share/clip-manager/history.db`), with content hashing so the same item is never saved twice in a row.
- Pressing **Super+V** runs `clip-manager.py show`, which talks to the daemon over a local Unix socket and tells it to pop up the history window — if the daemon isn't running yet, it gets started automatically.
- The popup is a borderless GTK window you can **drag to reposition**, search, filter by type, pin/unpin, delete, or clear from.
- **The Wayland problem:** GNOME's Wayland compositor (Mutter) deliberately blocks a background app with no focused window from reading the clipboard — this is intentional upstream behavior, not a bug. To get reliable background capture anyway, this project ships a small **GNOME Shell extension** that runs inside gnome-shell's own process (which *is* allowed to read the clipboard) and forwards new clipboard content to the daemon over the same local socket. This is what lets copy/cut be captured instantly, even on Wayland.
- Everything is local — no network access, no telemetry. Data stays in `~/.local/share/clip-manager/` and `~/.cache/clip-manager/`.

## Features

- Clipboard history for both **text and images**
- **Super+V** global keyboard shortcut to open the popup
- **Search** and filter by type (All / Text / Images)
- **Pin** important items so they're never auto-trimmed or cleared
- **Clear history** with one click — pinned items are always kept
- Items can only be deleted after being unpinned, so pinned content is never lost by accident
- Runs automatically in the background and **starts on login**
- Works on both **X11 and Wayland** (a small GNOME Shell extension is included to work around a Wayland clipboard-access restriction for background apps — see "How it works" above)
- Automatic de-duplication — copying the same content twice won't create duplicate entries
- Drag-to-move popup window, with a clean, non-intrusive UI (no dark mode, by design)

## Requirements

- Ubuntu / GNOME Shell (tested on Ubuntu 26.04, GNOME Shell 50.1)
- Python 3 with PyGObject (GTK3) — these are already installed on most GNOME/Ubuntu systems
- `gsettings` and `gnome-extensions` command-line tools (included by default on GNOME)

## Installation

1. Download and extract this bundle.
2. Open a terminal in the extracted folder.
3. Run the setup script (as your normal user — **do not** use `sudo`):

   ```bash
   chmod +x run.sh
   ./run.sh
   ```

4. Choose **1) Install** from the menu.

You can also skip the menu and run a step directly:

```bash
./run.sh install
./run.sh uninstall
```

### What installation does

| Step | What happens |
|---|---|
| 1 | Copies `clip-manager.py` to `~/.local/bin/clip-manager.py` |
| 2 | Creates an autostart entry so the clipboard daemon runs automatically on login |
| 3 | Installs and enables the companion GNOME Shell extension (needed for reliable clipboard capture on Wayland) |
| 4 | Sets up the **Super+V** keyboard shortcut — it checks for conflicts first and will never silently override an existing shortcut on the same key |
| 5 | Restarts the clipboard daemon so everything is live immediately |

> **First-time extension install:** if the GNOME Shell extension has never been installed before, GNOME requires a logout/login (or a full restart) before it becomes active. The installer will tell you if this is needed.

## Usage

- **Super+V** — open the clipboard history popup
- Type in the search box to filter history
- Click a row to copy it back to the clipboard
- Click the star icon to **pin** an item (pinned items stay at the top and are protected from clearing/trimming)
- Click the trash icon to delete an item (an item must be unpinned first)
- Click the broom icon to **clear all history** — pinned items are always kept
- **Esc** closes the popup
- Drag the popup's title bar to move it anywhere on screen

## Uninstallation

```bash
./run.sh uninstall
```

This removes:
- The installed script
- The autostart entry
- The GNOME Shell extension
- Only the keyboard shortcut entry that points to Clip Manager (any other custom shortcuts you have are left untouched)

Your clipboard history database is **not** deleted automatically. To remove it as well:

```bash
rm -rf ~/.local/share/clip-manager ~/.cache/clip-manager
```

## File locations

| Path | Purpose |
|---|---|
| `~/.local/bin/clip-manager.py` | The installed application |
| `~/.config/autostart/clip-manager.desktop` | Autostart entry |
| `~/.local/share/gnome-shell/extensions/clip-manager-watcher@websmartbd.local/` | GNOME Shell extension |
| `~/.local/share/clip-manager/history.db` | Clipboard history database |
| `~/.cache/clip-manager/` | Runtime socket, PID file, and logs |

## Troubleshooting

- **Super+V doesn't open the popup:** check for a conflicting shortcut in *Settings → Keyboard → Keyboard Shortcuts*. The installer only adds the shortcut automatically if that key combination is free.
- **Popup doesn't show after installing the extension for the first time:** log out and back in once (or restart), then try again.
- **Copy/cut isn't appearing in history:** confirm the GNOME Shell extension is enabled:

  ```bash
  gnome-extensions show clip-manager-watcher@websmartbd.local
  ```

  It should report `State: ENABLED`. If it says `OUT OF DATE`, your GNOME Shell version may need to be added to the extension's `shell-version` list in `extension/metadata.json`.
- **Something isn't working after an update:** restart the daemon manually:

  ```bash
  python3 ~/.local/bin/clip-manager.py restart
  ```

- Logs are at `~/.cache/clip-manager/daemon.log`.

## License

Personal project — use and modify freely.
