@phase_5 @meeting @explicit_consent
Feature: Consent-based meeting copilot
  As a support professional
  I want speaker-aware meeting notes and proposed actions
  So that I receive assistance without silently recording or inventing commitments

  Scenario: Require explicit consent before starting
    Given no meeting session is active
    When I try to start without acknowledging participant consent
    Then the session is rejected
    And no transcript can be captured

  Scenario: Display uncertain transcript segments
    Given I explicitly started a consented meeting
    When the transcription adapter supplies a low-confidence segment
    Then its speaker label and confidence are stored
    And uncertainty is visibly marked in the transcript and proposed summary

  Scenario: Keep promises and resolutions proposed until approved
    Given a transcript says an issue is resolved or promises a follow-up
    When meeting proposals are generated
    Then no verified outcome or follow-up activity is created
    When I explicitly approve the proposal
    Then one corresponding verified activity event is appended

  Scenario: Apply transcript retention
    Given a stopped meeting has passed its configured retention date
    When retention cleanup runs
    Then raw transcript segments are deleted
    And retained approved proposals no longer reference deleted transcript IDs
