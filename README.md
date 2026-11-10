# P0 GitOps 落地骨架（CNPG 示例）

> 目标：把 CNPG 数据库栈从“手动 kubectl”迁到 GitLab + ArgoCD。
> 原则：**GitOps 管“期望状态”，不管“运行时状态”；Operator 管“子资源”。**

---

## 1. 先回答：是不是 `kubectl get -o yaml` 传到 GitLab？

**不是。**

直接导出会带进大量运行时字段，导致 ArgoCD 永久 OutOfSync 或同步失败：

```text
status
metadata.uid / resourceVersion / generation / creationTimestamp / managedFields
Service.spec.clusterIP / clusterIPs / ipFamilies / nodePort
PVC.spec.volumeName
Deployment.spec.template.metadata.creationTimestamp
metadata.annotations.kubectl.kubernetes.io/last-applied-configuration
Secret.data（明文）
Operator 回填的默认值（CNPG 的 enablePDB / probes / wal_level 等）
```

正确顺序：

1. **能拿官方源就用官方源**：Operator/CRD 用官方 release manifest 或 Helm chart；
2. **拿不到官方源再提取**：只提取“用户意图”资源（CR、Deployment、Service、ConfigMap），并用脚本清洗；
3. **不要提交 Operator 生成的子资源**：CNPG 的 Pod/PVC、Tekton 的 Deployment、Prometheus 的 StatefulSet 都不要提交，提交它们的 **CR**；
4. **Secret 不进 Git**：用 Sealed Secrets / External Secrets / SOPS；
5. **用 ArgoCD Application + app-of-apps 指过来**，配 sync-wave、prune、ignoreDifferences。

---

## 2. 本骨架包含什么

```text
gitops-p0/
├── README.md
├── clusters/prod/
│   ├── app-of-apps.yaml                     # 总入口
│   └── applications/
│       ├── platform-cnpg-operator.yaml      # wave -10
│       ├── platform-cnpg-plugin.yaml        # wave -5
│       └── platform-cnpg-cluster.yaml       # wave 0
├── platform/cnpg/
│   ├── operator/cnpg-1.30.1.yaml            # 官方 manifest
│   ├── plugin/plugin-barman-cloud-0.15.1.yaml
│   └── cluster/
│       ├── cluster.yaml                     # 清洗后的 Cluster CR
│       ├── objectstore.yaml
│       └── scheduledbackup.yaml
└── scripts/
    └── clean-k8s-yaml.py                    # 清洗运行时字段
```

---

## 3. 关键设计

### 3.1 为什么拆成 3 个 Application

CRD/Operator 必须先于 CR 就绪；插件必须先于 Cluster 的 `plugins` 字段生效。用 sync-wave：

| Application | wave | 内容 |
|---|---:|---|
| `platform-cnpg-operator` | -10 | CRD + Operator |
| `platform-cnpg-plugin` | -5 | Barman Cloud Plugin + CRD |
| `platform-cnpg-cluster` | 0 | Cluster + ObjectStore + ScheduledBackup |

### 3.2 为什么 `prune: false`

CNPG 的 `Cluster` 删除会级联删除 PVC（数据丢失）。数据库、Kafka、ObjectStore 这类有状态 CR 必须 `prune: false`；删除要走单独的人工流程。

### 3.3 为什么要 `ignoreDifferences`

CNPG Operator 会向 `Cluster.spec` 回填默认值（`enablePDB`、`probes`、`replicationSlots.synchronizeReplicas` 等）。不加忽略，ArgoCD 会永远显示 OutOfSync。

本骨架忽略：

```yaml
ignoreDifferences:
  - group: postgresql.cnpg.io
    kind: Cluster
    jsonPointers:
      - /spec/enablePDB
      - /spec/probes
      - /spec/replicationSlots/synchronizeReplicas
      - /status
```

> `postgresql.parameters` 没有整体忽略，而是把当前参数完整写进 Git，避免“忽略后改参数不生效”。

---

## 4. 清洗脚本用法

```bash
# 单个资源
kubectl -n cnpg-system get cluster pg16-ceph -o yaml | \
  python3 scripts/clean-k8s-yaml.py > /tmp/cluster.clean.yaml

# 多资源
kubectl -n cicd get pipeline,task,eventlistener -o yaml | \
  python3 scripts/clean-k8s-yaml.py > /tmp/cicd.clean.yaml

# Secret 默认拒绝，确认要提交 SealedSecret 时再加 --allow-secret
```

脚本会删除：`status`、`uid`、`resourceVersion`、`generation`、`creationTimestamp`、`managedFields`、`ownerReferences`、`finalizers`、last-applied、revision、tracking-id、Service 的 ClusterIP/nodePort、PVC 的 volumeName 等。

> **注意**：脚本只去“运行时字段”，不会去 Operator 回填的默认值。
> CNPG 的 `enablePDB`、`probes`、`replicationSlots`，Strimzi 的默认值等仍会保留。
> 这类 CR 请使用本仓库里**手写的最小 spec**（如 `platform/cnpg/cluster/cluster.yaml`），不要直接拿脚本输出入库。

### 首次接管已有资源

当前资源是 `kubectl apply`（client-side）创建的，ArgoCD 第一次 `ServerSideApply` 会报字段冲突。二选一：

```bash
# 方式 A：先手动做一次 adoption，再交给 ArgoCD
kubectl apply --server-side --force-conflicts -f platform/cnpg/cluster/

# 方式 B：ArgoCD 首次 sync 时加 force（UI 勾选 FORCE，或 CLI --force）
argocd app sync platform-cnpg-cluster --force
```

之后再保持 ArgoCD 自动同步即可。

---

## 5. 推到 GitLab

```bash
cd gitops-p0
git init
git add .
git commit -m "feat: bootstrap P0 gitops (cnpg)"
git branch -M main
git remote add origin http://192.168.67.201/digitalsupply/platform-gitops.git
git push -u origin main
```

然后在集群里创建 app-of-apps：

```bash
kubectl apply -f clusters/prod/app-of-apps.yaml
```

> 先确认 `app-of-apps.yaml` 里的 `repoURL` 与你的 GitLab 地址一致。

---

## 6. 上线前验证

```bash
# 1. 服务端 dry-run（不落盘）
kubectl apply --server-side --dry-run=server -f platform/cnpg/cluster/

# 2. diff（对比当前集群）
kubectl diff -f platform/cnpg/cluster/

# 3. ArgoCD diff
argocd app diff platform-cnpg-cluster

# 4. 确认 PVC 没被 prune
kubectl -n cnpg-system get pvc
```

---

## 7. 下一步

- 按同样结构补 `platform/kafka/`、`platform/monitoring/`、`platform/cicd/`；
- 把现有 6 个业务 Application 也纳入 app-of-apps；
- 引入 Sealed Secrets / ESO，移除 open-webui 仓库里的明文 Secret；
- 给 ArgoCD 自身建一个 `argocd` Application（`prune=false`），实现“GitOps 管 GitOps”。
