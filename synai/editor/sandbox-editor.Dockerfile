FROM python:3.12-slim-bookworm

# SynAI stages its bundled editor in /tmp; no editor installation is needed here.
WORKDIR /workspace
