# Public mirror notice

This is a sanitized public snapshot of the internal `platform-gitops` repository.

- Internal GitLab URLs, cluster IPs and domains are kept as reference and are not
  reachable from the public internet.
- The n8n webhook URL has been replaced with a placeholder. The real value exists
  only in the internal repository / cluster.
- SealedSecrets are encrypted with the cluster's sealed-secrets controller key.
- This snapshot is not guaranteed to be directly deployable; use the internal
  repository as the source of truth.
