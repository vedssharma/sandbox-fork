# Reference fix used only by validate.py: the tool reads --retention in hours, so 30 days = 720.
tsdbctl compact --retention 720 && tsdbctl verify
