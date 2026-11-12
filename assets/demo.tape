# Records assets/demo.gif for the GitHub profile README.
# Needs VHS (https://github.com/charmbracelet/vhs) and a filled-in .env.
# Replace VIDEO_ID with a short video that has captions, then run from the repo root:
#   vhs assets/demo.tape

Output assets/demo.gif

Set Shell "bash"
Set FontSize 16
Set Width 1200
Set Height 640
Set Padding 20
Set Theme "GitHub Dark"
# The full pipeline takes minutes; play it back fast so the GIF stays short
Set PlaybackSpeed 4

Hide
Type "source .venv/bin/activate && clear"
Enter
Show

Type "yt2blog run https://www.youtube.com/watch?v=VIDEO_ID --no-ghost --force"
Sleep 500ms
Enter
Wait@10m /\$ $/
Sleep 3s
