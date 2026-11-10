# etcd 备份（Ceph RGW / S3）

> 最后更新：2026-11-09  
> 命名空间：`etcd-backup`  
> 节点：固定 `m01`（control-plane）  
> 主存储：`s3://etcd-backups/`（Ceph RGW `http://10.0.0.31:7480`）  
> 本地暂存：m01 `/var/backups/etcd`（24h hourly + 7 daily）  
> 频率：每小时 `:05`（Asia/Shanghai）

---

## 1. 设计

```text
m01 etcd (127.0.0.1:2379)
  │  etcdctl snapshot save
  ▼
m01:/var/backups/etcd/hourly/etcd-YYYYMMDD-HHMM.db   # 本地暂存
  │  aws s3 cp（chroot 到宿主）
  ▼
Ceph RGW: s3://etcd-backups/hourly/...                # 持久化
          s3://etcd-backups/daily/...                 # 每天一份
```

### 为什么不是 Ceph RBD PVC

- etcd 只监听宿主网络，证书在 control-plane 节点；
- Ceph RBD CSI nodeplugin 只跑在 **worker** 节点，control-plane 无法挂载 `ceph-rbd-sc`；
- Ceph RGW 已经是 Loki/CNPG 的 S3 后端，对象存储更适合备份（生命周期、跨节点）。

### 为什么用 chroot

- 宿主机有 `/usr/bin/aws` 和 `/usr/local/bin/etcdctl`；
- 容器只挂载宿主机 `/` 的只读副本，通过 `chroot /host` 调用这两个工具；
- 只需要 `CAP_SYS_CHROOT`，不需要 privileged；
- 不依赖 CSI、NFS、额外镜像构建。

## 2. 对象布局与生命周期

```text
s3://etcd-backups/
├── hourly/etcd-20261109-1605.db
├── hourly/etcd-20261109-1705.db
└── daily/etcd-20261109.db
```

Bucket lifecycle 已配置：

- `hourly/`：30 天过期
- `daily/`：90 天过期

本地暂存保留：

- hourly：约 24 个（26h 窗口）
- daily：7 个

单个快照约 120 MB；本地约 3–4 GB。

## 3. 依赖

### 3.1 Secret（不在 Git）

```bash
kubectl -n etcd-backup create secret generic etcd-backup-s3 \
  --from-literal=AWS_ACCESS_KEY_ID='...' \
  --from-literal=AWS_SECRET_ACCESS_KEY='...'
```

当前使用的是 CNPG 备份用户（`cnpg-system/pg-backup-s3-creds`）的凭据；也可以换成专用的 RGW 用户。

### 3.2 Bucket

已创建：

```bash
aws --endpoint-url http://10.0.0.31:7480 s3 mb s3://etcd-backups
```

lifecycle：

```bash
aws --endpoint-url http://10.0.0.31:7480 \
  s3api put-bucket-lifecycle-configuration \
  --bucket etcd-backups \
  --lifecycle-configuration file://s3-lifecycle.json
```

## 4. 验证

```bash
# 手动跑一次
kubectl -n etcd-backup create job --from=cronjob/etcd-snapshot etcd-snapshot-manual
kubectl -n etcd-backup logs job/etcd-snapshot-manual -f

# 本地
ls -lh /var/backups/etcd/hourly /var/backups/etcd/daily

# S3
aws --endpoint-url http://10.0.0.31:7480 s3 ls s3://etcd-backups/hourly/ | tail
aws --endpoint-url http://10.0.0.31:7480 s3 ls s3://etcd-backups/daily/

# 校验
aws --endpoint-url http://10.0.0.31:7480 s3 cp \
  s3://etcd-backups/hourly/<file>.db /tmp/check.db
etcdctl snapshot status /tmp/check.db -w table
```

## 5. 恢复（高风险，需维护窗口）

> 恢复 etcd 会回滚整个集群状态。先在测试环境演练并记录 RTO。

1. 从 S3 取快照：
   ```bash
   aws --endpoint-url http://10.0.0.31:7480 s3 cp \
     s3://etcd-backups/daily/etcd-YYYYMMDD.db /var/backups/etcd/restore.db
   ```
2. 停掉所有 control-plane 的静态 Pod：
   ```bash
   mv /etc/kubernetes/manifests /etc/kubernetes/manifests.bak
   ```
3. 在 m01 恢复：
   ```bash
   etcdctl snapshot restore /var/backups/etcd/restore.db \
     --name m01 \
     --initial-cluster m01=https://10.0.0.61:2380,master01=https://10.0.0.121:2380,master02=https://10.0.0.120:2380 \
     --initial-advertise-peer-urls https://10.0.0.61:2380 \
     --data-dir /var/lib/etcd-restore
   ```
4. 备份并替换 `/var/lib/etcd`，恢复 `/etc/kubernetes/manifests`。
5. 验证：
   ```bash
   kubectl get nodes
   kubectl get pods -A | grep -v Running
   ```

## 6. 已知限制

| 限制 | 说明 |
|---|---|
| 依赖 m01 的 aws/etcdctl | m01 重装后需确认 `/usr/bin/aws`、`/usr/local/bin/etcdctl` |
| Secret 不在 Git | 命名空间重建后需要重新创建 `etcd-backup-s3` |
| 依赖 Ceph RGW 可用 | RGW 故障时上传失败，本地暂存仍保留 |
| 本地 `/var/backups` | 约 3–4 GB，建议加磁盘水位告警 |
