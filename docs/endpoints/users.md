# Users

## GET /api/users/me
Return the authenticated user's own profile. **Auth**: Bearer JWT.
### Success 200 — UserMeResponse
```json
{
  "id": "uuid", "phone": "+966501234567", "role": "CUSTOMER", "status": "ACTIVE",
  "full_name": "Nora", "email": "nora@example.com", "rating": "5.0", "rating_count": 0
}
```
The client reads profile data here, not from the JWT (which carries only ids), so edits
take effect immediately.

## PATCH /api/users/me
Update editable fields. **Auth**: Bearer JWT.
### Body (all optional, `extra="forbid"`)
| field | type | constraints |
| full_name | string | ≤120 |
| email | string | basic email shape, ≤255 |
| dob | date | |
### Success 200 — UserMeResponse (the updated profile)

The actor is always the token subject; there is no path/body user id to tamper with.
