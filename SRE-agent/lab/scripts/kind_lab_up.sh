#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"

kind get clusters | rg -Fqx -- "${cluster_name}" || kind create cluster --name "${cluster_name}" --config lab/k8s/kind-config.yaml
docker build -t self-healing-sre:lab .
kind load docker-image self-healing-sre:lab --name "${cluster_name}"
kubectl apply -f lab/k8s/workloads.yaml
# Reload the rebuilt local tag even when the Deployment manifest is unchanged.
kubectl rollout restart deployment/demo-api deployment/payments -n sre-lab

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack --namespace monitoring --create-namespace --wait
helm upgrade --install loki grafana/loki \
  --namespace monitoring \
  --set deploymentMode=SingleBinary \
  --set loki.auth_enabled=false \
  --set loki.commonConfig.replication_factor=1 \
  --set loki.storage.type=filesystem \
  --set loki.useTestSchema=true \
  --set singleBinary.replicas=1 \
  --set backend.replicas=0 \
  --set read.replicas=0 \
  --set write.replicas=0 \
  --set chunksCache.enabled=false \
  --set resultsCache.enabled=false \
  --wait

kubectl apply -f lab/k8s/observability.yaml

kubectl rollout status deployment/demo-api -n sre-lab --timeout=120s
kubectl rollout status deployment/payments -n sre-lab --timeout=120s
./lab/scripts/kind_lab_configure_grafana.sh
