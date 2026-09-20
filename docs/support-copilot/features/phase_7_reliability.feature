@phase_7 @reliability @learning @eval @operations
Feature: Reliability, response learning, and controlled expansion
  As a support operations lead
  I want grounded response learning, retrieval evaluation, verified backups, and health monitoring
  So that the support copilot operates reliably without autonomous drift or secret leakage

  Scenario: Propose knowledge candidate only from human-confirmed sent response
    Given a client message has received a grounded suggestion
    And the human has edited and explicitly confirmed sending the exact reply
    When a learning candidate is proposed for that sent response
    Then the candidate records the suggestion ID, exact sent text, source versions, and safe metadata
    And no candidate can be created from an unconfirmed suggestion
    And the candidate lifecycle status is "proposed"

  Scenario: Rejecting a learning candidate has no knowledge effect
    Given a proposed learning candidate
    When an operator rejects the candidate
    Then the candidate lifecycle status is "rejected"
    And no new knowledge article or version is created
    And an audit event is recorded

  Scenario: Approving a learning candidate creates an immutable knowledge version
    Given a proposed learning candidate
    When an authorized approver reviews the candidate with decision "approve"
    Then a new immutable knowledge version or draft article is created
    And existing immutable versions remain unchanged
    And the candidate lifecycle status is "approved"
    And repeated approval requests are idempotent
    And an audit event is recorded

  Scenario: Retrieval evaluation measures recall and MRR deterministically
    Given a version-controlled synthetic evaluation dataset with queries and expected article keys
    When the retrieval evaluation runner executes against a populated knowledge base
    Then it reports recall@1, recall@3, and mean reciprocal rank
    And it reports the number of evaluated cases and failed case IDs
    And it rejects an empty or missing evaluation dataset

  Scenario: Online database backup with integrity verification
    Given an active operational database
    When an operator triggers an online backup
    Then an online SQLite backup is created using the SQLite backup API
    And a manifest is generated with schema revision, timestamp, and SHA-256 checksum
    And SQLite integrity check passes on the backup file

  Scenario: Safe restore with pre-flight verification and confirmation requirement
    Given a valid backup archive with manifest and checksum
    When a restore is attempted without the explicit confirmation flag
    Then the restore is refused
    When a restore is executed with the explicit confirmation flag
    Then the backup integrity and checksum are verified in an isolated location
    And a pre-restore safety backup of the active database is created
    And the active database is replaced atomically

  Scenario: Refusal of corrupt or tampered backup
    Given a backup with an altered checksum or corrupt SQLite database
    When a restore is attempted
    Then the restore is safely refused before modifying the active database
    And clear recovery guidance is provided

  Scenario: Operational health dashboard with secret redaction
    When an operator requests the operational health dashboard
    Then the status reports API health, database connectivity, schema revision, integrity check, and provider configuration
    And no API keys, tokens, shared secrets, raw database paths, or customer data are exposed
    And the overall state is reported as healthy, degraded, or unhealthy

  Scenario: Additional channels and outbound actions remain deferred
    Given no new usage evidence has been established for external chat channels
    When checking supported outbound capabilities
    Then autonomous replies and unverified external channels remain deferred
