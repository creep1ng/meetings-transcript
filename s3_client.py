"""S3 client for listing, downloading, and uploading objects with resilience."""

import io
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionError,
    ReadTimeoutError,
)

from config import Config as AppConfig

logger = logging.getLogger(__name__)


@dataclass
class S3Object:
    """Represents an S3 object with metadata."""

    key: str
    size_bytes: int
    last_modified: datetime
    etag: Optional[str] = None
    storage_class: Optional[str] = None


class S3Client:
    """S3 client with pagination, streaming, and atomic uploads."""

    def __init__(self, config: AppConfig):
        """Initialize S3 client with configuration.

        Args:
            config: Application configuration object.
        """
        self.config = config
        self._client = None
        self._resource = None

    @property
    def client(self):
        """Lazy initialize boto3 S3 client with retries and timeouts."""
        if self._client is None:
            client_config = Config(
                region_name=self.config.aws_region,
                retries={"max_attempts": self.config.max_retries, "mode": "adaptive"},
                read_timeout=self.config.s3_timeout,
                connect_timeout=self.config.s3_timeout,
            )

            # Check if we have credentials or should use IAM role
            if self.config.aws_access_key_id and self.config.aws_secret_access_key:
                self._client = boto3.client(
                    "s3",
                    aws_access_key_id=self.config.aws_access_key_id,
                    aws_secret_access_key=self.config.aws_secret_access_key,
                    config=client_config,
                )
            else:
                # Use IAM role or SSO
                logger.info("No AWS credentials found; using IAM role/SSO")
                self._client = boto3.client("s3", config=client_config)

        return self._client

    @property
    def resource(self):
        """Lazy initialize boto3 S3 resource."""
        if self._resource is None:
            client_config = Config(
                region_name=self.config.aws_region,
                retries={"max_attempts": self.config.max_retries, "mode": "adaptive"},
                read_timeout=self.config.s3_timeout,
                connect_timeout=self.config.s3_timeout,
            )

            if self.config.aws_access_key_id and self.config.aws_secret_access_key:
                self._resource = boto3.resource(
                    "s3",
                    aws_access_key_id=self.config.aws_access_key_id,
                    aws_secret_access_key=self.config.aws_secret_access_key,
                    config=client_config,
                )
            else:
                self._resource = boto3.resource("s3", config=client_config)

        return self._resource

    def _retry_with_backoff(self, func, operation_name: str):
        """Execute function with exponential backoff for transient errors.

        Args:
            func: Function to execute.
            operation_name: Name of the operation for logging.

        Returns:
            Result of the function.

        Raises:
            Exception: Last exception after retries exhausted.
        """
        last_exception = None
        base_delay = self.config.retry_backoff_base

        for attempt in range(self.config.max_retries + 1):
            try:
                return func()
            except (ConnectionError, ReadTimeoutError) as e:
                last_exception = e
                if attempt < self.config.max_retries:
                    delay = base_delay**attempt
                    logger.warning(
                        f"Transient error during {operation_name}: {e}. "
                        f"Retrying in {delay:.1f}s (attempt {attempt + 1}/{self.config.max_retries + 1})"
                    )
                    time.sleep(delay)
                else:
                    logger.error(f"Permanent error during {operation_name}: {e}")
                    raise
            except (BotoCoreError, ClientError) as e:
                error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
                # Retry on specific transient error codes
                if error_code in (
                    "ThrottlingException",
                    "ProvisionedThroughputExceededException",
                ):
                    last_exception = e
                    if attempt < self.config.max_retries:
                        delay = base_delay**attempt
                        logger.warning(
                            f"S3 throttling during {operation_name}: {error_code}. "
                            f"Retrying in {delay:.1f}s (attempt {attempt + 1}/{self.config.max_retries + 1})"
                        )
                        time.sleep(delay)
                    else:
                        raise
                else:
                    # Non-retryable error
                    logger.error(
                        f"S3 error during {operation_name}: {error_code} - {e}"
                    )
                    raise
            except Exception as e:
                logger.error(f"Unexpected error during {operation_name}: {e}")
                raise

        if last_exception is not None:
            raise last_exception

    def list_objects(
        self,
        prefix: Optional[str] = None,
        extensions: Optional[tuple] = None,
        max_keys: int = 1000,
    ) -> Iterator[S3Object]:
        """List objects in S3 bucket with pagination and filtering.

        Args:
            prefix: Optional prefix to filter objects.
            extensions: Optional tuple of extensions to filter (e.g., (".mp4", ".mov")).
            max_keys: Maximum number of keys per page (default 1000, S3 limit).

        Yields:
            S3Object instances matching the criteria.
        """
        prefix = prefix or self.config.video_prefix
        paginator = self.client.get_paginator("list_objects_v2")
        page_iterator = paginator.paginate(
            Bucket=self.config.s3_bucket_name,
            Prefix=prefix,
            PaginationConfig={"MaxItems": max_keys},
        )

        for page in page_iterator:
            if "Contents" not in page:
                continue

            for obj in page["Contents"]:
                key = obj["Key"]

                # Filter by extension if specified
                if extensions:
                    ext = Path(key).suffix.lower()
                    if ext not in extensions:
                        continue

                yield S3Object(
                    key=key,
                    size_bytes=obj["Size"],
                    last_modified=obj["LastModified"],
                    etag=obj.get("ETag", "").strip('"'),
                )

    def list_videos(self, extensions: Optional[tuple] = None) -> List[S3Object]:
        """List video objects in the configured video prefix.

        Args:
            extensions: Optional tuple of extensions to filter.

        Returns:
            List of S3Object instances.
        """
        extensions = extensions or self.config.allowed_video_extensions
        return list(
            self.list_objects(prefix=self.config.video_prefix, extensions=extensions)
        )

    def download_to_stream(self, key: str) -> io.BytesIO:
        """Download an S3 object to a streaming buffer.

        Args:
            key: S3 object key.

        Returns:
            io.BytesIO buffer containing the object data.
        """
        buffer = io.BytesIO()

        def _download():
            self.client.download_fileobj(
                Bucket=self.config.s3_bucket_name,
                Key=key,
                Fileobj=buffer,
            )

        self._retry_with_backoff(_download, f"download {key}")
        buffer.seek(0)
        return buffer

    def download_to_file(self, key: str, dest_path: Path) -> Path:
        """Download an S3 object to a local file.

        Args:
            key: S3 object key.
            dest_path: Destination file path.

        Returns:
            Path to the downloaded file.
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        def _download():
            self.client.download_file(
                Bucket=self.config.s3_bucket_name,
                Key=key,
                Filename=str(dest_path),
            )

        self._retry_with_backoff(_download, f"download {key}")
        logger.info(f"Downloaded {key} to {dest_path}")
        return dest_path

    def upload_from_stream(
        self,
        key: str,
        stream: io.BytesIO,
        metadata: Optional[dict] = None,
        content_type: str = "text/plain",
    ) -> str:
        """Upload a stream to S3 atomically (via temp key + copy).

        Args:
            key: Destination S3 key.
            stream: Data stream to upload.
            metadata: Optional metadata dict.
            content_type: Content type of the data.

        Returns:
            Final S3 key after atomic move.
        """
        # Upload to temporary key first
        temp_key = (
            f"{self.config.transcripts_prefix}.tmp/{uuid.uuid4().hex}_{Path(key).name}"
        )

        def _upload_temp():
            extra_args = {
                "ContentType": content_type,
            }
            if metadata:
                # Prefix metadata keys with x-amz-meta-
                extra_args["Metadata"] = "; ".join(
                    f"{k}={v}" for k, v in metadata.items()
                )
            extra_args["ContentType"] = content_type

            self.client.upload_fileobj(
                Fileobj=stream,
                Bucket=self.config.s3_bucket_name,
                Key=temp_key,
                ExtraArgs=extra_args,
            )

        self._retry_with_backoff(_upload_temp, f"upload temp {temp_key}")

        # Get ETag of uploaded temp object
        temp_obj = self.resource.Object(self.config.s3_bucket_name, temp_key)
        temp_obj.load()
        etag = temp_obj.e_tag.strip('"')

        # Copy to final destination atomically
        final_key = f"{self.config.transcripts_prefix}{Path(key).stem}.txt"

        def _copy():
            self.client.copy_object(
                Bucket=self.config.s3_bucket_name,
                CopySource={"Bucket": self.config.s3_bucket_name, "Key": temp_key},
                Key=final_key,
                MetadataDirective="COPY",
            )

        self._retry_with_backoff(_copy, f"copy to {final_key}")

        # Delete temporary object
        def _delete_temp():
            self.client.delete_object(
                Bucket=self.config.s3_bucket_name,
                Key=temp_key,
            )

        self._retry_with_backoff(_delete_temp, f"delete temp {temp_key}")

        # Update metadata on final object if needed
        if metadata:

            def _update_metadata():
                self.client.put_object_metadata(
                    Bucket=self.config.s3_bucket_name,
                    Key=final_key,
                    Metadata={f"x-amz-meta-{k}": str(v) for k, v in metadata.items()},
                    MetadataDirective="REPLACE",
                )

            self._retry_with_backoff(_update_metadata, f"update metadata {final_key}")

        logger.info(f"Atomically uploaded to {final_key} (source etag: {etag})")
        return final_key

    def upload_text(
        self,
        key: str,
        text: str,
        metadata: Optional[dict] = None,
    ) -> str:
        """Upload text content to S3.

        Args:
            key: S3 key.
            text: Text content to upload.
            metadata: Optional metadata dict.

        Returns:
            Final S3 key.
        """
        stream = io.BytesIO(text.encode("utf-8"))
        return self.upload_from_stream(
            key=key,
            stream=stream,
            metadata=metadata,
            content_type="text/plain; charset=utf-8",
        )

    def get_object_metadata(self, key: str) -> dict:
        """Get metadata for an S3 object.

        Args:
            key: S3 object key.

        Returns:
            Dict containing metadata.
        """

        def _get():
            return self.client.head_object(
                Bucket=self.config.s3_bucket_name,
                Key=key,
            )

        response = self._retry_with_backoff(_get, f"head_object {key}")
        if response is None:
            return {}
        return response.get("Metadata", {})

    def object_exists(self, key: str) -> bool:
        """Check if an object exists in S3.

        Args:
            key: S3 object key.

        Returns:
            True if object exists.
        """

        def _check():
            try:
                self.client.head_object(
                    Bucket=self.config.s3_bucket_name,
                    Key=key,
                )
                return True
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    return False
                raise

        result = self._retry_with_backoff(_check, f"check existence {key}")
        return result if result is not None else False

    def delete_object(self, key: str) -> None:
        """Delete an object from S3.

        Args:
            key: S3 object key.
        """

        def _delete():
            self.client.delete_object(
                Bucket=self.config.s3_bucket_name,
                Key=key,
            )

        self._retry_with_backoff(_delete, f"delete {key}")
        logger.info(f"Deleted {key} from S3")
