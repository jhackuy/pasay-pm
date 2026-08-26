# PASAY AgentTeams autonomous engineering team

This directory turns AgentTeams v1.2.3 into a PASAY-specific Manager → Team Leader → Workers workflow. It is intentionally separate from PASAY business code.

## Delivered team

| Agent | Authority |
| --- | --- |
| `default` Manager | Accepts one Owner goal, creates a Project DAG, tracks evidence, reports final status |
| `pasay-lead` | Decomposes and coordinates; does not implement |
| `pasay-auditor` | Read-only repository/runtime baseline |
| `pasay-builder` | Implements on an isolated branch; no merge/deploy |
| `pasay-qa` | Runs independent executable acceptance |
| `pasay-reviewer` | Read-only scope/security/architecture review |
| `pasay-brake` | Stops drift, duplicate findings and revision loops |

The systemd watchdog is a second, non-LLM brake. Every five minutes it fingerprints Project task states. Six unchanged checks pause the Project and freeze only the six PASAY Worker containers, stopping in-flight token use instead of allowing an invisible loop. The Manager and AgentTeams control plane remain available.

## One-time deployment on GX10

The four values below are secrets or installation-specific facts and must not be committed:

```bash
export AGENTTEAMS_LLM_API_KEY='...'
export AGENTTEAMS_ADMIN_PASSWORD='...'
export AGENTTEAMS_OPENAI_BASE_URL='https://provider.example/v1'
export PASAY_AGENT_MODEL='model-id'
export PASAY_GITHUB_MCP_URL='https://agentteams.example/mcp-servers/github/mcp'
bash ops/agentteams/bootstrap.sh
```

`PASAY_GITHUB_MCP_URL` must point to an AgentTeams/Higress GitHub MCP route whose credential is stored at the gateway. Do not put a GitHub PAT in Worker YAML or chat. The route must grant access to `jhackuy/pasay-pm`; Workers otherwise have a chat room but cannot operate on the private repository.

The script performs a non-interactive pinned v1.2.3 installation, deploys the Manager and six Workers, creates the `pasay-engineering` Team Room, installs the watchdog timer, and verifies the Team/Worker resources. It does not merge, deploy PASAY, write production secrets, or change production data.

## Owner usage

After installation, open Element at `http://192.168.50.42:18088` and send one message to the Manager:

```text
目标：完成 PASAY <milestone>，直到 READY_FOR_OWNER。
以仓库 AGENTS.md、当前 authority branch、真实测试和运行证据为准。
普通技术决定由团队自行处理；中途不要询问我。
不得 merge、production deploy、写 Secrets 或修改生产数据。
```

The Manager must create a Project rather than free-form chat. Final states are:

- `READY_FOR_OWNER`: implementation and independent evidence are complete; Owner performs final acceptance/merge decision.
- `BLOCKED_FOR_PRODUCT_DECISION`: the goal conflicts with an Owner-only business boundary or a required credential/capability is absent. The team stops once and reports evidence; it must not repeatedly ask.
- `PAUSED_BY_WATCHDOG`: workflow state did not change for 30 minutes. The Project and PASAY Worker containers are paused. After inspecting/replanning, resume with `~/.local/share/pasay-agentteams/resume-project.sh <project-id>`.

## Local checks

```bash
python3 -m pytest -q ops/agentteams/tests/test_watchdog.py
bash -n ops/agentteams/bootstrap.sh
bash -n ops/agentteams/install-watchdog.sh
bash -n ops/agentteams/resume-project.sh
```

## Known external prerequisites

- GX10 `192.168.50.42` must be reachable and Docker/Podman must work for user `cunzhang`.
- A model API or an OpenAI-compatible local endpoint must be reachable from AgentTeams containers. TRAE Ultra cannot supply this API.
- The GitHub MCP route must already be authorized for the private repository.
- User-level systemd must be available. For watchdog survival across logout, lingering for `cunzhang` must be enabled by the machine administrator.
