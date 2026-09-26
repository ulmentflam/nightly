# README Rewrite — Iteration Log

## Iteration 0 — Orchestrator setup
- Inputs ingested: current README (~440 lines), RFCs 001–008 inventory, user positioning brief.
- Snapshot saved to `.planning/drafts/README-iteration-3-pre.md`.
- Compliance checklist written.
- Routing decision: start at Step 1 (Architect) — README is technically complete but mis-positioned for "work while you sleep".

## Iteration 1 — Architect
- Input score: N/A (first pass).
- Action: full rewrite leading with the new tagline, the overnight-loop ASCII frame (17:00 → 07:00), and an explicit "Scope" blockquote pre-empting the overpromise risk.
- Output: `README.md`.
- Key changes:
  - New "Work while you sleep." H2 hero with overnight-loop story above the install fold.
  - Anti-overpromise scope blockquote naming the refusal-policy categories and the human-in-the-loop briefing.
  - Intra-day inclusivity sentence (long lunch / meeting marathon / flight).
  - "Latest release" badge added pointing at the releases page.
  - "Six hosts" section reframed as "Six hosts, one on-disk run state" with the cross-host suspend/resume benefit as the lede.
  - New "Keep-alive plumbing" sub-section with a Stop-hook table making the host-level mechanism legible.
  - Homebrew section updated — current README still had `--HEAD` boilerplate; rewrote to reflect tagged releases.
  - RFCs 001–008 referenced as accepted; RFC 009 explicitly called out as planned.

## Iteration 2 — Synthesizer
- Input score: not yet graded.
- Action: tightened cross-section links and regrouped "What it does."
- Output: `README.md`.
- Key changes:
  - Overnight-loop ASCII frame now names the briefing as "the review surface" and links forward to How it works.
  - "What it does" reorganised under four conceptual subheads (finds work / runs in isolation / refuses / hands you a reviewable morning) instead of a flat 12-bullet feature dump.
  - Forward pointer added so the loop story doesn't orphan from the deep section.

## Iteration 3 — Polisher
- Input score: not yet graded.
- Action: prose tightening, tone-of-voice pass, anti-overpromise sweep.
- Output: `README.md`, `.planning/drafts/marketing-headlines.md`.
- Key changes:
  - Hero subhead reflowed for rhythm (briefing.html called out by filename).
  - Scope blockquote prefixed with "**Scope:**" so it reads as a positive boundary, not an apology.
  - "Six hosts" lead rewritten to put the suspend/resume benefit first.
  - Keep-alive plumbing's "what" tied to the "why" (CLIs end after first response → hook re-injects continue).
  - Slash-commands intro now connects to the overnight-loop framing.
  - Headless / unattended intro clarifies it's the no-host-session path.
  - Marketing-headlines deliverable written: 12 headlines across 4 surfaces with 200-word rationale.

## Iteration 4 — Skeptic
- Input: rewritten `README.md` + `.planning/drafts/marketing-headlines.md`.
- Score: 100/100.
- Routing: PASS.
- Section breakdown:
  - Hero positioning (25/25), overnight-loop story (20/20), intra-day inclusivity (5/5),
    technical depth preserved (20/20), factual accuracy (10/10), anti-overpromise tone (10/10),
    CommonMark only (3/3), headlines deliverable (5/5), headlines anti-overpromise (2/2).
- Skeptic verified: refusal-policy phrasing, 5-of-6 Stop hook claim against the keep-alive table, RFC 001–008 accepted + RFC 009 planned, six host integrations matching the matrix, headlines all scoped to *draft* PRs.
- Status: TERMINATE SUCCESS. No further iteration required.
