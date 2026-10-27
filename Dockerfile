# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
#
# The meeseeks API. Config is mounted at /etc/meeseeks and state, including
# the ssh key and known_hosts, at /var/lib/meeseeks; see examples/compose.yaml.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

# ssh refuses to run for a uid with no passwd entry, so the user is created
# with a fixed uid and gid that host directories can be owned by. The gid is
# explicit because --user-group alone picks one from the system range.
RUN groupadd --system --gid 10001 meeseeks \
    && useradd --system --uid 10001 --gid 10001 --home-dir /var/lib/meeseeks --shell /usr/sbin/nologin meeseeks

COPY pyproject.toml LICENSE /src/
COPY meeseeks /src/meeseeks
RUN pip install --no-cache-dir '/src[api]'

USER meeseeks
ENV HOME=/var/lib/meeseeks PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
ENTRYPOINT ["meeseeks-api"]
CMD ["--config", "/etc/meeseeks/api.toml"]
