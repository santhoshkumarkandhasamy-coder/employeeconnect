import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/files/share_file_opener.dart';
import 'package:share_plus/share_plus.dart';
import 'package:employee_connect/features/auth/domain/auth_user.dart';
import 'package:employee_connect/features/auth/presentation/auth_controller.dart';
import 'package:employee_connect/features/auth/presentation/auth_state.dart';
import 'package:employee_connect/features/export/data/export_repository.dart';
import 'package:employee_connect/features/export/domain/export_models.dart';
import 'package:employee_connect/features/export/presentation/export_screen.dart';
import 'package:employee_connect/features/food/data/food_repository.dart';
import 'package:employee_connect/features/food/presentation/food_providers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class _Auth extends AuthController {
  _Auth(this.perms);
  final List<String> perms;
  @override
  AuthState build() => AuthAuthenticated(AuthUser(
        id: 'u1',
        employeeId: 'HR001',
        fullName: 'Hema',
        role: 'hr',
        permissions: perms,
        mustChangePassword: false,
      ),);
}

class _Repo implements ExportRepository {
  final calls = <Map<String, Object?>>[];
  ApiException? error;

  @override
  Future<List<DepartmentOption>> departments() async => const [DepartmentOption('d1', 'Sales')];

  @override
  Future<({File file, String filename})> download({
    required ExportType type,
    required DateTime from,
    required DateTime to,
    required ExportStore store,
    String? departmentId,
    String? employeeId,
    bool includeReasons = false,
    Map<String, String> extraQuery = const {},
    CancelToken? cancelToken,
    int maxBytes = exportMaxBytes,
    void Function(int received, int total)? onProgress,
  }) async {
    calls.add({
      'type': type.apiValue,
      'from': from,
      'to': to,
      'emp': employeeId,
      'reasons': includeReasons,
    });
    if (error != null) throw error!;
    final f = store.reserve('r.xlsx')..writeAsBytesSync([1, 2, 3]);
    return (file: f, filename: 'r.xlsx');
  }
}

class _Opener implements FileOpener {
  String? opened;
  bool existedWhenOpened = false;
  OpenResult result = OpenResult.handedOver;
  @override
  Future<OpenResult> open(String path) async {
    opened = path;
    existedWhenOpened = File(path).existsSync();
    return result;
  }
}

/// Adapter that serves a canned streamed response.
class _Adapter implements HttpClientAdapter {
  _Adapter(this.chunks, {this.status = 200, this.headers = const {}});
  final List<List<int>> chunks;
  final int status;
  final Map<String, List<String>> headers;
  bool cancelled = false;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    return ResponseBody(
      Stream.fromIterable([for (final c in chunks) Uint8List.fromList(c)]),
      status,
      headers: headers,
    );
  }

  @override
  void close({bool force = false}) {}
}

ExportRepository _repoWith(_Adapter a) =>
    ExportRepository(Dio(BaseOptions(baseUrl: 'http://x'))..httpClientAdapter = a);

Future<void> _tap(WidgetTester tester, String text) async {
  await tester.tap(find.text(text));
  await tester.pumpAndSettle();
}

const _hr = [
  'export.all',
  'attendance.view_all',
  'request.view_all',
  'late.view_all',
  'food.view_all',
];

void _tall(WidgetTester t) {
  t.view.physicalSize = const Size(800, 2400);
  t.view.devicePixelRatio = 1;
  addTearDown(t.view.reset);
}

void main() {
  group('permissions', () {
    test('needs export permission plus a sheet permission', () {
      expect(canOpenExport(const []), isFalse);
      expect(canOpenExport(const ['food.view_all']), isFalse);
      expect(canOpenExport(const ['export.all']), isFalse);
      expect(canOpenExport(const ['export.team', 'late.view_team']), isTrue);
    });

    test('manager without food sees no Food; all needs every sheet', () {
      const mgr = ['export.team', 'attendance.view_team', 'request.view_team', 'late.view_team'];
      final t = allowedExportTypes(mgr);
      expect(t, contains(ExportType.all));
      expect(t, isNot(contains(ExportType.food)));
      expect(canExportType(const ['export.all', 'late.view_all'], ExportType.all), isFalse);
      expect(allowedExportTypes(_hr), ExportType.values);
    });
  });

  group('range', () {
    test('366 days inclusive is allowed, 367 is not', () {
      final from = DateTime(2026, 1, 1);
      expect(validateExportRange(from, DateTime(2026, 12, 31)), isNull);
      expect(validateExportRange(from, DateTime(2027, 1, 1)), isNull); // 366 days
      expect(validateExportRange(from, DateTime(2027, 1, 2)), isNotNull);
      expect(validateExportRange(from, DateTime(2025, 12, 31)), isNotNull);
      expect(validateExportRange(null, from), isNotNull);
    });

    test('presets', () {
      final p = exportPresets(DateTime(2026, 1, 15));
      expect(p[0].from, DateTime(2026, 1, 1));
      expect(p[1].from, DateTime(2025, 12, 1));
      expect(p[1].to, DateTime(2025, 12, 31));
      expect(rangeDays(p[2].from, p[2].to), 30);
    });

    test('employee id filter', () {
      expect(validateEmployeeIdFilter(''), isNull);
      expect(validateEmployeeIdFilter(' EMP001 '), isNull);
      expect(validateEmployeeIdFilter('=1+1'), isNotNull);
    });
  });

  test('error messages are friendly', () {
    String m(String c, [int? s]) =>
        exportErrorMessage(ApiException(code: c, message: 'raw', status: s));
    expect(m('RANGE_TOO_LARGE'), contains('shorter'));
    expect(m('X', 429), contains('wait'));
    expect(m('RATE_LIMITED'), contains('wait'));
    expect(m('FORBIDDEN'), contains('access'));
    expect(m('NOT_FOUND'), contains('not found'));
    expect(m('TEMPORARILY_UNAVAILABLE'), contains('busy'));
    expect(m('WEIRD'), isNot(contains('raw')));
  });

  test('quota, busy and team-scope 403 messages', () {
    final quota = exportErrorMessage(const ApiException(
      code: 'EXPORT_QUOTA_EXCEEDED',
      message: 'raw',
      status: 429,
      retryAfter: Duration(hours: 5, minutes: 10),
    ),);
    expect(quota, contains('daily export limit'));
    expect(quota, contains('5 hours'));
    expect(
      exportErrorMessage(const ApiException(code: 'EXPORT_QUOTA_EXCEEDED', message: 'raw', status: 429)),
      contains('tomorrow'),
    );
    final busy = exportErrorMessage(const ApiException(
      code: 'TEMPORARILY_UNAVAILABLE',
      message: 'raw',
      status: 503,
      retryAfter: Duration(seconds: 30),
    ),);
    expect(busy, contains('busy'));
    expect(busy, contains('30 seconds'));
    const forbidden = ApiException(code: 'FORBIDDEN', message: 'raw', status: 403);
    expect(exportErrorMessage(forbidden), contains('part of your team'));
    expect(exportErrorMessage(forbidden, includeReasons: true), contains('Include reasons'));
  });

  test('Retry-After header reaches the exception through the streamed error path', () async {
    final r = _repoWith(_Adapter([
      '{"error":{"code":"EXPORT_QUOTA_EXCEEDED","message":"q"}}'.codeUnits,
    ], status: 429, headers: {
      'retry-after': ['3600'],
    },),);
    final dir = Directory.systemTemp.createTempSync('ec_export_ra');
    addTearDown(() => dir.deleteSync(recursive: true));
    await expectLater(
      r.download(
        type: ExportType.leave,
        from: DateTime(2026, 10, 1),
        to: DateTime(2026, 10, 31),
        store: ExportStore(dir),
      ),
      throwsA(isA<ApiException>()
          .having((e) => e.code, 'code', 'EXPORT_QUOTA_EXCEEDED')
          .having((e) => e.retryAfter, 'retryAfter', const Duration(seconds: 3600)),),
    );
  });

  test('include_reasons helpers', () {
    expect(canExportReasons(const ['export.all']), isFalse);
    expect(canExportReasons(const ['export.all', 'export.sensitive']), isTrue);
    expect(exportTypeHasReasons(ExportType.leave), isTrue);
    expect(exportTypeHasReasons(ExportType.all), isTrue);
    expect(exportTypeHasReasons(ExportType.food), isFalse);
    expect(exportTypeHasReasons(ExportType.attendance), isFalse);
    expect(isTeamScopedExport(const ['export.team']), isTrue);
    expect(isTeamScopedExport(const ['export.team', 'export.all']), isFalse);
  });

  test('safeExportName sanitises', () {
    expect(safeExportName('attachment; filename="a/../b c.xlsx"', 'f.xlsx'), 'b_c.xlsx');
    expect(safeExportName(null, 'f.xlsx'), 'f.xlsx');
    expect(safeExportName('attachment; filename="x.exe"', 'f.xlsx'), 'x.exe.xlsx');
  });

  test('error body bytes are decoded into the envelope', () {
    final req = RequestOptions(path: '/admin/export');
    final e = DioException(
      requestOptions: req,
      type: DioExceptionType.badResponse,
      response: Response<dynamic>(
        requestOptions: req,
        statusCode: 422,
        data: '{"error":{"code":"RANGE_TOO_LARGE","message":"big"}}'.codeUnits,
      ),
    );
    expect(ApiException.fromDio(decodeErrorBody(e)).code, 'RANGE_TOO_LARGE');
  });

  group('streaming download (review 08 L8)', () {
    late Directory dir;
    late ExportStore store;
    setUp(() {
      dir = Directory.systemTemp.createTempSync('ec_export_dl');
      store = ExportStore(dir);
    });
    tearDown(() {
      if (dir.existsSync()) dir.deleteSync(recursive: true);
    });

    Future<({File file, String filename})> run(ExportRepository r, {int? max, CancelToken? t}) =>
        r.download(
          type: ExportType.leave,
          from: DateTime(2026, 10, 1),
          to: DateTime(2026, 10, 31),
          store: store,
          maxBytes: max ?? exportMaxBytes,
          cancelToken: t,
        );

    test('writes the stream to the store with a sanitised name', () async {
      final r = _repoWith(_Adapter([
        [1, 2],
        [3, 4, 5],
      ], headers: {
        'content-disposition': ['attachment; filename="../evil name.xlsx"'],
      },),);
      final res = await run(r);
      expect(res.filename, 'evil_name.xlsx');
      expect(res.file.path.startsWith(dir.path), isTrue);
      expect(res.file.readAsBytesSync(), [1, 2, 3, 4, 5]);
    });

    test('aborts above the cap and leaves no partial file', () async {
      final r = _repoWith(_Adapter([
        List.filled(60, 1),
        List.filled(60, 1),
      ]),);
      await expectLater(
        run(r, max: 100),
        throwsA(isA<ApiException>().having((e) => e.code, 'code', 'FILE_TOO_LARGE')),
      );
      expect(dir.listSync(), isEmpty);
    });

    test('declared content-length above the cap is refused before writing', () async {
      final r = _repoWith(_Adapter([
        [1],
      ], headers: {
        'content-length': ['999999'],
      },),);
      await expectLater(
        run(r, max: 100),
        throwsA(isA<ApiException>().having((e) => e.code, 'code', 'FILE_TOO_LARGE')),
      );
      expect(dir.listSync(), isEmpty);
    });

    test('error response is decoded into the API envelope and nothing is written', () async {
      final r = _repoWith(_Adapter([
        '{"error":{"code":"RANGE_TOO_LARGE","message":"big"}}'.codeUnits,
      ], status: 422,),);
      await expectLater(
        run(r),
        throwsA(isA<ApiException>().having((e) => e.code, 'code', 'RANGE_TOO_LARGE')),
      );
      expect(dir.listSync(), isEmpty);
    });

    test('include_reasons is sent only when requested', () async {
      final seen = <RequestOptions>[];
      Dio mk() => Dio(BaseOptions(baseUrl: 'http://x'))
        ..httpClientAdapter = _Adapter([
          [1],
        ])
        ..interceptors.add(InterceptorsWrapper(onRequest: (o, h) {
          seen.add(o);
          h.next(o);
        },),);
      Future<void> go({required bool reasons}) => ExportRepository(mk()).download(
            type: ExportType.leave,
            from: DateTime(2026, 10, 1),
            to: DateTime(2026, 10, 31),
            store: store,
            includeReasons: reasons,
          );
      await go(reasons: false);
      await go(reasons: true);
      expect(seen[0].queryParameters.containsKey('include_reasons'), isFalse);
      expect(seen[1].queryParameters['include_reasons'], 'true');
    });

    test('extraQuery cannot override the fixed parameters', () async {
      late RequestOptions seen;
      final dio = Dio(BaseOptions(baseUrl: 'http://x'))
        ..httpClientAdapter = _Adapter([
          [1],
        ])
        ..interceptors.add(InterceptorsWrapper(onRequest: (o, h) {
          seen = o;
          h.next(o);
        },),);
      await ExportRepository(dio).download(
        type: ExportType.leave,
        from: DateTime(2026, 10, 1),
        to: DateTime(2026, 10, 31),
        store: store,
        extraQuery: const {'type': 'food', 'x_future': '1'},
      );
      expect(seen.queryParameters['type'], 'leave');
      expect(seen.queryParameters['x_future'], '1');
    });
  });

  group('share_plus result mapping and cleanup (review 08 M1 / L7)', () {
    test('only an explicit dismissal counts as dismissed', () {
      expect(openResultFor(ShareResultStatus.success), OpenResult.handedOver);
      expect(openResultFor(ShareResultStatus.unavailable), OpenResult.handedOver);
      expect(openResultFor(ShareResultStatus.dismissed), OpenResult.dismissed);
    });

    test('ShareFileOpener wipes the share copies at once when dismissed, after the grace when shared',
        () async {
      final root = Directory.systemTemp.createTempSync('ec_share');
      addTearDown(() {
        if (root.existsSync()) root.deleteSync(recursive: true);
      });
      final copies = Directory('${root.path}${Platform.pathSeparator}share_plus')..createSync();
      File('${copies.path}${Platform.pathSeparator}r.xlsx').writeAsBytesSync([1]);
      final store = ExportStore(Directory('${root.path}${Platform.pathSeparator}ec_exports'),
          extraDirs: [copies],);

      var status = ShareResultStatus.dismissed;
      final opener = ShareFileOpener(
        store: store,
        copyGrace: const Duration(milliseconds: 50),
        share: (files) async => ShareResult('', status),
      );
      expect(await opener.open('x.xlsx'), OpenResult.dismissed);
      await Future<void>.delayed(const Duration(milliseconds: 10));
      expect(copies.existsSync(), isFalse);

      copies.createSync();
      File('${copies.path}${Platform.pathSeparator}r.xlsx').writeAsBytesSync([1]);
      status = ShareResultStatus.success;
      expect(await opener.open('x.xlsx'), OpenResult.handedOver);
      expect(copies.existsSync(), isTrue, reason: 'receiving app may still read it');
      await Future<void>.delayed(const Duration(milliseconds: 200));
      expect(copies.existsSync(), isFalse);
    });

    test('a failing share is "unavailable"', () async {
      final opener = ShareFileOpener(share: (files) async => throw StateError('no'));
      expect(await opener.open('x.xlsx'), OpenResult.unavailable);
    });

    test('ExportStore.clear also deletes the share_plus copies', () async {
      final root = Directory.systemTemp.createTempSync('ec_share2');
      addTearDown(() {
        if (root.existsSync()) root.deleteSync(recursive: true);
      });
      final exports = Directory('${root.path}${Platform.pathSeparator}ec_exports')..createSync();
      final copies = Directory('${root.path}${Platform.pathSeparator}share_plus')..createSync();
      File('${exports.path}${Platform.pathSeparator}a.xlsx').writeAsBytesSync([1]);
      File('${copies.path}${Platform.pathSeparator}a.xlsx').writeAsBytesSync([1]);
      await ExportStore(exports, extraDirs: [copies]).clear();
      expect(exports.existsSync(), isFalse);
      expect(copies.existsSync(), isFalse);
      expect(root.listSync().where((e) => e.path.endsWith('.xlsx')), isEmpty);
    });
  });

  group('screen', () {
    late Directory dir;
    late _Repo repo;
    late _Opener opener;

    setUp(() {
      dir = Directory.systemTemp.createTempSync('ec_export_ui');
      repo = _Repo();
      opener = _Opener();
    });

    Widget wrap(List<String> perms) => ProviderScope(
          overrides: [
            authControllerProvider.overrideWith(() => _Auth(perms)),
            exportRepositoryProvider.overrideWithValue(repo),
            exportDirProvider.overrideWithValue(dir),
            fileOpenerProvider.overrideWithValue(opener),
            foodClockProvider.overrideWithValue(() => DateTime(2026, 10, 21, 12)),
          ],
          child: const MaterialApp(home: ExportScreen()),
        );

    testWidgets('exports, confirms once, shares, then deletes the file', (tester) async {
      _tall(tester);
      await tester.pumpWidget(wrap(_hr));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Leave'));
      await tester.pump();
      await tester.tap(find.text('Export Leave'));
      // The busy progress bar animates forever, so pumpAndSettle would time out.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));

      // Warning before the file leaves the app (review 08 L7).
      expect(find.text('Share this file?'), findsOneWidget);
      expect(opener.opened, isNull);
      await _tap(tester, 'Continue');

      expect(repo.calls.single['type'], 'leave');
      expect(repo.calls.single['from'], DateTime(2026, 10, 1));
      expect(repo.calls.single['to'], DateTime(2026, 10, 21));
      expect(opener.existedWhenOpened, isTrue);
      expect(File(opener.opened!).existsSync(), isFalse);

      // Second export in the same session: no second warning.
      opener.opened = null;
      await tester.tap(find.text('Export Leave'));
      // The busy progress bar animates forever, so pumpAndSettle would time out.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      expect(find.text('Share this file?'), findsNothing);
      expect(opener.opened, isNotNull);
    });

    testWidgets('cancelling the warning deletes the file and does not share', (tester) async {
      _tall(tester);
      await tester.pumpWidget(wrap(_hr));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(FilledButton));
      // The busy progress bar animates forever, so pumpAndSettle would time out.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      await _tap(tester, 'Cancel');
      expect(opener.opened, isNull);
      expect(dir.existsSync() ? dir.listSync() : const <FileSystemEntity>[], isEmpty);
      expect(find.textContaining('Share cancelled'), findsOneWidget);
    });

    testWidgets('dismissed share sheet deletes the file and says so', (tester) async {
      opener.result = OpenResult.dismissed;
      _tall(tester);
      await tester.pumpWidget(wrap(_hr));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(FilledButton));
      // The busy progress bar animates forever, so pumpAndSettle would time out.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      await _tap(tester, 'Continue');
      expect(opener.opened, isNotNull);
      expect(File(opener.opened!).existsSync(), isFalse);
      expect(find.textContaining('Share cancelled'), findsOneWidget);
    });

    testWidgets('sharing unavailable: file deleted, no path shown, no Copy path', (tester) async {
      opener.result = OpenResult.unavailable;
      _tall(tester);
      await tester.pumpWidget(wrap(_hr));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(FilledButton));
      // The busy progress bar animates forever, so pumpAndSettle would time out.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      await _tap(tester, 'Continue');
      expect(File(opener.opened!).existsSync(), isFalse);
      expect(find.text('Copy path'), findsNothing);
      expect(find.textContaining('not available'), findsOneWidget);
    });

    testWidgets('reasons switch is hidden without export.sensitive', (tester) async {
      _tall(tester);
      await tester.pumpWidget(wrap(_hr));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Leave'));
      await tester.pump();
      expect(find.byKey(const ValueKey('include-reasons')), findsNothing);
      await tester.tap(find.byType(FilledButton));
      // The busy progress bar animates forever, so pumpAndSettle would time out.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      await _tap(tester, 'Continue');
      expect(repo.calls.single['reasons'], isFalse);
    });

    testWidgets('reasons switch: off by default, explains health data, sent when on, hidden for food',
        (tester) async {
      _tall(tester);
      await tester.pumpWidget(wrap([..._hr, 'export.sensitive']));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Leave'));
      await tester.pump();
      final sw = find.byKey(const ValueKey('include-reasons'));
      await tester.ensureVisible(sw);
      expect(sw, findsOneWidget);
      expect(tester.widget<SwitchListTile>(sw).value, isFalse);
      expect(find.textContaining('health'), findsOneWidget);

      await tester.tap(sw);
      await tester.pump();
      expect(tester.widget<SwitchListTile>(sw).value, isTrue);
      await tester.ensureVisible(find.byType(FilledButton));
      await tester.tap(find.byType(FilledButton));
      // The busy progress bar animates forever, so pumpAndSettle would time out.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      await _tap(tester, 'Continue');
      expect(repo.calls.single['reasons'], isTrue);

      // Food has no reason columns: no switch, never sent.
      await tester.ensureVisible(find.text('Food'));
      await tester.tap(find.text('Food'));
      await tester.pump();
      expect(find.byKey(const ValueKey('include-reasons')), findsNothing);
      await tester.ensureVisible(find.byType(FilledButton));
      await tester.tap(find.byType(FilledButton));
      // The busy progress bar animates forever, so pumpAndSettle would time out.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      expect(repo.calls.last['type'], 'food');
      expect(repo.calls.last['reasons'], isFalse);
    });

    testWidgets('quota error shows the wait time', (tester) async {
      repo.error = const ApiException(
        code: 'EXPORT_QUOTA_EXCEEDED',
        message: 'raw',
        status: 429,
        retryAfter: Duration(hours: 3),
      );
      _tall(tester);
      await tester.pumpWidget(wrap(_hr));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(FilledButton));
      // The busy progress bar animates forever, so pumpAndSettle would time out.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      expect(find.textContaining('daily export limit'), findsOneWidget);
      expect(find.textContaining('3 hours'), findsOneWidget);
    });

    testWidgets('invalid employee id blocks the request', (tester) async {
      _tall(tester);
      await tester.pumpWidget(wrap(_hr));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextFormField), '=cmd');
      await tester.ensureVisible(find.byType(FilledButton));
      await tester.tap(find.byType(FilledButton));
      // The busy progress bar animates forever, so pumpAndSettle would time out.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      expect(repo.calls, isEmpty);
      expect(find.textContaining('letters, numbers'), findsOneWidget);
    });

    testWidgets('server error shows a friendly message', (tester) async {
      repo.error = const ApiException(code: 'RANGE_TOO_LARGE', message: 'raw', status: 422);
      _tall(tester);
      await tester.pumpWidget(wrap(_hr));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(FilledButton));
      // The busy progress bar animates forever, so pumpAndSettle would time out.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      expect(find.textContaining('Choose a shorter date range'), findsOneWidget);
      expect(opener.opened, isNull);
    });

    testWidgets('no permission shows empty state', (tester) async {
      _tall(tester);
      await tester.pumpWidget(wrap(const ['export.all']));
      await tester.pumpAndSettle();
      expect(find.text('You do not have access to reports.'), findsOneWidget);
      expect(find.byType(FilledButton), findsNothing);
    });
  });
}
