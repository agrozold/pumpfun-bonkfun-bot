#!/bin/bash
# Управление systemd сервисами pumpfun-bot

ACTION="$1"
BOT="${2:-bot-sniper-0-pump}"

case "$ACTION" in
    start)
        systemctl start "pumpfun-bot@${BOT}"
        echo "Started: $BOT"
        systemctl status "pumpfun-bot@${BOT}" --no-pager -l
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
        systemctl status "pumpfun-bot@${BOT}" --no-pager -l
        ;;
    logs)
        journalctl -u "pumpfun-bot@${BOT}" -f --no-pager
        ;;
    enable)
        systemctl enable "pumpfun-bot@${BOT}"
        echo "Enabled: $BOT (will start on boot)"
        ;;
    list)
        echo "Available bot configs:"
        ls -1 /opt/pumpfun-bonkfun-bot/bots/*.yaml | xargs -n1 basename | sed 's/.yaml//'
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs|enable|list} [bot-name]"
        echo "Default bot: bot-sniper-0-pump"
        exit 1
        ;;
esac
