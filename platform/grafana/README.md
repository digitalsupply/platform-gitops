# Grafana Operator（Stage A）

> 目标：不替换现有 Helm Grafana 的 Deployment/PVC，只用 Operator 接管 Datasource/Dashboard。

## 结构

```text
platform/grafana/
├── operator/
│   ├── namespace.yaml
│   └── operator.yaml          # Grafana Operator v5.25.0（Helm 渲染）
└── resources/
    ├── grafana.yaml           # external 模式，指向现有 Service
    ├── datasources.yaml       # 4 个 GrafanaDatasource
    └── dashboards.yaml        # 37 个 GrafanaDashboard（configMapRef）
```

## 存储（2026-11-09 迁移）

Grafana 数据盘仍由 Helm 管理（`grafana.persistence`），不属于本 App 跟踪范围：

- 新 PVC：`monitoring/prometheus-grafana-ceph`（10Gi，`ceph-rbd-sc`）
- 旧 PVC：`monitoring/prometheus-grafana`（NFS，已于 2026-11-09 验证后清理）
- Helm values：`platform/monitoring/values.yaml`

ArgoCD 只管理 external 模式的 `Grafana` CR，不会回滚 Grafana 的 Deployment/PVC。

## 关键配置

- Operator namespace：`grafana-operator`
- Grafana CR：`monitoring/prometheus-grafana`
- external URL：`http://prometheus-grafana.monitoring.svc:80`
- admin Secret：`monitoring/prometheus-grafana`（`admin-user` / `admin-password`）
- Dashboard 来源：现有 30 个 `grafana_dashboard=1` ConfigMap 的 37 个 key
- Helm sidecar：`grafana.sidecar.dashboards.enabled=false`、`grafana.sidecar.datasources.enabled=false`

## 从 sidecar 迁移时的两个坑

旧 sidecar provisioning 的资源会在 Grafana SQLite 里留下标志，导致 Operator 无法接管：

1. **Datasource** `readOnly=true` → PUT 403 `Cannot update read-only data source`
2. **Dashboard** 在 `dashboard_provisioning` 表里有记录 → DELETE 400 `provisioned dashboard cannot be deleted`

处理方式（维护窗口，先备份 `grafana.db`）：

```sql
UPDATE data_source SET read_only = 0;
DELETE FROM dashboard_provisioning;
```

然后重启 Grafana，Operator 会自动 reconcile。

## 回滚

```bash
kubectl -n argocd delete application platform-grafana platform-grafana-operator
helm -n monitoring upgrade prometheus prometheus-community/kube-prometheus-stack \
  --version 83.7.0 --reuse-values \
  --set grafana.sidecar.dashboards.enabled=true \
  --set grafana.sidecar.datasources.enabled=true
```

## Ingress / ServiceMonitor / prune

- `resources/ingress.yaml`：Adopt 现有 Traefik IngressRoute
  - `grafana.k8s.local` / `grafana.example.local`
  - HTTPS 使用 Traefik 默认自签证书（`tls: {}`）
  - HTTP 入口 301 到 HTTPS
- `operator/servicemonitor.yaml`：采集 Grafana Operator 自身指标
  - label `release: prometheus`
  - target `grafana-operator-metrics-service`，health `up`
- `platform-grafana` 已开启 `prune: true`
  - `Grafana` 连接根资源带 `Prune=false`，避免误删
  - 用户自建 Dashboard 不在 ArgoCD 跟踪范围

Grafana 自身通过 Helm values 设置了：

```text
GF_SERVER_ROOT_URL=https://grafana.k8s.local/
GF_SERVER_DOMAIN=grafana.k8s.local
```

## Stage B（未做）

用 internal 模式的 `Grafana` CR 替换 Helm Grafana（Deployment/PVC/grafana.ini），需要单独维护窗口。
