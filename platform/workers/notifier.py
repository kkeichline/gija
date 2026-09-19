"""Notification worker: SNS -> SQS -> OpenClaw, the stand-in for EventBridge + SNS paging a human.

Runs as a sidecar in the OpenClaw pod, so it reaches the hook endpoint on loopback
and OpenClaw itself can stay bound to 127.0.0.1. The wake text is built only from
fields the gateway controls; UDF error text never goes in it, because a wake is a
trusted system event in OpenClaw's main session.
"""

import json
import logging
import os
import time
import urllib.request

import boto3

HOOK_URL = os.environ.get("OPENCLAW_HOOK_URL", "http://127.0.0.1:18789/hooks/wake")
AGENT_ID = os.environ.get("OPENCLAW_AGENT_ID", "main")

sns = boto3.client("sns")
sqs = boto3.client("sqs")
log = logging.getLogger("notifier")
logging.basicConfig(level=logging.INFO, format="%(asctime)s notifier %(message)s")


def subscribe() -> str:
    topic_arn = sns.create_topic(Name="run-events")["TopicArn"]
    queue_url = sqs.create_queue(QueueName="run-events-openclaw")["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn,
                  Attributes={"RawMessageDelivery": "true"})
    return queue_url


def wake_text(event: dict) -> str:
    text = (f"[governed-gateway] Run {event['run_id']} ({event['problem']} on {event['dataset']}) "
            f"finished: {event['status'].upper()}")
    if event["status"] == "released":
        text += f", {event['rows']} row(s) released"
        if event["notes"]:
            text += "; " + "; ".join(event["notes"])
    elif event["status"] == "failed":
        text += ". The UDF raised an error; the agent can see a truncated message via get_run"
    return text + "."


def notify(event: dict) -> None:
    body = json.dumps({"text": wake_text(event), "mode": "now", "agentId": AGENT_ID}).encode()
    req = urllib.request.Request(HOOK_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {os.environ['OPENCLAW_HOOKS_TOKEN']}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        log.info("woke OpenClaw for %s (HTTP %s)", event["run_id"], resp.status)


def main() -> None:
    while True:
        try:
            queue_url = subscribe()
            break
        except Exception as exc:  # moto not up yet
            log.info("waiting for moto: %s", exc)
            time.sleep(3)
    while True:
        msgs = sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=10, MaxNumberOfMessages=5).get("Messages", [])
        for msg in msgs:
            try:
                notify(json.loads(msg["Body"]))
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
            except Exception as exc:  # leave it on the queue; SQS redelivers after the visibility timeout
                log.warning("notify failed, will retry: %s", exc)


if __name__ == "__main__":
    main()
