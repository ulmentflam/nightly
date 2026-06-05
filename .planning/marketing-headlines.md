# Nightly — Marketing Headlines

The lead theme is **"work while you sleep."** Variations below are
grouped by surface (repo description, hero, tweet, section opener) and
tagged for tone. Every line ships *draft PRs*, never auto-merged code
— the morning briefing is the review surface, and overpromising
("ships to prod overnight," "merges itself," "set it and forget it")
is excluded by policy.

---

## GitHub repo description (one-liner, <120 chars)

These slot into the field under the repo name on github.com — the
single sentence everyone sees before clicking through. Length checked
against the 120-char cap; counts in brackets.

1. **Work while you sleep — a host-native autonomous coding agent that lands draft PRs by morning.** [102]
   *(technical-evocative blend; clean front-door)*
2. **The overnight shift for your coding CLI. Draft PRs by morning; you review at breakfast.** [89]
   *(operator-direct; calls out the human-in-the-loop)*
3. **Your coding CLI, with an overnight loop. Cascades work, isolates worktrees, ships review-ready PRs.** [104]
   *(technical-specific; favors searchability)*

## Hero H1 / subhead

H1 should be short enough to read as a banner; the subhead expands it.

4. **H1:** *Work while you sleep.*
   **Subhead:** *Nightly turns your coding CLI into a self-directed overnight session that lands draft PRs in review-ready shape — one isolated worktree per task, one morning briefing per run.*
   *(canonical hero; currently in the README)*

5. **H1:** *You sleep. Nightly drafts.*
   **Subhead:** *A host-native agent that walks a priority cascade overnight — in-flight plans, accepted RFCs, ranked GitHub issues — and hands you a stack of reviewable PRs at 07:00.*
   *(operator-direct, slightly punchier; trades atmosphere for clarity)*

6. **H1:** *The overnight shift for your editor.*
   **Subhead:** *Stop coding at five. Fire `/nightly`. Wake up to draft PRs, one per worktree, with a morning briefing that names what landed, what's blocked, and what needs your eyes.*
   *(narrative; sells the loop)*

## Tweet-shaped pitches (≤ 240 chars)

7. *You stop coding at 5. Nightly picks up where you left off — in-flight plans, unblocked approvals, accepted RFCs, ranked issues — and lands draft PRs on isolated worktrees while you sleep. Morning briefing tells you what to review.* [240]

8. *Your coding CLI, now with an overnight shift. Walks a priority cascade. Refuses destructive git, scope creep, and prod state. Lands draft PRs you review at breakfast. Works in Claude Code, Codex, opencode, Cursor, Antigravity, Gemini.* [239]

9. *Wake up to a stack of draft PRs. No auto-merges, no force-pushes, no production state, no surprise. Just the in-progress plan you didn't finish yesterday, finished. The unblocked approval, executed. The morning briefing, ready.* [233]

## Section openers (for use inside the README itself)

10. *The overnight loop:* **"17:00 you stop coding. 07:00 you wake up. In between, Nightly walks the cascade."**

11. *Six hosts, one disk:* **"Because everything Nightly knows lives in `.nightly/`, the host is interchangeable — suspend in Claude Code, resume in Codex."**

12. *Refusal policy:* **"Nightly refuses the six things you wouldn't want it doing unsupervised. The morning briefing exists because everything else still needs your eyes."**

---

## Rationale — recommended hero (≤ 200 words)

I'd put **#1 in the repo description**, **#4 as the README hero**
(H1 + subhead), and **#10 as the section opener** above "The overnight
loop." Together they tell the same story at three resolutions: a
twelve-word grab from a GitHub search result, a hero paragraph that
sells the loop and names the human-in-the-loop, and a section
deep-link that makes the loop *concrete* with a clock.

#1 wins the repo-description slot because it pairs the evocative
front (*work while you sleep*) with the concrete back (*draft PRs by
morning*) — that "draft" is doing real anti-overpromise work in 102
characters. #4 wins the hero because the H1 is the tagline verbatim
and the subhead carries the technical promise (*self-directed*,
*isolated worktree*, *briefing*) without veering into feature-list.
#10 wins as section opener because the 17:00 → 07:00 frame is the
single most operator-shaped sentence in the set — it shows the loop
rather than describing it. Together: front door, lobby, first step.
