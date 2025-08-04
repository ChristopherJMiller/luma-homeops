# Phase 1 Deployment Instructions

## Overview
Deploy AlertManager Discord notifications and VyOS log buffering with zero downtime.

## Prerequisites
✅ Discord webhook sealed and committed  
✅ VyOS router accessible at 192.168.0.1  
✅ ArgoCD running and syncing  

## Step 1: Deploy AlertManager Configuration

The new AlertManager configuration will be automatically deployed via ArgoCD:

```bash
# Check if ArgoCD picked up the new application
kubectl get applications -n argo-cd | grep prometheus-stack-config

# Watch the sync process
kubectl get pods -n prometheus -w

# Verify AlertManager configuration loaded
kubectl logs -n prometheus -l app.kubernetes.io/name=alertmanager
```

**Expected behavior:**
- AlertManager pods will restart with new configuration
- Discord webhook will be loaded from sealed secret
- Weekend suppression rules will be active

## Step 2: Configure VyOS Log Buffer

Run the setup script on your VyOS router:

```bash
# Copy script to VyOS router
scp router/setup-syslog-buffer.sh vyos@192.168.0.1:/tmp/

# SSH to VyOS and run setup
ssh vyos@192.168.0.1
sudo /tmp/setup-syslog-buffer.sh
```

**Manual configuration steps on VyOS:**
```vyos
configure

# Configure syslog to receive remote logs
set system syslog host 0.0.0.0 facility all level info
set system syslog host 0.0.0.0 protocol udp  
set system syslog host 0.0.0.0 port 514

# Configure log files with rotation
set system syslog file kubernetes-logs facility all level info
set system syslog file kubernetes-logs archive size 100M
set system syslog file kubernetes-logs archive files 200

# Allow syslog from Kubernetes network
set firewall name LAN_LOCAL rule 100 action accept
set firewall name LAN_LOCAL rule 100 protocol udp
set firewall name LAN_LOCAL rule 100 destination port 514
set firewall name LAN_LOCAL rule 100 source address 192.168.0.0/24
set firewall name LAN_LOCAL rule 100 description "Kubernetes syslog"

commit
save
exit
```

## Step 3: Test Discord Notifications

Create a test alert to verify Discord integration:

```bash
# Create a test alert
kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: test-alert-pod
  namespace: default
  labels:
    app: test-alert
spec:
  containers:
  - name: test
    image: busybox
    command: ['sh', '-c', 'echo "Test alert generated"; exit 1']
  restartPolicy: Never
EOF

# Check if alert fires in Prometheus
kubectl port-forward -n prometheus svc/prometheus-operated 9090:9090
# Visit http://localhost:9090/alerts
```

**Manual test alert:**
```bash
# Force an alert for testing
kubectl patch deployment -n prometheus prometheus-stack-config --patch='{"spec":{"replicas":0}}'
# This should trigger a "DeploymentReplicasNotAvailable" alert

# Restore after testing
kubectl patch deployment -n prometheus prometheus-stack-config --patch='{"spec":{"replicas":1}}'
```

## Step 4: Test VyOS Log Reception

```bash
# Test log reception from Kubernetes cluster
kubectl run test-logger --image=busybox --rm -it -- logger -n 192.168.0.1 -P 514 "Test message from Kubernetes"

# Check logs received on VyOS
ssh vyos@192.168.0.1
sudo tail -f /var/log/kubernetes-logs
```

## Verification Checklist

- [ ] AlertManager pods running with new configuration
- [ ] Discord webhook secret loaded successfully  
- [ ] Test Discord notification received
- [ ] VyOS accepting syslog on port 514
- [ ] Log rotation configured (30-day retention)
- [ ] Firewall rules allowing syslog traffic
- [ ] Weekend suppression working (test on weekend)

## Monitoring

**AlertManager Status:**
```bash
# Check AlertManager config
kubectl exec -n prometheus alertmanager-prometheus-kube-prometheus-alertmanager-0 -- amtool config show

# Check active alerts
kubectl port-forward -n prometheus svc/alertmanager-operated 9093:9093
# Visit http://localhost:9093
```

**VyOS Storage Monitoring:**
```bash
# Check log directory size
ssh vyos@192.168.0.1 'du -sh /var/log/kubernetes*'

# Monitor storage usage
ssh vyos@192.168.0.1 'df -h | grep -E "(Filesystem|/var)"'
```

## Rollback Plan

If issues occur:

```bash
# Disable the new AlertManager config application
kubectl patch application prometheus-stack-config -n argo-cd --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}'

# Revert to previous AlertManager configuration
kubectl delete application prometheus-stack-config -n argo-cd

# Remove VyOS syslog configuration
ssh vyos@192.168.0.1
configure
delete system syslog host 0.0.0.0
delete firewall name LAN_LOCAL rule 100
commit
save
```

## Success Criteria

✅ Discord notifications received within 5 minutes of alerts  
✅ VyOS buffering logs from Kubernetes cluster  
✅ 20-30GB storage allocation on VyOS  
✅ Weekend suppression active for non-critical alerts  
✅ Zero downtime during deployment  

## Next Steps

Once Phase 1 is verified and stable:
- Proceed to Phase 2: Azure Log Analytics + Loki deployment
- Monitor VyOS CPU usage for 1 week before adding Loki
- Gather feedback on Discord notification format/timing