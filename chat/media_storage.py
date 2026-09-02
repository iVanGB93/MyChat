"""DigitalOcean Spaces adapter for Axonic's out-of-band media lane."""

from __future__ import annotations

from typing import BinaryIO

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def uses_spaces() -> bool:
    return getattr(settings, "MEDIA_STORAGE_BACKEND", "database") == "spaces"


def _client():
    missing = [
        name
        for name in ("SPACES_BUCKET", "SPACES_ACCESS_KEY", "SPACES_SECRET_KEY")
        if not getattr(settings, name, "")
    ]
    if missing:
        raise ImproperlyConfigured(
            "Spaces media storage is enabled but these settings are missing: "
            + ", ".join(missing)
        )

    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=settings.SPACES_REGION,
        endpoint_url=settings.SPACES_ENDPOINT,
        aws_access_key_id=settings.SPACES_ACCESS_KEY,
        aws_secret_access_key=settings.SPACES_SECRET_KEY,
        config=Config(signature_version="s3v4"),
    )


def object_key_for(blob) -> str:
    return f"media/{blob.room_id}/{blob.id}"


def create_presigned_upload(*, key: str, mime: str, md5: str) -> dict:
    params = {
        "Bucket": settings.SPACES_BUCKET,
        "Key": key,
        "ContentType": mime,
        "Metadata": {"md5": md5},
    }
    url = _client().generate_presigned_url(
        "put_object",
        Params=params,
        ExpiresIn=settings.MEDIA_PRESIGNED_UPLOAD_SECONDS,
        HttpMethod="PUT",
    )
    return {
        "url": url,
        "headers": {
            "Content-Type": mime,
            "x-amz-meta-md5": md5,
        },
    }


def create_multipart_upload(*, key: str, mime: str, md5: str) -> str:
    response = _client().create_multipart_upload(
        Bucket=settings.SPACES_BUCKET,
        Key=key,
        ContentType=mime,
        Metadata={"md5": md5},
    )
    return response["UploadId"]


def create_presigned_part_upload(*, key: str, upload_id: str, part_number: int) -> str:
    return _client().generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": settings.SPACES_BUCKET,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=settings.MEDIA_PRESIGNED_UPLOAD_SECONDS,
        HttpMethod="PUT",
    )


def list_multipart_parts(*, key: str, upload_id: str) -> list[dict]:
    client = _client()
    parts = []
    marker = 0
    while True:
        response = client.list_parts(
            Bucket=settings.SPACES_BUCKET,
            Key=key,
            UploadId=upload_id,
            PartNumberMarker=marker,
        )
        parts.extend(response.get("Parts", []))
        if not response.get("IsTruncated"):
            return parts
        marker = int(response.get("NextPartNumberMarker", 0))


def complete_multipart_upload(*, key: str, upload_id: str, parts: list[dict]) -> None:
    _client().complete_multipart_upload(
        Bucket=settings.SPACES_BUCKET,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={
            "Parts": [
                {"PartNumber": int(part["PartNumber"]), "ETag": part["ETag"]}
                for part in parts
            ]
        },
    )


def abort_multipart_upload(*, key: str, upload_id: str) -> None:
    try:
        _client().abort_multipart_upload(
            Bucket=settings.SPACES_BUCKET,
            Key=key,
            UploadId=upload_id,
        )
    except Exception as error:
        response = getattr(error, "response", None) or {}
        code = str((response.get("Error") or {}).get("Code", ""))
        if code not in {"NoSuchUpload", "404"}:
            raise


def upload_file(*, key: str, fileobj: BinaryIO, mime: str, md5: str) -> None:
    _client().upload_fileobj(
        fileobj,
        settings.SPACES_BUCKET,
        key,
        ExtraArgs={
            "ContentType": mime,
            "Metadata": {"md5": md5},
        },
    )


def inspect_object(key: str) -> dict:
    return _client().head_object(Bucket=settings.SPACES_BUCKET, Key=key)


def create_presigned_download(*, key: str, disposition: str) -> str:
    return _client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.SPACES_BUCKET,
            "Key": key,
            "ResponseContentDisposition": disposition,
        },
        ExpiresIn=settings.MEDIA_PRESIGNED_DOWNLOAD_SECONDS,
        HttpMethod="GET",
    )


def delete_object(key: str) -> None:
    _client().delete_object(Bucket=settings.SPACES_BUCKET, Key=key)
