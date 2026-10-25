# Public registry manifest-list digests resolved and verified during M3A.
FROM node:22.14.0-bookworm-slim@sha256:1c18d9ab3af4585870b92e4dbc5cac5a0dc77dd13df1a5905cea89fc720eb05b AS frontend
WORKDIR /opt/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund

FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db
ARG TRUSTED_REVISION
LABEL taskly.trusted_revision=$TRUSTED_REVISION taskly.profile="v1"
COPY --from=frontend /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend /usr/lib/x86_64-linux-gnu/libstdc++.so.6* /usr/lib/x86_64-linux-gnu/
COPY --from=frontend /lib/x86_64-linux-gnu/libgcc_s.so.1 /usr/lib/x86_64-linux-gnu/libgcc_s.so.1
COPY --from=frontend /opt/frontend/node_modules /opt/frontend/node_modules
COPY --from=frontend /opt/frontend/package.json /opt/frontend/package-lock.json /opt/frontend/
COPY backend/requirements.txt /opt/backend-requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: -r /opt/backend-requirements.txt \
    && /opt/venv/bin/python --version && node --version \
    && mkdir /workspace /reports && chmod 755 /workspace /reports
COPY agent/src /opt/agent
COPY agent/sandbox/launch.py /opt/taskly/launch.py
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/opt/venv/bin/python", "-I", "/opt/taskly/launch.py"]
CMD []
