"""Permanent refresh failures, as printed by Codex (verified against codex-cli 0.164.0, the pinned version).

These are Codex's own user-facing error messages. They are matched verbatim to
tell a login that must be redone apart from a transient refresh failure.
"""

REFRESH_TOKEN_EXPIRED_MESSAGE = "Your access token could not be refreshed because your refresh token has expired. Please log out and sign in again."
REFRESH_TOKEN_INVALIDATED_MESSAGE = "Your access token could not be refreshed because your refresh token was revoked. Please log out and sign in again."
# The refresh token was exchanged by another process first (0.164.0
# ``refresh_token_reused``): a second party refreshing the same login.
REFRESH_TOKEN_REUSED_MESSAGE = "Your access token could not be refreshed because your refresh token was already used. Please log out and sign in again."
