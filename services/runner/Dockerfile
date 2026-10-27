FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# Unprivileged identity for generated code. No home, no shell, no sudo.
RUN useradd --uid 20000 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin sandbox
COPY requirements.txt /tmp/requirements.txt
RUN pip install --root-user-action=ignore -r /tmp/requirements.txt && rm /tmp/requirements.txt
# Sandbox-side helper: the governed MCP client, importable as `governed`.
COPY sandbox/governed.py /usr/local/lib/python3.12/site-packages/governed.py
COPY sandbox/launch.py /opt/sandbox/launch.py
# Server code is root-only; the sandbox uid cannot read it.
COPY runner /srv/runner
RUN chmod -R go-rwx /srv && chmod 0755 /opt/sandbox && chmod 0644 /opt/sandbox/launch.py

WORKDIR /srv
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --retries=20 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]
CMD ["uvicorn", "runner.server:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
