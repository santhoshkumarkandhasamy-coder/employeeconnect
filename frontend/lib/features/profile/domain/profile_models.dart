import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';

/// api-contract.md section 2 (profile) and plan 07.

const List<String> bloodGroups = ['A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-'];

class EmergencyContact {
  const EmergencyContact({required this.name, required this.phone, this.relationship = ''});
  final String name;
  final String relationship;
  final String phone;

  static EmergencyContact? parse(Object? v) {
    if (v is! Map) return null;
    final name = '${v['name'] ?? ''}';
    final phone = '${v['phone'] ?? ''}';
    if (name.isEmpty && phone.isEmpty) return null;
    return EmergencyContact(name: name, phone: phone, relationship: '${v['relationship'] ?? ''}');
  }
}

class Profile {
  const Profile({
    required this.userId,
    required this.employeeId,
    required this.fullName,
    this.department,
    this.role = 'employee',
    this.officialEmail = '',
    this.photoUrl,
    this.dob,
    this.weddingAnniversary,
    this.personalEmail,
    this.phone,
    this.emergency,
    this.bloodGroup,
    this.address,
    this.detailsHidden = false,
    this.showCelebrations = false,
  });

  final String userId;
  final String employeeId;
  final String fullName;
  final String? department;
  final String role;
  final String officialEmail;
  final String? photoUrl;

  /// `YYYY-MM-DD`.
  final String? dob;
  final String? weddingAnniversary;
  final String? personalEmail;
  final String? phone;
  final EmergencyContact? emergency;
  final String? bloodGroup;
  final String? address;

  /// True for the offline copy: personal details are never written to the
  /// cache (see `sensitiveCacheKeys`), so editing is disabled.
  final bool detailsHidden;

  /// `show_celebrations`: whether colleagues see this user's birthday and
  /// anniversary (opt-in; false when the server omits it).
  final bool showCelebrations;

  factory Profile.fromJson(Map<String, dynamic> j, {bool detailsHidden = false}) {
    final dep = j['department'];
    String? nonEmpty(Object? v) => v is String && v.isNotEmpty ? v : null;
    return Profile(
      userId: '${j['user_id'] ?? j['id'] ?? ''}',
      employeeId: '${j['employee_id'] ?? ''}',
      fullName: '${j['full_name'] ?? ''}',
      department: dep is Map ? nonEmpty(dep['name']) : nonEmpty(dep),
      role: '${j['role'] ?? 'employee'}',
      officialEmail: '${j['official_email'] ?? ''}',
      photoUrl: nonEmpty(j['photo_url']),
      dob: nonEmpty(j['dob']),
      weddingAnniversary: nonEmpty(j['wedding_anniversary']),
      personalEmail: nonEmpty(j['personal_email']),
      phone: nonEmpty(j['phone']),
      emergency: EmergencyContact.parse(j['emergency_contact']),
      bloodGroup: nonEmpty(j['blood_group']),
      address: nonEmpty(j['address']),
      detailsHidden: detailsHidden,
      showCelebrations: j['show_celebrations'] == true,
    );
  }

  Profile copyWith({
    String? photoUrl,
    bool? detailsHidden,
    bool? showCelebrations,
  }) =>
      Profile(
        userId: userId,
        employeeId: employeeId,
        fullName: fullName,
        department: department,
        role: role,
        officialEmail: officialEmail,
        photoUrl: photoUrl ?? this.photoUrl,
        dob: dob,
        weddingAnniversary: weddingAnniversary,
        personalEmail: personalEmail,
        phone: phone,
        emergency: emergency,
        bloodGroup: bloodGroup,
        address: address,
        detailsHidden: detailsHidden ?? this.detailsHidden,
        showCelebrations: showCelebrations ?? this.showCelebrations,
      );
}

/// Explanation shown next to the DOB / anniversary fields and the switch.
const String celebrationsExplanation =
    'If switched on, colleagues can see your name and birthday or wedding '
    'anniversary (day and month only) on the home screen. Off by default.';

// ---- validation (mirrors the server rules in plan 07) -----------------------------

final RegExp _phoneRe = RegExp(r'^\+?[0-9]{8,15}$');
final RegExp _emailRe = RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$');

/// Removes spaces, dashes, dots and brackets people type into phone numbers.
String normalizePhone(String v) => v.replaceAll(RegExp(r'[\s\-().]'), '');

String? validatePhone(String? v) {
  final s = normalizePhone(v?.trim() ?? '');
  if (s.isEmpty) return null;
  return _phoneRe.hasMatch(s) ? null : 'Enter 8 to 15 digits, optionally starting with +';
}

String? validateEmail(String? v) {
  final s = v?.trim() ?? '';
  if (s.isEmpty) return null;
  if (s.length > 254) return 'Email is too long';
  return _emailRe.hasMatch(s) ? null : 'Enter a valid email address';
}

/// Date of birth: in the past, age 16 to 100. [dob] is `YYYY-MM-DD` or empty.
String? validateDob(String? dob, DateTime now) {
  if (dob == null || dob.isEmpty) return null;
  final d = parseYmd(dob);
  if (d == null) return 'Enter a valid date';
  final today = DateTime(now.year, now.month, now.day);
  if (!d.isBefore(today)) return 'Date of birth must be in the past';
  int age = today.year - d.year;
  if (today.month < d.month || (today.month == d.month && today.day < d.day)) age--;
  if (age < 16) return 'You must be at least 16 years old';
  if (age > 100) return 'Enter a valid date of birth';
  return null;
}

String? validateAnniversary(String? v, DateTime now) {
  if (v == null || v.isEmpty) return null;
  final d = parseYmd(v);
  if (d == null) return 'Enter a valid date';
  return d.isBefore(DateTime(now.year, now.month, now.day)) ? null : 'Anniversary must be in the past';
}

const int maxAddressLength = 300;

String? validateAddress(String? v) =>
    (v?.trim().length ?? 0) > maxAddressLength ? 'Address can be at most $maxAddressLength characters' : null;

/// Emergency contact needs name and phone together (relationship optional).
String? validateEmergencyName(String? name, String? phone) {
  final n = name?.trim() ?? '';
  final p = phone?.trim() ?? '';
  if (n.length > 100) return 'Name is too long';
  if (n.isEmpty && p.isNotEmpty) return 'Enter the contact name';
  return null;
}

String? validateEmergencyPhone(String? name, String? phone) {
  final n = name?.trim() ?? '';
  final p = phone?.trim() ?? '';
  if (p.isEmpty && n.isNotEmpty) return 'Enter the contact phone';
  return validatePhone(phone);
}

// ---- edit draft ---------------------------------------------------------------------

/// The editable fields as form strings, plus the diff against the saved
/// profile. Only changed keys are sent; an explicit `null` clears a field.
class ProfileDraft {
  const ProfileDraft({
    this.dob = '',
    this.anniversary = '',
    this.personalEmail = '',
    this.phone = '',
    this.emergencyName = '',
    this.emergencyRelationship = '',
    this.emergencyPhone = '',
    this.bloodGroup = '',
    this.address = '',
  });

  final String dob;
  final String anniversary;
  final String personalEmail;
  final String phone;
  final String emergencyName;
  final String emergencyRelationship;
  final String emergencyPhone;
  final String bloodGroup;
  final String address;

  factory ProfileDraft.from(Profile p) => ProfileDraft(
        dob: p.dob ?? '',
        anniversary: p.weddingAnniversary ?? '',
        personalEmail: p.personalEmail ?? '',
        phone: p.phone ?? '',
        emergencyName: p.emergency?.name ?? '',
        emergencyRelationship: p.emergency?.relationship ?? '',
        emergencyPhone: p.emergency?.phone ?? '',
        bloodGroup: p.bloodGroup ?? '',
        address: p.address ?? '',
      );

  ProfileDraft copyWith({
    String? dob,
    String? anniversary,
    String? personalEmail,
    String? phone,
    String? emergencyName,
    String? emergencyRelationship,
    String? emergencyPhone,
    String? bloodGroup,
    String? address,
  }) =>
      ProfileDraft(
        dob: dob ?? this.dob,
        anniversary: anniversary ?? this.anniversary,
        personalEmail: personalEmail ?? this.personalEmail,
        phone: phone ?? this.phone,
        emergencyName: emergencyName ?? this.emergencyName,
        emergencyRelationship: emergencyRelationship ?? this.emergencyRelationship,
        emergencyPhone: emergencyPhone ?? this.emergencyPhone,
        bloodGroup: bloodGroup ?? this.bloodGroup,
        address: address ?? this.address,
      );

  /// Normalised form values compared with [original]; true when nothing changed.
  bool sameAs(Profile original) => diff(original).isEmpty;

  /// Body for `PUT /me/profile`: changed keys only.
  Map<String, Object?> diff(Profile original) {
    final o = ProfileDraft.from(original);
    String t(String s) => s.trim();
    final out = <String, Object?>{};
    String? n(String s) => t(s).isEmpty ? null : t(s);

    if (t(dob) != t(o.dob)) out['dob'] = n(dob);
    if (t(anniversary) != t(o.anniversary)) out['wedding_anniversary'] = n(anniversary);
    if (t(personalEmail) != t(o.personalEmail)) out['personal_email'] = n(personalEmail);
    if (normalizePhone(t(phone)) != normalizePhone(t(o.phone))) {
      out['phone'] = n(normalizePhone(t(phone)));
    }
    if (t(bloodGroup) != t(o.bloodGroup)) out['blood_group'] = n(bloodGroup);
    if (t(address) != t(o.address)) out['address'] = n(address);

    final emergencyChanged = t(emergencyName) != t(o.emergencyName) ||
        t(emergencyRelationship) != t(o.emergencyRelationship) ||
        normalizePhone(t(emergencyPhone)) != normalizePhone(t(o.emergencyPhone));
    if (emergencyChanged) {
      out['emergency_contact'] = t(emergencyName).isEmpty && t(emergencyPhone).isEmpty
          ? null
          : {
              'name': t(emergencyName),
              if (t(emergencyRelationship).isNotEmpty) 'relationship': t(emergencyRelationship),
              'phone': normalizePhone(t(emergencyPhone)),
            };
    }

    return out;
  }
}

/// Age-safe birthday label (`12 Apr`), never the year.
String monthDayLabel(String? ymdValue) {
  final d = parseYmd(ymdValue);
  return d == null ? '' : '${d.day} ${monthNames[d.month - 1].substring(0, 3)}';
}

String profileErrorMessage(ApiException e) {
  switch (e.code) {
    case 'VALIDATION_ERROR':
      final msgs = e.details.map((d) => d.message).where((m) => m.isNotEmpty);
      return msgs.isEmpty ? 'Please check the details and try again.' : msgs.join('\n');
    case 'FILE_TOO_LARGE':
      return 'The photo is too large (up to 5 MB).';
    case 'FILE_TYPE_NOT_ALLOWED':
      return 'Use a JPEG or PNG photo.';
    case 'RATE_LIMITED':
      return 'Too many attempts. Please wait a moment and try again.';
    case 'DATA_UNREADABLE':
      return 'Your saved details cannot be read right now. Please contact HR.';
    case 'FORBIDDEN':
      return 'You do not have permission to do this.';
    default:
      return e.message;
  }
}
