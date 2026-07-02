# Robotic Vision Agent

A LangGraph-based sub-agent that reads kiosk application screens, identifies UI elements with exact coordinates, executes robotic arm actions, validates outcomes, and maintains a decision tree of screen transitions.

Part of a larger robotic kiosk test automation system. This sub-agent handles the vision + action layer only — it is designed to be driven by an orchestrator agent that feeds it test tasks.

---

## How it works

```
Screenshot → analyze_screen → plan_steps → execute_step → validate_step
                    ↑                                            │
                    └──────────── handle_retry ←─────────────── │ (fail)
                                                                 │ (pass)
                                                           advance_step → finalize
```

Each cycle through the graph handles one atomic step of a test task. The agent loops until all steps pass or retries are exhausted.

**What each node does:**

| Node | Responsibility |
|---|---|
| `analyze_screen` | Claude vision identifies every UI element — type, label, bounding box, center tap coordinate, description |
| `plan_steps` | Claude converts the task description into ordered atomic steps (`tap:`, `type:`, `verify:`) |
| `execute_step` | Dispatches `tap(x, y)` or `type_text()` to the robot arm, then captures the resulting screen |
| `validate_step` | Claude vision checks the after-screenshot; updates the decision tree |
| `handle_retry` | Increments retry counter, logs recovery hint, returns to `analyze_screen` |
| `finalize` | Computes pass/fail outcome, persists result JSON |

---

## Project structure

```
robotic-vision-agent-claude/
├── vision_agent/
│   ├── agent.py          # LangGraph StateGraph assembly + routing logic
│   ├── state.py          # VisionAgentState TypedDict schema
│   ├── prompts.py        # All three Claude prompts (analyze, plan, validate)
│   ├── llm.py            # LLM factory: ChatAnthropic (local) | ChatBedrockConverse (AWS)
│   ├── config.py         # pydantic-settings — all config via .env
│   ├── nodes/
│   │   ├── analyze.py
│   │   ├── plan.py
│   │   ├── execute.py
│   │   ├── validate.py
│   │   ├── retry.py
│   │   └── finalize.py
│   ├── robot/
│   │   └── stubs.py      # tap / type_text / capture_screen — replace bodies for real arm
│   └── storage/
│       ├── local.py      # LocalStorage (dev)
│       └── aws.py        # S3Storage + SQSImageQueue (AWS)
├── tests/
│   └── test_vision_agent.py
├── run_demo.py           # Demo with local screenshots (login + checkout flows)
├── pyproject.toml
└── .env.example
```

---

## Setup

```bash
# 1. Install
pip install -e .

# 2. Configure
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY

# 3. Run demo (login flow)
python run_demo.py

# 4. Run demo (checkout flow)
python run_demo.py checkout

# 5. Run tests
pytest tests/ -v
```

---

## Configuration

All configuration is in `.env`. The two most important toggles:

| Variable | Local / Dev | AWS |
|---|---|---|
| `VISION_BACKEND` | `anthropic` | `bedrock` |
| `STORAGE_BACKEND` | `local` | `s3` |

Switching to AWS requires only these two env-var changes — no code changes.

**Full `.env` reference:**

```bash
# Vision
VISION_BACKEND=anthropic          # or: bedrock
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-opus-4-8            # analysis, planning, Tier-3
ANTHROPIC_EXPLORER_MODEL=claude-opus-4-8   # app-exploration reasoning

# AWS Bedrock (only needed when VISION_BACKEND=bedrock)
BEDROCK_REGION=us-east-1
BEDROCK_MODEL_ID=anthropic.claude-opus-4-8

# Storage
STORAGE_BACKEND=local             # or: s3
S3_BUCKET=my-bucket
S3_PREFIX=vision-agent
SQS_QUEUE_URL=                    # SQS queue for robot image events

# Agent
MAX_RETRIES=3
SCREENSHOTS_DIR=./screenshots
RESULTS_DIR=./results
```

---

## Integrating with the orchestrator agent

```python
from vision_agent.agent import create_agent

agent = create_agent()

result = agent.invoke({
    "task_description": "Log in with email tester@kiosk.local and password Password123",
    "image_path": "/path/to/current_screen.png",  # or s3://bucket/key in AWS
    "screen_analysis": None,
    "planned_steps": [],
    "current_step_idx": 0,
    "step_results": [],
    "retry_count": 0,
    "screen_history": [],
    "decision_tree": {},   # pass accumulated tree across calls for navigation intelligence
    "outcome": "running",
    "summary": "",
    "error_message": None,
})

print(result["outcome"])        # "passed" | "failed"
print(result["summary"])        # human-readable summary
print(result["step_results"])   # per-step evidence (screenshots, observations)
print(result["decision_tree"])  # {screen_id: {step: next_screen_id}} — grows over time
```

Passing `decision_tree` back in on subsequent calls lets the agent accumulate navigation knowledge across multiple test runs.

---

## Robot arm integration

All robot arm calls are in `vision_agent/robot/stubs.py`. Each function mirrors the real arm's API exactly — only the function bodies change when hardware arrives:

```python
def tap(x: int, y: int) -> dict: ...
def type_text(text: str) -> dict: ...
def capture_screen(save_path: str) -> dict: ...
def swipe(x1, y1, x2, y2, duration_ms=300) -> dict: ...
```

---

## AWS deployment

This agent maps directly onto the AWS architecture:

| Layer | AWS service | Role |
|---|---|---|
| Vision inference | Amazon Bedrock (Claude) | Replaces direct Anthropic API — set `VISION_BACKEND=bedrock` |
| Agent runtime | ECS Fargate | One container per active test suite |
| Image queue | SQS (via Lambda from MSK) | `SQSImageQueue.receive_next()` in `storage/aws.py` |
| Image storage | S3 | Screenshots in, result JSON out — set `STORAGE_BACKEND=s3` |
| State store | ElastiCache Redis | Plug in at the orchestrator layer (not in this sub-agent) |
| Observability | CloudWatch + X-Ray | Add `LANGCHAIN_TRACING_V2=true` for LangSmith, or instrument with X-Ray SDK |

Install AWS extras:
```bash
pip install -e ".[aws]"
```

---

## Adding a new screen type

1. Add the new `screen_id` value to the `ANALYZE_SCREEN` prompt in `prompts.py`
2. Add the screen to `FLOWS` in `run_demo.py` with its screenshot sequence
3. No other changes needed — Claude vision handles element discovery automatically
