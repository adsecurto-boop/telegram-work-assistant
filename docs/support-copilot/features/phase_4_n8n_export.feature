@phase_4 @n8n @signed @read_only
Feature: Signed n8n report distribution boundary
  As a support professional
  I want n8n to distribute only finalized reports
  So that automation cannot mutate operational truth or access the database

  Scenario: Export a finalized report
    Given a finalized EOD report exists
    When n8n requests it with a current valid HMAC signature and report-export capability
    Then the exact finalized snapshot is returned
    And an audit event is recorded

  Scenario: Suppress a retry replay
    Given a signed export event was processed
    When the same event ID and payload are retried
    Then the previous response is returned
    And no second integration record is created

  Scenario: Reject excessive capability
    When n8n requests database-write or report-finalize capability
    Then the request is rejected
    And no report or activity state changes

  Scenario: Keep n8n outside the database boundary
    Then exported workflow definitions contain no SQLite access
    And n8n communicates only through the signed Core API endpoint
