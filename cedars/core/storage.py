"""Object storage for files too large for the database: uploads, run inputs and outputs, reference-data packs. One small interface over a
local folder (development, tests, the sandbox) and S3 (production, through the task role; MinIO stands in locally). Keys are relative
paths ("runs/<id>/inputs/job.zip"); `uri` gives the address a ReCET worker reads and writes (file:// or s3://), so the worker and the
platform see the same files. Large files never pass through a page: S3 downloads are pre-signed URLs.

Files live in four areas, one S3 bucket each in AWS (infra/terraform: the workers read uploads and refdata and write results):
  uploads    what people and systems upload (a run's job zip, its statewide factor table)
  results    what workers write (a run's job spec, outputs, receipt and progress)
  refdata    reference-data packs
  snapshots  publication snapshots

Settings (environment): CEDARS_STORAGE_BACKEND local | s3; CEDARS_STORAGE_ROOT (local folder, one sub-folder per area);
CEDARS_BUCKET_UPLOADS, CEDARS_BUCKET_RESULTS, CEDARS_BUCKET_REFDATA, CEDARS_BUCKET_SNAPSHOTS. The S3 endpoint, region and credentials
are boto3's own (AWS_ENDPOINT_URL for MinIO, the task role in AWS), never settings here."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from django.conf import settings


class StorageError(RuntimeError):
    """A key could not be read or written; the message names it."""


@dataclass(frozen=True)
class StoredFile:
    key: str
    size: int


def clean_key(key: str) -> str:
    """A relative POSIX key with no '..', leading slash or empty part."""
    p = PurePosixPath(str(key).replace("\\", "/"))
    if p.is_absolute() or any(part in ("..", "") for part in p.parts) or not p.parts:
        raise StorageError(f"not a storage key: {key!r}")
    return p.as_posix()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 22):
            h.update(block)
    return h.hexdigest()


class LocalStore:
    """Keys as files under one folder."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def path(self, key: str) -> Path:
        return self.root / clean_key(key)

    def uri(self, key: str) -> str:
        return self.path(key).as_uri() if not key.endswith("/") else self.path(key.rstrip("/")).as_uri() + "/"

    def put_file(self, key: str, src: Path) -> StoredFile:
        dest = self.path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return StoredFile(clean_key(key), dest.stat().st_size)

    def put_bytes(self, key: str, data: bytes) -> StoredFile:
        dest = self.path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return StoredFile(clean_key(key), len(data))

    def get_bytes(self, key: str) -> bytes:
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError as e:
            raise StorageError(f"no such file: {key}") from e

    def open(self, key: str):
        try:
            return open(self.path(key), "rb")  # noqa: SIM115 - the caller closes it (a streaming response)
        except FileNotFoundError as e:
            raise StorageError(f"no such file: {key}") from e

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def list(self, prefix: str) -> Iterator[StoredFile]:
        base = self.root / clean_key(prefix)
        if not base.is_dir():
            return
        for p in sorted(base.rglob("*")):
            if p.is_file():
                yield StoredFile(p.relative_to(self.root).as_posix(), p.stat().st_size)

    def delete_prefix(self, prefix: str) -> None:
        shutil.rmtree(self.root / clean_key(prefix), ignore_errors=True)

    def download_url(self, key: str, filename: str, seconds: int = 300) -> str | None:
        """Local files are streamed by the page that asks; there is no URL to hand out."""
        return None


class S3Store:
    """Keys as objects under one bucket and prefix."""

    def __init__(self, bucket: str, prefix: str = "", client=None):
        self.bucket, self.prefix = bucket, prefix.strip("/")
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.client = client

    def _key(self, key: str) -> str:
        k = clean_key(key) + ("/" if key.endswith("/") else "")
        return f"{self.prefix}/{k}" if self.prefix else k

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._key(key)}"

    def put_file(self, key: str, src: Path) -> StoredFile:
        self.client.upload_file(str(src), self.bucket, self._key(key))
        return StoredFile(clean_key(key), Path(src).stat().st_size)

    def put_bytes(self, key: str, data: bytes) -> StoredFile:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)
        return StoredFile(clean_key(key), len(data))

    def get_bytes(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()
        except self.client.exceptions.NoSuchKey as e:
            raise StorageError(f"no such file: {key}") from e

    def open(self, key: str):
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"]
        except self.client.exceptions.NoSuchKey as e:
            raise StorageError(f"no such file: {key}") from e

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError:
            return False

    def list(self, prefix: str) -> Iterator[StoredFile]:
        start = self._key(prefix).rstrip("/") + "/"
        strip = len(self.prefix) + 1 if self.prefix else 0
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=start):
            for obj in page.get("Contents", []):
                yield StoredFile(obj["Key"][strip:], obj["Size"])

    def delete_prefix(self, prefix: str) -> None:
        keys = [{"Key": self._key(f.key)} for f in self.list(prefix)]
        for i in range(0, len(keys), 1000):
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys[i : i + 1000]})

    def download_url(self, key: str, filename: str, seconds: int = 300) -> str | None:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._key(key), "ResponseContentDisposition": f'attachment; filename="{filename}"'},
            ExpiresIn=seconds,
        )


AREAS = ("uploads", "results", "refdata", "snapshots")
_stores: dict = {}


def store(area: str = "results"):
    """The configured store for one area (one per process)."""
    if area not in AREAS:
        raise StorageError(f"no storage area {area!r}")
    if area not in _stores:
        cfg = settings.CEDARS_STORAGE
        if cfg["backend"] == "s3":
            bucket = cfg["buckets"].get(area)
            if not bucket:
                raise StorageError(f"set CEDARS_BUCKET_{area.upper()} for the {area} bucket")
            _stores[area] = S3Store(bucket)
        else:
            _stores[area] = LocalStore(Path(cfg["root"]) / area)
    return _stores[area]


def reset() -> None:
    """Forget the stores (tests that change CEDARS_STORAGE)."""
    _stores.clear()
