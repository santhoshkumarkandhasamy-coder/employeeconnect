import 'package:employee_connect/app/router.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/server/company.dart';
import 'package:employee_connect/core/server/server_url.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart';
import 'package:employee_connect/features/admin/domain/ops_models.dart';
import 'package:employee_connect/features/auth/domain/auth_user.dart';
import 'package:employee_connect/features/auth/presentation/auth_state.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('normalizeServerInput', () {
    test('adds https and /api to a bare host', () {
      expect(normalizeServerInput('hr.company.com'), 'https://hr.company.com/api');
      expect(normalizeServerInput('  hr.company.com/ '), 'https://hr.company.com/api');
      expect(normalizeServerInput('https://hr.company.com:8443'), 'https://hr.company.com:8443/api');
      expect(normalizeServerInput('https://hr.company.com/api'), 'https://hr.company.com/api');
    });

    test('rejects http in release, allows it in debug', () {
      expect(normalizeServerInput('http://hr.company.com'), isNull);
      expect(normalizeServerInput('http://10.0.0.5:8000', release: false), 'http://10.0.0.5:8000/api');
    });

    test('rejects userinfo, query, fragment, other paths and junk', () {
      for (final bad in [
        'https://user:pw@hr.company.com',
        'https://hr.company.com?x=1',
        'https://hr.company.com/#a',
        'https://hr.company.com/other',
        'hr company',
        '',
        'ftp://hr.company.com',
      ]) {
        expect(normalizeServerInput(bad), isNull, reason: bad);
      }
    });
  });

  group('resolveBaseUrl', () {
    test('saved URL beats the build-time one', () {
      expect(
        resolveBaseUrl(saved: 'https://a.com/api', buildTime: 'https://b.com/api'),
        'https://a.com/api',
      );
    });
    test('build-time is used when nothing is saved', () {
      expect(resolveBaseUrl(buildTime: 'https://b.com/api'), 'https://b.com/api');
    });
    test('the placeholder means server setup', () {
      expect(resolveBaseUrl(buildTime: placeholderApiUrl), isNull);
      expect(resolveBaseUrl(saved: placeholderApiUrl, buildTime: placeholderApiUrl), isNull);
    });
  });

  group('company and routing', () {
    const unconfigured = CompanyInfo(configured: false);
    AuthUser user(List<String> perms, CompanyInfo? c) => AuthUser(
          id: 'u',
          employeeId: 'E1',
          fullName: 'A',
          role: 'admin',
          permissions: perms,
          mustChangePassword: false,
          company: c,
        );

    test('/auth/me company block is parsed', () {
      final u = AuthUser.fromMe({
        'id': 'u',
        'employee_id': 'E1',
        'full_name': 'A',
        'role': 'admin',
        'permissions': ['company.manage'],
        'company': {'name': 'Acme', 'logo_url': '/api/public/company/logo?v=2', 'logo_version': 2, 'configured': true},
      });
      expect(u.company?.name, 'Acme');
      expect(u.company?.logoVersion, 2);
      expect(u.needsCompanySetup, isFalse);
    });

    test('only a company.manage holder is forced into the wizard', () {
      expect(user(['company.manage'], unconfigured).needsCompanySetup, isTrue);
      expect(user(const [], unconfigured).needsCompanySetup, isFalse);
      expect(user(['company.manage'], null).needsCompanySetup, isFalse);
    });

    test('redirectFor traps the first admin in the wizard', () {
      final s = AuthAuthenticated(user(['company.manage'], unconfigured));
      expect(redirectFor(s, Routes.home), Routes.companySetup);
      expect(redirectFor(s, Routes.companySetup), isNull);
      final done = AuthAuthenticated(user(['company.manage'], const CompanyInfo(configured: true)));
      expect(redirectFor(done, Routes.companySetup), Routes.home);
    });

    test('server setup and access removed gate everything', () {
      expect(redirectFor(const AuthNeedsServer(), Routes.login), Routes.serverSetup);
      expect(redirectFor(const AuthNeedsServer(), Routes.serverSetup), isNull);
      expect(redirectFor(const AuthAccessRemoved(), Routes.home), Routes.accessRemoved);
      expect(redirectFor(const AuthAccessRemoved(), Routes.accessRemoved), isNull);
    });

    test('public company needs the employee-connect product marker', () {
      expect(CompanyInfo.parse({'product': 'employee-connect', 'name': 'X'})!.isEmployeeConnect, isTrue);
      expect(CompanyInfo.parse({'name': 'X'})!.isEmployeeConnect, isFalse);
    });
  });

  group('ops models', () {
    test('company name and logo validation', () {
      expect(validateCompanyName(' A '), isNotNull);
      expect(validateCompanyName('Acme'), isNull);
      expect(validateCompanyName('x' * 101), isNotNull);
      expect(validateLogoFile('logo.svg', 10), isNotNull);
      expect(validateLogoFile('logo.PNG', 10), isNull);
      expect(validateLogoFile('logo.jpg', 3 * 1024 * 1024), isNotNull);
    });

    test('license key format check', () {
      expect(validateLicenseKey(''), isNotNull);
      expect(validateLicenseKey('abc'), isNotNull);
      expect(validateLicenseKey('ECL1.payload'), isNotNull);
      expect(validateLicenseKey('ECL1.payload.sig'), isNull);
      expect(validateLicenseKey('ECL1.pay load.sig'), isNotNull);
    });

    test('license banner reflects status', () {
      LicenseInfo l(String status, {int active = 10, int max = 50, String onExpiry = 'block_new_users'}) =>
          LicenseInfo.fromJson({
            'status': status,
            'max_users': max,
            'active_users': active,
            'seats_available': max - active,
            'on_expiry': onExpiry,
            'deployment_id': 'd',
            'grace_until': '2026-11-01T00:00:00Z',
          });
      expect(licenseBanner(l('valid')), isNull);
      expect(licenseBanner(l('grace')), contains('grace'));
      expect(licenseBanner(l('expired', onExpiry: 'read_only')), contains('read-only'));
      expect(licenseBanner(l('expired')), contains('cannot be added'));
      expect(licenseBanner(l('valid', active: 60)), contains('More employees'));
      expect(seatsHint(l('valid', active: 50)), contains('No seats left'));
      expect(seatsHint(l('valid', active: 47)), contains('3 seats left'));
    });

    test('license and user limit errors have clear texts', () {
      const full = ApiException(
        code: 'USER_LIMIT_REACHED',
        message: 'x',
        status: 409,
        details: [FieldError('max_users', '50'), FieldError('active_users', '50')],
      );
      expect(adminErrorMessage(full), contains('(50)'));
      expect(adminErrorMessage(const ApiException(code: 'LICENSE_EXPIRED', message: 'x')), contains('expired'));
      expect(adminErrorMessage(const ApiException(code: 'LICENSE_OUTDATED', message: 'x')), contains('newer'));
    });

    test('deactivation reasons match the contract codes', () {
      expect(DeactivationReason.values.map((r) => r.code).toList(),
          ['resigned', 'terminated', 'contract_end', 'absconded', 'other'],);
      expect(DeactivationReason.labelFor('contract_end'), 'Contract ended');
    });

    test('employee carries the deactivation info', () {
      final e = Employee.fromJson({
        'id': '1',
        'employee_id': 'E1',
        'full_name': 'A',
        'status': 'inactive',
        'deactivated_at': '2026-10-01T10:00:00Z',
        'deactivation_reason': 'resigned',
      });
      expect(e.deactivatedAt, isNotNull);
      expect(e.deactivationReason, 'resigned');
    });

    test('session item and permissions', () {
      final s = SessionItem.fromJson({
        'id': 'f1',
        'user': {'id': 'u', 'employee_id': 'E1', 'full_name': 'Asha'},
        'client_type': 'web',
        'platform': 'web',
        'device_name': null,
        'app_version': '1.4.0',
        'ip': '203.0.113.7',
        'last_seen_at': '2026-10-22T09:00:00Z',
      });
      expect(s.isWeb, isTrue);
      expect(s.deviceLabel, 'web, v1.4.0');
      expect(adminSections(['session.manage', 'license.manage', 'company.manage']),
          [AdminSection.sessions, AdminSection.license, AdminSection.company],);
    });

    test('dashboard hides blocks the server nulled', () {
      final d = Dashboard.fromJson({
        'date': '2026-10-22',
        'employees': {'active': 120, 'inactive': 8},
        'attendance': null,
        'requests': {'leave_pending': 4, 'permission_pending': 2},
        'sessions': null,
        'password_reset_requests_open': 1,
        'seats': {'used': 120, 'total': 150, 'status': 'valid'},
      });
      expect(d.attendance, isNull);
      expect(d.sessions, isNull);
      expect(d.pendingRequests, 6);
      expect(d.seats!.total, 150);
      expect(d.resetOpen, 1);
    });

    test('system health degraded', () {
      final h = SystemHealth.fromJson({
        'status': 'degraded',
        'db': {'ok': true},
        'redis': {'ok': false},
        'worker': {'ok': true},
        'backup': {'ok': true},
        'tls': {'days_left': 5},
        'storage': {'uploads_free_pct': 40},
        'push': {'configured': false},
      });
      expect(h.degraded, isTrue);
      expect(h.checks['Redis'], isFalse);
      expect(h.tlsDaysLeft, 5);
    });
  });
}
