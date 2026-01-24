#!/bin/bash
# Управление systemd сервисами
# Использование: ./commands/systemd-control.sh [start|stop|restart|status] [bot-name]

ACTION="$1"
BOT="${2:-bot-sniper-0-pump}"

case "$ACTION" in
    start)
        systemctl start "pumpfun-bot@${BOT}"
        systemctl start pumpfun-metrics
        echo "Started: $BOT + metrics"
        ;;
    stop)
        systemctl stop "pumpfun-bot@${BOT}"
        echo "Stopped: $BOT"
        ;;
    restart)
        systemctl restart "pumpfun-bot@${BOT}"
        echo "Restarted: $BOT"
        ;;
    status)
        systemctl status "pumpfun-bot@${BOT}" --no-pager
        ;;
    logs)
        journalctl -u "pumpfun-bot@${BOT}" -f
        ;;
    *)
        echo "Usage: systemd-control.sh [start|stop|restart|status|logs] [bot-name]"
        echo "Available bots:"
        ls -1 /opt/pumpfun-bonkfun-bot/bots/*.yaml | xargs -n1 basename | sed 's/.yaml//'
        ;;
esac
