"""Gunicorn config for Raspberry Pi deployment."""

bind = "127.0.0.1:8008"
workers = 2
worker_class = "sync"
timeout = 30
accesslog = "-"
errorlog = "-"
loglevel = "info"
