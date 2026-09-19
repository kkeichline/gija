"""UDF promotion worker: the stand-in for the Lambda that moves UDFs out of staging.

A staged UDF is promoted to the approved bucket only when the gateway has tagged it
released=true (it produced an artifact that passed the release rule) AND approved=true
(a human approved it in a research workflow). The agent has no credentials for either
bucket, so it cannot promote anything itself.
"""

import logging
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

STAGING_BUCKET = "udf-staging"
APPROVED_BUCKET = "udf-approved"

s3 = boto3.client("s3")
log = logging.getLogger("mover")
logging.basicConfig(level=logging.INFO, format="%(asctime)s mover %(message)s")


def promoted(key: str) -> bool:
    try:
        s3.head_object(Bucket=APPROVED_BUCKET, Key=key)
        return True
    except ClientError:
        return False


def sweep() -> None:
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=STAGING_BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            tags = {t["Key"]: t["Value"] for t in s3.get_object_tagging(Bucket=STAGING_BUCKET, Key=key)["TagSet"]}
            if tags.get("released") != "true" or tags.get("approved") != "true" or promoted(key):
                continue
            s3.copy_object(Bucket=APPROVED_BUCKET, Key=key, CopySource={"Bucket": STAGING_BUCKET, "Key": key})
            log.info("promoted s3://%s/%s -> s3://%s/%s (approved by %s)",
                     STAGING_BUCKET, key, APPROVED_BUCKET, key, tags.get("approved_by", "?"))


def main() -> None:
    while True:
        try:
            try:
                s3.create_bucket(Bucket=APPROVED_BUCKET)
            except s3.exceptions.BucketAlreadyOwnedByYou:
                pass
            sweep()
        except (ClientError, BotoCoreError) as exc:  # moto starting, or no staging bucket yet
            log.info("waiting: %s", exc)
        time.sleep(5)


if __name__ == "__main__":
    main()
