# The demo at the top of the README: the quick start passes, the guardrails are turned
# off, the run fails and exits with 1. Recorded with VHS in the image of docs/demo/Dockerfile.

Output docs/assets/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 960
Set Height 700
Set Padding 20
Set TypingSpeed 35ms

Hide
Type "cd $(mktemp -d) && export PS1='$ ' && clear"
Enter
Show

Type "highbar init"
Sleep 300ms
Enter
Sleep 1.5s
Type "highbar run"
Sleep 300ms
Enter
Sleep 6s

Hide
Type "clear"
Enter
Show

Type "sed -i 's/GUARDRAILS = True/GUARDRAILS = False/' agent.py  # guardrails off"
Sleep 300ms
Enter
Type "highbar run"
Sleep 300ms
Enter
Sleep 6s
Type "echo $?"
Sleep 300ms
Enter
Sleep 5s
