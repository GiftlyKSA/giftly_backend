# Media

Zero-proxy uploads: the API never accepts bytes. It issues a pre-signed PUT URL with a
SERVER-generated key; the client PUTs straight to S3, then confirms the key.

## POST /api/media/upload-urls
**Auth**: Bearer JWT.
### Body
| field | type | notes |
| purpose | enum | ORDER_REQUEST \| DELIVERY_PROOF (sets the key prefix) |
| content_type | enum | image/jpeg \| image/png (pinned into the URL) |
| byte_size | int | > 0 and <= the upload cap |
### Success 201 — UploadUrlResponse
```json
{ "upload_url": "https://s3...", "storage_key": "orders/pending/<uuid>.jpg", "expires_in": 300 }
```
### Errors
| status | code | when |
| 400 | BAD_REQUEST | bad purpose/type, or the size is over the cap |

## POST /api/media/confirm
Confirm an uploaded object. **Auth**: Bearer JWT. **Body**: `{storage_key}`.
The server HEADs the object and verifies its real content type by **magic bytes** — a
`.jpg` that is actually a script is rejected. Keys are validated against a strict
allow-list (no `../`, no absolute paths).
### Success 200 — `{ "storage_key": "...", "confirmed": true }`
### Errors
| status | code | when |
| 400 | BAD_REQUEST | malformed key, object missing/oversized, or not a real image |
