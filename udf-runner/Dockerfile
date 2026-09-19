# Untrusted-code runner: executes one agent UDF with no network (the SageMaker Processing stand-in).
FROM python:3.12-slim
RUN pip install --no-cache-dir pandas==3.0.5 numpy==2.5.3 pyarrow==25.0.1
COPY runner.py /opt/runner/runner.py
USER 10001:10001
ENTRYPOINT ["python", "/opt/runner/runner.py"]
