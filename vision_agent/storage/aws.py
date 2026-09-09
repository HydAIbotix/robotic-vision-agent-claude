"""
S3-compatible object storage. Activated by STORAGE_BACKEND in {s3, minio, gcs, azure} — zero
agent code changes. This ONE class talks to any S3-compatible store; the target is chosen by
`s3_endpoint_url` (see vision_agent/storage/__init__.py), so it is fully cloud-agnostic:

  AWS S3      → endpoint blank (default AWS endpoint), default cred chain / IAM role
  MinIO       → endpoint http://minio:9000, path-style addressing, static keys (self-hosted, any cloud)
  GCS         → endpoint https://storage.googleapis.com  (S3 interoperability mode)
  Azure Blob  → endpoint of an S3-compatible gateway (e.g. via a proxy) — same code path

Historical AWS event path (unused off AWS): MSK robot.{id}.image → Lambda → SQS →
SQSImageQueue.receive_next(). On other clouds the event bus (Redis) replaces MSK/SQS.
"""
import json


class S3Storage:
    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        path_style: bool = True,
    ):
        import boto3
        from botocore.config import Config

        # path-style addressing (bucket in the URL path, not the host) is required by MinIO and
        # most self-hosted / non-AWS S3 gateways; harmless against real AWS S3.
        cfg = Config(s3={"addressing_style": "path" if path_style else "auto"}) if endpoint_url else None
        client_kwargs: dict = {}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        if region:
            client_kwargs["region_name"] = region
        # Explicit static keys only when provided; otherwise boto3's default provider chain
        # (env / instance role / workload identity) is used — the AWS-native behaviour.
        if access_key and secret_key:
            client_kwargs["aws_access_key_id"] = access_key
            client_kwargs["aws_secret_access_key"] = secret_key
        if cfg is not None:
            client_kwargs["config"] = cfg
        self._s3 = boto3.client("s3", **client_kwargs)
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def load(self, source: str) -> bytes:
        # Accept bare key or s3://bucket/key URI
        key = source.removeprefix(f"s3://{self._bucket}/")
        return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def save(self, data: bytes, key: str) -> str:
        full = self._full_key(key)
        self._s3.put_object(Bucket=self._bucket, Key=full, Body=data)
        return f"s3://{self._bucket}/{full}"

    def save_json(self, data: dict, key: str) -> str:
        return self.save(json.dumps(data, indent=2).encode(), key)


class SQSImageQueue:
    """
    Consume robot camera-ready events from SQS.
    These are published by the Lambda consumer that bridges MSK → SQS
    (as shown in the Stream Processing layer of the AWS architecture).
    Each message body: {"robot_id": "...", "s3_key": "...", "timestamp": ...}
    """
    def __init__(self, queue_url: str):
        import boto3
        self._sqs = boto3.client("sqs")
        self._url = queue_url

    def receive_next(self, wait_seconds: int = 5) -> dict | None:
        """Long-poll for the next image event. Returns the parsed message or None."""
        resp = self._sqs.receive_message(
            QueueUrl=self._url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=wait_seconds,
        )
        msgs = resp.get("Messages", [])
        if not msgs:
            return None
        m = msgs[0]
        self._sqs.delete_message(QueueUrl=self._url, ReceiptHandle=m["ReceiptHandle"])
        return json.loads(m["Body"])
