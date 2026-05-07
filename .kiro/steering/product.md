# Product: runbook-exec

`runbook-exec` is an AI-driven CLI tool that transforms passive Markdown runbooks into executable automation with safety gates and audit trails.

## Core Purpose

At incident time (e.g., 3 AM), instead of a human manually following a runbook step-by-step, `runbook-exec` reads the Markdown runbook, classifies each step by risk level using an LLM, executes safe steps autonomously, requests human approval for risky operations via Slack, and produces a tamper-evident audit log.

## Key Capabilities

- **Parse** Markdown runbooks and extract numbered steps with commands
- **Classify** each step as `read_only`, `modifying`, or `destructive` using Claude
- **Execute** steps with appropriate safety gates (auto-run vs. Slack approval)
- **Audit** every action in a tamper-evident hash-chained JSON log
- **Dry-run** mode to preview what would happen without executing
- **Validate** subcommand for CI/CD integration
- **Replay** previous executions from audit logs

## Primary Users

- DevOps / SRE engineers running incident response runbooks
- Platform engineers integrating runbook validation into CI/CD pipelines
- SRE managers reviewing post-incident audit trails

## Safety Bias Principle

When uncertain between two risk levels, always classify at the more cautious (higher risk) level.
