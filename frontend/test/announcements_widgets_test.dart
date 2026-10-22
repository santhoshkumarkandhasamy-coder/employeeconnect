import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/files/file_service.dart';
import 'package:employee_connect/features/announcements/data/announcement_file_picker.dart';
import 'package:employee_connect/features/announcements/data/announcement_repository.dart';
import 'package:employee_connect/features/announcements/domain/announcement_models.dart';
import 'package:employee_connect/features/announcements/presentation/admin_announcements_screen.dart';
import 'package:employee_connect/features/announcements/presentation/announcement_detail_screen.dart';
import 'package:employee_connect/features/announcements/presentation/announcement_form_screen.dart';
import 'package:employee_connect/features/announcements/presentation/announcements_screen.dart';
import 'package:employee_connect/features/auth/presentation/auth_controller.dart';
import 'package:employee_connect/features/food/data/food_repository.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/module_fakes.dart';

List<Override> _o(
  FakeAnnouncementRepo repo, {
  List<String>? perms,
  FakeFileService? files,
  MemExportStore? store,
  FakeOpener? opener,
  FakePicker? picker,
}) =>
    [
      announcementRepositoryProvider.overrideWithValue(repo),
      if (perms != null) authControllerProvider.overrideWith(() => FakeAuth(userWith(perms))),
      fileServiceProvider.overrideWithValue(files ?? FakeFileService()),
      if (store != null) exportStoreProvider.overrideWithValue(store),
      if (opener != null) fileOpenerProvider.overrideWithValue(opener),
      if (picker != null) announcementFilePickerProvider.overrideWithValue(picker),
    ];

/// Pushes [screen] on top of a home route so `Navigator.pop` has somewhere to go.
Widget _host(Widget screen) => Builder(
      builder: (context) => Scaffold(
        body: Center(
          child: ElevatedButton(
            onPressed: () => Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => screen)),
            child: const Text('open'),
          ),
        ),
      ),
    );

Future<void> _openForm(WidgetTester tester) async {
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
}

void _tall(WidgetTester t) {
  t.view.physicalSize = const Size(800, 2400);
  t.view.devicePixelRatio = 1;
  addTearDown(t.view.reset);
}

void main() {
  group('employee list', () {
    testWidgets('shows unread dot, priority badge and pins urgent first', (tester) async {
      final repo = FakeAnnouncementRepo([
        ann('1', title: 'Lunch menu', isRead: true),
        ann('2', title: 'Fire drill', priority: AnnouncementPriority.urgent),
        ann('3', title: 'Policy update', priority: AnnouncementPriority.important, isRead: true),
      ]);
      await tester.pumpWidget(app(const AnnouncementsScreen(), _o(repo)));
      await tester.pumpAndSettle();

      expect(find.text('Urgent'), findsOneWidget);
      expect(find.text('Important'), findsOneWidget);
      expect(find.byKey(const ValueKey('unread-dot')), findsOneWidget);
      final urgentY = tester.getTopLeft(find.text('Fire drill')).dy;
      expect(urgentY, lessThan(tester.getTopLeft(find.text('Lunch menu')).dy));
    });

    testWidgets('empty, unread-only empty, error with retry and offline banner', (tester) async {
      final repo = FakeAnnouncementRepo([]);
      await tester.pumpWidget(app(const AnnouncementsScreen(), _o(repo), mode: ThemeMode.dark));
      await tester.pumpAndSettle();
      expect(find.text('No announcements yet.'), findsOneWidget);

      await tester.tap(find.text('Unread'));
      await tester.pumpAndSettle();
      expect(find.textContaining('all caught up'), findsOneWidget);

      repo.listError = const ApiException(code: ApiException.networkCode, message: 'No connection.');
      await tester.tap(find.text('All'));
      await tester.pumpAndSettle();
      expect(find.text('No connection.'), findsOneWidget);

      repo
        ..listError = null
        ..stale = true
        ..items = [ann('1', title: 'Cached one')];
      await tester.tap(find.text('Try again'));
      await tester.pumpAndSettle();
      expect(find.text('Cached one'), findsOneWidget);
      expect(find.text('Offline. Showing saved data.'), findsOneWidget);
    });

    testWidgets('loads the next page when scrolling to the end', (tester) async {
      final repo = FakeAnnouncementRepo([
        for (var i = 0; i < 12; i++) ann('$i', title: 'Item $i', isRead: true),
      ])
        ..pageSize = 6;
      await tester.pumpWidget(app(const AnnouncementsScreen(), _o(repo)));
      await tester.pumpAndSettle();
      expect(repo.listPages, [1]);
      await tester.drag(find.byType(ListView), const Offset(0, -3000));
      await tester.pumpAndSettle();
      expect(repo.listPages, contains(2));
      await tester.drag(find.byType(ListView), const Offset(0, -3000));
      await tester.pumpAndSettle();
      expect(find.text('Item 11'), findsOneWidget);
    });

    testWidgets('opening an unread item marks it read and shows the body as plain text', (tester) async {
      final repo = FakeAnnouncementRepo([
        ann('7', title: 'Office closed', body: '<b>bold?</b> Friday off'),
      ]);
      await tester.pumpWidget(app(const AnnouncementsScreen(), _o(repo)));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Office closed'));
      await tester.pumpAndSettle();
      expect(repo.readIds, ['7']);
      // Rendered literally, never interpreted as markup.
      expect(find.text('<b>bold?</b> Friday off'), findsOneWidget);

      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('unread-dot')), findsNothing);
    });
  });

  group('detail', () {
    testWidgets('a deleted / untargeted announcement shows a not-available message', (tester) async {
      final repo = FakeAnnouncementRepo([])
        ..detailError = const ApiException(code: 'NOT_FOUND', message: 'nope', status: 404);
      await tester.pumpWidget(app(const AnnouncementDetailScreen(id: 'gone'), _o(repo)));
      await tester.pumpAndSettle();
      expect(find.textContaining('no longer available'), findsOneWidget);
      expect(repo.readIds, isEmpty);
    });

    testWidgets('image goes through the authenticated file service, not Image.network', (tester) async {
      final files = FakeFileService();
      final a = ann('9', imageUrl: '/api/files/aaaaaaaa-1111-2222-3333-444444444444');
      final repo = FakeAnnouncementRepo([a]);
      await tester.pumpWidget(app(AnnouncementDetailScreen(id: '9', initial: a), _o(repo, files: files)));
      await tester.pumpAndSettle();
      expect(files.requested, ['/api/files/aaaaaaaa-1111-2222-3333-444444444444']);
      final images = tester.widgetList<Image>(find.byType(Image));
      expect(images, isNotEmpty);
      expect(images.every((i) => i.image is MemoryImage), isTrue);
    });

    testWidgets('PDF is saved to private storage and handed to the opener', (tester) async {
      final files = FakeFileService()..data = tinyPdf;
      final store = MemExportStore();
      final opener = FakeOpener();
      final a = ann('p1', pdfUrl: '/api/files/bbbbbbbb-1111-2222-3333-444444444444', isRead: true);
      final repo = FakeAnnouncementRepo([a]);
      await tester.pumpWidget(app(
        AnnouncementDetailScreen(id: 'p1', initial: a),
        _o(repo, files: files, store: store, opener: opener),
      ),);
      await tester.pumpAndSettle();
      await tester.tap(find.text('PDF attachment'));
      // The busy indicator animates while the warning is open: no pumpAndSettle.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      // Warning before the file leaves the app (review 07 L7).
      expect(find.text('Open outside the app?'), findsOneWidget);
      expect(store.saved, isEmpty);
      expect(opener.opened, isEmpty);
      await tester.tap(find.text('Continue'));
      await tester.pumpAndSettle();
      expect(store.saved.keys, ['announcement-p1.pdf']);
      expect(opener.opened.single, endsWith('announcement-p1.pdf'));
    });

    testWidgets('cancelling the share warning keeps the PDF inside the app', (tester) async {
      final files = FakeFileService()..data = tinyPdf;
      final store = MemExportStore();
      final opener = FakeOpener();
      final a = ann('p3', pdfUrl: '/api/files/bbbbbbbb-1111-2222-3333-444444444444', isRead: true);
      await tester.pumpWidget(app(
        AnnouncementDetailScreen(id: 'p3', initial: a),
        _o(FakeAnnouncementRepo([a]), files: files, store: store, opener: opener),
      ),);
      await tester.pumpAndSettle();
      await tester.tap(find.text('PDF attachment'));
      // The busy indicator animates while the warning is open: no pumpAndSettle.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();
      expect(store.saved, isEmpty);
      expect(opener.opened, isEmpty);
    });

    testWidgets('shows "Edited by" only when the server sends updated_by', (tester) async {
      final edited = Announcement.fromJson({
        'id': 'e1',
        'title': 'T',
        'body': 'B',
        'is_read': true,
        'published_at': '2026-10-01T09:00:00Z',
        'updated_at': '2026-10-02T09:00:00Z',
        'updated_by': {'id': 'u9', 'employee_id': 'E9', 'full_name': 'Hema HR'},
      });
      await tester.pumpWidget(app(
        AnnouncementDetailScreen(id: 'e1', initial: edited),
        _o(FakeAnnouncementRepo([edited])),
      ),);
      await tester.pumpAndSettle();
      expect(find.textContaining('Edited by Hema HR'), findsOneWidget);

      final plain = ann('e2', isRead: true);
      await tester.pumpWidget(app(
        AnnouncementDetailScreen(id: 'e2', initial: plain),
        _o(FakeAnnouncementRepo([plain])),
      ),);
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('edited-label')), findsNothing);
    });

    testWidgets('PDF that cannot be opened shows where it was saved; fake PDFs are refused', (tester) async {
      final files = FakeFileService()..data = tinyPdf;
      final store = MemExportStore();
      final a = ann('p2', pdfUrl: '/api/files/bbbbbbbb-1111-2222-3333-444444444444', isRead: true);
      final repo = FakeAnnouncementRepo([a]);
      await tester.pumpWidget(app(
        AnnouncementDetailScreen(id: 'p2', initial: a),
        _o(repo, files: files, store: store, opener: FakeOpener(result: false)),
      ),);
      await tester.pumpAndSettle();
      await tester.tap(find.text('PDF attachment'));
      // The busy indicator animates while the warning is open: no pumpAndSettle.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      await tester.tap(find.text('Continue'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      expect(find.text('PDF saved'), findsOneWidget);
      await tester.tap(find.text('Close'));
      await tester.pumpAndSettle();

      files.data = tinyPng;
      store.saved.clear();
      await tester.tap(find.text('PDF attachment'));
      // The busy indicator animates while the warning is open: no pumpAndSettle.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      expect(find.text('This file is not a valid PDF.'), findsOneWidget);
      expect(store.saved, isEmpty);
    });
  });

  group('admin', () {
    testWidgets('users without announcement.manage see no admin list', (tester) async {
      final repo = FakeAnnouncementRepo([ann('1')]);
      await tester.pumpWidget(app(const AdminAnnouncementsScreen(), _o(repo, perms: ['food.view_all'])));
      await tester.pumpAndSettle();
      expect(find.textContaining('do not have permission'), findsOneWidget);
      expect(find.text('New'), findsNothing);
    });

    testWidgets('list, delete with confirmation', (tester) async {
      final repo = FakeAnnouncementRepo([
        ann('1', title: 'Old news'),
        ann('2', title: 'Fresh news'),
      ]);
      await tester.pumpWidget(app(const AdminAnnouncementsScreen(), _o(repo, perms: ['announcement.manage'])));
      await tester.pumpAndSettle();
      expect(find.text('Old news'), findsOneWidget);
      expect(find.text('New'), findsOneWidget);

      await tester.tap(find.byType(PopupMenuButton<String>).first);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Delete'));
      await tester.pumpAndSettle();
      expect(find.text('Delete announcement?'), findsOneWidget);
      await tester.tap(find.widgetWithText(FilledButton, 'Delete'));
      await tester.pumpAndSettle();
      expect(repo.deleted, ['1']);
      expect(find.text('Old news'), findsNothing);
    });

    testWidgets('empty list prompts to create', (tester) async {
      final repo = FakeAnnouncementRepo([]);
      await tester.pumpWidget(app(const AdminAnnouncementsScreen(), _o(repo, perms: ['announcement.manage'])));
      await tester.pumpAndSettle();
      expect(find.textContaining('No announcements yet'), findsOneWidget);
    });

    testWidgets('form validates title, text and audience before saving', (tester) async {
      _tall(tester);
      final repo = FakeAnnouncementRepo([]);
      await tester.pumpWidget(app(_host(const AnnouncementFormScreen()), _o(repo, perms: ['announcement.manage'])));
      await tester.pumpAndSettle();
      await _openForm(tester);

      await tester.tap(find.text('Publish'));
      await tester.pumpAndSettle();
      expect(find.text('Enter a title'), findsOneWidget);
      expect(find.text('Enter the announcement text'), findsOneWidget);
      expect(repo.created, isEmpty);

      await tester.enterText(find.widgetWithText(TextFormField, 'Title'), 'Holiday');
      await tester.enterText(find.widgetWithText(TextFormField, 'Text'), 'Closed on Friday');
      await tester.tap(find.byType(SwitchListTile));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Publish'));
      await tester.pumpAndSettle();
      expect(find.text('Choose at least one department'), findsOneWidget);
      expect(repo.created, isEmpty);
    });

    testWidgets('publishing sends the contract body with uploaded attachments', (tester) async {
      _tall(tester);
      final repo = FakeAnnouncementRepo([]);
      final picker = FakePicker()
        ..image = const PickedFile(path: '/tmp/a.png', name: 'a.png', sizeBytes: 1000)
        ..pdf = const PickedFile(path: '/tmp/a.pdf', name: 'a.pdf', sizeBytes: 2000);
      await tester.pumpWidget(app(
        _host(const AnnouncementFormScreen()),
        _o(repo, perms: ['announcement.manage'], picker: picker),
      ),);
      await tester.pumpAndSettle();
      await _openForm(tester);

      await tester.enterText(find.widgetWithText(TextFormField, 'Title'), 'Holiday');
      await tester.enterText(find.widgetWithText(TextFormField, 'Text'), 'Closed on Friday');
      await tester.tap(find.text('Urgent'));
      await tester.tap(find.byType(SwitchListTile));
      await tester.pumpAndSettle();
      await tester.tap(find.text('IT'));
      await tester.tap(find.text('Image (JPEG or PNG, up to 5 MB)'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('PDF (up to 10 MB)'));
      await tester.pumpAndSettle();
      expect(find.text('a.png'), findsOneWidget);
      expect(find.text('a.pdf'), findsOneWidget);

      await tester.tap(find.text('Publish'));
      await tester.pumpAndSettle();

      expect(repo.uploads.map((u) => u.pdf), [false, true]);
      final body = repo.created.single;
      expect(body['title'], 'Holiday');
      expect(body['priority'], 'urgent');
      expect(body['target_all'], false);
      expect(body['department_ids'], ['d1']);
      expect(body['image_file_id'], '22222222-aaaa-bbbb-cccc-000000000002');
      expect(body['pdf_file_id'], '11111111-aaaa-bbbb-cccc-000000000001');
    });

    testWidgets('oversized files are rejected before upload', (tester) async {
      _tall(tester);
      final repo = FakeAnnouncementRepo([]);
      final picker = FakePicker()
        ..image = const PickedFile(path: '/tmp/big.png', name: 'big.png', sizeBytes: 6 * 1024 * 1024);
      await tester.pumpWidget(app(
        _host(const AnnouncementFormScreen()),
        _o(repo, perms: ['announcement.manage'], picker: picker),
      ),);
      await tester.pumpAndSettle();
      await _openForm(tester);
      await tester.tap(find.text('Image (JPEG or PNG, up to 5 MB)'));
      await tester.pumpAndSettle();
      expect(find.textContaining('too large'), findsOneWidget);
      expect(find.text('big.png'), findsNothing);
    });

    testWidgets('editing keeps existing attachment ids and calls update', (tester) async {
      _tall(tester);
      final existing = ann(
        'e1',
        title: 'Old title',
        imageUrl: '/api/files/aaaaaaaa-1111-2222-3333-444444444444',
      );
      final repo = FakeAnnouncementRepo([existing]);
      await tester.pumpWidget(app(
        _host(AnnouncementFormScreen(existing: existing)),
        _o(repo, perms: ['announcement.manage']),
      ),);
      await tester.pumpAndSettle();
      await _openForm(tester);
      await tester.enterText(find.widgetWithText(TextFormField, 'Title'), 'New title');
      await tester.tap(find.text('Save changes'));
      await tester.pumpAndSettle();
      final (id, body) = repo.updated.single;
      expect(id, 'e1');
      expect(body['title'], 'New title');
      expect(body['image_file_id'], 'aaaaaaaa-1111-2222-3333-444444444444');
      expect(body['pdf_file_id'], isNull);
    });

    testWidgets('editing sends the version; 409 asks to reload', (tester) async {
      _tall(tester);
      final existing = ann('e1', title: 'Old title', version: 5);
      final repo = FakeAnnouncementRepo([existing])
        ..saveError = const ApiException(code: 'CONFLICT', message: 'version', status: 409);
      await tester.pumpWidget(app(
        _host(AnnouncementFormScreen(existing: existing)),
        _o(repo, perms: ['announcement.manage']),
      ),);
      await tester.pumpAndSettle();
      await _openForm(tester);
      await tester.tap(find.text('Save changes'));
      await tester.pumpAndSettle();
      expect(find.text('Announcement changed'), findsOneWidget);
      expect(find.text('Reload'), findsOneWidget);
    });

    testWidgets('editing an expired announcement needs a new expiry', (tester) async {
      _tall(tester);
      final existing = ann(
        'e2',
        title: 'Old',
        version: 2,
        expiresAt: DateTime.now().subtract(const Duration(days: 1)),
      );
      final repo = FakeAnnouncementRepo([existing]);
      await tester.pumpWidget(app(
        _host(AnnouncementFormScreen(existing: existing)),
        _o(repo, perms: ['announcement.manage']),
      ),);
      await tester.pumpAndSettle();
      await _openForm(tester);
      await tester.tap(find.text('Save changes'));
      await tester.pumpAndSettle();
      expect(repo.updated, isEmpty);
      expect(find.text(expiredNeedsNewExpiry), findsWidgets);
    });

    testWidgets('server validation errors are shown on the form', (tester) async {
      _tall(tester);
      final repo = FakeAnnouncementRepo([])
        ..saveError = const ApiException(
          code: 'VALIDATION_ERROR',
          message: 'bad',
          details: [FieldError('department_ids', 'Pick a department')],
        );
      await tester.pumpWidget(app(_host(const AnnouncementFormScreen()), _o(repo, perms: ['announcement.manage'])));
      await tester.pumpAndSettle();
      await _openForm(tester);
      await tester.enterText(find.widgetWithText(TextFormField, 'Title'), 'T');
      await tester.enterText(find.widgetWithText(TextFormField, 'Text'), 'B');
      await tester.tap(find.text('Publish'));
      await tester.pumpAndSettle();
      expect(find.text('Pick a department'), findsOneWidget);
    });
  });
}
