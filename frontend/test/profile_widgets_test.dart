import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/files/file_service.dart';
import 'package:employee_connect/core/security/screen_protect.dart';
import 'package:employee_connect/features/leave/data/attachment_service.dart';
import 'package:employee_connect/features/profile/data/profile_repository.dart';
import 'package:employee_connect/features/profile/presentation/profile_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/module_fakes.dart';

class _FakeAttachments implements AttachmentService {
  final List<AttachmentSource> asked = [];
  final List<String> discarded = [];

  @override
  Future<PickedAttachment?> pick(AttachmentSource source) async {
    asked.add(source);
    return const PickedAttachment('/tmp/p.jpg', 'p.jpg');
  }

  @override
  Future<void> discard(String path) async => discarded.add(path);
}

List<Override> _o(FakeProfileRepo repo, {AttachmentService? att}) => [
      profileRepositoryProvider.overrideWithValue(repo),
      fileServiceProvider.overrideWithValue(FakeFileService()),
      if (att != null) attachmentServiceProvider.overrideWithValue(att),
    ];

void _tall(WidgetTester t) {
  t.view.physicalSize = const Size(800, 3200);
  t.view.devicePixelRatio = 1;
  addTearDown(t.view.reset);
}

Future<void> _edit(WidgetTester tester) async {
  await tester.tap(find.byTooltip('Edit profile'));
  await tester.pumpAndSettle();
}

Finder _field(String label) => find.widgetWithText(TextFormField, label).first;

void main() {
  setUp(ScreenProtect.debugReset);

  testWidgets('shows profile data inside a SecureScreen (screenshots blocked)', (tester) async {
    _tall(tester);
    final repo = FakeProfileRepo(sampleProfile());
    await tester.pumpWidget(app(const ProfileScreen(), _o(repo)));
    await tester.pumpAndSettle();
    expect(ScreenProtect.holders, 1);
    expect(find.text('Asha Rao'), findsOneWidget);
    expect(find.textContaining('EMP001'), findsOneWidget);
    expect(find.text('asha@corp.com'), findsOneWidget);
    expect(find.text('+919999999999'), findsOneWidget);
    expect(find.text('12 MG Road'), findsOneWidget);
    // The secure flag is released again when the screen goes away.
    await tester.pumpWidget(const SizedBox.shrink());
    expect(ScreenProtect.holders, 0);
  });

  testWidgets('offline copy hides personal details and disables editing', (tester) async {
    _tall(tester);
    final repo = FakeProfileRepo(sampleProfile(), stale: true);
    await tester.pumpWidget(app(const ProfileScreen(), _o(repo), mode: ThemeMode.dark));
    await tester.pumpAndSettle();
    expect(find.text('Asha Rao'), findsOneWidget);
    expect(find.text('Offline. Showing saved data.'), findsOneWidget);
    expect(find.textContaining('not stored on this device'), findsOneWidget);
    expect(find.byTooltip('Edit profile'), findsNothing);
    expect(find.text('+919999999999'), findsNothing);
    // The celebrations switch is part of the personal section, so it is hidden too.
    expect(find.byKey(const ValueKey('celebrations-switch')), findsNothing);
  });

  testWidgets('load error shows retry', (tester) async {
    final repo = FakeProfileRepo(sampleProfile())
      ..loadError = const ApiException(code: ApiException.networkCode, message: 'No connection.');
    await tester.pumpWidget(app(const ProfileScreen(), _o(repo)));
    await tester.pumpAndSettle();
    expect(find.text('No connection.'), findsOneWidget);
    repo.loadError = null;
    await tester.tap(find.text('Try again'));
    await tester.pumpAndSettle();
    expect(find.text('Asha Rao'), findsOneWidget);
  });

  group('edit', () {
    testWidgets('invalid phone blocks saving', (tester) async {
      _tall(tester);
      final repo = FakeProfileRepo(sampleProfile());
      await tester.pumpWidget(app(const ProfileScreen(), _o(repo)));
      await tester.pumpAndSettle();
      await _edit(tester);
      await tester.enterText(_field('Phone'), '12');
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Enter 8 to 15 digits'), findsOneWidget);
      expect(repo.updates, isEmpty);
    });

    testWidgets('emergency name needs a phone', (tester) async {
      _tall(tester);
      final repo = FakeProfileRepo(sampleProfile());
      await tester.pumpWidget(app(const ProfileScreen(), _o(repo)));
      await tester.pumpAndSettle();
      await _edit(tester);
      await tester.enterText(_field('Name'), 'Ravi');
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(find.text('Enter the contact phone'), findsOneWidget);
      expect(repo.updates, isEmpty);
    });

    testWidgets('saves only what changed and returns to view mode', (tester) async {
      _tall(tester);
      final repo = FakeProfileRepo(sampleProfile());
      await tester.pumpWidget(app(const ProfileScreen(), _o(repo)));
      await tester.pumpAndSettle();
      await _edit(tester);
      await tester.enterText(_field('Phone'), '+91 98765 43210');
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(repo.updates, [
        {'phone': '+919876543210'},
      ]);
      expect(find.text('My profile'), findsOneWidget);
      expect(find.text('Profile saved.'), findsOneWidget);
    });

    testWidgets('server validation message is shown on the form', (tester) async {
      _tall(tester);
      final repo = FakeProfileRepo(sampleProfile())
        ..saveError = const ApiException(
          code: 'VALIDATION_ERROR',
          message: 'bad',
          details: [FieldError('phone', 'Phone already used')],
        );
      await tester.pumpWidget(app(const ProfileScreen(), _o(repo)));
      await tester.pumpAndSettle();
      await _edit(tester);
      await tester.enterText(_field('Phone'), '+919876543210');
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(find.text('Phone already used'), findsOneWidget);
      expect(find.text('Edit profile'), findsOneWidget); // still editing
    });

    testWidgets('cancel with unsaved changes asks before discarding', (tester) async {
      _tall(tester);
      final repo = FakeProfileRepo(sampleProfile());
      await tester.pumpWidget(app(const ProfileScreen(), _o(repo)));
      await tester.pumpAndSettle();
      await _edit(tester);
      await tester.enterText(_field('Phone'), '+919876543210');
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();
      expect(find.text('Discard changes?'), findsOneWidget);
      await tester.tap(find.text('Keep editing'));
      await tester.pumpAndSettle();
      expect(find.text('Edit profile'), findsOneWidget);
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Discard'));
      await tester.pumpAndSettle();
      expect(find.text('My profile'), findsOneWidget);
      expect(repo.updates, isEmpty);
    });

  });

  testWidgets('change photo uploads the picked image and removes the local copy', (tester) async {
    _tall(tester);
    final repo = FakeProfileRepo(sampleProfile());
    final att = _FakeAttachments();
    await tester.pumpWidget(app(const ProfileScreen(), _o(repo, att: att)));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Change photo'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Choose from gallery'));
    await tester.pumpAndSettle();
    expect(att.asked, [AttachmentSource.gallery]);
    expect(repo.photoUploads, 1);
    expect(att.discarded, ['/tmp/p.jpg']);
    expect(find.text('Photo updated.'), findsOneWidget);
  });
}
