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
resp=$(curl -s -X DELETE -H "X-Device-Token: $TOKEN" "$BASE/api/admin/db/purge?confirm=1&scope=logs&secondary=$TOKEN")
check "scope = logs" "logs" "$(printf '%s' "$resp" | jget 'd["scope"]')"
check "logs removeu 2 votos" "2" "$(printf '%s' "$resp" | jget 'd["removed"]["votes"]')"
check "logs removeu 0 plebiscitos" "0" "$(printf '%s' "$resp" | jget 'd["removed"]["polls"]')"
check "logs: historico mantem 2" "2" "$(printf '%s' "$resp" | jget 'd["history"]["count"]')"
check "logs: votos do primeiro = 0" "0" \
  "$(printf '%s' "$resp" | jget 'd["history"]["elections"][1]["total_votes"]')"

echo "== 6. Purga escopo 'all' (zera o banco) =="
resp=$(curl -s -X DELETE -H "X-Device-Token: $TOKEN" "$BASE/api/admin/db/purge?confirm=1&scope=all&secondary=$TOKEN")
check "scope = all" "all" "$(printf '%s' "$resp" | jget 'd["scope"]')"
check "all removeu 2 plebiscitos" "2" "$(printf '%s' "$resp" | jget 'd["removed"]["polls"]')"
check "all: historico vazio" "0" "$(printf '%s' "$resp" | jget 'd["history"]["count"]')"
check "all: sem plebiscito ativo" "None" "$(printf '%s' "$resp" | jget 'd["state"]["poll"]')"
check "all: fila de tokens zerada" "0" "$(printf '%s' "$resp" | jget 'd["state"]["voting_tokens"]')"
# Apos a purga total o proximo plebiscito reinicia a numeracao (id = 1).
resp=$(admin -X POST "$BASE/api/admin/poll" -H 'Content-Type: application/json' \
  -d '{"question":"Pos-purga","options":["Sim","Nao"]}')
check "novo id apos purge all = 1" "1" "$(printf '%s' "$resp" | jget 'd["poll"]["id"]')"

echo "== 7. Exclusao individual de plebiscito =="
code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$BASE/api/admin/polls/1")
check "delete poll sem token -> 401" "401" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "X-Device-Token: $TOKEN" \
  "$BASE/api/admin/polls/9999")
check "delete poll inexistente -> 404" "404" "$code"

# Excluir o plebiscito ATIVO remanescente da purga (#1) -> reseta o estado.
resp=$(curl -s -X DELETE -H "X-Device-Token: $TOKEN" "$BASE/api/admin/polls/1")
check "delete ativo: was_active = True" "True" "$(printf '%s' "$resp" | jget 'd["was_active"]')"
check "delete ativo: sem plebiscito ativo" "None" "$(printf '%s' "$resp" | jget 'd["state"]["poll"]')"
check "delete ativo: fila de tokens zerada" "0" \
  "$(printf '%s' "$resp" | jget 'd["state"]["voting_tokens"]')"
check "delete ativo: historico vazio" "0" "$(printf '%s' "$resp" | jget 'd["history"]["count"]')"

# Alfa recebe 1 voto; iniciar Beta encerra Alfa (preservando o historico).
resp=$(admin -X POST "$BASE/api/admin/poll" -H 'Content-Type: application/json' \
  -d '{"question":"Alfa","options":["Sim","Nao"]}')
ALFA_ID=$(printf '%s' "$resp" | jget 'd["poll"]["id"]')
curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"D-1"}' >/dev/null
curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_1"}' >/dev/null
resp=$(admin -X POST "$BASE/api/admin/poll" -H 'Content-Type: application/json' \
  -d '{"question":"Beta","options":["A","B"]}')
BETA_ID=$(printf '%s' "$resp" | jget 'd["poll"]["id"]')

# Excluir Alfa (encerrado, com 1 voto).
resp=$(curl -s -X DELETE -H "X-Device-Token: $TOKEN" "$BASE/api/admin/polls/$ALFA_ID")
check "delete encerrado: was_active = False" "False" "$(printf '%s' "$resp" | jget 'd["was_active"]')"
check "delete encerrado: removeu 1 voto" "1" "$(printf '%s' "$resp" | jget 'd["removed"]["votes"]')"
check "delete encerrado: historico mantem 1" "1" "$(printf '%s' "$resp" | jget 'd["history"]["count"]')"
check "delete encerrado: sobrou Beta ativo" "True" \
  "$(printf '%s' "$resp" | jget 'd["history"]["elections"][0]["active"]')"

# Excluir o plebiscito ATIVO (Beta).
resp=$(curl -s -X DELETE -H "X-Device-Token: $TOKEN" "$BASE/api/admin/polls/$BETA_ID")
check "delete ativo Beta: was_active = True" "True" "$(printf '%s' "$resp" | jget 'd["was_active"]')"
check "delete ativo Beta: historico vazio" "0" "$(printf '%s' "$resp" | jget 'd["history"]["count"]')"

echo "== 8. Limite de fila do check-in (Alerta Amarelo) =="
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/admin/settings/queue-threshold")
check "queue-threshold sem token -> 401" "401" "$code"
resp=$(admin "$BASE/api/admin/settings/queue-threshold")
check "limite padrao = 5" "5" "$(printf '%s' "$resp" | jget 'd["queue_warning_threshold"]')"
resp=$(admin -X POST "$BASE/api/admin/settings/queue-threshold" \
  -H 'Content-Type: application/json' -d '{"threshold":3}')
check "atualiza limite para 3" "3" "$(printf '%s' "$resp" | jget 'd["queue_warning_threshold"]')"
check "estado reflete o limite" "3" \
  "$(printf '%s' "$resp" | jget 'd["state"]["queue_warning_threshold"]')"
# A API de estado do check-in tambem expoe o limite (usada pela tela /checkin).
resp=$(curl -s "$BASE/api/booth/status")
check "check-in state expoe o limite" "3" \
  "$(printf '%s' "$resp" | jget 'd["queue_warning_threshold"]')"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Device-Token: $TOKEN" \
  -H 'Content-Type: application/json' -d '{"threshold":0}' \
  "$BASE/api/admin/settings/queue-threshold")
check "limite invalido (0) -> 400" "400" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Device-Token: $TOKEN" \
  -H 'Content-Type: application/json' -d '{"threshold":"abc"}' \
  "$BASE/api/admin/settings/queue-threshold")
check "limite nao numerico -> 400" "400" "$code"

echo "== 9. Backup do banco (download 1 clique) =="
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/admin/backup/download")
check "backup sem token -> 401" "401" "$code"
BK="/tmp/votacao_backup_test_$$.db"
code=$(curl -s -o "$BK" -w '%{http_code}' -H "X-Device-Token: $TOKEN" \
  "$BASE/api/admin/backup/download")
check "backup com token -> 200" "200" "$code"
grep -q 'SQLite format 3' "$BK"
check "arquivo e um SQLite valido" "0" "$?"
disp=$(curl -s -o /dev/null -D - -H "X-Device-Token: $TOKEN" \
  "$BASE/api/admin/backup/download" | tr -d '\r' | grep -ic 'attachment.*votacao_backup_')
check "resposta sugere nome votacao_backup_*.db" "1" "$disp"

echo "== 10. Restauracao do banco a partir de backup =="
# Cria um plebiscito com 1 check-in + 1 voto e tira um snapshot (.db).
admin -X POST "$BASE/api/admin/poll" -H 'Content-Type: application/json' \
  -d '{"question":"Pre-backup","options":["Sim","Nao"]}' >/dev/null
curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"BK-1"}' >/dev/null
curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_1"}' >/dev/null
BK2="/tmp/votacao_backup_snap_$$.db"
curl -s -o "$BK2" -H "X-Device-Token: $TOKEN" "$BASE/api/admin/backup/download"

# Restore sem arquivo / com arquivo invalido -> 400.
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Device-Token: $TOKEN" \
  "$BASE/api/admin/backup/restore")
check "restore sem arquivo -> 400" "400" "$code"
echo "nao sou um banco" > "/tmp/votacao_bad_$$.db"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Device-Token: $TOKEN" \
  -F "file=@/tmp/votacao_bad_$$.db" "$BASE/api/admin/backup/restore")
check "restore arquivo invalido -> 400" "400" "$code"

# Purga tudo para provar que o restauro traz os dados de volta.
curl -s -X DELETE -H "X-Device-Token: $TOKEN" \
  "$BASE/api/admin/db/purge?confirm=1&scope=all&secondary=$TOKEN" >/dev/null
resp=$(admin "$BASE/api/admin/history")
check "apos purga, historico vazio" "0" "$(printf '%s' "$resp" | jget 'd["count"]')"

# Restaura o snapshot -> o plebiscito e o voto voltam; fila volatil zerada.
resp=$(curl -s -X POST -H "X-Device-Token: $TOKEN" \
  -F "file=@$BK2" "$BASE/api/admin/backup/restore")
check "restore ok = True" "True" "$(printf '%s' "$resp" | jget 'd["ok"]')"
check "restore traz o historico (1)" "1" "$(printf '%s' "$resp" | jget 'd["history"]["count"]')"
check "restore preserva o voto" "1" \
  "$(printf '%s' "$resp" | jget 'd["history"]["elections"][0]["total_votes"]')"
check "restore zera a fila de tokens" "0" \
  "$(printf '%s' "$resp" | jget 'd["state"]["voting_tokens"]')"

echo "== 11. Purga exige verificacao secundaria =="
code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "X-Device-Token: $TOKEN" \
  "$BASE/api/admin/db/purge?confirm=1&scope=all")
check "purge sem secundaria -> 403" "403" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "X-Device-Token: $TOKEN" \
  "$BASE/api/admin/db/purge?confirm=1&scope=all&secondary=errado")
check "purge com secundaria errada -> 403" "403" "$code"
# Endpoint alias /api/admin/purge (POST) com a secundaria correta.
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Device-Token: $TOKEN" \
  -H 'Content-Type: application/json' -d '{"confirm":true,"scope":"all"}' \
  "$BASE/api/admin/purge?secondary=$TOKEN")
check "POST /api/admin/purge com secundaria -> 200" "200" "$code"

echo
echo "RESULTADO: $pass passaram, $fail falharam"
kill $SRV 2>/dev/null
if [ "$fail" -ne 0 ]; then echo "--- server log ---"; tail -30 "$LOG"; exit 1; fi
exit 0
