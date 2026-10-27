FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONPATH=/suite
RUN useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin tester
WORKDIR /suite
COPY tests/requirements.txt ./requirements.txt
RUN pip install --root-user-action=ignore -r requirements.txt
# Unit tests import the real validator / identity code from the data service.
COPY services/data_service/data_service ./data_service
COPY services/agent/agent/prepared ./prepared
COPY tests ./tests
COPY experiments ./experiments

USER 10002
ENTRYPOINT ["pytest", "-p", "no:cacheprovider"]
CMD ["tests", "-q", "-rA", "--junitxml=/reports/pytest.xml"]
