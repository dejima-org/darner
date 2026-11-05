# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
#
# The darner API. Config is mounted at /etc/darner and state, including
# the ssh key and known_hosts, at /var/lib/darner; see examples/compose.yaml.
# By digest, so the base can't change unreviewed. To update it, take the new
# digest from: docker buildx imagetools inspect python:3.12-slim
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

# ssh refuses to run for a uid with no passwd entry, so the user is created
# with a fixed uid and gid that host directories can be owned by. The gid is
# explicit because --user-group alone picks one from the system range.
RUN groupadd --system --gid 10001 darner \
    && useradd --system --uid 10001 --gid 10001 --home-dir /var/lib/darner --shell /usr/sbin/nologin darner

# Every dependency is pinned with its hashes, so an upstream release can't
# change the image unreviewed. The package itself is built with the pinned
# setuptools, without fetching anything else.
COPY requirements/api.txt /src/requirements/api.txt
RUN pip install --no-cache-dir --require-hashes -r /src/requirements/api.txt
COPY pyproject.toml LICENSE /src/
COPY darner /src/darner
RUN pip install --no-cache-dir --no-deps --no-build-isolation /src

USER darner
ENV HOME=/var/lib/darner PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
ENTRYPOINT ["darner-api"]
CMD ["--config", "/etc/darner/api.toml"]
