"""Temporal worker: runs ResearchRun workflows and their activities."""

import asyncio
import logging
import os

from kubernetes import config as k8s_config
from temporalio.client import Client
from temporalio.worker import Worker

from launcher.activities import approve_run, fetch_runs, review_run, run_harness_session, session_activity
from launcher.workflows import TASK_QUEUE, ResearchRun


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    k8s_config.load_incluster_config()
    temporal = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "temporal.temporal.svc.cluster.local:7233"))
    worker = Worker(temporal, task_queue=TASK_QUEUE, workflows=[ResearchRun],
                    activities=[run_harness_session, fetch_runs, review_run, session_activity, approve_run])
    logging.info("research worker polling task queue %r", TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
