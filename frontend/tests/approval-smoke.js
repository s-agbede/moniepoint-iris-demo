// Playwright CLI run-code function. All fabricated responses stay in this test.
// Exercises successful application even when explanation fails, and exact retry.
async (page) => {
  const session = {
    id: "ui-fixture-session",
    customer_id: "customer-001",
    created_at: "2026-10-21T14:00:00Z",
    use_cache: true,
    use_memory: false,
    use_history: true,
  };
  const proposal = {
    id: "ui-fixture-proposal",
    status: "pending",
    presented: true,
    before_enabled_transaction_types: ["card_payment"],
    enabled_transaction_types: ["card_payment", "transfer"],
  };
  const initial = {
    session_id: session.id,
    turn_id: "fixture-turn",
    request_id: "fixture-message",
    message: "Enable transfer savings",
    answer: "Please review the change.",
    status: "awaiting_approval",
    error: null,
    model: "test-fixture",
    trace: [],
    approval: proposal,
    event_kind: "message",
    proposal_id: null,
    decision: null,
    action: null,
    timings: { total_ms: 100, stages_ms: { model: 90 } },
  };
  let turns = [initial],
    decisions = [],
    pending = proposal;
  await page.route("**/api/support/**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname;
    let data;
    if (path.endsWith("/decision")) {
      decisions.push(request.postDataJSON());
      const retry = decisions.length > 1;
      const result = {
        ...initial,
        turn_id: "fixture-decision",
        request_id: decisions[0].request_id,
        message: "",
        event_kind: "decision",
        decision: "approve",
        proposal_id: proposal.id,
        approval: null,
        status: retry ? "completed" : "failed",
        answer: retry ? "Your change is applied." : null,
        error: retry ? null : "Explanation unavailable",
        action: { status: "applied" },
      };
      turns = [initial, result];
      pending = null;
      data = result;
    } else if (path.endsWith("/sessions")) data = [session];
    else
      data = {
        session,
        turns,
        messages: [],
        approval: pending,
        active_request_id:
          decisions.length === 1 ? decisions[0].request_id : null,
      };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(data),
    });
  });
  try {
    await page.goto("http://127.0.0.1:5173");
    await page
      .getByLabel("CONVERSATION", { exact: true })
      .selectOption(session.id);
    await page
      .getByRole("button", { name: "Approve change", exact: true })
      .click();
    await page
      .getByText("Change applied successfully", { exact: true })
      .waitFor();
    await page
      .getByRole("button", { name: "Retry same request", exact: true })
      .click();
    await page.getByText("Your change is applied.", { exact: true }).waitFor();
    if (
      decisions.length !== 2 ||
      JSON.stringify(decisions[0]) !== JSON.stringify(decisions[1])
    )
      throw new Error("Retry changed decision payload");
    if (
      await page
        .getByRole("button", { name: "Approve change", exact: true })
        .count()
    )
      throw new Error("Applied proposal still actionable");
    return "Applied outcome survives explanation failure; retry reuses exact decision; completed card disabled.";
  } finally {
    await page.unroute("**/api/support/**");
    await page.goto("http://127.0.0.1:5173");
  }
};
