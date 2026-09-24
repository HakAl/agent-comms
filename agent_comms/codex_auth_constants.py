"""Permanent refresh failures, as printed by Codex (verified against rust-v0.153.4).

These are Codex's own user-facing error messages. They are matched verbatim to
tell a login that must be redone apart from a transient refresh failure.
"""

REFRESH_TOKEN_EXPIRED_MESSAGE = "Your access token could not be refreshed because your refresh token has expired. Please log out and sign in again."
REFRESH_TOKEN_INVALIDATED_MESSAGE = "Your access token could not be refreshed because your refresh token was revoked. Please log out and sign in again."
