# README install demo — regenerate with:
#
#   bash docs/demo-prep.sh bare && vhs docs/demo-install.tape
#
# Deliberately records the *per-repo* bootstrap, not `curl … | bash`.
# The binary install mutates the operator's own machine
# (~/.local/share/nightly), which is not something a demo should do on
# every render — and `nightly init` is the step run in every repo
# thereafter, so it is the one worth showing.
#
# The `ls -a` at the end is the point: it shows the whole footprint, so a
# reader can see exactly what landed in their repo before running it.

Output docs/demo-install.gif

Set Shell "bash"
Set FontSize 16
Set Width 1100
Set Height 420
Set Theme "Dracula"
Set Padding 16
Set TypingSpeed 40ms

Hide
Type "export PATH=$HOME/.local/bin:$PATH PS1='$ ' && cd /private/tmp/nightly-demo-render && clear"
Enter
Sleep 1s
Show

Sleep 800ms

Type "nightly init"
Enter
Sleep 5s

Type "ls -a"
Enter
Sleep 4s
