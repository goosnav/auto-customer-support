---
title: Example dispatch policy
category: shipping
audience: customer
status: draft
shipping:
  event: dispatch
  min_days: 8
  max_days: 12
  day_type: calendar
  holidays: []
---
# Dispatch and delivery

For this fictional demonstration business, orders normally ship within 8–12 calendar days after the order is placed. This is the processing and dispatch window, not the delivery window.

Carrier transit begins after dispatch. There is no verified delivery-time policy in this example. The assistant must not calculate an arrival date from the dispatch window alone.

An estimate based on a date supplied by the customer does not confirm an individual order's progress. If the customer needs a definite status, tracking number, or an exception, a person must check the order. If the usual dispatch window has passed, request owner review.
