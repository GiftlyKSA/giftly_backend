# Orders

All money is a decimal STRING. Role and state authority are enforced server-side; the
actor comes from the JWT.

## POST /api/orders
Create a gift-request order. **Auth**: Bearer JWT · **Role**: CUSTOMER.
### Body
| field | type | required | notes |
| description | string | no | <= 2000 chars |
| delivery_city | string | yes | <= 100 chars |
| latitude / longitude | number | yes | within Saudi bounds |
| delivery_date | date | yes | today .. today + 180 days |
| request_media_keys | array<string> | no | 0–3 confirmed photo keys |
### Success 201 — OrderDetail (status NEW)
### Errors
| status | code | when |
| 400 | BAD_REQUEST | a request photo failed validation |
| 409 | CONFLICT | you already have 5 active orders |
| 422 | VALIDATION_ERROR | coordinates out of range / too many photos |

## GET /api/orders ?status=&cursor=&limit=
List your own orders (CUSTOMER), newest first, keyset-paged.

## GET /api/orders/available ?cursor=&limit=
The courier radar: NEW orders in the courier's city. **Role**: COURIER. Summaries carry
NO coordinates (the exact point is revealed only after you accept).

## GET /api/orders/{order_id}
Detail for an order you participate in. A non-participant gets 404 (existence is not
confirmed). A courier sees `latitude`/`longitude` only once the order is assigned.

## POST /api/orders/{order_id}/accept
Claim a NEW order. **Role**: COURIER (verified + active). Serialized by a Redis lock and
a row lock, so exactly one courier wins a race.
### Errors
| status | code | when |
| 403 | FORBIDDEN | not a verified/active courier, or at the 3-assignment limit |
| 409 | ORDER_ALREADY_ASSIGNED | another courier won, or it is no longer NEW |

## POST /api/orders/{order_id}/cancel
Cancel before IN_PROGRESS (either party). **Body**: `{reason?}`.
### Errors
| status | code | when |
| 404 | NOT_FOUND | not your order |
| 409 | INVALID_STATE_TRANSITION | the order is already in progress or later |
