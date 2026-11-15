# S1: Streaming chat with a tenant-scoped user

As an AP clerk, I want to ask P2Pilot a question and see the answer appear as it is generated,
so that I am not left waiting on a blank screen.

## Acceptance criteria

```gherkin
Feature: Streaming chat

  Scenario: Answer streams token by token
    Given I am signed in as clerk@acme.test
    When I send "hello"
    Then I receive a run_started event with a conversation id
    And I receive more than one token event
    And the stream ends with run_finished status "ok"

  Scenario: Follow-up keeps the conversation
    Given I sent a message in conversation C
    When I send another message with conversation id C
    Then the run_started event has conversation id C

  Scenario: Model gateway is down
    Given the LiteLLM gateway is unreachable
    When I send "hello"
    Then I receive an error event with code "model_unavailable" and retryable true
    And the stream still ends with run_finished status "error"
    And no internal details (hosts, credentials, stack traces) appear in the stream

  Scenario: Not signed in
    Given I send a request without a bearer token
    Then the API responds 401

  Scenario: Tenant isolation
    Given clerk@acme.test belongs only to tenant "acme"
    When they read vendors, invoices or memberships
    Then no rows from tenant "bharat" are returned
```

Tests: `backend/tests/test_chat_api.py`, `backend/tests/test_auth.py`,
`backend/tests/integration/test_rls.py`, `web/src/sse.test.ts`.
