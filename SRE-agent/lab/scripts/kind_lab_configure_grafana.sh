#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"

grafana_port="${SRE_LAB_GRAFANA_PORT:-13000}"
webhook_url="${SRE_LAB_WEBHOOK_URL:-http://host.docker.internal:8090/alerts/grafana/webhook?token=grafana-lab-token}"
response_file="$(mktemp)"

kubectl port-forward --address 127.0.0.1 -n monitoring svc/monitoring-grafana "${grafana_port}:80" >"${response_file}.port-forward" 2>&1 &
forward_pid=$!
cleanup() {
  kill "${forward_pid}" 2>/dev/null || true
  wait "${forward_pid}" 2>/dev/null || true
  rm -f "${response_file}" "${response_file}.port-forward"
}
trap cleanup EXIT

for _attempt in {1..30}; do
  if curl -fsS "http://127.0.0.1:${grafana_port}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

grafana_password="$(kubectl get secret monitoring-grafana -n monitoring -o jsonpath='{.data.admin-password}' | base64 --decode)"
grafana_api="http://127.0.0.1:${grafana_port}"
curl_auth=(--user "admin:${grafana_password}")

contact_uid="$(curl -fsS "${curl_auth[@]}" "${grafana_api}/api/v1/provisioning/contact-points" | jq -r '.[] | select(.name == "sre-agent-webhook") | .uid' | head -n 1)"
contact_payload="$(jq -n --arg uid "${contact_uid}" --arg url "${webhook_url}" '{uid:$uid,name:"sre-agent-webhook",type:"webhook",settings:{url:$url},disableResolveMessage:false}')"
if [[ -n "${contact_uid}" ]]; then
  curl -fsS "${curl_auth[@]}" -X PUT -H 'Content-Type: application/json' --data-binary "${contact_payload}" "${grafana_api}/api/v1/provisioning/contact-points/${contact_uid}" >/dev/null
else
  contact_payload="$(jq -n --arg url "${webhook_url}" '{name:"sre-agent-webhook",type:"webhook",settings:{url:$url},disableResolveMessage:false}')"
  curl -fsS "${curl_auth[@]}" -X POST -H 'Content-Type: application/json' --data-binary "${contact_payload}" "${grafana_api}/api/v1/provisioning/contact-points" >/dev/null
fi

folder_status="$(curl -sS "${curl_auth[@]}" -o "${response_file}" -w '%{http_code}' "${grafana_api}/api/folders/sre-agent-lab")"
if [[ "${folder_status}" == "404" ]]; then
  curl -fsS "${curl_auth[@]}" -X POST -H 'Content-Type: application/json' --data-binary '{"uid":"sre-agent-lab","title":"SRE Agent Lab"}' "${grafana_api}/api/folders" >/dev/null
elif [[ "${folder_status}" != "200" ]]; then
  printf 'Grafana folder lookup failed with HTTP %s\n' "${folder_status}" >&2
  exit 1
fi

datasource_uid="$(curl -fsS "${curl_auth[@]}" "${grafana_api}/api/datasources" | jq -r '.[] | select(.type == "prometheus") | .uid' | head -n 1)"
if [[ -z "${datasource_uid}" ]]; then
  printf 'No Prometheus datasource was found in Grafana.\n' >&2
  exit 1
fi

provision_rule() {
  local uid="$1"
  local title="$2"
  local expression="$3"
  local threshold="$4"
  local summary="$5"
  local description="$6"
  local labels_json="$7"
  local rule_payload
  local rule_status

  rule_payload="$(jq -n \
    --arg datasource_uid "${datasource_uid}" \
    --arg uid "${uid}" \
    --arg title "${title}" \
    --arg expression "${expression}" \
    --argjson threshold "${threshold}" \
    --arg summary "${summary}" \
    --arg description "${description}" \
    --argjson labels "${labels_json}" '{
      uid:$uid,
      orgID:1,
      folderUID:"sre-agent-lab",
      ruleGroup:"sre-agent-kind",
      title:$title,
      condition:"B",
      data:[
        {
          refId:"A",
          queryType:"",
          relativeTimeRange:{from:120,to:0},
          datasourceUid:$datasource_uid,
          model:{
            datasource:{type:"prometheus",uid:$datasource_uid},
            editorMode:"code",
            expr:$expression,
            instant:true,
            intervalMs:1000,
            legendFormat:"__auto",
            maxDataPoints:43200,
            range:false,
            refId:"A"
          }
        },
        {
          refId:"B",
          queryType:"",
          relativeTimeRange:{from:0,to:0},
          datasourceUid:"__expr__",
          model:{
            conditions:[{evaluator:{params:[$threshold],type:"gt"},operator:{type:"and"},query:{params:["A"]},reducer:{params:[],type:"last"},type:"query"}],
            datasource:{type:"__expr__",uid:"__expr__"},
            expression:"A",
            intervalMs:1000,
            maxDataPoints:43200,
            refId:"B",
            type:"classic_conditions"
          }
        }
      ],
      noDataState:"OK",
      execErrState:"Error",
      for:"15s",
      annotations:{description:$description,summary:$summary},
      labels:$labels,
      isPaused:false
    }')"

  rule_status="$(curl -sS "${curl_auth[@]}" -o "${response_file}" -w '%{http_code}' -X PUT -H 'Content-Type: application/json' -H 'X-Rule-Group-Interval: 10s' --data-binary "${rule_payload}" "${grafana_api}/api/v1/provisioning/alert-rules/${uid}")"
  if [[ "${rule_status}" == "404" ]]; then
    rule_status="$(curl -sS "${curl_auth[@]}" -o "${response_file}" -w '%{http_code}' -X POST -H 'Content-Type: application/json' -H 'X-Rule-Group-Interval: 10s' --data-binary "${rule_payload}" "${grafana_api}/api/v1/provisioning/alert-rules")"
  fi
  if [[ "${rule_status}" != "200" && "${rule_status}" != "201" ]]; then
    printf 'Grafana alert-rule %s failed with HTTP %s: %s\n' "${uid}" "${rule_status}" "$(<"${response_file}")" >&2
    exit 1
  fi
}

common_labels='{"namespace":"sre-lab","severity":"critical"}'
provision_rule \
  "kind_worker_oomkilled" \
  "Kind Worker OOMKilled" \
  'max(kube_pod_container_status_last_terminated_reason{namespace="sre-lab",pod=~"oom-worker-.*",reason="OOMKilled"})' \
  0.5 \
  "The Kind worker was terminated because it exceeded its memory limit." \
  "Kubernetes reports a real OOMKilled container state and restart loop." \
  "$(jq -nc --argjson base "${common_labels}" '$base + {incident_id:"KIND-OOM",scenario:"crashloop",service:"oom-worker"}')"

provision_rule \
  "kind_demo_api_high_5xx" \
  "Kind Demo API Dependency 503" \
  '(max(demo_mode_info{mode="dependency503"}) == 1) * (sum(rate(demo_http_requests_total{path="/",status=~"5.."}[1m])) / clamp_min(sum(rate(demo_http_requests_total{path="/"}[1m])), 0.001))' \
  0.2 \
  "The Kind demo API cannot reach its payments dependency." \
  "A real payments outage is producing HTTP 503 responses in the demo API." \
  "$(jq -nc --argjson base "${common_labels}" '$base + {incident_id:"KIND-DEP-503",scenario:"service503",service:"demo-api",dependency:"payments"}')"

provision_rule \
  "kind_demo_api_deployment_regression" \
  "Kind Demo API Deployment Regression" \
  'max(demo_incident_signal{scenario="deployment_regression",incident_id="INC-007"})' \
  0.5 \
  "A recent Kind demo API release is correlated with new HTTP 503 responses." \
  "The controlled bad-release deployment marker and application errors are active." \
  "$(jq -nc --argjson base "${common_labels}" '$base + {incident_id:"KIND-DEPLOY-REGRESSION",scenario:"deployment_regression",service:"demo-api",config_management:"gitops",current_version:"bad-release",previous_version:"known-good",gitops_manifest_path:"clusters/sre-lab/demo-api.json"}')"

provision_rule \
  "kind_demo_api_dns_failure" \
  "Kind Demo API DNS Failure" \
  'max(demo_incident_signal{scenario="dnsfailure",incident_id="INC-003"})' \
  0.5 \
  "The Kind demo API is reporting repeated dependency name-resolution failures." \
  "The workload remains running while concrete NXDOMAIN errors are emitted." \
  "$(jq -nc --argjson base "${common_labels}" '$base + {incident_id:"KIND-DNS",scenario:"dnsfailure",service:"demo-api"}')"

provision_rule \
  "kind_demo_api_ambiguous" \
  "Kind Demo API Ambiguous Latency" \
  'max(demo_incident_signal{scenario="ambiguous",incident_id="INC-AMB"})' \
  0.5 \
  "The Kind demo API has moderate latency without a confirmed root cause." \
  "The expected agent outcome is a grounded escalation without execution." \
  "$(jq -nc --argjson base "${common_labels}" '$base + {incident_id:"KIND-AMBIGUOUS",scenario:"ambiguous",service:"demo-api",severity:"warning"}')"

# Updating individual rules does not change an existing group's interval. Set it
# explicitly so every fresh fault reaches Pending and Alerting within one minute.
rule_group_url="${grafana_api}/api/v1/provisioning/folder/sre-agent-lab/rule-groups/sre-agent-kind"
rule_group_payload="$(curl -fsS "${curl_auth[@]}" "${rule_group_url}" | jq '.interval = 10')"
curl -fsS "${curl_auth[@]}" -X PUT -H 'Content-Type: application/json' --data-binary "${rule_group_payload}" "${rule_group_url}" >/dev/null

policy_payload='{"receiver":"sre-agent-webhook","group_by":["grafana_folder","alertname"],"group_wait":"0s","group_interval":"30s","repeat_interval":"1m"}'
curl -fsS "${curl_auth[@]}" -X PUT -H 'Content-Type: application/json' --data-binary "${policy_payload}" "${grafana_api}/api/v1/provisioning/policies" >/dev/null

printf 'Grafana contact point, notification policy, and five Kind scenario alerts are configured.\n'
