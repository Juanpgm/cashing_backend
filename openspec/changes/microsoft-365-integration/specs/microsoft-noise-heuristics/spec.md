# Microsoft Noise Heuristics Specification

## Purpose

Pre-LLM noise heuristics for Microsoft Graph evidence (Outlook mail, Outlook
calendar, OneDrive), mirroring the existing Google heuristics, dispatched by
source/provider. The shared LLM noise-scoring layer remains unchanged.

## Requirements

### Requirement: Outlook email noise scoring mirrors `score_non_personal_email`

The system MUST score Outlook email non-personal/noise likelihood using Graph
categories/`inferenceClassification` (or equivalent), matching the
conservative behavior of the Google heuristic (over-include rather than
under-include).

#### Scenario: Promotional Outlook email scored as noise-likely

- GIVEN an Outlook email classified as "other" `inferenceClassification`
- WHEN the noise heuristic scores it
- THEN it is scored as noise-likely

#### Scenario: Ambiguous email defaults to LLM review

- GIVEN an Outlook email with ambiguous classification signals
- WHEN the heuristic scores it
- THEN it is passed through to the LLM layer rather than silently discarded

### Requirement: Outlook calendar noise detection mirrors `is_noise_calendar`

The system MUST flag Outlook calendar noise events using attendee/
`responseStatus`/`isAllDay` equivalents, applying the same conservative
criteria as the Google heuristic.

#### Scenario: All-day event with no attendee response flagged

- GIVEN an all-day Outlook event with no attendee response recorded
- WHEN the heuristic evaluates it
- THEN it is flagged as noise-likely

#### Scenario: Confirmed meeting not flagged

- GIVEN a normal meeting with confirmed attendee responses
- WHEN the heuristic evaluates it
- THEN it is not flagged as noise

### Requirement: OneDrive noise filtering mirrors `is_noise_drive`

The system MUST apply a folder-based filter to exclude non-evidence OneDrive
folders, matching the Google Drive filter's behavior.

#### Scenario: Excluded-type OneDrive folder filtered out

- GIVEN a file located in a OneDrive folder equivalent to a
  Google-excluded folder type
- WHEN evidence discovery filters drive results
- THEN the file is filtered out before reaching the LLM layer

### Requirement: Heuristic dispatch is by source/provider; LLM layer unchanged

Noise heuristics MUST dispatch to the Google or Microsoft implementation
based on each evidence item's source/provider. The existing
`WORK_NOISE_SYSTEM_PROMPT` LLM layer MUST remain source-agnostic and
unchanged.

#### Scenario: Mixed Google/Microsoft batch scored correctly

- GIVEN a batch containing both Google-sourced and Microsoft-sourced evidence
  items
- WHEN pre-LLM noise scoring runs
- THEN each item is scored by its own provider's heuristic before reaching
  the shared LLM noise-scoring step

## Deferred to sdd-design

- Exact scoring thresholds/weights, dispatch mechanism (registry vs.
  conditional), and field-name mapping table.
