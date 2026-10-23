"""Reviewed model-visible descriptions for the support tools."""

DESCRIPTIONS: dict[str, str] = {
    "find_transactions": """\
Find transactions belonging to the current customer. Returns matching transaction
  IDs, amounts, types, statuses, and occurrence times.

WHEN TO USE: identifying the payment the customer means when its ID is unknown;
  narrowing candidates by time, type, or status. For a transaction investigation,
  search using the facts already provided before asking for a missing date or
  amount, unless sufficient search results are already available in the conversation.
WHEN NOT TO USE: reading a known transaction (use get_transaction); checking savings
  postings (use get_savings_contributions).
DO NOT USE FOR: accessing another customer's transactions or changing payments.
  Search is an appropriate first step in investigating missing savings, but its
  results alone do not establish the savings outcome or cause.
USAGE: all filters are optional and combined. since must not exceed until.
  Use {} when the customer remembers no payment details. No date, type, amount,
  or transaction ID is required to search. Perform this read-only lookup rather
  than asking permission or merely offering to search.
  Timestamps include a timezone. Customer identity is supplied by the backend. If
  several transactions fit, ask a targeted question using the returned dates and
  amounts rather than selecting arbitrarily. If none fit, ask for a missing detail.
  Do not invent filters. This small simulator returns all matches.
  amount_minor is in kobo; divide by 100 to display NGN, never label raw kobo as NGN.
EXAMPLES:
  - Find completed transfers: {"type":"transfer","status":"completed"}
  - List the customer's transactions: {}
""",
    "get_transaction": """\
Read one transaction belonging to the current customer. Returns payment status,
  type, amount, currency, and occurrence time.

WHEN TO USE: reading known transaction details that are missing or need refreshing.
  Reuse sufficient details already returned by find_transactions.
WHEN NOT TO USE: finding an unknown transaction ID (use find_transactions).
DO NOT USE FOR: proving savings posted, changing a payment, or accessing another
  customer's transaction.
USAGE: supply an ID provided by the customer or returned by find_transactions. A
  completed payment does not prove a savings contribution was due or posted. Amounts
  are in minor currency units.
EXAMPLES:
  - Inspect a candidate: {"transaction_id":"<ID returned by find_transactions>"}
""",
    "get_savings_plan": """\
Read the current customer's savings plan now or at a historical time. Returns its
  version, enabled transaction types, and fixed contribution settings.

WHEN TO USE: checking whether a transaction qualified for savings; inspecting
  current settings before recommending, offering, or proposing changes.
WHEN NOT TO USE: checking actual savings postings (use get_savings_contributions);
  reading processing outcomes (use get_sayt_processing).
DO NOT USE FOR: changing settings, recording consent, or proving savings posted.
USAGE: omit at for the current plan. For historical eligibility, use the
  transaction's occurred_at timestamp. Current settings may differ from those
  effective then. Customer identity comes from the backend.
EXAMPLES:
  - Inspect current settings: {}
  - Inspect historical settings: {"at":"<occurred_at returned by get_transaction>"}
""",
    "get_sayt_processing": """\
Read the recorded savings-processing outcome for a customer transaction. Returns the
  outcome, recorded reason, processing timestamps, and plan version used, or no
  recorded outcome.

WHEN TO USE: investigating whether savings processing occurred and whether it
  excluded, dropped, or processed an event.
WHEN NOT TO USE: checking payment status (use get_transaction); confirming a ledger
  posting (use get_savings_contributions).
DO NOT USE FOR: replaying events, recovering missed contributions, or inferring a
  cause solely from an absent record.
USAGE: supply a transaction ID. An absent record means no outcome is recorded; it
  does not establish why. Compare the recorded outcome with the transaction,
  historical plan, and ledger when relevant.
EXAMPLES:
  - Inspect processing: {"transaction_id":"<verified transaction ID>"}
""",
    "get_savings_contributions": """\
Read savings-ledger contributions associated with a transaction belonging to the
  current customer. Returns posted contributions and amounts.

WHEN TO USE: verifying whether savings actually posted for a specific payment.
WHEN NOT TO USE: determining eligibility (use get_savings_plan); reading processing
  reasons (use get_sayt_processing).
DO NOT USE FOR: creating or reversing savings entries, or explaining missing savings
  from an empty result alone.
USAGE: supply a transaction ID. An empty result means no contribution is recorded.
  The backend verifies transaction ownership, distinguishing an unknown transaction
  from a known transaction with no postings. Amounts are in minor currency units.
EXAMPLES:
  - Check postings: {"transaction_id":"<verified transaction ID>"}
""",
    "ask_policy_question": """\
Get general product guidance supported by policy documents. Returns an answer,
  source references, and retrieval/cache metadata.

WHEN TO USE: answering documented questions about product behaviour, eligibility,
  settings, or withdrawal rules.
WHEN NOT TO USE: checking a customer's actual settings, transaction outcome, or
  savings postings (use the corresponding account tools).
DO NOT USE FOR: authorizing changes, confirming account facts, or submitting
  customer identifiers, secrets, or personal information to shared policy caching.
USAGE: submit a self-contained general policy question. The backend controls
  semantic caching. Preserve qualifications in the returned answer; if evidence is
  missing, do not invent a rule. This tool does not perform account actions.
EXAMPLES:
  - Ask general guidance: {"question":"How do I change my weekly card spending
  limit?"}
  - Ask about product behaviour: {"question":"Can I pause Save As You Transact?"}
""",
    "propose_savings_plan_change": """\
Create a pending proposal for the complete desired set of savings-trigger
  transaction types. Returns a proposal ID and exact before/after settings. Does not
  change the plan.

WHEN TO USE: the customer intends a settings change and you have inspected the
  current plan.
WHEN NOT TO USE: the customer only wants an explanation; no current settings
  change is needed; the requested outcome is outside this tool’s capabilities.
  This changes future eligibility only, not historical contributions.
DO NOT USE FOR: recording consent, applying changes, changing contribution amounts,
  or recovering historical missed savings.
USAGE: supply the full desired set, not just additions. Preserve existing types
  unless the customer intends their removal. An empty list disables every trigger. A
  replacement supersedes the session's previous pending proposal. Customer and
  session identity are backend supplied. The backend rejects a proposal with no
  actual change.
EXAMPLES:
  - If the inspected plan enables only card payments and the customer wants
  transfers added: {"enabled_transaction_types":["card_payment","transfer"]}
""",
    "request_plan_change_approval": """\
Make an existing proposal available for customer approval. Returns a pending
  approval card; the conversation can continue while a decision is pending.

WHEN TO USE: an existing proposal exactly matches the intended change discussed with
  the customer.
WHEN NOT TO USE: no proposal exists (use propose_savings_plan_change); the customer
  only wants an explanation.
DO NOT USE FOR: approving on the customer's behalf, changing the proposal, bypassing
  consent, or treating a chat message as application-recorded approval.
USAGE: supply the proposal ID returned by propose_savings_plan_change. The
  application displays the stored before/after settings. An Approve action triggers
  backend validation and application; Decline leaves the plan unchanged. This tool
  returning a card means approval is pending, not that the change succeeded. Report
  success only after a later application event confirms it. Superseded or stale
  proposals require a fresh proposal and approval.
EXAMPLES:
  - Present the proposed change: {"proposal_id":"<ID returned by
  propose_savings_plan_change>"}
""",
}
