#!/bin/bash
# Регистрирует ежедневный бэкап в /etc/cron.daily.
source "$(dirname "$0")/common.sh"
TARGET=/etc/cron.daily/affbazaar-backup
$SUDO tee "$TARGET" >/dev/null <<CRON
#!/bin/bash
BACKUP_DIR="${BACKUP_DIR}" bash "$(pwd)/deploy/backup.sh" >> /var/log/affbazaar-backup.log 2>&1
CRON
$SUDO chmod +x "$TARGET"
green "✔ ежедневный бэкап: $TARGET → ${BACKUP_DIR}"
