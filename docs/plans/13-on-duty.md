# 13 - On Duty (client locations)

Status: IMPLEMENTED. Contract: api-contract section 14. Migration: `0019_on_duty`.
Tables: `client_locations`, `on_duty_requests`, `attendance.client_location_id`. Permission: `client_location.manage` (admin, hr).

## Goal
An employee who works a full day at a client site requests **On Duty** for those dates and picks the client location. The admin
sets each client location's geo coordinates and allowed radius. On an approved on-duty day, attendance can be punched **only**
inside that client location's radius; the office geofence does not count that day.

## Client locations (admin)
- Fields: name (unique, case-insensitive), address (optional), lat, lng, radius_m (20-5000, default 200), is_active.
- `GET/POST/PUT/DELETE /admin/client-locations` need `client_location.manage` (even GET: the coordinates are admin data).
- Delete of a location used by any on-duty request or attendance row -> 409 `IN_USE` (deactivate instead).
- Deactivating a location hides it from the request form. Approved requests keep working (their punches are still checked
  against the stored coordinates).
- App: Admin -> Client locations. The form has "Use my current location" so the admin can stand at the site, or paste
  coordinates from a map.

## On-duty requests
- Employee: `POST /on-duty/requests {client_location_id, from_date, to_date, reason}`; list / get / cancel like leave.
- Rules: from_date >= today (a past day cannot be punched at the site); at most 31 calendar days per request; not beyond
  `leave.max_advance_days`; at least one working day (`ZERO_WORKING_DAYS`); location must be active (`VALIDATION_ERROR`).
- Overlaps (409 `OVERLAPPING_REQUEST`): another pending/approved on-duty request (DB EXCLUDE is the final guard), or any
  pending/approved leave (full or half day). Leave applied later on an on-duty day is refused the same way, and approving a
  leave that overlaps an approved on-duty request (or the reverse) is refused.
- No balance and no monthly limit. `days` = working days at submit time (informational).
- Approval: same flow, scope and push as leave/permission (`/admin/requests?type=on_duty`,
  `PUT /admin/requests/on_duty/{id}/approve|reject`). No self-approval. Push types `new_request` / `on_duty_decision`.
- Deactivating an employee cancels their pending on-duty requests (`on_duty.auto_cancel`).
- Reasons and decision comments are encrypted at rest and follow `leave.reason_retention_days`.

## Attendance on an on-duty day
- Check-in: when the user has an APPROVED on-duty request covering the work date, the fence is the client location
  (lat/lng/radius_m), not the office. Outside -> 422 `OUTSIDE_CLIENT_LOCATION` with the location name (never the distance).
  All other checks are unchanged (fresh GPS fix, accuracy, mock location, selfie, already checked in, leave).
- The record stores `client_location_id`; check-out with `attendance.checkout_geofence` on uses the same site.
- Late rules are unchanged (shift start + grace).
- `GET /attendance/today` adds `on_duty` (the site with coordinates, so the app can show the live distance like it does for
  the office; null on a normal day). Records carry `client_location {id, name}`. Calendar days carry `on_duty` (site name).
- A pending request does not change anything: the office fence applies until it is approved.

## Export
- New sheet `On Duty` (type `on_duty`, also part of `type=all`): Request ID, Employee ID, Name, Department, Client Location,
  From, To, Days, Status, [Reason], Approver, [Decision Comment], Applied On, Decided On. Bracketed columns only with
  `include_reasons=true` + `export.sensitive`. Requests overlapping the range, all statuses.
- `Attendance` sheet gets a last column `Client Location` (empty = office punch). No coordinates are exported.

## Flutter
- Leave screen: Request tab segment "On duty" (client location picker, from/to dates, purpose). My Requests and Approvals
  show on-duty requests with the client location.
- Attendance Today: "On duty today at X" banner; proximity card measures the distance to the client site; the
  `OUTSIDE_CLIENT_LOCATION` message names the site. Home card mentions the site.
- Admin: "Client locations (on duty)" master with the geo picker. Export: "On Duty" report type.

## Tests
`backend/tests/test_on_duty.py` (API, attendance fence, calendar, export, permissions). Migration verified on PostgreSQL 16
(upgrade, downgrade, EXCLUDE / CHECK / FK RESTRICT). Flutter: analyzer clean, existing suite green.
