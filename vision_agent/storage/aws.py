"""
AWS storage backends. Activated by STORAGE_BACKEND=s3 in .env — zero agent code changes.

Deployment path from the AWS architecture diagram:
  Robot camera → capture_screen() → S3 (via robot SDK)
  MSK robot.{id}.image topic → Lambda → SQS → SQSImageQueue.receive_next()
  Vision results → S3 (via save_json) → RDS via downstream Lambda
"""
import json


class S3Storage:
    def __init__(self, bucket: str, prefix: str = ""):
        import boto3
        self._s3 = boto3.client("s3")
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
