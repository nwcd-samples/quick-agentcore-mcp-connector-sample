#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: deploy.sh <scenario1|scenario2|scenario3|scenario4> <stack-name> <artifact-bucket> <parameters.json> [region] [profile]

The artifact bucket must already exist in the deployment region. The parameter
file uses the JSON format accepted by `aws cloudformation deploy`.
EOF
  exit 2
}

[[ $# -ge 4 && $# -le 6 ]] || usage
SCENARIO=$1
STACK_NAME=$2
ARTIFACT_BUCKET=$3
PARAMETERS_FILE=$4
REGION=${5:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}
PROFILE=${6:-}
[[ -n "$REGION" ]] || {
  echo "Region is required: pass argument 5 or set AWS_REGION/AWS_DEFAULT_REGION" >&2
  exit 2
}

case "$SCENARIO" in
  scenario1) TEMPLATE=scenario1-native-lambda.yaml ;;
  scenario2) TEMPLATE=scenario2-mixed-auth.yaml ;;
  scenario3) TEMPLATE=scenario3-entra-obo.yaml ;;
  scenario4) TEMPLATE=scenario4-private-link.yaml ;;
  *) usage ;;
esac

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TEMPLATE_FILE="$ROOT/scenarios/$TEMPLATE"
[[ -f "$PARAMETERS_FILE" ]] || { echo "Parameter file not found: $PARAMETERS_FILE" >&2; exit 2; }
PARAMETERS_FILE=$(realpath "$PARAMETERS_FILE")
PACKAGED_TEMPLATE=$(mktemp "${TMPDIR:-/tmp}/quick-agentcore-${SCENARIO}.XXXXXX.yaml")
trap 'rm -f "$PACKAGED_TEMPLATE"' EXIT

AWS_ARGS=(--region "$REGION")
if [[ -n "$PROFILE" ]]; then
  AWS_ARGS+=(--profile "$PROFILE")
fi

ACCOUNT_ID=$(aws sts get-caller-identity "${AWS_ARGS[@]}" --query Account --output text)
CALLER_ARN=$(aws sts get-caller-identity "${AWS_ARGS[@]}" --query Arn --output text)
PARTITION=$(printf '%s' "$CALLER_ARN" | cut -d: -f2)
[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ && -n "$PARTITION" ]] || {
  echo "Could not determine caller account/partition" >&2
  exit 2
}
python3 "$ROOT/validate_parameters.py" \
  --template "$TEMPLATE_FILE" \
  --parameters "$PARAMETERS_FILE" \
  --region "$REGION" \
  --account-id "$ACCOUNT_ID" \
  --partition "$PARTITION"

aws cloudformation package \
  "${AWS_ARGS[@]}" \
  --template-file "$TEMPLATE_FILE" \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --s3-prefix "quick-agentcore-cloudformation/$SCENARIO" \
  --output-template-file "$PACKAGED_TEMPLATE"

aws cloudformation deploy \
  "${AWS_ARGS[@]}" \
  --template-file "$PACKAGED_TEMPLATE" \
  --stack-name "$STACK_NAME" \
  --parameter-overrides "file://$PARAMETERS_FILE" \
  --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --no-fail-on-empty-changeset

aws cloudformation describe-stacks \
  "${AWS_ARGS[@]}" \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[].{Key:OutputKey,Value:OutputValue}' \
  --output table
