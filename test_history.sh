#!/usr/bin/env bash
# Teste das APIs de Historico e Purga do banco (roda em porta isolada).
set -u
cd "$(dirname "$0")"

PORT=5056
TOKEN="testtoken123"
BASE="http://127.0.0.1:${PORT}"
DB="/tmp/votacao_hist_$$.db"
LOG="/tmp/votacao_hist_$$.log"

rm -f "$DB" "$DB-wal" "$DB-shm"

export DEVICE_TOKEN="$TOKEN"
export VOTING_DB="$DB"
export PORT="$PORT"

python3 app.py > "$LOG" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT

for i in $(seq 1 40); do
  if curl -s "$BASE/api/health" >/dev/null 2>&1; then break; fi
  sleep 0.25
done

pass=0; fail=0
check() { # descricao, esperado, obtido
  if [ "$2" = "$3" ]; then echo "  PASS  $1"; pass=$((pass+1));
  else echo "  FAIL  $1 (esperado=$2 obtido=$3)"; fail=$((fail+1)); fi
}
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
admin() { curl -s -H "X-Device-Token: $TOKEN" "$@"; }

echo "== 1. Historico exige token =="
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/admin/history")
check "history sem token -> 401" "401" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Device-Token: $TOKEN" "$BASE/api/admin/history")
check "history com token -> 200" "200" "$code"

echo "== 2. Preparar dois plebiscitos =="
admin -X POST "$BASE/api/admin/poll" -H 'Content-Type: application/json' \
  -d '{"question":"Primeiro plebiscito","options":["Sim","Nao"]}' >/dev/null
curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"H-1"}' >/dev/null
curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"H-2"}' >/dev/null
curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_1"}' >/dev/null
curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_2"}' >/dev/null
# Iniciar um novo plebiscito encerra o anterior (preserva votantes no historico).
admin -X POST "$BASE/api/admin/poll" -H 'Content-Type: application/json' \
  -d '{"question":"Segundo plebiscito","options":["A","B"]}' >/dev/null

echo "== 3. Conteudo do historico =="
resp=$(admin "$BASE/api/admin/history")
check "count = 2" "2" "$(printf '%s' "$resp" | jget 'd["count"]')"
check "mais recente primeiro" "Segundo plebiscito" \
  "$(printf '%s' "$resp" | jget 'd["elections"][0]["question"]')"
check "segundo e o ativo" "True" "$(printf '%s' "$resp" | jget 'd["elections"][0]["active"]')"
check "primeiro nao e ativo" "False" "$(printf '%s' "$resp" | jget 'd["elections"][1]["active"]')"
check "primeiro fechado" "closed" "$(printf '%s' "$resp" | jget 'd["elections"][1]["status"]')"
check "votos do primeiro = 2" "2" "$(printf '%s' "$resp" | jget 'd["elections"][1]["total_votes"]')"
check "votantes do primeiro = 2" "2" "$(printf '%s' "$resp" | jget 'd["elections"][1]["voters"]')"
check "percentual Sim = 50.0" "50.0" \
  "$(printf '%s' "$resp" | jget '[o["percent"] for o in d["elections"][1]["options"] if o["label"]=="Sim"][0]')"

echo "== 4. Purga exige confirmacao =="
code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "X-Device-Token: $TOKEN" "$BASE/api/admin/db/purge")
check "purge sem confirm -> 400" "400" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "X-Device-Token: $TOKEN" \
  "$BASE/api/admin/db/purge?confirm=1&scope=xxx")
check "purge scope invalido -> 400" "400" "$code"

echo "== 5. Purga escopo 'logs' (preserva plebiscitos) =="
resp=$(curl -s -X DELETE -H "X-Device-Token: $TOKEN" "$BASE/api/admin/db/purge?confirm=1&scope=logs")
check "scope = logs" "logs" "$(printf '%s' "$resp" | jget 'd["scope"]')"
check "logs removeu 2 votos" "2" "$(printf '%s' "$resp" | jget 'd["removed"]["votes"]')"
check "logs removeu 0 plebiscitos" "0" "$(printf '%s' "$resp" | jget 'd["removed"]["polls"]')"
check "logs: historico mantem 2" "2" "$(printf '%s' "$resp" | jget 'd["history"]["count"]')"
check "logs: votos do primeiro = 0" "0" \
  "$(printf '%s' "$resp" | jget 'd["history"]["elections"][1]["total_votes"]')"

echo "== 6. Purga escopo 'all' (zera o banco) =="
resp=$(curl -s -X DELETE -H "X-Device-Token: $TOKEN" "$BASE/api/admin/db/purge?confirm=1&scope=all")
check "scope = all" "all" "$(printf '%s' "$resp" | jget 'd["scope"]')"
check "all removeu 2 plebiscitos" "2" "$(printf '%s' "$resp" | jget 'd["removed"]["polls"]')"
check "all: historico vazio" "0" "$(printf '%s' "$resp" | jget 'd["history"]["count"]')"
check "all: sem plebiscito ativo" "None" "$(printf '%s' "$resp" | jget 'd["state"]["poll"]')"
check "all: fila de tokens zerada" "0" "$(printf '%s' "$resp" | jget 'd["state"]["voting_tokens"]')"
# Apos a purga total o proximo plebiscito reinicia a numeracao (id = 1).
resp=$(admin -X POST "$BASE/api/admin/poll" -H 'Content-Type: application/json' \
  -d '{"question":"Pos-purga","options":["Sim","Nao"]}')
check "novo id apos purge all = 1" "1" "$(printf '%s' "$resp" | jget 'd["poll"]["id"]')"

echo
echo "RESULTADO: $pass passaram, $fail falharam"
kill $SRV 2>/dev/null
if [ "$fail" -ne 0 ]; then echo "--- server log ---"; tail -30 "$LOG"; exit 1; fi
exit 0
