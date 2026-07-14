#!/bin/sh
set -eu

attempt=1
max_attempts="${DB_MIGRATION_MAX_ATTEMPTS:-30}"
retry_seconds="${DB_MIGRATION_RETRY_SECONDS:-2}"

until alembic upgrade head; do
    if [ "$attempt" -ge "$max_attempts" ]; then
        echo "数据库迁移连续失败 ${attempt} 次，终止 API 启动。" >&2
        exit 1
    fi
    echo "数据库尚未就绪，${retry_seconds} 秒后重试迁移（${attempt}/${max_attempts}）。" >&2
    attempt=$((attempt + 1))
    sleep "$retry_seconds"
done

exec uvicorn seeit.main:app --host 0.0.0.0 --port 9090
