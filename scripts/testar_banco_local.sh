#!/usr/bin/env bash
# Sobe um Postgres temporário, aplica stub do Supabase + migrações e roda os testes.
# Uso: ./scripts/testar_banco_local.sh   (precisa de Postgres 15+ instalado)
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
PGBIN="${PGBIN:-$(dirname "$(command -v pg_ctl 2>/dev/null || ls -d /usr/lib/postgresql/*/bin/pg_ctl | tail -1)")}"
DIR="$(mktemp -d)"
PORTA="${PORTA:-54329}"

# initdb não roda como root
if [ "$(id -u)" = "0" ]; then
  COMO=(runuser -u postgres --); chown -R postgres "$DIR"
else
  COMO=()
fi

limpar() { "${COMO[@]}" "$PGBIN/pg_ctl" -D "$DIR/dados" -m immediate stop >/dev/null 2>&1 || true; rm -rf "$DIR"; }
trap limpar EXIT

"${COMO[@]}" "$PGBIN/initdb" -D "$DIR/dados" -U postgres --locale=C.UTF-8 -E UTF8 >/dev/null 2>&1
"${COMO[@]}" "$PGBIN/pg_ctl" -D "$DIR/dados" -o "-p $PORTA -k $DIR" -l "$DIR/log" start >/dev/null

PSQL=(psql -h "$DIR" -p "$PORTA" -U postgres -d postgres -v ON_ERROR_STOP=1 -q)

"${PSQL[@]}" -f "$RAIZ/supabase/test/00_stub_supabase.sql"
for m in "$RAIZ"/supabase/migrations/*.sql; do
  echo "Aplicando $(basename "$m")"
  "${PSQL[@]}" -f "$m"
done
"${PSQL[@]}" -f "$RAIZ/supabase/test/01_testes.sql"
