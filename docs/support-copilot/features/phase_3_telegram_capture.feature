@phase_3 @telegram @read_only
Feature: Read-only Telegram support capture
  As a support professional
  I want approved Telegram chats captured through the official API
  So that I can request assistance without granting message-send authority

  Scenario: Capture a Telegram message with stable identity
    Given the Telegram adapter is limited to an approved chat
    When an inbound text update is received
    Then its update ID becomes the stable provider event identity
    And it is forwarded through the authenticated Core API
    And the adapter never writes SQLite directly

  Scenario: Replay an update safely
    Given an update was already captured
    When Telegram replays the same update ID and payload
    Then Core API idempotency returns the existing capture
    And no duplicate captured event is created

  Scenario: Preserve manual operation while disconnected
    Given the Telegram adapter is stopped or unavailable
    Then manual client-message entry continues independently
    And no stored state is corrupted
