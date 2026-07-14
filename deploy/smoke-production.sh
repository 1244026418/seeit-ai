#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

compose="docker compose --env-file deploy/.env.production -f docker-compose.prod.yml"
api_id=$($compose ps -q api)
mysql_id=$($compose ps -q mysql)
worker_id=$($compose ps -q worker)
broker_id=$($compose ps -q rmqbroker)

if [ -z "$api_id" ] || [ -z "$mysql_id" ] || [ -z "$worker_id" ] || [ -z "$broker_id" ]; then
  echo "生产容器不完整，请先检查 docker compose ps" >&2
  exit 1
fi

smoke_username="smoke_$(date +%s)_$$"
smoke_password="Smoke_$(openssl rand -hex 12)"
container_script=/tmp/seeit-production-smoke.py

cleanup() {
  docker exec -u 0 "$api_id" rm -f "$container_script" >/dev/null 2>&1 || true
  docker exec -e SMOKE_USERNAME="$smoke_username" "$mysql_id" sh -lc '
    mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" \
      -e "DELETE FROM users WHERE username = '\''${SMOKE_USERNAME}'\'';" >/dev/null
  ' >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker cp deploy/smoke-test.py "$api_id:$container_script" >/dev/null
docker exec \
  -e SMOKE_USERNAME="$smoke_username" \
  -e SMOKE_PASSWORD="$smoke_password" \
  "$api_id" python "$container_script" http://localhost:9090

consumer_group=$(docker exec "$worker_id" sh -lc 'printf %s "$ROCKETMQ_CONSUMER_GROUP"')
echo
echo "等待 RocketMQ 提交消费 Offset："
attempt=1
while [ "$attempt" -le 30 ]; do
  progress=$(docker exec "$broker_id" sh mqadmin consumerProgress \
    -n rmqnamesrv:9876 \
    -g "$consumer_group")
  diff_total=$(printf '%s\n' "$progress" | awk '/Diff Total:/ {print $3}')
  if [ "$diff_total" = "0" ]; then
    printf '%s\n' "$progress"
    exit 0
  fi
  sleep 1
  attempt=$((attempt + 1))
done

printf '%s\n' "$progress"
echo "RocketMQ 消费 Offset 在 30 秒内未追平" >&2
exit 1
