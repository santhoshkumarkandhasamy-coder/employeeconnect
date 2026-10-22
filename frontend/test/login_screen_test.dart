import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/security/biometric_gate.dart';
import 'package:employee_connect/core/storage/secure_storage.dart';
import 'package:employee_connect/features/auth/data/auth_repository.dart';
import 'package:employee_connect/features/auth/presentation/login_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fakes.dart';

Widget _app(FakeAuthRepository repo) => ProviderScope(
      overrides: [
        authRepositoryProvider.overrideWithValue(repo),
        tokenStoreProvider.overrideWithValue(MemoryTokenStore()),
        biometricGateProvider.overrideWithValue(FakeBiometricGate()),
      ],
      child: const MaterialApp(home: LoginScreen()),
    );

void main() {
  testWidgets('validates empty fields before calling the API', (tester) async {
    final repo = FakeAuthRepository();
    await tester.pumpWidget(_app(repo));
    await tester.pump();

    await tester.tap(find.text('Sign in'));
    await tester.pump();

    expect(find.text('Enter your Employee ID'), findsOneWidget);
    expect(find.text('Enter your password'), findsOneWidget);
    expect(repo.loginCalls, isEmpty);
  });

  testWidgets('submits trimmed credentials', (tester) async {
    final repo = FakeAuthRepository();
    await tester.pumpWidget(_app(repo));
    await tester.pump();

    await tester.enterText(find.widgetWithText(TextFormField, 'Employee ID'), ' EMP001 ');
    await tester.enterText(find.widgetWithText(TextFormField, 'Password'), 'Secret1!x');
    await tester.tap(find.text('Sign in'));
    await tester.pump();

    expect(repo.loginCalls, ['EMP001:Secret1!x']);
  });

  testWidgets('shows server error message on invalid credentials', (tester) async {
    final repo = FakeAuthRepository()
      ..loginError = const ApiException(
        code: 'INVALID_CREDENTIALS',
        message: 'Invalid Employee ID or password.',
        status: 401,
      );
    await tester.pumpWidget(_app(repo));
    await tester.pump();

    await tester.enterText(find.widgetWithText(TextFormField, 'Employee ID'), 'EMP001');
    await tester.enterText(find.widgetWithText(TextFormField, 'Password'), 'wrong');
    await tester.tap(find.text('Sign in'));
    await tester.pump();
    await tester.pump();

    expect(find.text('Invalid Employee ID or password.'), findsOneWidget);
  });

  testWidgets('expired temporary password tells the user to ask HR or Admin', (tester) async {
    final repo = FakeAuthRepository()
      ..loginError = const ApiException(
        code: 'TEMP_PASSWORD_EXPIRED',
        message: 'raw server text',
        status: 401,
      );
    await tester.pumpWidget(_app(repo));
    await tester.pump();

    await tester.enterText(find.widgetWithText(TextFormField, 'Employee ID'), 'EMP001');
    await tester.enterText(find.widgetWithText(TextFormField, 'Password'), 'Old!Temp1x');
    await tester.tap(find.text('Sign in'));
    await tester.pump();
    await tester.pump();

    expect(find.textContaining('Ask HR or Admin'), findsOneWidget);
    expect(find.text('raw server text'), findsNothing);
  });

  testWidgets('shows a lockout countdown and disables sign in', (tester) async {
    final until = DateTime.now().add(const Duration(minutes: 5)).toUtc();
    final repo = FakeAuthRepository()
      ..loginError = ApiException(
        code: 'ACCOUNT_LOCKED',
        message: 'Locked',
        status: 423,
        details: [FieldError('locked_until', until.toIso8601String())],
      );
    await tester.pumpWidget(_app(repo));
    await tester.pump();

    await tester.enterText(find.widgetWithText(TextFormField, 'Employee ID'), 'EMP001');
    await tester.enterText(find.widgetWithText(TextFormField, 'Password'), 'x');
    await tester.tap(find.text('Sign in'));
    await tester.pump();
    await tester.pump();

    expect(find.textContaining('Account locked. Try again in'), findsOneWidget);
    final button = tester.widget<FilledButton>(find.byType(FilledButton));
    expect(button.onPressed, isNull);

    // Dispose the screen so the periodic ticker is cancelled.
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('403 ACCOUNT_INACTIVE shows its message without a countdown', (tester) async {
    final repo = FakeAuthRepository()
      ..loginError = const ApiException(
        code: 'ACCOUNT_INACTIVE',
        message: 'This account is inactive.',
        status: 403,
      );
    await tester.pumpWidget(_app(repo));
    await tester.pump();

    await tester.enterText(find.widgetWithText(TextFormField, 'Employee ID'), 'EMP001');
    await tester.enterText(find.widgetWithText(TextFormField, 'Password'), 'x');
    await tester.tap(find.text('Sign in'));
    await tester.pump();
    await tester.pump();

    expect(find.text('This account is inactive.'), findsOneWidget);
    expect(find.textContaining('Try again in'), findsNothing);
    final button = tester.widget<FilledButton>(find.byType(FilledButton));
    expect(button.onPressed, isNotNull);
  });
}
