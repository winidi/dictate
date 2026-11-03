"""Dictate server: the desktop's recognizer behind an HTTP API, so a phone, a
laptop or anyone on your Tailscale/Headscale network can dictate through it.

    python -m server serve            # on your PC or on a VPS
    python -m server token add anna   # a token for one person or device

The API is the OpenAI transcription API (POST /v1/audio/transcriptions with a
bearer token), which the desktop client already speaks as the "Dictate server"
provider. Extras for the phone app ride along as form fields (polish, mode).
Nothing is stored: audio lives in memory for the length of one request, the log
holds durations and character counts only.
"""
