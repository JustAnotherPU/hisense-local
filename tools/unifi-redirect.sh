#!/bin/sh
# Run on a UniFi gateway (UDM, UDR, UCG, UXG) as root:
#   sh unifi-redirect.sh install WASHER_IP HA_IP     redirect the washer to the add-on, now and after reboots
#   sh unifi-redirect.sh ensure WASHER_IP HA_IP      install only if missing; prints "ok" or "reinstalled"
#   sh unifi-redirect.sh remove                      undo it
# A gateway firmware update wipes /etc but keeps /data. Run "ensure" from a scheduler on another
# machine (ssh root@GATEWAY sh /data/hisense-local/unifi-redirect.sh ensure ...) to put it back.
set -eu
DIR=${HL_DIR:-/data/hisense-local}
UNIT=${HL_UNIT:-/etc/systemd/system/hisense-local-rules}
ip_ok() { echo "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; }

case "${1:-}" in
install)
    W=${2:-}; HA=${3:-}
    ip_ok "$W" && ip_ok "$HA" || { echo "usage: $0 install WASHER_IP HA_IP" >&2; exit 2; }
    mkdir -p "$DIR"
    cat > "$DIR/washer-rules.sh" <<EOF
#!/bin/sh
# Keeps the washer on the hisense-local add-on. Idempotent; a timer runs it every 30 s.
W=$W; HA=$HA; P=4444; C="hisense-cap"
EOF
    cat >> "$DIR/washer-rules.sh" <<'EOF'
# -w: UniFi rewrites its own rules at the same time; wait for the xtables lock instead of failing
ipt() { iptables -w 10 "$@"; }
ipt -t nat -C PREROUTING -s $W/32 -p tcp -m tcp --dport 443 -m comment --comment $C -j DNAT --to-destination $HA:$P 2>/dev/null \
  || ipt -t nat -I PREROUTING 1 -s $W/32 -p tcp -m tcp --dport 443 -m comment --comment $C -j DNAT --to-destination $HA:$P
ipt -t nat -C POSTROUTING -s $W/32 -d $HA/32 -p tcp -m tcp --dport $P -m comment --comment $C -j MASQUERADE 2>/dev/null \
  || ipt -t nat -I POSTROUTING 1 -s $W/32 -d $HA/32 -p tcp -m tcp --dport $P -m comment --comment $C -j MASQUERADE
# The two ACCEPTs must sit before UniFi's own jump, or a zone rule such as "block IoT to LAN" drops the link.
jump=$(ipt -S FORWARD | grep -n -- "-j UBIOS_FORWARD_JUMP" | head -1 | cut -d: -f1)
for dir in out back; do
  if [ $dir = out ]; then R="-s $W/32 -d $HA/32 -p tcp -m tcp --dport $P"; else R="-s $HA/32 -d $W/32 -p tcp -m tcp --sport $P"; fi
  pos=$(ipt -S FORWARD | grep -n -- "$R -m comment --comment $C -j ACCEPT" | head -1 | cut -d: -f1)
  if [ -z "$pos" ] || { [ -n "$jump" ] && [ "$pos" -gt "$jump" ]; }; then
    [ -n "$pos" ] && ipt -D FORWARD $R -m comment --comment $C -j ACCEPT
    ipt -I FORWARD 1 $R -m comment --comment $C -j ACCEPT
    logger -t hisense-local "re-added FORWARD $dir rule"
  fi
done
EOF
    chmod 755 "$DIR/washer-rules.sh"
    printf '[Unit]\nDescription=Keep the Hisense washer on the hisense-local add-on\n[Service]\nType=oneshot\nExecStart=%s/washer-rules.sh\n' "$DIR" > "$UNIT.service"
    printf '[Unit]\nDescription=Run hisense-local-rules every 30 s\n[Timer]\nOnBootSec=45\nOnUnitActiveSec=30\nAccuracySec=5\n[Install]\nWantedBy=timers.target\n' > "$UNIT.timer"
    systemctl daemon-reload
    systemctl enable --now hisense-local-rules.timer
    "$DIR/washer-rules.sh"
    echo "redirect active: $W:443 -> $HA:4444. Set peer_ip to this gateway's address on the washer's network."
    ;;
ensure)
    if systemctl is-active --quiet hisense-local-rules.timer && [ -x "$DIR/washer-rules.sh" ] \
            && grep -q "^W=${2:-}; HA=${3:-};" "$DIR/washer-rules.sh"; then
        echo ok
    else
        sh "$0" install "${2:-}" "${3:-}" >/dev/null 2>&1 && echo reinstalled
    fi
    ;;
remove)
    systemctl disable --now hisense-local-rules.timer 2>/dev/null || true
    rm -f "$UNIT.service" "$UNIT.timer"; systemctl daemon-reload
    for tbl in nat filter; do
        iptables -w 10 -t $tbl -S | grep -- "--comment hisense-cap" | sed 's/^-A/-D/' |
            while read -r rule; do eval "iptables -w 10 -t $tbl $rule"; done
    done
    rm -f "$DIR/washer-rules.sh"
    echo "redirect removed"
    ;;
*)
    echo "usage: $0 install|ensure WASHER_IP HA_IP | remove" >&2; exit 2 ;;
esac
