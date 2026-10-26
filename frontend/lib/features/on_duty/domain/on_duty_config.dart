/// `on_duty` block of `GET /config` (docs/plans/13-on-duty-contract.md 14.9).
/// The Office | On Duty switch is shown only when [showSwitch].
class OnDutyConfig {
  const OnDutyConfig({
    this.enabled = false,
    this.hasLocations = false,
    this.approvalRequired = false,
    this.selfieRequired = false,
    this.checkoutGeofence = false,
    this.maxVisitsPerDay = 10,
    this.requestBackdateDays = 0,
    this.requestMaxAdvanceDays = 60,
  });

  final bool enabled;

  /// `enabled` AND the caller has at least one active assigned location.
  final bool hasLocations;
  final bool approvalRequired;
  final bool selfieRequired;
  final bool checkoutGeofence;
  final int maxVisitsPerDay;
  final int requestBackdateDays;
  final int requestMaxAdvanceDays;

  /// A server that predates On Duty sends no block: the feature stays hidden.
  static const none = OnDutyConfig();

  bool get showSwitch => enabled && hasLocations;

  factory OnDutyConfig.fromJson(Object? v) {
    if (v is! Map) return none;
    int n(String k, int d) => (v[k] as num?)?.toInt() ?? d;
    return OnDutyConfig(
      enabled: v['enabled'] == true,
      hasLocations: v['has_locations'] == true,
      approvalRequired: v['approval_required'] == true,
      selfieRequired: v['selfie_required'] == true,
      checkoutGeofence: v['checkout_geofence'] == true,
      maxVisitsPerDay: n('max_visits_per_day', 10),
      requestBackdateDays: n('request_backdate_days', 0),
      requestMaxAdvanceDays: n('request_max_advance_days', 60),
    );
  }
}
