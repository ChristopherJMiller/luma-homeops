# Homelab Monitoring Implementation Plan

## Overview
Implementation of comprehensive monitoring and alerting for Kubernetes cluster on Talos Linux with VyOS router integration.

## Current Infrastructure Analysis
- **Prometheus**: 200Gi storage, 2 replicas, 3Gi RAM each
- **AlertManager**: 5Gi storage, **NO RETENTION POLICY SET** (using defaults: likely 120h/5 days)
- **Grafana**: Available at dashboard.chrismiller.xyz
- **VyOS Router**: 100GB free storage, 7Gi free RAM
- **No UPS**: Skip power monitoring components

## Architecture Decision: Hybrid Approach
**VyOS Hardware**: Intel Celeron J6412 @ 2.0GHz (4 cores), 8Gi RAM (7Gi free)
- **Good for**: Log buffering, syslog aggregation, basic monitoring
- **Marginal for**: Loki (CPU-light but memory-intensive indexing)
- **Avoid**: NetFlow/sFlow analysis (too CPU intensive)

Architecture:
1. **VyOS** as log buffer (30 days with 20-30GB allocation) ✅ **RECOMMENDED**
2. **Azure Log Analytics** for long-term storage (90-day retention, cost-effective) ✅ **RECOMMENDED**  
3. **Loki on VyOS** - **SKIP** (4-core Celeron will struggle with log indexing under load)

## Implementation Phases

### Phase 1: Critical Alerting (Week 1) - ZERO DOWNTIME
**Goal**: Get immediate notifications when things break

#### 1.1 Configure AlertManager with Discord
- Add Discord webhook configuration
- Set up weekend suppression rules
- Configure alert routing by severity

#### 1.2 VyOS Log Buffer Setup
- Configure VyOS syslog receiver
- Set up log rotation (20-30GB limit, 30-day retention)
- Test emergency log collection

**Files to create/modify:**
- `cluster/prometheus-stack/alertmanager-config.yaml`
- `router/syslog-setup.sh`

### Phase 2: Log Aggregation (Week 2) - ZERO DOWNTIME
**Goal**: Collect and store logs for troubleshooting

#### 2.1 Azure Log Analytics (Terraform)
- Create Azure workspace
- Configure 90-day retention policy
- Set up cost controls and daily ingestion limits

#### 2.2 FluentBit DaemonSet
- Deploy log collectors to all nodes
- Configure multi-destination output
- Implement failover logic

**Files to create:**
- `terraform/azure-logging/`
- `cluster/logging/fluentbit.yaml`

### Phase 3: Enhanced Monitoring (Week 3) - CAREFUL DEPLOYMENT
**Goal**: Monitor all critical components

#### 3.1 Critical Alert Rules
- Certificate expiration (21 days warning, 7 days critical)
- Rook Ceph health and capacity
- Node resource exhaustion
- ArgoCD sync failures

#### 3.2 VyOS Router Monitoring
- SNMP exporter setup (low CPU impact)
- Network interface monitoring (bandwidth, errors)
- **Skip NetFlow/sFlow** (Celeron J6412 insufficient for flow analysis)

**Files to create:**
- `cluster/prometheus-stack/alert-rules.yaml`
- `cluster/monitoring/vyos-monitoring.yaml`

### Phase 4: Dashboards & Automation (Week 4)
**Goal**: Visibility and maintenance automation

#### 4.1 Grafana Dashboards
- Import ArgoCD dashboard (ID: 14584)
- Create homelab overview dashboard
- Set up log exploration views

#### 4.2 Maintenance Automation
- Monthly maintenance scripts
- Backup verification
- Update tracking for Renovate

**Files to create:**
- `scripts/monthly-maintenance.sh`
- `grafana/dashboards/homelab-overview.json`

## Risk Mitigation
1. **No downtime deployments**: All new components deployed alongside existing
2. **Gradual rollout**: Test each phase in non-production namespace first
3. **Rollback plan**: Keep original configurations until validated
4. **Resource monitoring**: Watch VyOS performance during Loki deployment

## Success Metrics
- [ ] Receive Discord alerts within 5 minutes of issues
- [ ] 30-day log retention on VyOS (20-30GB storage)
- [ ] 90-day log retention in Azure
- [ ] All critical services monitored
- [ ] Monthly maintenance reports automated

## Additional Research Report Integrations

### From Research Report - Missing Components:
1. **Hardware Monitoring**: SMART disk health monitoring (add to Phase 3)
2. **Network Flow Monitoring**: NetFlow/sFlow from VyOS (Phase 3 - if CPU allows)
3. **Backup Monitoring**: Automated backup verification scripts (Phase 4)
4. **Security Monitoring**: Failed authentication alerts (Phase 3)
5. **Circular Dependency Prevention**: External Matrix homeserver option (Phase 1 alternative)

### Recommended Additions to Implementation:
- **Phase 1 Enhancement**: Add Matrix notifications as Discord backup
- **Phase 3 Addition**: Deploy node-exporter with SMART monitoring
- **Phase 3 Addition**: ~~VyOS NetFlow monitoring~~ **SKIP** (Celeron J6412 insufficient)
- **Phase 4 Addition**: Backup verification automation
- **Phase 4 Addition**: Security event monitoring (failed logins, cert issues)
- **Phase 2 Alternative**: Consider lightweight Loki deployment on dedicated Kubernetes node instead of VyOS

## Next Steps
Ready to start Phase 1 with AlertManager Discord configuration?