import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/cache/json_cache.dart';
import 'package:employee_connect/core/files/file_service.dart';
import 'package:employee_connect/features/announcements/data/announcement_file_picker.dart';
import 'package:employee_connect/features/announcements/data/announcement_repository.dart';
import 'package:employee_connect/features/announcements/domain/announcement_models.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart' show Paged;
import 'package:employee_connect/features/auth/domain/auth_user.dart';
import 'package:employee_connect/features/auth/presentation/auth_controller.dart';
import 'package:employee_connect/features/auth/presentation/auth_state.dart';
import 'package:employee_connect/features/food/data/food_repository.dart';
import 'package:employee_connect/features/home/data/home_repository.dart';
import 'package:employee_connect/features/home/domain/home_models.dart';
import 'package:employee_connect/features/profile/data/profile_repository.dart';
import 'package:employee_connect/features/profile/domain/profile_models.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

/// 1x1 PNG.
final Uint8List tinyPng = base64Decode(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
);

final Uint8List tinyPdf = Uint8List.fromList(utf8.encode('%PDF-1.4\n%%EOF\n'));

class FakeAuth extends AuthController {
  FakeAuth(this.user);
  final AuthUser user;
  @override
  AuthState build() => AuthAuthenticated(user);

  int refreshCalls = 0;

  @override
  Future<void> refreshUser() async {
    refreshCalls++;
  }
}

AuthUser userWith(List<String> permissions, {String role = 'employee'}) => AuthUser(
      id: 'u1',
      employeeId: 'EMP001',
      fullName: 'Asha Rao',
      role: role,
      permissions: permissions,
      mustChangePassword: false,
    );

Widget app(Widget child, List<Override> overrides, {ThemeMode mode = ThemeMode.light}) => ProviderScope(
      key: UniqueKey(), // a fresh scope per call, so re-pumping with other overrides starts clean
      overrides: overrides,
      child: MaterialApp(
        themeMode: mode,
        theme: ThemeData(useMaterial3: true),
        darkTheme: ThemeData(useMaterial3: true, brightness: Brightness.dark),
        home: child,
      ),
    );

// ---- files ---------------------------------------------------------------------

class FakeFileService extends FileService {
  FakeFileService() : super(Dio());
  Uint8List data = tinyPng;
  final List<String> requested = [];

  @override
  Future<Uint8List> bytes(String url) async {
    requested.add(url);
    return data;
  }

  @override
  Future<String> upload(String path, {required String purpose, String? filename}) async => 'file-1';
}

/// ExportStore that never touches the disk (real file IO does not complete
/// inside `testWidgets` without runAsync).
class MemExportStore extends ExportStore {
  MemExportStore() : super(Directory('mem'));
  final Map<String, Uint8List> saved = {};
  int clears = 0;

  @override
  Future<File> save(Uint8List bytes, String filename) async {
    saved[filename] = bytes;
    return File('mem/$filename');
  }

  @override
  Future<void> clear() async => clears++;
}

class FakeOpener implements FileOpener {
  FakeOpener({this.result = true, this.dismissed = false});

  /// false = sharing unavailable.
  bool result;

  /// true = the user closed the share sheet (wins over [result]).
  bool dismissed;
  final List<String> opened = [];

  @override
  Future<OpenResult> open(String path) async {
    opened.add(path);
    if (dismissed) return OpenResult.dismissed;
    return result ? OpenResult.handedOver : OpenResult.unavailable;
  }
}

class FakePicker implements AnnouncementFilePicker {
  PickedFile? image;
  PickedFile? pdf;

  @override
  Future<PickedFile?> pickImage() async => image;
  @override
  Future<PickedFile?> pickPdf() async => pdf;
}

// ---- announcements ---------------------------------------------------------------

Announcement ann(
  String id, {
  String title = 'Title',
  String body = 'Body text',
  AnnouncementPriority priority = AnnouncementPriority.normal,
  bool isRead = false,
  String? imageUrl,
  String? pdfUrl,
  bool targetAll = true,
  List<String> departmentIds = const [],
  DateTime? publishedAt,
  DateTime? expiresAt,
  int? version,
}) =>
    Announcement(
      id: id,
      title: title,
      body: body,
      priority: priority,
      isRead: isRead,
      imageUrl: imageUrl,
      pdfUrl: pdfUrl,
      targetAll: targetAll,
      departmentIds: departmentIds,
      publishedAt: publishedAt ?? DateTime.now().subtract(const Duration(hours: 3)),
      expiresAt: expiresAt,
      version: version,
    );

class FakeAnnouncementRepo extends AnnouncementRepository {
  FakeAnnouncementRepo(this.items)
      : super(Dio(), MemoryJsonCache(), FileService(Dio()));

  List<Announcement> items;
  int unread = 0;
  int pageSize = 20;
  bool stale = false;
  ApiException? listError;
  ApiException? detailError;
  ApiException? saveError;
  List<Department> deps = const [Department(id: 'd1', name: 'IT'), Department(id: 'd2', name: 'HR')];
  final List<String> readIds = [];
  final List<String> deleted = [];
  final List<Map<String, Object?>> created = [];
  final List<(String, Map<String, Object?>)> updated = [];
  final List<int?> updatedVersions = [];
  final List<({String path, bool pdf})> uploads = [];
  final List<int> listPages = [];

  @override
  Future<Cached<Paged<Announcement>>> list({
    int page = 1,
    int pageSize = 20,
    bool unreadOnly = false,
  }) async {
    listPages.add(page);
    final e = listError;
    if (e != null) throw e;
    final size = this.pageSize < pageSize ? this.pageSize : pageSize;
    final src = unreadOnly ? [for (final a in items) if (!a.isRead) a] : items;
    final from = (page - 1) * size;
    final slice = from >= src.length ? <Announcement>[] : src.skip(from).take(size).toList();
    return Cached(Paged(items: slice, page: page, total: src.length, pageSize: size), stale: stale);
  }

  @override
  Future<int> unreadCount() async => unread;

  @override
  Future<Announcement> detail(String id) async {
    final e = detailError;
    if (e != null) throw e;
    return items.firstWhere((a) => a.id == id);
  }

  @override
  Future<void> markRead(String id) async => readIds.add(id);

  @override
  Future<Paged<Announcement>> adminList({int page = 1}) async {
    final e = listError;
    if (e != null) throw e;
    return Paged(items: items, page: 1, total: items.length, pageSize: 20);
  }

  @override
  Future<Announcement> create(Map<String, Object?> body) async {
    final e = saveError;
    if (e != null) throw e;
    created.add(body);
    return ann('new', title: '${body['title']}');
  }

  @override
  Future<Announcement> update(String id, Map<String, Object?> body, {int? version}) async {
    final e = saveError;
    if (e != null) throw e;
    updated.add((id, body));
    updatedVersions.add(version);
    return ann(id, title: '${body['title']}');
  }

  @override
  Future<void> delete(String id) async {
    deleted.add(id);
    items = [for (final a in items) if (a.id != id) a];
  }

  @override
  Future<ReadStats> reads(String id, {int page = 1}) async => const ReadStats(
        readers: [],
        readCount: 0,
        targetCount: 0,
        page: 1,
        hasMore: false,
      );

  @override
  Future<List<Department>> departments() async => deps;

  @override
  Future<String> uploadAttachment(String path, {required bool pdf, String? filename}) async {
    uploads.add((path: path, pdf: pdf));
    return pdf ? '11111111-aaaa-bbbb-cccc-000000000001' : '22222222-aaaa-bbbb-cccc-000000000002';
  }
}

// ---- profile -----------------------------------------------------------------------

Profile sampleProfile({
  String? dob = '1995-04-12',
  String? phone = '+919999999999',
  bool hidden = false,
  EmergencyContact? emergency,
  bool showCelebrations = false,
}) =>
    Profile(
      showCelebrations: showCelebrations,
      userId: 'u1',
      employeeId: 'EMP001',
      fullName: 'Asha Rao',
      department: 'IT',
      officialEmail: 'asha@corp.com',
      dob: dob,
      phone: phone,
      personalEmail: 'asha@gmail.com',
      address: '12 MG Road',
      bloodGroup: 'O+',
      emergency: emergency,
      detailsHidden: hidden,
    );

class FakeProfileRepo extends ProfileRepository {
  FakeProfileRepo(this.current, {this.stale = false}) : super(Dio(), MemoryJsonCache());

  Profile current;
  bool stale;
  ApiException? saveError;
  ApiException? loadError;
  final List<Map<String, Object?>> updates = [];
  int photoUploads = 0;

  @override
  Future<Cached<Profile>> profile() async {
    final e = loadError;
    if (e != null) throw e;
    return Cached(stale ? current.copyWith(detailsHidden: true) : current, stale: stale);
  }

  @override
  Future<Profile> update(Map<String, Object?> body) async {
    final e = saveError;
    if (e != null) throw e;
    updates.add(body);
    final sc = body['show_celebrations'];
    if (sc is bool) current = current.copyWith(showCelebrations: sc);
    return current;
  }

  @override
  Future<Profile?> uploadPhoto(String path, {String? filename}) async {
    photoUploads++;
    return current;
  }
}

// ---- home --------------------------------------------------------------------------

class FakeHomeRepo extends HomeRepository {
  FakeHomeRepo(this.data) : super(Dio(), MemoryJsonCache());

  HomeData data;
  bool stale = false;
  ApiException? error;
  Future<void>? gate;

  @override
  Future<Cached<HomeData>> home() async {
    await gate;
    final e = error;
    if (e != null) throw e;
    return Cached(data, stale: stale);
  }
}

HomeData sampleHome({DateTime? now, bool canChangeFood = true}) {
  final n = now ?? DateTime(2026, 10, 21, 10);
  String d(int plus) => '${n.year}-${n.month.toString().padLeft(2, '0')}-${(n.day + plus).toString().padLeft(2, '0')}';
  return HomeData.fromJson({
    'today_attendance': {'check_in_at': '2026-10-21T04:00:00Z', 'check_out_at': null, 'status': 'present'},
    'leave_balances': [
      {'leave_type': 'Casual', 'remaining': 8.0},
      {'leave_type': 'Sick', 'remaining': 7.5},
    ],
    'permission_remaining': {'count': 2, 'minutes': 180},
    'unread_announcements': 3,
    'pending_requests': 1,
    'food_today': {'date': d(0), 'need_food': null, 'can_change': canChangeFood},
    'celebrations': [
      {
        'user': {'id': 'c2', 'employee_id': 'E2', 'full_name': 'Ravi Kumar', 'department': 'HR', 'photo_url': null},
        'type': 'anniversary',
        'date': d(2),
        'years': 3,
      },
      {
        'user': {'id': 'c1', 'employee_id': 'E1', 'full_name': 'Meera Nair', 'department': 'IT', 'photo_url': null},
        'type': 'birthday',
        'date': d(0),
      },
    ],
  });
}
