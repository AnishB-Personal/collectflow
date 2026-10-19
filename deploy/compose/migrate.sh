#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Running CollectFlow database migrations inside backend container..."
docker compose -f "${SCRIPT_DIR}/docker-compose.yml" exec -T backend alembic upgrade head
echo "==> Migrations successfully completed."
