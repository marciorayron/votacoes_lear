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
# PIN de encerramento do plebiscito ativo (exigido por /api/admin/close).
cpin()  { admin "$BASE/api/admin/state" | jget 'd["closure_pin"] or ""'; }

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

echo "== 12. Anonimato do esquema de votos (sem vinculo votante->voto) =="
resp=$(python3 - "$DB" <<'PY'
import json, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
vote_cols = [r[1] for r in db.execute("PRAGMA table_info(votes)")]
poll_cols = [r[1] for r in db.execute("PRAGMA table_info(polls)")]
print(json.dumps({"vote_cols": vote_cols, "polls_integrity": "integrity_hash" in poll_cols}))
PY
)
check "votes = (id, poll_id, option_label)" "['id', 'poll_id', 'option_label']" \
  "$(printf '%s' "$resp" | jget 'd["vote_cols"]')"
check "polls expoe integrity_hash" "True" "$(printf '%s' "$resp" | jget 'd["polls_integrity"]')"

echo "== 13. Integridade SHA-256 (selo no fechamento) =="
resp=$(admin -X POST "$BASE/api/admin/poll" -H 'Content-Type: application/json' \
  -d '{"question":"Auditoria","options":["Sim","Nao"]}')
AUD_ID=$(printf '%s' "$resp" | jget 'd["poll"]["id"]')
curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"V-1"}' >/dev/null
curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"V-2"}' >/dev/null
curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_1"}' >/dev/null
curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_2"}' >/dev/null

# Antes do fechamento o digest ainda NAO esta selado.
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/admin/verify")
check "verify sem token -> 401" "401" "$code"
resp=$(admin "$BASE/api/admin/verify")
check "verify ativo: sealed = False" "False" "$(printf '%s' "$resp" | jget 'd["sealed"]')"
check "verify ativo: reason = not_sealed" "not_sealed" "$(printf '%s' "$resp" | jget 'd["reason"]')"
check "verify ativo: valid = False" "False" "$(printf '%s' "$resp" | jget 'd["valid"]')"
check "algoritmo declarado = SHA-256" "SHA-256" "$(printf '%s' "$resp" | jget 'd["algorithm"]')"

# Encerrar sela o digest SHA-256 do estado final (polls.integrity_hash).
CPIN=$(cpin)
resp=$(admin -X POST "$BASE/api/admin/close" -H 'Content-Type: application/json' \
  -d "{\"closure_pin\":\"$CPIN\"}")
check "close com o PIN do plebiscito -> closed" "closed" \
  "$(printf '%s' "$resp" | jget 'd["poll"]["status"]')"
resp=$(admin "$BASE/api/admin/polls/$AUD_ID/verify")
check "verify encerrado: sealed = True" "True" "$(printf '%s' "$resp" | jget 'd["sealed"]')"
check "verify encerrado: valid = True" "True" "$(printf '%s' "$resp" | jget 'd["valid"]')"
check "verify encerrado: reason = ok" "ok" "$(printf '%s' "$resp" | jget 'd["reason"]')"
HASH1=$(printf '%s' "$resp" | jget 'd["expected"]')
printf '%s' "$HASH1" | grep -Eq '^[0-9a-f]{64}$'
check "digest e SHA-256 hex (64 chars)" "0" "$?"

# Relatorio (Ata) traz o bloco de integridade selado/valido e as regras.
resp=$(admin "$BASE/api/admin/report")
check "report: integridade selada" "True" "$(printf '%s' "$resp" | jget 'd["integrity"]["sealed"]')"
check "report: integridade valida" "True" "$(printf '%s' "$resp" | jget 'd["integrity"]["valid"]')"
check "report: digest = selo gravado" "1" \
  "$(printf '%s' "$resp" | jget 'int(d["integrity"]["hash"] == d["integrity"]["computed"])')"
check "report: votos hasheados = 2" "2" "$(printf '%s' "$resp" | jget 'd["integrity"]["votes_hashed"]')"
check "report: polit. token unico = True" "True" "$(printf '%s' "$resp" | jget 'd["rules"]["single_token_policy"]')"
check "report: cooldown do check-in" "$(python3 -c "import os;print(float(os.environ.get('CHECKIN_COOLDOWN_SECONDS','2')))")" \
  "$(printf '%s' "$resp" | jget 'd["rules"]["checkin_cooldown"]')"
check "report: atraso de resultados" "$(python3 -c "import os;print(int(os.environ.get('RESULTS_DELAY_SECONDS','5')))")" \
  "$(printf '%s' "$resp" | jget 'd["rules"]["results_delay"]')"
check "report: total de votantes = 2" "2" "$(printf '%s' "$resp" | jget 'd["total_voters_registered"]')"

# Historico: cada plebiscito carrega o proprio bloco de integridade.
resp=$(admin "$BASE/api/admin/history")
check "historico: selo valido" "True" \
  "$(printf '%s' "$resp" | jget 'd["elections"][0]["integrity"]["valid"]')"
check "historico: hash == digest recalculado" "1" \
  "$(printf '%s' "$resp" | jget 'int(d["elections"][0]["integrity"]["hash"] == d["elections"][0]["integrity"]["computed"])')"

echo "== 14. Ata de fechamento: assinaturas dinamicas + integridade no HTML =="
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/admin/relatorio")
check "Ata sem token -> 401" "401" "$code"
ATA="$BASE/admin/relatorio?token=$TOKEN"
code=$(curl -s -o /dev/null -w '%{http_code}' "$ATA")
check "Ata com token -> 200" "200" "$code"

# Sem parametros: usa as linhas padrao (assinatura manual).
html=$(curl -s "$ATA")
printf '%s' "$html" | grep -q 'Presidente da Mesa'
check "Ata usa assinaturas padrao" "0" "$?"
printf '%s' "$html" | grep -q "$HASH1"
check "Ata exibe o digest SHA-256" "0" "$?"

# Linhas dinamicas (listas paralelas sig_nome/sig_cargo/sig_doc).
html=$(curl -s "$ATA&sig_nome=Ana%20Souza&sig_cargo=Presidenta&sig_doc=MAT-001&sig_nome=Bruno%20Lima&sig_cargo=Secretario&sig_doc=MAT-002")
printf '%s' "$html" | grep -q 'Ana Souza';   check "Ata mostra 1o signatario" "0" "$?"
printf '%s' "$html" | grep -q 'Presidenta';  check "Ata mostra o cargo do 1o" "0" "$?"
printf '%s' "$html" | grep -q 'MAT-001';     check "Ata mostra a matricula do 1o" "0" "$?"
printf '%s' "$html" | grep -q 'Bruno Lima';  check "Ata mostra o 2o signatario" "0" "$?"
printf '%s' "$html" | grep -q 'MAT-002';     check "Ata mostra a matricula do 2o" "0" "$?"
printf '%s' "$html" | grep -q 'Presidente da Mesa'
check "Ata NAO usa o padrao com assinaturas informadas" "1" "$?"

# Linhas sem nome sao ignoradas (volta as linhas padrao).
html=$(curl -s "$ATA&sig_nome=&sig_cargo=Presidente")
printf '%s' "$html" | grep -q 'Presidente da Mesa'
check "Ata ignora linhas sem nome" "0" "$?"

# Relatorio Geral de Historico (A4) tambem abre com token.
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/admin/relatorio/historico?token=$TOKEN")
check "Relatorio de historico -> 200" "200" "$code"

echo "== 15. Deteccao de adulteracao externa do banco =="
# Altera um voto direto no SQLite (simula edicao por terceiros/sqlite3).
python3 - "$DB" "$AUD_ID" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
# O ultimo voto de "Auditoria" e 'Nao' (opt_2): troca para 'Sim'.
cur = db.execute(
    "UPDATE votes SET option_label = 'Sim' WHERE id = "
    "(SELECT MAX(id) FROM votes WHERE poll_id = ?)",
    (int(sys.argv[2]),),
)
print("linhas_alteradas=%d" % cur.rowcount)
db.commit()
db.close()
PY
resp=$(admin "$BASE/api/admin/polls/$AUD_ID/verify")
check "apos adulteracao: valid = False" "False" "$(printf '%s' "$resp" | jget 'd["valid"]')"
check "apos adulteracao: reason = checksum_mismatch" "checksum_mismatch" \
  "$(printf '%s' "$resp" | jget 'd["reason"]')"
check "apos adulteracao: selo preservado" "$HASH1" "$(printf '%s' "$resp" | jget 'd["expected"]')"
check "apos adulteracao: digest diverge do selo" "1" \
  "$(printf '%s' "$resp" | jget 'int(d["expected"] != d["actual"])')"

resp=$(admin "$BASE/api/admin/history")
check "historico: conta 1 plebiscito divergente" "1" \
  "$(printf '%s' "$resp" | jget 'int(d["elections"][0]["integrity"]["valid"] == False)')"

echo "== 16. PIN de encerramento e conciliacao de fechamento =="
resp=$(admin -X POST "$BASE/api/admin/poll" -H 'Content-Type: application/json' \
  -d '{"question":"Plebiscito com PIN","options":["Sim","Nao"]}')
PIN_ID=$(printf '%s' "$resp" | jget 'd["poll"]["id"]')
CPIN=$(printf '%s' "$resp" | jget 'd["poll"]["closure_pin"] or ""')
printf '%s' "$CPIN" | grep -Eq '^[0-9]{4,6}$'
check "plebiscito nasce com PIN numerico (4 a 6 digitos)" "0" "$?"
check "state expoe o mesmo closure_pin" "$CPIN" "$(cpin)"

# 2 credenciados, 1 voto -> 1 token pendente (votante ainda na fila).
curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"PIN-1"}' >/dev/null
curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_1"}' >/dev/null
curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"PIN-2"}' >/dev/null

resp=$(admin "$BASE/api/admin/state")
check "conciliacao: check-ins = 2" "2" \
  "$(printf '%s' "$resp" | jget 'd["reconciliation"]["checkins_total"]')"
check "conciliacao: votos = 1" "1" \
  "$(printf '%s' "$resp" | jget 'd["reconciliation"]["votes_cast"]')"
check "conciliacao: tokens ativos = 1" "1" \
  "$(printf '%s' "$resp" | jget 'd["reconciliation"]["active_tokens"]')"
check "conciliacao: esperado = votos + tokens" "2" \
  "$(printf '%s' "$resp" | jget 'd["reconciliation"]["expected_checkins"]')"
check "conciliacao: balancada (diferenca zero)" "True" \
  "$(printf '%s' "$resp" | jget 'd["reconciliation"]["balanced"]')"

resp=$(admin "$BASE/api/admin/report")
check "Ata: conciliacao balanceada" "True" \
  "$(printf '%s' "$resp" | jget 'd["reconciliation"]["balanced"]')"
check "Ata: isolamento de rede declarado" "$(python3 -c "import os;print(os.environ.get('NETWORK_MODE','Rede Local / Offline'))")" \
  "$(printf '%s' "$resp" | jget 'd["network_isolation"]')"

echo "== 17. Inspetor do banco (somente leitura) =="
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/admin/db/inspect?table=votes")
check "inspect sem token -> 401" "401" "$code"

resp=$(admin "$BASE/api/admin/db/inspect?table=votes")
check "inspect votes: read_only = True" "True" \
  "$(printf '%s' "$resp" | jget 'd["read_only"]')"
check "inspect votes: marcada como anonima" "True" \
  "$(printf '%s' "$resp" | jget 'd["anonymous"]')"
check "inspect votes: esquema (id, poll_id, option_label)" "['id', 'poll_id', 'option_label']" \
  "$(printf '%s' "$resp" | jget 'd["columns"]')"
# O total exibido bate com a contagem real no SQLite (sem vazar vinculo).
EXPECTED_VOTES=$(python3 - "$DB" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
print(db.execute("SELECT COUNT(*) FROM votes").fetchone()[0])
PY
)
check "inspect votes: count = COUNT(*) real" "$EXPECTED_VOTES" \
  "$(printf '%s' "$resp" | jget 'd["count"]')"
check "inspect votes: sem coluna de identificacao" "0" \
  "$(printf '%s' "$resp" | jget 'int(any(c in d["columns"] for c in ("badge_id","voter_id","ip","created_at")))')"

resp=$(admin "$BASE/api/admin/db/inspect?table=xxx")
check "inspect: tabela fora da whitelist cai em polls" "polls" \
  "$(printf '%s' "$resp" | jget 'd["table"]')"
check "inspect: whitelist anunciada" "['polls', 'voters', 'votes', 'booths', 'settings']" \
  "$(printf '%s' "$resp" | jget 'd["tables"]')"
check "inspect polls: expoe a coluna closure_pin" "1" \
  "$(printf '%s' "$resp" | jget 'int("closure_pin" in d["columns"])')"

echo "== 18. Log de auditoria =="
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/admin/audit")
check "audit sem token -> 401" "401" "$code"

# Fechamentos negados (sem PIN e com PIN errado) NAO encerram o plebiscito.
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Device-Token: $TOKEN" \
  "$BASE/api/admin/close")
check "close sem PIN -> 403" "403" "$code"
resp=$(curl -s -X POST -H "X-Device-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"closure_pin":"000000"}' "$BASE/api/admin/close")
check "close com PIN errado -> invalid_closure_pin" "invalid_closure_pin" \
  "$(printf '%s' "$resp" | jget 'd["error"]')"
resp=$(admin "$BASE/api/admin/state")
check "plebiscito segue aberto apos negativas" "open" \
  "$(printf '%s' "$resp" | jget 'd["poll"]["status"]')"

# Pareamento ativo + cabine nova sem PIN -> 403 (evento auditado).
admin -X POST "$BASE/api/admin/pairing" -H 'Content-Type: application/json' \
  -d '{"generate":true}' >/dev/null
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/booth/register" \
  -H 'Content-Type: application/json' -d '{"booth_id":"audit-cab"}')
check "cabine nova sem PIN de pareamento -> 403" "403" "$code"
admin -X POST "$BASE/api/admin/pairing" -H 'Content-Type: application/json' \
  -d '{"clear":true}' >/dev/null

# Fechamento legitimo pelo header X-Closure-Pin.
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Device-Token: $TOKEN" \
  -H "X-Closure-Pin: $CPIN" "$BASE/api/admin/close")
check "close com X-Closure-Pin correto -> 200" "200" "$code"
resp=$(admin "$BASE/api/admin/state")
check "plebiscito encerrado pelo PIN" "closed" \
  "$(printf '%s' "$resp" | jget 'd["poll"]["status"]')"

resp=$(admin "$BASE/api/admin/audit?limit=200")
check "audit: registro de fechamento negado" "1" \
  "$(printf '%s' "$resp" | jget 'int(any(e["event"]=="poll_close_denied" for e in d["entries"]))')"
check "audit: registro de fechamento aceito" "1" \
  "$(printf '%s' "$resp" | jget 'int(any(e["event"]=="poll_close" for e in d["entries"]))')"
check "audit: fechamento aponta o plebiscito" "$PIN_ID" \
  "$(printf '%s' "$resp" | jget '[e["poll_id"] for e in d["entries"] if e["event"]=="poll_close"][0]')"
check "audit: registro de pareamento negado" "1" \
  "$(printf '%s' "$resp" | jget 'int(any(e["event"]=="booth_pair_denied" for e in d["entries"]))')"
check "audit: evento mais recente primeiro (id decrescente)" "1" \
  "$(printf '%s' "$resp" | jget 'int(all(d["entries"][i]["id"] >= d["entries"][i+1]["id"] for i in range(len(d["entries"])-1)))')"
check "audit: todo evento traz IP do ator" "1" \
  "$(printf '%s' "$resp" | jget 'int(all(bool(e["actor_ip"]) for e in d["entries"]))')"

resp=$(admin "$BASE/api/admin/audit?limit=1")
check "audit: limit=1 respeitado" "1" "$(printf '%s' "$resp" | jget 'd["count"]')"
check "audit: limit=1 e o poll_close" "poll_close" \
  "$(printf '%s' "$resp" | jget 'd["entries"][0]["event"]')"

echo
echo "RESULTADO: $pass passaram, $fail falharam"
kill $SRV 2>/dev/null
if [ "$fail" -ne 0 ]; then echo "--- server log ---"; tail -30 "$LOG"; exit 1; fi
exit 0
