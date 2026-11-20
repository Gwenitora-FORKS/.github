"""Count the distinct images (not tags) published for each loader on Docker Hub.

Several tags often point to the same image (``latest``, ``1.21``,
``1.21.1``, ``1.21.1_latest``...), so tags are grouped by the digest of the
image they point to. A multi-architecture image (amd64 + arm64 + attestations)
has one single index digest and therefore counts as one image.

Writes one shields.io endpoint file per loader (``<loader>.json``) plus
``total.json`` into the output directory given as first argument.
Exits with a non-zero code if Docker Hub could not be read for a loader, so
the workflow keeps the previously published badges instead of wrong ones.
"""

import json
import sys
import time
import urllib.error
import urllib.request
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
TAGS_URL = "https://hub.docker.com/v2/repositories/{namespace}/{repo}/tags?page_size=100"

BADGE_STYLE = {
    "schemaVersion": 1,
    "color": "B900F4",
    "labelColor": "16171C",
    "namedLogo": "docker",
    "logoColor": "F4F2EC",
    "cacheSeconds": 3600,
}


def fetch_json(url, retries=3):
    """GET a Docker Hub API page, retrying on transient errors."""
    request = urllib.request.Request(url, headers={"User-Agent": "minecraftserv-badges"})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 or attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(5 * attempt)
    raise RuntimeError("unreachable")


def image_id(tag):
    """Return the identity of the image a tag points to, or None."""
    digest = tag.get("digest")
    if digest:
        return digest
    # Fallback for entries without an index digest: identify the image by
    # the set of its per-architecture digests.
    arch_digests = sorted(
        image["digest"] for image in tag.get("images") or [] if image.get("digest")
    )
    return "|".join(arch_digests) or None


def count_images(loader):
    """Count the distinct images of minecraftserv/<loader> (0 if the repo does not exist)."""
    images = set()
    url = TAGS_URL.format(namespace=NAMESPACE, repo=loader)
    try:
        while url:
            page = fetch_json(url)
            for tag in page.get("results") or []:
                identity = image_id(tag)
                if identity:
                    images.add(identity)
            url = page.get("next")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return 0
        raise
    return len(images)


def badge(label, count):
    unit = "image" if count == 1 else "images"
    return {**BADGE_STYLE, "label": label, "message": f"{count:,} {unit}"}


def main():
    output_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    output_dir.mkdir(parents=True, exist_ok=True)

    counts = {}
    failed = []
    for loader in LOADERS:
        try:
            counts[loader] = count_images(loader)
            print(f"{loader}: {counts[loader]} image(s)")
        except Exception as error:  # noqa: BLE001 - report every loader before failing
            failed.append(loader)
            print(f"::error::Could not count the images of {loader}: {error}")

    if failed:
        sys.exit(1)

    for loader, count in counts.items():
        (output_dir / f"{loader}.json").write_text(json.dumps(badge(loader, count)))
    total = sum(counts.values())
    (output_dir / "total.json").write_text(json.dumps(badge("minecraftserv", total)))
    print(f"total: {total} image(s)")


if __name__ == "__main__":
    main()
