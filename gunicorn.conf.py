# Render pe WEB_CONCURRENCY ki wajah se 2+ workers ban jate the -> alag-alag memory,
# duplicate checker, products list out-of-sync. Isliye 1 worker force kiya.
workers = 1
threads = 4
timeout = 120
