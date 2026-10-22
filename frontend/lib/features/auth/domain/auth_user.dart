import '../../../core/server/company.dart';

class AuthUser {
  const AuthUser({
    required this.id,
    required this.employeeId,
    required this.fullName,
    required this.role,
    required this.permissions,
    required this.mustChangePassword,
    this.company,
  });

  final String id;
  final String employeeId;
  final String fullName;
  final String role;
  final List<String> permissions;
  final bool mustChangePassword;

  /// Branding block of `/auth/me`; null for login replies.
  final CompanyInfo? company;

  bool get isAdmin => role == 'admin';
  bool hasPermission(String code) => permissions.contains(code);

  /// A `company.manage` holder must finish the Company setup wizard first.
  bool get needsCompanySetup =>
      hasPermission('company.manage') && company != null && !company!.configured;

  /// Login / change-password shape: `{ must_change_password, user: {...} }`.
  factory AuthUser.fromLogin(Map<String, dynamic> json) {
    final u = json['user'] as Map<String, dynamic>;
    return AuthUser._parse(u, json['must_change_password'] as bool? ?? false);
  }

  /// `/auth/me` shape (flat).
  factory AuthUser.fromMe(Map<String, dynamic> json) =>
      AuthUser._parse(json, json['must_change_password'] as bool? ?? false);

  factory AuthUser._parse(Map<String, dynamic> u, bool mustChange) => AuthUser(
        id: u['id'] as String,
        employeeId: u['employee_id'] as String,
        fullName: (u['full_name'] as String?) ?? '',
        role: (u['role'] as String?) ?? 'employee',
        permissions: ((u['permissions'] as List<dynamic>?) ?? const <dynamic>[])
            .cast<String>()
            .toList(growable: false),
        mustChangePassword: mustChange,
        company: CompanyInfo.parse(u['company']),
      );

  AuthUser copyWith({bool? mustChangePassword, CompanyInfo? company}) => AuthUser(
        id: id,
        employeeId: employeeId,
        fullName: fullName,
        role: role,
        permissions: permissions,
        mustChangePassword: mustChangePassword ?? this.mustChangePassword,
        company: company ?? this.company,
      );
}
