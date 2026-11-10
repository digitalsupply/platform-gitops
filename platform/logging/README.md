# 日志栈（Loki + Alloy）运维说明

> 最后更新：2026-11-09  
> 命名空间：`logging`  
> Helm release：`loki`（chart `loki` 6.55.0 / Loki 3.6.7，SimpleScalable）  
> 采集：`alloy` Helm release（DaemonSet）

---

## 1. 管理方式

Loki 由 **Helm 手工管理**，不是 ArgoCD 直接管理。原因：

- S3（Ceph RGW）的 `access_key_id` / `secret_access_key` 在 Helm release 中，不能进 Git；
- Loki 是有状态组件（backend/write 使用 PVC），不适合无人值守自动同步。

`platform/logging/values.yaml` 是**非 Secret 部分的 source of truth**。升级必须带 `--reuse-values`：

```bash
helm -n logging upgrade loki /root/.cache/helm/repository/loki-6.55.0.tgz \
  --reuse-values \
  -f platform/logging/values.yaml
```

> 使用本地缓存 chart 是为离线环境准备；有外网时可换成
> `grafana/loki --version 6.55.0`。

---

## 2. 2026-11-09 修复：Grafana Logs Drilldown 无数据

### 现象

Grafana 的 `Drilldown → Logs` 页面能打开，但面板报错/空白。Grafana 日志：

```text
Failed resource call from loki ...
GET /loki/api/v1/index/volume -> 500
error="<html><head><title>502 Bad Gateway</title></head> ... nginx/1.29.8"
```

Loki gateway 日志：

```text
[error] loki-read.logging.svc.cluster.local could not be resolved
        (110: Operation timed out)
```

### 根因

`loki-gateway` 的 nginx 使用 `resolver kube-dns.kube-system.svc.cluster.local.`。
在 CNI/conntrack 切换后，已有 nginx worker 的 UDP resolver socket 状态失效，
导致所有 `/loki/api/v1/*` 反向代理请求在 DNS 解析阶段等待 30s 后返回 502。
`getent` / `nslookup` 正常，只有 nginx 内部 resolver 受影响。

验证：

```bash
kubectl -n logging exec deploy/loki-gateway -- \
  curl -s -m 5 -o /dev/null -w '%{http_code}\n' \
  -H 'X-Scope-OrgID: fake' \
  http://loki-read.logging.svc.cluster.local:3100/loki/api/v1/labels   # 200

kubectl -n logging exec deploy/loki-gateway -- \
  curl -s -m 35 -o /dev/null -w '%{http_code}\n' \
  -H 'X-Scope-OrgID: fake' \
  http://127.0.0.1:8080/loki/api/v1/labels                            # 502
```

临时恢复：

```bash
kubectl -n logging exec deploy/loki-gateway -- kill -HUP 1
```

### 固化到 Git 的修复

1. **resolver 固定为 kube-dns ClusterIP**
   ```yaml
   gateway:
     nginxConfig:
       resolver: "10.96.0.10 valid=10s"
   ```
   - 去掉启动时的二次解析；
   - `valid=10s` 缩短上游地址缓存。

2. **liveness probe 走真实读路径**
   ```yaml
   gateway:
     livenessProbe:
       httpGet:
         path: /loki/api/v1/labels
         port: http-metrics
         httpHeaders:
           - name: X-Scope-OrgID
             value: fake
       initialDelaySeconds: 60
       periodSeconds: 60
       timeoutSeconds: 10
       failureThreshold: 5
   ```
   解析器再次失效时，连续 5 分钟失败后 kubelet 会重启 gateway，自动恢复。

3. **把手工 patch 的 memcached 大小写回 Git**
   - `chunksCache.allocatedMemory: 700`（容器 limit 1Gi）
   - `resultsCache.allocatedMemory: 350`（容器 limit 512Mi）
   - 原 chart 默认 8192/1024 MiB，会在下次 Helm upgrade 时重新引入 OOM。

4. **把只存在于 live ConfigMap 的保留策略写回 Git**
   - `compactor` retention 120h
   - `limits_config.retention_period: 120h`
   - `limits_config.max_query_lookback: 120h`
   - `limits_config.reject_old_samples_max_age: 120h`

---

## 3. 验证

```bash
# gateway / Loki
kubectl -n logging get pods -l app.kubernetes.io/component=gateway
kubectl -n logging logs deploy/loki-gateway --tail=50 | grep -i 'could not be resolved' || true

kubectl -n logging exec deploy/loki-gateway -- \
  curl -s -m 10 -o /dev/null -w 'labels=%{http_code}\n' \
  -H 'X-Scope-OrgID: fake' http://127.0.0.1:8080/loki/api/v1/labels

kubectl -n logging exec deploy/loki-gateway -- \
  curl -s -m 10 -o /dev/null -w 'limits=%{http_code}\n' \
  -H 'X-Scope-OrgID: fake' http://127.0.0.1:8080/loki/api/v1/drilldown-limits

# Grafana 数据源代理
kubectl -n monitoring port-forward svc/prometheus-grafana 13000:80
AUTH='admin:<password>'
curl -s -u "$AUTH" -m 15 -o /dev/null -w '%{http_code}\n' \
  'http://127.0.0.1:13000/api/datasources/uid/dfi90c2a1osn4c/resources/drilldown-limits'
```

Grafana 侧确认：

```bash
kubectl -n monitoring exec deploy/prometheus-grafana -- \
  grafana cli --homepath /usr/share/grafana plugins ls | grep -E 'lokiexplore|drilldown'
```

预期：

```text
grafana-lokiexplore-app @ 1.0.37
grafana-metricsdrilldown-app @ 1.0.34
grafana-exploretraces-app @ 1.3.3
```

---

## 4. 回滚

```bash
# 回滚到上一个 Helm revision
helm -n logging history loki
helm -n logging rollback loki <revision>
```

如果只想撤回 gateway resolver/liveness：

- 从 `platform/logging/values.yaml` 删除 `gateway.nginxConfig.resolver` 和
  `gateway.livenessProbe`，重新执行 helm upgrade；
- 临时恢复仍可用 `kill -HUP 1`。

---

## 5. 已知限制

| 限制 | 说明 |
|---|---|
| 单副本 | backend/read/write/gateway 都是 1 副本，升级/重启有短暂中断 |
| S3 凭据不在 Git | 依赖 `--reuse-values`，不要用 `--reset-values` |
| ClusterIP 硬编码 | `10.96.0.10` 是 kube-dns Service IP；若重建集群需同步修改 |
| liveness 依赖 Loki | Loki 长时间不可用时 gateway 会重启，属于预期行为 |

---

## 6. Alloy（Pod 日志采集）

> Helm release：`alloy`（chart 1.13.0 / Alloy v1.20.0）  
> 类型：DaemonSet，每节点一个 Pod  
> 作用：只采集 Pod 日志，不做 metrics、不做 Kubernetes events

### 6.1 管理方式

| 对象 | 管理方式 |
|---|---|
| Helm release `alloy` | 手工 Helm（与 Loki 一致） |
| Helm values | `platform/logging/alloy-values.yaml`（Git） |
| ConfigMap `alloy-config` | ArgoCD `platform-alloy-config` |
| Config 内容 | `platform/logging/alloy/alloy-config.yaml`（Git） |

升级：

```bash
helm -n logging upgrade alloy /root/.cache/helm/repository/alloy-1.13.0.tgz \
  --reuse-values \
  -f platform/logging/alloy-values.yaml
```

### 6.2 资源调整（2026-11-09）

实际用量 63–101Mi / 8–27m CPU，原配置 request 128Mi / limit 512Mi 偏大。
已调整为：

```yaml
alloy:
  resources:
    requests: {cpu: 50m, memory: 64Mi}
    limits:   {cpu: 300m, memory: 256Mi}
configReloader:
  resources:
    requests: {cpu: 5m, memory: 32Mi}
    limits:   {cpu: 50m, memory: 64Mi}
```

### 6.3 日志链路

```text
/var/log/pods/*.log
  → discovery.kubernetes（仅本节点 Pod）
  → loki.source.file
  → loki.process（CRI 解析、丢弃 120h 前日志）
  → loki.write → http://loki-gateway.logging.svc.cluster.local
```

Config 变更走 ArgoCD；DaemonSet 资源变更走 Helm。

### 6.4 注意

- Alloy 当前仍运行在 control-plane 节点；移除 control-plane 日志会影响 apiserver/etcd 等问题排查。
- `kubectl top node` 的百分比按 allocatable 计算，control-plane 节点可能 >100%，以 node conditions 为准。
