# P0-6 Secret 管理：切换方案与执行状态

## 当前状态（2026-11-08）

- Sealed Secrets Controller **v0.41.2** 已通过 GitOps 安装（`platform-sealed-secrets` App，`kube-system`）；
- open-webui 的 3 个 Secret 已迁移为 `SealedSecret`，控制器创建的 Secret 数据哈希与迁移前一致；
- 私钥备份：`/root/k8s-postmortem/backup/sealed-secrets-key.yaml`（**必须妥善保存**）。

## 现状

| Secret | namespace | 状态 |
|---|---|---|
| `frp-token` | open-webui | ✅ 已迁移为 SealedSecret |
| `harbor-pull` | open-webui | ✅ 已迁移为 SealedSecret |
| `open-webui-config` | open-webui | ✅ 已迁移为 SealedSecret |
| `gitlab-auth` | cicd | ⏳ 待迁移（建议同时轮换 GitLab token） |
| `pg-secret` | cnpg-system | ⏳ 待迁移 |
| `pg-superuser-secret` | cnpg-system | ⏳ 待迁移 |
| `pg-backup-s3-creds` | cnpg-system | ⏳ 待迁移 |

> 明文 Secret 进入 Git 后，即使后续删除，历史提交仍可恢复；必须轮换凭据。

## 方案选择

| 方案 | 适用场景 | 优点 | 缺点 |
|---|---|---|---|
| **Sealed Secrets** | 纯 GitOps、无外部 Secret Manager | 控制器 + `kubeseal`，简单 | 集群私钥丢失则无法解密；轮换需重新加密 |
| **External Secrets Operator (ESO)** | 已有 Vault / 云 Secret Manager | 凭据不落 Git，集中管理 | 需要外部 Secret Store |

推荐：如果暂时没有 Vault，先用 **Sealed Secrets**；后续再评估 ESO。

## Sealed Secrets 落地步骤

### 1. 安装控制器（通过 ArgoCD）

新增 `platform/secrets/sealed-secrets/`：

```yaml
# 官方 manifest 或 Helm chart
# https://github.com/bitnami-labs/sealed-secrets/releases
```

ArgoCD Application：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: platform-sealed-secrets
  namespace: argocd
  annotations:
    argocd.argoproj.io/sync-wave: "-20"
spec:
  project: default
  source:
    repoURL: http://192.168.67.201/digitalsupply/platform-gitops.git
    targetRevision: main
    path: platform/secrets/sealed-secrets
  destination:
    server: https://kubernetes.default.svc
    namespace: kube-system
  syncPolicy:
    automated:
      prune: false
      selfHeal: true
    syncOptions:
      - ServerSideApply=true
```

### 2. 安装 kubeseal CLI

```bash
# 从 release 下载，或
go install github.com/bitnami-labs/sealed-secrets/cmd/kubeseal@latest
```

### 3. 逐个迁移 Secret

```bash
# 以 open-webui/frp-token 为例
kubectl -n open-webui get secret frp-token -o yaml \
  | kubeseal --format yaml \
  > platform/secrets/open-webui/frp-token.sealed.yaml
```

提交 `SealedSecret`，从 open-webui 的 GitOps 仓库删除明文 `Secret`。

### 4. 迁移顺序（建议）

1. `open-webui` 的 3 个 Secret（业务，风险最低）；
2. `cicd/gitlab-auth`（CI push 凭据）；
3. `cnpg-system/pg-secret`、`pg-superuser-secret`、`pg-backup-s3-creds`（数据库，最后做）。

### 5. 轮换凭据

明文 Secret 曾进入 Git，迁移后必须：

- 轮换 GitLab token；
- 轮换 PostgreSQL 应用用户/超级用户密码；
- 轮换 S3 备份凭据；
- 更新 SealedSecret 并提交。

## ESO 方案（替代）

1. 安装 External Secrets Operator；
2. 创建 `SecretStore` / `ClusterSecretStore` 指向 Vault；
3. 用 `ExternalSecret` 替代 `Secret`；
4. 凭据只在 Vault 中维护，Git 里只有引用。

## 验收

- [ ] Git 仓库中不再有明文 `Secret`
- [ ] `kubectl get sealedsecret -A` 与 `kubectl get externalsecret -A` 正常
- [ ] 业务 Pod 能正常挂载/读取 Secret
- [ ] CI 能正常 push
- [ ] CNPG 备份/恢复不受影响
- [ ] 旧凭据已轮换
