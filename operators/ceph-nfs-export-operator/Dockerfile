# Built on the ceph image so libcephfs (via python3-cephfs) and the ceph
# CLI are present natively. Trade-off: ~1 GB base layer; pulled once per
# node. We stay on the image's system Python (3.9 on CentOS Stream 9)
# because python3-cephfs is built only for the system interpreter — kopf
# is therefore pinned to <1.39 (the last py3.9-compatible series) until
# the ceph base image moves to a newer Stream / Python.
FROM quay.io/ceph/ceph:v19.2.3

RUN dnf install -y python3-pip \
 && dnf clean all \
 && rm -rf /var/cache/dnf

WORKDIR /app

COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir --upgrade pip \
 && python3 -m pip install --no-cache-dir -r requirements.txt

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

# --liveness exposes /healthz on :8080 for the kubelet probe.
# --all-namespaces is required explicitly in kopf 1.38+ (omitting it
# emits a FutureWarning about becoming an error in a future release).
ENTRYPOINT ["python3", "-m", "kopf", "run", "--standalone", \
            "--all-namespaces", \
            "--liveness=http://0.0.0.0:8080/healthz", \
            "/app/src/operator.py"]
