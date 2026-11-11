# Reference fix used only by validate.py: sessiond drops sessions when resized past 65536 buckets,
# and 65536 already brings ~50k sessions under load 1.
bin/sessctl resize 65536 && bin/sessctl stats
