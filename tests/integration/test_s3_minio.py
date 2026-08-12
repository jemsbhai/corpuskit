"""Opt-in real MinIO acceptance for the production S3 adapter."""

from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import pytest

from corpuskit.config import Settings
from corpuskit.persistence.artifact_store import ObjectNotFoundError, S3ObjectStore


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_minio_private_content_address_roundtrip() -> None:
    endpoint = os.environ.get("CORPUSKIT_TEST_S3_ENDPOINT")
    if endpoint is None:
        pytest.skip("set CORPUSKIT_TEST_S3_ENDPOINT to run the real MinIO contract")
    settings = Settings(
        environment="test",
        artifact_backend="s3",
        artifact_s3_endpoint=endpoint,
        artifact_s3_bucket=os.environ.get("CORPUSKIT_TEST_S3_BUCKET", "corpuskit-artifacts"),
        artifact_s3_region="us-east-1",
        artifact_s3_access_key_id=os.environ.get("CORPUSKIT_TEST_S3_ACCESS_KEY", "corpuskit-local"),
        artifact_s3_secret_access_key=os.environ.get(
            "CORPUSKIT_TEST_S3_SECRET_KEY", "corpuskit-local-secret"
        ),
        artifact_s3_path_style=True,
        _env_file=None,
    )
    store = S3ObjectStore.from_settings(settings)
    content = b'{"minio":"verified"}'
    digest = hashlib.sha256(content).hexdigest()
    nonce = uuid4().hex
    key = f"artifacts/v1/{nonce}/{nonce}/project/corpus-text/{digest[:2]}/{digest}"

    try:
        created = await store.put(
            key=key,
            content=content,
            sha256=digest,
            media_type="application/json",
        )
        duplicate = await store.put(
            key=key,
            content=content,
            sha256=digest,
            media_type="application/json",
        )
        descriptor = await store.stat(key)
        opened = await store.open(key, chunk_bytes=5)
        downloaded = b"".join([chunk async for chunk in opened.chunks])
        signed = await store.presign_get(
            key,
            expires_seconds=30,
            content_disposition='attachment; filename="minio.json"',
        )

        assert created.created is True
        assert duplicate.created is False
        assert descriptor.sha256 == digest
        assert descriptor.media_type == "application/json"
        assert downloaded == content
        assert key in await store.list_keys(f"artifacts/v1/{nonce}", limit=10)
        assert signed is not None
        assert "X-Amz-Expires=30" in signed
    finally:
        await store.delete(key)
    with pytest.raises(ObjectNotFoundError):
        await store.stat(key)
