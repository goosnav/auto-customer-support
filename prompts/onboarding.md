# Business knowledge interview

Copy the prompt below into your preferred chatbot. It can run locally or in another chat app. This interview does not need access to this support server.

Review every generated document, save the Markdown files in `knowledge/inbox/` (or your configured `KNOWLEDGE_DIR/inbox/`), run ingestion, and approve the resulting documents in the dashboard. An uploaded file cannot approve itself. Do not upload a whole private company brain: prepare the subset of facts that customers may see.

---

You are helping me prepare a customer-facing knowledge base for a local AI support assistant. Interview me, then produce ordinary Markdown documents with YAML frontmatter. Accuracy matters more than completing every field. Do not invent a policy, a price, a contact, a promise, a capability, or a deadline. Mark unconfirmed information in a separate owner-only gap checklist; never turn it into a customer policy.

Ask focused questions in batches of 4–6. Start with the business type, its customers, what they commonly need help with, and which existing policy documents I can supply. Adapt later questions to the answers instead of asking irrelevant questions. When I say I do not know, record the gap and continue. Distinguish what I have actually confirmed from what you are recommending.

Cover these subjects where relevant:

1. Business name, service area, public contact routes, operating hours and timezone, languages, and the voice customers expect.
2. Products or services, specifications, compatibility, exclusions, recurring subscriptions, and links to public references. Distinguish a general product description from live price, stock, or booking availability.
3. Fulfillment: when the processing clock starts, minimum and maximum dispatch times, calendar versus business days, cutoff times, recognized holidays, preorders, product-specific exceptions, carrier transit, destinations, and lost packages. Keep dispatch and arrival separate. If a single structured timing policy cannot represent the actual rules, write the rules in prose and require a person for arithmetic.
4. Returns, refunds, cancellation, changes, damaged/incorrect items, eligibility, the clock's starting event, deadlines, exclusions, required evidence, and who can authorize an exception.
5. Account and order questions: what information identifies a case, how identity is actually verified, and what live systems would need to be connected. An email typed in chat, a phone caller ID, or an order number is not proof of identity.
6. Escalations: which requests always need a person, what information that person needs, preferred customer follow-up channel, any confirmed response-time commitment, and what to say outside operating hours. Do not claim an integration or a service commitment exists unless I confirm it.
7. The ten most common support questions and the exact answers I approve. Ask about contradictory rules and recent changes. For temporary policies, ask for an expiry date.
8. Data boundaries: information customers may see versus internal notes, information never to request, and any retention preferences I have actually chosen. Do not ask me to paste secrets, passwords, API keys, payment-card details, customer lists, or private order histories.

Before generating documents, show a concise list of confirmed policies and unresolved conflicts. Ask me to correct them. Then output one complete fenced Markdown block per file, with its filename outside the block:

- `business.md`
- `products.md`
- `shipping-policy.md` (only if applicable)
- `returns.md` (only if applicable)
- `faq.md`
- `support.md`
- `owner-gaps.md` (internal; never customer knowledge)

Use this frontmatter, filling only verified values:

```yaml
---
title: "A descriptive policy title"
category: shipping
audience: customer
status: draft
updated_at: "YYYY-MM-DD"
---
```

Allowed categories are `business`, `products`, `shipping`, `returns`, `faq`, and `support`. Dates must be real ISO dates, not the literal placeholder above. Use `valid_until: "YYYY-MM-DD"` only for an actual expiry the owner confirms. Internal notes must use `audience: internal`; the server will refuse customer approval of those documents. All other generated files still begin as drafts and require owner approval in the application. Do not use YAML aliases or custom tags.

For one uniform dispatch policy, include this structured block **only after I explicitly confirm all its values**:

```yaml
shipping:
  event: dispatch
  min_days: 8
  max_days: 12
  day_type: calendar
  holidays: []
```

The numbers above are a format example, not default policy. `min_days` and `max_days` are integers. `day_type` is exactly `calendar` or `business`. Business days mean Monday through Friday, excluding ISO dates listed in `holidays`. A blank holiday list means no holiday calendar is configured. If timing depends on cutoffs, locations, custom schedules, product, preorder status, or another rule not represented here, omit the structured block and explain the limitation. Do not label a dispatch promise as arrival.

Write each customer document as clear statements that may safely be quoted to a customer, with exceptions beside the rule they qualify. Avoid instructions aimed at the language model and avoid duplicate competing policy copies. Keep recommendations and unresolved questions in the internal gap checklist. Finish with a short handoff: review the files, copy customer documents into the inbox, ingest, read them in the dashboard, approve confirmed current documents, and test normal and exception questions.
