#!/usr/bin/env bash
# Sets up Tier B (experiments/TIERB.md) on one machine: tools, the simulator and spec binaries, a kind cluster with
# KWOK, and the controller. Run from the repository root of a checkout of this branch, with Docker running and Go on
# PATH. It deletes and recreates the kind cluster $KIND_CLUSTER_NAME. When it finishes, $TIERB_HOME/env.sh holds the
# environment experiments/tierb/run_tierb.py needs (experiments/tierb/env/run-driver.sh sources it).
#
# This is the sequence the Tier B run used (experiments/RESEARCH_LOG.md, "Tier B environment"), in one script:
#   - the kind config adds containerd's restrict_oom_score_adj, for hosts that withhold CAP_SYS_RESOURCE;
#   - the KWOK image is pulled on the host and loaded into the node, for hosts whose proxy the node cannot reach;
#   - the controller image, the simulator and the chart all come from a clean clone at $IMAGE_SHA. A git worktree
#     would leave the simulator without a VCS stamp.
set -euo pipefail

TIERB_HOME=${TIERB_HOME:-/tmp/tierb}
IMAGE_SHA=${IMAGE_SHA:-eff89a17bb3a7c22ac2db77621d72d96f2843215}
KIND_CLUSTER_NAME=${KIND_CLUSTER_NAME:-test-cluster}
KIND_NODE_IMAGE=${KIND_NODE_IMAGE:-kindest/node:v1.34.0}
KWOK_IMAGE=registry.k8s.io/kwok/kwok:v0.8.0
ENV_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(pwd)
[ -f "$REPO/experiments/TIERB.md" ] || { echo "run from the repository root" >&2; exit 1; }
mkdir -p "$TIERB_HOME/bin"
export PATH=$TIERB_HOME/bin:$PATH

echo "== tools"
GOBIN=$TIERB_HOME/bin go install sigs.k8s.io/kind@v0.30.0
GOBIN=$TIERB_HOME/bin go install github.com/google/ko@v0.18.0
if ! command -v helm >/dev/null; then
  curl -fsSL -o "$TIERB_HOME/helm.tgz" https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz
  tar -xzf "$TIERB_HOME/helm.tgz" -C "$TIERB_HOME" && mv "$TIERB_HOME/linux-amd64/helm" "$TIERB_HOME/bin/helm"
fi
command -v kubectl >/dev/null || { echo "kubectl is needed on PATH" >&2; exit 1; }
docker info >/dev/null || { echo "Docker is not running" >&2; exit 1; }

echo "== binaries"
rm -rf "$TIERB_HOME/clone"
git clone -q "$REPO" "$TIERB_HOME/clone"
git -C "$TIERB_HOME/clone" checkout -q --detach "$IMAGE_SHA"
(cd "$TIERB_HOME/clone" && go build -o "$TIERB_HOME/bin/csim" ./hack/consolidation-sim)
# The Tier B spec comes from this checkout. go test -c writes no VCS stamp; the driver records this checkout's HEAD
# in each cell's manifest instead.
(cd "$REPO/test" && go test -c -o "$TIERB_HOME/bin/perf.test" ./suites/performance)

echo "== cluster"
kind delete cluster --name "$KIND_CLUSTER_NAME" >/dev/null 2>&1 || true
# The config mounts a repository path relative to the working directory, so this runs from the repository root.
kind create cluster --name "$KIND_CLUSTER_NAME" --image "$KIND_NODE_IMAGE" --config "$ENV_DIR/kind-cluster.yaml"
# kind's add-ons must be running before the taint: its local-path provisioner does not tolerate CriticalAddonsOnly,
# and a pending add-on pod fails the suite's clean-cluster check before every spec.
kubectl -n local-path-storage rollout status deploy/local-path-provisioner --timeout=300s
kubectl -n kube-system rollout status deploy/coredns --timeout=300s
kubectl taint nodes "$KIND_CLUSTER_NAME-control-plane" CriticalAddonsOnly:NoSchedule

echo "== KWOK"
docker pull -q "$KWOK_IMAGE"
# --platform makes a single-platform archive; kind load docker-image fails on Docker's containerd image store.
docker save --platform linux/amd64 -o "$TIERB_HOME/kwok.tar" "$KWOK_IMAGE"
kind load image-archive "$TIERB_HOME/kwok.tar" --name "$KIND_CLUSTER_NAME"
./hack/install-kwok.sh
kubectl -n kube-system wait --for=condition=Available deploy --all --timeout=300s

echo "== controller"
kubectl -n kube-system create configmap tierb-instance-types \
  --from-file=instance_types.json="$ENV_DIR/instance_types.json" --dry-run=client -o yaml | kubectl apply -f -
IMAGE=$(cd "$TIERB_HOME/clone" && KO_DOCKER_REPO=kind.local KIND_CLUSTER_NAME=$KIND_CLUSTER_NAME \
  ko build sigs.k8s.io/karpenter/kwok)
kubectl apply -f "$TIERB_HOME/clone/kwok/charts/crds"
helm upgrade --install karpenter "$TIERB_HOME/clone/kwok/charts" --namespace kube-system --skip-crds \
  -f "$ENV_DIR/helm-values.yaml" --set controller.image.repository="${IMAGE%%:*}" --set controller.image.tag="${IMAGE##*:}"
kubectl -n kube-system rollout status deploy karpenter --timeout=300s

cat > "$TIERB_HOME/env.sh" <<EOF
export PATH=$TIERB_HOME/bin:\$PATH
export CSIM=$TIERB_HOME/bin/csim PERF_TEST=$TIERB_HOME/bin/perf.test
export IMAGE_REVISION=$(git -C "$TIERB_HOME/clone" rev-parse HEAD) IMAGE=$IMAGE KIND_NODE_IMAGE=$KIND_NODE_IMAGE
EOF
echo "== ready: $TIERB_HOME/env.sh"
cat "$TIERB_HOME/env.sh"
