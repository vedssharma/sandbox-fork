# What the runbook says, as written (validate.py checks that this loses data).
tsdbctl stats && tsdbctl compact --retention 30 && tsdbctl verify
