"""Count the distinct images (not tags) published for each loader on Docker Hub.

Several tags often point to the same image (``latest``, ``1.21``,
``1.21.1``, ``1.21.1_latest``...), so tags are grouped by the digest of the
image they point to. A multi-architecture image (amd64 + arm64 + attestations)
has one single index digest and therefore counts as one image.

The tags are read through the registry API (the one ``docker push`` uses, as
in the reset-images action), not through the Hub API: the Hub API refuses to
page deep into repositories with many tags (HTTP 403 on vanilla, fabric and
quilt). Each digest is read with a HEAD request, which does not download the
image and should not count as a pull.

Writes one shields.io endpoint file per loader (``<loader>.json``) plus
``total.json`` into the output directory given as first argument.
Optional DOCKERHUB_USERNAME / DOCKERHUB_TOKEN environment variables raise the
registry rate limits. Exits with a non-zero code if a loader could not be
counted, so the workflow keeps the previously published badges.
"""

import base64
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

NAMESPACE = "minecraftserv"
LOADERS = [
    "vanilla",
    "bukkit",
    "spigot",
    "paper",
    "forge",
    "neoforge",
    "fabric",
    "quilt",
    "velocity",
    "horizon",
]

REGISTRY = "https://registry-1.docker.io"
AUTH_URL = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repository}:pull"
# Every manifest type buildx can push: multi-arch indexes first.
MANIFEST_TYPES = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
USER_AGENT = "minecraftserv-badges/1.0"
WORKERS = 8
TOKEN_LIFETIME_SECONDS = 45  # registry tokens are short-lived
RETRIES = 5
RETRYABLE_CODES = {429, 500, 502, 503, 504}
NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')

BADGE_STYLE = {
    "schemaVersion": 1,
    "color": "B900F4",
    "labelColor": "16171C",
    "namedLogo": "docker",
    "logoColor": "F4F2EC",
    "cacheSeconds": 3600,
}


class NotFound(Exception):
    """The repository, or the tag, does not exist (HTTP 404)."""


class Registry:
    """Minimal Docker registry client for one repository, safe across threads."""

    def __init__(self, repository, username=None, password=None):
        self.repository = repository
        self.base_url = f"{REGISTRY}/v2/{repository}"
        self.basic_auth = None
        if username and password:
            raw = f"{username}:{password}".encode()
            self.basic_auth = "Basic " + base64.b64encode(raw).decode()
        self._token = None
        self._token_at = 0.0
        self._lock = threading.Lock()

    def token(self, force_refresh=False):
        with self._lock:
            expired = time.monotonic() - self._token_at >= TOKEN_LIFETIME_SECONDS
            if force_refresh or self._token is None or expired:
                headers = {"User-Agent": USER_AGENT}
                if self.basic_auth:
                    headers["Authorization"] = self.basic_auth
                url = AUTH_URL.format(repository=self.repository)
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.load(response)
                self._token = payload.get("token") or payload.get("access_token")
                if not self._token:
                    raise RuntimeError(f"registry token refused for {self.repository}")
                self._token_at = time.monotonic()
            return self._token

    def request(self, url, method="GET", accept=None):
        """Send an authenticated request and return (headers, body), with retries."""
        force_refresh = False
        for attempt in range(1, RETRIES + 1):
            headers = {
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {self.token(force_refresh)}",
            }
            if accept:
                headers["Accept"] = accept
            request = urllib.request.Request(url, headers=headers, method=method)
            force_refresh = False
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.headers, response.read()
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    raise NotFound(url) from error
                if error.code == 401 and attempt < RETRIES:
                    force_refresh = True  # token expired in flight
                    continue
                if error.code not in RETRYABLE_CODES or attempt == RETRIES:
                    raise
                delay = retry_delay(error.headers.get("Retry-After"), attempt)
            except (urllib.error.URLError, TimeoutError):
                if attempt == RETRIES:
                    raise
                delay = retry_delay(None, attempt)
            time.sleep(delay)
        raise RuntimeError("unreachable")

    def list_tags(self):
        tags = []
        url = f"{self.base_url}/tags/list?n=1000"
        while url:
            headers, body = self.request(url)
            tags.extend(json.loads(body).get("tags") or [])
            match = NEXT_LINK.search(headers.get("Link") or "")
            url = absolute_url(match.group(1)) if match else None
        return tags

    def digest_of(self, tag):
        """Digest of the image a tag points to (None if the tag vanished meanwhile)."""
        try:
            headers, _ = self.request(
                f"{self.base_url}/manifests/{tag}", method="HEAD", accept=MANIFEST_TYPES
            )
        except NotFound:
            return None
        return headers.get("Docker-Content-Digest")


def retry_delay(retry_after, attempt):
    try:
        return max(int(retry_after), 1)
    except (TypeError, ValueError):
        return 2**attempt


def absolute_url(link):
    return link if link.startswith("http") else REGISTRY + link


def count_images(loader, username=None, password=None):
    """Return (images, tags) for minecraftserv/<loader>; (0, 0) if the repo does not exist."""
    registry = Registry(f"{NAMESPACE}/{loader}", username, password)
    try:
        tags = registry.list_tags()
    except NotFound:
        return 0, 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        digests = list(pool.map(registry.digest_of, tags))
    missing = digests.count(None)
    if missing:
        print(f"::warning::{loader}: no digest for {missing} tag(s), left out of the count")
    return len(set(digests) - {None}), len(tags)


def badge(label, count):
    unit = "image" if count == 1 else "images"
    return {**BADGE_STYLE, "label": label, "message": f"{count:,} {unit}"}


def write_summary(rows, total):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = ["| Loader | Images | Tags |", "|---|---:|---:|"]
    lines += [f"| {loader} | {images} | {tags} |" for loader, images, tags in rows]
    lines.append(f"| **total** | **{total}** | |")
    with open(summary_path, "a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


def main():
    output_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    output_dir.mkdir(parents=True, exist_ok=True)
    username = os.environ.get("DOCKERHUB_USERNAME") or None
    password = os.environ.get("DOCKERHUB_TOKEN") or None

    rows = []
    failed = []
    for loader in LOADERS:
        started = time.monotonic()
        try:
            images, tags = count_images(loader, username, password)
        except Exception as error:  # noqa: BLE001 - report every loader before failing
            failed.append(loader)
            print(f"::error::Could not count the images of {loader}: {error}")
            continue
        rows.append((loader, images, tags))
        print(f"{loader}: {images} image(s) for {tags} tag(s) in {time.monotonic() - started:.0f}s")

    if failed:
        sys.exit(1)

    for loader, images, _ in rows:
        (output_dir / f"{loader}.json").write_text(json.dumps(badge(loader, images)))
    total = sum(images for _, images, _ in rows)
    (output_dir / "total.json").write_text(json.dumps(badge("minecraftserv", total)))
    write_summary(rows, total)
    print(f"total: {total} image(s)")


if __name__ == "__main__":
    main()
