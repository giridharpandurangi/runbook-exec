# Requirements Document

## Introduction

`runbook-exec` is an AI-driven CLI tool that transforms passive Markdown runbooks into executable automation with safety gates and audit trails. The system reads existing operational runbooks, classifies steps by risk level, executes safe steps autonomously, requests human approval for risky operations via Slack, and produces tamper-evident audit logs. The primary use case is incident response: at 3 AM, instead of a human manually following a runbook, the executor runs it end-to-end with appropriate safety controls.

## Glossary

- **Runbook**: A Markdown file containing step-by-step operational instructions
- **Executor**: The runbook-exec CLI tool that parses and executes runbooks
- **Step**: A single numbered instruction within a runbook
- **Classifier**: The LLM-powered component that assigns risk levels to steps
- **Risk_Level**: One of three classifications: read_only, modifying, or destructive
- **Approval_Workflow**: The Slack-based mechanism for obtaining human authorization
- **Audit_Log**: A tamper-evident JSON file recording all execution actions
- **Audit_Entry**: A single record in the audit log with a hash chain
- **Shell_Executor**: The component that runs shell commands via subprocess
- **LLM**: Large Language Model (Anthropic Claude) used for classification and decisions
- **Dry_Run_Mode**: Execution mode that simulates without running commands
- **Auto_Approve_Level**: Configuration setting that bypasses approval for specified risk levels

## Requirements

### Requirement 1: Parse Markdown Runbooks

**User Story:** As a DevOps engineer, I want the Executor to read my existing Markdown runbooks, so that I can automate operational procedures without rewriting documentation.

#### Acceptance Criteria

1. WHEN a valid Markdown file is provided, THE Parser SHALL extract all numbered list items as Steps
2. WHEN a Step contains a fenced code block, THE Parser SHALL extract the code block content as the command
3. WHEN a Step contains inline code (backticks), THE Parser SHALL extract the inline code as the command
4. THE Parser SHALL preserve section headers as logical groupings for Steps
5. WHEN a Markdown file contains unnumbered lists, THE Parser SHALL ignore them
6. WHEN a Markdown file is malformed, THE Parser SHALL return a descriptive error message

### Requirement 2: Classify Step Risk Levels

**User Story:** As a DevOps engineer, I want each step automatically classified by risk level, so that I know which operations are safe to run autonomously.

#### Acceptance Criteria

1. WHEN a Step is parsed, THE Classifier SHALL assign exactly one Risk_Level
2. WHEN a Step contains read-only commands (df, ls, cat, kubectl get, ps, HTTP GET), THE Classifier SHALL assign read_only
3. WHEN a Step contains state-modifying commands (logrotate, systemctl restart, file edits), THE Classifier SHALL assign modifying
4. WHEN a Step contains destructive commands (rm -rf, DROP TABLE, kubectl delete, sudo), THE Classifier SHALL assign destructive
5. WHEN a Step contains sudo with a command not in the safelist, THE Classifier SHALL assign destructive
6. WHEN the Classifier is uncertain between two Risk_Levels, THE Classifier SHALL assign the more cautious Risk_Level
7. WHEN a Step sends external communications (email, Slack post, webhook), THE Classifier SHALL assign modifying or destructive
8. THE Classifier SHALL use the LLM to determine Risk_Level for each Step
9. WHEN the LLM API call fails, THE Classifier SHALL return an error and halt and request human direction per REQ 11

### Requirement 3: Execute Steps Based on Risk Level

**User Story:** As a DevOps engineer, I want read-only steps to run automatically and risky steps to require approval, so that I can balance automation speed with safety.

#### Acceptance Criteria

1. WHEN a Step has Risk_Level read_only, THE Executor SHALL run the Step without approval
2. WHEN a Step has Risk_Level modifying, THE Executor SHALL request approval before running
3. WHEN a Step has Risk_Level destructive, THE Executor SHALL request approval before running
4. WHEN Auto_Approve_Level is configured for a Risk_Level, THE Executor SHALL run Steps of that Risk_Level without approval
5. WHEN a Step execution completes, THE Executor SHALL capture stdout, stderr, exit code, and duration
6. WHEN a Step execution exceeds the configured timeout, THE Shell_Executor SHALL terminate the process and record a timeout error
7. THE Executor SHALL execute Steps in the order they appear in the Runbook

### Requirement 4: Request Approval via Slack

**User Story:** As an oncall engineer, I want to approve risky runbook steps from Slack on my phone, so that I can respond to incidents without opening a laptop.

#### Acceptance Criteria

1. WHEN a Step requires approval, THE Approval_Workflow SHALL post a message to the configured Slack channel
2. THE Approval_Workflow message SHALL include the Step text, the exact command, and Approve/Deny buttons
3. WHEN a user clicks Approve, THE Approval_Workflow SHALL allow execution to continue
4. WHEN a user clicks Deny, THE Approval_Workflow SHALL halt execution and request human direction with options: continue, retry, abort, skip
5. WHEN no response is received within the configured timeout, THE Approval_Workflow SHALL halt execution and record a timeout error
6. THE Approval_Workflow SHALL record the approver's Slack user ID in the Audit_Log
7. WHEN the Slack API call fails, THE Approval_Workflow SHALL return an error and halt execution

### Requirement 5: Make LLM-Driven Execution Decisions

**User Story:** As a DevOps engineer, I want the Executor to read command output and make intelligent branching decisions, so that runbooks can adapt to actual system state.

#### Acceptance Criteria

1. WHEN a Step completes, THE Executor SHALL send the Step output to the LLM
2. THE LLM SHALL decide whether to continue, skip subsequent Steps, or abort execution
3. WHEN the LLM decides to skip Steps, THE Executor SHALL record the reasoning in the Audit_Log
4. WHEN the LLM decides to abort, THE Executor SHALL halt execution and record the reasoning
5. WHEN the no-llm-context flag is set, THE Executor SHALL not send Step output to the LLM for post-step decision calls
6. WHEN the no-llm-context flag is set, THE Executor SHALL continue to the next Step without LLM decision-making
7. THE Classifier SHALL always use the LLM for initial step classification regardless of the no-llm-context flag

### Requirement 6: Generate Tamper-Evident Audit Logs

**User Story:** As an SRE manager, I want every executed runbook to produce an audit log showing who approved what and when, so that we can review incidents post-mortem and meet compliance requirements.

#### Acceptance Criteria

1. THE Executor SHALL write every action to a structured JSON Audit_Log file
2. EACH Audit_Entry SHALL include action type, timestamp, Step details, command, output, exit code, duration, and approver ID
3. EACH Audit_Entry SHALL include a hash of the previous Audit_Entry
4. THE Audit_Log SHALL be append-only and THE Executor SHALL never overwrite an existing Audit_Log file
5. WHEN an Audit_Entry is added, THE Executor SHALL compute the hash using the previous entry's hash
6. THE Executor SHALL record parse, classify, execute, approve, deny, skip, and abort actions
7. WHEN execution completes, THE Executor SHALL write a summary Audit_Entry
8. THE Executor SHALL write Audit_Log files to a configurable directory, defaulting to ./audit-logs/
9. THE Executor SHALL name each Audit_Log file using the format {incident-id}-{timestamp}.json
10. WHEN no incident-id is provided, THE Executor SHALL use the Runbook filename as the identifier in the Audit_Log filename

### Requirement 7: Support Dry-Run Mode

**User Story:** As a DevOps engineer, I want to dry-run a runbook to see exactly what would happen, so that I can build trust before executing real production commands.

#### Acceptance Criteria

1. WHEN the dry-run flag is set, THE Executor SHALL parse and classify all Steps without executing commands
2. WHEN the dry-run flag is set, THE Executor SHALL display each Step with its Risk_Level and whether it would require approval
3. WHEN the dry-run flag is set, THE Executor SHALL show the LLM's classification reasoning for each Step
4. WHEN the dry-run flag is set, THE Executor SHALL write an Audit_Log file marked with mode: dry_run
5. WHEN the dry-run flag is set, THE Executor SHALL not post to Slack

### Requirement 8: Execute Shell Commands Safely

**User Story:** As a DevOps engineer, I want commands to run with timeouts and output capture, so that hung processes don't block incident response.

#### Acceptance Criteria

1. WHEN a Step command is executed, THE Shell_Executor SHALL run it via subprocess
2. THE Shell_Executor SHALL capture stdout and stderr separately
3. THE Shell_Executor SHALL record the exit code
4. THE Shell_Executor SHALL measure execution duration
5. WHEN a command exceeds the timeout, THE Shell_Executor SHALL terminate the process
6. THE default timeout SHALL be 5 minutes per Step
7. WHERE a custom timeout is configured, THE Shell_Executor SHALL use the custom timeout

### Requirement 9: Provide CLI Interface

**User Story:** As a DevOps engineer, I want a clear CLI interface with subcommands, so that I can run, validate, and replay runbooks.

#### Acceptance Criteria

1. THE CLI SHALL provide a run subcommand that executes a Runbook
2. THE run subcommand SHALL accept a Runbook file path as a required argument
3. THE run subcommand SHALL accept optional flags: dry-run, incident-id, auto-approve
4. THE CLI SHALL provide a validate subcommand that parses and classifies without executing
5. THE validate subcommand SHALL display each Step with its Risk_Level
6. THE CLI SHALL provide a replay subcommand that displays a previous execution from an Audit_Log
7. THE replay subcommand SHALL accept an Audit_Log file path as a required argument
8. WHEN an invalid subcommand is provided, THE CLI SHALL display usage help

### Requirement 10: Integrate with Anthropic LLM

**User Story:** As a DevOps engineer, I want the Executor to use Claude for classification and decision-making, so that step analysis is intelligent and context-aware.

#### Acceptance Criteria

1. THE Executor SHALL use the Anthropic API for LLM calls
2. THE Executor SHALL use claude-sonnet-4-5 as the default model
3. THE Executor SHALL read the API key from the ANTHROPIC_API_KEY environment variable
4. WHEN the ANTHROPIC_API_KEY is not set, THE Executor SHALL return an error and halt
5. WHEN an LLM API call fails, THE Executor SHALL retry up to 3 times with exponential backoff
6. WHEN all retries fail, THE Executor SHALL return an error and halt and request human direction per REQ 11

### Requirement 11: Handle Step Execution Failures

**User Story:** As a DevOps engineer, I want the Executor to pause and ask for direction when a step fails, so that I can decide whether to continue, retry, or abort.

#### Acceptance Criteria

1. WHEN a Step exits with a non-zero exit code, THE Executor SHALL pause execution
2. WHEN a Step times out, THE Executor SHALL pause execution
3. WHEN an LLM API error occurs, THE Executor SHALL pause execution
4. WHEN execution is paused, THE Executor SHALL request human direction via Slack
5. THE failure prompt SHALL offer options: continue, retry, abort, skip
6. WHEN the user selects continue, THE Executor SHALL proceed to the next Step
7. WHEN the user selects retry, THE Executor SHALL re-run the failed Step
8. WHEN the user selects abort, THE Executor SHALL halt execution
9. WHEN the user selects skip, THE Executor SHALL skip the failed Step and continue

### Requirement 12: Load Configuration from File

**User Story:** As a DevOps engineer, I want to set default configuration in a file, so that I don't have to pass the same flags on every run.

#### Acceptance Criteria

1. THE Executor SHALL read configuration from a .runbook-exec.toml file in the project root
2. THE configuration file SHALL support timeout, slack_channel, auto_approve_level, and llm_model settings
3. WHERE a configuration file exists, THE Executor SHALL use its values as defaults
4. WHEN a CLI flag is provided, THE Executor SHALL override the configuration file value
5. WHEN no configuration file exists, THE Executor SHALL use built-in defaults

### Requirement 13: Display Rich Terminal Output

**User Story:** As a DevOps engineer, I want color-coded terminal output during runs, so that I can quickly understand execution status.

#### Acceptance Criteria

1. THE Executor SHALL display each Step with color-coded status
2. WHEN a Step is running, THE Executor SHALL display it in yellow
3. WHEN a Step succeeds, THE Executor SHALL display it in green
4. WHEN a Step fails, THE Executor SHALL display it in red
5. WHEN a Step is skipped, THE Executor SHALL display it in gray
6. WHEN execution completes, THE Executor SHALL display a summary with total Steps, successes, failures, and skips

### Requirement 14: Ensure Deterministic Testing

**User Story:** As a developer, I want the Parser and Classifier to produce reproducible results, so that I can write reliable tests.

#### Acceptance Criteria

1. WHEN the same Runbook is parsed twice, THE Parser SHALL produce identical Step lists
2. WHEN the same Step is classified with a mocked LLM, THE Classifier SHALL produce the same Risk_Level
3. THE Parser SHALL not depend on system state or timestamps
4. THE Classifier SHALL not introduce randomness beyond the LLM response

### Requirement 15: Prevent Data Exfiltration

**User Story:** As a security-conscious engineer, I want to prevent sensitive command output from being sent to third-party LLMs, so that I can use the Executor with confidential systems.

#### Acceptance Criteria

1. THE Executor SHALL provide a no-llm-context flag
2. WHEN the no-llm-context flag is set, THE Executor SHALL not send Step output to the LLM for post-step decision calls
3. WHEN the no-llm-context flag is set, THE Classifier SHALL still use the LLM for initial step classification
4. WHEN the no-llm-context flag is set, THE Executor SHALL display a warning that post-step LLM decision-making is disabled
5. THE Executor SHALL document in the README which data is sent to the LLM

### Requirement 16: Validate Runbooks Without Execution

**User Story:** As a runbook author, I want to validate my runbook to see which steps will auto-run versus require approval, so that I can write runbooks the Executor handles well.

#### Acceptance Criteria

1. THE validate subcommand SHALL parse the Runbook
2. THE validate subcommand SHALL classify each Step
3. THE validate subcommand SHALL display each Step with its Risk_Level
4. THE validate subcommand SHALL indicate whether each Step would require approval
5. THE validate subcommand SHALL not execute any commands
6. THE validate subcommand SHALL not write to the Audit_Log
7. THE validate subcommand SHALL exit with code 0 if validation succeeds

### Requirement 17: Replay Previous Executions

**User Story:** As an SRE manager, I want to replay an audit log to see what happened during a previous runbook execution, so that I can review incidents and identify improvements.

#### Acceptance Criteria

1. THE replay subcommand SHALL read an Audit_Log file
2. THE replay subcommand SHALL display each Audit_Entry in chronological order
3. THE replay subcommand SHALL show Step text, command, Risk_Level, approval status, output, exit code, and duration
4. THE replay subcommand SHALL verify the hash chain integrity
5. WHEN the hash chain is broken, THE replay subcommand SHALL display a warning
6. THE replay subcommand SHALL display the execution summary

### Requirement 18: Package for Distribution

**User Story:** As a user, I want to install runbook-exec via pip, so that I can start using it immediately.

#### Acceptance Criteria

1. THE package SHALL be installable via pip install runbook-exec
2. THE package SHALL include all required dependencies
3. WHEN installed, THE runbook-exec command SHALL be available in the user's PATH
4. THE package SHALL include example runbooks in an examples directory

### Requirement 19: Provide Comprehensive Documentation

**User Story:** As a new user, I want complete documentation with quickstart and examples, so that I can understand how to use runbook-exec.

#### Acceptance Criteria

1. THE project SHALL include a README.md file
2. THE README SHALL include a quickstart section
3. THE README SHALL include configuration documentation
4. THE README SHALL include a recorded terminal demo
5. THE README SHALL document which data is sent to the LLM
6. THE README SHALL include the sample disk-full.md runbook
7. THE README SHALL document the safety bias principle

### Requirement 20: Support CI/CD Integration

**User Story:** As a platform engineer, I want to add runbook validation to PR checks, so that runbook changes are validated before merge.

#### Acceptance Criteria

1. THE project SHALL include a GitHub Action workflow file
2. THE GitHub Action SHALL run runbook-exec validate on runbook files
3. THE GitHub Action SHALL fail the check if validation fails
4. THE GitHub Action SHALL be documented in the README

### Requirement 21: Achieve Test Coverage

**User Story:** As a developer, I want comprehensive test coverage, so that I can trust the Executor's behavior.

#### Acceptance Criteria

1. THE test suite SHALL achieve at least 80% line coverage
2. THE test suite SHALL include unit tests for Parser, Classifier, Shell_Executor, and Audit_Log
3. THE test suite SHALL include integration tests for end-to-end execution
4. THE test suite SHALL include property-based tests for Parser round-trip behavior
5. THE test suite SHALL include property-based tests for Audit_Log hash chain integrity
6. THE test suite SHALL mock LLM and Slack API calls for deterministic testing
