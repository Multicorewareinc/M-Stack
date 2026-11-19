#!/usr/bin/env bash
# End-to-end: provision an org + org_admin user, log in, mint an API key, and spend it
# against the live gateway. Targets the api/tests/manual-test/docker-compose.full.yml stack.
# Requires: bash, curl, jq.  Run from Git Bash on Windows, or any POSIX shell.
set -euo pipefail

ACP=${ACP:-http://localhost:9200}        # admin-control-plane
ORG=${ORG:-http://localhost:9220}        # organization-control-plane
GW=${GW:-http://localhost:8080}          # model-gateway
ADMIN_KEY=${ADMIN_KEY:-test-admin-key}   # Admin CP super-admin/UI bearer
SERVICE_KEY=${SERVICE_KEY:-test-service-key}  # inter-service bearer (Org CP /v1/*)

TS=$(date +%s)
EMAIL="apiowner-${TS}@example.com"
PASS_INIT='OwnerSecret123!'
PASS_NEW='OwnerRotated456!'

echo "== 1. Free plan id =="
PLAN_ID=$(curl -fsS "$ACP/v1/plans" -H "Authorization: Bearer $ADMIN_KEY" \
  | jq -r '.[] | select(.name=="Free") | .id')
echo "plan_id=$PLAN_ID"

echo "== 2. Create org =="
ORG_ID=$(curl -fsS -X POST "$ACP/v1/organizations" \
  -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d "{\"name\":\"ApiKeyOrg-${TS}\",\"plan_id\":\"$PLAN_ID\"}" | jq -r '.id')
echo "org_id=$ORG_ID"

echo "== 3. org_admin role id (auto-seeded with the org) =="
ROLE_ID=$(curl -fsS "$ORG/v1/roles" \
  -H "Authorization: Bearer $SERVICE_KEY" -H "X-Organization-Id: $ORG_ID" \
  | jq -r '.[] | select(.name=="org_admin") | .id')
echo "role_id=$ROLE_ID"

echo "== 4. Create owner user (with password) =="
OWNER_ID=$(curl -fsS -X POST "$ORG/v1/users" \
  -H "Authorization: Bearer $SERVICE_KEY" -H "X-Organization-Id: $ORG_ID" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"apiowner-${TS}\",\"email\":\"$EMAIL\",\"password\":\"$PASS_INIT\"}" \
  | jq -r '.id')
echo "owner_id=$OWNER_ID"

echo "== 5. Assign org_admin role (grants apikey.create) =="
curl -fsS -X PUT "$ORG/v1/users/$OWNER_ID/roles" \
  -H "Authorization: Bearer $SERVICE_KEY" -H "X-Organization-Id: $ORG_ID" \
  -H 'Content-Type: application/json' \
  -d "{\"role_ids\":[\"$ROLE_ID\"]}" > /dev/null

echo "== 6. Login (initial password, forces a change) =="
TOKEN=$(curl -fsS -X POST "$ORG/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS_INIT\"}" | jq -r '.access_token')

echo "== 7. Rotate the forced initial password =="
curl -fsS -X POST "$ORG/api/auth/password" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"current_password\":\"$PASS_INIT\",\"new_password\":\"$PASS_NEW\"}" > /dev/null

echo "== 8. Login again (new password) -> access token =="
TOKEN=$(curl -fsS -X POST "$ORG/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS_NEW\"}" | jq -r '.access_token')
echo "access_token=${TOKEN:0:24}..."

echo "== 9. Create API key (raw_key returned ONCE) =="
RAW_KEY=$(curl -fsS -X POST "$ORG/api/api-keys" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"curl-smoke-key","expires_at":"2030-01-01T00:00:00Z"}' | jq -r '.raw_key')
echo "raw_key=$RAW_KEY"

echo "== 10. Test the key against the gateway =="
curl -fsS -X POST "$GW/v1/chat/completions" \
  -H "Authorization: Bearer $RAW_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"llama-3-8b","messages":[{"role":"user","content":"hi from a minted key"}]}' \
  | jq '{object, model}'

echo "== done =="
