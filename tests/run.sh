#!/bin/bash
# Прогон всех проверок на временных базах. Токен Telegram не нужен — бот подменяется фейком.
set -e
cd "$(dirname "$0")/.."
PY=${PY:-./venv/bin/python}
[ -x "$PY" ] || PY=python3
for t in tests/smoke.py tests/test_gate.py tests/test_publish.py tests/test_flows.py tests/test_ads.py tests/test_ad_flow.py tests/test_edge.py tests/test_web_admin.py tests/test_races.py tests/test_auth.py tests/test_members.py; do
    echo "=== $t ==="
    SP=$(mktemp -d) "$PY" "$t"
done
echo "ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ"
