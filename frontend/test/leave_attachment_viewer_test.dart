import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/files/file_service.dart';
import 'package:employee_connect/features/leave/domain/leave_models.dart';
import 'package:employee_connect/features/leave/presentation/widgets/attachment_viewer.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class _FakeFiles extends FileService {
  _FakeFiles(this.error) : super(Dio());
  final ApiException? error;
  final urls = <String>[];

  @override
  Future<Uint8List> bytes(String url) async {
    urls.add(url);
    final e = error;
    if (e != null) throw e;
    return Uint8List.fromList([0x25, 0x50, 0x44, 0x46]);
  }
}

Widget _wrap(_FakeFiles files) => ProviderScope(
      overrides: [fileServiceProvider.overrideWithValue(files)],
      child: const MaterialApp(
        home: Scaffold(
          body: AttachmentButton(
            item: RequestItem(
              kind: RequestKind.leave,
              id: 'r1',
              status: RequestStatus.pending,
              attachmentUrl: '/api/files/abcdef12-3456',
              attachmentContentType: 'image/png',
            ),
          ),
        ),
      ),
    );

void main() {
  testWidgets('404 shows a friendly message', (t) async {
    final files = _FakeFiles(const ApiException(code: 'NOT_FOUND', message: 'x', status: 404));
    await t.pumpWidget(_wrap(files));
    await t.tap(find.text('View document'));
    await t.pumpAndSettle();
    expect(files.urls, ['/api/files/abcdef12-3456']);
    expect(find.textContaining('no longer available'), findsOneWidget);
  });
}
