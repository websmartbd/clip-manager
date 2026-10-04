/*
 * clip-manager-watcher
 *
 * Runs inside gnome-shell itself, which (unlike an ordinary background app)
 * is allowed to read the clipboard on Wayland even when no window of ours
 * has keyboard focus. Every time the clipboard text changes, this forwards
 * it to the clip-manager daemon over its local Unix socket, so the daemon
 * can save it just like it already does for its own X11/poll-based capture.
 *
 * This extension has no UI, no preferences, and does nothing to the
 * clipboard itself besides reading it.
 */

import St from 'gi://St';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const POLL_MS = 400;

export default class ClipManagerWatcherExtension extends Extension {
    enable() {
        console.log('[clip-manager-watcher] enable() called');
        this._lastText = null;
        this._lastImageHash = null;
        this._sockPath = GLib.build_filenamev(
            [GLib.get_home_dir(), '.cache', 'clip-manager', 'clip.sock']
        );
        console.log('[clip-manager-watcher] socket path: ' + this._sockPath);
        this._timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, POLL_MS, () => {
            this._checkClipboard();
            return GLib.SOURCE_CONTINUE;
        });
    }

    disable() {
        console.log('[clip-manager-watcher] disable() called');
        if (this._timeoutId) {
            GLib.source_remove(this._timeoutId);
            this._timeoutId = null;
        }
        this._lastText = null;
    }

    _checkClipboard() {
        const clipboard = St.Clipboard.get_default();
        clipboard.get_text(St.ClipboardType.CLIPBOARD, (_clipboard, text) => {
            if (text) {
                if (text === this._lastText)
                    return;
                console.log('[clip-manager-watcher] new clipboard text detected, length=' + text.length);
                this._lastText = text;
                this._lastImageHash = null;
                this._sendRaw('text:' + text);
                return;
            }
            // No text on the clipboard -- see if there's an image instead.
            this._checkClipboardImage(clipboard);
        });
    }

    _checkClipboardImage(clipboard) {
        let mimetypes;
        try {
            mimetypes = clipboard.get_mimetypes(St.ClipboardType.CLIPBOARD);
        } catch (e) {
            return; // this GNOME version doesn't support querying mimetypes
        }
        if (!mimetypes || mimetypes.length === 0)
            return;

        // Prefer png, then jpeg, then whatever other image type is offered.
        // (Some GNOME versions have had trouble returning content for
        // image/png specifically -- trying jpeg as a fallback covers that.)
        const candidates = [];
        if (mimetypes.includes('image/png'))
            candidates.push('image/png');
        if (mimetypes.includes('image/jpeg'))
            candidates.push('image/jpeg');
        for (const m of mimetypes) {
            if (m.startsWith('image/') && !candidates.includes(m))
                candidates.push(m);
        }
        if (candidates.length === 0)
            return;

        this._tryImageMimetypes(clipboard, candidates, 0);
    }

    _tryImageMimetypes(clipboard, candidates, index) {
        if (index >= candidates.length)
            return;
        const mimetype = candidates[index];
        clipboard.get_content(St.ClipboardType.CLIPBOARD, mimetype, (_clipboard, bytes) => {
            if (!bytes || bytes.get_size() === 0) {
                this._tryImageMimetypes(clipboard, candidates, index + 1);
                return;
            }
            const raw = bytes.toArray ? bytes.toArray() : bytes.get_data();
            // Cheap change-check: hash length + a few sample bytes instead of
            // the whole buffer, just to avoid re-sending the same image
            // every poll tick.
            const sig = mimetype + ':' + raw.length + ':' +
                (raw.length ? raw[0] + ',' + raw[raw.length - 1] : '');
            if (sig === this._lastImageHash)
                return;
            this._lastImageHash = sig;
            this._lastText = null;
            console.log('[clip-manager-watcher] new clipboard image detected, mimetype=' + mimetype + ' size=' + raw.length);
            const b64 = GLib.base64_encode(raw);
            this._sendRaw('image:' + mimetype + ':' + b64);
        });
    }

    _sendRaw(payload) {
        try {
            const address = Gio.UnixSocketAddress.new(this._sockPath);
            const client = new Gio.SocketClient();
            const connection = client.connect(address, null);
            const outStream = connection.get_output_stream();

            const payloadBytes = new TextEncoder().encode(payload);

            // 4-byte big-endian length prefix, matching the daemon's framing.
            const header = new Uint8Array(4);
            new DataView(header.buffer).setUint32(0, payloadBytes.length, false);

            const frame = new Uint8Array(header.length + payloadBytes.length);
            frame.set(header, 0);
            frame.set(payloadBytes, header.length);

            outStream.write_bytes(new GLib.Bytes(frame), null);
            connection.close(null);
            console.log('[clip-manager-watcher] sent to daemon successfully (' + payloadBytes.length + ' bytes)');
        } catch (e) {
            console.log('[clip-manager-watcher] FAILED to send to daemon: ' + e);
        }
    }
}
