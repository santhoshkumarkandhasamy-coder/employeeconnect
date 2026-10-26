import '../../../core/utils/dates.dart';

/// Client-side checks for an On Duty request (contract 14.3). The server
/// re-checks everything and stays authoritative.

const int maxOnDutySpanDays = 31;
const int maxPurposeLength = 300;

DateTime _d(DateTime t) => DateTime(t.year, t.month, t.day);

/// Calendar days, inclusive.
int onDutyDays(DateTime from, DateTime to) => _d(to).difference(_d(from)).inDays + 1;

DateTime earliestOnDutyFrom(DateTime today, int backdateDays) =>
    _d(today).subtract(Duration(days: backdateDays));

DateTime latestOnDutyTo(DateTime today, int maxAdvanceDays) =>
    _d(today).add(Duration(days: maxAdvanceDays));

String? validatePurpose(String? v) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Enter the purpose of the visit';
  if (t.length > maxPurposeLength) return 'At most $maxPurposeLength characters';
  return null;
}

/// Returns the first problem with the request, or null when it looks valid.
String? validateOnDutyRequest({
  required DateTime? from,
  required DateTime? to,
  required String purpose,
  required DateTime today,
  int backdateDays = 0,
  int maxAdvanceDays = 60,
}) {
  if (from == null || to == null) return 'Choose the from and to dates.';
  if (_d(to).isBefore(_d(from))) return 'The to date cannot be before the from date.';
  if (onDutyDays(from, to) > maxOnDutySpanDays) {
    return 'A request can cover at most $maxOnDutySpanDays days.';
  }
  if (_d(from).isBefore(earliestOnDutyFrom(today, backdateDays))) {
    return backdateDays == 0
        ? 'The from date cannot be in the past.'
        : 'The from date can be at most $backdateDays days in the past.';
  }
  if (_d(to).isAfter(latestOnDutyTo(today, maxAdvanceDays))) {
    return 'The to date can be at most $maxAdvanceDays days ahead.';
  }
  return validatePurpose(purpose);
}

/// Body of `POST /on-duty/requests`. [locationId] null = any assigned location.
Map<String, Object?> onDutyRequestBody({
  required DateTime from,
  required DateTime to,
  required String? locationId,
  required String purpose,
}) =>
    {
      'from_date': ymd(from),
      'to_date': ymd(to),
      'location_id': locationId,
      'purpose': purpose.trim(),
    };
