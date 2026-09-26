# README Rewrite — Compliance Checklist (final)

Source: user prompt requirements. Status as of iteration 4 (Skeptic PASS).

## Positioning

- [x] Tagline "work while you sleep" appears prominently in the hero — H2 directly under the badges.
- [x] Overnight-loop story leads the README — 17:00 → 07:00 ASCII frame appears before Install.
- [x] Does NOT scare off intra-day operators — "long lunch / meeting marathon / flight" line in the hero.
- [x] Hero is punchy; technical detail deferred but still reachable (forward link to "How it works").

## Don't-oversell guardrails

- [x] Copy says "draft PRs in review-ready shape" — no "merged code" / "ships to prod" language anywhere.
- [x] Refusal policy surfaced — scope blockquote in hero plus "Refuses what it shouldn't do" subsection.
- [x] Morning briefing framed as the human review surface — twice (hero scope blockquote, overnight-loop frame).
- [x] No "set it and forget it" language.

## Don't-bury-the-depth guardrails

- [x] Cascade ordering preserved — both as bullet (What it does) and ASCII (How it works).
- [x] Plan lifecycle preserved.
- [x] CLI surface (doctor / verify / ci / bug / vault) preserved in full table.
- [x] Host-specific dispatch matrix preserved.
- [x] `.nightly/` runtime folder map preserved.
- [x] `.planning/` design folder description preserved.
- [x] Headless / parallelism preserved.
- [x] Repo layout preserved.
- [x] Development section preserved.
- [x] Add-a-host and add-a-proposer recipes preserved.

## Factual specifics

- [x] Six host integrations named.
- [x] Three primary / three secondary split correct.
- [x] Stop-hook names accurate per host.
- [x] opencode is soft (rule-text only).
- [x] Releases linked to github.com/ulmentflam/nightly/releases.
- [x] Homebrew tap form correct: `brew install ulmentflam/tap/nightly`.
- [x] install.sh path preserved.
- [x] RFCs 001–008 referenced as accepted; RFC 009 planned (not yet drafted).
- [x] Six-category refusal policy enumerated correctly.
- [x] Specialists named: implementer, tester, reviewer, researcher.
- [x] Autonomy bar: single file, < 80 LOC, `{lint_debt, dep_upgrade}`.

## Markdown

- [x] CommonMark only — no HTML pass-through.
- [x] Badges (CI / Latest release / Python / License / ruff) intact and improved.

## Marketing headlines deliverable

- [x] 12 headlines.
- [x] Tones varied — technical-specific, evocative, operator-direct.
- [x] Repo description option ≤ 120 chars (102 chars on the recommended pick).
- [x] Hero H1 + subhead options.
- [x] Tweet-shaped options.
- [x] Section-opener options.
- [x] No overclaim — every line scoped to draft PRs / briefings / review-shaped outputs.
- [x] Rationale ≤ 200 words picks 2–3 for the hero.

## Output paths

- [x] `/Users/evanowen/Library/Mobile Documents/com~apple~CloudDocs/Workspace/playground/nightly/README.md` overwritten.
- [x] `/Users/evanowen/Library/Mobile Documents/com~apple~CloudDocs/Workspace/playground/nightly/.planning/drafts/marketing-headlines.md` created.

## Process

- [x] Architect pass.
- [x] Synthesizer pass.
- [x] Polisher pass.
- [x] Skeptic pass — 100/100.
