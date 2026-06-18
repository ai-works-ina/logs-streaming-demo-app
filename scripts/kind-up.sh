#!/usr/bin/env bash
# Bring the SRE lab up on a local kind cluster: create the cluster, build + load the app
# images, and apply the manifests. Idempotent — safe to re-run. Requires Docker running.
#
#   bash scripts/kind-up.sh
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER=sre-lab
SERVICES=(webapp api auth payments worker)   # build from services/<name>

echo "==> ensuring kind cluster '$CLUSTER'"
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind create cluster --name "$CLUSTER" --config k8s/kind-config.yaml
else
  echo "    (cluster already exists)"
fi

echo "==> building app images"
for svc in "${SERVICES[@]}"; do
  echo "    sre-lab-$svc:dev"
  docker build -q -t "sre-lab-$svc:dev" "services/$svc" >/dev/null
done
echo "    sre-lab-loadgen:dev"
docker build -q -t "sre-lab-loadgen:dev" loadgen >/dev/null

echo "==> loading images into kind"
for svc in "${SERVICES[@]}"; do
  kind load docker-image "sre-lab-$svc:dev" --name "$CLUSTER"
done
kind load docker-image "sre-lab-loadgen:dev" --name "$CLUSTER"

echo "==> applying manifests"
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmaps.yaml
kubectl apply -f k8s/data.yaml
# observability/ is applied here once it lands (task: observability stack on k8s)
[ -d k8s/observability ] && kubectl apply -f k8s/observability/ || true
kubectl apply -f k8s/app.yaml

echo "==> waiting for rollouts"
for d in postgres redis gateway webapp api auth payments worker loadgen; do
  kubectl -n lab rollout status "deploy/$d" --timeout=120s || true
done

echo "==> done. lab at http://localhost:8080  (kubectl -n lab get pods)"
