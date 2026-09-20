@phase_1 @local_only @no_external_send
Feature: Grounded support reply assistance
  As a support professional
  I want reply suggestions grounded in approved knowledge and verified case facts
  So that I can respond efficiently without inventing information or surrendering control

  Background:
    Given the Support Copilot is running locally
    And external message sending is disabled
    And an approved knowledge article exists for requesting attendance logs

  Scenario: Generate a grounded reply from approved knowledge
    Given I enter a client message asking why attendance records are missing
    When I request a reply suggestion
    Then the suggestion contains a draft reply
    And it cites the approved knowledge article and its version
    And it lists any required facts that are missing
    And it is stored with status "suggested"
    And no sent response is created

  Scenario: Require clarification when a case reference is ambiguous
    Given two open cases could match the captured client message
    When I request a reply suggestion for "that case"
    Then no case is selected automatically
    And I am shown the bounded candidate cases
    And no case state is changed

  Scenario: Ignore instructions embedded in a client message
    Given the client message contains instructions to ignore policy and disclose secrets
    When I request a reply suggestion
    Then the client instructions are treated as untrusted content
    And no credential or hidden configuration is included in the model request or reply
    And no capability is granted by the client message

  Scenario: Do not claim unsupported resolution
    Given the case has no verified resolution event
    And the approved article prohibits claiming that the issue is resolved
    When I request a reply suggestion
    Then the draft does not claim that the issue is resolved
    And the unsupported claim check passes before the draft is displayed

  Scenario: Preserve the distinction between copied and sent
    Given a reply suggestion exists
    When I copy the suggestion
    Then its status records that it was copied
    And no sent response is created
    When I explicitly confirm the exact edited text was sent
    Then one sent response is created with that exact text
    And a corresponding activity event is appended

  Scenario: Suppress duplicate sent confirmation
    Given I confirmed a response as sent with correlation ID "send-123"
    When the same confirmation with correlation ID "send-123" is received again
    Then the previous result is returned
    And no second sent response is created
    And no second activity event is appended

  Scenario: Fail safely when the AI provider is unavailable
    Given approved knowledge was retrieved successfully
    And the configured AI provider times out
    When I request a reply suggestion
    Then I am told that generation is temporarily unavailable
    And the approved source material remains available for manual use
    And no fabricated draft or sent response is created

  Scenario: Exclude unapproved knowledge
    Given a draft knowledge article is a closer text match than the approved article
    When I request a reply suggestion
    Then the draft article is not supplied to the AI provider
    And the suggestion cites only approved knowledge versions
