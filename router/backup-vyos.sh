set -euo pipefail

VYOS_HOST="${VYOS_HOST:-192.168.0.1}"
VYOS_USER="${VYOS_USER:-chris}"
BACKUP_FILE="vyos_config.txt"
TEMP_FILE="vyos_config.tmp"

echo "Backing up VyOS configuration from $VYOS_HOST..."

ssh "$VYOS_USER@$VYOS_HOST" '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' > "$TEMP_FILE"

if [ -f "$TEMP_FILE" ] && [ -s "$TEMP_FILE" ]; then
    echo "Backup completed successfully ($(wc -l < "$TEMP_FILE") lines)"
    
    echo "Organizing configuration..."
    
    cat > "$BACKUP_FILE" << EOF
# VyOS Configuration
# Generated: $(date)

# SYSTEM CONFIGURATION
# ===================
EOF
    grep -E "^set system" "$TEMP_FILE" | sort >> "$BACKUP_FILE"
    echo "" >> "$BACKUP_FILE"
    
    echo "# INTERFACES" >> "$BACKUP_FILE"
    echo "# ==========" >> "$BACKUP_FILE"
    grep -E "^set interfaces" "$TEMP_FILE" | sort >> "$BACKUP_FILE"
    echo "" >> "$BACKUP_FILE"
    
    echo "# FIREWALL CONFIGURATION" >> "$BACKUP_FILE"
    echo "# =====================" >> "$BACKUP_FILE"
    echo "## Global Options" >> "$BACKUP_FILE"
    grep -E "^set firewall global-options" "$TEMP_FILE" | sort >> "$BACKUP_FILE"
    echo "" >> "$BACKUP_FILE"
    echo "## Groups" >> "$BACKUP_FILE"
    grep -E "^set firewall group" "$TEMP_FILE" | sort >> "$BACKUP_FILE"
    echo "" >> "$BACKUP_FILE"
    echo "## IPv4 Rules" >> "$BACKUP_FILE"
    grep -E "^set firewall ipv4" "$TEMP_FILE" | sort >> "$BACKUP_FILE"
    echo "" >> "$BACKUP_FILE"
    
    echo "# NAT CONFIGURATION" >> "$BACKUP_FILE"
    echo "# =================" >> "$BACKUP_FILE"
    echo "## Destination NAT" >> "$BACKUP_FILE"
    grep -E "^set nat destination" "$TEMP_FILE" | sort >> "$BACKUP_FILE"
    echo "" >> "$BACKUP_FILE"
    echo "## Source NAT" >> "$BACKUP_FILE"
    grep -E "^set nat source" "$TEMP_FILE" | sort >> "$BACKUP_FILE"
    echo "" >> "$BACKUP_FILE"
    
    echo "# SERVICES" >> "$BACKUP_FILE"
    echo "# ========" >> "$BACKUP_FILE"
    echo "## DHCP Server" >> "$BACKUP_FILE"
    grep -E "^set service dhcp-server" "$TEMP_FILE" | sort >> "$BACKUP_FILE"
    echo "" >> "$BACKUP_FILE"
    echo "## DNS Forwarding" >> "$BACKUP_FILE"
    grep -E "^set service dns" "$TEMP_FILE" | sort >> "$BACKUP_FILE"
    echo "" >> "$BACKUP_FILE"
    echo "## NTP" >> "$BACKUP_FILE"
    grep -E "^set service ntp" "$TEMP_FILE" | sort >> "$BACKUP_FILE"
    echo "" >> "$BACKUP_FILE"
    echo "## SSH" >> "$BACKUP_FILE"
    grep -E "^set service ssh" "$TEMP_FILE" | sort >> "$BACKUP_FILE"
    echo "" >> "$BACKUP_FILE"
    
    echo "# CONTAINER CONFIGURATION" >> "$BACKUP_FILE"
    echo "# ======================" >> "$BACKUP_FILE"
    grep -E "^set container" "$TEMP_FILE" | sort >> "$BACKUP_FILE"
    
    rm -f "$TEMP_FILE"
    echo "Configuration organized and saved to: $BACKUP_FILE"
else
    echo "Warning: Backup file is empty or missing"
    exit 1
fi
