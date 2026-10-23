"use strict";

(() => {
  const $ = (selector) => document.querySelector(selector);
  const uniqueId = () => typeof crypto.randomUUID === "function" ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
  const state = { token: "", status: null, conversations: [], tickets: [], documents: [], outbox: [], selected: null, selectedDoc: null, filter: "all", view: "overview", simulator: [], simulatorId: null, simulatorName: "", simulatorEmail: "", simulatorPending: null, renderId: 0 };
  try { state.token = sessionStorage.getItem("auto_support_admin") || ""; } catch (_) { /* Private browser storage may be unavailable. */ }
  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "text") node.textContent = value;
      else if (key === "class") node.className = value;
      else if (key === "on") for (const [event, handler] of Object.entries(value)) node.addEventListener(event, handler);
      else if (value !== null && value !== undefined) { if (key in node && !key.startsWith("aria-")) node[key] = value; else node.setAttribute(key, value); }
    }
    for (const child of (Array.isArray(children) ? children : [children])) if (child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    return node;
  }
  const text = (tag, value, className = "") => el(tag, { text: value, class: className });
  const badge = (value, tone = "") => text("span", String(value || "unknown").replaceAll("_", " "), `badge ${tone}`);
  const statusBadge = (value) => badge(value, ["open", "pending", "queued", "draft", "needs_human", "waiting_owner", "changed"].includes(value) ? "orange" : ["approved", "resolved", "sent", "active"].includes(value) ? "green" : value === "failed" ? "red" : "");
  const button = (label, action, className = "") => el("button", { class: `button ${className}`, type: "button", text: label, on: { click: action } });
  const linkButton = (label, href, className = "") => el("a", { class: `button ${className}`, href, text: label });
  const formatDate = (value) => { if (!value) return "—"; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(date); };
  const relative = (value) => { if (!value) return "—"; const minutes = Math.floor((Date.now() - new Date(value).getTime()) / 60000); return minutes < 1 ? "Just now" : minutes < 60 ? `${minutes}m ago` : minutes < 1440 ? `${Math.floor(minutes / 60)}h ago` : formatDate(value); };
  function notify(message) { const node = $("#toast"); node.textContent = message; node.hidden = false; clearTimeout(notify.timer); notify.timer = setTimeout(() => { node.hidden = true; }, 5000); }
  function errorMessage(detail) { if (Array.isArray(detail)) return detail.map((item) => item.msg || String(item)).join("; "); return typeof detail === "string" ? detail : "The server could not complete this request."; }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${state.token}`);
    if (options.body && !(options.body instanceof FormData)) { headers.set("Content-Type", "application/json"); options.body = JSON.stringify(options.body); }
    const response = await fetch(`/api/admin${path}`, { ...options, headers, credentials: "same-origin" });
    const value = response.headers.get("content-type")?.includes("json") ? await response.json() : await response.text();
    if (!response.ok) { if (response.status === 401 || response.status === 403) lock(); throw new Error(errorMessage(value.detail || value.error || value)); }
    return value;
  }
  async function busy(node, action) { if (node) node.disabled = true; try { return await action(); } catch (error) { notify(error.message || "Something went wrong."); return undefined; } finally { if (node?.isConnected) node.disabled = false; } }
  async function refreshData() {
    const [status, conversations, tickets, documents, outbox] = await Promise.all([api("/status"), api("/conversations"), api("/tickets"), api("/knowledge"), api("/outbox")]);
    Object.assign(state, { status, conversations: conversations.items || [], tickets: tickets.items || [], documents: documents.items || [], outbox: outbox.items || [] });
    $("#business-name").textContent = status.business_name || "Your workspace";
    const count = status.counts?.tickets_open ?? state.tickets.filter((item) => item.status !== "resolved").length;
    $("#nav-inbox-count").textContent = count;
    $("#nav-inbox-count").hidden = !count;
    $("#last-updated").textContent = `UPDATED ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  }
  function lock() {
    state.token = "";
    state.simulator = [];
    state.simulatorId = null;
    state.selected = null;
    try { sessionStorage.removeItem("auto_support_admin"); } catch (_) { /* no storage */ }
    $("#workspace").hidden = true;
    $("#login-screen").hidden = false;
    $("#admin-token").value = "";
  }
  async function openWorkspace() {
    await refreshData();
    $("#login-screen").hidden = true;
    $("#workspace").hidden = false;
    try { sessionStorage.setItem("auto_support_admin", state.token); } catch (_) { /* token remains in memory */ }
    await render();
  }
  function pageHeader(kicker, title, description, actions = []) {
    return el("div", { class: "page-head" }, [el("div", {}, [text("p", kicker, "eyebrow"), text("h1", title), text("p", description)]), el("div", { class: "page-actions" }, actions)]);
  }
  function panel(title, subtitle, content, action = null) {
    return el("section", { class: "panel" }, [el("div", { class: "panel-head" }, [el("div", {}, [text("h2", title), subtitle ? text("p", subtitle) : null]), action]), content]);
  }
  function empty(title, description, action = null, icon = "↗") {
    return el("div", { class: "empty-state" }, [text("div", icon, "empty-icon"), text("h3", title), text("p", description), action]);
  }
  function readiness() {
    return { knowledge: state.documents.some((doc) => doc.status === "approved"), model: !!state.status?.model?.available, email: !!state.status?.channels?.email, phone: !!state.status?.channels?.phone };
  }
  function renderOverview() {
    const ready = readiness();
    const counts = state.status.counts || {};
    const root = el("div");
    root.append(pageHeader("WORKSPACE / OVERVIEW", "Support, under control.", "A clear view of what your customers need and what needs your attention.", [linkButton("Open customer chat ↗", "/widget"), linkButton("Test an answer ↗", "#test", "primary")]));
    root.append(el("div", { class: "hero-band" }, [text("div", ready.knowledge ? "✓" : "+", "hero-mark"), el("div", {}, [text("h3", ready.knowledge ? (ready.model ? "Your knowledge is ready to answer." : "Knowledge is ready. Language model is offline.") : "Make it yours. Start with your knowledge."), text("p", ready.knowledge ? (ready.model ? "Customer questions use approved documents. Anything unresolved becomes a ticket." : "Approved source excerpts and policy calculations remain available. Connect Ollama for generated answers.") : "Add your business policies and FAQs, review them, then try a customer question.")]), linkButton(ready.knowledge ? "View setup ↗" : "Add knowledge ↗", ready.knowledge ? "#setup" : "#knowledge", "small")]));
    const stats = [["Conversations", counts.conversations ?? state.conversations.length, "ACROSS ALL CHANNELS", "↔"], ["Needs your attention", counts.tickets_open ?? state.tickets.filter((item) => item.status !== "resolved").length, "OPEN FOLLOW-UPS", "↗"], ["Approved documents", counts.documents_approved ?? state.documents.filter((item) => item.status === "approved").length, "AVAILABLE TO THE AGENT", "≡"], ["Email outbox", counts.outbox_pending ?? state.outbox.filter((item) => ["queued", "pending", "draft"].includes(item.status)).length, "AWAITING DELIVERY", "↗"]];
    root.append(el("div", { class: "stats-grid" }, stats.map(([label, value, detail, icon], index) => el("div", { class: "stat" }, [el("div", { class: "stat-top" }, [text("span", label), text("span", icon, "stat-icon")]), text("div", value, "stat-value"), text("div", detail, `stat-bottom ${index === 1 && value ? "attention" : ""}`)]))));
    const openTickets = state.tickets.filter((item) => item.status !== "resolved").slice(0, 5);
    const activity = openTickets.length ? ticketTable(openTickets) : empty("No open follow-ups", "Questions the agent cannot resolve will appear here, with the conversation and a clear next step.", linkButton("Try an escalation ↗", "#test", "small"), "✓");
    const steps = [[ready.knowledge, "01", "Give the agent its knowledge", "Add and approve your policies, products, and FAQs.", "#knowledge", "Open knowledge"], [ready.model, "02", "Connect a local model", "Ollama handles responses on your own machine.", "#setup", "Check setup"], [false, "03", "Test a real customer question", "Check the answer, evidence, and human handoff.", "#test", "Open test desk"]];
    const stepNodes = el("div", { class: "setup-steps" }, steps.map(([done, number, title, description, href, label]) => el("div", { class: "setup-step" }, [text("span", done ? "✓" : number, `step-number ${done ? "done" : ""}`), el("div", {}, [text("h3", title), text("p", description), el("a", { href, text: `${label} ↗` })])])));
    const channels = el("div", { class: "panel-body" }, [["Web chat", true, "↔"], ["Email", ready.email, "✉"], ["Phone", ready.phone, "⌁"]].map(([name, active, symbol]) => el("div", { class: "channel-row" }, [el("span", { class: "channel-name" }, [text("span", symbol, "channel-symbol"), name]), badge(active ? "ENABLED" : "NOT CONNECTED", active ? "green" : "")])));
    root.append(el("div", { class: "overview-grid" }, [el("div", {}, [panel("Needs a human", "The questions that need your judgment.", activity, linkButton("View inbox ↗", "#inbox", "small")), panel("Recent conversations", "Every interaction, with its context.", state.conversations.length ? conversationTable(state.conversations.slice(0, 4)) : empty("Your inbox starts here", "Once a customer reaches out through chat, email, or phone, their conversation will appear in this workspace.", null, "↔"))]), el("div", {}, [panel("Your first good answer", "A small setup. A useful starting point.", stepNodes), panel("Channels", "Configured connections", channels)])]));
    return root;
  }
  function ticketTable(tickets) {
    const table = el("table", {}, [el("thead", {}, [el("tr", {}, ["CUSTOMER / QUESTION", "STATUS", "UPDATED"].map((label) => text("th", label)))])]);
    table.append(el("tbody", {}, tickets.map((ticket) => el("tr", {}, [el("td", {}, [el("button", { class: "row-link", text: ticket.subject || ticket.reason || "Customer follow-up", on: { click: () => { state.selected = ticket.conversation_id; location.hash = "inbox"; if (state.view === "inbox") render(); } } }), text("span", ticket.customer_email || "Contact not yet provided", "cell-sub")]), el("td", {}, statusBadge(ticket.status)), text("td", relative(ticket.updated_at || ticket.created_at), "row-time")]))));
    return el("div", { class: "table-wrap" }, table);
  }
  function conversationTable(conversations) {
    return el("div", { class: "table-wrap" }, el("table", {}, el("tbody", {}, conversations.map((conversation) => el("tr", {}, [el("td", {}, [el("button", { class: "row-link", text: conversation.customer_name || conversation.customer_email || "Visitor", on: { click: () => { state.selected = conversation.id; location.hash = "inbox"; } } }), text("span", conversation.preview || "Conversation started", "cell-sub")]), el("td", {}, badge(conversation.channel || "web")), text("td", relative(conversation.updated_at || conversation.created_at), "row-time")])))));
  }
  function messageNode(message) {
    const role = ["user", "customer"].includes(message.role) ? "customer" : ["owner", "human"].includes(message.role) ? "owner" : "assistant";
    const labels = { customer: "CUSTOMER", owner: "TEAM REPLY", assistant: "SUPPORT AGENT" };
    const sources = message.sources || [];
    return el("article", { class: `message ${role}` }, [el("div", { class: "message-header" }, [labels[role], message.created_at ? text("span", formatDate(message.created_at)) : null]), text("div", message.content || message.text || "", "message-body"), sources.length ? el("div", { class: "source-list", "aria-label": "Answer sources" }, sources.map((source) => text("span", source.title || "Approved source", "source-chip"))) : null]);
  }
  async function renderInbox(root, renderId) {
    root.append(pageHeader("WORKSPACE / CONVERSATIONS", "The whole conversation.", "Review the context, handle exceptions, and follow up with an answer."));
    let conversations = state.conversations;
    if (state.filter === "open") conversations = conversations.filter((conversation) => state.tickets.some((ticket) => ticket.conversation_id === conversation.id && ticket.status !== "resolved"));
    else if (state.filter !== "all") conversations = conversations.filter((conversation) => conversation.channel === state.filter);
    const list = el("div", { class: "conversation-list" });
    const filters = el("div", { class: "filters" }, [["all", "All"], ["open", "Needs you"], ["web", "Web"], ["email", "Email"], ["phone", "Phone"]].map(([value, label]) => el("button", { class: `filter ${state.filter === value ? "active" : ""}`, text: label, type: "button", on: { click: () => { state.filter = value; render(); } } })));
    if (!conversations.length) list.append(empty(state.filter === "all" ? "No conversations yet" : "Nothing in this view", "Customer conversations will appear here when this channel receives a message.", null, "↔"));
    for (const conversation of conversations) list.append(el("button", { class: `conversation-row ${state.selected === conversation.id ? "selected" : ""}`, type: "button", on: { click: () => { state.selected = conversation.id; render(); } } }, [el("div", { class: "conversation-top" }, [text("span", conversation.customer_name || conversation.customer_email || "Visitor", "conversation-name"), text("span", relative(conversation.updated_at || conversation.created_at), "row-time")]), text("p", conversation.preview || "Conversation started"), el("div", { class: "conversation-meta" }, [badge(conversation.channel || "web"), statusBadge(conversation.status || "active")])]));
    const detail = el("section", { class: "panel" });
    root.append(el("div", { class: "inbox-layout" }, [el("section", { class: "panel" }, [filters, list]), detail]));
    if (!state.selected) { detail.append(empty("Pick up where the agent left off", "Select a conversation to read its history, inspect the evidence, and respond to a follow-up.", null, "↗")); return; }
    detail.append(text("div", "Loading conversation", "loading"));
    try {
      const conversation = await api(`/conversations/${encodeURIComponent(state.selected)}`);
      if (state.renderId !== renderId) return;
      detail.replaceChildren();
      const ticket = conversation.ticket || state.tickets.find((item) => item.conversation_id === conversation.id);
      detail.append(el("div", { class: "panel-head" }, [el("div", {}, [text("h2", conversation.customer_name || conversation.customer_email || "Customer conversation"), text("p", conversation.customer_email || "No email provided")]), badge(conversation.channel || "web")]));
      if (ticket) detail.append(el("div", { class: "ticket-info" }, [statusBadge(ticket.status), text("p", ticket.reason || ticket.subject || "The customer requested a follow-up."), text("p", ticket.followup_due_at ? `Follow-up due ${formatDate(ticket.followup_due_at)}` : "Track this ticket until the customer has an answer.", "fine-print")]));
      const messages = el("div", { class: "message-stack" }, (conversation.messages || []).map(messageNode));
      detail.append(messages);
      if (ticket) {
        const textarea = el("textarea", { id: "owner-reply", required: true, maxlength: 8000, placeholder: "Write the answer the customer needs…", rows: 4 });
        const resolve = el("input", { type: "checkbox", checked: true });
        const submit = el("button", { type: "submit", class: "button primary", text: conversation.customer_email ? "Queue reply ↗" : "Reply in chat ↗" });
        const form = el("form", { class: "reply-form", on: { submit: (event) => { event.preventDefault(); if (!textarea.value.trim()) return; busy(submit, async () => { const result = await api(`/tickets/${encodeURIComponent(ticket.id)}/reply`, { method: "POST", body: { message: textarea.value.trim(), resolve: resolve.checked } }); notify(result.message || (conversation.customer_email ? "Reply recorded. Check Email delivery for the queued email." : "Reply added to the conversation.")); await refreshData(); await render(); }); } } }, [el("label", { htmlFor: "owner-reply", text: "Your reply" }), textarea, el("div", { class: "form-actions" }, [el("label", { class: "checkbox-label" }, [resolve, "Mark resolved after replying"]), submit]), text("p", conversation.customer_email ? "Replies also appear in the customer chat. Email delivery follows your configured outbox settings." : "The customer can see this reply in their original chat. No email address is available for an email follow-up.", "subtext")]);
        detail.append(form);
        if (ticket.status !== "resolved") detail.append(el("div", { class: "panel-body" }, button("Mark resolved without reply", (event) => busy(event.currentTarget, async () => { await api(`/tickets/${encodeURIComponent(ticket.id)}`, { method: "PATCH", body: { status: "resolved" } }); await refreshData(); await render(); notify("Ticket marked resolved."); }), "small")));
      } else detail.append(el("div", { class: "panel-body" }, text("p", "This conversation has no escalation ticket. The customer can request a human follow-up at any time.", "subtext")));
      messages.scrollTop = messages.scrollHeight;
    } catch (error) { if (state.renderId === renderId) detail.replaceChildren(text("div", error.message, "field-error")); }
  }
  async function uploadFiles(files, control) {
    if (!files.length) { notify("Choose one or more Markdown files first."); return; }
    if ([...files].some((file) => !file.name.toLowerCase().endsWith(".md"))) { notify("Choose Markdown (.md) files only."); return; }
    await busy(control, async () => {
      const form = new FormData();
      for (const file of files) form.append("files", file);
      const result = await api("/knowledge/upload", { method: "POST", body: form });
      await refreshData(); await render();
      notify(result.message || "Documents imported for review. Approve each document before it can answer customers.");
    });
  }
  async function renderKnowledge(root, renderId) {
    root.append(pageHeader("WORKSPACE / KNOWLEDGE", "What your agent knows.", "Your policies are the source of truth. Review new or changed documents before they answer customers.", [button("Scan inbox folder ↻", (event) => busy(event.currentTarget, async () => { const result = await api("/knowledge/ingest", { method: "POST" }); await refreshData(); await render(); notify(result.message || `Inbox scanned. ${Array.isArray(result.imported) ? result.imported.length : result.imported || 0} documents imported.`); }), "small")]));
    const fileInput = el("input", { type: "file", accept: ".md,text/markdown", multiple: true, id: "knowledge-files", "aria-label": "Choose Markdown documents" });
    const uploadButton = button("Import documents ↗", (event) => uploadFiles(fileInput.files, event.currentTarget), "primary small");
    const drop = el("div", { class: "upload-zone", on: { dragover: (event) => { event.preventDefault(); drop.classList.add("dragging"); }, dragleave: () => drop.classList.remove("dragging"), drop: (event) => { event.preventDefault(); drop.classList.remove("dragging"); uploadFiles(event.dataTransfer.files, uploadButton); } } }, [text("div", "↥", "upload-symbol"), text("h3", "Drop in your business knowledge"), text("p", "Markdown files. Your business, products, policies, and answers."), fileInput, uploadButton]);
    const table = el("table", {}, el("thead", {}, el("tr", {}, ["DOCUMENT", "STATUS", ""].map((label) => text("th", label)))));
    const rows = el("tbody");
    for (const doc of state.documents) rows.append(el("tr", {}, [el("td", {}, [el("button", { class: "row-link", text: doc.title || doc.source || "Untitled document", on: { click: () => { state.selectedDoc = doc.id; render(); } } }), text("span", `${doc.category || "general"} · ${formatDate(doc.updated_at)}`, "cell-sub")]), el("td", {}, statusBadge(doc.status)), el("td", {}, !["approved", "expired", "archived"].includes(doc.status) ? button("Review ↗", () => { state.selectedDoc = doc.id; render(); }, "small") : null)]));
    table.append(rows);
    const right = el("div");
    const intro = el("div", { class: "panel-body knowledge-description" }, [text("p", "A small, accurate knowledge base works better than a large pile of guesses."), el("ul", {}, ["Describe your products and services.", "State shipping windows, returns, and exceptions.", "Mark uncertain information as unknown.", "Keep private business notes out of customer answers."].map((item) => text("li", item))), button("Copy onboarding prompt ↗", (event) => busy(event.currentTarget, async () => { const prompt = await api("/onboarding-prompt"); await copyText(typeof prompt === "string" ? prompt : prompt.prompt || ""); notify("Onboarding prompt copied. Paste it into your preferred chat assistant."); }), "small"), text("p", "Use the prompt to build a knowledge pack through a guided interview, then import the resulting Markdown files.", "subtext")]);
    right.append(panel("Start with a guided interview", "A useful first knowledge pack.", intro));
    const preview = el("section", { class: "panel" });
    if (state.selectedDoc) right.append(preview);
    root.append(el("div", { class: "knowledge-layout" }, [el("div", {}, [drop, panel("Documents", `${state.documents.length} total · ${state.documents.filter((doc) => doc.status === "approved").length} approved`, state.documents.length ? el("div", { class: "table-wrap" }, table) : empty("No documents yet", "Upload a Markdown file, or drop files into your knowledge inbox and scan the folder.", null, "≡"))]), right]));
    if (state.selectedDoc) {
      preview.append(text("div", "Loading document", "loading"));
      try {
        const doc = await api(`/knowledge/${encodeURIComponent(state.selectedDoc)}`);
        if (state.renderId !== renderId) return;
        preview.replaceChildren(el("div", { class: "panel-head" }, [text("h2", doc.title || "Document review", "doc-title"), statusBadge(doc.status)]), text("pre", doc.content || "", "doc-preview"));
        if (!["approved", "expired", "archived"].includes(doc.status)) preview.append(el("div", { class: "panel-body" }, [text("p", "Approval makes this document available in customer answers. Check the facts, dates, and any sensitive content.", "subtext"), button("Approve for customer answers ✓", (event) => busy(event.currentTarget, async () => { await api(`/knowledge/${encodeURIComponent(doc.id)}/approve`, { method: "POST" }); await refreshData(); await render(); notify("Document approved."); }), "primary small")]));
        if (doc.status !== "archived") preview.append(el("div", { class: "panel-body" }, button("Archive · remove from customer answers", (event) => busy(event.currentTarget, async () => { await api(`/knowledge/${encodeURIComponent(doc.id)}/archive`, { method: "POST" }); await refreshData(); state.selectedDoc = null; await render(); notify("Document archived. It is no longer available for customer answers."); }), "small")));
      } catch (error) { if (state.renderId === renderId) preview.replaceChildren(text("div", error.message, "field-error")); }
    }
  }
  function renderTest() {
    const root = el("div");
    root.append(pageHeader("WORKSPACE / TEST DESK", "Ask before they do.", "Try a customer question and check the answer, sources, and escalation behavior.", [button("New conversation ↻", () => { state.simulator = []; state.simulatorId = null; state.simulatorName = ""; state.simulatorEmail = ""; state.simulatorPending = null; render(); }, "small"), linkButton("Try voice chat ↗", "/widget", "small")]));
    root.append(text("div", "Tests create real local conversations and follow-up tickets. Customer emails are optional; leave the field empty for a local-only test.", "notice neutral"));
    const log = el("div", { class: "message-stack simulator-log", id: "simulator-log" });
    if (!state.simulator.length) log.append(empty("Give it a real question", "Start with a policy question. Then try a missing answer or ask to speak with the owner.", null, "↔"));
    else for (const message of state.simulator) log.append(messageNode(message));
    const input = el("textarea", { id: "sim-message", rows: 3, maxlength: 6000, placeholder: "For example: I ordered 4 days ago. When should it ship?", required: true });
    const email = el("input", { id: "sim-email", type: "email", placeholder: "Optional, for a follow-up", autocomplete: "off", value: state.simulatorEmail, disabled: !!state.simulatorId });
    const name = el("input", { id: "sim-name", placeholder: "Test customer", maxlength: 100, autocomplete: "off", value: state.simulatorName, disabled: !!state.simulatorId });
    const submit = el("button", { class: "button primary", type: "submit", text: "Send question ↗" });
    const form = el("form", { class: "simulator-form", on: { submit: (event) => { event.preventDefault(); const message = input.value.trim(); if (!message) return; busy(submit, async () => {
      if (state.simulatorPending?.message !== message) state.simulatorPending = { message, id: uniqueId() };
      const response = await api("/simulate", { method: "POST", body: { message, conversation_id: state.simulatorId || undefined, customer_email: email.value.trim() || undefined, customer_name: name.value.trim() || undefined, client_message_id: state.simulatorPending.id } });
      state.simulatorId = response.conversation_id;
      state.simulatorName = name.value.trim(); state.simulatorEmail = email.value.trim(); state.simulatorPending = null;
      state.simulator.push({ role: "user", content: message }, { role: "assistant", content: response.reply || response.answer, sources: response.sources || [] });
      await refreshData(); await render();
      if (response.needs_human) notify(`Follow-up ticket created${response.ticket_id ? ` · ${String(response.ticket_id).slice(0, 8)}` : ""}. Review it in Conversations.`);
      else notify(response.mode === "knowledge-only" ? "Answered from approved knowledge. The language model was not used." : "Answer ready. Review the cited sources.");
    }); } } }, [el("div", { class: "input-row" }, [el("div", {}, [el("label", { htmlFor: "sim-name", text: "Customer name" }), name]), el("div", {}, [el("label", { htmlFor: "sim-email", text: "Contact email" }), email])]), el("label", { htmlFor: "sim-message", text: "Customer question" }), input, el("div", { class: "form-actions" }, [text("span", "Uses your currently approved knowledge.", "subtext"), submit])]);
    const suggestions = ["I ordered 4 days ago. When should it ship?", "What is your return policy?", "Can you check the exact status of my order?", "I'd like a person to follow up with me."];
    const prompts = el("div", { class: "panel-body" }, [el("div", { class: "suggestions" }, suggestions.map((message) => el("button", { class: "suggestion", type: "button", on: { click: () => { input.value = message; input.focus(); } } }, [message, text("span", "↗")]))), el("div", { class: "test-note" }, [el("strong", { text: "Look for a useful answer, not just a confident one." }), text("p", "An estimate should say what it is based on. An exact order status needs verified order data. A missing answer should create a clear human follow-up.")])]);
    root.append(el("div", { class: "test-layout" }, [panel("Customer simulator", state.simulatorId ? `Conversation ${String(state.simulatorId).slice(0, 8)}` : "A new conversation", el("div", {}, [log, form]), badge(state.status.model?.available ? "MODEL CONNECTED" : "KNOWLEDGE ONLY", state.status.model?.available ? "green" : "orange")), el("div", {}, [panel("Try these questions", "Test answers and the exception path.", prompts), panel("Voice testing", "Speak through the customer widget.", el("div", { class: "panel-body" }, [text("p", "The microphone sends audio to your server. Local transcription and speech playback must be configured first.", "subtext"), linkButton("Open customer chat ↗", "/widget", "small")]))])]));
    requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
    return root;
  }
  function renderOutbox() {
    const root = el("div");
    root.append(pageHeader("WORKSPACE / EMAIL DELIVERY", "Follow through.", "Review queued messages and see whether each customer follow-up was actually sent."));
    const enabled = !!state.status.email_delivery_enabled;
    root.append(text("div", enabled ? "Email delivery is enabled. Messages marked sent have been handed to your configured SMTP server; this does not confirm receipt in the customer's inbox." : "Email delivery is disabled. Drafts stay here until you configure SMTP and explicitly enable delivery. Customer chat replies remain available in their conversation.", enabled ? "notice neutral" : "notice"));
    const body = el("div");
    for (const item of state.outbox) {
      const canSend = enabled && ["pending", "queued", "draft", "failed"].includes(item.status);
      body.append(el("article", { class: "outbox-card" }, [el("div", { class: "outbox-head" }, [el("div", {}, [text("h3", item.subject || "Customer follow-up"), text("div", `TO ${item.to_address || item.to || "No recipient"}`, "outbox-to")]), statusBadge(item.status)]), text("div", item.body || "", "outbox-body"), item.last_error ? text("p", item.last_error, "error-text") : null, el("div", { class: "outbox-actions" }, [text("span", `${item.kind || "reply"} · ${formatDate(item.created_at)}`, "subtext"), canSend ? button(item.status === "failed" ? "Retry delivery ↗" : "Send email ↗", (event) => busy(event.currentTarget, async () => { const result = await api(`/outbox/${encodeURIComponent(item.id)}/send`, { method: "POST" }); await refreshData(); await render(); notify(result.message || "Delivery attempt completed. Check the message status."); }), "primary small") : text("span", item.status === "sent" ? "HANDED TO SMTP" : "DELIVERY NOT AVAILABLE", "fine-label")]) ]));
    }
    root.append(panel("Outbox", `${state.outbox.length} recorded messages`, state.outbox.length ? body : empty("No email waiting", "Owner notifications and customer follow-ups will appear here when a conversation needs a human answer.", linkButton("Configure email ↗", "#setup", "small"), "✉")));
    return root;
  }
  function setting(label, value, tone = null) { return el("div", { class: "config-field" }, [text("span", label), tone !== null ? badge(value, tone) : text("span", String(value ?? "Not configured"), "config-value")]); }
  async function copyText(value) { if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(value); return; } const area = el("textarea", { value, style: "position:fixed;left:-10000px" }); document.body.append(area); area.select(); const copied = document.execCommand("copy"); area.remove(); if (!copied) throw new Error("Clipboard is unavailable. Select and copy the text directly."); }
  function renderSetup() {
    const root = el("div");
    const status = state.status;
    const ready = readiness();
    root.append(pageHeader("WORKSPACE / SETUP", "Keep it in your hands.", "Control the model, business knowledge, and connections your support team relies on.", [button("Refresh connections ↻", (event) => busy(event.currentTarget, async () => { await refreshData(); await render(); notify("Connection status refreshed."); }), "small")]));
    const model = el("div", { class: "panel-body" }, [setting("Provider", status.model?.provider || "ollama"), setting("Model", status.model?.name), setting("Connection", ready.model ? "AVAILABLE" : "NOT REACHABLE", ready.model ? "green" : "orange"), text("p", "Run Ollama on the same machine or set its address in your environment configuration. The app uses approved source excerpts if the model is unavailable.", "setup-copy"), text("pre", `ollama pull ${status.model?.name || "qwen3:8b"}\nollama serve`, "code-block")]);
    const stt = status.voice?.stt;
    const tts = status.voice?.tts;
    const voice = el("div", { class: "panel-body" }, [setting("Transcription", typeof stt === "object" ? `${stt.provider || "Local STT"} · ${stt.model || ""}` : stt || "Not configured"), setting("Microphone input", stt?.enabled && stt?.installed ? "CONFIGURED" : stt?.enabled ? "INSTALL VOICE EXTRA" : "DISABLED", stt?.enabled && stt?.installed ? "green" : ""), setting("Speech output", typeof tts === "object" ? (tts.configured ? tts.provider || "Local TTS configured" : "Not configured") : tts || "Not configured"), text("p", "Browser voice uses push-to-talk: record, transcribe locally, answer, and play local speech. Microphone access requires localhost or HTTPS. Configuration does not verify that model downloads or the speech server are ready.", "setup-copy"), text("p", "Phone calls need a configured provider and a public HTTPS endpoint. The MVP phone adapter uses provider speech services; number rental and usage can incur fees.", "setup-copy")]);
    const channels = el("div", { class: "panel-body" }, [setting("Web chat", "ENABLED", "green"), setting("Incoming email", ready.email ? "CONFIGURED" : "NOT CONFIGURED", ready.email ? "green" : ""), setting("Outbound email", status.email_delivery_enabled ? "ENABLED" : "DISABLED", status.email_delivery_enabled ? "green" : ""), setting("Telephone", ready.phone ? "CONFIGURED" : "NOT CONFIGURED", ready.phone ? "green" : ""), text("p", "Configure credentials in your local environment file. The dashboard never displays provider passwords or secret keys. Follow the README for email polling and phone routing.", "setup-copy")]);
    const snippet = `<script src="${(status.public_base_url || location.origin).replace(/\/$/, "")}/static/widget.js" defer><\/script>`;
    const widget = el("div", { class: "panel-body" }, [text("p", "Add this script to a page you control. It opens a small customer chat without giving visitors access to this admin workspace.", "setup-copy"), text("pre", snippet, "code-block"), el("div", { class: "page-actions" }, [button("Copy embed code ↗", (event) => busy(event.currentTarget, async () => { await copyText(snippet); notify("Embed code copied."); }), "small"), linkButton("Preview widget ↗", "/widget", "small")]), text("p", "For a public website, replace the address with your server's public HTTPS address and configure the allowed embedding origins. The server must remain online for customers to receive answers.", "subtext")]);
    root.append(el("div", { class: "detail-grid" }, [panel("Local language model", "Reasoning and response generation", model), panel("Voice", "Speech on your own infrastructure", voice), panel("Channels & delivery", "Opt in to external connections", channels), panel("Add chat to your website", "A small, independent embed", widget)]));
    root.append(el("div", { class: "notice neutral", style: "margin-top:24px;margin-bottom:0" }, "This is an MVP. Test your business policies, escalation paths, mail delivery, and phone behavior before opening access to customers. Your README contains setup, operating limits, and recovery instructions."));
    return root;
  }
  async function render() {
    if (!state.status || !state.token) return;
    const allowed = ["overview", "inbox", "knowledge", "test", "outbox", "setup"];
    state.view = allowed.includes(location.hash.slice(1)) ? location.hash.slice(1) : "overview";
    const renderId = ++state.renderId;
    for (const nav of document.querySelectorAll("[data-view]")) { const active = nav.dataset.view === state.view; nav.classList.toggle("active", active); if (active) nav.setAttribute("aria-current", "page"); else nav.removeAttribute("aria-current"); }
    const page = $("#page-content");
    const container = el("div");
    page.replaceChildren(container);
    if (state.view === "overview") container.append(renderOverview());
    else if (state.view === "inbox") await renderInbox(container, renderId);
    else if (state.view === "knowledge") await renderKnowledge(container, renderId);
    else if (state.view === "test") container.append(renderTest());
    else if (state.view === "outbox") container.append(renderOutbox());
    else container.append(renderSetup());
    document.title = `${({ overview: "Overview", inbox: "Conversations", knowledge: "Knowledge", test: "Test desk", outbox: "Email delivery", setup: "Setup" })[state.view]} · Auto Support`;
  }
  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector("button");
    $("#login-error").textContent = "";
    state.token = $("#admin-token").value.trim();
    submit.disabled = true;
    try { await openWorkspace(); } catch (error) { $("#login-error").textContent = error.message; } finally { submit.disabled = false; }
  });
  $("#sign-out").addEventListener("click", lock);
  $("#mobile-lock").addEventListener("click", lock);
  $("#refresh").addEventListener("click", (event) => busy(event.currentTarget, async () => { await refreshData(); await render(); notify("Workspace updated."); }));
  window.addEventListener("hashchange", () => render());
  if (state.token) openWorkspace().catch((error) => { lock(); $("#login-error").textContent = error.message; });
})();
