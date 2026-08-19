# AWS Production-Readiness Assessment (2026-07-20)

> Archived from CLAUDE.md (2026-08-19). Analysis only — no code changes. Cloud migration is
> deferred until the local-lab real-robot E2E run works (see the memory pointer
> `aws-readiness-2026-07-20`).

Reviewed `Design/aws_environment_stack.svg` (edge → ingestion → stream processing → agent
orchestration → vision inference → storage → web → observability) against the current
implementation. **Analysis only — NO code changes were made**, per the standing priority: the
immediate goal is a **local-lab, real-robot, end-to-end run with nothing on the cloud**; AWS migration
is explicitly deferred until after that. This section records the assessment so it isn't re-derived.

**Readiness verdict (if we had to use AWS "tomorrow"):**
- **Lift-and-shift** (same app on ECS/EC2 + Bedrock + S3 + RDS, no topology change): **~80% — days.**
  The three config toggles already exist and are wired end-to-end: `VISION_BACKEND=bedrock`
  (`llm.py` → `ChatBedrockConverse`), `STORAGE_BACKEND=s3` (`storage/aws.py` `S3Storage` +
  `SQSImageQueue` already written), `db_url=postgresql://…` (SQLAlchemy is engine-agnostic;
  `api/database.py` migrations are plain SQL). `boto3`/`langchain-aws` are declared under the `[aws]`
  extra. Remaining work is provisioning + secrets + auth + containerization, not app rewrites.
- **Full diagram topology** (IoT Core/MQTT → MSK/Kafka → Lambda → SQS → SageMaker → Fargate + Redis
  + API Gateway WS): **~30–40% — weeks-to-months.** The event-driven fabric does not exist: we use
  **synchronous point-to-point REST** to the robot (`real_robot.py` POST→poll), **in-process threads**
  (`supervisor/` `ThreadPoolExecutor`, `api/main.py` daemon threads) instead of Fargate containers,
  **in-memory LangGraph state + a `run_id→callback` broadcaster dict** instead of ElastiCache Redis,
  and **an in-process FastAPI WebSocket** instead of API Gateway WS. There is no MSK, no IoT Core, no
  Greengrass, no CloudWatch/X-Ray.
- **Gating risk is orthogonal to cloud:** the `real` backend is still **unverified against physical
  arm/camera/card hardware** (only the AGV `/base/*` path has run live). Cloud migration should not
  start until the local hardware loop is proven — moving an unverified integration into a distributed
  event mesh multiplies debugging cost.

**Key architectural divergence — "SageMaker endpoint" ≠ our vision.** The diagram's VISION INFERENCE
layer assumes a **self-hosted, trained CV model on a SageMaker GPU endpoint**. Our differentiator is
the opposite: **Claude Opus general vision, zero per-app training**. So for us that box maps to a
**Bedrock Claude invocation** (already the `bedrock` toggle), NOT SageMaker. Keep SageMaker only if we
later host an auxiliary open-vision/OCR model to push more validation into the 0-LLM tiers (relevant
to the camera-frame Tier-1 gap — see [[camera-vision-test-2026-07-16]]). Adopting SageMaker as the
primary path would discard the training-free advantage.

**Compatibility by layer (what's ready / what changes):**
- **Storage → S3 / RDS:** HIGH. `S3Storage`, `SQSImageQueue`, `db_url` swap all present. Change: point
  `s3_bucket`/`sqs_queue_url`/`db_url` at real resources; migrate SQLite rows to Postgres once.
- **Vision → Bedrock:** HIGH. One flag. Change: IAM role for Bedrock; confirm `claude-opus-4-8` model
  id in the target region.
- **Web API + WS → ECS + API Gateway:** MEDIUM. FastAPI containerizes cleanly, but the **in-process
  broadcaster** must move to a shared bus (Redis pub/sub or API Gateway WS) before the web tier can
  scale beyond one instance; today WS clients must hit the same process running the suite.
- **Orchestration → Fargate + Redis + Lambda scheduler:** LOW. `supervisor` is single-host threads;
  agent state is in-memory. Needs externalized state (Redis) and a per-suite container model.
- **Robot comms → IoT Core / API Gateway command dispatch:** LOW. We assume a **LAN-reachable robot
  HTTP server** (matches the diagram's "REST receiver" edge box) and drive it directly. The
  MQTT/IoT-Core command path + ACK-over-MSK is unbuilt. For a lab and many customer sites, direct REST
  (or REST-over-VPN) may stay simpler than MSK; MSK/IoT earns its keep only at real fleet scale.
- **Ingestion/stream (MSK, Lambda telemetry) & Observability (CloudWatch/X-Ray):** NONE yet. Net-new.
- **Security (VPC/IAM/Secrets Manager/WAF/mTLS):** LOW. Today: `.env` secrets, `CORS *`, no auth. All
  net-new for production.

**Main advantages of the AWS stack (why it's worth it later):** horizontal scale (one Fargate
container per suite → many kiosks/robots concurrently, vs our single-host thread pool); decoupling &
resilience (MSK/SQS buffer robot events so a slow consumer or restart doesn't drop telemetry);
managed durability (S3 for images/sensor bags, RDS for results — no local disk/SQLite ceiling); elastic
vision cost (Bedrock/SageMaker autoscale vs a fixed box); security & compliance posture for customer
demos (IAM per service, Secrets Manager, WAF, IoT mTLS with per-robot certs); observability (CloudWatch/
X-Ray traces, command-latency & inference-SLA alarms) we currently approximate with `print()` + WS
events; and OTA/edge management via Greengrass. Trade-off to weigh: MSK + SageMaker are heavyweight for
current lab scale — a leaner first cloud step (ECS + Bedrock + S3 + RDS + SQS, deferring MSK/IoT/
Greengrass) captures ~80% of the benefit at a fraction of the ops cost.

**When we do migrate — regression-safety rules (nothing here changes local behaviour):** every cloud
hook stays behind the existing `*_backend` toggles defaulting to local; add cloud config only via
`Settings` (never `os.environ`); externalizing the broadcaster must keep the in-process path as the
default so playwright/demo runs are byte-identical; keep `local`/`playwright` fully functional so the
lab loop never depends on cloud. See [[aws-readiness-2026-07-20]].

