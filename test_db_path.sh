#!/usr/bin/env bash
# Valida a inicializacao do banco (criacao de diretorio + fallback).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="/tmp/dbtest_$$"
rm -rf "$WORK"; mkdir -p "$WORK"

pass=0; fail=0
check() { if [ "$2" = "$3" ]; then echo "  PASS  $1"; pass=$((pass+1));
          else echo "  FAIL  $1 (esperado=$2 obtido=$3)"; fail=$((fail+1)); fi; }

echo "== A. Diretorio aninhado inexistente e criado (bug original) =="
NEST="$WORK/a/b/c/votacao.db"
out=$(cd "$HERE" && DEVICE_TOKEN=t VOTING_DB="$NEST" PORT=5091 timeout 3 python3 app.py 2>&1)
echo "$out" | grep -q 'unable to open database file'; check "sem OperationalError" "1" "$?"
[ -f "$NEST" ]; check "arquivo criado no diretorio aninhado" "0" "$?"

echo "== B. Caminho relativo (resolvido a partir do cwd) =="
mkdir -p "$WORK/rel"
out=$(cd "$WORK/rel" && DEVICE_TOKEN=t VOTING_DB="dados/votacao.db" PORT=5092 timeout 3 python3 "$HERE/app.py" 2>&1)
[ -f "$WORK/rel/dados/votacao.db" ]; check "criou dados/votacao.db relativo ao cwd" "0" "$?"

echo "== C. Diretorio sem permissao de escrita -> fallback =="
mkdir -p "$WORK/ro"; chmod 500 "$WORK/ro"
rm -f "$HERE/voting.db"
out=$(cd "$HERE" && DEVICE_TOKEN=t VOTING_DB="$WORK/ro/votacao.db" PORT=5093 timeout 3 python3 app.py 2>&1)
echo "$out" | grep -q "Sem permissao de escrita"; check "detectou falta de permissao" "0" "$?"
echo "$out" | grep -qi "usando fallback"; check "anunciou fallback" "0" "$?"
echo "$out" | grep -q "SISTEMA DE VOTACAO LOCAL"; check "servidor iniciou mesmo assim" "0" "$?"
[ -f "$HERE/voting.db" ]; check "banco criado no fallback do script" "0" "$?"
chmod 700 "$WORK/ro"
rm -f "$HERE/voting.db" "$HERE/voting.db-wal" "$HERE/voting.db-shm"

echo "== D. Caminho invalido (pai e um arquivo regular) -> fallback =="
touch "$WORK/regular"
rm -f "$HERE/voting.db"
out=$(cd "$HERE" && DEVICE_TOKEN=t VOTING_DB="$WORK/regular/sub/votacao.db" PORT=5094 timeout 3 python3 app.py 2>&1)
echo "$out" | grep -qi "fallback"; check "anunciou fallback" "0" "$?"
echo "$out" | grep -q "SISTEMA DE VOTACAO LOCAL"; check "servidor iniciou mesmo assim" "0" "$?"
[ -f "$HERE/voting.db" ]; check "banco criado no fallback do script" "0" "$?"
rm -f "$HERE/voting.db" "$HERE/voting.db-wal" "$HERE/voting.db-shm"

rm -rf "$WORK"
echo
echo "RESULTADO: $pass passaram, $fail falharam"
[ "$fail" -eq 0 ]
