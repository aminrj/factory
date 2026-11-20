# Threat model: #1

## Assets
The package version string (not sensitive).

## Entry points and trust boundaries
One unauthenticated GET route.

## Data flow to models
None. No model is called.

## Threats and controls
| ID | Category | Threat in this app | Control (in code) | Enforcing test | Decision |
|---|---|---|---|---|---|
| T1 | Web: information disclosure | The endpoint leaks more than the version | Return only the `version` key | Unit test asserts the exact JSON body | ACCEPTED |
| T2 | LLM01 Prompt injection | Not applicable, no model | none | none | REJECTED |

## Accepted risks
The version is public by design.
