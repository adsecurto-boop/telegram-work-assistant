@phase_2 @local_only @truthful_reports
Feature: Daily memory and truthful reports
  As a support professional
  I want TOD and EOD reports derived from verified work events
  So that suggested text is never misrepresented as completed work

  Scenario: Preview a report from verified activity
    Given verified support and testing events exist for my local calendar day
    When I preview an EOD report
    Then each reported work item references its source activity event
    And suggested and copied replies are excluded from completed work
    And the preview stores a deterministic facts hash

  Scenario: Reject stale report finalization
    Given I created an EOD preview
    And a later activity event was recorded for the same local day
    When I finalize the original preview
    Then finalization is rejected as stale
    And I must create a new preview

  Scenario: Preserve a finalized report across restart
    Given an unchanged report preview exists
    When I explicitly finalize it with its facts hash
    Then the finalized snapshot remains stored with its exact source event IDs
