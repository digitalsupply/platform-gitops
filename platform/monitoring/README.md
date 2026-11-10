# 监控栈（kube-prometheus-stack）运维说明

> 最后更新：2026-11-09
> 版本：kube-prometheus-stack 83.7.0
> 命名空间：`monitoring`
> Helm release：`prometheus`

## 1. 当前状态

`monitoring` 命名空间由 **Helm** 的 `kube-prometheus-stack` 管理，**不是 ArgoCD 管理**。
因此 ArgoCD 的 `selfHeal` 不会回滚 Prometheus/Alertmanager 的 storage 配置。

**例外：PrometheusRule 已纳入 GitOps。** `monitoring` 下 6 个自建规则由 ArgoCD
Application `platform-monitoring-rules` 管理，源目录 `platform/monitoring/rules/`
（2026-11-09 迁移）。Helm chart 自带的 35 个规则仍随 chart 版本发布，不在该目录中。

ArgoCD 只管理 `platform-grafana` App，它对应 external 模式的 `Grafana` CR
（`platform/grafana/resources/grafana.yaml`），不包含 Deployment/PVC/StorageClass 字段。

2026-11-09 已完成数据盘从 NFS 到 Ceph RBD 的迁移：

| 组件 | 新 PVC | 容量 | StorageClass | 旧 NFS PVC（已清理） |
|---|---|---:|---|---|
| Prometheus | `prometheus-db-ceph-prometheus-prometheus-prometheus-0` | 100Gi | `ceph-rbd-sc` | `prometheus-prometheus-prometheus-db-prometheus-prometheus-prometheus-0`（已删除） |
| Alertmanager | `alertmanager-db-ceph-alertmanager-prometheus-alertmanager-0` | 20Gi | `ceph-rbd-sc` | `alertmanager-prometheus-alertmanager-db-alertmanager-prometheus-alertmanager-0`（已删除） |
| Grafana | `prometheus-grafana-ceph` | 10Gi | `ceph-rbd-sc` | `prometheus-grafana`（已删除） |

关键配置：

- Prometheus retention：`15d` / `25GiB`
- Prometheus `securityContext.fsGroupChangePolicy: OnRootMismatch`，避免 kubelet 对 19G+ TSDB 递归 chown
- Ceph 集群：`10.0.0.31/41/51:6789`，pool `k8s-rbd`，StorageClass `ceph-rbd-sc`（`Retain`）
- 旧监控 NFS PVC 已于 2026-11-09 验证后清理，NFS 上只保留业务 PVC

## 2. 标准升级 / 变更方式

`platform/monitoring/values.yaml` 是存储、保留策略和 Alertmanager 路由的 source of truth。

```bash
helm -n monitoring upgrade prometheus prometheus-community/kube-prometheus-stack \
  --version 83.7.0 \
  -f platform/monitoring/values.yaml \
  --reuse-values
```

必须带 `--reuse-values`，否则未写入 Git 的 `grafana.adminPassword` 会被重置。

升级后检查：

```bash
kubectl -n monitoring get pods -l 'app.kubernetes.io/name in (prometheus,alertmanager,grafana)'
kubectl -n monitoring get pvc | grep -E 'ceph|nfs-sc'
kubectl -n monitoring get prometheus prometheus-prometheus \
  -o jsonpath='{.spec.storage.volumeClaimTemplate.metadata.name}{" "}{.spec.storage.volumeClaimTemplate.spec.storageClassName}{"\n"}'
kubectl -n monitoring get alertmanager prometheus-alertmanager \
  -o jsonpath='{.spec.storage.volumeClaimTemplate.metadata.name}{" "}{.spec.storage.volumeClaimTemplate.spec.storageClassName}{"\n"}'
```

> 修改 `volumeClaimTemplate.metadata.name` 会触发 StatefulSet 使用新的 PVC 名称，
> 不会自动迁移旧数据。必须提前创建/拷贝 PVC，详见第 4 节。

## 3. 日常健康检查

```bash
# 组件状态
kubectl -n monitoring get pods -o wide | grep -E 'prometheus-prometheus-0|alertmanager-prometheus|prometheus-grafana'

# Prometheus
kubectl -n monitoring run prom-check --rm -i --restart=Never \
  --image=curlimages/curl:8.10.1 --command -- \
  sh -c 'curl -s -o /dev/null -w "ready=%{http_code}\n" http://prometheus-prometheus.monitoring:9090/-/ready'

# Grafana（admin 账号在 secret prometheus-grafana）
kubectl -n monitoring get secret prometheus-grafana \
  -o jsonpath='{.data.admin-user}' | base64 -d; echo
kubectl -n monitoring get secret prometheus-grafana \
  -o jsonpath='{.data.admin-password}' | base64 -d; echo

# Ceph 容量（重点看 MAX AVAIL，不要只看 HEALTH_OK）
kubectl -n ceph-csi-rbd logs deploy/ceph-csi-rbd-provisioner -c csi-rbdplugin --tail=20
```

Ceph 侧建议在能访问 mon 的机器上执行：

```bash
ceph -s
ceph df
ceph df detail | grep -E 'POOL|k8s-rbd'
```

**注意：** `ceph df` 的 `MAX AVAIL` 取所有 OSD 中最紧张的那个。
即使集群总剩余 47GiB，只要有一个 OSD 100% full，`k8s-rbd` 也会显示 `0 B`，
所有写入和 RBD map 都会失败。此时先清理孤儿镜像/扩容 OSD，不要反复重试挂载。

## 4. PVC 迁移手册（NFS → Ceph）

以 Prometheus 为例（Alertmanager 同理，容量改为 20Gi）：

### 4.1 记录目标 PVC 名称

`volumeClaimTemplate.metadata.name` 不是最终 PVC 名。Operator 会拼接：

```text
<pvc-template-name>-prometheus-<prometheus-name>-0
< pvc-template-name>-alertmanager-<alertmanager-name>-0
```

本环境：

```text
prometheus-db-ceph + prometheus-prometheus  -> prometheus-db-ceph-prometheus-prometheus-prometheus-0
alertmanager-db-ceph + prometheus-alertmanager -> alertmanager-db-ceph-alertmanager-prometheus-alertmanager-0
```

名字算错时 Operator 会额外创建一个空 PVC，Prometheus 会以空数据启动，务必先确认。

### 4.2 停写并拷贝

```bash
# 1) 缩到 0，确认 Pod 退出
kubectl -n monitoring patch prometheus prometheus-prometheus --type merge -p '{"spec":{"replicas":0}}'
kubectl -n monitoring get pods -l app.kubernetes.io/name=prometheus

# 2) 创建目标 PVC（容量与旧盘一致或更大）
#    也可以用 volumeClaimTemplate 让 Operator 创建，但迁移场景建议手工创建并先拷数据。

# 3) helper Pod 同时挂载旧 NFS PVC 和新 Ceph PVC
#    - 用 tar 流式拷贝，避免 cp 在大量小文件上过慢
#    - 目标根目录 chown 1000:2000 + chmod 2777（setgid），配合 OnRootMismatch
#    - 不要在 sync/chown 未完成时 force delete helper
```

### 4.3 切换 CR / Helm values

```bash
kubectl -n monitoring patch prometheus prometheus-prometheus --type merge -p '{
  "spec": {
    "replicas": 1,
    "storage": {
      "volumeClaimTemplate": {
        "metadata": {"name": "prometheus-db-ceph"},
        "spec": {
          "storageClassName": "ceph-rbd-sc",
          "accessModes": ["ReadWriteOnce"],
          "resources": {"requests": {"storage": "100Gi"}}
        }
      }
    }
  }
}'
```

随后务必同步 Helm values（否则下一次 `helm upgrade` 会改回 `nfs-sc`）：

```bash
helm -n monitoring upgrade prometheus prometheus-community/kube-prometheus-stack \
  --version 83.7.0 -f platform/monitoring/values.yaml --reuse-values
```

### 4.4 验证

```bash
kubectl -n monitoring get pod prometheus-prometheus-prometheus-0 -o wide
kubectl -n monitoring get pvc prometheus-db-ceph-prometheus-prometheus-prometheus-0
```

Prometheus 日志应出现 `Server is ready to receive web requests.`，
`/-/ready` 返回 200，且历史数据可查（用旧时间点执行 query_range）。

## 5. 回滚说明（NFS 数据已清理）

2026-11-09 验证完成后，三个旧监控 NFS PVC 已删除，**NFS 回滚路径不再可用**。
下面的命令仅作参考；执行后会创建新的空 NFS PVC，不会恢复旧数据。

```bash
# 仅作参考：旧 NFS 数据已删除
helm -n monitoring upgrade prometheus prometheus-community/kube-prometheus-stack \
  --version 83.7.0 --reuse-values \
  --set prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.metadata.name=prometheus-db \
  --set prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.storageClassName=nfs-sc \
  --set alertmanager.alertmanagerSpec.storage.volumeClaimTemplate.metadata.name=alertmanager-db \
  --set alertmanager.alertmanagerSpec.storage.volumeClaimTemplate.spec.storageClassName=nfs-sc \
  --set grafana.persistence.existingClaim=prometheus-grafana \
  --set grafana.persistence.storageClassName=nfs-sc
```

如果确实需要恢复：

- Grafana：用 `k8s-postmortem/backup/grafana.db` + Dashboard JSON；
- Prometheus：从 TSDB 快照/备份恢复；
- Alertmanager：配置在 Helm values/Git 中，silences 可重建。

> 当前 Ceph 是监控数据的唯一副本，建议尽快为 `k8s-rbd` 增加快照/备份策略。

## 6. 已知坑与处理

### 6.1 递归 fsGroup chown

**现象：** Pod 长时间 `Init:0/1`，kubelet 在递归 chown 19G TSDB；严重时节点 I/O D 状态、
kubelet 无响应，只能重启节点/VM。

**处理：**

- Prometheus CR 设置 `securityContext.fsGroupChangePolicy: OnRootMismatch`
- 确保 PVC 根目录 group 已是 2000（`chown 1000:2000` + `chmod 2777`）
- 不要在拷贝 helper 未退出时启动 Prometheus

### 6.2 Ceph 池满 / 单个 OSD full

**现象：**

```text
HEALTH_ERR 1 full osd(s); 15 pool(s) full
k8s-rbd  USED 127 GiB  MAX AVAIL 0 B
```

但 `ceph -s` 显示集群还有几十 GiB。原因是某个 OSD 先满，`MAX AVAIL` 被它拉成 0。

**处理：**

1. `rbd ls -l k8s-rbd` 列出镜像
2. 与 `kubectl get pv` 的 `spec.csi.volumeHandle` 对比，找出没有 PV 引用的孤儿镜像
3. `rbd status` 确认 `Watchers: none` 后 `rbd rm`
4. 确认 `MAX AVAIL` 恢复为正值，再重试挂载

本次迁移中删除了 5 个孤儿镜像（含一个算错名字、已拷入 19.3G 数据的 100Gi 镜像），
`k8s-rbd` 从 100% 降到约 48%，`MAX AVAIL` 恢复到 25GiB。

### 6.3 WAL segments are not sequential

**现象：**

```text
opening storage failed: get segment range: segments are not sequential
```

**原因：** 拷贝期间 Prometheus 未完全停写，或 helper 被 force delete，导致 WAL 段缺失/
多出 `00000000`。

**处理：**

- 优先从旧 PVC 重新 `cp -a` 整个 `wal/` 目录
- 删除失败启动产生的 `00000000` 段
- 若无法恢复，删除/移走 `wal/`，Prometheus 会保留已落盘 block，只丢失未压缩窗口的数据

### 6.4 RWO 卷跨节点挂载

**现象：**

```text
FailedAttachVolume ... Volume is already exclusively attached to one node
```

**处理：**

- 确认旧 Pod 已真正退出（`kubectl get pod -o wide`，必要时 force delete）
- 检查 `kubectl get volumeattachment | grep <pv>`
- 确认节点 kubelet 没有残留 RBD map（`rbd showmapped` / `mount | grep pvc-`）

### 6.5 Grafana Operator Dashboard 同步失败

**现象：** 37 个 `GrafanaDashboard` 的 `DashboardSynchronized=False`，ArgoCD `platform-grafana` Degraded。

**原因：** Grafana Pod 重启期间 Operator 连不上 `prometheus-grafana.monitoring.svc:80`，
报 `dial tcp ... connect: operation not permitted`。

**处理：** Grafana 恢复后重启 Operator 强制重新 reconcile：

```bash
kubectl -n grafana-operator rollout restart deploy/grafana-operator
kubectl -n monitoring get grafanadashboard -o json | \
  python3 -c "import sys,json,collections; d=json.load(sys.stdin); c=collections.Counter(); [c.update([(x['type'],x['status'])]) for i in d['items'] for x in (i.get('status',{}).get('conditions') or [])]; print(c)"
```

---

## 7. PrometheusRule 管理（ArgoCD）

`monitoring` 下 6 个自建规则已从手工 `kubectl apply` 迁移到 GitOps：

| 文件 | 规则 | 告警数 |
|---|---|---:|
| `rules/app-monitoring-alerts.yaml` | cert-manager / tekton / traefik / kafka / storage / cnpg | 9 |
| `rules/bsm-coverage-alerts.yaml` | bsm.availability / bsm.coverage | 12 |
| `rules/ceph-cluster-alerts.yaml` | ceph.health / mon / osd / pg / capacity | 19 |
| `rules/cluster-wide-alerts.yaml` | pod / node / otel-demo / workload / cache | 15 |
| `rules/coverage-gap-alerts.yaml` | logging / cni / lb / gitops / default-workloads | 10 |
| `rules/tempo3-alerts.yaml` | tempo3.availability / intake / query / storage / pipeline | 20 |

变更流程：

```bash
# 1. 编辑 platform/monitoring/rules/*.yaml 并提交
# 2. 本地校验（可选）
kubectl apply --dry-run=server -f platform/monitoring/rules/

# 3. 推送 main 后由 ArgoCD 自动同步
kubectl -n argocd get app platform-monitoring-rules
```

注意事项：

- 每个 PrometheusRule 必须保留 `release: prometheus` 标签，否则 Prometheus
  的 `ruleSelector` 不会发现它；
- 只改 `platform/monitoring/rules/`，不要在该 Application 中加入 chart 生成的规则；
- `coverage-gap-alerts` 中重复的 `MetalLBSpeakerNotReady` 已移除，保留
  `bsm-coverage-alerts` 中的定义；
- 团队规则（`deeptutor`、`compassio`）仍由各自仓库的 ArgoCD App 管理。

---

## 8. Alertmanager 通知链路（n8n + 企业微信）

路由（`values.yaml` 的 `alertmanager.config`）：

| 级别 | n8n | 企业微信 |
|---|---|---|
| critical / warning | ✅ | ✅ |
| info / none | ✅ | ❌ |

- `n8n-webhook` 是默认 receiver，所有告警都会到；
- `wecom` receiver 指向 `http://wecom-adapter.monitoring.svc:8080/`，
  只接 `critical|warning`，第一个路由带 `continue: true`，因此两路并行；
- `wecom-adapter` 把 Alertmanager webhook 负载转成企业微信群机器人 markdown，
  webhook key 在 Secret `wecom-adapter` 的 `webhook-url`；
- 适配器限流 3s/条、单条最多 10 条告警、内容 4096 字节截断。

**关键坑（2026-11-09 修复）：** `wecom-adapter` 的 NetworkPolicy 原来只写了
`ipBlock: 0.0.0.0/0` 放行 53/443。在 Cilium 下这能放行外部 DNS（8.8.8.8），
但 **不能放行到 kube-dns ClusterIP 后端的流量**，导致适配器所有域名解析超时
（`URLError: [Errno -3] Try again`），企业微信链路一直不可用。修复方式是显式放行
到 `kube-system/k8s-app=kube-dns` Pod 的 53/UDP、53/TCP：

```yaml
egress:
  - to:
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: kube-system
        podSelector:
          matchLabels:
            k8s-app: kube-dns
    ports:
      - {port: 53, protocol: UDP}
      - {port: 53, protocol: TCP}
```

**二期已完成（2026-11-09）：** `wecom-adapter` 的 Deployment/Service/ConfigMap/
NetworkPolicy 已迁入 `platform/monitoring/wecom/`，由 ArgoCD Application
`platform-monitoring-wecom` 管理；webhook URL 保存在 `sealedsecret.yaml`
（SealedSecret，scope 为 `monitoring/wecom-adapter`）。

Secret 更新流程：

```bash
kubectl -n monitoring get secret wecom-adapter -o yaml \
  | kubeseal --format yaml \
      --controller-name sealed-secrets-controller \
      --controller-namespace kube-system \
  > platform/monitoring/wecom/sealedsecret.yaml
# 提交并同步后，重启 Deployment 让 env 重新注入
kubectl -n monitoring rollout restart deploy/wecom-adapter
```

## 9. 团队 AlertmanagerConfig 约定

Alertmanager CR 的 `alertmanagerConfigSelector` / `alertmanagerConfigNamespaceSelector`
都是 `{}`，因此任意命名空间的 `AlertmanagerConfig` 都会被合并。

基础路由（`n8n-webhook`、`wecom`）都设置了 `continue: true`，团队路由可以追加在
后面而不会被提前截断。示例：

```yaml
apiVersion: monitoring.coreos.com/v1alpha1
kind: AlertmanagerConfig
metadata:
  name: team-routes
  namespace: deeptutor
spec:
  route:
    receiver: team-webhook
    matchers:
      - name: namespace
        value: deeptutor
    continue: true
  receivers:
    - name: team-webhook
      webhookConfigs:
        - url: https://example.invalid/team
          sendResolved: true
```

约定：

- 团队自己的 `AlertmanagerConfig` 放在各自 GitOps 仓库的命名空间下；
- 匹配条件必须包含 `namespace`，避免误接其他团队的告警；
- `continue: true` 表示团队通道之外，基础 n8n / 企业微信通道仍会收到；
- 如果团队只想要自己的通道、不要基础通道，需要单独设计路由，不要直接改平台基础路由。

## 10. GitOps 白名单：只加载 Git 管理的规则和配置

为避免手工 `kubectl apply` 的规则/配置被 Prometheus/Alertmanager 误加载，
所有 GitOps 管理的对象必须带：

```yaml
metadata:
  labels:
    platform.gitops/managed: "true"
```

覆盖方式：

| 对象 | 标签来源 |
|---|---|
| chart 自带 35 个 PrometheusRule | `values.yaml` 的 `defaultRules.labels` |
| `platform/monitoring/rules/` 6 个自建规则 | `rules/kustomization.yaml` 里的 JSON6902 patch |
| deeptutor 2 个规则 | `deeptutor-gitops/overlays/prod` 的 patch |
| compassio 1 个规则 | `compassio-gitops/overlays/prod` 的 patch |
| 团队 `AlertmanagerConfig` | 必须手工写在 `metadata.labels` 里 |

2026-11-10 已完成 selector 收紧。Prometheus 的 `ruleSelector` 和
Alertmanager 的 `alertmanagerConfigSelector` 当前为：

```yaml
matchLabels:
  release: prometheus
  platform.gitops/managed: "true"
```

效果：

- 手工创建、**没有** `platform.gitops/managed=true` 的 PrometheusRule 不会被加载；
- 手工创建、**没有**该标签的 AlertmanagerConfig 不会被合并；
- 该标签不是安全边界，拥有写权限的人仍可伪造；真正的强制需要配合
  RBAC / Kyverno / OPA 等准入策略。

验证：

```bash
# 44 = 35 chart + 6 platform + 2 deeptutor + 1 compassio
kubectl get prometheusrules -A -o json \
  | jq '[.items[] | select(.metadata.labels["platform.gitops/managed"]=="true")]|length'

# 应返回 44
```

## 11. CRD 管理（GitOps）

prometheus-operator 的 10 个 CRD 已固化到：

```text
platform/monitoring/crds/
├── kustomization.yaml
├── alertmanagerconfigs.monitoring.coreos.com.yaml
├── alertmanagers.monitoring.coreos.com.yaml
├── podmonitors.monitoring.coreos.com.yaml
├── probes.monitoring.coreos.com.yaml
├── prometheusagents.monitoring.coreos.com.yaml
├── prometheuses.monitoring.coreos.com.yaml
├── prometheusrules.monitoring.coreos.com.yaml
├── scrapeconfigs.monitoring.coreos.com.yaml
├── servicemonitors.monitoring.coreos.com.yaml
└── thanosrulers.monitoring.coreos.com.yaml
```

由 ArgoCD Application `platform-monitoring-crds` 管理：

- `sync-wave: "-10"`，先于 `platform-monitoring-stack`（wave 10）；
- `prune: false`，ArgoCD 不会删除 CRD；
- `ServerSideApply=true` + `Force=true`，用于接管历史手工 apply 的字段；
- 主监控栈保持 `skipCrds: true`，避免 CRD 被两个 App 重复管理。

升级 CRD：

```bash
helm show crds prometheus-community/kube-prometheus-stack --version <新版本> \
  > /tmp/kps-crds.yaml

# 按 CRD 名拆分覆盖 platform/monitoring/crds/
# 提交推送后，platform-monitoring-crds 自动同步
kubectl -n argocd get app platform-monitoring-crds
```

验证：

```bash
kubectl get crd -o json | jq -r '.items[]
  | select(.metadata.name|test("monitoring.coreos.com$"))
  | [.metadata.name, .metadata.annotations["operator.prometheus.io/version"]]
  | @tsv'
# 所有 CRD 应与运行的 operator 版本一致（当前 0.90.1）
```

## 附：历史方案（未执行）

以下内容是最初计划的“把 monitoring 整体迁到 ArgoCD”方案，仅作记录。
本次没有执行，因为需要关闭 Helm 子 chart 并重建 Prometheus/Alertmanager，
风险高于收益；当前采用 Helm + values 文件管理，ArgoCD 不接管 monitoring。

- 关闭 `prometheus.enabled` / `alertmanager.enabled`
- 用 GitOps 创建同名 CR
- 新增 `platform-monitoring` Application
- 维护窗口内切换，观察告警链路

---

## 8. Prometheus Adapter（Compassio HPA 自定义指标）

> Chart：`prometheus-adapter` 5.3.0（已 vendor 到 `platform/monitoring/prometheus-adapter-chart/`）  
> Values：`platform/monitoring/prometheus-adapter-values.yaml`  
> 命名空间：`monitoring`

### 8.1 用途

把 Compassio 的 LLM 等待指标暴露成 external metric：

```text
compassio_next_llm_wait_p95
```

HPA 用它判断“学生是否真的在等 LLM”，而不是只看 CPU。

### 8.2 为什么 vendor chart 并改 probes

上游 chart 默认的 liveness/readiness probe 是：

```yaml
httpGet:
  path: /healthz
  port: https
  scheme: HTTPS
```

adapter 自签证书只覆盖 `localhost/127.0.0.1`，kubelet 从 Pod IP 探测会 TLS 校验失败，
导致 CrashLoopBackOff。vendored chart 已把两个 probe 改成：

```yaml
tcpSocket:
  port: https
```

### 8.3 安装/升级

```bash
helm -n monitoring upgrade --install prometheus-adapter \
  platform/monitoring/prometheus-adapter-chart \
  -f platform/monitoring/prometheus-adapter-values.yaml \
  --wait
```

adapter 固定在 `worker02`（该节点镜像拉取正常），单副本。

### 8.4 验证

```bash
kubectl get apiservice | grep metrics.k8s.io
kubectl get --raw \
  /apis/external.metrics.k8s.io/v1beta1/namespaces/compassio/compassio_next_llm_wait_p95
```

预期返回 `value: "0"`（没有人等待 LLM 时为 0）。
