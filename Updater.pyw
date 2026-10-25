"""Binary-to-Text updater.

Checks the latest GitHub release, verifies the asset SHA-256 digest, stages the
new script beside the target, keeps a dated backup, then launches the updated app.
The update is reported complete as soon as the new file is installed and launch is
handed to the operating system; no startup wait is performed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk

try:
    import winsound
except ImportError:  # Non-Windows fallback for syntax/testing environments.
    winsound = None

GITHUB_OWNER = "Anotherone-h9e"
GITHUB_REPO = "Binary-to-Text"
ASSET_NAME = "Biner-Ke-Text.py"
APP_FILENAME = "Biner-Ke-Text.pyw"
CURRENT_VERSION = "0.1.0"  # Update this when publishing a tagged release.
API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
USER_AGENT = "Binary-to-Text-Updater/1.0"
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 25 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30

APP_DIR = Path(__file__).resolve().parent
TARGET_PATH = APP_DIR / APP_FILENAME
LOG_PATH = APP_DIR / "updater.log"

# Keep the installed-version marker out of the app folder so it does not clutter
# the user's view. On Windows this resolves to %LOCALAPPDATA%\Updater.
if os.name == "nt":
    _local_app_data = os.environ.get("LOCALAPPDATA")
    if _local_app_data:
        VERSION_DIR = Path(_local_app_data) / "Updater"
    else:
        VERSION_DIR = Path.home() / "AppData" / "Local" / "Updater"
else:
    VERSION_DIR = Path.home() / ".local" / "share" / "Updater"

VERSION_FILE = VERSION_DIR / "installed_version.txt"
# Keep recovery copies inside the updater data folder, not beside the app.
BACKUP_DIR = VERSION_DIR / "backups"
try:
    VERSION_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # Let version read/write functions report/fallback gracefully if this location
    # is unavailable; the updater's app folder remains separate from this marker.
    pass


def get_installed_version() -> str:
    """Read the last successfully installed release tag from a local file."""
    try:
        value = VERSION_FILE.read_text(encoding="utf-8").strip()
        if value and re.fullmatch(r"v?\d+(?:\.\d+)*", value, re.IGNORECASE):
            return value
    except OSError:
        pass
    return CURRENT_VERSION


def save_installed_version(version: str) -> None:
    """Persist the installed version in the LocalAppData Updater folder atomically."""
    if not re.fullmatch(r"v?\d+(?:\.\d+)*", version, re.IGNORECASE):
        raise RuntimeError(f"Invalid version tag for saving: {version}")
    VERSION_DIR.mkdir(parents=True, exist_ok=True)
    temp = VERSION_FILE.with_suffix(VERSION_FILE.suffix + ".tmp")
    try:
        temp.write_text(version + "\n", encoding="utf-8")
        os.replace(temp, VERSION_FILE)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def request_bytes(url: str, limit: int, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as response:
        final_url = response.geturl()
        if not final_url.startswith("https://"):
            raise RuntimeError("The server redirected the download to a non-HTTPS connection.")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("The response exceeds the allowed size limit.")
    return data


def is_internet_available() -> bool:
    """Check for working internet access using two independent HTTPS endpoints."""
    probes = (
        "https://www.google.com/generate_204",
        "https://github.com/",
    )
    for url in probes:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=4) as response:
                if response.status < 500:
                    return True
        except (OSError, urllib.error.URLError, TimeoutError, ValueError):
            continue
    return False


def download_asset(url: str, limit: int, progress_callback) -> bytes:
    """Download an asset in chunks and report byte-based progress (0..100)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"}
    )
    chunks = []
    total_read = 0
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as response:
        final_url = response.geturl()
        if not final_url.startswith("https://"):
            raise RuntimeError("The server redirected the download to a non-HTTPS connection.")
        header_size = response.headers.get("Content-Length")
        expected = int(header_size) if header_size and header_size.isdigit() else 0
        if expected > limit:
            raise RuntimeError("The update file exceeds the allowed size limit.")
        progress_callback(5, "Downloading update…")
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total_read += len(chunk)
            if total_read > limit:
                raise RuntimeError("The update file exceeds the allowed size limit.")
            chunks.append(chunk)
            if expected:
                # Reserve 5-80% for download; later stages use 80-100%.
                percent = 5 + int(75 * min(total_read / expected, 1.0))
                progress_callback(percent, f"Downloading update… {total_read:,} / {expected:,} byte")
            else:
                # Unknown Content-Length: use an indeterminate bar until download ends.
                progress_callback(None, f"Downloading update… {total_read:,} byte")
    if expected and total_read != expected:
        raise RuntimeError("Downloaded size does not match the server Content-Length.")
    progress_callback(80, "Download complete; verifying file…")
    return b"".join(chunks)


def version_key(value: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", value or "")
    return tuple(int(n) for n in nums) or (0,)


def get_latest_release() -> dict:
    data = json.loads(request_bytes(API_URL, MAX_METADATA_BYTES).decode("utf-8"))
    if data.get("draft") or data.get("prerelease"):
        raise RuntimeError("The latest release is a draft or prerelease and will not be installed automatically.")
    tag = str(data.get("tag_name", "")).strip()
    if not tag:
        raise RuntimeError("The GitHub release has no version tag.")
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("The release asset list is invalid.")
    asset = next((a for a in assets if a.get("name") == ASSET_NAME), None)
    if not asset:
        raise RuntimeError(f"Asset '{ASSET_NAME}' was not found in GitHub Releases.")
    url = asset.get("browser_download_url", "")
    expected_prefix = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/download/"
    if not isinstance(url, str) or not url.startswith(expected_prefix):
        raise RuntimeError("The asset URL does not match the expected repository.")
    digest = asset.get("digest", "")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise RuntimeError("The release does not provide a valid SHA-256 digest; the update was cancelled.")
    return {"tag": tag, "name": str(data.get("name") or tag), "body": str(data.get("body") or ""),
            "url": url, "digest": digest.split(":", 1)[1].lower(), "size": asset.get("size", 0)}


class UpdaterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Binary-to-Text — Updater")
        self.root.geometry("600x440")
        self.root.minsize(500, 360)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.busy = False
        self.release = None
        self.update_succeeded = False
        self.status = tk.StringVar(value=f"Installed version: {get_installed_version()}")
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Binary-to-Text Updater", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.status, wraplength=550).pack(anchor="w", pady=(8, 10))
        self.notes = tk.Text(frame, height=12, wrap="word", state="disabled")
        self.notes.pack(fill="both", expand=True)
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(12, 0))
        self.check_btn = ttk.Button(bar, text="Check for Updates", command=self.check_update)
        self.check_btn.pack(side="left")
        self.install_btn = ttk.Button(bar, text="Install Update", command=self.install_update, state="disabled")
        self.install_btn.pack(side="left", padx=8)
        ttk.Button(bar, text="Close", command=self.close).pack(side="right")
        self.progress_text = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.progress_text).pack(anchor="w", pady=(10, 0))
        self.progress = ttk.Progressbar(frame, mode="indeterminate", maximum=100)
        self.progress.pack(fill="x", pady=(4, 0))

    def set_notes(self, text: str) -> None:
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", text or "No release notes provided.")
        self.notes.configure(state="disabled")

    def set_busy(self, busy: bool, status: str, installing: bool = False) -> None:
        self.busy = busy
        self.status.set(status)
        self.check_btn.configure(state="disabled" if busy else "normal")
        self.install_btn.configure(state="disabled" if busy or not self.release else "normal")
        if busy:
            self.progress.stop()
            self.progress.configure(mode="indeterminate", maximum=100)
            if installing:
                # A smooth marquee for installation: sweeps left-to-right and back
                # at a moderate pace. Keep it running; don't restart per download chunk.
                self.progress_text.set("Preparing update…")
                self.progress.start(28)
            else:
                # Preserve the original behavior for Check Updates.
                self.progress_text.set("")
                self.progress.start(14)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=100)
            if self.update_succeeded:
                self.progress.configure(value=100)
                self.progress_text.set("100% — Complete")
            else:
                self.progress.configure(value=0)
                self.progress_text.set("")

    def report_progress(self, percent, message: str) -> None:
        """Keep a smooth left-to-right marquee moving while updating status text."""
        def apply():
            self.status.set(message)
            # Do not restart the animation for every download chunk; doing so
            # makes the moving segment appear to stutter or jump back to start.
            if str(self.progress["mode"]) != "indeterminate":
                self.progress.stop()
                self.progress.configure(mode="indeterminate", maximum=100)
                self.progress.start(28)
            if percent is None:
                self.progress_text.set(message)
            else:
                value = max(0, min(100, int(percent)))
                self.progress_text.set(f"{value}% — {message}")
        try:
            self.root.after(0, apply)
        except tk.TclError:
            pass

    def check_update(self) -> None:
        if self.busy:
            return
        self.set_busy(True, "Contacting GitHub and checking for releases...")
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self) -> None:
        try:
            release = get_latest_release()
            installed_version = get_installed_version()
            current = version_key(installed_version)
            latest = version_key(release["tag"])
            if latest <= current:
                self.root.after(0, self._check_done, None, f"No updates available. Installed version: {installed_version}; latest GitHub version: {release['tag']}.")
            else:
                self.root.after(0, self._check_done, release, f"Update available: {installed_version} → {release['tag']}")
        except Exception as exc:
            log(f"Release check failed: {exc}")
            if not is_internet_available():
                self.root.after(0, self._show_no_internet)
            else:
                self.root.after(0, self._check_done, None, f"Check failed: {exc}")

    def _show_no_internet(self) -> None:
        """Restore the UI and show the requested Windows error alert."""
        self.release = None
        self.set_busy(False, "No internet connection!")
        self.install_btn.configure(state="disabled")
        self.set_notes("No internet connection! Please check your internet connection and try again.")
        try:
            if winsound is not None:
                winsound.MessageBeep(winsound.MB_ICONHAND)
        except (RuntimeError, OSError):
            pass
        messagebox.showerror(
            "No internet connection!",
            "No internet connection!\n\nPlease connect to the internet and try again.",
            parent=self.root,
        )

    def _check_done(self, release, status: str) -> None:
        self.release = release
        self.set_busy(False, status)
        self.install_btn.configure(state="normal" if release else "disabled")
        if release:
            self.set_notes(f"Release: {release['name']}\nTag: {release['tag']}\n\n{release['body']}\n\nAsset: {ASSET_NAME}\nSHA-256: {release['digest']}")
        else:
            self.set_notes(status)

    def install_update(self) -> None:
        if self.busy or not self.release:
            return
        if not TARGET_PATH.is_file():
            messagebox.showerror("File Not Found", f"Application file not found:\n{TARGET_PATH}", parent=self.root)
            return
        if not messagebox.askyesno("Confirm Update", "The application file will be replaced and the updated application will be opened. The update is considered complete once the file is installed and the new process starts successfully. A backup of the previous version will be kept for manual recovery. Continue?", parent=self.root):
            return
        self.set_busy(True, "Preparing update…", installing=True)
        threading.Thread(target=self._install_worker, args=(self.release.copy(),), daemon=True).start()

    def _install_worker(self, release: dict) -> None:
        temp_path = None
        backup_path = None
        installed = False
        previous_version = get_installed_version()
        version_saved = False
        try:
            data = download_asset(
                release["url"], MAX_ASSET_BYTES,
                lambda pct, msg: self.report_progress(pct, msg),
            )
            if release.get("size") and len(data) != int(release["size"]):
                raise RuntimeError("Downloaded file size does not match the release metadata.")
            self.report_progress(83, "Verifying SHA-256…")
            actual = hashlib.sha256(data).hexdigest()
            if actual != release["digest"]:
                raise RuntimeError("SHA-256 digest mismatch; the file was not installed.")
            self.report_progress(87, "Checking Python syntax…")
            source = data.decode("utf-8-sig")
            compile(source, str(TARGET_PATH), "exec")
            with tempfile.NamedTemporaryFile("wb", dir=APP_DIR, prefix=".b2t-update-", suffix=".tmp", delete=False) as f:
                temp_path = Path(f.name)
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            self.report_progress(90, "Backing up the previous version…")
            # Keep each dated backup so successful updates never erase recovery copies.
            safe_tag = re.sub(r"[^A-Za-z0-9._-]+", "_", release["tag"]).strip("._-") or "release"
            stamp = time.strftime("%Y%m%d-%H%M%S")
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            backup_path = BACKUP_DIR / f"{APP_FILENAME}.backup-{safe_tag}-{stamp}"
            if backup_path.exists():
                raise RuntimeError(f"Backup file already exists: {backup_path}")
            shutil.copy2(TARGET_PATH, backup_path)
            self.report_progress(94, "Installing the new application file…")
            os.replace(temp_path, TARGET_PATH)
            temp_path = None
            installed = True
            save_installed_version(release["tag"])
            version_saved = True
            self.report_progress(97, "Launching the updated application…")
            env = os.environ.copy()
            env.pop("B2T_UPDATE_HEALTH_FILE", None)
            env.pop("B2T_UPDATE_VERSION", None)
            env.pop("B2T_RELAUNCH_APP_ON_UPDATER_CLOSE", None)
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            subprocess.Popen(
                [sys.executable, str(TARGET_PATH)], cwd=str(APP_DIR), env=env,
                close_fds=(os.name != "nt"), creationflags=creationflags,
            )
            # Finish immediately once installation is complete and the OS accepted launch.
            # The dated backup remains on disk for manual recovery if the app later fails.
            log(f"Update to {release['tag']} installed and process launch requested; backup kept at {backup_path}.")
            self.update_succeeded = True
            self.report_progress(100, "Update complete!")
            self.root.after(0, self._install_done, True,
                            f"Update {release['tag']} installed successfully. The previous version backup is saved at:\n{backup_path}")
            return
        except Exception as exc:
            log(f"Install failed: {exc}")
            # Restore old file if replacement occurred but launching the new process failed.
            if installed and backup_path and backup_path.exists():
                try:
                    # Copy rather than move: the backup may be on another drive,
                    # and keeping it is useful for manual recovery.
                    shutil.copy2(backup_path, TARGET_PATH)
                    if version_saved:
                        save_installed_version(previous_version)
                    log("New app launch failed; previous version restored.")
                except OSError as rollback_exc:
                    log(f"Rollback failed: {rollback_exc}")
                    exc = RuntimeError(f"{exc}\nAutomatic recovery failed; the backup remains at: {backup_path}")
            self.root.after(0, self._install_done, False, str(exc))
        finally:
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _install_done(self, success: bool, message: str) -> None:
        if success:
            self.update_succeeded = True
        self.set_busy(False, message)
        if success:
            messagebox.showinfo("Update Successful", message, parent=self.root)
        else:
            messagebox.showerror("Update Failed", message, parent=self.root)

    def close(self) -> None:
        if self.busy:
            if not messagebox.askyesno("Process Running", "A process is currently running. Close this window anyway?", parent=self.root):
                return
        relaunch_path = os.environ.get("B2T_RELAUNCH_APP_ON_UPDATER_CLOSE")
        should_relaunch = bool(relaunch_path and not self.update_succeeded and os.path.isfile(relaunch_path))
        self.root.destroy()
        if should_relaunch:
            try:
                env = os.environ.copy()
                env.pop("B2T_RELAUNCH_APP_ON_UPDATER_CLOSE", None)
                subprocess.Popen([sys.executable, relaunch_path], cwd=str(Path(relaunch_path).resolve().parent), env=env)
            except OSError as exc:
                log(f"Could not relaunch main app after updater close: {exc}")


def main() -> None:
    log("Updater started.")
    root = tk.Tk()
    UpdaterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
