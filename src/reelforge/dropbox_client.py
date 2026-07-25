"""Minimal Dropbox client built on the raw HTTP API.

Refresh-token auth on purpose: Dropbox short-lived tokens die after 4 hours,
which is useless for a scheduled job. The app key/secret plus a refresh token
mint a fresh access token on every run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import Config
from .http import ApiError, new_session, request

log = logging.getLogger(__name__)

RPC = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"
TOKEN_URL = "https://api.dropbox.com/oauth2/token"

# Dropbox requires upload sessions above 150 MB; stay well clear of the edge.
SINGLE_SHOT_LIMIT = 140 * 1024 * 1024
CHUNK = 8 * 1024 * 1024

MEDIA_EXTENSIONS = {".mp4", ".mov", ".m4v", ".jpg", ".jpeg", ".png", ".heic", ".webp"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}


@dataclass
class DropboxFile:
    name: str
    path: str
    content_hash: str
    size: int
    modified: str

    @property
    def suffix(self) -> str:
        return Path(self.name).suffix.lower()

    @property
    def is_video(self) -> bool:
        return self.suffix in VIDEO_EXTENSIONS

    @property
    def is_media(self) -> bool:
        return self.suffix in MEDIA_EXTENSIONS

    @property
    def is_audio(self) -> bool:
        return self.suffix in AUDIO_EXTENSIONS


class Dropbox:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session = new_session()
        self._token: str | None = None

    # -- auth -----------------------------------------------------------

    @property
    def token(self) -> str:
        if self._token is None:
            response = request(
                self.session,
                "POST",
                TOKEN_URL,
                label="dropbox token refresh",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.config.dropbox_refresh_token,
                },
                auth=(self.config.dropbox_app_key, self.config.dropbox_app_secret),
            )
            self._token = response.json()["access_token"]
            log.info("Dropbox access token refreshed")
        return self._token

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _rpc(self, endpoint: str, payload: dict) -> dict:
        response = request(
            self.session,
            "POST",
            f"{RPC}/{endpoint}",
            label=f"dropbox {endpoint}",
            headers={**self._auth_header(), "Content-Type": "application/json"},
            json=payload,
        )
        return response.json() if response.content else {}

    # -- listing --------------------------------------------------------

    def list_media(self, folder: str) -> list[DropboxFile]:
        """List media files directly inside `folder`, oldest first."""
        return sorted(
            [f for f in self._all_files(folder) if f.is_media], key=lambda f: f.modified
        )

    def _all_files(self, folder: str) -> list[DropboxFile]:
        entries: list[dict] = []
        try:
            page = self._rpc(
                "files/list_folder",
                {"path": self._normalise(folder), "recursive": False, "limit": 500},
            )
        except ApiError as exc:
            if "not_found" in exc.body:
                log.warning("Dropbox folder %s does not exist yet", folder)
                return []
            raise

        entries.extend(page.get("entries", []))
        while page.get("has_more"):
            page = self._rpc("files/list_folder/continue", {"cursor": page["cursor"]})
            entries.extend(page.get("entries", []))

        return [
            DropboxFile(
                name=e["name"],
                path=e["path_lower"],
                content_hash=e.get("content_hash", ""),
                size=e.get("size", 0),
                modified=e.get("server_modified", ""),
            )
            for e in entries
            if e.get(".tag") == "file"
        ]

    def list_audio(self, folder: str) -> list[DropboxFile]:
        """Licensed music beds. Empty folder simply means no music layer."""
        entries = self._all_files(folder)
        return sorted([f for f in entries if f.is_audio], key=lambda f: f.name)

    # -- transfer -------------------------------------------------------

    def download(self, path: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        response = request(
            self.session,
            "POST",
            f"{CONTENT}/files/download",
            label=f"dropbox download {path}",
            headers={
                **self._auth_header(),
                "Dropbox-API-Arg": _api_arg({"path": path}),
            },
            stream=True,
        )
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 512):
                handle.write(chunk)
        log.info("Downloaded %s -> %s (%.1f MB)", path, destination.name, destination.stat().st_size / 1e6)
        return destination

    def upload(self, source: Path, dest_path: str) -> str:
        """Upload a file, overwriting any existing one. Returns the Dropbox path."""
        size = source.stat().st_size
        if size <= SINGLE_SHOT_LIMIT:
            result = self._upload_single(source, dest_path)
        else:
            result = self._upload_session(source, dest_path, size)
        log.info("Uploaded %s -> %s", source.name, result)
        return result

    def _upload_single(self, source: Path, dest_path: str) -> str:
        response = request(
            self.session,
            "POST",
            f"{CONTENT}/files/upload",
            label=f"dropbox upload {dest_path}",
            headers={
                **self._auth_header(),
                "Content-Type": "application/octet-stream",
                "Dropbox-API-Arg": _api_arg(
                    {"path": dest_path, "mode": "overwrite", "mute": True}
                ),
            },
            data=source.read_bytes(),
        )
        return response.json()["path_lower"]

    def _upload_session(self, source: Path, dest_path: str, size: int) -> str:
        with source.open("rb") as handle:
            first = handle.read(CHUNK)
            start = request(
                self.session,
                "POST",
                f"{CONTENT}/files/upload_session/start",
                label="dropbox upload_session/start",
                headers={
                    **self._auth_header(),
                    "Content-Type": "application/octet-stream",
                    "Dropbox-API-Arg": _api_arg({"close": False}),
                },
                data=first,
            ).json()
            session_id = start["session_id"]
            offset = len(first)

            while offset < size:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                request(
                    self.session,
                    "POST",
                    f"{CONTENT}/files/upload_session/append_v2",
                    label="dropbox upload_session/append",
                    headers={
                        **self._auth_header(),
                        "Content-Type": "application/octet-stream",
                        "Dropbox-API-Arg": _api_arg(
                            {
                                "cursor": {"session_id": session_id, "offset": offset},
                                "close": False,
                            }
                        ),
                    },
                    data=chunk,
                )
                offset += len(chunk)

        finish = request(
            self.session,
            "POST",
            f"{CONTENT}/files/upload_session/finish",
            label="dropbox upload_session/finish",
            headers={
                **self._auth_header(),
                "Content-Type": "application/octet-stream",
                "Dropbox-API-Arg": _api_arg(
                    {
                        "cursor": {"session_id": session_id, "offset": offset},
                        "commit": {"path": dest_path, "mode": "overwrite", "mute": True},
                    }
                ),
            },
            data=b"",
        )
        return finish.json()["path_lower"]

    def temporary_link(self, path: str) -> str:
        """A direct, publicly fetchable URL valid for ~4 hours.

        This is what Instagram's container endpoint downloads the render from,
        so no separate CDN or bucket is needed.
        """
        return self._rpc("files/get_temporary_link", {"path": path})["link"]

    # -- housekeeping ---------------------------------------------------

    def ensure_folder(self, path: str) -> None:
        try:
            self._rpc("files/create_folder_v2", {"path": self._normalise(path), "autorename": False})
            log.info("Created Dropbox folder %s", path)
        except ApiError as exc:
            if "conflict" in exc.body:
                return  # already there
            raise

    def move(self, from_path: str, to_path: str) -> None:
        try:
            self._rpc(
                "files/move_v2",
                {"from_path": from_path, "to_path": to_path, "autorename": True},
            )
        except ApiError as exc:
            log.warning("Could not move %s -> %s: %s", from_path, to_path, exc)

    @staticmethod
    def _normalise(path: str) -> str:
        """Dropbox wants '' for root and a leading slash everywhere else."""
        path = path.strip()
        if path in {"", "/"}:
            return ""
        return path if path.startswith("/") else f"/{path}"


def _api_arg(payload: dict) -> str:
    """Dropbox-API-Arg must be ASCII-safe JSON on a single line."""
    return json.dumps(payload, ensure_ascii=True)
