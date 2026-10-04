#!/bin/bash
# Clip Manager installer / uninstaller
# Run as your normal user (NOT with sudo):
#   ./run.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.local/bin"
AUTOSTART_DIR="$HOME/.config/autostart"
EXT_UUID="clip-manager-watcher@websmartbd.local"
EXT_DEST="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"
LAUNCH_CMD="python3 $INSTALL_DIR/clip-manager.py show"
RUN_CMD="python3 $INSTALL_DIR/clip-manager.py run"
SHORTCUT_KEY="<Super>v"
KB_NAME="Clip Manager"
BASE="org.gnome.settings-daemon.plugins.media-keys"
BIND_BASE="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
LIST_PATH_PREFIX="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"

do_install() {
    echo "Installing Clip Manager..."
    echo ""

    # --- 1. Script ---
    mkdir -p "$INSTALL_DIR"
    cp "$SCRIPT_DIR/clip-manager.py" "$INSTALL_DIR/clip-manager.py"
    chmod +x "$INSTALL_DIR/clip-manager.py"
    echo "  [ok] Installed script to $INSTALL_DIR/clip-manager.py"

    # --- 2. Autostart on login ---
    mkdir -p "$AUTOSTART_DIR"
    cat > "$AUTOSTART_DIR/clip-manager.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Clip Manager
Comment=Clipboard history daemon
Exec=$RUN_CMD
X-GNOME-Autostart-enabled=true
NoDisplay=true
Terminal=false
EOF
    echo "  [ok] Set up autostart on login"

    # --- 3. GNOME Shell extension (Wayland clipboard bridge) ---
    if [ -f "$SCRIPT_DIR/extension/metadata.json" ] && [ -f "$SCRIPT_DIR/extension/extension.js" ]; then
        mkdir -p "$EXT_DEST"
        cp "$SCRIPT_DIR/extension/metadata.json" "$EXT_DEST/metadata.json"
        cp "$SCRIPT_DIR/extension/extension.js" "$EXT_DEST/extension.js"
        echo "  [ok] Installed GNOME Shell extension"
        if command -v gnome-extensions >/dev/null 2>&1; then
            gnome-extensions enable "$EXT_UUID" 2>/dev/null || true
            STATE=$(gnome-extensions show "$EXT_UUID" 2>/dev/null | grep "State:" || echo "State: UNKNOWN")
            echo "       $STATE"
            if ! echo "$STATE" | grep -q "ENABLED"; then
                echo "       NOTE: if this is the FIRST install of the extension, log out"
                echo "       and back in once -- it won't actually run until you do."
            fi
        else
            echo "       (gnome-extensions command not found -- is this GNOME Shell?)"
        fi
    else
        echo "  [--] No extension/ folder found next to this script -- skipped."
    fi

    # --- 4. Keyboard shortcut: Super+V ---
    EXISTING=$(gsettings get "$BASE" custom-keybindings)
    PATHS=$(echo "$EXISTING" | grep -o "'[^']*'" | tr -d "'")

    MATCH_PATH=""
    for p in $PATHS; do
        CMD=$(gsettings get "$BIND_BASE:$p" command 2>/dev/null | tr -d "'")
        if [ "$CMD" = "$LAUNCH_CMD" ]; then
            MATCH_PATH="$p"
            break
        fi
    done

    if [ -n "$MATCH_PATH" ]; then
        gsettings set "$BIND_BASE:$MATCH_PATH" name "$KB_NAME"
        gsettings set "$BIND_BASE:$MATCH_PATH" binding "$SHORTCUT_KEY"
        echo "  [ok] Updated existing shortcut entry -> $SHORTCUT_KEY"
    else
        CONFLICT=""
        for p in $PATHS; do
            B=$(gsettings get "$BIND_BASE:$p" binding 2>/dev/null | tr -d "'")
            if [ "$B" = "$SHORTCUT_KEY" ]; then
                CONFLICT="$p"
                break
            fi
        done
        if [ -n "$CONFLICT" ]; then
            echo "  [!!] $SHORTCUT_KEY is already bound to another shortcut."
            echo "       Not overriding it automatically -- set your own shortcut in:"
            echo "       Settings > Keyboard > Custom Shortcuts, pointing to:"
            echo "       $LAUNCH_CMD"
        else
            NEW_PATH="${LIST_PATH_PREFIX}clipmanager/"
            if [ -z "$PATHS" ]; then
                NEWLIST="['$NEW_PATH']"
            else
                NEWLIST="${EXISTING%]}, '$NEW_PATH']"
            fi
            gsettings set "$BASE" custom-keybindings "$NEWLIST"
            gsettings set "$BIND_BASE:$NEW_PATH" name "$KB_NAME"
            gsettings set "$BIND_BASE:$NEW_PATH" command "$LAUNCH_CMD"
            gsettings set "$BIND_BASE:$NEW_PATH" binding "$SHORTCUT_KEY"
            echo "  [ok] Added shortcut: $SHORTCUT_KEY -> opens Clip Manager"
        fi
    fi

    # --- 5. Start the daemon now ---
    pkill -9 -f "clip-manager.py" 2>/dev/null || true
    rm -f "$HOME/.cache/clip-manager/clip.sock" \
          "$HOME/.cache/clip-manager/daemon.pid" \
          "$HOME/.cache/clip-manager/daemon-spawn.lock" 2>/dev/null || true
    sleep 0.3
    python3 "$INSTALL_DIR/clip-manager.py" restart

    echo ""
    echo "Done! Press $SHORTCUT_KEY to open Clip Manager."
    echo "It will also start automatically next time you log in."
}

do_uninstall() {
    echo "Uninstalling Clip Manager..."
    echo ""

    # --- 1. Stop the daemon ---
    pkill -9 -f "clip-manager.py" 2>/dev/null || true
    rm -f "$HOME/.cache/clip-manager/clip.sock" \
          "$HOME/.cache/clip-manager/daemon.pid" \
          "$HOME/.cache/clip-manager/daemon-spawn.lock" 2>/dev/null || true
    echo "  [ok] Stopped the daemon"

    # --- 2. Remove autostart entry ---
    rm -f "$AUTOSTART_DIR/clip-manager.desktop"
    echo "  [ok] Removed autostart entry"

    # --- 3. Remove the script ---
    rm -f "$INSTALL_DIR/clip-manager.py"
    echo "  [ok] Removed $INSTALL_DIR/clip-manager.py"

    # --- 4. Remove the GNOME Shell extension ---
    if command -v gnome-extensions >/dev/null 2>&1; then
        gnome-extensions disable "$EXT_UUID" 2>/dev/null || true
    fi
    rm -rf "$EXT_DEST"
    echo "  [ok] Removed GNOME Shell extension"

    # --- 5. Remove the keyboard shortcut entry (only the one pointing at us) ---
    EXISTING=$(gsettings get "$BASE" custom-keybindings)
    PATHS=$(echo "$EXISTING" | grep -o "'[^']*'" | tr -d "'")

    KEEP=()
    REMOVED=0
    for p in $PATHS; do
        CMD=$(gsettings get "$BIND_BASE:$p" command 2>/dev/null | tr -d "'")
        if [ "$CMD" = "$LAUNCH_CMD" ]; then
            gsettings reset "$BIND_BASE:$p" name 2>/dev/null || true
            gsettings reset "$BIND_BASE:$p" command 2>/dev/null || true
            gsettings reset "$BIND_BASE:$p" binding 2>/dev/null || true
            REMOVED=1
        else
            KEEP+=("'$p'")
        fi
    done

    if [ "$REMOVED" = "1" ]; then
        if [ ${#KEEP[@]} -eq 0 ]; then
            gsettings set "$BASE" custom-keybindings "[]"
        else
            NEWLIST="[$(IFS=,; echo "${KEEP[*]}")]"
            gsettings set "$BASE" custom-keybindings "$NEWLIST"
        fi
        echo "  [ok] Removed the keyboard shortcut"
    else
        echo "  [--] No matching keyboard shortcut found (nothing to remove)"
    fi

    echo ""
    echo "Clip Manager has been uninstalled."
    echo "Your clipboard history database was left untouched at:"
    echo "  ~/.local/share/clip-manager/history.db"
    echo "Remove it yourself if you want that gone too:"
    echo "  rm -rf ~/.local/share/clip-manager ~/.cache/clip-manager"
}

# --- Menu ---
# Allow non-interactive use too: ./run.sh install | ./run.sh uninstall
case "$1" in
    install)
        do_install
        exit 0
        ;;
    uninstall)
        do_uninstall
        exit 0
        ;;
esac

echo "Clip Manager setup"
echo "==================="
echo "  1) Install   (copy script, enable extension, set up Super+V, enable autostart)"
echo "  2) Uninstall  (remove everything Clip Manager added)"
echo "  3) Cancel"
echo ""
read -rp "Select an option [1-3]: " choice

case "$choice" in
    1) do_install ;;
    2) do_uninstall ;;
    *) echo "Cancelled."; exit 0 ;;
esac
