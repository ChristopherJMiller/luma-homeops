# --- Configuration ---
SSH_USER=$(whoami)
ROUTER_IP="192.168.0.1"
DHCP_COMMAND="/opt/vyatta/bin/vyatta-op-cmd-wrapper show dhcp server leases"
CONNECT_TIMEOUT=5 # seconds for curl connection timeout

echo "Attempting to fetch DHCP leases from ${ROUTER_IP} as user ${SSH_USER}..."
# The sed command helps normalize line endings if the SSH server sends 

dhcp_leases_output=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "${SSH_USER}@${ROUTER_IP}" "${DHCP_COMMAND}" 2>/dev/null | sed 's/\r$//')

# Check if SSH command was successful and produced output
if [ $? -ne 0 ]; then
    echo "Error: Failed to SSH to ${ROUTER_IP} or execute command."
    echo "Please check your SSH configuration, username, network connectivity, and ensure the host key for ${ROUTER_IP} is known if not using BatchMode=yes with key auth."
    exit 1
fi

if [ -z "$dhcp_leases_output" ]; then
    echo "Error: No output received from DHCP lease command on ${ROUTER_IP}."
    echo "Ensure the command '${DHCP_COMMAND}' is correct and provides output on the router."
    exit 1
fi

echo "DHCP leases fetched successfully."
echo "Identifying IP addresses without hostnames..."

# awk script explanation:
# NR > 2: Skip the first two header lines of the DHCP lease output.
# NF == 10: Select lines that have exactly 10 fields. Based on the provided example,
#           lines without a hostname have 10 fields (IP, MAC, State, LStart_Date, LStart_Time,
#           LExp_Date, LExp_Time, Remaining, Pool, Origin).
#           Lines with a hostname would have 11 fields.
# $1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ : Ensures the first field is a valid IP address format.
# {print $1}: If conditions are met, print the first field (the IP address).
candidate_ips=$(echo "$dhcp_leases_output" | awk '
NR > 2 && NF == 10 && $1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {
    # Further check: ensure Pool (field 9) and Origin (field 10) look reasonable
    # This is an optional heuristic if the NF==10 check needs more specificity
    # For now, relying on NF==10 as per the observed structure
    print $1
}
')

if [ -z "$candidate_ips" ]; then
    echo "No IP addresses without hostnames found in the DHCP leases."
    exit 0
fi

echo "Found potential IP addresses without hostnames:"
echo "${candidate_ips}"
echo ""
echo "Checking these IPs for responsive services on port 443 (potential IPMI dashboards)..."

found_ipmi_count=0
while IFS= read -r ip; do
    if [ -z "$ip" ]; then
        continue
    fi
    echo -n "Checking ${ip}:443 ... "
    # Using curl:
    # -s: Silent mode (no progress meter)
    # -k: Allow insecure server connections (IPMI often uses self-signed certs)
    # -I: Perform a HEAD request (faster, as we only need headers/status)
    # -o /dev/null: Discard the actual page content
    # -w "%{http_code}": Output only the HTTP status code
    # --connect-timeout: Max time allowed for connection
    # The URL must be https for port 443
    http_code=$(curl -s -k -I -o /dev/null -w "%{http_code}" --connect-timeout "${CONNECT_TIMEOUT}" "https://${ip}:443")

    # Check if http_code indicates a successful response (e.g., 2xx or 3xx)
    # "000" typically means curl couldn't connect or timed out.
    if [[ "$http_code" =~ ^[23] ]]; then # Matches 2xx (e.g. 200) or 3xx (e.g. 302) status codes
        echo "Responded with HTTP $http_code. Potential IPMI dashboard: https://${ip}:443"
        found_ipmi_count=$((found_ipmi_count + 1))
    elif [ "$http_code" != "000" ] && [ ! -z "$http_code" ]; then
        # Responded with a non-2xx/3xx code (e.g. 401, 403, 500). Still, something is there.
        echo "Responded with HTTP $http_code. A web service is present but may not be the IPMI dashboard or requires auth: https://${ip}:443"
    else
        echo "No successful HTTP response (Code: $http_code)."
    fi
done <<< "$candidate_ips"

echo ""
if [ $found_ipmi_count -gt 0 ]; then
    echo "Found $found_ipmi_count potential IPMI dashboard(s)."
else
    echo "No IPMI dashboards (responding with 2xx/3xx HTTP codes) were found on the identified IPs."
fi

exit 0 
