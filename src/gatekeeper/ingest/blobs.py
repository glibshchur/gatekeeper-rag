"""Content-addressed blob storage for raw documents.

Keys are derived from the SHA-256 of the bytes, not from the path. Two consequences worth
having: identical content stored under different paths is deduplicated for free, and
re-ingesting an unchanged document is a no-op rather than an overwrite. The blob store is
therefore append-only in practice, which is the right property for a corpus that an audit
log points at.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from gatekeeper.config import get_settings

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

logger = logging.getLogger(__name__)


def blob_key(tenant_slug: str, source: str, content_hash: str) -> str:
    # The two-character shard keeps object listings usable at corpus scale.
    return f"{tenant_slug}/{source}/{content_hash[:2]}/{content_hash}"


@lru_cache
def client() -> S3Client:
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
        region_name="us-east-1",
    )


def ensure_bucket() -> str:
    bucket = get_settings().s3_bucket
    try:
        client().head_bucket(Bucket=bucket)
    except ClientError:
        client().create_bucket(Bucket=bucket)
        logger.info("created bucket %s", bucket)
    return bucket


def exists(key: str) -> bool:
    try:
        client().head_object(Bucket=get_settings().s3_bucket, Key=key)
    except ClientError:
        return False
    return True


def put(key: str, data: bytes, content_type: str = "text/markdown") -> str:
    if exists(key):
        return key  # content-addressed: same key means identical bytes
    client().put_object(
        Bucket=get_settings().s3_bucket, Key=key, Body=data, ContentType=content_type
    )
    return key


def get(key: str) -> bytes:
    response = client().get_object(Bucket=get_settings().s3_bucket, Key=key)
    return bytes(response["Body"].read())
