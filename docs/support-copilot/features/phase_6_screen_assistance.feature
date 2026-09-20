@phase_6 @screen @on_demand @no_control
Feature: Consent-based on-demand screen assistance
  As a support professional
  I want evidence-grounded help for one selected support window
  So that I can troubleshoot without granting silent capture or computer control

  Scenario: Require an approved window
    Given screen capture is paused
    When I request a screenshot without selecting a window
    Then capture is rejected
    When I select one enumerated window
    Then only that exact window may produce an on-demand preview

  Scenario: Block sensitive windows and content
    Given a window title or local OCR indicates password, payment, banking, or credential content
    When capture or analysis is requested
    Then the screenshot is withheld from remote analysis
    And no screenshot bytes are persisted

  Scenario: Redact locally before visual analysis
    Given a selected support window contains an email, phone number, account number, or API token
    When I capture one screenshot
    Then local OCR identifies the sensitive text regions
    And the returned preview and remote-model input have those regions blacked out

  Scenario: Ground troubleshooting in approved knowledge
    Given a locally redacted screenshot and approved matching knowledge
    When I explicitly request visual analysis
    Then proposed steps cite immutable approved knowledge versions
    And uncertainty is visible
    And the assistant cannot control the mouse or keyboard

  Scenario: Pause immediately
    Given a window is approved
    When I use the pause button or pause hotkey
    Then the approved window and screenshot preview are cleared
    And another capture requires a new explicit selection
