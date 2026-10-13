#!/usr/bin/env bash
# Teste end-to-end do Sistema de Votacao Local (roda em porta isolada).
set -u
cd "$(dirname "$0")"

PORT=5055
TOKEN="testtoken123"
BASE="http://127.0.0.1:${PORT}"
DB="/tmp/votacao_e2e_$$.db"
LOG="/tmp/votacao_e2e_$$.log"

rm -f "$DB" "$DB-wal" "$DB-shm"

export DEVICE_TOKEN="$TOKEN"
export VOTING_DB="$DB"
export PORT="$PORT"
export RESULTS_DELAY_SECONDS=2
export CHECKIN_COOLDOWN_SECONDS=2

python3 app.py > "$LOG" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT

# aguarda o servidor subir
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

echo "== 1. Admin exige token =="
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/admin/state")
check "sem token -> 401" "401" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Device-Token: wrong" "$BASE/api/admin/state")
check "token errado -> 401" "401" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Device-Token: $TOKEN" "$BASE/api/admin/state")
check "token correto -> 200" "200" "$code"

echo "== 2. Criar plebiscito dinamico =="
resp=$(curl -s -X POST "$BASE/api/admin/poll" -H "X-Device-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"question":"Aprova o novo turno?","options":["Sim","Nao"]}')
echo "$resp" | grep -q '"question":"Aprova o novo turno?"'
check "poll criada com pergunta" "0" "$?"

echo "== 3. Check-in =="
resp=$(curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"BADGE-001"}')
echo "$resp" | grep -q '"status":"authorized"'; check "badge novo autorizado" "0" "$?"
echo "$resp" | grep -q '"voting_tokens":1';      check "tokens = 1" "0" "$?"

resp=$(curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"BADGE-001"}')
echo "$resp" | grep -q '"status":"already_voted"'; check "badge repetido bloqueado" "0" "$?"

resp=$(curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"BADGE-002"}')
echo "$resp" | grep -q '"voting_tokens":2'; check "tokens = 2" "0" "$?"

echo "== 4. Booth status / poll =="
resp=$(curl -s "$BASE/api/booth/status")
echo "$resp" | grep -q '"available":true'; check "cabine ve vaga" "0" "$?"
resp=$(curl -s "$BASE/api/poll")
echo "$resp" | grep -q '"label":"Sim"'; check "poll expoe opcoes" "0" "$?"

echo "== 5. Votar =="
resp=$(curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_1"}')
echo "$resp" | grep -q '"status":"recorded"'; check "voto registrado" "0" "$?"
echo "$resp" | grep -q '"voting_tokens":1';   check "token consumido (1 restante)" "0" "$?"

resp=$(curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_2"}')
echo "$resp" | grep -q '"status":"recorded"'; check "segundo voto registrado" "0" "$?"

resp=$(curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_1"}')
echo "$resp" | grep -q '"status":"no_token"'; check "sem token -> recusado" "0" "$?"

resp=$(curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_9"}')
echo "$resp" | grep -q '"error"'; check "opcao invalida -> erro" "0" "$?"

echo "== 6. Atraso controlado dos resultados =="
resp=$(curl -s "$BASE/api/results")
echo "$resp" | grep -q '"total":0'; check "logo apos votar total exibido = 0" "0" "$?"
echo "$resp" | grep -q '"total_pending_display":2'; check "2 votos em carencia" "0" "$?"
sleep 2.5
resp=$(curl -s "$BASE/api/results")
echo "$resp" | grep -q '"total":2'; check "apos delay total exibido = 2" "0" "$?"

echo "== 7. Reset / close =="
resp=$(curl -s -X POST "$BASE/api/admin/reset" -H "X-Device-Token: $TOKEN" \
  -H 'Content-Type: application/json' -d '{"clear_voters":true}')
echo "$resp" | grep -q '"total_votes":0'; check "reset zera votos" "0" "$?"
echo "$resp" | grep -q '"voters_checked_in":0'; check "reset limpa crachas" "0" "$?"

resp=$(curl -s -X POST "$BASE/api/admin/close" -H "X-Device-Token: $TOKEN")
echo "$resp" | grep -q '"status":"closed"'; check "close encerra poll" "0" "$?"

resp=$(curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"BADGE-003"}')
echo "$resp" | grep -q '"status":"no_poll"'; check "check-in apos encerrar -> no_poll" "0" "$?"

echo "== 8. Paginas HTML =="
for p in / /checkin /cabine /painel-tv /admin; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$p")
  check "GET $p -> 200" "200" "$code"
done

echo "== 9. Erro 404 de API =="
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/nao-existe")
check "API inexistente -> 404" "404" "$code"

echo "== 10. Cabines: registro, numeracao e alocacao =="
# Novo plebiscito (o anterior foi encerrado na secao 7).
curl -s -H "X-Device-Token: $TOKEN" -X POST "$BASE/api/admin/poll" -H 'Content-Type: application/json' \
  -d '{"question":"Cabines?","options":["Sim","Nao"]}' >/dev/null

resp=$(curl -s -X POST "$BASE/api/booth/register" -H 'Content-Type: application/json' -d '{"booth_id":"cab-A"}')
echo "$resp" | grep -q '"label":"Cabine 01"'; check "cabine A registrada como Cabine 01" "0" "$?"
resp=$(curl -s -X POST "$BASE/api/booth/register" -H 'Content-Type: application/json' -d '{"booth_id":"cab-B"}')
echo "$resp" | grep -q '"label":"Cabine 02"'; check "cabine B registrada como Cabine 02" "0" "$?"

resp=$(curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"BOOTH-1"}')
echo "$resp" | grep -q '"label":"Cabine 01"';       check "check-in aloca a Cabine 01" "0" "$?"
echo "$resp" | grep -q '"booth":{"id":"cab-A"';     check "resposta traz o objeto booth" "0" "$?"

resp=$(curl -s "$BASE/api/booth/status?booth_id=cab-A")
echo "$resp" | grep -q '"assigned":true';  check "Cabine 01 marcada como assigned" "0" "$?"
resp=$(curl -s "$BASE/api/booth/status?booth_id=cab-B")
echo "$resp" | grep -q '"assigned":false'; check "Cabine 02 ainda nao assigned" "0" "$?"

resp=$(curl -s -X POST "$BASE/api/booth/heartbeat" -H 'Content-Type: application/json' -d '{"booth_id":"cab-A","status":"idle"}')
echo "$resp" | grep -q '"idle":true'; check "heartbeat idle libera a cabine" "0" "$?"

resp=$(curl -s "$BASE/api/booths")
echo "$resp" | grep -q '"count":2'; check "GET /api/booths lista 2 cabines" "0" "$?"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/booth/heartbeat" \
  -H 'Content-Type: application/json' -d '{"booth_id":"nao-existe"}')
check "heartbeat de cabine desconhecida -> 404" "404" "$code"

echo "== 11. Relatorio oficial / Ata de fechamento =="
# Novo plebiscito com opcoes conhecidas (encerra o anterior e limpa crachas).
curl -s -H "X-Device-Token: $TOKEN" -X POST "$BASE/api/admin/poll" -H 'Content-Type: application/json' \
  -d '{"question":"Aprova a Ata?","options":["Chapa A","Chapa B","Chapa C"]}' >/dev/null

# 3 check-ins e 3 votos: opt_1 x2, opt_2 x1 (opt_3 fica com 0).
for b in RPT-1 RPT-2 RPT-3; do
  curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' \
    -d "{\"badge_id\":\"$b\"}" >/dev/null
done
curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_1"}' >/dev/null
curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_1"}' >/dev/null
curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_2"}' >/dev/null

# Endpoint JSON protegido: sem token -> 401.
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/admin/report")
check "report JSON sem token -> 401" "401" "$code"

# Encerra o plebiscito e consulta o relatorio agregado.
curl -s -X POST "$BASE/api/admin/close" -H "X-Device-Token: $TOKEN" >/dev/null
resp=$(curl -s -H "X-Device-Token: $TOKEN" "$BASE/api/admin/report")
check "report traz a pergunta" "Aprova a Ata?" \
  "$(printf '%s' "$resp" | jget 'd["poll"]["question"]')"
check "report: Chapa A = 2" "2" \
  "$(printf '%s' "$resp" | jget '[o["count"] for o in d["poll"]["options"] if o["label"]=="Chapa A"][0]')"
check "report: Chapa B = 1" "1" \
  "$(printf '%s' "$resp" | jget '[o["count"] for o in d["poll"]["options"] if o["label"]=="Chapa B"][0]')"
check "report: Chapa C = 0" "0" \
  "$(printf '%s' "$resp" | jget '[o["count"] for o in d["poll"]["options"] if o["label"]=="Chapa C"][0]')"
check "report: total de votos = 3" "3" \
  "$(printf '%s' "$resp" | jget 'd["poll"]["total_votes"]')"
check "report: votantes unicos = 3" "3" \
  "$(printf '%s' "$resp" | jget 'd["total_voters_registered"]')"
check "report: status closed" "closed" \
  "$(printf '%s' "$resp" | jget 'd["poll"]["status"]')"
check "report: closed_at preenchido" "True" \
  "$(printf '%s' "$resp" | jget 'bool(d["poll"]["closed_at"])')"
check "report: percentual Chapa A = 66.7" "66.7" \
  "$(printf '%s' "$resp" | jget '[o["percent"] for o in d["poll"]["options"] if o["label"]=="Chapa A"][0]')"

# Pagina HTML (A4) protegida pelo mesmo Device Token (query ?token=).
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/admin/relatorio")
check "pagina relatorio sem token -> 401" "401" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/admin/relatorio?token=$TOKEN")
check "pagina relatorio com token -> 200" "200" "$code"
body=$(curl -s "$BASE/admin/relatorio?token=$TOKEN")
echo "$body" | grep -q 'Aprova a Ata?';          check "pagina relatorio traz a pergunta" "0" "$?"
echo "$body" | grep -q 'Ata de Fechamento';      check "pagina rotulada como Ata de Fechamento" "0" "$?"
echo "$body" | grep -q 'Comissão Eleitoral';     check "pagina traz bloco de assinaturas" "0" "$?"

echo "== 12. Pareamento (PIN), IP do cliente e remocao de cabines =="
# Estado inicial: pareamento desativado e cabines expondo client_ip.
resp=$(curl -s -H "X-Device-Token: $TOKEN" "$BASE/api/admin/state")
echo "$resp" | grep -q '"pairing_enabled":false'; check "pareamento desativado por padrao" "0" "$?"
echo "$resp" | grep -q '"client_ip"';             check "estado expoe client_ip das cabines" "0" "$?"

# Gera um PIN de pareamento numerico de 6 digitos.
resp=$(curl -s -X POST "$BASE/api/admin/pairing" -H "X-Device-Token: $TOKEN" \
  -H 'Content-Type: application/json' -d '{"generate":true}')
PIN=$(printf '%s' "$resp" | jget 'd["pairing_pin"]')
case "$PIN" in
  [0-9][0-9][0-9][0-9][0-9][0-9]) ok=1;; *) ok=0;;
esac
check "admin gera PIN numerico de 6 digitos" "1" "$ok"
echo "$resp" | grep -q '"pairing_enabled":true'; check "pareamento ativado" "0" "$?"

# Cabine nova sem PIN (ou com PIN errado) -> 403 pairing_required.
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/booth/register" \
  -H 'Content-Type: application/json' -d '{"booth_id":"pair-A"}')
check "registro sem PIN -> 403" "403" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/booth/register" \
  -H 'Content-Type: application/json' -d '{"booth_id":"pair-A","pairing_pin":"000000x"}')
check "registro com PIN errado -> 403" "403" "$code"

# PIN correto -> pareia, guarda token e captura o IP (X-Forwarded-For).
resp=$(curl -s -X POST "$BASE/api/booth/register" -H 'Content-Type: application/json' \
  -H 'X-Forwarded-For: 10.20.30.40' \
  -d "{\"booth_id\":\"pair-A\",\"pairing_pin\":\"$PIN\"}")
BTOK=$(printf '%s' "$resp" | jget 'd["booth"]["pairing_token"]')
echo "$resp" | grep -q '"label":"Cabine 03"'; check "PIN correto pareia a cabine (Cabine 03)" "0" "$?"
[ -n "$BTOK" ]; check "resposta traz o pairing_token" "0" "$?"

# Re-registro apresentando o token dispensa o PIN.
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/booth/register" \
  -H 'Content-Type: application/json' -d "{\"booth_id\":\"pair-A\",\"pairing_token\":\"$BTOK\"}")
check "re-registro com token -> 200" "200" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/booth/register" \
  -H 'Content-Type: application/json' -d '{"booth_id":"pair-A"}')
check "re-registro sem token -> 403" "403" "$code"

# O IP observado no heartbeat aparece no painel admin.
curl -s -X POST "$BASE/api/booth/heartbeat" -H 'Content-Type: application/json' \
  -H 'X-Forwarded-For: 10.20.30.40' -d '{"booth_id":"pair-A","status":"idle"}' >/dev/null
resp=$(curl -s -H "X-Device-Token: $TOKEN" "$BASE/api/admin/state")
echo "$resp" | grep -q '"client_ip":"10.20.30.40"'; check "IP do heartbeat exposto no admin" "0" "$?"

# Remover / desconectar a cabine (deregistro).
resp=$(curl -s -X DELETE -H "X-Device-Token: $TOKEN" "$BASE/api/admin/booths/pair-A")
printf '%s' "$resp" | grep -q '"label":"Cabine 03"'; check "cabine removida sai da lista" "1" "$?"
code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "X-Device-Token: $TOKEN" \
  "$BASE/api/admin/booths/pair-A")
check "remover cabine inexistente -> 404" "404" "$code"

# Desativar o pareamento libera o registro novamente (modo legado).
resp=$(curl -s -X POST "$BASE/api/admin/pairing" -H "X-Device-Token: $TOKEN" \
  -H 'Content-Type: application/json' -d '{"clear":true}')
echo "$resp" | grep -q '"pairing_enabled":false'; check "desativar pareamento" "0" "$?"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/booth/register" \
  -H 'Content-Type: application/json' -d '{"booth_id":"pair-B"}')
check "apos desativar, registro aberto -> 200" "200" "$code"

echo
echo "RESULTADO: $pass passaram, $fail falharam"
kill $SRV 2>/dev/null
if [ "$fail" -ne 0 ]; then echo "--- server log ---"; tail -30 "$LOG"; exit 1; fi
exit 0
