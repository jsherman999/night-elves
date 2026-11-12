-- Send a message with Messages.app: osascript imessage.applescript <recipient> <text>
-- The recipient is a phone number (+15550001111) or an Apple ID email. Messages must be
-- signed in to iMessage, and the first run asks for permission to control Messages.
on run argv
	if (count of argv) is not 2 then error "usage: osascript imessage.applescript <recipient> <text>"
	set toHandle to item 1 of argv
	set msgText to item 2 of argv
	tell application "Messages"
		set svc to 1st account whose service type = iMessage
		send msgText to participant toHandle of svc
	end tell
end run
