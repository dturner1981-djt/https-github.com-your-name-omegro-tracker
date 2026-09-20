"""Microsoft Graph client for SharePoint reads.

Two auth modes:

  * **App-only** (client credentials) — for the scheduled refresh. Needs an
    Entra app registration with the application permissions `Sites.Read.All`
    and `Files.Read.All`, admin-consented.
  * **Device code** — for a human running `omegro-tracker refresh` locally
    against their own access. No secret needed.

Large workbooks are always downloaded and parsed locally. The Graph workbook
session API is the obvious alternative but times out on the OG scorecard, which
is several megabytes of formula-heavy sheets.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import requests

GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com"


class GraphError(RuntimeError):
    pass


@dataclass
class DriveItem:
    id: str
    name: str
    size: int
    last_modified: str
    download_url: str | None
    is_folder: bool


class GraphClient:
    def __init__(
        self,
        token: str,
        *,
        download_dir: str | Path = ".cache/graph",
        timeout: int = 120,
        session: requests.Session | None = None,
    ) -> None:
        self._token = token
        self.download_dir = Path(download_dir)
        self.timeout = timeout
        self._session = session or requests.Session()

    # -- construction -------------------------------------------------------

    @classmethod
    def from_env(cls, **kw: Any) -> "GraphClient":
        """Client credentials from OMEGRO_TRACKER_{TENANT_ID,CLIENT_ID,CLIENT_SECRET}.

        Falls back to OMEGRO_TRACKER_TOKEN, which lets a caller paste a token
        obtained elsewhere (useful in CI where the secret lives in the runner).
        """
        if token := os.environ.get("OMEGRO_TRACKER_TOKEN"):
            return cls(token, **kw)

        tenant = os.environ.get("OMEGRO_TRACKER_TENANT_ID")
        client_id = os.environ.get("OMEGRO_TRACKER_CLIENT_ID")
        secret = os.environ.get("OMEGRO_TRACKER_CLIENT_SECRET")
        if not (tenant and client_id and secret):
            raise GraphError(
                "No Graph credentials. Set OMEGRO_TRACKER_TENANT_ID, "
                "OMEGRO_TRACKER_CLIENT_ID and OMEGRO_TRACKER_CLIENT_SECRET, "
                "or OMEGRO_TRACKER_TOKEN, or run with --device-code."
            )

        resp = requests.post(
            f"{AUTHORITY}/{tenant}/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "client_secret": secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=60,
        )
        if resp.status_code != 200:
            raise GraphError(f"Token request failed ({resp.status_code}): {resp.text[:400]}")
        return cls(resp.json()["access_token"], **kw)

    @classmethod
    def from_device_code(cls, tenant: str, client_id: str, scopes: list[str], **kw: Any) -> "GraphClient":
        """Delegated sign-in. Prints a code for the operator to enter."""
        scope = " ".join(scopes + ["offline_access"])
        start = requests.post(
            f"{AUTHORITY}/{tenant}/oauth2/v2.0/devicecode",
            data={"client_id": client_id, "scope": scope},
            timeout=60,
        )
        if start.status_code != 200:
            raise GraphError(f"Device code request failed: {start.text[:400]}")
        flow = start.json()
        print(flow["message"], flush=True)

        deadline = time.time() + int(flow.get("expires_in", 900))
        interval = int(flow.get("interval", 5))
        while time.time() < deadline:
            time.sleep(interval)
            poll = requests.post(
                f"{AUTHORITY}/{tenant}/oauth2/v2.0/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": client_id,
                    "device_code": flow["device_code"],
                },
                timeout=60,
            )
            body = poll.json()
            if poll.status_code == 200:
                return cls(body["access_token"], **kw)
            if body.get("error") == "authorization_pending":
                continue
            if body.get("error") == "slow_down":
                interval += 5
                continue
            raise GraphError(f"Device code sign-in failed: {body.get('error_description', body)}")
        raise GraphError("Device code sign-in timed out.")

    # -- requests -----------------------------------------------------------

    def _get(self, url: str, **kw: Any) -> requests.Response:
        if not url.startswith("http"):
            url = f"{GRAPH}{url}"
        for attempt in range(4):
            resp = self._session.get(
                url,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self.timeout,
                **kw,
            )
            # Graph throttles with 429 and a Retry-After; a refresh that walks
            # several folders will hit it.
            if resp.status_code == 429:
                time.sleep(int(resp.headers.get("Retry-After", 2 ** attempt)))
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            return resp
        return resp  # type: ignore[possibly-undefined]

    @staticmethod
    def _item(raw: dict[str, Any]) -> DriveItem:
        return DriveItem(
            id=raw["id"],
            name=raw.get("name", ""),
            size=int(raw.get("size") or 0),
            last_modified=raw.get("lastModifiedDateTime", ""),
            download_url=raw.get("@microsoft.graph.downloadUrl"),
            is_folder="folder" in raw,
        )

    # -- drive operations ---------------------------------------------------

    def item(self, drive_id: str, item_id: str) -> DriveItem:
        resp = self._get(f"/drives/{drive_id}/items/{item_id}")
        if resp.status_code != 200:
            raise GraphError(f"item {item_id}: {resp.status_code} {resp.text[:200]}")
        return self._item(resp.json())

    def item_by_path(self, drive_id: str, path: str) -> DriveItem:
        """Path is relative to the drive root. Note that a SharePoint *web* URL
        includes the document-library segment ("Shared Documents") and a drive
        path does not — strip it when adapting a link from a browser."""
        resp = self._get(f"/drives/{drive_id}/root:/{path.strip('/')}")
        if resp.status_code != 200:
            raise GraphError(f"path {path!r}: {resp.status_code} {resp.text[:200]}")
        return self._item(resp.json())

    def children(self, drive_id: str, folder_path: str) -> Iterator[DriveItem]:
        root = folder_path.strip("/")
        url = (
            f"/drives/{drive_id}/root:/{root}:/children"
            if root
            else f"/drives/{drive_id}/root/children"
        )
        while url:
            resp = self._get(url)
            if resp.status_code != 200:
                raise GraphError(f"children {folder_path!r}: {resp.status_code} {resp.text[:200]}")
            body = resp.json()
            for raw in body.get("value", []):
                yield self._item(raw)
            url = body.get("@odata.nextLink")

    def download(self, drive_id: str, item: DriveItem) -> Path:
        """Fetch to the cache directory, keyed by item id and mtime so an
        unchanged file is not re-fetched."""
        stamp = item.last_modified.replace(":", "").replace("-", "")[:15]
        dest = self.download_dir / f"{item.id}_{stamp}_{item.name}"
        if dest.exists() and dest.stat().st_size == item.size:
            return dest

        dest.parent.mkdir(parents=True, exist_ok=True)
        url = item.download_url or f"/drives/{drive_id}/items/{item.id}/content"
        with self._get(url, stream=True) as resp:
            if resp.status_code not in (200, 206):
                raise GraphError(f"download {item.name}: {resp.status_code}")
            tmp = dest.with_suffix(dest.suffix + ".part")
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    fh.write(chunk)
            tmp.replace(dest)
        return dest
