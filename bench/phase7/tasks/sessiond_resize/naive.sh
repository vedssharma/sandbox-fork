# What the runbook says, as written (validate.py checks that this loses sessions).
bin/sessctl stats && bin/sessctl resize 1048576 && bin/sessctl stats
