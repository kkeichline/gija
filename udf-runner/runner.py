"""Runs one agent-submitted UDF inside a network-isolated job pod.

Mirrors SageMaker Processing's container layout: the filtered snapshot is under
/opt/ml/processing/input, the UDF under /opt/ml/processing/code, and the artifact
goes to /opt/ml/processing/output. This process executes agent code, so nothing
here is trusted for enforcement; the gateway's release rule checks the output.
"""

import importlib.util
import os
import sys

import pandas as pd

INPUT = "/opt/ml/processing/input/data.parquet"
CODE = "/opt/ml/processing/code/udf.py"
OUTPUT_DIR = "/opt/ml/processing/output"


def main() -> None:
    df = pd.read_parquet(INPUT)
    spec = importlib.util.spec_from_file_location("udf", CODE)
    udf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(udf)

    result = udf.run(df)
    if not isinstance(result, pd.DataFrame):
        raise TypeError(f"run() must return a pandas DataFrame, got {type(result).__name__}")
    result.to_csv(os.path.join(OUTPUT_DIR, os.environ["ARTIFACT_FILE"]), index=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # the gateway reads a truncated summary from the termination message
        with open("/dev/termination-log", "w") as f:
            f.write(f"{type(exc).__name__}: {exc}"[:300])
        sys.exit(1)
