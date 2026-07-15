#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/deploy/.env.production"
BACKUP_DIR="$ROOT_DIR/backups"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$ROOT_DIR/docker-compose.prod.yml")

if [[ ! -f "$ENV_FILE" ]]; then
  echo "缺少 $ENV_FILE" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$BACKUP_DIR/mysql-$STAMP.sql.gz"

"${COMPOSE[@]}" exec -T mysql sh -c 'exec mysqldump --single-transaction --quick --no-tablespaces -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"' | gzip > "$TARGET"
find "$BACKUP_DIR" -type f -name 'mysql-*.sql.gz' -mtime +7 -delete
echo "MySQL 备份已生成：$TARGET"
