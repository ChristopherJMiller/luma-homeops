# Built on the ceph image so `ceph` + `cephfs-shell` are present without
# fighting Debian's stale ceph-common. Trade-off: ~1 GB base layer. For a
# single-replica operator that's fine; pulled once per node.
FROM quay.io/ceph/ceph:v19.2.3

# CentOS Stream 9's default python3 is 3.9; kopf >=1.39 requires >=3.10.
# Install 3.12 from AppStream and use it for everything operator-related.
RUN dnf install -y python3.12 python3.12-pip \
 && dnf clean all \
 && rm -rf /var/cache/dnf

WORKDIR /app

COPY requirements.txt ./
RUN python3.12 -m pip install --no-cache-dir --upgrade pip \
 && python3.12 -m pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# kopf calls getpass.getuser() at startup; that fails with KeyError when
# the running UID has no /etc/passwd entry. Add one for UID 2016 so kopf
# can derive its identity field.
RUN echo 'operator:x:2016:2016::/app:/sbin/nologin' >> /etc/passwd \
 && echo 'operator:x:2016:' >> /etc/group

# Match the rook-ceph-tools UID so volume mounts (rook-ceph-mon secret,
# mon-endpoints ConfigMap) are readable without chowning.
USER 2016:2016

ENV PYTHONUNBUFFERED=1

# --liveness exposes /healthz on :8080 for the kubelet probe. Invoke kopf
# via `python3.12 -m kopf` so we're not relying on the kopf entrypoint
# script landing on $PATH.
ENTRYPOINT ["python3.12", "-m", "kopf", "run", "--standalone", \
            "--liveness=http://0.0.0.0:8080/healthz", \
            "/app/src/operator.py"]
