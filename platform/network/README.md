# 网络组件 GitOps 管理（MetalLB + Cilium L2）

> 最后更新：2026-11-09  
> 目标：把“MetalLB Speaker → Cilium L2 Announcements”的变更从手工操作变成 Git 中的期望状态  
> 当前 VIP：`10.0.0.252`（Traefik）

---

## 1. 职责划分

| 能力 | 组件 | 是否由本目录管理 |
|---|---|---|
| LB IP 分配（写 `status.loadBalancer.ingress`） | MetalLB controller | ✅ `platform/network/metallb` |
| L2 ARP/NDP 宣告 | Cilium agent + `CiliumL2AnnouncementPolicy` | ✅ `platform/network/cilium/l2-policy` |
| MetalLB speaker | DaemonSet（保持禁用） | ✅ 通过 kustomize patch 固定为 0 副本 |
| Cilium Helm values | Helm release `cilium` | ⚠️ 文件在 Git，升级仍走手动 Helm（见 §5） |

`platform/network/cilium/values.yaml` 是 Cilium 的 source of truth，但 Cilium
本身仍由 Helm 管理，不由 ArgoCD 执行 `helm upgrade`。原因：

1. 现有 release 是手工 Helm 创建的，交给 ArgoCD 会变成 `helm template + apply`，
   Helm release secret 与实际状态脱节；
2. CNI 升级需要 preflight 和逐节点观察，不适合无人值守自动同步；
3. 与 `platform/monitoring` 的既有模式保持一致（values 在 Git，升级命令固定）。

---

## 2. 目录结构

```text
platform/network/
├── README.md
├── cilium/
│   ├── values.yaml                  # Cilium Helm values（含 l2announcements.enabled=true）
│   └── l2-policy/
│       └── l2-announcement-policy.yaml
└── metallb/
    ├── kustomization.yaml
    ├── metallb-native.yaml          # MetalLB v0.13.12 官方组件（含 CRD/RBAC/controller/speaker）
    ├── pools.yaml                   # IPAddressPool + L2Advertisement
    └── speaker-disabled-patch.yaml  # 把 speaker 固定为不可调度
```

ArgoCD Application：

```text
clusters/prod/applications/
├── platform-metallb.yaml       # sync-wave -5
└── platform-cilium-l2.yaml     # sync-wave 0
```

同步顺序很重要：

```text
wave -5  platform-metallb      先让 MetalLB speaker 变成 0 Pod
wave  0  platform-cilium-l2    再让 Cilium 开始宣告 10.0.0.252
```

反过来的话，会有两个组件同时应答同一个 VIP 的 ARP，造成抖动。

---

## 3. 首次接管（adoption）

这些资源之前是 `kubectl apply` 创建的，第一次 ServerSideApply 可能报字段冲突。
按仓库 README 的约定，先手动 adoption 一次：

```bash
# 1) 预演
kubectl kustomize platform/network/metallb | kubectl diff -f -

# 2) 服务端 apply，强制接管字段
kubectl apply --server-side --force-conflicts \
  -f <(kubectl kustomize platform/network/metallb)

# 3) 确认 speaker 仍为 0 Pod、controller 正常
kubectl -n metallb-system get ds speaker
kubectl -n metallb-system get deploy controller
```

然后合入 `clusters/prod/applications/platform-metallb.yaml` 和
`platform-cilium-l2.yaml`，由 app-of-apps 创建两个 Application。

如果 ArgoCD 第一次 sync 仍报冲突：

```bash
argocd app sync platform-metallb --force
```

> 只在 adoption 时使用 `--force`，不要把它写成常驻的 `Force=true`。

---

## 4. 验证

```bash
# Application
kubectl -n argocd get applications platform-metallb platform-cilium-l2

# MetalLB：controller 在，speaker 期望 0
kubectl -n metallb-system get ds speaker deploy controller
kubectl -n metallb-system get pods

# Cilium L2：策略存在，Lease 有 holder
kubectl get ciliuml2announcementpolicy traefik-l2 -o yaml
kubectl -n kube-system get lease cilium-l2announce-traefik-traefik

# VIP 与 ARP
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: studyfree.xyz' http://10.0.0.252/
ip neigh flush 10.0.0.252; ping -c1 10.0.0.252; ip neigh show 10.0.0.252

# 漂移测试：手动把 speaker 的禁用 label 删掉，ArgoCD 应在一分钟内改回来
kubectl -n metallb-system patch ds speaker --type=json -p '[
  {"op":"remove","path":"/spec/template/spec/nodeSelector/metallb.io/announcer-disabled"}
]'
# 期望：speaker Pod 短暂出现后再次归零
```

---

## 5. Cilium 升级 / 变更

```bash
helm -n kube-system upgrade cilium oci://quay.io/cilium/charts/cilium \
  --version 1.20.4 \
  --skip-crds \
  --reuse-values \
  -f platform/network/cilium/values.yaml

# l2announcements / kubeProxyReplacement / bpf.masquerade 属于启动期配置，
# 改完必须重启 DS，否则 config-drift-checker 只会告警：
kubectl -n kube-system rollout restart ds/cilium
kubectl -n kube-system rollout status ds/cilium --timeout=420s
```

升级后检查：

```bash
kubectl -n kube-system exec ds/cilium -- cilium-dbg status --verbose | grep -iE 'l2|kube-proxy|masquerad'
kubectl -n kube-system get lease | grep cilium-l2
```

---

## 6. 回滚

原则：**先停 Cilium 宣告，再恢复 MetalLB speaker。**

### 6.1 回到 MetalLB speaker（推荐）

```bash
# 1) 删除 Cilium L2 策略（或先把它从 Git 移除）
kubectl delete ciliuml2announcementpolicy traefik-l2

# 2) 移除 speaker 禁用 patch
#    方式 A：Git 中删除 kustomization.yaml 里的 patches 条目并提交
#    方式 B：临时手动（ArgoCD 会在一分钟内改回禁用状态，所以只是应急）
kubectl -n metallb-system patch ds speaker --type=json -p '[
  {"op":"remove","path":"/spec/template/spec/nodeSelector/metallb.io/announcer-disabled"}
]'

# 3) 验证
kubectl -n metallb-system rollout status ds/speaker --timeout=180s
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: studyfree.xyz' http://10.0.0.252/
```

注意：因为 `platform-metallb` 的 `selfHeal` 开启，手动方式 B 会被 ArgoCD 改回
禁用。正式回滚必须改 Git（删除 patch）或先暂停该 Application 的自动同步：

```bash
argocd app set platform-metallb --sync-policy none
```

### 6.2 只回滚节点范围

修改 `l2-announcement-policy.yaml` 的 `nodeSelector` 为 `kubernetes.io/hostname: worker03`，
提交后 ArgoCD 同步。已有 Lease 不会被抢占，所以当前 holder 不变。

### 6.3 回滚 Cilium L2 开关

```bash
# 修改 values.yaml: l2announcements.enabled: false
helm -n kube-system upgrade cilium ... -f platform/network/cilium/values.yaml
kubectl -n kube-system rollout restart ds/cilium
```

---

## 7. 已知限制

| 限制 | 说明 |
|---|---|
| L2 Announcements 是 Beta | Cilium 升级时需回归 ARP/Lease |
| `externalTrafficPolicy: Local` 不兼容 | Traefik 必须保持 `Cluster` |
| failover 10–20s | 默认 Lease 15s/5s/2s，可按需调短 |
| 单活宣告 | 同一 VIP 同一时刻只有一个节点应答 ARP |
| `prune: false` | 删除策略/组件必须显式操作，避免误删导致 VIP 消失 |
| 未管理 Secret | `memberlist`、`webhook-server-cert` 由 MetalLB 自己生成，不入 Git |

---

## 8. 与本次手工变更的对应关系

| 手工操作 | GitOps 表达 |
|---|---|
| `helm upgrade --set l2announcements.enabled=true` | `cilium/values.yaml` 中的 `l2announcements.enabled: true` |
| 创建 `CiliumL2AnnouncementPolicy` | `cilium/l2-policy/l2-announcement-policy.yaml` |
| 给 speaker 加 `metallb.io/announcer-disabled` | `metallb/speaker-disabled-patch.yaml` |
| MetalLB controller/IPAM 保留 | `metallb/metallb-native.yaml` 中的 controller，`prune: false` |
