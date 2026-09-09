# Kubernetes deploy (cloud-agnostic)

The **same image** as `docker-compose.yml`; only the orchestrator differs. Works identically on
AKS (Azure), GKE (Google), EKS (AWS), or any conformant cluster.

## 1. Build & push the image (any OCI registry)
```bash
docker build -t REGISTRY/kioskqa:latest .
docker push REGISTRY/kioskqa:latest
# Replace REGISTRY/kioskqa:latest in api-deployment.yaml and worker-deployment.yaml.
```

## 2. Provide the three backing services
Point `configmap.yaml` at whichever you use — **managed is recommended in production**:

| Need        | Azure                          | Google            | AWS / any        | In-cluster dev (Helm) |
|-------------|--------------------------------|-------------------|------------------|-----------------------|
| PostgreSQL  | Azure Database for PostgreSQL  | Cloud SQL         | RDS              | `helm install pg bitnami/postgresql` |
| Object store| Blob (S3 gateway) or MinIO     | GCS (S3 interop)  | S3 / MinIO       | `helm install minio bitnami/minio` |
| Redis       | Azure Cache for Redis          | Memorystore       | ElastiCache      | `helm install redis bitnami/redis` |

## 3. Apply
```bash
cp secret.example.yaml secret.yaml   # fill ANTHROPIC_API_KEY etc.; keep OUT of git
kubectl apply -f secret.yaml
kubectl apply -k .                    # namespace + config + api + worker + ingress
```

## 4. Verify
```bash
kubectl -n kioskqa get pods
kubectl -n kioskqa port-forward svc/kioskqa-api 8001:80
curl localhost:8001/api/health        # "platform" block shows the resolved backends
```

## Scaling
- **API tier** auto-scales via the HPA in `api-deployment.yaml` (CPU-based; raise `maxReplicas`).
- **Worker tier** scales independently (`kubectl -n kioskqa scale deploy/kioskqa-worker --replicas=N`).
- State is external (Postgres/object store/Redis), so pods stay stateless and disposable.
- **WebSockets need NO sticky sessions.** With `EVENT_BUS_BACKEND=redis`, live run events are
  published to Redis and fanned out to every replica, so a client may connect to any API pod and
  still receive events produced by a worker on a different pod. (With the in-memory bus you'd be
  limited to a single replica.)
