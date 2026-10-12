#!/usr/bin/env bash
# Teste de PERSISTENCIA do estado (SQLite) e ATOMICIDADE da fila de tokens.
#
# Valida que, apos REINICIAR o servidor, continuam persistidos:
#   * o plebiscito ativo (pergunta, opcoes, status open/closed);
#   * a fila de voting_tokens;
#   * os crachas (voters) e os votos ja registrados.
# Valida tambem que o decremento de tokens e atomico sob concorrencia
# (varias cabines disputando - nunca se consome mais que o disponivel).
#
# Roda em porta isolada e usa um banco temporario dedicado.
set -u
cd "$(dirname "$0")"

PORT=5056
TOKEN="persisttoken456"
BASE="http://127.0.0.1:${PORT}"
DB="/tmp/votacao_persist_$$.db"
LOG="/tmp/votacao_persist_$$.log"
TMPD="/tmp/votacao_persist_$$.d"
mkdir -p "$TMPD"

rm -f "$DB" "$DB-wal" "$DB-shm"

export DEVICE_TOKEN="$TOKEN"
export VOTING_DB="$DB"
export PORT="$PORT"
export RESULTS_DELAY_SECONDS=0
export CHECKIN_COOLDOWN_SECONDS=0

SRV=""
start_server() {
  python3 app.py > "$LOG" 2>&1 &
  SRV=$!
  for _ in $(seq 1 80); do
    if curl -s "$BASE/api/health" >/dev/null 2>&1; then return 0; fi
    if ! kill -0 "$SRV" 2>/dev/null; then
      echo "ERRO: servidor morreu ao iniciar"; cat "$LOG"; exit 1
    fi
    sleep 0.25
  done
  echo "ERRO: timeout esperando o servidor"; cat "$LOG"; exit 1
}

stop_server() {
  [ -n "$SRV" ] || return 0
  kill "$SRV" 2>/dev/null
  for _ in $(seq 1 50); do
    kill -0 "$SRV" 2>/dev/null || break
    sleep 0.1
  done
  kill -9 "$SRV" 2>/dev/null
  wait "$SRV" 2>/dev/null
  SRV=""
  sleep 0.5   # garante que o socket/DB foram liberados antes do proximo boot
}

trap 'stop_server' EXIT

pass=0; fail=0
check() { # descricao, esperado, obtido
  if [ "$2" = "$3" ]; then echo "  PASS  $1"; pass=$((pass+1));
  else echo "  FAIL  $1 (esperado=$2 obtido=$3)"; fail=$((fail+1)); fi
}

admin() { curl -s -H "X-Device-Token: $TOKEN" "$@"; }
jget()  { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

# -------------------------------------------------------------------------- #
echo "== Fase 1: popular o estado (servidor #1) =="
start_server

resp=$(admin -X POST "$BASE/api/admin/poll" -H 'Content-Type: application/json' \
  -d '{"question":"Pergunta persistente?","options":["Sim","Nao","Talvez"]}')
POLL_ID=$(printf '%s' "$resp" | jget 'd["poll"]["id"]')
echo "$resp" | grep -q '"question":"Pergunta persistente?"'
check "plebiscito criado (id=$POLL_ID)" "0" "$?"

curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"P-1"}' >/dev/null
resp=$(curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"P-2"}')
echo "$resp" | grep -q '"voting_tokens":2'; check "2 check-ins -> fila = 2" "0" "$?"

resp=$(curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_1"}')
echo "$resp" | grep -q '"voting_tokens":1'; check "1 voto -> fila = 1" "0" "$?"

resp=$(admin "$BASE/api/admin/state")
echo "$resp" | grep -q '"voting_tokens":1';   check "pre-restart: fila    = 1" "0" "$?"
echo "$resp" | grep -q '"voters_checked_in":2'; check "pre-restart: crachas = 2" "0" "$?"

# Registra uma cabine para provar que a numeracao sobrevive ao reboot.
resp=$(curl -s -X POST "$BASE/api/booth/register" -H 'Content-Type: application/json' \
  -d '{"booth_id":"persist-cab-A"}')
echo "$resp" | grep -q '"label":"Cabine 01"'; check "cabine registrada (Cabine 01, pre-restart)" "0" "$?"

# -------------------------------------------------------------------------- #
echo "== Fase 2: reiniciar e verificar persistencia (servidor #2) =="
stop_server
start_server

resp=$(admin "$BASE/api/admin/state")
echo "$resp" | grep -q '"question":"Pergunta persistente?"'; check "pergunta persistiu" "0" "$?"
echo "$resp" | grep -q '"label":"Talvez"';                   check "opcoes persistiram" "0" "$?"
echo "$resp" | grep -q '"status":"open"';                    check "poll continua aberta" "0" "$?"
echo "$resp" | grep -q '"voting_tokens":1';                  check "FILA de tokens persistiu (1)" "0" "$?"
echo "$resp" | grep -q '"voters_checked_in":2';              check "crachas persistiram (2)" "0" "$?"
echo "$resp" | grep -q '"total_votes":1';                    check "votos persistiram (1)" "0" "$?"

resp=$(curl -s "$BASE/api/booth/status")
echo "$resp" | grep -q '"available":true';            check "cabine ainda ve vaga" "0" "$?"
echo "$resp" | grep -q "\"poll_id\":$POLL_ID";         check "poll_id preservado" "0" "$?"

resp=$(curl -s "$BASE/api/booths")
echo "$resp" | grep -q '"label":"Cabine 01"';          check "numeracao da cabine persistiu apos reboot" "0" "$?"

# O token remanescente continua utilizavel apos o restart.
resp=$(curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' -d '{"option_id":"opt_2"}')
echo "$resp" | grep -q '"status":"recorded"';  check "voto aceito apos restart" "0" "$?"
echo "$resp" | grep -q '"voting_tokens":0';    check "token remanescente consumido" "0" "$?"

# -------------------------------------------------------------------------- #
echo "== Fase 3: status 'closed' persiste apos reinicio =="
admin -X POST "$BASE/api/admin/close" >/dev/null
resp=$(admin "$BASE/api/admin/state")
echo "$resp" | grep -q '"status":"closed"'; check "close aplicado (pre-restart)" "0" "$?"
stop_server
start_server
resp=$(admin "$BASE/api/admin/state")
echo "$resp" | grep -q '"status":"closed"'; check "status closed persistiu" "0" "$?"
resp=$(curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' -d '{"badge_id":"P-9"}')
echo "$resp" | grep -q '"status":"no_poll"'; check "check-in apos restart -> no_poll" "0" "$?"

# -------------------------------------------------------------------------- #
echo "== Fase 4: atomicidade da fila sob concorrencia (servidor #3) =="
# Novo plebiscito (zera fila/crachas) + 5 check-ins -> fila = 5.
admin -X POST "$BASE/api/admin/poll" -H 'Content-Type: application/json' \
  -d '{"question":"Corrida?","options":["A","B"]}' >/dev/null
for i in $(seq 1 5); do
  curl -s -X POST "$BASE/api/checkin" -H 'Content-Type: application/json' \
    -d "{\"badge_id\":\"R-$i\"}" >/dev/null
done
resp=$(admin "$BASE/api/admin/state")
echo "$resp" | grep -q '"voting_tokens":5'; check "fila preparada = 5" "0" "$?"

# 12 votos disparados em paralelo: apenas 5 podem ser aceitos.
vote_pids=()
for i in $(seq 1 12); do
  curl -s -X POST "$BASE/api/vote" -H 'Content-Type: application/json' \
    -d '{"option_id":"opt_1"}' > "$TMPD/vote_$i.json" &
  vote_pids+=("$!")
done
wait "${vote_pids[@]}"   # aguarda apenas os votos (o servidor roda em background)

recorded=$(grep -l '"status":"recorded"' "$TMPD"/vote_*.json 2>/dev/null | wc -l | tr -d ' ')
rejected=$(grep -l '"status":"no_token"' "$TMPD"/vote_*.json 2>/dev/null | wc -l | tr -d ' ')
check "exatamente 5 votos aceitos" "5" "$recorded"
check "exatamente 7 recusados"     "7" "$rejected"
resp=$(curl -s "$BASE/api/booth/status")
echo "$resp" | grep -q '"voting_tokens":0';  check "fila zerada (sem negativos)" "0" "$?"

stop_server

# -------------------------------------------------------------------------- #
echo
echo "RESULTADO: $pass passaram, $fail falharam"
if [ "$fail" -ne 0 ]; then echo "--- server log ---"; tail -40 "$LOG"; exit 1; fi
rm -rf "$DB" "$DB-wal" "$DB-shm" "$LOG" "$TMPD"
exit 0
