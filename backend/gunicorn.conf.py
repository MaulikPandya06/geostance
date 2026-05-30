import multiprocessing

# One process keeps the footprint inside Render's free-tier 512 MB limit.
# gthread allows up to `threads` concurrent requests; I/O-bound LLM calls
# release the GIL so threads actually help here.
workers      = 1
worker_class = "gthread"
threads      = 4

# LLM report generation can take 15-90 s depending on feature.
# Default 30 s would SIGKILL the worker mid-generation.
timeout      = 180

# Keep-alive for the reverse proxy (Render uses one).
keepalive    = 5

# Load the app once before forking — shares memory across threads, reduces RSS.
preload_app  = True

# Recycle workers periodically to prevent slow memory leaks.
max_requests        = 200
max_requests_jitter = 20

# Log to stdout so Render picks it up.
accesslog = "-"
errorlog  = "-"
loglevel  = "info"