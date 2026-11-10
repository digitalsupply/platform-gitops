#!/usr/bin/env python3
"""
清洗 kubectl get -o yaml 的运行时字段，生成适合提交 Git 的期望状态 YAML。

用法:
  kubectl -n ns get deploy myapp -o yaml | python3 clean-k8s-yaml.py
  python3 clean-k8s-yaml.py -f live.yaml > clean.yaml

注意:
  - 默认拒绝 Secret（避免明文入库）。确需输出时加 --allow-secret。
  - 只做“去运行时字段”，不改变业务 spec。
"""
import argparse
import sys
import yaml

DROP_METADATA = {
    "uid",
    "resourceVersion",
    "generation",
    "creationTimestamp",
    "deletionTimestamp",
    "deletionGracePeriodSeconds",
    "managedFields",
    "selfLink",
    "ownerReferences",
    "finalizers",
}
DROP_ANNOTATIONS = {
    "kubectl.kubernetes.io/last-applied-configuration",
    "deployment.kubernetes.io/revision",
    "argocd.argoproj.io/tracking-id",
    "kubectl.kubernetes.io/restartedAt",
}
DROP_ANNOTATION_PREFIXES = ("kubectl.kubernetes.io/", "argocd.argoproj.io/")


def clean_metadata(m):
    for k in DROP_METADATA:
        m.pop(k, None)
    ann = m.get("annotations")
    if isinstance(ann, dict):
        for k in list(ann):
            if k in DROP_ANNOTATIONS or k.startswith(DROP_ANNOTATION_PREFIXES):
                ann.pop(k, None)
        if not ann:
            m.pop("annotations", None)
    labels = m.get("labels")
    if isinstance(labels, dict):
        for k in ("pod-template-hash", "controller-revision-hash", "statefulset.kubernetes.io/pod-name"):
            labels.pop(k, None)
        if not labels:
            m.pop("labels", None)


def clean_doc(obj, allow_secret=False):
    if not isinstance(obj, dict):
        return obj

    kind = obj.get("kind", "")
    if kind == "Secret" and not allow_secret:
        raise SystemExit(
            f"拒绝输出 Secret {obj.get('metadata', {}).get('name')}："
            "请改用 SealedSecret / ExternalSecret / SOPS。"
        )

    obj.pop("status", None)
    if isinstance(obj.get("metadata"), dict):
        clean_metadata(obj["metadata"])

    spec = obj.get("spec")
    if isinstance(spec, dict):
        if kind == "Service":
            for k in ("clusterIP", "clusterIPs", "ipFamilies", "ipFamilyPolicy",
                      "healthCheckNodePort", "loadBalancerIP"):
                spec.pop(k, None)
            for p in spec.get("ports", []) or []:
                p.pop("nodePort", None)
        if kind == "PersistentVolumeClaim":
            spec.pop("volumeName", None)
        if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"):
            tmpl = spec.get("template")
            if isinstance(tmpl, dict) and isinstance(tmpl.get("metadata"), dict):
                tmpl["metadata"].pop("creationTimestamp", None)
        if kind == "Job":
            spec.pop("selector", None)
            spec.pop("manualSelector", None)

    if kind == "List":
        obj["items"] = [clean_doc(i, allow_secret) for i in obj.get("items", [])]
    return obj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-f", "--file", help="输入 YAML 文件；省略则读 stdin")
    ap.add_argument("--allow-secret", action="store_true", help="允许输出 Secret（不推荐）")
    args = ap.parse_args()

    if args.file:
        with open(args.file) as fh:
            docs = list(yaml.safe_load_all(fh))
    else:
        docs = list(yaml.safe_load_all(sys.stdin))

    cleaned = []
    for d in docs:
        if not d:
            continue
        # kubectl get 多资源会返回 kind: List，拆成独立文档，方便 ArgoCD 逐资源跟踪
        if d.get("kind") == "List":
            cleaned.extend(clean_doc(i, args.allow_secret) for i in d.get("items", []))
        else:
            cleaned.append(clean_doc(d, args.allow_secret))
    yaml.safe_dump_all(cleaned, sys.stdout, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    main()
