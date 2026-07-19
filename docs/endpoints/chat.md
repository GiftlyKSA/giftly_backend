# Chat

Per-order messaging between the customer and the assigned courier. The conversation is
opened automatically when the order is assigned (Phase 6). Message content and the inbox
preview are **AES-256-GCM encrypted at rest** (ADR 0004); plaintext never lands in an
unencrypted column. Every route is participant-only — a non-member gets 404.

## GET /api/conversations ?cursor=&limit=
The caller's inbox, most-recent activity first (keyset paged). **Role**: CUSTOMER or
COURIER. Each row carries the DECRYPTED last-message preview, the other member's id, and
the caller's unread count. The cursor is an opaque `<iso8601>|<uuid>`.

## GET /api/conversations/{id}/messages ?cursor=&limit=
Decrypted messages, newest first (keyset paged by message id). **Role**: participant.

## POST /api/conversations/{id}/messages
Send a text message (1–4000 chars). **Role**: participant. The message is encrypted,
appended (messages are append-only), and PUBLISHED to the conversation's Redis channel so
every open WebSocket — on any instance — receives it live.
### Body
`{ "text": "..." }` → 201 with the decrypted message.

## POST /api/conversations/{id}/read
Mark the caller's inbound messages read and clear their unread count. **Role**:
participant. Only `is_read`/`read_at` change on messages (the append-only trigger permits
it). → 204.

## WS /api/ws/conversations/{id}?token=<access_token>
The live chat socket. Authenticated by the access token in the `token` query parameter
(same verification and denylist as the REST API); only the conversation's two members may
connect — anyone else is closed (4401 unauthenticated, 4403 not a participant).

- **Server → client**: each new message on the conversation is pushed as JSON
  (`id`, `conversation_id`, `sender_id`, `message_type`, `content`, `is_read`, `created_at`).
- **Client → server**: a text frame (`{"text": "..."}` or raw text) sends a message,
  exactly as the REST POST would.

Live delivery rides Redis pub/sub (channel `chat:conversation:<id>`), so horizontal
scaling works: a message sent on one instance reaches sockets on every instance. Chat
search is deliberately out of scope — it conflicts with at-rest encryption (ADR 0004).
