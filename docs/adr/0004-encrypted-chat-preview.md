# ADR 0004 — The chat inbox preview is encrypted

## Status
Accepted (2026-07-17)

## Context
`messages.content` is Restricted and encrypted at rest. The inbox needs a short preview
of the last message per conversation.

## Decision
Store the 100-character truncation ENCRYPTED (same AES-256-GCM key, same scheme) in
`conversations.last_message_preview_encrypted`, and decrypt it at inbox render (one
decrypt per row). The column name carries the `_encrypted` suffix.

## Consequences
- Plaintext never lands in an unencrypted column, so the preview cannot leak cleartext
  and defeat the message encryption.
- One decrypt per inbox row is measured and acceptable; chat search remains out of scope
  precisely because it conflicts with encryption.
