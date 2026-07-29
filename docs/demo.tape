# README demo — regenerate with:
#
#   bash docs/demo-prep.sh init && vhs docs/demo.tape
#
# The prep script builds a throwaway repo at /private/tmp/nightly-demo-render
# and runs `nightly init` there. Never record against a real checkout:
# `nightly init` and `nightly start` both write state.
#
# What this shows, and why. The cascade is what distinguishes Nightly from
# "an agent in a loop", so the demo ends on `nightly next` naming its pick
# and the rule behind it. The status transition before it is not padding:
# `ready` is deliberately not a cascade source (only `in_progress` and an
# approved `blocked:` are), so a demo that skipped it would end on "no
# work — backlog is empty".
#
# Sleeps are sized to the slowest observed run of each command, not the
# average. Typing that overlaps a command's own output looks broken.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 16
Set Width 1100
Set Height 400
Set Theme "Dracula"
Set Padding 16
Set TypingSpeed 40ms

# Setup is hidden: open on a clean prompt in the demo repo, not on the
# plumbing that got us there.
Hide
Type "export PATH=$HOME/.local/bin:$PATH PS1='$ ' && cd /private/tmp/nightly-demo-render && clear"
Enter
Sleep 1s
Show

Sleep 800ms

# 1. Open a session. Everything Nightly knows lives on disk under this run.
Type "nightly start"
Enter
Sleep 3s

# 2. Queue a piece of work.
Type "nightly task add-retry-budget -d 'Add a retry budget to the auth client'"
Enter
Sleep 3s

# 3. Move it in-flight — this is what makes it cascade-visible.
Type "nightly task add-retry-budget --status in_progress"
Enter
Sleep 3s

# 4. Ask what to work on. It picks, and says why it picked that.
Type "nightly next"
Enter
Sleep 5s
