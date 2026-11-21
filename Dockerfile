# syntax=docker/dockerfile:1
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /research
COPY requirements.lock /research/
RUN pip install --disable-pip-version-check --no-cache-dir -r requirements.lock
ADD --checksum=sha256:93b2445f15f395b81c20556777ece19a6c7507df950f13fa9fac4a3be739af0d https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/base.tar.gz
RUN mkdir -p /opt/base && tar -xzf /tmp/base.tar.gz -C /opt/base --strip-components=1 && rm /tmp/base.tar.gz && \
    python -m compileall -q /opt/base/sqlglot /opt/base/tests
WORKDIR /app
COPY *.py *.gz position-readers.json /app/
RUN python -B prepare.py /opt/base
ENV PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
ENTRYPOINT ["python", "-B", "select_tests.py"]
