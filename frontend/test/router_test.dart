import 'package:employee_connect/app/router.dart';
import 'package:employee_connect/features/auth/presentation/auth_state.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fakes.dart';

void main() {
  test('unauthenticated users are sent to login', () {
    const s = AuthUnauthenticated();
    expect(redirectFor(s, Routes.home), Routes.login);
    expect(redirectFor(s, Routes.login), isNull);
  });

  test('initial state stays on splash', () {
    expect(redirectFor(const AuthInitial(), Routes.login), Routes.splash);
    expect(redirectFor(const AuthInitial(), Routes.splash), isNull);
  });

  test('forced password change traps navigation', () {
    const s = AuthNeedsPasswordChange(testUser);
    expect(redirectFor(s, Routes.home), Routes.changePassword);
    expect(redirectFor(s, Routes.changePassword), isNull);
  });

  test('locked and blocked states gate everything', () {
    expect(redirectFor(const AuthLocked(testUser), Routes.home), Routes.locked);
    expect(redirectFor(const AuthBlocked(), Routes.home), Routes.blocked);
  });

  test('authenticated users leave gate screens, keep app routes', () {
    const s = AuthAuthenticated(testUser);
    expect(redirectFor(s, Routes.login), Routes.home);
    expect(redirectFor(s, Routes.splash), Routes.home);
    expect(redirectFor(s, Routes.leave), isNull);
  });
}
