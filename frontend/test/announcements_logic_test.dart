import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/cache/json_cache.dart';
import 'package:employee_connect/core/files/file_service.dart';
import 'package:employee_connect/features/announcements/data/announcement_repository.dart';
import 'package:employee_connect/features/announcements/domain/announcement_models.dart';
import 'package:employee_connect/features/announcements/presentation/announcement_widgets.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fakes.dart';
import 'helpers/module_fakes.dart';

void main() {
  group('models', () {
    test('pinUrgent puts urgent first and keeps the rest in order', () {
      final out = pinUrgent([
        ann('a'),
        ann('b', priority: AnnouncementPriority.urgent),
        ann('c', priority: AnnouncementPriority.important),
        ann('d', priority: AnnouncementPriority.urgent),
      ]);
      expect(out.map((e) => e.id), ['b', 'd', 'a', 'c']);
    });

    test('mergePages drops duplicates', () {
      final out = mergePages([ann('a'), ann('b')], [ann('b'), ann('c')]);
      expect(out.map((e) => e.id), ['a', 'b', 'c']);
    });

    test('fromJson parses and strips control characters', () {
      final a = Announcement.fromJson({
        'id': 'x',
        'title': 'Hi\u0000 there\n',
        'body': 'Line1\nLine2\u0007',
        'priority': 'urgent',
        'image_url': '/api/files/aaaaaaaa-1111-2222-3333-444444444444',
        'pdf_url': null,
        'target_all': false,
        'department_ids': ['d1'],
        'published_at': '2026-10-21T04:00:00Z',
        'is_read': true,
        'read_count': 4,
        'target_count': 10,
      });
      expect(a.title, 'Hi there');
      expect(a.body, 'Line1\nLine2');
      expect(a.priority, AnnouncementPriority.urgent);
      expect(a.isRead, isTrue);
      expect(a.imageFileId, 'aaaaaaaa-1111-2222-3333-444444444444');
      expect(a.pdfFileId, isNull);
      expect(a.readCount, 4);
      expect(a.departmentIds, ['d1']);
    });

    test('unknown priority falls back to normal', () {
      expect(AnnouncementPriority.parse('???'), AnnouncementPriority.normal);
    });

    test('relativeTime', () {
      final now = DateTime(2026, 10, 21, 12);
      expect(relativeTime(now.subtract(const Duration(seconds: 20)), now), 'Just now');
      expect(relativeTime(now.subtract(const Duration(minutes: 5)), now), '5 min ago');
      expect(relativeTime(now.subtract(const Duration(hours: 3)), now), '3 h ago');
      expect(relativeTime(now.subtract(const Duration(days: 1)), now), '1 day ago');
      expect(relativeTime(now.subtract(const Duration(days: 20)), now), '1 Oct 2026');
      expect(relativeTime(null, now), '');
    });

    test('badgeText hides zero and caps at 99+', () {
      expect(badgeText(0), isNull);
      expect(badgeText(7), '7');
      expect(badgeText(150), '99+');
    });

    test('ReadStats.parse', () {
      final s = ReadStats.parse({
        'items': [
          {
            'user': {'id': '1', 'employee_id': 'E1', 'full_name': 'Amy'},
            'read_at': '2026-10-21T04:00:00Z',
          },
        ],
        'page': 1,
        'page_size': 1,
        'total': 3,
        'read_count': 3,
        'target_count': 12,
      });
      expect(s.readers.single.user.fullName, 'Amy');
      expect(s.fraction, 0.25);
      expect(s.hasMore, isTrue);
    });
  });

  group('validation', () {
    test('title', () {
      expect(validateAnnouncementTitle('  '), isNotNull);
      expect(validateAnnouncementTitle('a' * 151), isNotNull);
      expect(validateAnnouncementTitle('ok'), isNull);
    });

    test('body', () {
      expect(validateAnnouncementBody(''), isNotNull);
      expect(validateAnnouncementBody('x' * 5001), isNotNull);
      expect(validateAnnouncementBody('x' * 5000), isNull);
    });

    test('targets need a department unless target_all', () {
      expect(validateTargets(targetAll: false, departmentIds: {}), isNotNull);
      expect(validateTargets(targetAll: false, departmentIds: {'d1'}), isNull);
      expect(validateTargets(targetAll: true, departmentIds: {}), isNull);
    });

    test('expiry must be in the future', () {
      final now = DateTime(2026, 10, 21);
      expect(validateExpiry(null, now), isNull);
      expect(validateExpiry(DateTime(2026, 10, 20), now), isNotNull);
      expect(validateExpiry(DateTime(2026, 10, 22), now), isNull);
    });

    test('attachment size limits', () {
      expect(validateAttachmentSize(6 * 1024 * 1024, pdf: false), isNotNull);
      expect(validateAttachmentSize(6 * 1024 * 1024, pdf: true), isNull);
      expect(validateAttachmentSize(11 * 1024 * 1024, pdf: true), isNotNull);
    });

    test('permission gate', () {
      expect(canManageAnnouncements(['announcement.manage']), isTrue);
      expect(canManageAnnouncements(['food.view_all']), isFalse);
    });

    test('request body clears departments when target_all and always sends file keys', () {
      final b = announcementBody(
        title: ' T ',
        body: ' B ',
        priority: AnnouncementPriority.important,
        targetAll: true,
        departmentIds: {'d1'},
      );
      expect(b['title'], 'T');
      expect(b['department_ids'], isEmpty);
      expect(b.containsKey('image_file_id'), isTrue);
      expect(b['image_file_id'], isNull);
      expect(b['priority'], 'important');
    });

    test('error messages are mapped', () {
      expect(
        announcementErrorMessage(const ApiException(code: 'NOT_FOUND', message: 'x')),
        contains('no longer available'),
      );
      expect(
        announcementErrorMessage(const ApiException(code: 'FILE_TYPE_NOT_ALLOWED', message: 'x')),
        contains('not allowed'),
      );
    });
  });

  group('files', () {
    test('fileIdFromUrl accepts only file links', () {
      expect(fileIdFromUrl('/api/files/aaaaaaaa-1111-2222-3333-444444444444'),
          'aaaaaaaa-1111-2222-3333-444444444444',);
      expect(fileIdFromUrl('https://evil.example/api/files/aaaaaaaa-1111'), 'aaaaaaaa-1111');
      expect(fileIdFromUrl('/api/other/aaaaaaaa'), isNull);
      expect(fileIdFromUrl(null), isNull);
      expect(fileIdFromUrl(''), isNull);
    });

    test('looksLikePdf and the local file name', () {
      expect(looksLikePdf(tinyPdf), isTrue);
      expect(looksLikePdf(tinyPng), isFalse);
      expect(pdfFileName('../../etc/passwd'), 'announcement-______etc_passwd.pdf');
    });

    test('FileService requests /files/{id} with the app client and caches in memory', () async {
      final adapter = ScriptedAdapter((o) => (status: 200, body: <String, Object?>{}));
      final dio = Dio(BaseOptions(baseUrl: 'https://x.test/api'))..httpClientAdapter = _BytesAdapter(adapter);
      final svc = FileService(dio);
      const url = 'https://evil.test/api/files/aaaaaaaa-1111-2222';
      final a = await svc.bytes(url);
      final b = await svc.bytes(url);
      expect(a, tinyPng);
      expect(b, tinyPng);
      expect(adapter.requests.length, 1);
      // Always the configured base URL, never the host in the returned link.
      expect(adapter.requests.single.uri.host, 'x.test');
      expect(adapter.requests.single.path, '/files/aaaaaaaa-1111-2222');
      await expectLater(svc.bytes('/api/not-a-file'), throwsA(isA<FormatException>()));
    });
  });

  group('repository', () {
    AnnouncementRepository repo(ScriptedAdapter adapter, {JsonCache? cache}) {
      final dio = Dio(BaseOptions(baseUrl: 'https://x.test/api'))..httpClientAdapter = adapter;
      return AnnouncementRepository(dio, cache ?? MemoryJsonCache(), FileService(dio), scope: 'u1');
    }

    test('list parses a page and caches page 1 for offline use', () async {
      var offline = false;
      final adapter = _SwitchAdapter(() => offline, {
        'items': [
          {'id': 'a1', 'title': 'Hello', 'body': 'B', 'priority': 'normal', 'is_read': false},
        ],
        'page': 1,
        'page_size': 20,
        'total': 1,
      });
      final cache = MemoryJsonCache();
      final r = AnnouncementRepository(
        Dio(BaseOptions(baseUrl: 'https://x.test/api'))..httpClientAdapter = adapter,
        cache,
        FileService(Dio()),
        scope: 'u1',
      );
      final first = await r.list();
      expect(first.value.items.single.title, 'Hello');
      expect(first.stale, isFalse);
      offline = true;
      final second = await r.list();
      expect(second.stale, isTrue);
      expect(second.value.items.single.id, 'a1');
      // Page 2 never falls back to the cache.
      await expectLater(r.list(page: 2), throwsA(isA<ApiException>()));
    });

    test('unreadCount accepts the documented key variants', () async {
      for (final key in ['count', 'unread_count', 'unread']) {
        final r = repo(ScriptedAdapter((o) => (status: 200, body: {key: 4})));
        expect(await r.unreadCount(), 4);
      }
    });

    test('markRead posts to /announcements/{id}/read', () async {
      final adapter = ScriptedAdapter((o) => (status: 204, body: <String, Object?>{}));
      await repo(adapter).markRead('abc');
      expect(adapter.requests.single.method, 'POST');
      expect(adapter.requests.single.path, '/announcements/abc/read');
    });

    test('create sends the contract body to /admin/announcements', () async {
      final adapter = ScriptedAdapter((o) => (
            status: 201,
            body: {'id': 'n1', 'title': 'T', 'body': 'B', 'priority': 'normal'},
          ),);
      final created = await repo(adapter).create(announcementBody(
        title: 'T',
        body: 'B',
        priority: AnnouncementPriority.normal,
        targetAll: false,
        departmentIds: {'d2', 'd1'},
        pdfFileId: 'p1',
      ),);
      expect(created.id, 'n1');
      final req = adapter.requests.single;
      expect(req.path, '/admin/announcements');
      final data = req.data as Map;
      expect(data['department_ids'], ['d1', 'd2']);
      expect(data['pdf_file_id'], 'p1');
      expect(data['target_all'], false);
    });

    test('server errors surface as ApiException with the mapped code', () async {
      final adapter = ScriptedAdapter((o) => (
            status: 404,
            body: {'error': {'code': 'NOT_FOUND', 'message': 'gone'}},
          ),);
      await expectLater(
        repo(adapter).detail('x'),
        throwsA(isA<ApiException>().having((e) => e.code, 'code', 'NOT_FOUND')),
      );
    });

    test('update sends If-Match with the version, none without', () async {
      final adapter = ScriptedAdapter((o) => (status: 200, body: {'id': 'a', 'title': 't', 'body': 'b', 'version': 4}));
      final r = repo(adapter);
      final out = await r.update('a', {'title': 't'}, version: 3);
      expect(out.version, 4);
      expect(adapter.requests.single.method, 'PUT');
      expect(adapter.requests.single.headers['If-Match'], '"3"');
      await r.update('a', {'title': 't'});
      expect(adapter.requests.last.headers.containsKey('If-Match'), isFalse);
    });

    test('409 CONFLICT is detected', () async {
      final adapter = ScriptedAdapter((o) => (
            status: 409,
            body: {'error': {'code': 'CONFLICT', 'message': 'version'}},
          ),);
      await expectLater(
        repo(adapter).update('a', {}, version: 1),
        throwsA(predicate((e) => e is ApiException && isAnnouncementConflict(e))),
      );
    });
  });

  group('review 07 (admin view, errors)', () {
    test('employee view without target fields and with edited info', () {
      final a = Announcement.fromJson({
        'id': 'x',
        'title': 't',
        'body': 'b',
        'published_at': '2026-10-20T04:00:00Z',
        'updated_by': {'id': 'u1', 'full_name': 'Hema HR'},
        'updated_at': '2026-10-21T04:00:00Z',
      });
      expect(a.targetAll, isTrue);
      expect(a.departmentIds, isEmpty);
      expect(a.version, isNull);
      expect(a.editedLabel(DateTime.utc(2026, 10, 22)), contains('Edited'));
    });

    test('admin view parses version and status; expiry detection', () {
      final a = Announcement.fromJson({
        'id': 'x', 'title': 't', 'body': 'b', 'version': 3, 'status': 'expired',
        'target_all': false, 'department_ids': ['d1'],
      });
      expect(a.version, 3);
      expect(a.isExpired(DateTime.now()), isTrue);
    });

    test('renotify is only in the body when requested', () {
      Map<String, Object?> body(bool r) => announcementBody(
            title: 't', body: 'b', priority: AnnouncementPriority.normal,
            targetAll: true, departmentIds: {}, renotify: r,
          );
      expect(body(false).containsKey('renotify'), isFalse);
      expect(body(true)['renotify'], true);
    });

    test('rate limit message includes server text and wait', () {
      final msg = announcementErrorMessage(const ApiException(
        code: 'RATE_LIMITED',
        message: 'Daily notification limit reached.',
        status: 429,
        retryAfter: Duration(minutes: 42),
      ),);
      expect(msg, contains('Daily notification limit reached.'));
      expect(msg, contains('42 min'));
      expect(waitLabel(const Duration(seconds: 3600 + 600)), '1 h 10 min');
      expect(waitLabel(const Duration(seconds: 20)), '20 s');
    });

    test('conflict message asks to reload', () {
      expect(
        announcementErrorMessage(const ApiException(code: 'CONFLICT', message: 'x', status: 409)),
        contains('Reload'),
      );
    });
  });
}

/// Serves [tinyPng] for any request, delegating bookkeeping to [inner].
class _BytesAdapter implements HttpClientAdapter {
  _BytesAdapter(this.inner);
  final ScriptedAdapter inner;

  @override
  Future<ResponseBody> fetch(RequestOptions options, Stream<Uint8List>? requestStream, Future<void>? cancelFuture) async {
    inner.requests.add(options);
    return ResponseBody.fromBytes(tinyPng, 200, headers: {
      Headers.contentTypeHeader: ['image/png'],
    },);
  }

  @override
  void close({bool force = false}) {}
}

/// Answers with [body] until [offline] flips, then fails like a dead network.
class _SwitchAdapter implements HttpClientAdapter {
  _SwitchAdapter(this.offline, this.body);
  final bool Function() offline;
  final Map<String, Object?> body;

  @override
  Future<ResponseBody> fetch(RequestOptions options, Stream<Uint8List>? requestStream, Future<void>? cancelFuture) async {
    if (offline()) {
      throw DioException.connectionError(requestOptions: options, reason: 'offline');
    }
    return ResponseBody.fromString(
      jsonEncode(body),
      200,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}
